"""Managed process registry for LUCID-launched Claude/Codex/Bash sessions."""
from __future__ import annotations

import os
import signal
import shlex
import sqlite3
import subprocess
import time
from pathlib import Path
from typing import Optional

from . import claude_state, live_state, runtime
from core.common.text_encoding import subprocess_text_kwargs


STATE_DIR = Path(os.environ.get("LUCID_STATE_DIR", "~/.lucid")).expanduser()
DB_PATH = STATE_DIR / "registry.sqlite"
_PANE_ACTIVITY: dict[str, dict] = {}


def _conn() -> sqlite3.Connection:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH), timeout=5)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS managed_process (
            id TEXT PRIMARY KEY,
            platform TEXT NOT NULL,
            pid INTEGER NOT NULL,
            cwd TEXT NOT NULL,
            tty TEXT,
            argv TEXT NOT NULL,
            tmux_session TEXT,
            started_at_ms INTEGER NOT NULL,
            updated_at_ms INTEGER NOT NULL,
            exited_at_ms INTEGER,
            exit_code INTEGER,
            session_id TEXT,
            transcript_path TEXT,
            session_file_path TEXT,
            status TEXT NOT NULL
        )
    """)
    columns = {row[1] for row in conn.execute("PRAGMA table_info(managed_process)")}
    if "session_file_path" not in columns:
        conn.execute("ALTER TABLE managed_process ADD COLUMN session_file_path TEXT")
    if "display_name" not in columns:
        conn.execute("ALTER TABLE managed_process ADD COLUMN display_name TEXT")
    if "completed" not in columns:
        conn.execute("ALTER TABLE managed_process ADD COLUMN completed INTEGER NOT NULL DEFAULT 0")
    return conn


def register_process(
    process_id: str,
    platform: str,
    pid: int,
    cwd: str,
    argv: str,
    tty: str = "",
    tmux_session: str = "",
    display_name: str = "",
) -> None:
    now = int(time.time() * 1000)
    with _conn() as conn:
        conn.execute("""
            INSERT OR REPLACE INTO managed_process
            (id, platform, pid, cwd, tty, argv, tmux_session, started_at_ms,
             updated_at_ms, exited_at_ms, exit_code, session_id, transcript_path, session_file_path, status, display_name)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, NULL, NULL, NULL, 'busy', ?)
        """, (process_id, platform, pid, cwd, tty, argv, tmux_session, now, now, display_name))


def update_binding(process_id: str, session_id: str, transcript_path: str, session_file_path: str = "") -> None:
    now = int(time.time() * 1000)
    with _conn() as conn:
        conn.execute("""
            UPDATE managed_process
            SET session_id = ?, transcript_path = ?, session_file_path = ?, updated_at_ms = ?
            WHERE id = ?
        """, (session_id, transcript_path, session_file_path, now, process_id))


def mark_exit(process_id: str, exit_code: int | None) -> None:
    now = int(time.time() * 1000)
    with _conn() as conn:
        conn.execute("""
            UPDATE managed_process
            SET exited_at_ms = ?, exit_code = ?, updated_at_ms = ?, status = 'idle'
            WHERE id = ?
        """, (now, exit_code, now, process_id))


def delete_process(process_id: str) -> None:
    with _conn() as conn:
        conn.execute("DELETE FROM managed_process WHERE id = ?", (process_id,))


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, PermissionError, OSError):
        return False
    return True


def _transcript_updated_at(transcript_path: str | None, fallback_ms: int) -> int:
    if not transcript_path:
        return fallback_ms
    try:
        st = Path(transcript_path).stat()
    except OSError:
        return fallback_ms
    return max(fallback_ms, int(st.st_mtime * 1000))


def _terminal_activity_state(tmux_session: str | None, platform: str, fallback_ms: int) -> tuple[dict, int]:
    if not tmux_session:
        return live_state.classify_capture_diff(None, None), fallback_ms
    now_ms = int(time.time() * 1000)
    previous = _PANE_ACTIVITY.get(tmux_session) or {}
    if previous and now_ms - int(previous.get("captured_at_ms") or 0) < live_state.CAPTURE_INTERVAL_MS:
        return dict(previous.get("state") or live_state.classify_capture_diff(None, None, captured_at_ms=now_ms)), int(
            previous.get("updated_at_ms") or fallback_ms
        )

    capture = live_state.tail_capture(live_state.capture_tmux_pane(tmux_session, lines=live_state.CAPTURE_TAIL_LINES))
    previous_capture = previous.get("current_capture")
    terminal_state = live_state.classify_capture_diff(capture, previous_capture, captured_at_ms=now_ms)
    if terminal_state.get("triage") == "working":
        updated_at_ms = now_ms
    else:
        updated_at_ms = int(previous.get("updated_at_ms") or fallback_ms)

    _PANE_ACTIVITY[tmux_session] = {
        "previous_capture": previous_capture,
        "current_capture": capture,
        "state": dict(terminal_state),
        "captured_at_ms": now_ms,
        "updated_at_ms": updated_at_ms,
    }
    return terminal_state, max(fallback_ms, updated_at_ms)


def _managed_triage(
    platform: str,
    alive: bool,
    exit_code: int | None,
    transcript_path: str | None,
    tmux_session: str | None,
    idle_seconds: int,
    terminal_state: dict | None = None,
) -> dict:
    if platform == "bash":
        return {
            "status": "bash",
            "waiting_for": None,
            "permission_msg": None,
            "permission_ts": None,
            "triage": "bash",
            "reason": "Bash",
            "suggestion": "",
            "activity_label": "Bash",
        }

    if not alive:
        return live_state.classify_capture_diff(None, None, captured_at_ms=int(time.time() * 1000))

    if platform in {"codex", "claude"}:
        return terminal_state if terminal_state is not None else live_state.classify_capture_diff(None, None)

    return live_state.classify_capture_diff(None, None, captured_at_ms=int(time.time() * 1000))


def managed_windows(node_id: str, node_name: str) -> list[dict]:
    with _conn() as conn:
        rows = conn.execute("""
            SELECT id, platform, pid, cwd, tty, argv, tmux_session, started_at_ms,
                   updated_at_ms, exited_at_ms, exit_code, session_id, transcript_path, session_file_path, status,
                   display_name, completed
            FROM managed_process
            ORDER BY updated_at_ms DESC
        """).fetchall()

    windows: list[dict] = []
    for row in rows:
        (proc_id, platform, pid, cwd, tty, argv, tmux_session, started_at,
         updated_at, exited_at, exit_code, session_id, transcript_path, session_file_path, status, display_name, completed) = row
        alive = _pid_alive(int(pid)) if not exited_at else False
        session_data = claude_state.load_session(session_file_path) if platform == "claude" else {}
        if platform == "claude" and session_data:
            session_id = session_data.get("sessionId") or session_id
            cwd = session_data.get("cwd") or cwd
            transcript_path = transcript_path or claude_state.transcript_path_for_session(session_data)
        effective_updated_at = _transcript_updated_at(transcript_path, int(updated_at))
        if platform == "bash":
            terminal_state, terminal_updated_at = {}, effective_updated_at
        else:
            terminal_state, terminal_updated_at = _terminal_activity_state(tmux_session, platform, effective_updated_at) if alive else ({}, effective_updated_at)
        effective_updated_at = max(effective_updated_at, terminal_updated_at)
        idle_seconds = max(0, int(time.time() - effective_updated_at / 1000))
        if completed:
            triage = {
                "status": "completed",
                "waiting_for": None,
                "permission_msg": None,
                "permission_ts": None,
                "triage": "completed",
                "reason": "Completed",
                "suggestion": "",
                "activity_label": "Completed",
            }
        else:
            triage = _managed_triage(platform, alive, exit_code, transcript_path, tmux_session, idle_seconds, terminal_state)
        actions = ["focus", "close", "rename", "resume", "fork", "terminal"]
        if platform == "claude":
            actions.append("review")
        if platform == "bash":
            actions = ["focus", "close", "rename", "terminal"]
        name = display_name or ("Bash" if platform == "bash" else f"{platform}-{proc_id[:8]}")
        windows.append({
            "node_id": node_id,
            "node_name": node_name,
            "platform": platform,
            "pid": int(pid),
            "session_id": session_id or proc_id,
            "cwd": cwd,
            "project_name": Path(cwd).name or cwd,
            "project_slug": cwd.replace("/", "-").replace("_", "-").replace(".", "-"),
            "name": name,
            "display_name": display_name or "",
            "status": triage["status"],
            "waiting_for": triage["waiting_for"],
            "started_at": int(started_at),
            "updated_at": effective_updated_at,
            "version": "",
            "tty": tty or None,
            "transcript_path": transcript_path,
            "alive": alive,
            "idle_seconds": idle_seconds,
            "permission_msg": triage["permission_msg"],
            "permission_ts": triage["permission_ts"],
            "current_task": "" if platform == "bash" else argv,
            "triage": triage["triage"],
            "triage_reason": triage["reason"],
            "triage_suggestion": triage["suggestion"],
            "activity_label": triage.get("activity_label") or "",
            "skills_used": [],
            "memory_ops": [],
            "background_tasks": [],
            "managed": True,
            "tmux_session": tmux_session,
            "window_key": f"{node_id}:{platform}:{pid}",
            "session_key": f"{node_id}:{platform}:{session_id or proc_id}",
            "node_health": "healthy",
            "actions": actions,
        })
    return windows


def find_managed_window(platform: str, pid: int) -> Optional[dict]:
    with _conn() as conn:
        row = conn.execute("""
            SELECT id, platform, pid, cwd, tty, argv, tmux_session, started_at_ms,
                   updated_at_ms, exited_at_ms, exit_code, session_id, transcript_path, session_file_path,
                   status, display_name, completed
            FROM managed_process
            WHERE platform = ? AND pid = ?
        """, (platform, pid)).fetchone()
    if not row:
        return None
    (proc_id, stored_platform, stored_pid, cwd, tty, argv, tmux_session, started_at,
     updated_at, exited_at, exit_code, session_id, transcript_path, session_file_path,
     status, display_name, completed) = row
    session_data = claude_state.load_session(session_file_path) if stored_platform == "claude" else {}
    if stored_platform == "claude" and session_data:
        session_id = session_data.get("sessionId") or session_id
        cwd = session_data.get("cwd") or cwd
        transcript_path = transcript_path or claude_state.transcript_path_for_session(session_data)
    alive = _pid_alive(int(stored_pid)) if not exited_at else False
    return {
        "id": proc_id,
        "platform": stored_platform,
        "pid": int(stored_pid),
        "cwd": cwd,
        "tty": tty or "",
        "argv": argv,
        "tmux_session": tmux_session or "",
        "started_at": int(started_at),
        "updated_at": int(updated_at),
        "exited_at": exited_at,
        "exit_code": exit_code,
        "session_id": session_id or proc_id,
        "transcript_path": transcript_path or "",
        "session_file_path": session_file_path or "",
        "status": status,
        "display_name": display_name or "",
        "completed": bool(completed),
        "name": display_name or ("Bash" if stored_platform == "bash" else f"{stored_platform}-{proc_id[:8]}"),
        "project_name": Path(cwd).name or cwd,
        "alive": alive,
    }


def close_managed(platform: str, pid: int) -> dict:
    with _conn() as conn:
        row = conn.execute(
            "SELECT id, pid, tmux_session FROM managed_process WHERE platform = ? AND pid = ?",
            (platform, pid),
        ).fetchone()
    if not row:
        return {"ok": False, "error": f"managed process not found platform={platform} pid={pid}"}
    proc_id, stored_pid, tmux_session = row
    if tmux_session:
        try:
            result = subprocess.run(
                [runtime.tmux_bin(), "kill-session", "-t", tmux_session],
                capture_output=True,
                timeout=5,
                **subprocess_text_kwargs(),
            )
        except FileNotFoundError:
            return {"ok": False, "error": "tmux is required to terminate managed terminal sessions"}
        except subprocess.TimeoutExpired:
            return {"ok": False, "error": f"tmux kill-session timed out for {tmux_session}"}
        stderr = result.stderr.strip()
        session_already_gone = "can't find session" in stderr or "no server running" in stderr
        if result.returncode != 0 and not session_already_gone:
            return {"ok": False, "error": stderr or f"tmux kill-session failed rc={result.returncode}"}
        delete_process(proc_id)
        return {"ok": True, "pid": pid, "platform": platform, "tmux_session": tmux_session, "deleted": True}
    try:
        os.kill(int(stored_pid), signal.SIGTERM)
    except ProcessLookupError:
        delete_process(proc_id)
        return {"ok": True, "message": "already dead", "deleted": True}
    except PermissionError as e:
        return {"ok": False, "error": str(e)}
    except OSError as e:
        return {"ok": False, "error": str(e)}
    delete_process(proc_id)
    return {"ok": True, "pid": pid, "platform": platform, "deleted": True}


def rename_managed(platform: str, pid: int, display_name: str) -> dict:
    clean_name = " ".join(str(display_name or "").split())[:80]
    with _conn() as conn:
        row = conn.execute(
            "SELECT id FROM managed_process WHERE platform = ? AND pid = ?",
            (platform, pid),
        ).fetchone()
        if not row:
            return {"ok": False, "error": f"managed process not found platform={platform} pid={pid}"}
        conn.execute(
            "UPDATE managed_process SET display_name = ? WHERE id = ?",
            (clean_name, row[0]),
        )
    return {"ok": True, "pid": pid, "platform": platform, "name": clean_name, "display_name": clean_name}


def set_completed(platform: str, pid: int) -> dict:
    with _conn() as conn:
        row = conn.execute(
            "SELECT id FROM managed_process WHERE platform = ? AND pid = ?",
            (platform, pid),
        ).fetchone()
        if not row:
            return {"ok": False, "error": f"managed process not found platform={platform} pid={pid}"}
        conn.execute(
            "UPDATE managed_process SET completed = 1 WHERE id = ?",
            (row[0],),
        )
    return {"ok": True, "pid": pid, "platform": platform, "completed": True}


def unset_completed(platform: str, pid: int) -> dict:
    with _conn() as conn:
        row = conn.execute(
            "SELECT id FROM managed_process WHERE platform = ? AND pid = ?",
            (platform, pid),
        ).fetchone()
        if not row:
            return {"ok": False, "error": f"managed process not found platform={platform} pid={pid}"}
        conn.execute(
            "UPDATE managed_process SET completed = 0 WHERE id = ?",
            (row[0],),
        )
    return {"ok": True, "pid": pid, "platform": platform, "completed": False}


def attach_command(platform: str, pid: int) -> Optional[str]:
    with _conn() as conn:
        row = conn.execute(
            "SELECT tmux_session FROM managed_process WHERE platform = ? AND pid = ?",
            (platform, pid),
        ).fetchone()
    if not row or not row[0]:
        return None
    return f"{shlex.quote(runtime.tmux_bin())} attach -t {shlex.quote(str(row[0]))}"


def tmux_session_for(platform: str, pid: int) -> Optional[str]:
    with _conn() as conn:
        row = conn.execute(
            "SELECT tmux_session FROM managed_process WHERE platform = ? AND pid = ?",
            (platform, pid),
        ).fetchone()
    if not row or not row[0]:
        return None
    return str(row[0])
