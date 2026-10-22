import asyncio
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mediafix import scan as scan_mod
from mediafix.probe import AudioStreamInfo, MediaInfo


def _probe_ok():
    """A probe result that keeps items selectable (English stereo track)."""
    return MediaInfo(
        path="x.mkv", size=0, duration=1.0,
        audio=[AudioStreamInfo(index=0, codec="aac", channels=2, layout="stereo", language="eng")],
        subtitles=[],
    )


class ScanTimingTests(unittest.TestCase):
    """duration_scanned is elapsed seconds, not a sum of file sizes."""

    def _config(self, root: Path, tmp: Path):
        import argparse
        cfg = argparse.Namespace(
            video_ext={".mkv"},
            subtitle_ext={".srt"},
            sub_language="eng",
            ffprobe="ffprobe",
            cache_path=tmp / "cache.json",
            swears_path=None,
            censor_track_title="Censored (Bleeparr)",
        )
        return cfg

    def test_duration_is_seconds_and_bytes_are_separate(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            root = tmp / "media"
            root.mkdir()
            (root / "a.mkv").write_bytes(b"x" * 4096)
            (root / "b.mkv").write_bytes(b"x" * 8192)
            config = self._config(root, tmp)

            with unittest.mock.patch.object(scan_mod.probe_mod, "probe", return_value=_probe_ok()):
                result = scan_mod.scan([root], config, use_cache=False)

            # A 12KB payload must never be reported as ~12 seconds or ~12TB.
            self.assertLess(result.duration_scanned, 60.0)
            self.assertGreaterEqual(result.duration_scanned, 0.0)
            self.assertEqual(result.total_bytes, 12288)

    def test_duration_is_not_a_size(self):
        """Regression: the field was assigned sum(i.size), a ~7.5TB value."""
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            root = tmp / "media"
            root.mkdir()
            big = root / "big.mkv"
            big.write_bytes(b"x" * 20_000_000)
            config = self._config(root, tmp)

            with unittest.mock.patch.object(scan_mod.probe_mod, "probe", return_value=_probe_ok()):
                result = scan_mod.scan([root], config, use_cache=False)

            # With the bug both fields equal 20,000,000; duration must be far lower.
            self.assertLess(result.duration_scanned, 300.0)
            self.assertEqual(result.total_bytes, 20_000_000)


class VisibleShadowTests(unittest.TestCase):
    """tui.py assigned self.visible, shadowing Textual's DOMNode.visible property.

    Assigning a list raised TypeError: unhashable type: 'list' inside
    Textual's visibility setter, because `self.visible: list = []` still
    assigns, and the instance attribute wins over the base-class property.
    """

    def test_select_screen_does_not_shadow_dom_visible(self):
        from mediafix import tui as tui_mod
        from textual.dom import DOMNode

        self.assertTrue(hasattr(DOMNode, "visible"), "Textual DOMNode must still expose visible")

        app = tui_mod.MediaFixApp(config=None, roots=[])
        screen = tui_mod.SelectScreen(app)
        # The attribute that holds filtered rows must not be named `visible`.
        self.assertFalse(
            "visible" in screen.__dict__,
            f"SelectScreen shadows DOMNode.visible with {screen.__dict__.get('visible')!r}",
        )
        self.assertIsInstance(screen.shown, list)
        # And the inherited property must still work.
        self.assertIsInstance(DOMNode.visible.fget(screen), bool)


class DataTableApiTests(unittest.TestCase):
    """_refresh used table.update_cell_at_row(row, col, value), which does not exist.

    Every selection toggle (space/s/a/S/A) raised AttributeError, then
    CellDoesNotExist when retried with update_cell. The correct call is
    update_cell_at(Coordinate(row, col), value).
    """

    def test_update_cell_at_row_does_not_exist(self):
        from textual.widgets import DataTable

        self.assertFalse(
            hasattr(DataTable, "update_cell_at_row"),
            "update_cell_at_row appeared; revisit which API _refresh should use",
        )
        self.assertTrue(hasattr(DataTable, "update_cell_at"))

    def test_columns_match_cells_order(self):
        from mediafix import tui as tui_mod

        app = tui_mod.MediaFixApp(config=None, roots=[])
        screen = tui_mod.SelectScreen(app)
        item = scan_mod.MediaItem(path="a.mkv", size=2048, duration=120.0, audio=[], subtitles=[])
        self.assertEqual(len(screen._cells(item)), len(screen.COLUMNS))


class RunScreenUpdateTests(unittest.TestCase):
    """RunScreen._apply also called the nonexistent update_cell_at_row.

    Progress updates happen on every poll, so the run screen crashed on the
    first job state change.
    """

    def test_apply_updates_cells_without_error(self):
        import argparse
        import queue

        from textual.coordinate import Coordinate
        from textual.widgets import DataTable

        from mediafix import runner as runner_mod
        from mediafix import tui as tui_mod

        class FakeRunner:
            def __init__(self, config, dry_run=False, force=False):
                self.events = queue.Queue()

            def plan(self, items):
                return [runner_mod.Job(kind="sub", path=i.path, item=i) for i in items]

            def wait(self, timeout=None):
                return True

            def run(self, on_ready=None):
                return None

            def cancel(self):
                pass

        async def main():
            cfg = argparse.Namespace(model="small", device="cpu", compute_type="int8")
            app = tui_mod.MediaFixApp(config=cfg, roots=[])
            items = [
                scan_mod.MediaItem(path=f"m{i}.mkv", size=2048, duration=120.0, audio=[], subtitles=[])
                for i in range(3)
            ]
            with unittest.mock.patch.object(tui_mod.runner_mod, "Runner", FakeRunner), \
                 unittest.mock.patch.object(tui_mod.downmix_mod, "preflight", return_value=[]):
                screen = tui_mod.RunScreen(app, items)
                async with app.run_test() as pilot:
                    await pilot.pause()
                    app.push_screen(screen)
                    await pilot.pause()
                    table = screen.query_one("#jobs", DataTable)
                    self.assertEqual(table.row_count, 3)

                    job = screen.jobs[1]
                    screen.runner.events.put((job, runner_mod.JOB_DONE, "12 cues -> m1.srt", 1.0))
                    # _apply is what used to raise; call it directly too.
                    screen._apply(table, job, runner_mod.JOB_DONE, "12 cues -> m1.srt", 1.0)

                    def cell(row, col):
                        rk, ck = table.coordinate_to_cell_key(Coordinate(row, col))
                        return str(table.get_cell(rk, ck))

                    self.assertEqual(cell(1, 1), "done")
                    self.assertEqual(cell(1, 2), "100%")
                    self.assertEqual(cell(1, 4), "12 cues -> m1.srt")

        asyncio.run(main())


class SelectScreenInteractionTests(unittest.TestCase):
    """Drives the real Textual app: scan results render and keys mutate state."""

    def _run(self, body):
        async def main():
            with tempfile.TemporaryDirectory() as td:
                root = Path(td)
                for name in ("a.mkv", "b.mkv", "c.mkv"):
                    (root / name).write_bytes(b"x" * 2048)
                import argparse

                cfg = argparse.Namespace(
                    video_ext={".mkv"}, subtitle_ext={".srt"}, sub_language="eng",
                    ffprobe="ffprobe", cache_path=None, model="small",
                    device="cpu", compute_type="int8", jobs=2,
                    swears_path=None, censor_track_title="Censored (Bleeparr)",
                )
                from mediafix import tui as tui_mod

                app = tui_mod.MediaFixApp(config=cfg, roots=[])
                with unittest.mock.patch(
                    "mediafix.probe.probe", side_effect=lambda path, *a, **k: _probe_ok()
                ):
                    app.result = scan_mod.scan([str(root)], cfg, use_cache=False)
                    async with app.run_test() as pilot:
                        await pilot.pause()
                        app.push_screen(tui_mod.SelectScreen(app))
                        await pilot.pause()
                        await pilot.pause()
                        await body(pilot, app.screen, app)

        asyncio.run(main())

    def test_rows_render_and_toggle_keys_work(self):
        from textual.widgets import DataTable

        async def body(pilot, screen, app):
            table = screen.query_one("#items", DataTable)
            self.assertEqual(len(screen.shown), 3)
            self.assertEqual(table.row_count, 3)

            def cells(row):
                from textual.coordinate import Coordinate

                out = []
                for col in range(len(screen.COLUMNS)):
                    rk, ck = table.coordinate_to_cell_key(Coordinate(row, col))
                    out.append(str(table.get_cell(rk, ck)))
                return out

            before = cells(0)
            await pilot.press("s")
            await pilot.pause()
            self.assertNotEqual(cells(0)[1], before[1], "'s' must repaint the Sub column")
            self.assertEqual(
                cells(0)[0], "[ ]",
                "a job flag must not select the row: the selection is the run set",
            )

            await pilot.press("s")
            await pilot.pause()
            self.assertEqual(cells(0)[0], "[ ]", "the flag never changes the selection")

            await pilot.press("space")
            await pilot.pause()
            self.assertEqual(cells(0)[0], "[x]", "space selects the row")

            await pilot.press("space")
            await pilot.pause()
            self.assertEqual(cells(0)[0], "[ ]", "space toggles selection off")

            await pilot.press("f")
            await pilot.pause()
            self.assertNotEqual(screen.mode, "all", "'f' cycles the filter")

            from textual.widgets import Button

            active = screen.query_one(f"#filter-{screen.mode}", Button)
            self.assertEqual(active.variant, "primary", "the active filter is highlighted")
            self.assertEqual(screen.query_one("#filter-all", Button).variant, "default")
            self.assertIn(screen.mode, str(table.border_title))

            await pilot.press("r")
            await pilot.pause()
            self.assertTrue(app.dry_run, "'r' toggles dry run")

        self._run(body)

    def test_bulk_toggles_every_visible_row_when_nothing_selected(self):
        async def body(pilot, screen, app):
            self.assertTrue(all(i.want_subtitle for i in screen.shown))
            await pilot.press("S")
            await pilot.pause()
            self.assertTrue(
                all(not i.want_subtitle for i in screen.shown),
                "S with nothing selected falls back to every visible row",
            )
            await pilot.press("S")
            await pilot.pause()
            self.assertTrue(all(i.want_subtitle for i in screen.shown))

        self._run(body)


class SelectFlagTests(unittest.TestCase):
    """Selection and the per-file job flags are independent of each other.

    Regression: unchecking censoring (or subs/downmix) and then pressing
    space used to restore the flag, because the space handler reset every
    want_* from needs_* on select and cleared them on deselect.
    """

    @staticmethod
    def _items():
        # Mirrors scan.classify: a fresh item's flags start equal to its needs.
        return [
            scan_mod.MediaItem(
                path="a.mkv", size=1, duration=1.0,
                needs_subtitle=True, needs_audio=False, needs_censor=True,
                want_subtitle=True, want_audio=False, want_censor=True,
            ),
            scan_mod.MediaItem(
                path="b.mkv", size=1, duration=1.0,
                needs_subtitle=True, needs_audio=True, needs_censor=False,
                want_subtitle=True, want_audio=True, want_censor=False,
            ),
        ]

    def _run(self, body, items=None):
        async def main():
            import argparse

            cfg = argparse.Namespace(
                video_ext={".mkv"}, subtitle_ext={".srt"}, sub_language="eng",
                ffprobe="ffprobe", cache_path=None, model="small",
                device="cpu", compute_type="int8", jobs=2,
                swears_path=None, censor_track_title="Censored (Bleeparr)",
            )
            from mediafix import tui as tui_mod

            app = tui_mod.MediaFixApp(config=cfg, roots=[])
            app.result = scan_mod.ScanResult(
                roots=["."], items=list(items or self._items())
            )
            async with app.run_test() as pilot:
                await pilot.pause()
                app.push_screen(tui_mod.SelectScreen(app))
                await pilot.pause()
                await pilot.pause()
                await body(pilot, app.screen, app)

        asyncio.run(main())

    def test_space_preserves_an_unchecked_flag(self):
        async def body(pilot, screen, app):
            item = screen.shown[0]
            self.assertTrue(item.want_censor)
            await pilot.press("space")
            await pilot.pause()
            self.assertTrue(item.selected)

            await pilot.press("c")
            await pilot.pause()
            self.assertFalse(item.want_censor)

            await pilot.press("space")
            await pilot.pause()
            await pilot.press("space")
            await pilot.pause()
            self.assertTrue(item.selected)
            self.assertFalse(
                item.want_censor,
                "deselect + reselect must not restore a cleared flag",
            )

        self._run(body)

    def test_selection_buttons_preserve_flags(self):
        async def body(pilot, screen, app):
            item = screen.shown[0]
            await pilot.press("c")
            await pilot.pause()
            self.assertFalse(item.want_censor)

            await pilot.click("#sel-all")
            await pilot.pause()
            self.assertTrue(item.selected)
            self.assertFalse(item.want_censor, "Select all must not reset flags")

            await pilot.click("#sel-none")
            await pilot.pause()
            self.assertFalse(item.selected)
            self.assertFalse(item.want_censor, "None must not reset flags")

            await pilot.click("#sel-invert")
            await pilot.pause()
            self.assertTrue(item.selected)
            self.assertFalse(item.want_censor, "Invert must not reset flags")

        self._run(body)

    def test_bulk_scopes_to_the_selected_rows(self):
        async def body(pilot, screen, app):
            first, second = screen.shown
            await pilot.press("space")
            await pilot.pause()
            self.assertTrue(first.selected)

            # Only row 0 is selected and already has subs on -> one press
            # unchecks just that row.
            await pilot.press("S")
            await pilot.pause()
            self.assertFalse(first.want_subtitle)
            self.assertTrue(second.want_subtitle, "unselected rows are untouched")
            self.assertFalse(second.selected)

            await pilot.press("S")
            await pilot.pause()
            self.assertTrue(first.want_subtitle)
            self.assertTrue(first.selected)

        self._run(body)

    def test_bulk_checks_when_any_selected_row_is_unchecked(self):
        async def body(pilot, screen, app):
            first, second = screen.shown
            first.selected = second.selected = True
            first.want_subtitle = False
            await pilot.press("S")
            await pilot.pause()
            self.assertTrue(first.want_subtitle)
            self.assertTrue(second.want_subtitle)

        self._run(body)

    def test_toggles_clamp_to_what_the_file_needs(self):
        async def body(pilot, screen, app):
            first, second = screen.shown
            # Row 0 needs no downmix: 'a' must not flag it.
            await pilot.press("a")
            await pilot.pause()
            self.assertFalse(first.want_audio)
            self.assertFalse(first.selected, "a no-op toggle must not select")

            # Bulk downmix over a selection that includes the clean file.
            first.selected = second.selected = True
            second.want_audio = False
            await pilot.press("A")
            await pilot.pause()
            self.assertFalse(first.want_audio, "files without the need stay off")
            self.assertTrue(second.want_audio)
            self.assertTrue(second.selected)

        self._run(body)

    def test_dry_run_button_follows_the_key(self):
        async def body(pilot, screen, app):
            from textual.widgets import Button

            button = screen.query_one("#dry-btn", Button)
            self.assertEqual(str(button.label), "Dry run: off")
            await pilot.press("r")
            await pilot.pause()
            self.assertEqual(str(button.label), "Dry run: on")
            self.assertEqual(button.variant, "warning")
            await pilot.click("#dry-btn")
            await pilot.pause()
            self.assertEqual(str(button.label), "Dry run: off")
            self.assertFalse(app.dry_run)

        self._run(body)


class EnterStartsRunTests(unittest.TestCase):
    """The enter key has to start the run even though the table has focus.

    A focused DataTable binds "enter" to select_cursor, and the app hands a
    key to the focused widget before the screen binding is looked at, so the
    screen binding needs priority=True.
    """

    @staticmethod
    def _items():
        return [
            scan_mod.MediaItem(
                path="a.mkv", size=1, duration=1.0,
                needs_subtitle=True, want_subtitle=True,
                selected=True,
            ),
        ]

    def _run(self, body):
        import argparse
        import queue

        from mediafix import runner as runner_mod
        from mediafix import tui as tui_mod

        class FakeRunner:
            def __init__(self, config, dry_run=False, force=False):
                self.events = queue.Queue()

            def plan(self, items):
                self.items = items
                return [runner_mod.Job(kind="sub", path=i.path, item=i) for i in items]

            def wait(self, timeout=None):
                return True

            def run(self, on_ready=None):
                return None

            def cancel(self):
                pass

        async def main():
            cfg = argparse.Namespace(
                video_ext={".mkv"}, subtitle_ext={".srt"}, sub_language="eng",
                ffprobe="ffprobe", cache_path=None, model="small",
                device="cpu", compute_type="int8", jobs=2,
                swears_path=None, censor_track_title="Censored (Bleeparr)",
            )
            app = tui_mod.MediaFixApp(config=cfg, roots=[])
            app.result = scan_mod.ScanResult(roots=["."], items=self._items())
            with unittest.mock.patch.object(tui_mod.runner_mod, "Runner", FakeRunner), \
                 unittest.mock.patch.object(tui_mod.downmix_mod, "preflight", return_value=[]):
                async with app.run_test() as pilot:
                    await pilot.pause()
                    app.push_screen(tui_mod.SelectScreen(app))
                    await pilot.pause()
                    await pilot.pause()
                    await body(pilot, app.screen, app)

        asyncio.run(main())

    def test_enter_opens_the_run_screen(self):
        async def body(pilot, screen, app):
            from mediafix import tui as tui_mod

            self.assertIsInstance(screen, tui_mod.SelectScreen)
            await pilot.press("enter")
            await pilot.pause()
            await pilot.pause()
            self.assertIsInstance(
                app.screen, tui_mod.RunScreen,
                "enter must start the run instead of only moving the table cursor",
            )

    def test_enter_binding_is_priority(self):
        from mediafix import tui as tui_mod

        binding = [
            b for b in tui_mod.SelectScreen.BINDINGS
            if getattr(b, "key", None) == "enter"
        ]
        self.assertTrue(binding, "the enter binding is missing")
        self.assertTrue(
            binding[0].priority,
            "the enter binding must be priority, the focused table binds enter too",
        )


class FilterHidesSelectedRowsTests(unittest.TestCase):
    """Start must not drop the rows the filter hides.

    The filter only decides what is on screen. A selected row stays
    selected, so the run set is every selected row - not every visible one.
    """

    @staticmethod
    def _items():
        return [
            scan_mod.MediaItem(
                path="visible.mkv", size=1, duration=1.0,
                needs_subtitle=True, want_subtitle=True,
                selected=True,
            ),
            scan_mod.MediaItem(
                path="hidden.mkv", size=1, duration=1.0,
                needs_subtitle=False, want_subtitle=False,
                needs_censor=True, want_censor=True,
                selected=True,
            ),
        ]

    def _run(self, body):
        import argparse
        import queue

        from mediafix import runner as runner_mod
        from mediafix import tui as tui_mod

        class FakeRunner:
            def __init__(self, config, dry_run=False, force=False):
                self.events = queue.Queue()

            def plan(self, items):
                return [runner_mod.Job(kind="sub", path=i.path, item=i) for i in items]

            def wait(self, timeout=None):
                return True

            def run(self, on_ready=None):
                return None

            def cancel(self):
                pass

        async def main():
            cfg = argparse.Namespace(
                video_ext={".mkv"}, subtitle_ext={".srt"}, sub_language="eng",
                ffprobe="ffprobe", cache_path=None, model="small",
                device="cpu", compute_type="int8", jobs=2,
                swears_path=None, censor_track_title="Censored (Bleeparr)",
            )
            app = tui_mod.MediaFixApp(config=cfg, roots=[])
            app.result = scan_mod.ScanResult(roots=["."], items=self._items())
            with unittest.mock.patch.object(tui_mod.runner_mod, "Runner", FakeRunner), \
                 unittest.mock.patch.object(tui_mod.downmix_mod, "preflight", return_value=[]):
                async with app.run_test() as pilot:
                    await pilot.pause()
                    app.push_screen(tui_mod.SelectScreen(app))
                    await pilot.pause()
                    await pilot.pause()
                    await body(pilot, app.screen, app)

        asyncio.run(main())

    def test_start_runs_the_hidden_selected_rows_too(self):
        async def body(pilot, screen, app):
            from mediafix import tui as tui_mod

            screen.mode = scan_mod.FILTER_SUB
            screen._rebuild()
            await pilot.pause()
            self.assertEqual(
                [i.path for i in screen.shown], ["visible.mkv"],
                "the filter hides the second row",
            )

            await pilot.press("enter")
            await pilot.pause()
            await pilot.pause()
            self.assertIsInstance(app.screen, tui_mod.RunScreen)
            paths = sorted(i.path for i in app.screen.items)
            self.assertEqual(
                paths, ["hidden.mkv", "visible.mkv"],
                "the selected row hidden by the filter must still be run",
            )


class WildcardSearchTests(unittest.TestCase):
    """The search box filters the scan result and must not steal screen keys."""

    @staticmethod
    def _items():
        return [
            scan_mod.MediaItem(
                path="alpha.mkv", size=1, duration=1.0,
                needs_subtitle=True, needs_audio=True,
                want_subtitle=True, want_audio=True,
            ),
            scan_mod.MediaItem(
                path="beta.mkv", size=1, duration=1.0,
                needs_subtitle=True, want_subtitle=True,
            ),
            scan_mod.MediaItem(
                path="gamma.mkv", size=1, duration=1.0,
                needs_audio=True, want_audio=True,
            ),
        ]

    def _run(self, body):
        import argparse
        import queue

        from mediafix import runner as runner_mod
        from mediafix import tui as tui_mod

        class FakeRunner:
            def __init__(self, config, dry_run=False, force=False):
                self.events = queue.Queue()

            def plan(self, items):
                return [runner_mod.Job(kind="sub", path=i.path, item=i) for i in items]

            def wait(self, timeout=None):
                return True

            def run(self, on_ready=None):
                return None

            def cancel(self):
                pass

        async def main():
            cfg = argparse.Namespace(
                video_ext={".mkv"}, subtitle_ext={".srt"}, sub_language="eng",
                ffprobe="ffprobe", cache_path=None, model="small",
                device="cpu", compute_type="int8", jobs=2,
                swears_path=None, censor_track_title="Censored (Bleeparr)",
            )
            app = tui_mod.MediaFixApp(config=cfg, roots=[])
            app.result = scan_mod.ScanResult(roots=["."], items=self._items())
            with unittest.mock.patch.object(tui_mod.runner_mod, "Runner", FakeRunner), \
                 unittest.mock.patch.object(tui_mod.downmix_mod, "preflight", return_value=[]):
                async with app.run_test() as pilot:
                    await pilot.pause()
                    app.push_screen(tui_mod.SelectScreen(app))
                    await pilot.pause()
                    await pilot.pause()
                    await body(pilot, app.screen, app)

        asyncio.run(main())

    @staticmethod
    async def _type(pilot, text):
        for key in text:
            await pilot.press(key)
        await pilot.pause()

    def test_slash_focuses_the_box_and_filters_as_you_type(self):
        async def body(pilot, screen, app):
            from textual.widgets import DataTable

            from mediafix import tui as tui_mod

            search = screen.query_one("#search-input", tui_mod.SearchInput)
            self.assertFalse(search.has_focus)
            await pilot.press("slash")
            await pilot.pause()
            self.assertTrue(search.has_focus, "'/' focuses the search box")

            await self._type(pilot, "beta")
            self.assertEqual(search.value, "beta")
            self.assertEqual(screen.pattern, "beta")
            self.assertEqual([i.name for i in screen.shown], ["beta.mkv"])
            self.assertEqual(screen.query_one("#items", DataTable).row_count, 1)
            self.assertIn("beta", str(screen.query_one("#items", DataTable).border_title))

            alpha = app.result.items[0]
            self.assertTrue(alpha.want_subtitle, "typed letters must not toggle flags")
            self.assertTrue(alpha.want_audio, "typed letters must not toggle flags")
            self.assertFalse(alpha.selected, "typed letters must not select rows")

        self._run(body)

    def test_typing_s_does_not_toggle_subtitles(self):
        async def body(pilot, screen, app):
            from mediafix import tui as tui_mod

            alpha = app.result.items[0]
            alpha.want_subtitle = False
            await pilot.press("slash")
            await pilot.press("s")
            await pilot.pause()
            search = screen.query_one("#search-input", tui_mod.SearchInput)
            self.assertEqual(search.value, "s", "the letter belongs in the box")
            self.assertFalse(alpha.want_subtitle, "the screen's 's' binding must be disabled")

        self._run(body)

    def test_enter_returns_to_the_table_and_keeps_the_pattern(self):
        async def body(pilot, screen, app):
            from textual.widgets import DataTable

            from mediafix import tui as tui_mod

            await pilot.press("slash")
            await self._type(pilot, "beta")
            await pilot.press("enter")
            await pilot.pause()
            self.assertIsInstance(app.screen, tui_mod.SelectScreen)
            self.assertEqual(screen.pattern, "beta")
            self.assertEqual([i.name for i in screen.shown], ["beta.mkv"])
            self.assertTrue(
                screen.query_one("#items", DataTable).has_focus,
                "enter in the box must not start the run; it returns to the table",
            )

        self._run(body)

    def test_escape_clears_the_pattern_without_leaving_the_screen(self):
        async def body(pilot, screen, app):
            from textual.widgets import DataTable

            from mediafix import tui as tui_mod

            await pilot.press("slash")
            await self._type(pilot, "beta")
            self.assertEqual(screen.pattern, "beta")
            await pilot.press("escape")
            await pilot.pause()
            self.assertIsInstance(app.screen, tui_mod.SelectScreen)
            self.assertEqual(screen.pattern, "")
            self.assertEqual(len(screen.shown), 3)
            self.assertEqual(screen.query_one("#search-input", tui_mod.SearchInput).value, "")
            self.assertTrue(screen.query_one("#items", DataTable).has_focus)

        self._run(body)

    def test_pattern_ands_with_the_mode_filter(self):
        async def body(pilot, screen, app):
            from textual.widgets import DataTable

            screen.mode = scan_mod.FILTER_SUB
            screen._rebuild()
            await pilot.press("slash")
            await self._type(pilot, "a")
            self.assertEqual(
                [i.name for i in screen.shown], ["alpha.mkv", "beta.mkv"],
                "only sub rows whose name/path matches the pattern",
            )
            title = str(screen.query_one("#items", DataTable).border_title)
            self.assertEqual(title, "sub  a - 2 of 3 files")

        self._run(body)

    def test_a_row_hidden_by_the_pattern_is_still_run(self):
        async def body(pilot, screen, app):
            from mediafix import tui as tui_mod

            app.result.items[2].selected = True
            await pilot.press("slash")
            await self._type(pilot, "beta")
            self.assertEqual([i.name for i in screen.shown], ["beta.mkv"])
            await pilot.press("enter")
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            await pilot.pause()
            self.assertIsInstance(app.screen, tui_mod.RunScreen)
            self.assertEqual([i.name for i in app.screen.items], ["gamma.mkv"])

        self._run(body)


class ScanScreenReuseTests(unittest.TestCase):
    """Back from the file list returns to a usable scan screen.

    The scan button used to stay disabled after the first scan, and every
    rescan pushed another screen, so the stack grew and the buried screens
    were unreachable.
    """

    def test_back_returns_to_a_rearmed_scan_screen(self):
        import argparse

        from textual.widgets import Button

        from mediafix import tui as tui_mod

        async def main():
            cfg = argparse.Namespace(
                video_ext={".mkv"}, subtitle_ext={".srt"}, sub_language="eng",
                ffprobe="ffprobe", cache_path=None, model="small",
                device="cpu", compute_type="int8", jobs=2,
                swears_path=None, censor_track_title="Censored (Bleeparr)",
            )
            app = tui_mod.MediaFixApp(config=cfg, roots=[])
            app.result = scan_mod.ScanResult(roots=["."], items=[])
            async with app.run_test() as pilot:
                await pilot.pause()
                scan_screen = app.screen
                self.assertIsInstance(scan_screen, tui_mod.ScanScreen)
                scan_screen.query_one("#scan-btn", Button).disabled = True

                app.push_screen(tui_mod.SelectScreen(app))
                await pilot.pause()
                await pilot.pause()
                self.assertIsInstance(app.screen, tui_mod.SelectScreen)

                app.screen.action_back()
                await pilot.pause()
                await pilot.pause()
                self.assertIs(app.screen, scan_screen, "back returns to the scan screen")
                self.assertFalse(
                    scan_screen.query_one("#scan-btn", Button).disabled,
                    "the scan button has to be usable again",
                )

                # Rescan must not pile up another screen.
                depth = len(app.screen_stack)
                app.push_screen(tui_mod.SelectScreen(app))
                await pilot.pause()
                app.screen._rescan()
                await pilot.pause()
                await pilot.pause()
                self.assertEqual(
                    len(app.screen_stack), depth,
                    "rescan must not grow the screen stack",
                )

        asyncio.run(main())


class CancelledDownmixTests(unittest.TestCase):
    """A cancelled downmix must leave the original file alone."""

    @staticmethod
    def _vendor():
        import audio_downmix

        return audio_downmix

    def test_cancelled_run_keeps_the_original(self):
        vendor = self._vendor()
        from mediafix import config as config_mod
        from mediafix import downmix as downmix_mod

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            src = root / "movie.mkv"
            src.write_bytes(b"original bytes")
            tmp_out = root / ".movie.tmp.mkv"

            args = downmix_mod.build_args(config_mod.Config())
            calls = {"n": 0}

            def cancel_check():
                calls["n"] += 1
                # The first check happens before ffmpeg runs, the second one
                # right before the original would be replaced.
                return calls["n"] > 1

            args.cancel_check = cancel_check

            video = vendor.VideoFile(
                str(src), None,
                [vendor.AudioStream(0, "ac3", 6, "5.1", "eng")],
                [],
            )

            def fake_run_ffmpeg(cmd, verbose, cancel_check=None):
                tmp_out.write_bytes(b"half written")

            def fake_remux(mkvmerge, path, verbose, cancel_check=None):
                return path

            with unittest.mock.patch.object(vendor, "analyze_file", return_value=video), \
                 unittest.mock.patch.object(vendor, "run_ffmpeg", fake_run_ffmpeg), \
                 unittest.mock.patch.object(vendor, "remux_with_mkvmerge", fake_remux), \
                 unittest.mock.patch.object(
                     vendor, "resolve_output_path", return_value=(str(tmp_out), True)):
                _, _, status, message = vendor.process_one(args, str(src), None)

            self.assertEqual(status, vendor.STATUS_CANCELLED)
            self.assertEqual(src.read_bytes(), b"original bytes")
            self.assertFalse(tmp_out.exists(), "the half written file must be removed")

    def test_cancelled_before_start_keeps_the_original(self):
        vendor = self._vendor()
        from mediafix import config as config_mod
        from mediafix import downmix as downmix_mod

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            src = root / "movie.mkv"
            src.write_bytes(b"original bytes")

            args = downmix_mod.build_args(config_mod.Config())
            args.cancel_check = lambda: True

            video = vendor.VideoFile(
                str(src), None,
                [vendor.AudioStream(0, "ac3", 6, "5.1", "eng")],
                [],
            )
            with unittest.mock.patch.object(vendor, "analyze_file", return_value=video):
                _, _, status, _message = vendor.process_one(args, str(src), None)

            self.assertEqual(status, vendor.STATUS_CANCELLED)
            self.assertEqual(src.read_bytes(), b"original bytes")

    def test_runner_maps_the_cancelled_status(self):
        from mediafix import runner as runner_mod

        self.assertEqual(runner_mod.JOB_CANCELLED, "cancelled")


class CliOptionTests(unittest.TestCase):
    """Options that were accepted but ignored."""

    def test_config_can_come_after_the_subcommand(self):
        from mediafix import cli as cli_mod

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            config_path = root / "config.toml"
            config_path.write_text(
                'model = "large"\n', encoding="utf-8"
            )
            media = root / "media"
            media.mkdir()
            (media / "a.mkv").write_bytes(b"x")

            code = cli_mod.main([
                "scan", "--config", str(config_path), "--json", str(media),
            ])
            self.assertEqual(code, cli_mod.EXIT_OK)

    def test_config_before_the_subcommand_still_works(self):
        from mediafix import cli as cli_mod

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            config_path = root / "config.toml"
            config_path.write_text('model = "large"\n', encoding="utf-8")
            media = root / "media"
            media.mkdir()
            (media / "a.mkv").write_bytes(b"x")

            code = cli_mod.main([
                "--config", str(config_path), "scan", "--json", str(media),
            ])
            self.assertEqual(code, cli_mod.EXIT_OK)

    def test_scan_honours_only(self):
        from mediafix import cli as cli_mod

        def fake_scan(roots, config, **kwargs):
            sub = scan_mod.MediaItem(
                path="sub.mkv", needs_subtitle=True, want_subtitle=True,
            )
            aud = scan_mod.MediaItem(
                path="aud.mkv", needs_audio=True, want_audio=True,
            )
            return scan_mod.ScanResult(roots=list(roots), items=[sub, aud])

        with tempfile.TemporaryDirectory() as td:
            media = Path(td) / "media"
            media.mkdir()
            (media / "a.mkv").write_bytes(b"x")

            with unittest.mock.patch.object(cli_mod.scan_mod, "scan", fake_scan):
                import io
                import contextlib

                buffer = io.StringIO()
                with contextlib.redirect_stdout(buffer):
                    cli_mod.main(["scan", "--only", "sub", str(media)])
            output = buffer.getvalue()
            self.assertIn("sub.mkv", output)
            self.assertNotIn("aud.mkv", output)

    def test_scan_honours_match(self):
        from mediafix import cli as cli_mod

        def fake_scan(roots, config, **kwargs):
            return scan_mod.ScanResult(roots=list(roots), items=[
                scan_mod.MediaItem(
                    path="/media/Star.Wars.S01E01.mkv", needs_subtitle=True,
                ),
                scan_mod.MediaItem(path="/media/Other.mp4", needs_audio=True),
            ])

        with tempfile.TemporaryDirectory() as td:
            media = Path(td) / "media"
            media.mkdir()
            (media / "a.mkv").write_bytes(b"x")

            with unittest.mock.patch.object(cli_mod.scan_mod, "scan", fake_scan):
                import contextlib
                import io

                buffer = io.StringIO()
                with contextlib.redirect_stdout(buffer):
                    cli_mod.main(["scan", "--match", "*star*", str(media)])
            output = buffer.getvalue()
            self.assertIn("Star.Wars.S01E01.mkv", output)
            self.assertNotIn("Other.mp4", output)

    def test_apply_honours_match(self):
        from mediafix import cli as cli_mod

        def fake_scan(roots, config, **kwargs):
            return scan_mod.ScanResult(roots=list(roots), items=[
                scan_mod.MediaItem(
                    path="/media/Star.Wars.S01E01.mkv",
                    needs_subtitle=True, want_subtitle=True,
                ),
                scan_mod.MediaItem(
                    path="/media/Other.mp4", needs_audio=True, want_audio=True,
                ),
            ])

        seen = {}

        def fake_batch(config, items, dry_run, force, assume_yes):
            seen["paths"] = [i.path for i in items]
            return cli_mod.EXIT_OK

        with tempfile.TemporaryDirectory() as td:
            media = Path(td) / "media"
            media.mkdir()
            (media / "a.mkv").write_bytes(b"x")

            with unittest.mock.patch.object(cli_mod.scan_mod, "scan", fake_scan), \
                 unittest.mock.patch.object(cli_mod, "_run_batch", fake_batch):
                import contextlib
                import io

                with contextlib.redirect_stdout(io.StringIO()):
                    code = cli_mod.main(["apply", "--match", "star", str(media)])
            self.assertEqual(code, cli_mod.EXIT_OK)
            self.assertEqual(seen["paths"], ["/media/Star.Wars.S01E01.mkv"])

    def test_cache_option_reaches_the_config(self):
        from mediafix import cli as cli_mod
        from mediafix import config as config_mod

        seen = {}

        def fake_scan(roots, config, **kwargs):
            seen["cache_path"] = config.cache_path
            return scan_mod.ScanResult(roots=list(roots), items=[])

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            media = root / "media"
            media.mkdir()
            (media / "a.mkv").write_bytes(b"x")
            cache = root / "probe-cache.json"

            with unittest.mock.patch.object(cli_mod.scan_mod, "scan", fake_scan):
                cli_mod.main(["scan", "--cache", str(cache), str(media)])

            self.assertEqual(seen["cache_path"], str(cache))

    def test_config_load_keeps_the_cache_path(self):
        from mediafix import config as config_mod

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            config_path = root / "config.toml"
            config_path.write_text(
                'cache_path = "%s"\n' % (root / "from-config.json").as_posix(),
                encoding="utf-8",
            )
            config = config_mod.load(str(config_path), {})
            self.assertEqual(
                config.cache_path, (root / "from-config.json").as_posix()
            )


if __name__ == "__main__":
    unittest.main()
