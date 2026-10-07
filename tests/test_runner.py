import sys
import threading
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mediafix import config as config_mod
from mediafix import runner as runner_mod
from mediafix.censor import CensorResult
from mediafix.scan import MediaItem
from mediafix.subtitles import SubtitleResult


def item(path="Movie.mkv", sub=True, audio=True, censor=False, selected=True, size=1024):
    return MediaItem(
        path=path, size=size, duration=60.0, needs_subtitle=sub, needs_audio=audio,
        needs_censor=censor, selectable=sub or audio or censor, selected=selected,
        want_subtitle=sub and selected, want_audio=audio and selected,
        want_censor=censor and selected,
    )


class PlanTests(unittest.TestCase):
    def setUp(self):
        self.config = config_mod.Config()

    def test_both_kinds(self):
        jobs = runner_mod.Runner(self.config).plan([item()])
        self.assertEqual([j.kind for j in jobs],
                         [runner_mod.KIND_SUBTITLE, runner_mod.KIND_DOWNMIX])
        self.assertTrue(all(j.state == runner_mod.JOB_QUEUED for j in jobs))

    def test_subtitle_then_censor_then_downmix(self):
        jobs = runner_mod.Runner(self.config).plan([item(censor=True)])
        self.assertEqual([j.kind for j in jobs],
                         [runner_mod.KIND_SUBTITLE, runner_mod.KIND_CENSOR, runner_mod.KIND_DOWNMIX])

    def test_censor_without_downmix(self):
        jobs = runner_mod.Runner(self.config).plan([item(sub=True, audio=False, censor=True)])
        self.assertEqual([j.kind for j in jobs],
                         [runner_mod.KIND_SUBTITLE, runner_mod.KIND_CENSOR])

    def test_subtitle_only_with_censor_needed_is_fine(self):
        jobs = runner_mod.Runner(self.config).plan([item(audio=False, censor=True)])
        self.assertIn(runner_mod.KIND_SUBTITLE, [j.kind for j in jobs])
        self.assertIn(runner_mod.KIND_CENSOR, [j.kind for j in jobs])

    def test_only_selected_items(self):
        jobs = runner_mod.Runner(self.config).plan([item("a.mkv", selected=False)])
        self.assertEqual(jobs, [])

    def test_subtitle_only(self):
        jobs = runner_mod.Runner(self.config).plan([item(audio=False)])
        self.assertEqual([j.kind for j in jobs], [runner_mod.KIND_SUBTITLE])

    def test_downmix_only(self):
        jobs = runner_mod.Runner(self.config).plan([item(sub=False)])
        self.assertEqual([j.kind for j in jobs], [runner_mod.KIND_DOWNMIX])

    def test_nothing_selected_is_empty(self):
        self.assertEqual(runner_mod.Runner(self.config).plan([]), [])


class RunTests(unittest.TestCase):
    def setUp(self):
        self.config = replace(config_mod.Config(), downmix_jobs=2)

    def _run(self, items, sub_result=None, downmix_result=("processed", "1 track(s)"),
             censor_result=None):
        runner = runner_mod.Runner(self.config)
        runner.plan(items)
        sub_result = sub_result or SubtitleResult("Movie.eng.srt", 12, "en", 60.0)
        censor_result = censor_result or CensorResult("Movie.mkv", segments=1, model_hits=1)
        with mock.patch("mediafix.subtitles.run_subtitle_job", return_value=sub_result), \
             mock.patch("mediafix.censor.run_censor_job", return_value=censor_result), \
             mock.patch("mediafix.downmix.run_one", return_value=downmix_result), \
             mock.patch("mediafix.downmix.check_space", return_value=None):
            summary = runner.run()
        return runner, summary

    def test_all_success(self):
        runner, summary = self._run([item()])
        self.assertEqual(summary.total, 2)
        self.assertEqual(summary.done, 2)
        self.assertEqual(summary.failed, 0)
        self.assertEqual(summary.exit_code, 0)
        states = {j.kind: j.state for j in runner.jobs}
        self.assertEqual(states[runner_mod.KIND_SUBTITLE], runner_mod.JOB_DONE)
        self.assertEqual(states[runner_mod.KIND_DOWNMIX], runner_mod.JOB_DONE)

    def test_censor_all_success(self):
        runner, summary = self._run([item(censor=True)])
        self.assertEqual(summary.total, 3)
        self.assertEqual(summary.done, 3)
        states = [j.state for j in runner.jobs]
        self.assertEqual(states, [runner_mod.JOB_DONE] * 3)

    def test_censor_skip_lets_downmix_run(self):
        skipped = CensorResult("Movie.mkv", skipped=True, message="no profanity found")
        runner, summary = self._run([item(sub=False, censor=True)],
                                    censor_result=skipped)
        self.assertEqual(summary.skipped, 1)
        self.assertEqual(summary.done, 1)
        states = {j.kind: j.state for j in runner.jobs}
        self.assertEqual(states[runner_mod.KIND_CENSOR], runner_mod.JOB_SKIPPED)
        self.assertEqual(states[runner_mod.KIND_DOWNMIX], runner_mod.JOB_DONE)

    def test_censor_failure_blocks_downmix(self):
        failed = CensorResult("Movie.mkv", error="ffmpeg out of space")
        runner, summary = self._run([item(sub=False, censor=True, audio=True)],
                                    censor_result=failed)
        self.assertEqual(summary.failed, 1)
        self.assertEqual(summary.skipped, 1)
        states = {j.kind: j.state for j in runner.jobs}
        self.assertEqual(states[runner_mod.KIND_CENSOR], runner_mod.JOB_FAILED)
        self.assertEqual(states[runner_mod.KIND_DOWNMIX], runner_mod.JOB_SKIPPED)
        downmix_note = [j.message for j in runner.jobs if j.kind == runner_mod.KIND_DOWNMIX][0]
        self.assertIn("previous censor", downmix_note)

    def test_censor_skips_when_subtitle_fails(self):
        quiet = SubtitleResult("Movie.eng.srt", 0, "en", 0.0, error="no speech detected")
        runner, summary = self._run([item(censor=True)], sub_result=quiet)
        self.assertEqual(summary.failed, 1)
        self.assertEqual(summary.skipped, 2)
        states = {j.kind: j.state for j in runner.jobs}
        self.assertEqual(states[runner_mod.KIND_SUBTITLE], runner_mod.JOB_FAILED)
        self.assertEqual(states[runner_mod.KIND_CENSOR], runner_mod.JOB_SKIPPED)
        self.assertEqual(states[runner_mod.KIND_DOWNMIX], runner_mod.JOB_SKIPPED)
        censor_note = [j.message for j in runner.jobs if j.kind == runner_mod.KIND_CENSOR][0]
        self.assertIn("upstream subtitle", censor_note)

    def test_fatal_subtitle_aborts_censor_and_downmix(self):
        fatal = SubtitleResult("Movie.eng.srt", 0, "en", 0.0,
                               error="model load failed", fatal=True)
        runner, summary = self._run([item(censor=True)], sub_result=fatal)
        self.assertEqual(summary.failed, 1)
        self.assertEqual(summary.skipped, 2)
        states = {j.kind: j.state for j in runner.jobs}
        self.assertEqual(states[runner_mod.KIND_SUBTITLE], runner_mod.JOB_FAILED)
        self.assertEqual(states[runner_mod.KIND_CENSOR], runner_mod.JOB_SKIPPED)
        self.assertEqual(states[runner_mod.KIND_DOWNMIX], runner_mod.JOB_SKIPPED)
        censor_note = [j.message for j in runner.jobs if j.kind == runner_mod.KIND_CENSOR][0]
        self.assertIn("pipeline aborted", censor_note)

    def test_fatal_subtitle_error_is_not_reported_as_cancelled(self):
        fatal = SubtitleResult("Movie.eng.srt", 0, "en", 0.0,
                               error="model load failed: ModuleNotFoundError", fatal=True)
        runner, summary = self._run([item(audio=False)], sub_result=fatal)
        self.assertEqual(summary.failed, 1)
        self.assertEqual(summary.cancelled, 0)
        self.assertEqual(summary.exit_code, 1)
        self.assertEqual(runner.jobs[0].state, runner_mod.JOB_FAILED)

    def test_fatal_error_skips_the_rest_of_the_library(self):
        items = [item(f"{n}.mkv", audio=False) for n in range(5)]
        fatal = SubtitleResult("x.eng.srt", 0, "en", 0.0,
                               error="model load failed", fatal=True)
        runner, summary = self._run(items, sub_result=fatal)
        self.assertEqual(summary.failed, 1)
        self.assertEqual(summary.skipped, 4)
        self.assertEqual(summary.exit_code, 1)
        self.assertTrue(all(
            "pipeline aborted" in job.message
            for job in runner.jobs if job.state == runner_mod.JOB_SKIPPED
        ))

    def test_per_file_error_does_not_stop_the_pipeline(self):
        items = [item(f"{n}.mkv", audio=False) for n in range(3)]
        quiet = SubtitleResult("x.eng.srt", 0, "en", 0.0, error="no speech detected")
        _runner, summary = self._run(items, sub_result=quiet)
        self.assertEqual(summary.failed, 3)
        self.assertEqual(summary.skipped, 0)

    def test_subtitle_failure_is_recorded(self):
        bad = SubtitleResult("Movie.eng.srt", 0, "en", 0.0, error="ffprobe boom")
        runner, summary = self._run([item(audio=False)], sub_result=bad)
        self.assertEqual(summary.failed, 1)
        self.assertEqual(summary.exit_code, 1)
        self.assertIn("ffprobe boom", summary.failures[0][2])

    def test_downmix_failure_is_recorded(self):
        runner, summary = self._run([item(sub=False)],
                                    downmix_result=("failed", "ffmpeg exited 1"))
        self.assertEqual(summary.failed, 1)
        self.assertEqual(summary.failures[0][0], runner_mod.KIND_DOWNMIX)

    def test_skip_is_not_a_failure(self):
        runner, summary = self._run([item(sub=False)],
                                    downmix_result=("skip-has-stereo", "already has stereo"))
        self.assertEqual(summary.skipped, 1)
        self.assertEqual(summary.failed, 0)
        self.assertEqual(summary.exit_code, 0)

    def test_space_check_failure_blocks_downmix(self):
        runner = runner_mod.Runner(self.config)
        runner.plan([item(sub=False)])
        with mock.patch("mediafix.subtitles.run_subtitle_job"), \
             mock.patch("mediafix.downmix.check_space", return_value="insufficient free space"), \
             mock.patch("mediafix.downmix.run_one") as run_one:
            summary = runner.run()
        run_one.assert_not_called()
        self.assertEqual(summary.failed, 1)
        self.assertIn("insufficient free space", summary.failures[0][2])

    def test_empty_plan_is_success(self):
        runner = runner_mod.Runner(self.config)
        summary = runner.run()
        self.assertEqual(summary.total, 0)
        self.assertEqual(summary.exit_code, 0)

    def test_both_kinds_on_several_files(self):
        items = [item("a.mkv"), item("b.mkv", sub=False), item("c.mkv", audio=False)]
        runner, summary = self._run(items)
        self.assertEqual(summary.total, 4)
        self.assertEqual(summary.done, 4)
        kinds = sorted(j.kind for j in runner.jobs)
        self.assertEqual(kinds, ["downmix", "downmix", "subtitle", "subtitle"])

    def test_cancel_marks_remaining_jobs(self):
        runner = runner_mod.Runner(self.config)
        runner.plan([item("a.mkv"), item("b.mkv")])
        runner.cancel()
        with mock.patch("mediafix.subtitles.run_subtitle_job",
                        return_value=SubtitleResult("x.srt", 1, "en", 1.0)), \
             mock.patch("mediafix.downmix.run_one", return_value=("processed", "ok")), \
             mock.patch("mediafix.downmix.check_space", return_value=None):
            summary = runner.run()
        self.assertEqual(summary.total, 4)
        self.assertEqual(summary.cancelled, 4)
        self.assertEqual(summary.failed, 0)
        self.assertEqual(summary.done, 0)

    def test_progress_is_monotonic_and_bounded(self):
        runner = runner_mod.Runner(self.config)
        runner.plan([item(sub=False)])
        for job in runner.jobs:
            runner._emit(job, progress=5.0)
            self.assertEqual(job.progress, 1.0)
            runner._emit(job, progress=-3.0)
            self.assertEqual(job.progress, 0.0)

    def test_events_reach_the_queue(self):
        runner = runner_mod.Runner(self.config)
        job = runner_mod.Job(runner_mod.KIND_DOWNMIX, "a.mkv", item(sub=False))
        runner.jobs = [job]
        runner._emit(job, runner_mod.JOB_RUNNING, "starting")
        emitted_job, state, message, _ = runner.events.get(timeout=1)
        self.assertIs(emitted_job, job)
        self.assertEqual(state, runner_mod.JOB_RUNNING)
        self.assertEqual(message, "starting")
        self.assertIsNotNone(job.started)

    def test_terminal_state_stamps_finish_time(self):
        runner = runner_mod.Runner(self.config)
        job = runner_mod.Job(runner_mod.KIND_DOWNMIX, "a.mkv", item(sub=False))
        runner.jobs = [job]
        runner._emit(job, runner_mod.JOB_RUNNING, "starting")
        runner._emit(job, runner_mod.JOB_DONE, "ok")
        self.assertIsNotNone(job.finished)
        self.assertEqual(job.progress, 1.0)
        self.assertGreaterEqual(job.elapsed, 0.0)

    def test_parallel_downmix_workers_do_not_exceed_limit(self):
        config = replace(config_mod.Config(), downmix_jobs=2)
        items = [item(f"{n}.mkv", sub=False) for n in range(6)]
        runner = runner_mod.Runner(config)
        runner.plan(items)

        lock = threading.Lock()
        active = {"now": 0, "peak": 0}

        def slow_run_one(args, path, cancel=None, progress=None, total_bytes=None):
            with lock:
                active["now"] += 1
                active["peak"] = max(active["peak"], active["now"])
            threading.Event().wait(0.05)
            with lock:
                active["now"] -= 1
            return "processed", "ok"

        with mock.patch("mediafix.downmix.check_space", return_value=None), \
             mock.patch("mediafix.downmix.run_one", side_effect=slow_run_one):
            summary = runner.run()

        self.assertEqual(summary.done, 6)
        self.assertLessEqual(active["peak"], 2)

    def test_subtitle_exception_is_a_per_file_failure_not_a_crash(self):
        class GuessingError(Exception):
            pass

        items = [item(f"{n}.mkv", audio=False) for n in range(3)]
        ok = SubtitleResult("x.eng.srt", 12, "en", 60.0)
        runner = runner_mod.Runner(self.config)
        runner.plan(items)
        with mock.patch(
            "mediafix.subtitles.run_subtitle_job",
            side_effect=[GuessingError("Insufficient data to process the guess"), ok, ok],
        ):
            summary = runner.run()

        self.assertEqual(summary.failed, 1)
        self.assertEqual(summary.done, 2)
        self.assertEqual(
            [j.state for j in runner.jobs],
            [runner_mod.JOB_FAILED, runner_mod.JOB_DONE, runner_mod.JOB_DONE],
        )
        messages = [msg for (_, _, msg, _) in runner.events.queue if msg]
        self.assertFalse(any("pipeline crashed" in msg for msg in messages))
        self.assertEqual(summary.exit_code, 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)