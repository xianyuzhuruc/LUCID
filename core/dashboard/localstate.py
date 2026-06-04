"""Local machine state exposed to hub and agent modes."""
from __future__ import annotations

import os
import socket
import time
from typing import Optional

from core.common.text_encoding import read_utf8
from core.conversations import codex, history, search, transcripts
from core.knowledge import memory, skills
from core.terminal import patrol, sessions


def default_node_id() -> str:
    return os.environ.get("LUCID_NODE_ID") or socket.gethostname() or "local"


def _session_key(node_id: str, platform: str, session_id: str | None) -> str:
    return f"{node_id}:{platform}:{session_id or 'unknown'}"


def local_node_info(node_id: Optional[str] = None) -> dict:
    nid = node_id or default_node_id()
    return {
        "id": nid,
        "hostname": socket.gethostname(),
        "agent_version": "0.1.0",
        "time_ms": int(time.time() * 1000),
    }


def enriched_snapshot() -> dict:
    """Return current local managed windows.

    Unmanaged Claude Code live-window scanning is intentionally disabled. Claude
    history, search, and timelines still read ~/.claude transcripts elsewhere.
    """
    return {"windows": [], "counts": {"total": 0, "busy": 0, "waiting": 0, "idle": 0, "bash": 0, "completed": 0}, "ts": int(time.time() * 1000)}


def wire_snapshot(node_id: Optional[str] = None, node_name: Optional[str] = None) -> dict:
    """Return local snapshot in node-aware hub/agent wire format."""
    nid = node_id or default_node_id()
    snap = enriched_snapshot()
    try:
        from core.terminal.registry import managed_windows
        managed = managed_windows(nid, node_name or nid)
    except Exception:
        managed = []
    if managed:
        snap["windows"].extend(managed)
        snap["counts"]["total"] += len(managed)
        snap["counts"]["busy"] += sum(1 for w in managed if w.get("triage") == "working")
        snap["counts"]["waiting"] += sum(1 for w in managed if w.get("triage") == "waiting")
        snap["counts"]["idle"] += sum(1 for w in managed if w.get("triage") == "stalled")
        snap["counts"]["bash"] += sum(1 for w in managed if w.get("triage") == "bash")
        snap["counts"]["completed"] += sum(1 for w in managed if w.get("triage") == "completed")
        snap["windows"].sort(key=lambda w: (
            (w.get("name") or w.get("project_name") or "").lower(),
        ))

    snap["node"] = local_node_info(nid)
    snap["nodes"] = [{"id": nid, "name": node_name or nid, "health": "healthy", "window_count": len(snap["windows"])}]
    return snap


def history_sessions(q: str | None = None, page: int = 1, limit: int = 30, node_id: Optional[str] = None) -> dict:
    nid = node_id or default_node_id()
    data = history.list_sessions(q=q, page=page, limit=limit)
    for row in data.get("sessions", []):
        platform = row.get("platform", "claude")
        sid = row.get("session_id", "")
        row["node_id"] = nid
        row["node_name"] = nid
        row["session_key"] = _session_key(nid, platform, sid)
    data["node"] = local_node_info(nid)
    return data


def search_hits(q: str, limit: int = 60, node_id: Optional[str] = None) -> dict:
    nid = node_id or default_node_id()
    hits = search.search(q, limit=limit)
    for hit in hits:
        platform = hit.get("platform", "claude")
        sid = hit.get("session_id", "")
        hit["node_id"] = nid
        hit["node_name"] = nid
        hit["session_key"] = _session_key(nid, platform, sid)
    return {"hits": hits, "q": q, "node": local_node_info(nid)}


def skills_payload(node_id: Optional[str] = None) -> dict:
    nid = node_id or default_node_id()
    data = history.list_sessions(limit=9999)
    session_count: dict[str, int] = {}
    invoke_count: dict[str, int] = {}
    reads_count: dict[str, int] = {}
    writes_count: dict[str, int] = {}
    bash_refs_count: dict[str, int] = {}
    for s in data["sessions"]:
        for sk in s.get("skills_used", []):
            session_count[sk] = session_count.get(sk, 0) + 1
        bd = s.get("skill_breakdown") or {}
        for sk, cnt in (bd.get("per_skill_invokes") or {}).items():
            invoke_count[sk] = invoke_count.get(sk, 0) + cnt
        for sk, cnt in (bd.get("per_skill_reads") or {}).items():
            reads_count[sk] = reads_count.get(sk, 0) + cnt
        for sk, cnt in (bd.get("per_skill_writes") or {}).items():
            writes_count[sk] = writes_count.get(sk, 0) + cnt
        for sk, cnt in (bd.get("per_skill_bash_refs") or {}).items():
            bash_refs_count[sk] = bash_refs_count.get(sk, 0) + cnt

    all_skills = skills.list_all_skills()
    for s in all_skills:
        name = s["name"]
        inv = invoke_count.get(name, 0)
        rd = reads_count.get(name, 0)
        wr = writes_count.get(name, 0)
        brefs = bash_refs_count.get(name, 0)
        s["node_id"] = nid
        s["session_count"] = session_count.get(name, 0)
        s["invoke_count"] = inv
        s["reads"] = rd
        s["writes"] = wr
        s["bash_refs"] = brefs
        s["total_activity"] = inv + rd + wr + brefs
    all_skills.sort(key=lambda s: (-s["total_activity"], -s["invoke_count"], s["name"]))
    return {"skills": all_skills, "node": local_node_info(nid)}


def memory_payload(project: str | None = None, node_id: Optional[str] = None) -> dict:
    nid = node_id or default_node_id()
    data = history.list_sessions(limit=9999)
    read_count: dict[str, int] = {}
    write_count: dict[str, int] = {}
    for s in data["sessions"]:
        for m in s.get("memory_ops", []):
            name = m["name"]
            if m["operation"] == "read":
                read_count[name] = read_count.get(name, 0) + 1
            else:
                write_count[name] = write_count.get(name, 0) + 1
    result = memory.list_memories(project_slug=project)
    for group_mems in result.get("groups", {}).values():
        for m in group_mems:
            stem = m.get("file_stem", m["name"])
            m["node_id"] = nid
            m["read_sessions"] = read_count.get(stem, 0)
            m["write_sessions"] = write_count.get(stem, 0)
    result["node"] = local_node_info(nid)
    return result


def memory_detail(name: str) -> dict:
    for proj_dir in sessions.PROJECTS_DIR.iterdir() if sessions.PROJECTS_DIR.exists() else []:
        mem_dir = proj_dir / "memory"
        if not mem_dir.is_dir():
            continue
        f = mem_dir / f"{name}.md"
        if not f.exists():
            continue
        text = read_utf8(f)
        fm = memory._parse_frontmatter(text) if hasattr(memory, "_parse_frontmatter") else {}
        body_start = text.find("\n---", 3)
        body = text[body_start + 4:].strip() if body_start > 0 else text
        return {
            "name": fm.get("name", name),
            "description": fm.get("description", ""),
            "type": fm.get("type", "unknown"),
            "content": body,
            "path": str(f),
        }
    raise FileNotFoundError(name)


def timeline_for_session(platform: str, session_id: str, limit: int = 2000) -> dict:
    if platform == "codex":
        from core.conversations.codex import CODEX_SESSIONS_DIR
        if CODEX_SESSIONS_DIR.exists():
            for f in CODEX_SESSIONS_DIR.rglob("*.jsonl"):
                if session_id in f.stem:
                    return {
                        "session_id": session_id,
                        "project_slug": "codex",
                        "events": codex.codex_timeline(str(f), limit=limit),
                        "platform": "codex",
                    }
    for proj_dir in sessions.PROJECTS_DIR.iterdir() if sessions.PROJECTS_DIR.exists() else []:
        if not proj_dir.is_dir():
            continue
        f = proj_dir / f"{session_id}.jsonl"
        if f.exists():
            fp = str(f)
            return {
                "session_id": session_id,
                "project_slug": proj_dir.name,
                "events": transcripts.timeline(fp, limit=limit),
                "platform": "claude",
                "skills_used": transcripts.extract_skills_used(fp),
                "memory_ops": transcripts.extract_memory_ops(fp),
                "plan_history": transcripts.extract_plan_history(fp),
            }
    raise FileNotFoundError(session_id)
