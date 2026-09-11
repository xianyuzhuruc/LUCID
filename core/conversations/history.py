"""Index all past sessions from history.jsonl + projects/**/*.jsonl + codex."""
from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
import time
from dataclasses import dataclass, asdict, field
from pathlib import Path
from typing import Optional

from core.common.text_encoding import open_utf8, read_utf8, subprocess_text_kwargs
from core.terminal.sessions import CLAUDE_HOME, HOME_BASE, PROJECTS_DIR

HISTORY_JSONL = CLAUDE_HOME / "history.jsonl"
CLAUDE_SESSIONS_DIR = CLAUDE_HOME / "sessions"
_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,255}$")


@dataclass
class HistorySession:
    session_id: str
    project: str
    project_name: str
    first_input: str
    input_count: int
    first_ts: str
    last_ts: str
    transcript_path: Optional[str]
    transcript_size: int
    transcript_mtime: int
    is_alive: bool
    platform: str = "claude"
    model: str = ""
    skills_used: list = field(default_factory=list)
    memory_ops: list = field(default_factory=list)
    skill_breakdown: dict = field(default_factory=dict)
    memory_breakdown: dict = field(default_factory=dict)


_cache: list[HistorySession] = []
_cache_ts: float = 0
_CACHE_TTL = 30


def invalidate_cache() -> None:
    global _cache, _cache_ts
    _cache = []
    _cache_ts = 0


def _validate_session_id(session_id: str) -> str:
    sid = str(session_id or "")
    if sid in {".", ".."} or not _SESSION_ID_RE.fullmatch(sid):
        raise ValueError("invalid session id")
    return sid


def _remove_claude_history_entries(session_id: str) -> int:
    if not HISTORY_JSONL.exists():
        return 0
    raw_lines = HISTORY_JSONL.read_bytes().splitlines(keepends=True)
    kept: list[bytes] = []
    removed = 0
    for raw_line in raw_lines:
        try:
            entry = json.loads(raw_line.decode("utf-8"))
        except Exception:
            kept.append(raw_line)
            continue
        if entry.get("sessionId") == session_id:
            removed += 1
        else:
            kept.append(raw_line)
    if not removed:
        return 0

    HISTORY_JSONL.parent.mkdir(parents=True, exist_ok=True)
    original_mode = HISTORY_JSONL.stat().st_mode & 0o777
    fd, temp_name = tempfile.mkstemp(prefix=".history-delete-", dir=str(HISTORY_JSONL.parent))
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "wb") as f:
            f.writelines(kept)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(temp_path, original_mode)
        os.replace(temp_path, HISTORY_JSONL)
    finally:
        if temp_path.exists():
            temp_path.unlink()
    return removed


def _delete_claude_session(session_id: str) -> tuple[int, int]:
    transcript_paths: list[Path] = []
    if PROJECTS_DIR.exists():
        for project_dir in PROJECTS_DIR.iterdir():
            if project_dir.is_dir():
                transcript = project_dir / f"{session_id}.jsonl"
                if transcript.is_file() or transcript.is_symlink():
                    transcript_paths.append(transcript)

    metadata_paths: list[Path] = []
    if CLAUDE_SESSIONS_DIR.exists():
        for metadata in CLAUDE_SESSIONS_DIR.glob("*.json"):
            try:
                payload = json.loads(read_utf8(metadata))
            except Exception:
                continue
            if payload.get("sessionId") == session_id:
                metadata_paths.append(metadata)

    removed_history_entries = _remove_claude_history_entries(session_id)
    if not transcript_paths and not metadata_paths and not removed_history_entries:
        raise FileNotFoundError(session_id)

    deleted_files = 0
    for path in transcript_paths + metadata_paths:
        path.unlink()
        deleted_files += 1
    return deleted_files, removed_history_entries


def _delete_codex_session(session_id: str) -> tuple[int, int]:
    from . import codex

    matches: list[Path] = []
    if codex.CODEX_SESSIONS_DIR.exists():
        for rollout in codex.CODEX_SESSIONS_DIR.rglob("*.jsonl"):
            metadata = codex._parse_session_meta(rollout)
            if metadata and metadata.get("id") == session_id:
                matches.append(rollout)
    if not matches:
        raise FileNotFoundError(session_id)
    for rollout in matches:
        rollout.unlink()
    return len(matches), 0


def delete_session(platform: str, session_id: str) -> dict:
    """Physically remove one inactive session's persisted artifacts."""
    normalized_platform = str(platform or "").strip().lower()
    sid = _validate_session_id(session_id)
    if normalized_platform == "claude":
        deleted_files, removed_history_entries = _delete_claude_session(sid)
    elif normalized_platform == "codex":
        deleted_files, removed_history_entries = _delete_codex_session(sid)
    else:
        raise ValueError(f"deleting {normalized_platform or 'unknown'} sessions is not supported")
    invalidate_cache()
    return {
        "ok": True,
        "action": "deleted",
        "platform": normalized_platform,
        "session_id": sid,
        "deleted_files": deleted_files,
        "removed_history_entries": removed_history_entries,
    }


def _load_history_jsonl() -> dict[str, dict]:
    out: dict[str, dict] = {}
    if not HISTORY_JSONL.exists():
        return out
    try:
        with open_utf8(HISTORY_JSONL) as f:
            for line in f:
                try:
                    d = json.loads(line)
                except Exception:
                    continue
                sid = d.get("sessionId", "")
                if not sid:
                    continue
                display = d.get("display", "")
                ts = d.get("timestamp", "")
                project = d.get("project", "")
                if sid not in out:
                    out[sid] = {
                        "first_input": display[:300],
                        "first_ts": ts,
                        "last_ts": ts,
                        "project": project,
                        "count": 0,
                    }
                out[sid]["count"] += 1
                out[sid]["last_ts"] = ts
    except Exception:
        pass
    return out


def _scan_transcripts() -> dict[str, dict]:
    out: dict[str, dict] = {}
    if not PROJECTS_DIR.exists():
        return out
    for proj_dir in PROJECTS_DIR.iterdir():
        if not proj_dir.is_dir():
            continue
        for f in proj_dir.glob("*.jsonl"):
            if f.name.endswith(".wakatime"):
                continue
            if "subagents" in f.parts:
                continue
            sid = f.stem
            try:
                st = f.stat()
            except Exception:
                continue
            out[sid] = {
                "path": str(f),
                "size": st.st_size,
                "mtime": int(st.st_mtime * 1000),
                "project_slug": proj_dir.name,
            }
    return out


def _find_alive_pids() -> set[str]:
    sessions_dir = CLAUDE_SESSIONS_DIR
    alive: set[str] = set()
    if not sessions_dir.exists():
        return alive
    for f in sessions_dir.glob("*.json"):
        if f.name.startswith("session-"):
            continue
        try:
            d = json.loads(read_utf8(f))
            pid = d.get("pid")
            sid = d.get("sessionId", "")
            if pid and sid:
                try:
                    os.kill(int(pid), 0)
                    alive.add(sid)
                except (ProcessLookupError, PermissionError, OSError):
                    pass
        except Exception:
            pass
    return alive


def _extract_skills_from_transcript(path: Path) -> list[str]:
    """Extract unique skill names invoked via Skill tool_use."""
    skills: list[str] = []
    seen: set[str] = set()
    try:
        with open_utf8(path) as f:
            for line in f:
                try:
                    d = json.loads(line)
                except Exception:
                    continue
                if d.get("type") != "assistant":
                    continue
                content = (d.get("message") or {}).get("content", [])
                if not isinstance(content, list):
                    continue
                for c in content:
                    if isinstance(c, dict) and c.get("name") == "Skill":
                        skill_name = (c.get("input") or {}).get("skill", "")
                        if skill_name and skill_name not in seen:
                            seen.add(skill_name)
                            skills.append(skill_name)
    except Exception:
        pass
    return skills


def _build_index() -> list[HistorySession]:
    hist = _load_history_jsonl()
    transcripts = _scan_transcripts()
    alive = _find_alive_pids()

    all_sids = set(hist.keys()) | set(transcripts.keys())
    sessions: list[HistorySession] = []

    for sid in all_sids:
        h = hist.get(sid, {})
        t = transcripts.get(sid, {})

        project = h.get("project", "")
        project_name = project.rsplit("/", 1)[-1] if project else (
            t.get("project_slug", "").replace("-", "/").split("/")[-1] or "unknown"
        )

        first_input = h.get("first_input", "")
        if not first_input and t.get("path"):
            first_input = _extract_first_user_text(Path(t["path"]))

        skills = []
        mem_ops = []
        model = ""
        skill_breakdown = {}
        memory_breakdown = {}
        tp = t.get("path")
        if tp:
            skills = _extract_skills_from_transcript(Path(tp))
            from .transcripts import extract_memory_ops, count_skill_activity, count_memory_activity
            mem_ops = extract_memory_ops(tp)
            model = _extract_model(Path(tp))
            sa = count_skill_activity(tp)
            skill_breakdown = {
                "per_skill_invokes": sa.get("per_skill_invokes", {}),
                "per_skill_reads": sa.get("per_skill_reads", {}),
                "per_skill_writes": sa.get("per_skill_writes", {}),
                "per_skill_bash_refs": sa.get("per_skill_bash_refs", {}),
            }
            memory_breakdown = count_memory_activity(tp)

        sessions.append(HistorySession(
            session_id=sid,
            project=project,
            project_name=project_name,
            first_input=first_input,
            input_count=h.get("count", 0),
            first_ts=h.get("first_ts", ""),
            last_ts=h.get("last_ts", ""),
            transcript_path=tp,
            transcript_size=t.get("size", 0),
            transcript_mtime=t.get("mtime", 0),
            is_alive=sid in alive,
            platform="claude",
            model=model,
            skills_used=skills,
            memory_ops=mem_ops,
            skill_breakdown=skill_breakdown,
            memory_breakdown=memory_breakdown,
        ))

    # Merge Codex sessions
    try:
        from .codex import list_codex_sessions
        for cs in list_codex_sessions():
            d = cs.to_history_dict()
            sessions.append(HistorySession(**d))
    except Exception:
        pass

    sessions.sort(key=lambda s: s.transcript_mtime or 0, reverse=True)
    return sessions


def _extract_first_user_text(path: Path) -> str:
    try:
        with open_utf8(path) as f:
            for line in f:
                try:
                    d = json.loads(line)
                except Exception:
                    continue
                if d.get("type") != "user":
                    continue
                msg = d.get("message", {})
                content = msg.get("content", [])
                if isinstance(content, str):
                    return content[:300]
                if isinstance(content, list):
                    for c in content:
                        if isinstance(c, dict) and c.get("type") == "text":
                            return (c.get("text") or "")[:300]
    except Exception:
        pass
    return ""


def _extract_model(path: Path) -> str:
    try:
        with open_utf8(path) as f:
            for line in f:
                try:
                    d = json.loads(line)
                except Exception:
                    continue
                if d.get("type") != "assistant":
                    continue
                return (d.get("message") or {}).get("model", "")
    except Exception:
        pass
    return ""


def _rg_search_sessions(query: str) -> dict[str, list[str]]:
    """Use ripgrep to find session IDs + match snippets.

    Returns {session_id: [snippet1, snippet2, ...]}.
    """
    import re as _re
    search_dirs: list[str] = []
    if PROJECTS_DIR.exists():
        search_dirs.append(str(PROJECTS_DIR))
    codex_dir = HOME_BASE / ".codex" / "sessions"
    if codex_dir.exists():
        search_dirs.append(str(codex_dir))
    if not search_dirs:
        return {}
    cmd = [
        "rg", "-i", "-S",
        "--max-count", "3",
        "-g", "*.jsonl",
        "-g", "!*.wakatime",
        "-g", "!*subagents*",
        "--no-heading",
        query,
    ] + search_dirs
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=5, **subprocess_text_kwargs())
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return {}
    result: dict[str, list[str]] = {}
    ql = query.lower()
    for raw_line in proc.stdout.splitlines():
        # format: /path/to/sid.jsonl:jsonl_content
        colon = raw_line.find(".jsonl:")
        if colon < 0:
            continue
        fpath = raw_line[:colon + 6]
        content = raw_line[colon + 7:]
        sid = Path(fpath).stem
        # Extract a human-readable snippet around the match
        idx = content.lower().find(ql)
        if idx < 0:
            continue
        start = max(0, idx - 60)
        end = min(len(content), idx + len(query) + 60)
        snippet = content[start:end].replace("\n", " ").replace("\\n", " ").strip()
        if start > 0:
            snippet = "…" + snippet
        if end < len(content):
            snippet = snippet + "…"
        if sid not in result:
            result[sid] = []
        if len(result[sid]) < 3:
            result[sid].append(snippet)
    return result


def list_sessions(
    q: Optional[str] = None,
    page: int = 1,
    limit: int = 30,
    include_alive: bool = True,
    platform: Optional[str] = None,
) -> dict:
    global _cache, _cache_ts
    now = time.time()
    if now - _cache_ts > _CACHE_TTL or not _cache:
        _cache = _build_index()
        _cache_ts = now

    filtered = _cache
    if not include_alive:
        filtered = [s for s in filtered if not s.is_alive]
    if platform:
        filtered = [s for s in filtered if s.platform == platform]
    rg_matches: dict[str, list[str]] = {}
    if q:
        ql = q.lower()
        meta_sids = {
            s.session_id for s in filtered
            if ql in s.first_input.lower()
            or ql in s.project_name.lower()
            or ql in s.session_id.lower()
            or ql in s.project.lower()
            or ql in (s.transcript_path or "").lower()
        }
        rg_matches = _rg_search_sessions(q)
        all_sids = meta_sids | set(rg_matches.keys())
        filtered = [s for s in filtered if s.session_id in all_sids]

    total = len(filtered)
    start = (page - 1) * limit
    page_items = filtered[start : start + limit]

    sessions_out = []
    for s in page_items:
        d = asdict(s)
        d["match_snippets"] = rg_matches.get(s.session_id, [])
        sessions_out.append(d)
    return {
        "total": total,
        "page": page,
        "limit": limit,
        "sessions": sessions_out,
    }
