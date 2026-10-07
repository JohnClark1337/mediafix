import sys
import types
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mediafix import config as config_mod
from mediafix import subtitles as sub_mod


class GuessingError(Exception):
    pass


class _Region:
    def configure(self, *args, **kwargs):
        return None


class _Entry:
    content = b"1\n00:00:01,000 --> 00:00:02,000\nhello\n\n"


class _Language:
    def __init__(self, value):
        self.value = value

    def __str__(self):
        return self.value

    def __repr__(self):
        return f"Language({self.value})"

    def __eq__(self, other):
        return isinstance(other, _Language) and other.value == self.value

    def __hash__(self):
        return hash(self.value)


def _install(subliminal_attrs):
    """Inject fake ``subliminal`` + ``babelfish`` modules so tests run without them."""
    module = types.ModuleType("subliminal")
    module.region = _Region()
    for key, value in subliminal_attrs.items():
        setattr(module, key, value)
    babelfish = types.ModuleType("babelfish")
    babelfish.Language = _Language
    return mock.patch.dict(
        sys.modules, {"subliminal": module, "babelfish": babelfish}
    )


class DownloadSubtitleTextTests(unittest.TestCase):
    def setUp(self):
        self.config = config_mod.Config()

    def test_guessing_error_returns_none(self):
        class BadVideo:
            @classmethod
            def fromname(cls, name):
                raise GuessingError(f"Insufficient data to process the guess for {name!r}")

        with _install({"Video": BadVideo, "download_best_subtitles": None}):
            self.assertIsNone(
                sub_mod.download_subtitle_text("/media/Movies/Apocalpyse Now.mkv", self.config)
            )

    def test_any_fromname_failure_returns_none(self):
        for exc_type in (ValueError, RuntimeError, GuessingError):

            class BadVideo:
                @classmethod
                def fromname(cls, name):
                    raise exc_type("boom")

            with _install({"Video": BadVideo, "download_best_subtitles": None}), self.subTest(exc=exc_type):
                self.assertIsNone(
                    sub_mod.download_subtitle_text("Unparseable Name.mkv", self.config)
                )

    def test_success_returns_decoded_content(self):
        captured = {}

        def download(videos, languages, providers=None, provider_configs=None):
            captured["names"] = [v.name for v in videos]
            captured["languages"] = languages
            captured["providers"] = providers
            captured["configs"] = provider_configs
            return {videos[0]: [_Entry()]}

        class Video:
            def __init__(self, name):
                self.name = name

            @classmethod
            def fromname(cls, name):
                return cls(name)

        with _install({"Video": Video, "download_best_subtitles": download}):
            text = sub_mod.download_subtitle_text("Movie With Unusual Name.mkv", self.config)

        self.assertEqual(text, "1\n00:00:01,000 --> 00:00:02,000\nhello\n\n")
        self.assertEqual(captured["names"], ["Movie With Unusual Name.mkv"])
        self.assertEqual(captured["languages"], {_Language("eng")})
        self.assertIsNone(captured["providers"])
        self.assertEqual(captured["configs"], {})

    def test_provider_failure_returns_none(self):
        class Video:
            def __init__(self, name):
                self.name = name

            @classmethod
            def fromname(cls, name):
                return cls(name)

        def download(videos, languages, providers=None, provider_configs=None):
            raise TimeoutError("provider outage")

        with _install({"Video": Video, "download_best_subtitles": download}):
            self.assertIsNone(
                sub_mod.download_subtitle_text("Still A Movie.mkv", self.config)
            )

    def test_disabled_search_returns_none(self):
        config = replace(self.config, subtitle_search=False)
        with _install({"Video": object, "download_best_subtitles": None}):
            self.assertIsNone(
                sub_mod.download_subtitle_text("Whatever.mkv", config)
            )


if __name__ == "__main__":
    unittest.main()