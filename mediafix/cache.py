"""Resolution of the Hugging Face model cache.

huggingface_hub computes its cache paths at import time and writes a token
file next to the model cache. A relative or unwritable HF_HOME therefore
surfaces as a bare ``PermissionError: './hf/token'`` with no hint about which
path failed, and the check cannot even run when the library is not installed
yet.

The resolution below mirrors huggingface_hub's own defaults, so the check
works with and without the library:

    HF_HOME     = $HF_HOME, else $XDG_CACHE_HOME/huggingface, else ~/.cache/huggingface
    HF_HUB_CACHE= $HF_HUB_CACHE, else $HF_HOME/hub
    token       = $HF_HOME/token
"""

import os
from pathlib import Path

DEFAULT_CACHE_DIR = ".cache/huggingface"


def _expand(path: str) -> str:
    return os.path.expanduser(path)


def resolve_hf_home() -> str:
    """The directory huggingface_hub would use as HF_HOME."""
    try:
        from huggingface_hub import constants

        return str(constants.HF_HOME)
    except ImportError:
        pass
    env = os.environ.get("HF_HOME")
    if env:
        return _expand(env)
    xdg = os.environ.get("XDG_CACHE_HOME")
    base = _expand(xdg) if xdg else str(Path.home() / ".cache")
    return os.path.join(base, "huggingface")


def resolve_hub_cache(hf_home: str | None = None) -> str:
    """The directory the model weights live in."""
    env = os.environ.get("HF_HUB_CACHE")
    if env:
        return _expand(env)
    try:
        from huggingface_hub import constants

        return str(constants.HF_HUB_CACHE)
    except ImportError:
        pass
    return os.path.join(hf_home or resolve_hf_home(), "hub")


def resolve_token_path(hf_home: str | None = None) -> str:
    """The file huggingface_hub writes its token to."""
    try:
        from huggingface_hub import constants

        return str(constants.HF_TOKEN_PATH)
    except ImportError:
        pass
    return os.path.join(hf_home or resolve_hf_home(), "token")


def is_absolute(path: str) -> bool:
    return os.path.isabs(path)
