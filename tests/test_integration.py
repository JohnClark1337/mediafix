import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mediafix import config as config_mod
from mediafix import downmix as downmix_mod
from mediafix import srt as srt_mod
from mediafix import scan as scan_mod
from mediafix.probe import probe

HAVE_TOOLS = all(shutil.which(t) for t in ("ffmpeg", "ffprobe"))


def make_surround(path, seconds=2, channels="5.1", codec="ac3", video=True):
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y"]
    if video:
        cmd += ["-f", "lavfi", "-i", "color=c=black:s=64x64:r=5"]
    cmd += [
        "-f", "lavfi", "-i", f"anullsrc=r=48000:cl={channels}",
        "-t", str(seconds), "-c:a", codec, str(path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise unittest.SkipTest("could not synthesize test clip: " + result.stderr[:200])
    return str(path)


@unittest.skipUnless(HAVE_TOOLS, "ffmpeg/ffprobe not installed")
class RealProbeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_surround_probe_classification(self):
        source = self.root / "Movie.mkv"
        make_surround(source)

        info = probe(source)
        self.assertIsNone(info.error)
        self.assertEqual(len(info.audio), 1)
        self.assertEqual(info.audio[0].channels, 6)
        self.assertTrue(info.has_surround)
        self.assertFalse(info.has_english_stereo)
        self.assertTrue(info.needs_audio)
        self.assertIsNotNone(info.duration)

        item = scan_mod.classify(source, info, [], "en")
        self.assertTrue(item.needs_audio)
        self.assertTrue(item.needs_subtitle)
        self.assertGreater(item.size, 0)

    def test_stereo_only_probe(self):
        source = self.root / "Mono.mkv"
        make_surround(source, channels="stereo", codec="aac")
        info = probe(source)
        self.assertEqual(info.audio[0].channels, 2)
        self.assertFalse(info.needs_audio)

    def test_sidecar_clears_need_after_write(self):
        source = self.root / "Movie.mkv"
        make_surround(source)

        before = scan_mod.classify(source, probe(source), [], "en")
        self.assertTrue(before.needs_subtitle)

        destination = srt_mod.sidecar_path(source, "en")
        srt_mod.write_atomic(destination, srt_mod.render_srt([]) or "1\n00:00:00,000 --> 00:00:01,000\nHi\n")
        self.assertTrue(destination.exists())

        after = scan_mod.classify(
            source, probe(source), scan_mod.find_sidecars(source, (".srt",)), "en"
        )
        self.assertFalse(after.needs_subtitle)
        self.assertTrue(after.needs_audio)

    def test_full_scan_walks_real_tree(self):
        make_surround(self.root / "a.mkv")
        make_surround(self.root / "sub" / "b.mkv") if (self.root / "sub").mkdir() or True else None
        make_surround(self.root / "@eaDir" / "c.mkv") if (self.root / "@eaDir").mkdir() or True else None

        result = scan_mod.scan([str(self.root)], config_mod.Config(), jobs=4, use_cache=False)
        names = sorted(Path(i.path).name for i in result.items)
        self.assertEqual(names, ["a.mkv", "b.mkv"])
        self.assertEqual(result.needs_audio_count, 2)


@unittest.skipUnless(HAVE_TOOLS, "ffmpeg/ffprobe not installed")
class RealDownmixTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.config = replace(config_mod.Config(), remux=shutil.which("mkvmerge") is not None)

    def tearDown(self):
        self.tmp.cleanup()

    def test_in_place_downmix_roundtrip(self):
        source = self.root / "Movie.mkv"
        make_surround(source)

        before = probe(source)
        self.assertTrue(before.needs_audio)

        args = downmix_mod.build_args(self.config)
        status, message = downmix_mod.run_one(args, str(source))
        self.assertEqual(status, "processed", message)

        after = probe(source)
        self.assertTrue(after.has_english_stereo, "expected a new English stereo track")
        self.assertTrue(after.has_surround, "original surround track must be preserved")
        self.assertEqual(len(after.audio), 2)
        self.assertFalse(after.needs_audio, "re-scan should no longer flag this file")

    def test_second_pass_is_skipped_not_repeated(self):
        source = self.root / "Movie.mkv"
        make_surround(source)

        args = downmix_mod.build_args(self.config)
        first, _ = downmix_mod.run_one(args, str(source))
        self.assertEqual(first, "processed")

        second, message = downmix_mod.run_one(args, str(source))
        self.assertEqual(second, "skip-has-stereo")
        self.assertEqual(len(probe(source).audio), 2)

    def test_dry_run_changes_nothing(self):
        source = self.root / "Movie.mkv"
        make_surround(source)
        before = os.path.getsize(source)

        args = downmix_mod.build_args(self.config, dry_run=True)
        status, _ = downmix_mod.run_one(args, str(source))
        self.assertEqual(status, "processed")
        self.assertEqual(os.path.getsize(source), before)
        self.assertEqual(len(probe(source).audio), 1)

    def test_force_adds_a_second_stereo_track(self):
        source = self.root / "Movie.mkv"
        make_surround(source)
        args = downmix_mod.build_args(self.config)
        downmix_mod.run_one(args, str(source))
        self.assertEqual(len(probe(source).audio), 2)

        forced = downmix_mod.build_args(self.config, force=True)
        status, _ = downmix_mod.run_one(forced, str(source))
        self.assertEqual(status, "processed")
        self.assertEqual(len(probe(source).audio), 3)

    def test_no_remux_path(self):
        source = self.root / "Movie.mkv"
        make_surround(source)
        config = replace(self.config, remux=False)
        args = downmix_mod.build_args(config)
        self.assertFalse(args.remux)
        status, message = downmix_mod.run_one(args, str(source))
        self.assertEqual(status, "processed", message)
        self.assertTrue(probe(source).has_english_stereo)

    def test_corrupt_input_fails_without_losing_the_file(self):
        source = self.root / "Broken.mkv"
        source.write_bytes(b"not a real matroska file")
        original = source.read_bytes()

        args = downmix_mod.build_args(self.config)
        status, _ = downmix_mod.run_one(args, str(source))
        self.assertEqual(status, "failed")
        self.assertTrue(source.exists())
        self.assertEqual(source.read_bytes(), original)
        self.assertEqual([p.name for p in self.root.iterdir()], ["Broken.mkv"])


if __name__ == "__main__":
    unittest.main(verbosity=2)