"""Unified tmux capture-diff state inference for managed agent terminals."""
from __future__ import annotations

import re
import subprocess
import time
from typing import Optional

from . import runtime
from core.common.text_encoding import subprocess_text_kwargs


CAPTURE_TAIL_LINES = 30
CAPTURE_INTERVAL_MS = 3000

_ANSI_RE = re.compile(
    r"\x1b\[[0-?]*[ -/]*[@-~]"
    r"|\x1b\][^\x07]*(?:\x07|\x1b\\)"
    r"|\x1b[@-_]"
)
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
_ERROR_RE = re.compile(r"\berror\b", re.IGNORECASE)
_YES_RE = re.compile(r"\byes\b", re.IGNORECASE)
_NO_RE = re.compile(r"\bno\b", re.IGNORECASE)


def capture_tmux_pane(session_name: str, lines: int = CAPTURE_TAIL_LINES, timeout: float = 2.0) -> Optional[str]:
    """Capture the last ``lines`` of a tmux pane as plain text."""
    if not session_name:
        return None
    safe_lines = max(1, min(lines, 200))
    try:
        proc = subprocess.run(
            [runtime.tmux_bin(), "capture-pane", "-p", "-t", session_name, "-S", f"-{safe_lines}"],
            capture_output=True,
            timeout=timeout,
            **subprocess_text_kwargs(),
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout


def tail_capture(text: str | None, lines: int = CAPTURE_TAIL_LINES) -> str | None:
    """Return a stable normalized representation of the captured tail."""
    if text is None:
        return None
    clean = _CONTROL_RE.sub("", _ANSI_RE.sub("", text.replace("\r\n", "\n").replace("\r", "\n")))
    return "\n".join(clean.split("\n")[-lines:])


def classify_capture_diff(current_capture: str | None, previous_capture: str | None, *, captured_at_ms: int | None = None) -> dict:
    """Classify Codex/Claude state using only current vs previous tmux tails.

    Rules:
    - no current capture means Waiting, because the managed terminal cannot be inspected;
    - first successful capture or any changed tail means Working;
    - unchanged tail with ``error`` or both ``yes`` and ``no`` means Waiting;
    - unchanged tail without those markers means Stalled.
    """
    now_ms = int(captured_at_ms if captured_at_ms is not None else time.time() * 1000)
    if current_capture is None:
        return _state_payload("waiting", now_ms)
    if previous_capture is None or current_capture != previous_capture:
        return _state_payload("working", now_ms)
    if _waiting_marker(current_capture):
        return _state_payload("waiting", now_ms)
    return _state_payload("stalled", now_ms)


def _waiting_marker(capture: str) -> bool:
    return bool(_ERROR_RE.search(capture) or (_YES_RE.search(capture) and _NO_RE.search(capture)))


def _state_payload(triage: str, captured_at_ms: int) -> dict:
    label = {"working": "Working", "stalled": "Stalled", "waiting": "Waiting"}[triage]
    return {
        "status": triage,
        "waiting_for": label if triage == "waiting" else None,
        "permission_msg": label if triage == "waiting" else None,
        "permission_ts": str(captured_at_ms) if triage == "waiting" else None,
        "triage": triage,
        "reason": label,
        "suggestion": "",
        "activity_label": label,
        "captured_at_ms": captured_at_ms,
    }
