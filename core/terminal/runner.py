"""CLI helpers for LUCID-managed Claude/Codex/Bash launches."""
from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from . import registry, runtime
from core.common.text_encoding import open_utf8, read_utf8, subprocess_text_kwargs


MANAGED_PLATFORMS = ("claude", "codex", "bash")


@dataclass(frozen=True)
class RunArguments:
    cwd: str
    tmux: bool
    tmux_session: str
    display_name: str
    command: list[str]


def _codex_files() -> set[Path]:
    base = Path.home() / ".codex" / "sessions"
    if not base.exists():
        return set()
    return set(base.rglob("*.jsonl"))


def _find_codex_file(session_id: str) -> Path | None:
    for path in _codex_files():
        if path.stem == session_id or session_id in path.stem:
            return path
    return None


def _codex_resume_session_id(command: list[str]) -> str:
    try:
        resume_index = command.index("resume")
    except ValueError:
        return ""
    if resume_index + 1 >= len(command):
        return ""
    candidate = command[resume_index + 1]
    return "" if candidate.startswith("-") else candidate


def _claude_session_files() -> list[Path]:
    base = Path.home() / ".claude" / "sessions"
    if not base.exists():
        return []
    return [path for path in base.glob("*.json") if not path.name.startswith("session-")]


def _bind_new_codex_session(process_id: str, before: set[Path]) -> None:
    deadline = time.time() + 30
    while time.time() < deadline:
        current = _codex_files()
        new_files = sorted(current - before, key=lambda p: p.stat().st_mtime if p.exists() else 0, reverse=True)
        for f in new_files:
            meta = _read_codex_meta(f)
            sid = meta.get("id")
            if sid:
                registry.update_binding(process_id, sid, str(f))
                return
        time.sleep(1)


def _read_codex_meta(path: Path) -> dict:
    try:
        with open_utf8(path) as f:
            d = json.loads(f.readline())
    except Exception:
        return {}
    if d.get("type") != "session_meta":
        return {}
    return d.get("payload") or {}


def _bind_claude_session(process_id: str, pid: int) -> None:
    deadline = time.time() + 30
    while time.time() < deadline:
        for path in _claude_session_files():
            try:
                data = json.loads(read_utf8(path))
            except Exception:
                continue
            if int(data.get("pid") or -1) != pid:
                continue
            session_id = data.get("sessionId", "")
            cwd = data.get("cwd", "")
            if not session_id:
                continue
            project_slug = cwd.replace("/", "-").replace("_", "-").replace(".", "-")
            transcript = Path.home() / ".claude" / "projects" / project_slug / f"{session_id}.jsonl"
            registry.update_binding(process_id, session_id, str(transcript), str(path))
            return
        time.sleep(1)


def run_process(
    platform: str,
    command: list[str],
    cwd: str | None = None,
    tmux_session: str = "",
    display_name: str = "",
) -> int:
    if not command:
        raise ValueError("command is required")
    workdir = cwd or os.getcwd()
    before = _codex_files() if platform == "codex" else set()
    proc = subprocess.Popen(command, cwd=workdir)
    process_id = str(uuid.uuid4())
    registry.register_process(
        process_id=process_id,
        platform=platform,
        pid=proc.pid,
        cwd=workdir,
        argv=" ".join(shlex.quote(x) for x in command),
        tty=os.ttyname(0) if sys.stdin.isatty() else "",
        tmux_session=tmux_session,
        display_name=display_name,
    )
    if platform == "codex":
        resume_session_id = _codex_resume_session_id(command)
        if resume_session_id:
            transcript = _find_codex_file(resume_session_id)
            registry.update_binding(process_id, resume_session_id, str(transcript) if transcript else "")
        else:
            _bind_new_codex_session(process_id, before)
    elif platform == "claude":
        _bind_claude_session(process_id, proc.pid)
    rc = proc.wait()
    registry.mark_exit(process_id, rc)
    return rc


def launch_tmux(
    platform: str,
    command: list[str],
    cwd: str | None = None,
    session_name: str | None = None,
    display_name: str = "",
) -> dict:
    workdir = cwd or os.getcwd()
    name = session_name or f"lucid-{platform}-{uuid.uuid4().hex[:8]}"
    quoted_cmd = " ".join(shlex.quote(x) for x in command)
    python_bin = shlex.quote(sys.executable)
    code_dir = shlex.quote(str(Path(__file__).resolve().parents[2]))
    display_arg = f" --display-name {shlex.quote(display_name)}" if display_name else ""
    exec_cmd = (
        f"cd {shlex.quote(workdir)} && "
        f"PYTHONPATH={code_dir}${{PYTHONPATH:+:$PYTHONPATH}} "
        f"{python_bin} -m core.terminal.runner exec --platform {shlex.quote(platform)} "
        f"--tmux-session {shlex.quote(name)}{display_arg} -- {quoted_cmd}"
    )
    if platform == "bash":
        runner = exec_cmd
    else:
        runner = (
            f"{exec_cmd}; "
            'rc=$?; printf "\\n[LUCID] command exited rc=%s\\n" "$rc"; '
            "exec bash -l"
        )
    proc = subprocess.run(
        [runtime.tmux_bin(), "new-session", "-d", "-s", name, runner],
        capture_output=True,
        timeout=10,
        **subprocess_text_kwargs(),
    )
    return {
        "ok": proc.returncode == 0,
        "tmux_session": name,
        "stdout": proc.stdout,
        "stderr": proc.stderr,
        "rc": proc.returncode,
        "display_name": display_name,
    }


def _strip_command_separator(command: list[str]) -> list[str]:
    if command and command[0] == "--":
        return command[1:]
    return command


def _default_command(platform: str, command: list[str]) -> list[str]:
    stripped = _strip_command_separator(command)
    if stripped:
        return stripped
    if platform == "bash":
        return ["bash", "-l"]
    return [platform]


def _parse_run_tail(
    platform: str,
    cwd: str,
    tmux: bool,
    tmux_session: str,
    display_name: str,
    tail: list[str],
) -> RunArguments:
    index = 0
    while index < len(tail):
        item = tail[index]
        if item == "--":
            return RunArguments(cwd=cwd, tmux=tmux, tmux_session=tmux_session, display_name=display_name, command=tail[index + 1 :])
        if item == "--tmux":
            tmux = True
            index += 1
            continue
        if item == "--cwd":
            if index + 1 >= len(tail):
                raise ValueError("--cwd requires a value")
            cwd = tail[index + 1]
            index += 2
            continue
        if item.startswith("--cwd="):
            cwd = item.split("=", 1)[1]
            index += 1
            continue
        if item == "--tmux-session":
            if index + 1 >= len(tail):
                raise ValueError("--tmux-session requires a value")
            tmux_session = tail[index + 1]
            index += 2
            continue
        if item.startswith("--tmux-session="):
            tmux_session = item.split("=", 1)[1]
            index += 1
            continue
        if item == "--display-name":
            if index + 1 >= len(tail):
                raise ValueError("--display-name requires a value")
            display_name = tail[index + 1]
            index += 2
            continue
        if item.startswith("--display-name="):
            display_name = item.split("=", 1)[1]
            index += 1
            continue
        if item.startswith("--"):
            return RunArguments(cwd=cwd, tmux=tmux, tmux_session=tmux_session, display_name=display_name, command=[platform, *tail[index:]])
        return RunArguments(cwd=cwd, tmux=tmux, tmux_session=tmux_session, display_name=display_name, command=tail[index:])
    return RunArguments(cwd=cwd, tmux=tmux, tmux_session=tmux_session, display_name=display_name, command=[])


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="lucid")
    sub = parser.add_subparsers(dest="cmd", required=True)

    exec_p = sub.add_parser("exec")
    exec_p.add_argument("--platform", choices=MANAGED_PLATFORMS, required=True)
    exec_p.add_argument("--cwd", default="")
    exec_p.add_argument("--tmux-session", default="")
    exec_p.add_argument("--display-name", default="")
    exec_p.add_argument("command", nargs=argparse.REMAINDER)

    launch_p = sub.add_parser("launch")
    launch_p.add_argument("--platform", choices=MANAGED_PLATFORMS, required=True)
    launch_p.add_argument("--cwd", default="")
    launch_p.add_argument("--tmux", action="store_true")
    launch_p.add_argument("--display-name", default="")
    launch_p.add_argument("command", nargs=argparse.REMAINDER)

    run_p = sub.add_parser("run", help="Run a Claude, Codex, or Bash process under LUCID management")
    run_p.add_argument("platform", choices=MANAGED_PLATFORMS)
    run_p.add_argument("--cwd", default="")
    run_p.add_argument("--tmux", action="store_true")
    run_p.add_argument("--tmux-session", default="")
    run_p.add_argument("--display-name", default="")
    run_p.add_argument("command", nargs=argparse.REMAINDER)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.cmd == "exec":
        command = _strip_command_separator(list(args.command))
        return run_process(
            args.platform,
            command,
            cwd=args.cwd or None,
            tmux_session=args.tmux_session,
            display_name=args.display_name,
        )
    if args.cmd == "launch":
        command = _strip_command_separator(list(args.command))
        if args.tmux:
            print(json.dumps(launch_tmux(args.platform, command, cwd=args.cwd or None, display_name=args.display_name)))
            return 0
        return run_process(args.platform, command, cwd=args.cwd or None, display_name=args.display_name)
    if args.cmd == "run":
        try:
            run_args = _parse_run_tail(
                args.platform,
                cwd=args.cwd,
                tmux=args.tmux,
                tmux_session=args.tmux_session,
                display_name=args.display_name,
                tail=list(args.command),
            )
        except ValueError as exc:
            parser.error(str(exc))
        command = _default_command(args.platform, run_args.command)
        if run_args.tmux:
            print(
                json.dumps(
                    launch_tmux(
                        args.platform,
                        command,
                        cwd=run_args.cwd or None,
                        session_name=run_args.tmux_session or None,
                        display_name=run_args.display_name,
                    )
                )
            )
            return 0
        return run_process(args.platform, command, cwd=run_args.cwd or None, display_name=run_args.display_name)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
