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

            await pilot.press("s")
            await pilot.pause()
            self.assertEqual(cells(0)[0], "[x]", "re-pressing 's' should select the row")

            await pilot.press("space")
            await pilot.pause()
            self.assertEqual(cells(0)[0], "[ ]", "space toggles selection off")

            await pilot.press("f")
            await pilot.pause()
            self.assertNotEqual(screen.mode, "all", "'f' cycles the filter")

            await pilot.press("r")
            await pilot.pause()
            self.assertTrue(app.dry_run, "'r' toggles dry run")

        self._run(body)

    def test_bulk_toggle_selects_all_shown(self):
        async def body(pilot, screen, app):
            await pilot.press("S")
            await pilot.pause()
            self.assertTrue(all(i.want_subtitle for i in screen.shown))

        self._run(body)


if __name__ == "__main__":
    unittest.main()