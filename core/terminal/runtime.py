"""Runtime paths for optional rootless local dependencies."""
from __future__ import annotations

import os
import subprocess
from pathlib import Path


def runtime_dir() -> Path:
    default_dir = Path(__file__).resolve().parents[1] / ".lucid-runtime"
    return Path(os.environ.get("LUCID_RUNTIME_DIR", str(default_dir))).expanduser()


def runtime_bin_dir() -> Path:
    return runtime_dir() / "env" / "bin"


def _tmux_works(path: str | Path) -> bool:
    try:
        result = subprocess.run(
            [str(path), "-V"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=2,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def _path_tmux() -> str:
    for raw_dir in os.environ.get("PATH", "").split(os.pathsep):
        if not raw_dir:
            continue
        candidate = Path(raw_dir) / "tmux"
        if candidate.exists() and os.access(candidate, os.X_OK) and _tmux_works(candidate):
            return str(candidate)
    return "tmux"


def tmux_bin() -> str:
    configured = os.environ.get("LUCID_TMUX", "")
    if configured:
        return configured
    candidate = runtime_bin_dir() / "tmux"
    if candidate.exists() and os.access(candidate, os.X_OK) and _tmux_works(candidate):
        return str(candidate)
    return _path_tmux()
