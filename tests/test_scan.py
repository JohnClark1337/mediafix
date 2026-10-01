import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mediafix import srt as srt_mod
from mediafix import scan as scan_mod
from mediafix.probe import AudioStreamInfo, MediaInfo, SubtitleStreamInfo


def info(path="x.mkv", audio=(), subs=(), duration=100.0):
    return MediaInfo(
        path=path, size=1024, duration=duration,
        audio=[AudioStreamInfo(**a) for a in audio],
        subtitles=[SubtitleStreamInfo(**s) for s in subs],
    )


SURROUND_EN = dict(index=0, codec="ac3", channels=6, layout="5.1", language="eng")
STEREO_EN = dict(index=0, codec="aac", channels=2, layout="stereo", language="eng")
STEREO_FR = dict(index=0, codec="aac", channels=2, layout="stereo", language="fre")


class TimestampTests(unittest.TestCase):
    def test_basic(self):
        self.assertEqual(srt_mod.format_timestamp(0), "00:00:00,000")
        self.assertEqual(srt_mod.format_timestamp(3661.5), "01:01:01,500")

    def test_rounding_rollover(self):
        self.assertEqual(srt_mod.format_timestamp(1.9999), "00:00:02,000")
        self.assertEqual(srt_mod.format_timestamp(59.9999), "00:01:00,000")

    def test_negative_clamped(self):
        self.assertEqual(srt_mod.format_timestamp(-5), "00:00:00,000")


class RenderTests(unittest.TestCase):
    def test_numbering_and_gaps(self):
        from mediafix.subtitles import Segment

        text = srt_mod.render_srt([
            Segment(0, 1.5, "One"),
            Segment(1.5, 3, "Two"),
        ])
        self.assertEqual(
            text,
            "1\n00:00:00,000 --> 00:00:01,500\nOne\n\n"
            "2\n00:00:01,500 --> 00:00:03,000\nTwo\n",
        )

    def test_blank_segments_dropped_without_gaps(self):
        from mediafix.subtitles import Segment

        text = srt_mod.render_srt([Segment(0, 1, "A"), Segment(1, 2, "   ")])
        self.assertNotIn("2\n", text)

    def test_end_never_precedes_start(self):
        from mediafix.subtitles import Segment

        text = srt_mod.render_srt([Segment(5, 5, "Same")])
        self.assertIn("00:00:05,000 --> 00:00:05,050", text)


class AtomicWriteTests(unittest.TestCase):
    def test_write_and_no_temp_left(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "movie.eng.srt"
            srt_mod.write_atomic(target, "hello\n")
            self.assertEqual(target.read_text(encoding="utf-8"), "hello\n")
            self.assertEqual([p.name for p in Path(tmp).iterdir()], ["movie.eng.srt"])

    def test_overwrites_existing(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "movie.eng.srt"
            target.write_text("old", encoding="utf-8")
            srt_mod.write_atomic(target, "new")
            self.assertEqual(target.read_text(encoding="utf-8"), "new")

    def test_sidecar_naming(self):
        self.assertEqual(srt_mod.sidecar_path("/m/Show.S01E01.mkv", "en").name, "Show.S01E01.eng.srt")
        self.assertEqual(srt_mod.sidecar_path("/m/Movie.mp4", "spa").name, "Movie.spa.srt")


class SidecarDetectionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.video = self.root / "Movie.mkv"
        self.video.write_bytes(b"0")

    def tearDown(self):
        self.tmp.cleanup()

    def _touch(self, name):
        (self.root / name).write_text("x", encoding="utf-8")

    def _find(self):
        return scan_mod.find_sidecars(self.video, (".srt", ".ass"))

    def test_no_sidecars(self):
        self.assertEqual(self._find(), [])

    def test_plain_srt_counts_as_english(self):
        self._touch("Movie.srt")
        found = self._find()
        self.assertEqual(len(found), 1)
        self.assertTrue(found[0].is_english)

    def test_language_tagged(self):
        self._touch("Movie.en.srt")
        self._touch("Movie.fr.srt")
        found = {s.language: s for s in self._find()}
        self.assertTrue(found["en"].is_english)
        self.assertFalse(found["fr"].is_english)

    def test_forced_english_does_not_satisfy(self):
        self._touch("Movie.en.forced.srt")
        found = self._find()
        self.assertTrue(found[0].is_english)
        self.assertTrue(found[0].is_forced)

    def test_similar_stem_ignored(self):
        self._touch("Movie2.srt")
        self._touch("OtherMovie.srt")
        self.assertEqual(self._find(), [])

    def test_ass_supported(self):
        self._touch("Movie.eng.ass")
        found = self._find()
        self.assertEqual(len(found), 1)
        self.assertTrue(found[0].is_english)


class ClassifyTests(unittest.TestCase):
    def setUp(self):
        self.cfg = scan_mod.MediaItem(path="x.mkv")
        self.cfg.__class__ = scan_mod.MediaItem

    def test_surround_only_needs_both(self):
        item = scan_mod.classify("x.mkv", info(audio=[SURROUND_EN]), [], "en")
        self.assertTrue(item.needs_subtitle)
        self.assertTrue(item.needs_audio)
        self.assertTrue(item.selectable)
        self.assertTrue(item.want_subtitle and item.want_audio)

    def test_english_sidecar_clears_subtitle_need(self):
        sidecars = [scan_mod.SidecarInfo("x.eng.srt", "eng", True, False)]
        item = scan_mod.classify("x.mkv", info(audio=[SURROUND_EN]), sidecars, "en")
        self.assertFalse(item.needs_subtitle)
        self.assertTrue(item.needs_audio)

    def test_french_sidecar_does_not_clear(self):
        sidecars = [scan_mod.SidecarInfo("x.fre.srt", "fre", False, False)]
        item = scan_mod.classify("x.mkv", info(audio=[SURROUND_EN]), sidecars, "en")
        self.assertTrue(item.needs_subtitle)

    def test_embedded_english_subtitle_clears(self):
        subs = [dict(index=2, codec="subrip", language="eng")]
        item = scan_mod.classify("x.mkv", info(audio=[SURROUND_EN], subs=subs), [], "en")
        self.assertFalse(item.needs_subtitle)

    def test_existing_english_stereo_clears_audio(self):
        audio = [SURROUND_EN, dict(index=1, codec="aac", channels=2, language="eng")]
        item = scan_mod.classify("x.mkv", info(audio=audio), [], "en")
        self.assertFalse(item.needs_audio)
        self.assertTrue(item.needs_subtitle)

    def test_stereo_only_still_needs_subtitles(self):
        item = scan_mod.classify("x.mkv", info(audio=[STEREO_EN]), [], "en")
        self.assertFalse(item.needs_audio)
        self.assertTrue(item.needs_subtitle)
        self.assertTrue(item.selectable)

    def test_fully_clean_file_needs_nothing(self):
        sidecars = [scan_mod.SidecarInfo("x.eng.srt", "eng", True, False)]
        item = scan_mod.classify("x.mkv", info(audio=[STEREO_EN]), sidecars, "en")
        self.assertFalse(item.needs_any)
        self.assertFalse(item.selectable)
        self.assertFalse(item.want_subtitle)
        self.assertFalse(item.want_audio)

    def test_foreign_stereo_does_not_count_as_english(self):
        item = scan_mod.classify("x.mkv", info(audio=[SURROUND_EN, STEREO_FR]), [], "en")
        self.assertTrue(item.needs_audio)

    def test_no_audio_unselectable(self):
        item = scan_mod.classify("x.mkv", info(audio=[]), [], "en")
        self.assertFalse(item.selectable)
        self.assertIn("no audio", item.error)

    def test_probe_error_unselectable(self):
        broken = info(audio=[SURROUND_EN])
        broken.error = "ffprobe failed"
        item = scan_mod.classify("x.mkv", broken, [], "en")
        self.assertFalse(item.selectable)
        self.assertFalse(item.needs_any)


class WalkTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_skips_junk_directories(self):
        for name in ["a.mkv", "@eaDir/b.mkv", ".hidden/c.mkv", "nightmix_output/d.mkv",
                     "Trash/.Trash-0/e.mkv", ".dotfile/f.mkv", "keep.txt"]:
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"0")

        found = [Path(p).name for p in scan_mod.walk([str(self.root)], (".mkv",))]
        self.assertEqual(sorted(found), ["a.mkv"])

    def test_recurses_and_sorts(self):
        for name in ["z.mkv", "sub/a.mkv", "sub/deep/m.mkv"]:
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"0")
        found = [Path(p).name for p in scan_mod.walk([str(self.root)], (".mkv",))]
        self.assertEqual(found, ["a.mkv", "m.mkv", "z.mkv"])

    def test_single_file_accepted(self):
        target = self.root / "one.mp4"
        target.write_bytes(b"0")
        self.assertEqual(scan_mod.walk([str(target)], (".mp4",)), [str(target)])


class FilterTests(unittest.TestCase):
    def _items(self):
        both = scan_mod.MediaItem(path="b.mkv", needs_subtitle=True, needs_audio=True)
        sub = scan_mod.MediaItem(path="s.mkv", needs_subtitle=True)
        aud = scan_mod.MediaItem(path="a.mkv", needs_audio=True)
        clean = scan_mod.MediaItem(path="c.mkv")
        return [both, sub, aud, clean]

    def _paths(self, mode):
        return [i.path for i in scan_mod.filter_items(self._items(), mode)]

    def test_modes(self):
        self.assertEqual(self._paths("all"), ["b.mkv", "s.mkv", "a.mkv", "c.mkv"])
        self.assertEqual(self._paths("sub"), ["b.mkv", "s.mkv"])
        self.assertEqual(self._paths("audio"), ["b.mkv", "a.mkv"])
        self.assertEqual(self._paths("both"), ["b.mkv"])
        self.assertEqual(self._paths("clean"), ["c.mkv"])


class HumanTests(unittest.TestCase):
    def test_size(self):
        self.assertEqual(scan_mod.human_size(512), "512 B")
        self.assertEqual(scan_mod.human_size(1024 ** 3), "1.0 GB")

    def test_duration(self):
        self.assertEqual(scan_mod.human_duration(None), "--:--")
        self.assertEqual(scan_mod.human_duration(65), "1:05")
        self.assertEqual(scan_mod.human_duration(3725), "1:02:05")


if __name__ == "__main__":
    unittest.main(verbosity=2)