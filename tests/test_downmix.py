import os
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import audio_downmix as vendor

from mediafix import config as config_mod
from mediafix import downmix as downmix_mod


class BuildArgsTests(unittest.TestCase):
    def setUp(self):
        self.config = config_mod.Config()

    def test_defaults_are_in_place_and_lossless_containers(self):
        args = downmix_mod.build_args(self.config)
        self.assertTrue(args.in_place)
        self.assertFalse(args.dry_run)
        self.assertFalse(args.force)
        self.assertTrue(args.remux)
        self.assertTrue(args.loudnorm)
        self.assertFalse(args.replace)
        self.assertEqual(args.enhance, 1.5)
        self.assertEqual(args.voice, 2.0)
        self.assertEqual(args.bitrate, "192k")
        self.assertEqual(args.loudness, -16.0)

    def test_tool_paths_flow_through(self):
        config = replace(self.config, ffmpeg="/opt/ffmpeg", ffprobe="/opt/ffprobe",
                         mkvmerge="/opt/mkvmerge")
        args = downmix_mod.build_args(config)
        self.assertEqual(args.ffmpeg, "/opt/ffmpeg")
        self.assertEqual(args.ffprobe, "/opt/ffprobe")
        self.assertEqual(args.mkvmerge, "/opt/mkvmerge")

    def test_toggles(self):
        args = downmix_mod.build_args(
            replace(self.config, loudnorm=False, remux=False, replace=True),
            force=True, dry_run=True,
        )
        self.assertFalse(args.loudnorm)
        self.assertFalse(args.remux)
        self.assertTrue(args.replace)
        self.assertTrue(args.force)
        self.assertTrue(args.dry_run)

    def test_paths_placeholder_is_the_only_path(self):
        args = downmix_mod.build_args(self.config)
        self.assertEqual(list(args.paths), ["__placeholder__"])

    def test_enhance_zero_disables_filter_requirement(self):
        args = downmix_mod.build_args(replace(self.config, enhance=0))
        with mock.patch.object(vendor, "has_filter") as has_filter:
            vendor.check_filter_support(args)
        has_filter.assert_not_called()

    def test_enhance_positive_requires_filter(self):
        args = downmix_mod.build_args(self.config)
        with mock.patch.object(vendor, "has_filter", return_value=False):
            with self.assertRaises(vendor.ToolError):
                vendor.check_filter_support(args)


class SpaceCheckTests(unittest.TestCase):
    def setUp(self):
        self.config = config_mod.Config()
        self.size = 4 * 1024 ** 3

    def test_reports_shortage(self):
        with mock.patch.object(downmix_mod, "free_space_bytes", return_value=2 * 1024 ** 3):
            message = downmix_mod.check_space("/x", self.size, margin_gb=2)
        self.assertIsNotNone(message)
        self.assertIn("insufficient free space", message)

    def test_passes_when_enough(self):
        with mock.patch.object(downmix_mod, "free_space_bytes", return_value=10 * 1024 ** 3):
            self.assertIsNone(downmix_mod.check_space("/x", self.size, margin_gb=2))

    def test_unknown_space_does_not_block(self):
        with mock.patch.object(downmix_mod, "free_space_bytes", return_value=0):
            self.assertIsNone(downmix_mod.check_space("/x", self.size, margin_gb=2))

    def test_margin_widens_requirement(self):
        with mock.patch.object(downmix_mod, "free_space_bytes", return_value=5 * 1024 ** 3):
            self.assertIsNone(downmix_mod.check_space("/x", self.size, margin_gb=0))
            self.assertIsNotNone(downmix_mod.check_space("/x", self.size, margin_gb=4))


class TempMonitorTests(unittest.TestCase):
    def test_finds_temp_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "Movie.mkv"
            source.write_bytes(b"x" * 10)
            monitor = downmix_mod.TempSizeMonitor(str(source))
            self.assertEqual(monitor.bytes_written, 0)

            temp = Path(tmp) / ".Movie.deadbeef.mkv"
            temp.write_bytes(b"x" * 500)
            self.assertEqual(monitor.bytes_written, 500)

            remux = Path(tmp) / ".Movie.deadbeef.remux.mkv"
            remux.write_bytes(b"x" * 900)
            self.assertEqual(monitor.bytes_written, 900)

            unrelated = Path(tmp) / ".Other.deadbeef.mkv"
            unrelated.write_bytes(b"x" * 7000)
            self.assertEqual(monitor.bytes_written, 900)

    def test_missing_directory_is_safe(self):
        monitor = downmix_mod.TempSizeMonitor("/nonexistent/dir/Movie.mkv")
        self.assertEqual(monitor.bytes_written, 0)


class RunOneTests(unittest.TestCase):
    def setUp(self):
        self.args = downmix_mod.build_args(config_mod.Config())

    def test_maps_processed(self):
        with mock.patch.object(vendor, "process_one",
                               return_value=("s", None, vendor.STATUS_PROCESSED, "1 track(s)")):
            status, message = downmix_mod.run_one(self.args, "/x/Movie.mkv")
        self.assertEqual(status, vendor.STATUS_PROCESSED)
        self.assertIn("1 track", message)

    def test_maps_failure(self):
        with mock.patch.object(vendor, "process_one",
                               return_value=("s", None, vendor.STATUS_FAILED, "ffmpeg exited 1")):
            status, message = downmix_mod.run_one(self.args, "/x/Movie.mkv")
        self.assertEqual(status, vendor.STATUS_FAILED)
        self.assertIn("ffmpeg exited 1", message)

    def test_unexpected_exception_is_contained(self):
        with mock.patch.object(vendor, "process_one", side_effect=ZeroDivisionError("boom")):
            status, message = downmix_mod.run_one(self.args, "/x/Movie.mkv")
        self.assertEqual(status, vendor.STATUS_FAILED)
        self.assertIn("ZeroDivisionError", message)

    def test_skip_statuses_pass_through(self):
        for status in (vendor.STATUS_SKIP_STEREO, vendor.STATUS_NO_AUDIO, vendor.STATUS_UNCHANGED):
            with mock.patch.object(vendor, "process_one",
                                   return_value=("s", None, status, "reason")):
                got, _ = downmix_mod.run_one(self.args, "/x/Movie.mkv")
            self.assertEqual(got, status)


class ResolveOutputTests(unittest.TestCase):
    def test_in_place_uses_hidden_temp_beside_source(self):
        dst, is_temp = vendor.resolve_output_path("/lib/Movie.mkv", None, "./out", True)
        self.assertTrue(is_temp)
        self.assertTrue(os.path.basename(dst).startswith(".Movie."))
        self.assertTrue(dst.endswith(".mkv"))
        self.assertEqual(os.path.dirname(dst), "/lib")


if __name__ == "__main__":
    unittest.main(verbosity=2)