"""Claude session metadata helpers.

Status decisions are made from tmux capture diffs in ``live_state`` and
``registry``. Claude session JSON is only used to resolve binding metadata.
"""
from __future__ import annotations

import json
from pathlib import Path

from core.common.text_encoding import read_utf8
from .sessions import PROJECTS_DIR


def _cwd_to_project_slug(cwd: str) -> str:
    return cwd.replace("/", "-").replace("_", "-").replace(".", "-")


def load_session(session_file_path: str | None) -> dict:
    if not session_file_path:
        return {}
    p = Path(session_file_path)
    if not p.exists():
        return {}
    try:
        data = json.loads(read_utf8(p))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def transcript_path_for_session(session: dict) -> str:
    session_id = str(session.get("sessionId") or "")
    cwd = str(session.get("cwd") or "")
    explicit = str(session.get("transcriptPath") or session.get("transcript_path") or "")
    if explicit:
        return explicit
    if not session_id or not cwd:
        return ""
    return str(PROJECTS_DIR / _cwd_to_project_slug(cwd) / f"{session_id}.jsonl")
