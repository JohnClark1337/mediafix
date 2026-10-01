import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from queue import Empty, Queue

from mediafix import downmix as downmix_mod
from mediafix import subtitles as sub_mod
from mediafix.probe import MediaInfo
from mediafix.scan import MediaItem

JOB_QUEUED = "queued"
JOB_RUNNING = "running"
JOB_DONE = "done"
JOB_FAILED = "failed"
JOB_SKIPPED = "skipped"
JOB_CANCELLED = "cancelled"

KIND_SUBTITLE = "subtitle"
KIND_DOWNMIX = "downmix"

TERMINAL_STATES = {JOB_DONE, JOB_FAILED, JOB_SKIPPED, JOB_CANCELLED}

_SUCCESS = {"processed"}


@dataclass
class Job:
    kind: str
    path: str
    item: MediaItem
    state: str = JOB_QUEUED
    message: str = ""
    progress: float = 0.0
    started: float | None = None
    finished: float | None = None

    @property
    def label(self) -> str:
        return os.path.basename(self.path)

    @property
    def elapsed(self) -> float:
        if self.started is None:
            return 0.0
        end = self.finished if self.finished is not None else time.monotonic()
        return max(0.0, end - self.started)


@dataclass
class Summary:
    total: int = 0
    done: int = 0
    failed: int = 0
    skipped: int = 0
    cancelled: int = 0
    elapsed: float = 0.0
    failures: list[tuple[str, str, str]] = field(default_factory=list)

    @property
    def exit_code(self) -> int:
        return 1 if self.failed else 0


class Runner:
    def __init__(self, config, dry_run: bool = False, force: bool = False, keep_going: bool = True):
        self.config = config
        self.dry_run = dry_run
        self.force = force
        self.keep_going = keep_going
        self.events: Queue = Queue()
        self.cancel_event = threading.Event()
        self.jobs: list[Job] = []
        self._lock = threading.Lock()
        self._done = threading.Event()
        self._abort_reason: str | None = None

    def plan(self, items) -> list[Job]:
        jobs: list[Job] = []
        for item in items:
            if not item.selected:
                continue
            if item.want_subtitle:
                jobs.append(Job(KIND_SUBTITLE, item.path, item))
            if item.want_audio:
                jobs.append(Job(KIND_DOWNMIX, item.path, item))
        self.jobs = jobs
        return jobs

    def _emit(self, job: Job, state: str | None = None, message: str | None = None,
              progress: float | None = None) -> None:
        if state is not None:
            job.state = state
            if state == JOB_RUNNING and job.started is None:
                job.started = time.monotonic()
            if state in TERMINAL_STATES:
                job.finished = time.monotonic()
                if state == JOB_DONE:
                    job.progress = 1.0
        if message is not None:
            job.message = message
        if progress is not None:
            job.progress = max(0.0, min(1.0, progress))
        self.events.put((job, job.state, job.message, job.progress))

    def cancel(self) -> None:
        self.cancel_event.set()
        self.events.put((None, "cancelling", "stopping after the current job", 0.0))

    @property
    def cancelled(self) -> bool:
        return self.cancel_event.is_set()

    def _next_pending(self, kind: str) -> Job | None:
        with self._lock:
            for job in self.jobs:
                if job.kind == kind and job.state == JOB_QUEUED:
                    return job
        return None

    def _subtitle_loop(self, engine) -> None:
        while not self.cancel_event.is_set():
            job = self._next_pending(KIND_SUBTITLE)
            if job is None:
                break
            self._emit(job, JOB_RUNNING, "starting")
            result = sub_mod.run_subtitle_job(
                engine, job.item, self.config,
                progress=lambda fraction, message, _job=job: self._emit(_job, progress=fraction, message=message),
                cancel=self.cancel_event,
            )
            if result.error:
                self._emit(job, JOB_FAILED, result.error)
                if result.fatal:
                    self._abort_reason = result.error
                    self.events.put((None, "aborted", f"subtitle pipeline stopped: {result.error}", 0.0))
                    break
                continue
            if result.cancelled:
                self._emit(job, JOB_CANCELLED, "cancelled")
            else:
                self._emit(job, JOB_DONE, f"{result.segments} cues -> {os.path.basename(result.path)}")

    def _downmix_job(self, job: Job, args) -> None:
        if self.cancel_event.is_set():
            self._emit(job, JOB_CANCELLED, "cancelled")
            return

        shortage = downmix_mod.check_space(job.path, job.item.size, self.config.free_space_margin_gb)
        if shortage:
            self._emit(job, JOB_FAILED, shortage)
            return

        self._emit(job, JOB_RUNNING, "starting")
        status, message = downmix_mod.run_one(
            args, job.path, cancel=self.cancel_event,
            progress=lambda fraction, note, _job=job: self._emit(
                _job, progress=fraction, message=f"{note or 'processing'}"
            ),
            total_bytes=job.item.size,
        )
        if status == "failed":
            self._emit(job, JOB_FAILED, message)
        elif status == "skip-has-stereo":
            self._emit(job, JOB_SKIPPED, message)
        elif status in ("skip-no-audio", "unchanged"):
            self._emit(job, JOB_SKIPPED, message)
        else:
            suffix = " [dry-run]" if self.dry_run else ""
            self._emit(job, JOB_DONE, f"{message}{suffix}")

    def run(self, on_ready=None) -> Summary:
        summary = Summary(total=len(self.jobs))
        if not self.jobs:
            self._done.set()
            return summary

        started = time.monotonic()
        args = downmix_mod.build_args(self.config, force=self.force, dry_run=self.dry_run)

        engine = None
        if any(job.kind == KIND_SUBTITLE for job in self.jobs):
            from mediafix.subtitles import SubtitleEngine

            engine = SubtitleEngine(self.config)
            if on_ready:
                on_ready("loading subtitle model")

        subtitle_thread = None
        if engine is not None and not self.cancel_event.is_set():
            subtitle_thread = threading.Thread(
                target=self._safe_subtitles, args=(engine,), daemon=True,
                name="mediafix-subtitles",
            )
            subtitle_thread.start()

        downmix_jobs = [job for job in self.jobs if job.kind == KIND_DOWNMIX]
        pool = ThreadPoolExecutor(max_workers=max(1, self.config.downmix_jobs))
        try:
            if downmix_jobs:
                futures = [pool.submit(self._downmix_job, job, args) for job in downmix_jobs]
                for future in futures:
                    future.result()
        finally:
            pool.shutdown(wait=True)
            if subtitle_thread is not None:
                subtitle_thread.join()

        summary.elapsed = time.monotonic() - started
        for job in self.jobs:
            if job.state == JOB_QUEUED:
                if self._abort_reason:
                    self._emit(job, JOB_SKIPPED, f"pipeline aborted: {self._abort_reason}")
                else:
                    self._emit(job, JOB_CANCELLED, "not run")
        self._done.set()
        for job in self.jobs:
            if job.state == JOB_DONE:
                summary.done += 1
            elif job.state == JOB_FAILED:
                summary.failed += 1
                summary.failures.append((job.kind, job.label, job.message))
            elif job.state == JOB_CANCELLED:
                summary.cancelled += 1
            elif job.state == JOB_SKIPPED:
                summary.skipped += 1
            else:
                summary.failed += 1
                summary.failures.append((job.kind, job.label, job.message or "did not run"))
        return summary

    def _safe_subtitles(self, engine) -> None:
        try:
            self._subtitle_loop(engine)
        except Exception as exc:  # noqa: BLE001
            self.events.put((None, "error", f"subtitle pipeline crashed: {type(exc).__name__}: {exc}", 0.0))

    def wait(self, timeout: float | None = None) -> bool:
        return self._done.wait(timeout)