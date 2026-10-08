import time
from pathlib import Path
from queue import Empty

from rich.text import Text
from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.coordinate import Coordinate
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

    /* --- scan screen --- */
    #scan-box {
        width: 80;
        height: auto;
        margin: 2 0 0 0;
        padding: 1 3;
        border: round $accent;
        border-title-style: bold;
        border-title-color: $accent;
    }
    #scan-title { text-style: bold; margin-bottom: 1; }
    #root-input { margin-bottom: 1; }
    #scan-btn { width: 16; }
    #scan-status { margin-top: 1; color: $text-muted; }

    /* --- select screen: one titled block per concern --- */
    #select-summary {
        height: auto;
        margin: 0 2;
        padding: 0 1;
        border: round $accent;
        border-title-style: bold;
        border-title-color: $accent;
    }
    #filter-bar {
        height: auto;
        margin: 0 2;
        padding: 0 1;
        border-top: solid $primary;
        border-title-style: bold;
        border-title-color: $primary;
    }
    #select-bar {
        height: auto;
        margin: 0 2;
        padding: 0 1;
        border-top: solid $secondary;
        border-title-style: bold;
        border-title-color: $secondary;
    }
    #job-bar {
        height: auto;
        margin: 0 2;
        padding: 0 1;
        border-top: solid $warning;
        border-title-style: bold;
        border-title-color: $warning;
    }
    #run-bar {
        height: auto;
        margin: 0 2;
        padding: 0 1;
        border-top: solid $success;
        border-title-style: bold;
        border-title-color: $success;
    }
    #filter-bar Button, #select-bar Button, #job-bar Button, #run-bar Button {
        border: none;
        height: 1;
        min-height: 1;
        min-width: 0;
        padding: 0 1;
        margin: 0 1 0 0;
    }
    #filter-bar .filter-btn { min-width: 9; }
    #items {
        height: 1fr;
        margin: 0 2 1 2;
        border-top: solid $panel;
        border-title-style: bold;
        border-title-color: $text-muted;
    }

    /* --- run screen --- */
    #progress-box {
        height: auto;
        margin: 0 1;
        padding: 0 1;
        border-top: solid $primary;
        border-title-style: bold;
        border-title-color: $primary;
    }
    #run-summary { padding: 0 1; }
    #run-current { padding: 0 1; color: $text-muted; }
    #jobs {
        height: 1fr;
        margin: 0 1;
        border-top: solid $panel;
        border-title-style: bold;
        border-title-color: $text-muted;
    }
    #run-log {
        height: 8;
        margin: 0 1;
        border: round $panel;
        border-title-style: bold;
        border-title-color: $text-muted;
    }
    #run-actions {
        height: auto;
        margin: 0 1;
        padding: 0 1;
        border-top: solid $success;
        border-title-style: bold;
        border-title-color: $success;
    }
    #run-actions Button {
        border: none;
        height: 1;
        min-height: 1;
        padding: 0 1;
        margin-right: 1;
    }
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
        self.query_one("#scan-box", Vertical).border_title = "scan library"
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
    # Column labels, in the same order as SelectScreen._cells returns values.
    COLUMNS = ("Sel", "Sub", "Aud", "Cen", "Size", "Length", "File")

    # Bulk flags: field -> (want attribute, needs attribute, label).
    BULK_FIELDS = {
        "sub": ("want_subtitle", "needs_subtitle", "subtitles"),
        "audio": ("want_audio", "needs_audio", "downmixes"),
        "censor": ("want_censor", "needs_censor", "censoring"),
    }

    BINDINGS = [
        Binding("space", "toggle_row", "select"),
        Binding("s", "toggle_sub", "sub"),
        Binding("a", "toggle_audio", "audio"),
        Binding("c", "toggle_censor", "censor"),
        Binding("S", "bulk_sub", "sub sel"),
        Binding("A", "bulk_audio", "audio sel"),
        Binding("C", "bulk_censor", "censor sel"),
        Binding("f", "cycle_filter", "filter"),
        Binding("r", "toggle_dry_run", "dry run"),
        Binding("enter", "start_run", "run"),
        Binding("escape", "back", "back"),
    ]

    def __init__(self, mediafix: MediaFixApp):
        super().__init__()
        self.mediafix = mediafix
        self.mode = FILTER_ALL
        self.shown: list = []

    def compose(self) -> ComposeResult:
        yield Header()
        with Vertical(id="select-body"):
            yield Static("", id="select-summary")
            with Horizontal(id="filter-bar"):
                for mode in FILTERS:
                    yield Button(mode, id=f"filter-{mode}", classes="filter-btn")
            with Horizontal(id="select-bar"):
                yield Button("Select all", id="sel-all")
                yield Button("None", id="sel-none")
                yield Button("Invert", id="sel-invert")
            with Horizontal(id="job-bar"):
                yield Button("Subs on/off", id="job-sub", variant="warning")
                yield Button("Downmix on/off", id="job-audio", variant="warning")
                yield Button("Censor on/off", id="job-censor", variant="warning")
            with Horizontal(id="run-bar"):
                yield Button("Start", id="sel-start", variant="success")
                yield Button("Dry run: off", id="dry-btn")
                yield Button("Rescan", id="sel-rescan")
            yield DataTable(id="items", cursor_type="row", zebra_stripes=True)
        yield Footer()

    def on_mount(self) -> None:
        titles = {
            "#select-summary": "status",
            "#filter-bar": "filter",
            "#select-bar": "selection",
            "#job-bar": "jobs for the selection (S / A / C)",
            "#run-bar": "run",
        }
        for selector, title in titles.items():
            self.query_one(selector).border_title = title
        self.query_one("#items", DataTable).focus()
        self._update_dry_button()
        self._rebuild()

    def _rebuild(self) -> None:
        table = self.query_one("#items", DataTable)
        previous = None
        try:
            previous = table.cursor_row
        except Exception:  # noqa: BLE001
            previous = None

        table.clear(columns=True)
        table.add_columns(*self._headers())
        self.shown = scan_mod.filter_items(self.mediafix.result.items, self.mode)
        for index, item in enumerate(self.shown):
            table.add_row(*self._cells(item), key=str(index))
        if self.shown:
            table.move_cursor(row=min(max(previous or 0, 0), len(self.shown) - 1))
        table.border_title = f"{self.mode} - {len(self.shown)} of {len(self.mediafix.result.items)} files"
        self._update_filter_buttons()
        self._update_summary()

    @classmethod
    def _headers(cls) -> tuple:
        styles = {
            "Sel": "bold",
            "Sub": "bold cyan",
            "Aud": "bold cyan",
            "Cen": "bold cyan",
            "Size": "bold",
            "Length": "bold",
            "File": "bold",
        }
        return tuple(Text(label, style=styles.get(label, "")) for label in cls.COLUMNS)

    @staticmethod
    def _flag(want: bool, needs: bool) -> Text:
        if want:
            return Text("want", style="bold green")
        if needs:
            return Text("miss", style="yellow")
        return Text("-", style="dim")

    @staticmethod
    def _cells(item) -> tuple:
        if item.error:
            mark = Text("-", style="dim")
            sub = aud = cen = Text("-", style="dim")
            length = Text(item.error, style="bold red")
            name = Text(item.name, style="bold red")
        else:
            mark = Text("[x]", style="bold green") if item.selected else Text("[ ]", style="dim")
            sub = SelectScreen._flag(item.want_subtitle, item.needs_subtitle)
            aud = SelectScreen._flag(item.want_audio, item.needs_audio)
            cen = SelectScreen._flag(item.want_censor, item.needs_censor)
            length = Text(human_duration(item.duration))
            name = Text(item.name)
        return (mark, sub, aud, cen, Text(human_size(item.size)), length, name)

    def _refresh(self, item) -> None:
        table = self.query_one("#items", DataTable)
        try:
            row = self.shown.index(item)
        except ValueError:
            return
        cells = self._cells(item)
        # update_cell_at is coordinate-based, so it stays correct regardless of
        # the RowKey/ColumnKey objects the table generated internally.
        for column, value in enumerate(cells):
            table.update_cell_at(Coordinate(row, column), value)
        self._update_summary()

    def _update_summary(self) -> None:
        result = self.mediafix.result
        selected = sum(1 for i in self.shown if i.selected)
        subs = sum(1 for i in self.shown if i.selected and i.want_subtitle)
        cens = sum(1 for i in self.shown if i.selected and i.want_censor)
        auds = sum(1 for i in self.shown if i.selected and i.want_audio)
        mode = "[bold red]DRY RUN" if self.mediafix.dry_run else "[bold green]LIVE"
        text = (
            f"{mode}[/] | [bold]{len(self.shown)}/{len(result.items)}[/] | "
            f"missing: [yellow]{result.needs_subtitle_count}[/] sub, "
            f"[yellow]{result.needs_audio_count}[/] stereo, "
            f"[yellow]{result.needs_censor_count}[/] swears | "
            f"selected [bold cyan]{selected}[/]: "
            f"[green]{subs}[/] sub, [green]{cens}[/] cen, [green]{auds}[/] aud"
        )
        self.query_one("#select-summary", Static).update(text)

    def _current(self):
        table = self.query_one("#items", DataTable)
        if not self.shown:
            return None
        row = table.cursor_row
        if row is None or row < 0 or row >= len(self.shown):
            return None
        return self.shown[row]

    def action_toggle_row(self) -> None:
        item = self._current()
        if item is None or not item.selectable:
            return
        # Selection and the per-file job flags are independent: unchecking a
        # flag must survive space, Select all/None/Invert and refiltering.
        item.selected = not item.selected
        self._refresh(item)

    def action_toggle_sub(self) -> None:
        self._toggle_field("sub")

    def action_toggle_audio(self) -> None:
        self._toggle_field("audio")

    def action_toggle_censor(self) -> None:
        self._toggle_field("censor")

    def _toggle_field(self, field: str) -> None:
        item = self._current()
        if item is None or not item.selectable:
            return
        key, needs_key, label = self.BULK_FIELDS[field]
        if not getattr(item, needs_key):
            # Clamp: a file that needs no such job can never carry the flag,
            # but an existing flag can always be cleared.
            if getattr(item, key):
                setattr(item, key, False)
                self._refresh(item)
            else:
                self.notify(f"this file needs no {label}", severity="information")
            return
        setattr(item, key, not getattr(item, key))
        if getattr(item, key):
            item.selected = True
        self._refresh(item)

    def action_bulk_sub(self) -> None:
        self._toggle_job("sub")

    def action_bulk_audio(self) -> None:
        self._toggle_job("audio")

    def action_bulk_censor(self) -> None:
        self._toggle_job("censor")

    def _bulk_targets(self) -> tuple[list, bool]:
        """Rows a bulk action touches: the selection, else every visible row.

        The bool is True when the fallback (nothing selected) was used.
        """
        selected = [i for i in self.shown if i.selectable and i.selected]
        if selected:
            return selected, False
        return [i for i in self.shown if i.selectable], True

    def _toggle_job(self, field: str) -> None:
        key, needs_key, label = self.BULK_FIELDS[field]
        targets, fallback = self._bulk_targets()
        if not targets:
            self.notify("no rows to change", severity="warning")
            return
        if fallback:
            self.notify("nothing selected - applying to every visible row",
                        severity="information")
        # Clamp: files that do not need the job never carry the flag.
        clamped = False
        for item in targets:
            if not getattr(item, needs_key) and getattr(item, key):
                setattr(item, key, False)
                clamped = True
        eligible = [i for i in targets if getattr(i, needs_key)]
        if not eligible:
            if clamped:
                self._rebuild()
            self.notify(f"no row here needs {label}", severity="warning")
            return
        # State-aware: check everything if anything is unchecked, otherwise
        # uncheck everything.
        turn_on = any(not getattr(i, key) for i in eligible)
        for item in eligible:
            setattr(item, key, turn_on)
            if turn_on:
                item.selected = True
        self._rebuild()
        verb = "enabled" if turn_on else "disabled"
        self.notify(f"{verb} {label} on {len(eligible)} row(s)")

    def action_cycle_filter(self) -> None:
        index = FILTERS.index(self.mode)
        self.mode = FILTERS[(index + 1) % len(FILTERS)]
        self._rebuild()

    def action_toggle_dry_run(self) -> None:
        self.mediafix.dry_run = not self.mediafix.dry_run
        self._update_dry_button()
        self._update_summary()

    def action_back(self) -> None:
        self.app.pop_screen()

    def action_start_run(self) -> None:
        items = [i for i in self.shown if i.selected and i.actionable]
        if not items:
            self.notify("nothing selected", severity="warning")
            return
        self.app.push_screen(RunScreen(self.mediafix, items))

    def _update_filter_buttons(self) -> None:
        for mode in FILTERS:
            try:
                button = self.query_one(f"#filter-{mode}", Button)
            except Exception:  # noqa: BLE001 - screen may be mid-teardown
                continue
            button.variant = "primary" if mode == self.mode else "default"

    def _update_dry_button(self) -> None:
        button = self.query_one("#dry-btn", Button)
        on = self.mediafix.dry_run
        button.label = f"Dry run: {'on' if on else 'off'}"
        button.variant = "warning" if on else "default"

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
        for item in self.shown:
            if not item.selectable:
                continue
            item.selected = not item.selected
        self._rebuild()

    @on(Button.Pressed, "#job-sub")
    def _job_sub_pressed(self) -> None:
        self._toggle_job("sub")

    @on(Button.Pressed, "#job-audio")
    def _job_audio_pressed(self) -> None:
        self._toggle_job("audio")

    @on(Button.Pressed, "#job-censor")
    def _job_censor_pressed(self) -> None:
        self._toggle_job("censor")

    @on(Button.Pressed, "#dry-btn")
    def _dry_pressed(self) -> None:
        self.action_toggle_dry_run()

    def _set_all(self, value: bool) -> None:
        for item in self.shown:
            if not item.selectable:
                continue
            item.selected = value
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
            with Vertical(id="progress-box"):
                yield Static("starting...", id="run-summary")
                yield ProgressBar(id="overall")
                yield ProgressBar(id="current")
                yield Static("", id="run-current", markup=False)
            yield DataTable(id="jobs", cursor_type="row")
            yield RichLog(id="run-log", wrap=True, markup=False)
            with Horizontal(id="run-actions"):
                yield Button("Cancel", id="cancel-btn", variant="error")
                yield Button("Back to list", id="back-btn", variant="primary", disabled=True)
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#progress-box", Vertical).border_title = "progress"
        table = self.query_one("#jobs", DataTable)
        table.border_title = "jobs"
        table.add_columns("Job", "State", "Progress", "File", "Detail")
        self.row_of = {}
        for index, job in enumerate(self.jobs):
            self.row_of[id(job)] = index
            table.add_row(
                job.kind, self._state_cell(job.state), "",
                job.label, "", key=str(index),
            )
        self.query_one("#run-log", RichLog).border_title = "log"
        self.query_one("#run-actions", Horizontal).border_title = "actions"
        for bar_id in ("#overall", "#current"):
            bar = self.query_one(bar_id, ProgressBar)
            bar.total = 100
            bar.progress = 0
        self.query_one("#cancel-btn", Button).focus()
        self.set_interval(0.4, self._poll)
        self._batch()

    @staticmethod
    def _state_cell(state: str) -> Text:
        label = STATE_LABELS.get(state, state)
        return Text(label, style=STATE_COLORS.get(state, ""))

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
        table.update_cell_at(Coordinate(row, 1), self._state_cell(state))
        table.update_cell_at(Coordinate(row, 2), f"{int(progress * 100):>3}%")
        table.update_cell_at(Coordinate(row, 4), message)
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
        failed = counts.get(runner_mod.JOB_FAILED, 0)
        tone = "bold red" if failed else "bold green"
        self.query_one("#run-summary", Static).update(
            f"[{tone}]finished[/] in {elapsed / 60:.1f} min   {parts}"
        )
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