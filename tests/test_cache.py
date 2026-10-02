import argparse
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mediafix import selftest as selftest_mod
from mediafix import subtitles as subtitles_mod

VENV_PY = Path(sys.executable)
REPO_ROOT = Path(__file__).resolve().parent.parent


def _run_isolated(env_overrides: dict, body: str) -> subprocess.CompletedProcess:
    """Run `body` in a fresh interpreter.

    huggingface_hub resolves HF_HOME at import time and caches it in
    constants, so in-process tests cannot change it afterwards.
    """
    env = {**os.environ, **env_overrides}
    env["PYTHONPATH"] = str(REPO_ROOT)
    return subprocess.run(
        [str(VENV_PY), "-c", body],
        capture_output=True, text=True, env=env, cwd=str(REPO_ROOT),
    )


class CacheCheckTests(unittest.TestCase):
    def test_relative_hf_home_is_rejected_with_guidance(self):
        body = (
            "import os\n"
            "os.environ['HF_HOME'] = './rel/cache'\n"
            "from mediafix import selftest\n"
            "try:\n"
            "    selftest.check_cache_writable()\n"
            "except selftest.CheckFailure as exc:\n"
            "    print('CAUGHT:', exc)\n"
            "else:\n"
            "    print('NOT CAUGHT')\n"
        )
        proc = _run_isolated({}, body)
        self.assertIn("CAUGHT:", proc.stdout, proc.stderr)
        self.assertIn("relative", proc.stdout)

    def test_writable_cache_passes(self):
        with tempfile.TemporaryDirectory() as td:
            target = Path(td) / "hf"
            body = (
                "import os\n"
                f"os.environ['HF_HOME'] = {str(target)!r}\n"
                "from mediafix import selftest\n"
                "print('\\n'.join(selftest.check_cache_writable()))\n"
            )
            proc = _run_isolated({}, body)
            self.assertIn("cache is writable", proc.stdout, proc.stderr)


class ModelCacheErrorTests(unittest.TestCase):
    """SubtitleEngine.load must fail early and clearly on a bad cache path.

    huggingface_hub writes HF_HOME/token; a relative HF_HOME surfaces as a bare
    'Permission denied' with no indication of which path failed.
    """

    def test_relative_hf_home_raises_model_cache_error(self):
        body = (
            "import os\n"
            "os.environ['HF_HOME'] = './home/mediafix/.cache/huggingface'\n"
            "from mediafix import subtitles\n"
            "try:\n"
            "    subtitles.SubtitleEngine._check_cache()\n"
            "except subtitles.ModelCacheError as exc:\n"
            "    print('CAUGHT:', exc)\n"
            "else:\n"
            "    print('NOT CAUGHT')\n"
        )
        proc = _run_isolated({}, body)
        self.assertIn("CAUGHT:", proc.stdout, proc.stderr)
        self.assertIn("not an absolute path", proc.stdout)

    def test_model_cache_error_is_a_runtime_error(self):
        self.assertTrue(issubclass(subtitles_mod.ModelCacheError, RuntimeError))

    def test_load_calls_the_cache_guard(self):
        """The guard must be wired into load(), not merely defined.

        Otherwise it is dead code and the original bare PermissionError returns.
        """
        import inspect

        source = inspect.getsource(subtitles_mod.SubtitleEngine.load)
        self.assertIn(
            "_check_cache()",
            source,
            "SubtitleEngine.load must call _check_cache() before importing the model",
        )

    def test_load_raises_before_touching_faster_whisper(self):
        """With a bad cache, load() must fail with ModelCacheError.

        faster_whisper is not importable in a bare environment, so if the guard
        ran first we get ModelCacheError rather than ImportError.
        """
        with tempfile.TemporaryDirectory() as td:
            body = (
                "import os\n"
                "os.environ['HF_HOME'] = './rel/cache'\n"
                "from mediafix import subtitles\n"
                "import argparse\n"
                "cfg = argparse.Namespace(model='small', device='cpu',\n"
                "    compute_type='int8', cpu_threads=2)\n"
                "try:\n"
                "    subtitles.SubtitleEngine(cfg).load()\n"
                "except subtitles.ModelCacheError as exc:\n"
                "    print('CAUGHT ModelCacheError')\n"
                "except ImportError as exc:\n"
                "    print('IMPORT ERROR:', exc)\n"
            )
            proc = _run_isolated({}, body)
            self.assertIn("CAUGHT ModelCacheError", proc.stdout, proc.stderr)

    def test_good_cache_passes_check(self):
        with tempfile.TemporaryDirectory() as td:
            target = Path(td) / "hf"
            body = (
                "import os\n"
                f"os.environ['HF_HOME'] = {str(target)!r}\n"
                "from mediafix import subtitles\n"
                "subtitles.SubtitleEngine._check_cache()\n"
                "print('OK')\n"
            )
            proc = _run_isolated({}, body)
            self.assertIn("OK", proc.stdout, proc.stderr)


class SelftestSectionTests(unittest.TestCase):
    def test_cache_section_runs_before_model(self):
        import inspect

        source = inspect.getsource(selftest_mod.run)
        self.assertIn('("cache"', source)
        self.assertLess(source.index('("cache"'), source.index('("model"'))

    def test_uid_helper_is_portable(self):
        self.assertIsInstance(selftest_mod._uid(), str)


if __name__ == "__main__":
    unittest.main()