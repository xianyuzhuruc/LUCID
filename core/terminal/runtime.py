"""Runtime paths for optional rootless local dependencies."""
from __future__ import annotations

import os
from pathlib import Path


def runtime_dir() -> Path:
    default_dir = Path(__file__).resolve().parents[1] / ".lucid-runtime"
    return Path(os.environ.get("LUCID_RUNTIME_DIR", str(default_dir))).expanduser()


def runtime_bin_dir() -> Path:
    return runtime_dir() / "env" / "bin"


def tmux_bin() -> str:
    configured = os.environ.get("LUCID_TMUX", "")
    if configured:
        return configured
    candidate = runtime_bin_dir() / "tmux"
    if candidate.exists() and os.access(candidate, os.X_OK):
        return str(candidate)
    return "tmux"
