import time
from pathlib import Path
from queue import Empty

from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import Screen
from textual.widgets import (
    Button,
    DataTable,
    Footer,
    Header,
    Input,
    Label,
    ProgressBar,
    RichLog,
    Static,
)

from mediafix import scan as scan_mod
from mediafix import downmix as downmix_mod
from mediafix import runner as runner_mod
from mediafix.scan import FILTER_ALL, FILTERS, human_duration, human_size

STATE_LABELS = {
    runner_mod.JOB_QUEUED: "queued",
    runner_mod.JOB_RUNNING: "running",
    runner_mod.JOB_DONE: "done",
    runner_mod.JOB_FAILED: "failed",
    runner_mod.JOB_SKIPPED: "skipped",
    runner_mod.JOB_CANCELLED: "cancelled",
}

STATE_COLORS = {
    runner_mod.JOB_QUEUED: "grey50",
    runner_mod.JOB_RUNNING: "yellow",
    runner_mod.JOB_DONE: "green",
    runner_mod.JOB_FAILED: "red",
    runner_mod.JOB_SKIPPED: "grey42",
    runner_mod.JOB_CANCELLED: "grey42",
}


class MediaFixApp(App):
    TITLE = "mediafix"
    SUB_TITLE = "subtitles + stereo downmixes"

    CSS = """
    Screen { background: $surface; }
    #scan-box { width: 80; padding: 2 4; }
    #scan-title { text-style: bold; margin-bottom: 1; }
    #root-input { margin-bottom: 1; }
    #scan-btn { width: 16; }
    #scan-status { margin-top: 1; color: $text-muted; }
    #select-summary { padding: 1 2; color: $text-muted; }
    #filter-bar { height: auto; padding: 0 1; }
    .filter-btn { min-width: 9; margin-right: 1; }
    #bulk-bar { height: auto; padding: 0 1 1 1; }
    #bulk-bar Button { margin-right: 1; }
    #items { height: 1fr; }
    #run-body { padding: 0 1; }
    #run-summary { padding: 1 2; }
    #run-current { padding: 0 2; color: $text-muted; }
    #jobs { height: 1fr; }
    #run-actions { height: auto; padding: 1 1 0 1; }
    #run-actions Button { margin-right: 1; }
    #run-log { height: 12; border: round $panel; }
    """

    def __init__(self, config, roots):
        super().__init__()
        self.config = config
        self.roots = list(roots)
        self.result = None
        self.dry_run = False
        self.force = False

    def on_mount(self) -> None:
        self.push_screen(ScanScreen(self))

    def rescan(self) -> None:
        self.push_screen(ScanScreen(self))


class ScanScreen(Screen):
    BINDINGS = [
        Binding("escape", "quit_app", "quit"),
    ]

    def __init__(self, mediafix: MediaFixApp):
        super().__init__()
        self.mediafix = mediafix

    def compose(self) -> ComposeResult:
        root = self.mediafix.roots[0] if self.mediafix.roots else "/media"
        with Vertical(id="scan-box"):
            yield Label("Media library to scan", id="scan-title")
            yield Input(value=root, placeholder="/media", id="root-input")
            yield Button("Scan", id="scan-btn", variant="primary")
            yield Static("", id="scan-status")

    def on_mount(self) -> None:
        self.query_one("#root-input", Input).focus()

    @on(Button.Pressed, "#scan-btn")
    def _start(self) -> None:
        root = self.query_one("#root-input", Input).value.strip()
        if not root:
            self.query_one("#scan-status", Static).update("enter a path first")
            return
        if not Path(root).expanduser().exists():
            self.query_one("#scan-status", Static).update(f"path not found: {root}")
            return
        self.mediafix.roots = [root]
        self.query_one("#scan-btn", Button).disabled = True
        self.query_one("#scan-status", Static).update("scanning...")
        self._scan(root)

    @work(thread=True)
    def _scan(self, root: str) -> None:
        try:
            result = scan_mod.scan(
                [root], self.mediafix.config, progress=self._progress
            )
        except Exception as exc:  # noqa: BLE001
            self.app.call_from_thread(self._failed, f"{type(exc).__name__}: {exc}")
            return
        self.app.call_from_thread(self._finished, result)

    def _progress(self, done: int, total: int) -> None:
        self.app.call_from_thread(self._set_status, f"probing {done}/{total}...")

    def _set_status(self, text: str) -> None:
        try:
            self.query_one("#scan-status", Static).update(text)
        except Exception:  # noqa: BLE001 - screen may be gone
            pass

    def _failed(self, message: str) -> None:
        self.query_one("#scan-btn", Button).disabled = False
        self.query_one("#scan-status", Static).update(f"scan failed: {message}")

    def _finished(self, result) -> None:
        self.mediafix.result = result
        self.app.push_screen(SelectScreen(self.mediafix))

    def action_quit_app(self) -> None:
        self.app.exit()


class SelectScreen(Screen):
    BINDINGS = [
        Binding("space", "toggle_row", "select"),
        Binding("s", "toggle_sub", "sub"),
        Binding("a", "toggle_audio", "audio"),
        Binding("S", "bulk_sub", "sub all"),
        Binding("A", "bulk_audio", "audio all"),
        Binding("f", "cycle_filter", "filter"),
        Binding("r", "toggle_dry_run", "dry run"),
        Binding("enter", "start_run", "run"),
        Binding("escape", "back", "back"),
    ]

    def __init__(self, mediafix: MediaFixApp):
        super().__init__()
        self.mediafix = mediafix
        self.mode = FILTER_ALL
        self.visible: list = []

    def compose(self) -> ComposeResult:
        yield Header()
        with Vertical(id="select-body"):
            yield Static("", id="select-summary")
            with Horizontal(id="filter-bar"):
                for mode in FILTERS:
                    yield Button(mode, id=f"filter-{mode}", classes="filter-btn")
            with Horizontal(id="bulk-bar"):
                yield Button("Select all", id="sel-all")
                yield Button("None", id="sel-none")
                yield Button("Invert", id="sel-invert")
                yield Button("Start", id="sel-start", variant="success")
                yield Button("Rescan", id="sel-rescan")
            yield DataTable(id="items", cursor_type="row", zebra_stripes=True)
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#items", DataTable).focus()
        self._rebuild()

    def _rebuild(self) -> None:
        table = self.query_one("#items", DataTable)
        previous = None
        try:
            previous = table.cursor_row
        except Exception:  # noqa: BLE001
            previous = None

        table.clear(columns=True)
        table.add_columns("Sel", "Sub", "Aud", "Size", "Length", "File")
        self.visible = scan_mod.filter_items(self.mediafix.result.items, self.mode)
        for index, item in enumerate(self.visible):
            table.add_row(*self._cells(item), key=str(index))
        if self.visible:
            table.move_cursor(row=min(max(previous or 0, 0), len(self.visible) - 1))
        self._update_summary()

    @staticmethod
    def _cells(item) -> tuple:
        if item.error:
            mark = "-"
            sub = aud = "-"
            length = item.error
        else:
            mark = "[x]" if item.selected else "[ ]"
            sub = "want" if item.want_subtitle else ("miss" if item.needs_subtitle else "-")
            aud = "want" if item.want_audio else ("miss" if item.needs_audio else "-")
            length = human_duration(item.duration)
        return (mark, sub, aud, human_size(item.size), length, item.name)

    def _refresh(self, item) -> None:
        table = self.query_one("#items", DataTable)
        try:
            row = self.visible.index(item)
        except ValueError:
            return
        cells = self._cells(item)
        for column, value in enumerate(cells):
            table.update_cell_at_row(row, column, value)
        self._update_summary()

    def _update_summary(self) -> None:
        result = self.mediafix.result
        selected = sum(1 for i in self.visible if i.selected)
        subs = sum(1 for i in self.visible if i.selected and i.want_subtitle)
        auds = sum(1 for i in self.visible if i.selected and i.want_audio)
        mode = "DRY RUN" if self.mediafix.dry_run else "LIVE"
        text = (
            f"{mode} | {len(self.visible)} shown of {len(result.items)} | "
            f"{result.needs_subtitle_count} missing subs, {result.needs_audio_count} missing stereo | "
            f"selected {selected} ({subs} sub, {auds} aud)"
        )
        self.query_one("#select-summary", Static).update(text)

    def _current(self):
        table = self.query_one("#items", DataTable)
        if not self.visible:
            return None
        row = table.cursor_row
        if row is None or row < 0 or row >= len(self.visible):
            return None
        return self.visible[row]

    def action_toggle_row(self) -> None:
        item = self._current()
        if item is None or not item.selectable:
            return
        item.selected = not item.selected
        if item.selected:
            item.want_subtitle = item.needs_subtitle
            item.want_audio = item.needs_audio
        else:
            item.want_subtitle = item.want_audio = False
        self._refresh(item)

    def action_toggle_sub(self) -> None:
        item = self._current()
        if item is None or not item.selectable:
            return
        item.want_subtitle = not item.want_subtitle
        if item.want_subtitle:
            item.selected = True
        self._refresh(item)

    def action_toggle_audio(self) -> None:
        item = self._current()
        if item is None or not item.selectable:
            return
        item.want_audio = not item.want_audio
        if item.want_audio:
            item.selected = True
        self._refresh(item)

    def action_bulk_sub(self) -> None:
        self._bulk(lambda item: "sub")

    def action_bulk_audio(self) -> None:
        self._bulk(lambda item: "audio")

    def _bulk(self, field: str) -> None:
        for item in self.visible:
            if not item.selectable:
                continue
            key = "want_subtitle" if field == "sub" else "want_audio"
            missing = item.needs_subtitle if field == "sub" else item.needs_audio
            setattr(item, key, missing)
            if missing:
                item.selected = True
        self._rebuild()

    def action_cycle_filter(self) -> None:
        index = FILTERS.index(self.mode)
        self.mode = FILTERS[(index + 1) % len(FILTERS)]
        self._rebuild()

    def action_toggle_dry_run(self) -> None:
        self.mediafix.dry_run = not self.mediafix.dry_run
        self._update_summary()

    def action_back(self) -> None:
        self.app.pop_screen()

    def action_start_run(self) -> None:
        items = [i for i in self.mediafix.result.items if i.selected]
        if not items:
            self.query_one("#select-summary", Static).update("nothing selected")
            return
        self.app.push_screen(RunScreen(self.mediafix, items))

    @on(Button.Pressed, ".filter-btn")
    def _filter_pressed(self, event: Button.Pressed) -> None:
        self.mode = str(event.button.id).replace("filter-", "")
        self._rebuild()

    @on(Button.Pressed, "#sel-all")
    def _sel_all(self) -> None:
        self._set_all(True)

    @on(Button.Pressed, "#sel-none")
    def _sel_none(self) -> None:
        self._set_all(False)

    @on(Button.Pressed, "#sel-invert")
    def _sel_invert(self) -> None:
        for item in self.visible:
            if not item.selectable:
                continue
            item.selected = not item.selected
            item.want_subtitle = item.needs_subtitle and item.selected
            item.want_audio = item.needs_audio and item.selected
        self._rebuild()

    def _set_all(self, value: bool) -> None:
        for item in self.visible:
            if not item.selectable:
                continue
            item.selected = value
            item.want_subtitle = value and item.needs_subtitle
            item.want_audio = value and item.needs_audio
        self._rebuild()

    @on(Button.Pressed, "#sel-start")
    def _start_pressed(self) -> None:
        self.action_start_run()

    @on(Button.Pressed, "#sel-rescan")
    def _rescan(self) -> None:
        self.app.pop_screen()
        self.mediafix.rescan()


class RunScreen(Screen):
    BINDINGS = [
        Binding("escape", "cancel", "cancel"),
        Binding("c", "cancel", "cancel"),
    ]

    def __init__(self, mediafix: MediaFixApp, items):
        super().__init__()
        self.mediafix = mediafix
        self.items = items
        self.runner = runner_mod.Runner(
            mediafix.config, dry_run=mediafix.dry_run, force=mediafix.force
        )
        self.jobs = self.runner.plan(items)
        self.row_of: dict[int, int] = {}
        self.summary = None
        self.started = time.monotonic()
        self._finished = False

    def compose(self) -> ComposeResult:
        yield Header()
        with Vertical(id="run-body"):
            yield Static("starting...", id="run-summary")
            yield ProgressBar(id="overall")
            yield ProgressBar(id="current")
            yield Static("", id="run-current")
            yield DataTable(id="jobs", cursor_type="row")
            yield RichLog(id="run-log", wrap=True, markup=False)
            with Horizontal(id="run-actions"):
                yield Button("Cancel", id="cancel-btn", variant="error")
                yield Button("Back to list", id="back-btn", variant="primary", disabled=True)
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#jobs", DataTable)
        table.add_columns("Job", "State", "Progress", "File", "Detail")
        self.row_of = {}
        for index, job in enumerate(self.jobs):
            self.row_of[id(job)] = index
            table.add_row(
                job.kind, STATE_LABELS.get(job.state, job.state), "",
                job.label, "", key=str(index),
            )
        for bar_id in ("#overall", "#current"):
            bar = self.query_one(bar_id, ProgressBar)
            bar.total = 100
            bar.progress = 0
        self.query_one("#cancel-btn", Button).focus()
        self.set_interval(0.4, self._poll)
        self._batch()

    @work(thread=True)
    def _batch(self) -> None:
        try:
            for warning in downmix_mod.preflight(self.mediafix.config, force=self.mediafix.force):
                self.app.call_from_thread(self._log, f"warning: {warning}")
        except downmix_mod.ToolError as exc:
            self.app.call_from_thread(self._abort, str(exc))
            return

        summary = self.runner.run(
            on_ready=lambda message: self.app.call_from_thread(self._log, message)
        )
        self.app.call_from_thread(self._record_summary, summary)

    def _log(self, message: str) -> None:
        try:
            self.query_one("#run-log", RichLog).write(message)
        except Exception:  # noqa: BLE001 - screen may be torn down
            pass

    def _abort(self, message: str) -> None:
        self._finished = True
        self.query_one("#run-summary", Static).update(f"cannot start: {message}")

    def _record_summary(self, summary) -> None:
        self.summary = summary

    def _poll(self) -> None:
        if self._finished:
            return
        table = self.query_one("#jobs", DataTable)

        while True:
            try:
                job, state, message, progress = self.runner.events.get_nowait()
            except Empty:
                break
            if job is None:
                self._log(f"{state}: {message}")
                continue
            self._apply(table, job, state, message, progress)

        overall = sum(job.progress for job in self.jobs)
        self.query_one("#overall", ProgressBar).progress = int(100 * overall / max(1, len(self.jobs)))

        running = [job for job in self.jobs if job.state == runner_mod.JOB_RUNNING]
        if running:
            active = running[0]
            self.query_one("#current", ProgressBar).progress = int(100 * active.progress)
            self.query_one("#run-current", Static).update(
                f"{active.kind}: {active.label}  {int(active.progress * 100)}%  {active.message}"
            )

        if not self.runner.wait(0.01):
            return
        self._finish()

    def _apply(self, table, job, state: str, message: str, progress: float) -> None:
        row = self.row_of.get(id(job))
        if row is None:
            return
        table.update_cell_at_row(row, 1, STATE_LABELS.get(state, state))
        table.update_cell_at_row(row, 2, f"{int(progress * 100):>3}%")
        table.update_cell_at_row(row, 4, message)
        if state in runner_mod.TERMINAL_STATES:
            self._log(f"{STATE_LABELS.get(state, state):<9} {job.kind:<8} {job.label}  {message}")

    def _finish(self) -> None:
        self._finished = True
        elapsed = time.monotonic() - self.started
        counts: dict[str, int] = {}
        for job in self.jobs:
            counts[job.state] = counts.get(job.state, 0) + 1
        parts = "   ".join(
            f"{STATE_LABELS.get(state, state)}: {count}" for state, count in counts.items()
        )
        self.query_one("#run-summary", Static).update(f"finished in {elapsed / 60:.1f} min   {parts}")
        self.query_one("#run-current", Static).update("rescan to see the results reflected")
        self.query_one("#overall", ProgressBar).progress = 100
        self.query_one("#back-btn", Button).disabled = False
        self.query_one("#cancel-btn", Button).disabled = True

    def action_cancel(self) -> None:
        if not self._finished:
            self.runner.cancel()
            self._log("cancel requested; finishing the current job")

    @on(Button.Pressed, "#cancel-btn")
    def _cancel_pressed(self) -> None:
        self.action_cancel()

    @on(Button.Pressed, "#back-btn")
    def _back_pressed(self) -> None:
        self.app.pop_screen()