import argparse
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mediafix import cli as cli_mod


def parse_only(argv):
    """Run cli.main() far enough to get the argparse namespace, nothing further."""
    real = argparse.ArgumentParser.parse_args
    captured = {}

    def spy(self, args=None, namespace=None):
        captured["ns"] = real(self, args, namespace)
        raise SystemExit(0)

    argparse.ArgumentParser.parse_args = spy
    try:
        try:
            cli_mod.main(argv)
        except SystemExit:
            pass
    finally:
        argparse.ArgumentParser.parse_args = real
    return captured.get("ns")


class PathResolutionTests(unittest.TestCase):
    def test_every_subcommand_defines_paths(self):
        # Regression: `tui` never got the paths positional, so the default
        # command died with AttributeError on args.paths.
        for argv in (["tui"], ["tui", "."], ["scan"], ["scan", "."], ["apply"], ["apply", "."]):
            with self.subTest(argv=argv):
                ns = parse_only(argv)
                self.assertIsNotNone(ns)
                self.assertIsInstance(getattr(ns, "paths", None), list)

    def test_no_subcommand_defines_paths(self):
        # A bare invocation defaults to tui but parses against the top-level
        # parser, which also needs to tolerate a missing paths attribute.
        ns = parse_only([])
        self.assertIsNotNone(ns)
        self.assertIsNone(getattr(ns, "command", None))

    def test_resolve_paths_uses_explicit_arg(self):
        ns = parse_only(["tui", "."])
        self.assertEqual(cli_mod._resolve_paths(ns), ["."])

    def test_resolve_paths_falls_back_to_media_root(self):
        ns = parse_only(["tui"])
        ns.paths = []
        with mock.patch.dict(os.environ, {"MEDIA_ROOT": str(Path.cwd())}):
            self.assertEqual(cli_mod._resolve_paths(ns), [str(Path.cwd())])

    def test_resolve_paths_accepts_namespace_without_paths(self):
        # A namespace that simply has no paths attribute at all.
        ns = argparse.Namespace()
        with mock.patch.dict(os.environ, {"MEDIA_ROOT": str(Path.cwd())}):
            self.assertEqual(cli_mod._resolve_paths(ns), [str(Path.cwd())])

    def test_resolve_paths_rejects_missing_paths(self):
        ns = parse_only(["tui"])
        ns.paths = ["/definitely/not/here"]
        with self.assertRaises(SystemExit):
            cli_mod._resolve_paths(ns)


class CensorArgTests(unittest.TestCase):
    def test_only_accepts_censor(self):
        ns = parse_only(["scan", "--only", "censor", "."])
        self.assertEqual(ns.only, "censor")

    def test_censor_options_parse(self):
        ns = parse_only([
            "apply", "-y", "--only", "censor", ".",
            "--swears", "my.txt", "--bleeptool", "M", "--beep",
            "--beep-mode", "words", "--pre-buffer", "50", "--post-buffer", "200",
            "--boost-db", "3", "--censor-models", "tiny.en,base.en",
            "--no-subtitle-search", "--subliminal-providers", "opensubtitles.com,podnapisi",
        ])
        self.assertEqual(ns.swears_path, "my.txt")
        self.assertEqual(ns.bleeptool, "M")
        self.assertTrue(ns.beep)
        self.assertEqual(ns.beep_mode, "words")
        self.assertEqual(ns.pre_buffer_ms, 50)
        self.assertEqual(ns.post_buffer_ms, 200)
        self.assertEqual(ns.boost_db, 3)
        self.assertEqual(ns.censor_models, "tiny.en,base.en")
        self.assertFalse(ns.subtitle_search)
        self.assertEqual(ns.subliminal_providers, ["opensubtitles.com", "podnapisi"])

    def test_check_defines_skip_censor(self):
        ns = parse_only(["check", "--skip-censor"])
        self.assertTrue(ns.skip_censor)

    def test_tui_exposes_censor_options(self):
        ns = parse_only(["tui", "--swears", "s.txt"])
        self.assertEqual(ns.swears_path, "s.txt")


if __name__ == "__main__":
    unittest.main()