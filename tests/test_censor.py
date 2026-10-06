import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mediafix import config as config_mod
from mediafix.censor import (
    CensorEngine, SwearMatcher, TimedSegment, _bleeptool_tiers, _build_censored_filtergraph,
    _fix_absolute_times, _normalize_whisper_word, _parse_ass, _parse_srt_loose, _parse_sub,
    _parse_vtt, _tier_model, bad_sections_from_file, find_bad_sections, fuzzy_match,
    load_swears, mask_word, merge_whisper_and_subtitles, parse_timed_segments, select_mutes,
)


def matches(*entries):
    """Build a matcher from entries in the swears.txt layout.

    A bare string like "asshole" is treated as a swear with no substitution;
    "blow job|ballgame" keeps the substitution part (which is ignored).
    """
    terms = []
    for entry in entries:
        terms.append(entry if "|" in entry else f"{entry}|")
    return _matcher_from_text(terms)


def _matcher_from_text(entries: list[str] | None):
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="utf-8") as handle:
        handle.write("\n".join(entries or []))
        path = handle.name
    try:
        return load_swears(path)
    finally:
        os.unlink(path)


class SwearListTests(unittest.TestCase):
    def setUp(self):
        self.matcher = _matcher_from_text(["asshole|jerk", "shit", "blow job"])

    def test_single_word_matches(self):
        self.assertTrue(self.matcher.any_in("you asshole"))
        self.assertTrue(self.matcher.find("say shit twice").count("shit") == 1)

    def test_word_boundaries(self):
        self.assertFalse(self.matcher.any_in("theshithole"))
        self.assertFalse(self.matcher.any_in("baseless"))

    def test_case_insensitive(self):
        self.assertTrue(self.matcher.any_in("ASSHOLE"))
        self.assertTrue(self.matcher.find("Blow Job") == ["blow job"])

    def test_phrase_does_not_match_single_word(self):
        self.assertFalse(self.matcher.any_in("a blow"))
        self.assertFalse(self.matcher.any_in("job"))

    def test_find_returns_distinct_lowercased(self):
        self.assertEqual(self.matcher.find("Shit SHIT shit"), ["shit"])

    def test_word_is_bad(self):
        self.assertTrue(self.matcher.word_is_bad("asshole"))
        self.assertTrue(self.matcher.word_is_bad("shit"))
        self.assertFalse(self.matcher.word_is_bad("blow job"))  # phrase, not single word
        self.assertFalse(self.matcher.word_is_bad("kind"))

    def test_empty_matcher_matches_nothing(self):
        empty = matches()
        self.assertFalse(empty.any_in(""))
        self.assertFalse(empty.any_in("asshole"))
        self.assertEqual(empty.find("asshole"), [])

    def test_packaged_list_contains_known_entries(self):
        matcher = load_swears()
        self.assertTrue(matcher.any_in("you asshole"))
        self.assertTrue(matcher.any_in("what the shit"))
        self.assertEqual(len(matcher.single_words) >= 100, True)


class SearchFileTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "movie.eng.srt"

    def tearDown(self):
        self.tmp.cleanup()

    def test_clean_file_returns_false(self):
        self.path.write_text("1\n00:00:00,000 --> 00:00:01,000\nHello there.\n", encoding="utf-8")
        self.assertIs(matches().search_file(self.path), False)

    def test_dirty_file_returns_true(self):
        self.path.write_text("1\n00:00:00,000 --> 00:00:01,000\nYou asshole.\n", encoding="utf-8")
        self.assertIs(matches("asshole").search_file(self.path), True)

    def test_unreadable_returns_none(self):
        self.assertIs(matches().search_file(self.path), None)


class MaskingTests(unittest.TestCase):
    def test_masks_all_but_first_letter(self):
        self.assertEqual(mask_word("asshole"), "a******")
        self.assertEqual(mask_word("a"), "*")


class ParserTests(unittest.TestCase):
    def test_srt_loose(self):
        text = "1\n00:00:01,000 --> 00:00:02,500\nHello\nworld\n\n2\n00:10:00,000 --> 00:10:00,500\nDone\n"
        segments = _parse_srt_loose(text)
        self.assertEqual(len(segments), 2)
        self.assertEqual(segments[0].start, 1.0)
        self.assertEqual(segments[0].end, 2.5)
        self.assertEqual(segments[0].text, "Hello world")
        self.assertEqual(segments[1].index, 2)

    def test_srt_loose_comma_and_dot_ms(self):
        text = "1\n00:00:00,250 --> 00:00:01.750\nA\n"
        segments = _parse_srt_loose(text)
        self.assertEqual(segments[0].start, 0.25)
        self.assertEqual(segments[0].end, 1.75)

    def test_vtt_with_header_and_notes(self):
        text = (
            "WEBVTT\n\nNOTE this is a note\n\n"
            "00:00:01.000 --> 00:00:03.000 align:middle\nHi\n\n"
            "00:10:00 --> 00:10:02\nShort\n"
        )
        segments = _parse_vtt(text)
        self.assertEqual([s.text for s in segments], ["Hi", "Short"])
        self.assertEqual(segments[1].start, 600.0)

    def test_ass(self):
        text = (
            "Dialogue: 0,0:00:02.00,0:00:04.50,Default,,0,0,0,,You get an asshole line\n"
            "Comment: 0,0:00:09.00,0:00:10.00,Default,,0,0,0,,ignored\n"
        )
        segments = _parse_ass(text)
        self.assertEqual(len(segments), 1)
        self.assertEqual(segments[0].start, 2.0)
        self.assertEqual(segments[0].end, 4.5)
        self.assertEqual(segments[0].text, "You get an asshole line")

    def test_microdvd(self):
        text = "{100}{225}What an asshole|line break"
        segments = _parse_sub(text)
        self.assertEqual(segments[0].start, 4.0)  # 100 frames @ 25fps
        self.assertEqual(segments[0].end, 9.0)
        self.assertEqual(segments[0].text, "What an asshole line break")

    def test_parse_dispatch_by_suffix(self):
        with tempfile.TemporaryDirectory() as tmp:
            srt = Path(tmp) / "m.srt"
            vtt = Path(tmp) / "m.vtt"
            ass = Path(tmp) / "m.ass"
            srt.write_text("1\n00:00:00,000 --> 00:00:01,000\nText\n", encoding="utf-8")
            vtt.write_text("00:00:00.000 --> 00:00:01.000\nText\n", encoding="utf-8")
            ass.write_text("Dialogue: 0,0:00:00.00,0:00:01.00,Default,,0,0,0,,Text\n", encoding="utf-8")
            self.assertEqual(len(parse_timed_segments(srt)), 1)
            self.assertEqual(len(parse_timed_segments(vtt)), 1)
            self.assertEqual(len(parse_timed_segments(ass)), 1)

    def test_missing_file_returns_none(self):
        self.assertIsNone(parse_timed_segments("/definitely/not/here.srt"))


class SectionTests(unittest.TestCase):
    def setUp(self):
        self.matcher = matches("asshole")

    def test_find_bad_sections(self):
        segments = [
            TimedSegment(0.0, 1.0, "Hello there", 1),
            TimedSegment(2.0, 3.0, "Go away asshole", 2),
        ]
        sections = find_bad_sections(segments, self.matcher)
        self.assertEqual(len(sections), 1)
        self.assertEqual(sections[0]["words"], ["asshole"])
        self.assertEqual(sections[0]["start"], 2.0)

    def test_bad_sections_from_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "m.srt"
            path.write_text(
                "1\n00:00:00,000 --> 00:00:01,000\nClean\n\n"
                "2\n00:00:02,000 --> 00:00:03,000\nDirty asshole\n",
                encoding="utf-8",
            )
            sections = bad_sections_from_file(path, self.matcher)
            self.assertEqual(sections[0]["sub_index"], 2)


class WhisperMergeTests(unittest.TestCase):
    def test_word_mutes_are_merged(self):
        matcher = matches("asshole")
        sections = [{
            "sub_index": 1, "start": 1.0, "end": 2.0,
            "words": ["asshole"], "content": "you asshole",
        }]
        hits = [{"clip_number": 0, "start": 0.3, "end": 0.7, "word": "asshole", "model": "S"}]
        mutes, fallback = merge_whisper_and_subtitles(hits, sections, fallback_enabled=True)
        self.assertEqual(fallback, [])
        self.assertEqual(mutes[0]["start"], 0.3)
        self.assertEqual(mutes[0]["end"], 0.7)
        self.assertEqual(mutes[0]["word"], "asshole")
        self.assertFalse(mutes[0].get("fallback"))

    def test_unheard_word_falls_back_to_whole_section(self):
        matcher = matches("asshole")
        sections = [{
            "sub_index": 1, "start": 5.0, "end": 6.0,
            "words": ["asshole"], "content": "you asshole",
        }]
        mutes, fallback = merge_whisper_and_subtitles([], sections, fallback_enabled=True)
        self.assertEqual(fallback, [0])
        self.assertTrue(mutes[0]["fallback"])
        self.assertEqual(mutes[0]["start"], 5.0)

    def test_unheard_word_without_fallback_keeps_nothing(self):
        sections = [{
            "sub_index": 1, "start": 5.0, "end": 6.0,
            "words": ["asshole"], "content": "you asshole",
        }]
        mutes, fallback = merge_whisper_and_subtitles([], sections, fallback_enabled=False)
        self.assertEqual(mutes, [])
        self.assertEqual(fallback, [])

    def test_fuzzy_match_accepts_close_transcription(self):
        sections = [{
            "sub_index": 1, "start": 1.0, "end": 2.0,
            "words": ["asshole"], "content": "you asshole",
        }]
        hits = [{"clip_number": 0, "start": 0.2, "end": 0.5, "word": "ashole", "model": "M"}]
        mutes, fallback = merge_whisper_and_subtitles(hits, sections, fallback_enabled=True)
        self.assertEqual(fallback, [])
        self.assertEqual(mutes[0]["word"], "ashole")

    def test_absolute_times_are_relative_to_section_start(self):
        sections = [{
            "sub_index": 1, "start": 10.0, "end": 11.0,
            "words": ["asshole"], "content": "you asshole",
        }]
        mutes = [{
            "start": 0.5, "end": 0.9, "word": "asshole",
            "clip_number": 0, "model": "S",
        }]
        _fix_absolute_times(mutes, sections)
        self.assertEqual(mutes[0]["start"], 10.5)
        self.assertEqual(mutes[0]["end"], 10.9)


class SelectMuteTests(unittest.TestCase):
    def _mutes(self):
        return [
            {"word": "asshole", "start": 1.0, "end": 1.2, "fallback": False},
            {"word": ["asshole"], "start": 5.0, "end": 6.0, "fallback": True},
        ]

    def test_default_keeps_everything(self):
        self.assertEqual(len(select_mutes(self._mutes(), None)), 2)
        self.assertEqual(len(select_mutes(self._mutes(), "")), 2)

    def test_words_only(self):
        result = select_mutes(self._mutes(), "words")
        self.assertEqual(len(result), 1)
        self.assertFalse(result[0]["fallback"])

    def test_segments_only(self):
        result = select_mutes(self._mutes(), "segments")
        self.assertEqual(len(result), 1)
        self.assertTrue(result[0]["fallback"])


class TierTests(unittest.TestCase):
    def test_tiers_parsed(self):
        self.assertEqual(_bleeptool_tiers("S-M-FSM"), [("S", "S"), ("M", "M"), ("FSM", "subtitle")])
        self.assertEqual(_bleeptool_tiers(" M "), [("M", "M")])
        self.assertEqual(_bleeptool_tiers(""), [])

    def test_model_names(self):
        self.assertEqual(_tier_model("S", ("tiny.en", "base.en")), "tiny.en")
        self.assertEqual(_tier_model("M", ("tiny.en", "base.en")), "base.en")
        self.assertEqual(_tier_model("M", ()), "medium.en")
        self.assertEqual(_tier_model("S", ()), "small.en")


class FiltergraphTests(unittest.TestCase):
    def test_mute_pipeline(self):
        parts, out = _build_censored_filtergraph(
            [{"start": 1.0, "end": 1.5}], 100, 100, False, "0:a:0", "cens0"
        )
        self.assertEqual(
            parts,
            ["[0:a:0]anull[cens0_base]",
             "[cens0_base]volume=enable='between(t,0.9,1.6)':volume=0[cens0_m0]"],
        )
        self.assertEqual(out, "[cens0_m0]")

    def test_beep_pipeline_mixes_a_tone(self):
        parts, out = _build_censored_filtergraph(
            [{"start": 1.0, "end": 1.5}], 0, 0, True, "0:a:0", "cens0"
        )
        self.assertTrue(out == "[cens0]")
        self.assertTrue(any("aevalsrc=sin(2*PI*1000*t):d=0.5:s=44100" in p for p in parts))
        self.assertTrue(parts[-1].startswith("[cens0_m0][cens0_dbeep0]amix="))


class EngineCacheTests(unittest.TestCase):
    def test_model_cached_after_first_load(self):
        config = config_mod.Config()
        engine = CensorEngine(config)
        class _Fake:
            pass
        engine._models["small.en"] = _Fake()
        self.assertIsInstance(engine.model("small.en"), _Fake)


if __name__ == "__main__":
    unittest.main(verbosity=2)