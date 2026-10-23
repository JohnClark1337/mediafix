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
    #filter-buttons { height: 1; }
    #search-input {
        height: 1;
        border: none;
        padding: 0 1;
        background: $panel;
    }
    #search-input:focus { background: $boost; }
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
        """Go back to the scan screen.

        The scan screen is reused instead of pushing a new one: pushing would
        grow the screen stack on every rescan and leave dead screens
        underneath, with the scan button still disabled.
        """
        self.pop_screen()


class ScanScreen(Screen):
    BINDINGS = [
        Binding("escape", "quit_app", "quit"),
    ]

    def __init__(self, mediafix: MediaFixApp):
        super().__init__()
        self.mediafix = mediafix

    def compose(self) -> ComposeResult:
        root = self.mediafix.roots[0] if self.mediafix.roots else scan_mod.default_root()
        with Vertical(id="scan-box"):
            yield Label("Media library to scan", id="scan-title")
            yield Input(value=root, placeholder=scan_mod.default_root(), id="root-input")
            yield Button("Scan", id="scan-btn", variant="primary")
            yield Static("", id="scan-status")

    def on_mount(self) -> None:
        self.query_one("#scan-box", Vertical).border_title = "scan library"
        self.query_one("#root-input", Input).focus()

    def on_screen_resume(self) -> None:
        """Re-arm the screen when the selection screen pops back to it.

        Without this the scan button stays disabled after the first scan and
        the screen is a dead end.
        """
        try:
            self.query_one("#scan-btn", Button).disabled = False
            self.query_one("#scan-status", Static).update("")
        except Exception:  # noqa: BLE001 - screen may be mid-teardown
            pass

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


class SearchInput(Input):
    """The wildcard search box on the selection screen.

    Escape clears the pattern and hands focus back to the table. The binding
    lives here rather than on the screen so it wins over the screen's "back"
    binding while the box has focus.
    """

    BINDINGS = [Binding("escape", "clear_search", "clear search", show=False)]

    def action_clear_search(self) -> None:
        self.value = ""
        self.screen.query_one("#items", DataTable).focus()


class SelectScreen(Screen):
    # Column labels, in the same order as SelectScreen._cells returns values.
    COLUMNS = ("Sel", "Sub", "Aud", "Cen", "Size", "Length", "File")

    # Bulk flags: field -> (want, needs, redo-capable, force, label).
    BULK_FIELDS = {
        "sub": ("want_subtitle", "needs_subtitle", None, None, "subtitles"),
        "audio": (
            "want_audio", "needs_audio", "has_downmix", "force_audio",
            "downmixes",
        ),
        "censor": (
            "want_censor", "needs_censor", "has_censored", "force_censor",
            "censoring",
        ),
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
        Binding("slash", "focus_search", "search"),
        Binding("r", "toggle_dry_run", "dry run"),
        # The table has focus, and a focused DataTable binds "enter" to
        # select_cursor. A normal binding would lose that race, so this one
        # is priority: the app checks priority bindings before forwarding a
        # key to the focused widget.
        Binding("enter", "start_run", "run", priority=True),
        Binding("escape", "back", "back"),
    ]

    # Actions the screen must not run while the search box has focus. The
    # priority "enter" binding in particular is checked before the focused
    # widget, and the letter bindings would otherwise swallow typed text.
    SEARCH_BLOCKED_ACTIONS = frozenset({
        "toggle_row", "toggle_sub", "toggle_audio", "toggle_censor",
        "bulk_sub", "bulk_audio", "bulk_censor", "cycle_filter",
        "toggle_dry_run", "start_run", "back", "focus_search",
    })

    def __init__(self, mediafix: MediaFixApp):
        super().__init__()
        self.mediafix = mediafix
        self.mode = FILTER_ALL
        self.pattern = ""
        self.shown: list = []

    def check_action(self, action: str, parameters: tuple) -> bool | None:
        searching = isinstance(self.focused, SearchInput)
        if searching and action in self.SEARCH_BLOCKED_ACTIONS:
            # None disables the action but keeps the footer stable (greyed).
            # A disabled action also lets the key fall through to the input.
            return None
        return True

    def compose(self) -> ComposeResult:
        yield Header()
        with Vertical(id="select-body"):
            yield Static("", id="select-summary")
            with Vertical(id="filter-bar"):
                with Horizontal(id="filter-buttons"):
                    for mode in FILTERS:
                        yield Button(mode, id=f"filter-{mode}", classes="filter-btn")
                yield SearchInput(
                    placeholder="search name or path: * ? [seq]   (/ to edit, esc clears)",
                    id="search-input",
                )
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
        items = scan_mod.filter_items(self.mediafix.result.items, self.mode)
        self.shown = scan_mod.filter_by_pattern(items, self.pattern)
        for index, item in enumerate(self.shown):
            table.add_row(*self._cells(item), key=str(index))
        if self.shown:
            table.move_cursor(row=min(max(previous or 0, 0), len(self.shown) - 1))
        label = self.mode if not self.pattern else f"{self.mode}  {self.pattern}"
        table.border_title = f"{label} - {len(self.shown)} of {len(self.mediafix.result.items)} files"
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
    def _flag(want: bool, needs: bool, has_redo: bool = False,
              force: bool = False) -> Text:
        if want and force:
            return Text("redo", style="bold magenta")
        if want:
            return Text("want", style="bold green")
        if needs:
            return Text("miss", style="yellow")
        if has_redo:
            return Text("old", style="dim cyan")
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
            aud = SelectScreen._flag(
                item.want_audio, item.needs_audio, item.has_downmix,
                item.force_audio,
            )
            cen = SelectScreen._flag(
                item.want_censor, item.needs_censor, item.has_censored,
                item.force_censor,
            )
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
        # The counts cover the whole result, not just the visible rows: a
        # filter hides rows, it does not deselect them.
        items = result.items
        shown_ids = {id(s) for s in self.shown}
        selected = sum(1 for i in items if i.selected)
        subs = sum(1 for i in items if i.selected and i.want_subtitle)
        cens = sum(1 for i in items if i.selected and i.want_censor)
        auds = sum(1 for i in items if i.selected and i.want_audio)
        hidden = sum(
            1 for i in items if i.selected and id(i) not in shown_ids
        )
        mode = "[bold red]DRY RUN" if self.mediafix.dry_run else "[bold green]LIVE"
        hidden_note = f" ([dim]{hidden} hidden by filter/search[/])" if hidden else ""
        text = (
            f"{mode}[/] | [bold]{len(self.shown)}/{len(result.items)}[/] shown | "
            f"missing: [yellow]{result.needs_subtitle_count}[/] sub, "
            f"[yellow]{result.needs_audio_count}[/] stereo, "
            f"[yellow]{result.needs_censor_count}[/] swears | "
            f"selected [bold cyan]{selected}[/]{hidden_note}: "
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
        key, needs_key, redo_key, force_key, label = self.BULK_FIELDS[field]
        needs = getattr(item, needs_key)
        can_redo = bool(redo_key and getattr(item, redo_key))
        if can_redo and not needs:
            forced = bool(getattr(item, force_key))
            if forced:
                setattr(item, key, False)
                setattr(item, force_key, False)
            else:
                setattr(item, key, True)
                setattr(item, force_key, True)
                if not item.selected:
                    self.notify(
                        f"{label} redo flagged, but the row is not selected - "
                        "press space to run it",
                        severity="information",
                    )
            self._refresh(item)
            return
        if not needs:
            # Clamp: a file that needs no such job can never carry a normal
            # flag, but an existing flag (or redo) can always be cleared.
            cleared = False
            if getattr(item, key):
                setattr(item, key, False)
                cleared = True
            if force_key and getattr(item, force_key):
                setattr(item, force_key, False)
                cleared = True
            if cleared:
                self._refresh(item)
            else:
                self.notify(f"this file needs no {label}", severity="information")
            return
        turn_on = not getattr(item, key)
        setattr(item, key, turn_on)
        if force_key:
            setattr(item, force_key, False)
        # A job flag never changes the selection: the selection is what the
        # user asked to run. Silently adding a file to the run set would run
        # its other flagged jobs too.
        if turn_on and not item.selected:
            self.notify(
                f"{label} flagged, but the row is not selected - press space to run it",
                severity="information",
            )
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
        key, needs_key, redo_key, force_key, label = self.BULK_FIELDS[field]
        targets, fallback = self._bulk_targets()
        if not targets:
            self.notify("no rows to change", severity="warning")
            return
        if fallback:
            self.notify("nothing selected - applying to every visible row",
                        severity="information")
        # Clamp: files that neither need the job nor have a generated track
        # never carry the flag.
        clamped = False
        for item in targets:
            can_redo = bool(redo_key and getattr(item, redo_key))
            if not getattr(item, needs_key) and not can_redo:
                if getattr(item, key):
                    setattr(item, key, False)
                    clamped = True
                if force_key and getattr(item, force_key):
                    setattr(item, force_key, False)
                    clamped = True
        eligible = [
            i for i in targets
            if getattr(i, needs_key)
            or (redo_key and getattr(i, redo_key))
        ]
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
            if force_key:
                redo = bool(
                    turn_on
                    and redo_key
                    and getattr(item, redo_key)
                    and not getattr(item, needs_key)
                )
                setattr(item, force_key, redo)
            if turn_on and fallback:
                # The fallback touched every visible row, most of which are
                # not selected. Flags only run on selected rows, so they have
                # to be selected here or the change would do nothing.
                item.selected = True
        self._rebuild()
        verb = "enabled" if turn_on else "disabled"
        self.notify(f"{verb} {label} on {len(eligible)} row(s)")

    def action_cycle_filter(self) -> None:
        index = FILTERS.index(self.mode)
        self.mode = FILTERS[(index + 1) % len(FILTERS)]
        self._rebuild()

    def action_focus_search(self) -> None:
        search = self.query_one("#search-input", SearchInput)
        search.focus()
        # Replace the old pattern on the first keystroke instead of appending.
        search.select_all()

    @on(Input.Changed, "#search-input")
    def _search_changed(self, event: Input.Changed) -> None:
        self.pattern = event.value
        self._rebuild()

    @on(Input.Submitted, "#search-input")
    def _search_submitted(self) -> None:
        self.query_one("#items", DataTable).focus()

    def action_toggle_dry_run(self) -> None:
        self.mediafix.dry_run = not self.mediafix.dry_run
        self._update_dry_button()
        self._update_summary()

    def action_back(self) -> None:
        self.app.pop_screen()

    def _selected_items(self) -> list:
        """Every selected, actionable item - including rows hidden by a filter.

        The filter only decides what is on screen. A selected row stays
        selected, so Start must not silently drop the hidden ones.
        """
        return [
            i for i in self.mediafix.result.items if i.selected and i.actionable
        ]

    def action_start_run(self) -> None:
        items = self._selected_items()
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
            try:
                for warning in downmix_mod.preflight(
                    self.mediafix.config, force=self.mediafix.force
                ):
                    self.app.call_from_thread(self._log, f"warning: {warning}")
            except downmix_mod.ToolError as exc:
                self.app.call_from_thread(self._abort, str(exc))
                return
            except Exception as exc:  # noqa: BLE001
                self.app.call_from_thread(self._abort, str(exc))
                return

            summary = self.runner.run(
                on_ready=lambda message: self.app.call_from_thread(self._log, message)
            )
            self.app.call_from_thread(self._record_summary, summary)
        finally:
            # Guarantee the screen can always finish even if run() throws before
            # emitting events.
            self.app.call_from_thread(self._poll)

    def _log(self, message: str) -> None:
        try:
            self.query_one("#run-log", RichLog).write(message)
        except Exception:  # noqa: BLE001 - screen may be torn down
            pass

    def _abort(self, message: str) -> None:
        self._finished = True
        self.query_one("#run-summary", Static).update(f"cannot start: {message}")
        self.query_one("#run-current", Static).update("nothing was changed")
        # The run never started, so there is nothing to cancel and nothing to
        # wait for. The back button has to be enabled here, otherwise the
        # screen is a dead end: _poll stops once _finished is set, so _finish
        # (which enables it) never runs.
        self.query_one("#back-btn", Button).disabled = False
        self.query_one("#cancel-btn", Button).disabled = True
        self._log(f"cannot start: {message}")

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