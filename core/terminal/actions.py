"""Side-effectful actions: focus, fork, close, review."""
from __future__ import annotations

import os
import signal
import shlex
import subprocess
from pathlib import Path
from typing import Optional

from . import registry
from .sessions import CLAUDE_HOME
from core.common.text_encoding import subprocess_text_kwargs

# Focus shim resolution: a user override at ~/.claude/focus-tty.sh wins; otherwise
# the bundled cross-setup default (Terminal.app / iTerm2 / tmux) shipped with the repo.
_USER_FOCUS_SCRIPT = CLAUDE_HOME / "focus-tty.sh"
_BUNDLED_FOCUS_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "focus-tty.sh"


def _resolve_focus_script() -> Optional[Path]:
    if _USER_FOCUS_SCRIPT.exists():
        return _USER_FOCUS_SCRIPT
    if _BUNDLED_FOCUS_SCRIPT.exists():
        return _BUNDLED_FOCUS_SCRIPT
    return None


def focus_terminal(tty: str) -> dict:
    """Activate the terminal tab that owns `tty`.

    Prefers a user override at ~/.claude/focus-tty.sh; falls back to the bundled
    scripts/focus-tty.sh, which handles plain Terminal.app / iTerm2 tabs and tmux
    panes on macOS out of the box.
    """
    if not tty:
        return {"ok": False, "error": "no tty"}
    script = _resolve_focus_script()
    if script is None:
        return {
            "ok": False,
            "error": f"no focus-tty.sh found (looked at {_USER_FOCUS_SCRIPT} and {_BUNDLED_FOCUS_SCRIPT})",
        }
    # Direct exec respects the script's own shebang (matches the original behavior
    # and any user override). If the +x bit was lost on an odd checkout, retry via
    # bash (covers bash/POSIX scripts; a non-bash override should keep its +x).
    # The whole thing is shielded so focus_terminal NEVER raises — a TimeoutExpired
    # (e.g. a blocking macOS Automation prompt) or a missing `bash` must return the
    # structured error, not bubble up as a 500 in the request handler.
    try:
        try:
            proc = subprocess.run(
                [str(script), tty],
                capture_output=True, timeout=10, **subprocess_text_kwargs(),
            )
        except PermissionError:
            proc = subprocess.run(
                ["bash", str(script), tty],
                capture_output=True, timeout=10, **subprocess_text_kwargs(),
            )
    except subprocess.TimeoutExpired:
        # stable contract: the child (e.g. a blocking Automation prompt) was killed
        return {"ok": False, "error": "focus timed out after 10s", "code": None}
    except Exception as e:
        return {"ok": False, "error": str(e)}
    # `code` lets the UI distinguish the script's exit codes (3 detached / 4 no-tab
    # / 5 permission-denied / 6 unsupported) instead of a generic failure.
    return {
        "ok": proc.returncode == 0,
        "code": proc.returncode,
        "stdout": proc.stdout,
        "stderr": proc.stderr,
    }


_FORK_APPLESCRIPT_ITERM = '''
tell application "iTerm2"
    activate
    set newWin to (create window with default profile)
    tell current session of newWin
        write text {cmd}
    end tell
end tell
'''


def _claude_window(pid: int) -> Optional[dict]:
    return registry.find_managed_window("claude", pid)


def fork_session(pid: int) -> dict:
    """Open a new iTerm2 window and fork the session (new ID, inherits history)."""
    w = _claude_window(pid)
    if not w:
        return {"ok": False, "error": f"no window pid={pid}"}

    session_id = w.get("session_id") or ""
    cwd = w.get("cwd") or str(Path.home())
    inner = f"cd {shlex.quote(cwd)} && claude --resume {shlex.quote(session_id)} --fork-session"
    quoted_for_applescript = '"' + inner.replace('\\', '\\\\').replace('"', '\\"') + '"'
    script = _FORK_APPLESCRIPT_ITERM.format(cmd=quoted_for_applescript)

    try:
        proc = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True, timeout=10, **subprocess_text_kwargs(),
        )
    except Exception as e:
        return {"ok": False, "error": str(e)}
    return {
        "ok": proc.returncode == 0,
        "cwd": cwd,
        "session_id": session_id,
        "stdout": proc.stdout,
        "stderr": proc.stderr,
    }


_review_results: dict[int, dict] = {}


def _build_review_summary(transcript_path: str, limit: int = 40) -> str:
    """Extract last N turns as compact text for review prompt."""
    from core.conversations.transcripts import timeline
    events = timeline(transcript_path, limit=limit)
    lines: list[str] = []
    for ev in events:
        kind = ev.get("kind", "")
        ts = (ev.get("ts") or "")[:19]
        if kind == "user_text":
            lines.append(f"[USER {ts}] {ev.get('text','')[:500]}")
        elif kind == "assistant_text":
            lines.append(f"[ASSISTANT {ts}] {ev.get('text','')[:500]}")
        elif kind == "tool_use":
            extra = ", ".join(f"{k}={v!r}" for k, v in list(ev.get("extra", {}).items())[:2])
            lines.append(f"[TOOL {ts}] {ev.get('tool','')}({extra})")
        elif kind == "tool_result":
            lines.append(f"[RESULT] {ev.get('text','')[:200]}")
    return "\n".join(lines)


def review_session_start(pid: int) -> dict:
    """Start a background `claude -p` review (non-interactive, no new window)."""
    w = _claude_window(pid)
    if not w:
        return {"ok": False, "error": f"no window pid={pid}"}
    if pid in _review_results and _review_results[pid].get("status") == "running":
        return {"ok": True, "status": "already_running"}

    name = w.get("display_name") or w.get("name") or w.get("project_name") or "session"
    cwd = w.get("cwd") or str(Path.home())
    transcript = w.get("transcript_path") or ""
    if not transcript:
        return {"ok": False, "error": "no transcript to review"}

    summary = _build_review_summary(transcript, limit=40)

    prompt = (
        f"请 review 以下 Claude Code session 的工作成果。\n"
        f"Session: {name}\n"
        f"CWD: {cwd}\n\n"
        f"## 最近对话记录\n\n{summary}\n\n"
        f"请检查：\n"
        f"1. 任务是否完成\n"
        f"2. 有无低级错误或遗漏\n"
        f"3. 有无安全问题\n"
        f"4. 给出结论：PASS（可以关闭） / FAIL（需要继续或修复） / PARTIAL（部分完成）\n"
        f"用中文回答，200字以内。"
    )

    prompt_file = Path(f"/tmp/lucid-review-{pid}.txt")
    prompt_file.write_text(prompt, encoding="utf-8")

    _review_results[pid] = {"status": "running", "name": name}

    import threading

    def _run():
        try:
            cmd = f'cat {shlex.quote(str(prompt_file))} | claude -p --output-format text'
            proc = subprocess.run(
                ["zsh", "-c", f"source ~/.zshrc 2>/dev/null; cd {shlex.quote(cwd)} && {cmd}"],
                capture_output=True, timeout=120, **subprocess_text_kwargs(),
            )
            _review_results[pid] = {
                "status": "done",
                "name": name,
                "verdict": proc.stdout.strip()[-3000:],
                "rc": proc.returncode,
                "error": proc.stderr.strip()[-500:] if proc.returncode != 0 else "",
            }
        except Exception as e:
            _review_results[pid] = {"status": "error", "name": name, "error": str(e)}
        finally:
            prompt_file.unlink(missing_ok=True)

    threading.Thread(target=_run, daemon=True).start()
    return {"ok": True, "status": "started", "name": name}


def review_session_result(pid: int) -> dict:
    """Get the result of a background review."""
    return _review_results.get(pid, {"status": "not_found"})


def close_session(pid: int) -> dict:
    """Send SIGTERM to a Claude Code session for graceful shutdown."""
    w = _claude_window(pid)
    if not w:
        return {"ok": False, "error": f"no window pid={pid}"}
    if not w.get("alive"):
        return {"ok": True, "message": "already dead"}
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return {"ok": True, "message": "already dead"}
    except PermissionError:
        return {"ok": False, "error": "permission denied"}
    except Exception as e:
        return {"ok": False, "error": str(e)}
    return {"ok": True, "pid": pid, "message": f"SIGTERM sent to {pid}"}


_REVIEW_PROMPT = "请 review 你刚才做的工作，检查是否有低级错误、遗漏、安全问题。列出发现的问题和修复建议。"


def review_session(pid: int) -> dict:
    """Open a new iTerm2 window, resume the session, and send a review prompt."""
    w = _claude_window(pid)
    if not w:
        return {"ok": False, "error": f"no window pid={pid}"}

    session_id = w.get("session_id") or ""
    cwd = w.get("cwd") or str(Path.home())
    resume_cmd = f"cd {shlex.quote(cwd)} && claude --resume {shlex.quote(session_id)}"
    # AppleScript: open new window → type resume command → wait a bit → type review prompt
    escaped_resume = resume_cmd.replace('\\', '\\\\').replace('"', '\\"')
    escaped_review = _REVIEW_PROMPT.replace('\\', '\\\\').replace('"', '\\"')
    script = f'''tell application "iTerm2"
    activate
    set newWin to (create window with default profile)
    tell current session of newWin
        write text "{escaped_resume}"
        delay 3
        write text "{escaped_review}"
    end tell
end tell'''
    try:
        proc = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True, timeout=15, **subprocess_text_kwargs(),
        )
    except Exception as e:
        return {"ok": False, "error": str(e)}
    return {
        "ok": proc.returncode == 0,
        "pid": pid,
        "session_id": session_id,
    }
