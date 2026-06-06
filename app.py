"""LUCID — FastAPI app: dashboard backend + SSE."""
from __future__ import annotations

import asyncio
import base64
import binascii
import contextlib
import io
import json
import logging
import mimetypes
import os
import select
import shlex
import shutil
import socket
import struct
import subprocess
import sys
import threading
import time
import urllib.parse
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from fastapi import Body, FastAPI, Header, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, Response
from paramiko.ssh_exception import AuthenticationException, NoValidConnectionsError, SSHException
from sse_starlette.sse import EventSourceResponse
from websockets.asyncio.client import connect as websocket_connect

from core.common.text_encoding import read_utf8, subprocess_text_kwargs, write_utf8
from core.conversations import codex, history, search, transcripts
from core.dashboard import localstate
from core.hub import nodes
from core.hub.skills_sync import (
    build_agent_skills_tarball_raw,
    build_skills_tarball_bytes,
    install_skills_append_only,
    install_skills_to_remote,
    pull_skills_via_ssh,
    sync_skills_via_ssh,
)
from core.hub.ssh_deploy import DeployRequest, deploy_agent
from core.knowledge import memory, plans, skills
from core.terminal import actions, path_browser, registry, runner, runtime

try:
    import fcntl
    import pty
    import termios
except ImportError:  # pragma: no cover - Windows hub mode does not expose local tmux PTYs.
    fcntl = None
    pty = None
    termios = None

HERE = Path(__file__).parent
STATIC_DIR = HERE / "static"


# ---------- shared in-memory state ----------

class State:
    def __init__(self) -> None:
        self.last_snapshot: dict = {"windows": [], "counts": {}, "ts": 0}
        self.last_signature: tuple = ()
        self.subscribers: set[asyncio.Queue] = set()

    def diff_signature(self, snap: dict) -> tuple:
        # Include derived triage fields so prompt/completion changes inferred
        # from transcripts or tmux panes are pushed even when process metadata
        # did not change.
        return tuple(
            (
                w.get("window_key") or w.get("pid"),
                w.get("status"),
                w.get("waiting_for"),
                w.get("triage"),
                w.get("triage_reason"),
                w.get("activity_label"),
                w.get("permission_msg"),
                w.get("name"),
                w.get("updated_at"),
                w.get("node_health"),
            )
            for w in snap["windows"]
        )


state = State()


@dataclass
class DeployJob:
    id: str
    status: str
    step: str
    message: str
    next_action: str
    started_at: float
    updated_at: float
    events: list[dict[str, Any]] = field(default_factory=list)
    result: dict[str, Any] | None = None
    error: str = ""


DEPLOY_JOBS: dict[str, DeployJob] = {}
DEPLOY_JOBS_LOCK = threading.Lock()
DEPLOY_JOB_LIMIT = 20
LOCAL_AGENT_READY_TIMEOUT_SECONDS = int(os.environ.get("LUCID_LOCAL_AGENT_READY_TIMEOUT_SECONDS", "900"))
DEPLOY_STEP_ACTIONS = {
    "queued": "Waiting for the backend worker to start.",
    "validate": "Checking the request fields.",
    "prepare_local": "Preparing local files before SSH starts.",
    "connect_ssh": "Waiting for SSH to connect or fail.",
    "detect_remote": "Reading remote OS and architecture.",
    "runtime_micromamba": "Downloading micromamba if it is not cached yet.",
    "runtime_download": "Downloading target-platform runtime packages. The first run can take several minutes.",
    "runtime_download_retry": "Network download failed once; waiting briefly before retrying.",
    "runtime_pack": "Packing the offline runtime bundle.",
    "runtime_cache": "Using the existing runtime bundle.",
    "upload": "Uploading files to the remote server.",
    "stop_agent": "Stopping the previous remote agent before replacing it.",
    "install_app": "Extracting the current checkout on the remote server.",
    "install_runtime": "Creating the remote offline Python/tmux runtime.",
    "start_agent": "Starting the remote agent process.",
    "health_check": "Waiting for the remote agent health endpoint.",
    "persist_config": "Writing hub node configuration.",
    "verify_tunnel": "Opening the local SSH tunnel and checking the agent through it.",
    "complete": "Refresh the node list or launch a managed process.",
    "failed": "Read the error, fix the reported issue, and retry deploy.",
}


class QuietDeployPollFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if isinstance(args, tuple) and len(args) >= 3:
            return not str(args[2]).startswith("/api/nodes/deploy/jobs/")
        return "/api/nodes/deploy/jobs/" not in record.getMessage()


logging.getLogger("uvicorn.access").addFilter(QuietDeployPollFilter())


def _enriched_snapshot() -> dict:
    if _is_hub_mode():
        return nodes.aggregate_snapshot()
    return localstate.wire_snapshot(_local_node_id(), _local_node_id())


def _refresh_snapshot_cache() -> None:
    try:
        state.last_snapshot = _enriched_snapshot()
        state.last_signature = state.diff_signature(state.last_snapshot)
    except Exception as e:
        print(f"[snapshot-refresh] error: {e}")


def _mode() -> str:
    return os.environ.get("LUCID_MODE", "hub").strip().lower() or "hub"


def _is_hub_mode() -> bool:
    return _mode() == "hub"


def _local_node_id() -> str:
    return os.environ.get("LUCID_NODE_ID") or ("local" if _mode() != "agent" else localstate.default_node_id())


def _require_agent_auth(authorization: str | None) -> None:
    token = os.environ.get("LUCID_AGENT_TOKEN", "")
    if not token:
        return
    expected = f"Bearer {token}"
    if authorization != expected:
        raise HTTPException(401, "invalid agent token")


def _public_node(node: nodes.NodeConfig, health: dict | None = None) -> dict:
    return {
        "id": node.id,
        "name": node.display_name,
        "kind": node.kind,
        "host": node.host or node.ssh_host,
        "user": node.user,
        "ssh_port": node.ssh_port,
        "local_port": node.local_port,
        "agent_port": node.agent_port,
        "remote_dir": node.remote_dir,
        "auto_tunnel": node.auto_tunnel,
        "auto_deploy": node.auto_deploy,
        "url": node.base_url if node.kind != "local" else "",
        "health": (health or {}).get("health", "unknown"),
        "error": (health or {}).get("error"),
        "window_count": int((health or {}).get("window_count", 0)),
        "hostname": (health or {}).get("hostname", ""),
    }


def _is_local_enabled_node(node: nodes.NodeConfig) -> bool:
    return node.id == "local" and node.kind in {"local", "agent"}


def _configured_node(node_id: str) -> nodes.NodeConfig:
    node = nodes.node_by_id(node_id)
    if not node:
        raise HTTPException(404, f"unknown node {node_id}")
    return node


def _ssh_attach_command(node: nodes.NodeConfig, attach_command: str) -> str:
    target = node.ssh_host
    if not target:
        target = f"{node.user}@{node.host}" if node.user and node.host else node.host
    if not target:
        return attach_command
    cmd = ["ssh"]
    if node.identity_file:
        cmd.extend(["-i", str(Path(node.identity_file).expanduser())])
    if node.ssh_port:
        cmd.extend(["-p", str(node.ssh_port)])
    cmd.extend(["-t", target, attach_command])
    return " ".join(shlex.quote(part) for part in cmd)


def _coerce_command(payload: dict) -> list[str]:
    raw = payload.get("command")
    if isinstance(raw, str):
        command = shlex.split(raw)
    elif isinstance(raw, list) and all(isinstance(item, str) for item in raw):
        command = list(raw)
    else:
        raise HTTPException(400, "command must be a string or list of strings")
    if not command:
        raise HTTPException(400, "command is required")
    return command


# ---------- command resolution cache ----------

_command_cache: dict[str, list[str]] = {}
_node_cache: str | None = None


def _clear_resolution_cache() -> None:
    global _command_cache, _node_cache
    _command_cache.clear()
    _node_cache = None


def _find_node_binary(candidate_dirs: list[Path] | None = None) -> str | None:
    """Search for a working ``node`` binary, preferring newest nvm install."""
    global _node_cache
    if _node_cache is not None:
        return _node_cache
    search: list[Path] = list(candidate_dirs or [])
    # nvm — newest first
    nvm_versions_dir = Path.home() / ".nvm" / "versions" / "node"
    if nvm_versions_dir.exists():
        try:
            versions = sorted(
                [d for d in nvm_versions_dir.iterdir() if d.is_dir()],
                reverse=True,
            )
            for vdir in versions:
                bin_dir = vdir / "bin"
                if bin_dir.is_dir():
                    search.append(bin_dir)
        except OSError:
            pass
    search.extend([
        Path("/usr/local/bin"),
        Path("/usr/bin"),
        Path.home() / ".local/bin",
        Path.home() / "bin",
    ])
    for base in search:
        candidate = base / "node"
        if candidate.exists() and os.access(candidate, os.X_OK):
            _node_cache = str(candidate)
            return _node_cache
    return None


def _resolve_command_local(command: list[str]) -> list[str]:
    global _command_cache
    executable = command[0] if command else ""
    if not executable:
        raise HTTPException(400, "command is required")
    # Hit cache: already resolved this command before
    cache_key = executable if "/" not in executable else str(Path(executable).expanduser())
    if cache_key in _command_cache:
        cached = _command_cache[cache_key]
        # Re-validate: if the cached binary is gone, clear and re-resolve
        if Path(cached[-1] if len(cached) > len(command) else cached[0]).exists():
            return cached + command[1:]
        _command_cache.pop(cache_key, None)
    # Already an absolute/relative path — validate it exists and is executable
    if "/" in executable:
        path = Path(executable).expanduser()
        if not (path.exists() and os.access(path, os.X_OK)):
            raise HTTPException(
                400,
                f"command not found or not executable: {executable}",
            )
        result = _maybe_node_prepend([str(path)] + command[1:])
    else:
        # Resolve via shutil.which (respects current process PATH)
        resolved = shutil.which(executable)
        if resolved is not None:
            result = _maybe_node_prepend([resolved] + command[1:])
        else:
            # PATH fallback: scan common locations
            _extra_dirs = [
                Path("/usr/local/bin"),
                Path("/usr/bin"),
                Path("/usr/local/sbin"),
                Path("/usr/sbin"),
                Path.home() / ".local/bin",
                Path.home() / "bin",
                Path.home() / ".bun/bin",
                Path("/root/.bun/bin"),
            ]
            nvm_versions_dir = Path.home() / ".nvm" / "versions" / "node"
            if nvm_versions_dir.exists():
                try:
                    versions = sorted(
                        [d for d in nvm_versions_dir.iterdir() if d.is_dir()],
                        reverse=True,
                    )
                    for vdir in versions:
                        bin_dir = vdir / "bin"
                        if bin_dir.is_dir():
                            _extra_dirs.append(bin_dir)
                except OSError:
                    pass
            for base in _extra_dirs:
                candidate = base / executable
                if candidate.exists() and os.access(candidate, os.X_OK):
                    result = _maybe_node_prepend([str(candidate)] + command[1:])
                    break
            else:
                # Last resort: ask a login shell via subprocess (gets the
                # full interactive PATH the user sees at their prompt)
                try:
                    shell_result = subprocess.run(
                        ["bash", "-lc", f"which {shlex.quote(executable)}"],
                        capture_output=True,
                        text=True,
                        timeout=5,
                    )
                    if shell_result.returncode == 0 and shell_result.stdout.strip():
                        resolved_shell = shell_result.stdout.strip().split("\n")[0]
                        result = _maybe_node_prepend([resolved_shell] + command[1:])
                    else:
                        raise HTTPException(
                            400,
                            f"command not found on agent PATH: {executable}",
                        )
                except HTTPException:
                    raise
                except Exception:
                    raise HTTPException(
                        400,
                        f"command not found on agent PATH: {executable}",
                    )
    _command_cache[cache_key] = result
    return result


def _maybe_node_prepend(command: list[str]) -> list[str]:
    """If ``command[0]`` is a Node.js script (shebang ``#!/usr/bin/env node``),
    prepend a matching ``node`` binary so the tmux session doesn't pick up
    an old system node that lacks top-level-await support."""
    script = Path(command[0])
    if script.suffix == ".node":
        # native binary — no shebang needed
        return command
    try:
        shebang = script.read_text(encoding="utf-8")[:128]
    except (OSError, UnicodeDecodeError):
        return command
    if not shebang.startswith("#!") or "node" not in shebang:
        return command
    # Prefer a known-good nvm node over whatever happens to be next to the
    # script on disk (a system-path sibling like /usr/local/bin/node is
    # often the stale version we're trying to avoid).
    node = _find_node_binary()
    if node is not None:
        return [node] + command
    # Fallback: sibling node in the same directory
    sibling = script.parent / "node"
    if sibling.exists() and os.access(sibling, os.X_OK):
        return [str(sibling)] + command
    return command


def _clean_display_name(value: Any) -> str:
    text = " ".join(str(value or "").split())
    return text[:80]


def _local_path_list(raw_path: str | None = None) -> dict:
    try:
        return path_browser.list_directories(raw_path)
    except FileNotFoundError as e:
        raise HTTPException(404, f"path does not exist: {raw_path or Path.home()}") from e
    except NotADirectoryError as e:
        raise HTTPException(400, str(e)) from e
    except PermissionError as e:
        raise HTTPException(403, f"path is not readable: {raw_path or Path.home()}") from e
    except OSError as e:
        raise HTTPException(400, f"path browse failed for {raw_path or Path.home()}: {e}") from e


def _resolve_local_path(raw_path: str) -> Path:
    if not str(raw_path or "").strip():
        raise HTTPException(400, "path is required")
    try:
        path = Path(raw_path).expanduser().resolve(strict=True)
    except FileNotFoundError as e:
        raise HTTPException(404, f"path does not exist: {raw_path}") from e
    except OSError as e:
        raise HTTPException(400, f"path resolution failed: {e}") from e
    return path


def _resolve_local_file(raw_path: str) -> Path:
    if not str(raw_path or "").strip():
        raise HTTPException(400, "path is required")
    try:
        path = Path(raw_path).expanduser().resolve(strict=True)
    except FileNotFoundError as e:
        raise HTTPException(404, f"file does not exist: {raw_path}") from e
    except OSError as e:
        raise HTTPException(400, f"file path failed: {e}") from e
    if not path.is_file():
        raise HTTPException(400, f"path is not a file: {path}")
    return path


def _local_file_read(raw_path: str) -> dict:
    path = _resolve_local_file(raw_path)
    if not os.access(path, os.R_OK):
        raise HTTPException(403, f"file is not readable: {path}")
    try:
        content = read_utf8(path)
        stat = path.stat()
    except PermissionError as e:
        raise HTTPException(403, f"file is not readable: {path}") from e
    except OSError as e:
        raise HTTPException(400, f"file read failed for {path}: {e}") from e
    return {
        "ok": True,
        "path": str(path),
        "name": path.name,
        "content": content,
        "mtime_ms": int(stat.st_mtime * 1000),
        "size": stat.st_size,
    }


MIME_MAP = {
    # images
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".svg": "image/svg+xml",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
    ".ico": "image/x-icon",
    ".tiff": "image/tiff",
    ".tif": "image/tiff",
    # documents
    ".pdf": "application/pdf",
    # fallback for binary
    ".bin": "application/octet-stream",
}


def _guess_mime(path: str) -> str:
    """Guess MIME type from file extension, with a few explicit overrides."""
    suffix = Path(path).suffix.lower()
    if suffix in MIME_MAP:
        return MIME_MAP[suffix]
    guessed, _ = mimetypes.guess_type(path)
    return guessed or "application/octet-stream"


def _local_file_raw(raw_path: str) -> tuple[bytes, str]:
    path = _resolve_local_file(raw_path)
    if not os.access(path, os.R_OK):
        raise HTTPException(403, f"file is not readable: {path}")
    try:
        content = path.read_bytes()
    except PermissionError as e:
        raise HTTPException(403, f"file is not readable: {path}") from e
    except OSError as e:
        raise HTTPException(400, f"file read failed for {path}: {e}") from e
    mime = _guess_mime(str(path))
    return content, mime


def _local_file_write(payload: dict) -> dict:
    path = _resolve_local_file(str(payload.get("path") or ""))
    if not os.access(path, os.W_OK):
        raise HTTPException(403, f"file is not writable: {path}")
    content = str(payload.get("content") or "")
    try:
        write_utf8(path, content)
        stat = path.stat()
    except PermissionError as e:
        raise HTTPException(403, f"file is not writable: {path}") from e
    except OSError as e:
        raise HTTPException(400, f"file write failed for {path}: {e}") from e
    return {
        "ok": True,
        "path": str(path),
        "name": path.name,
        "mtime_ms": int(stat.st_mtime * 1000),
        "size": stat.st_size,
    }


def _local_path_delete(raw_path: str) -> dict:
    path = _resolve_local_path(raw_path)
    directory = path.parent
    if not os.access(directory, os.W_OK | os.X_OK):
        raise HTTPException(403, f"directory is not writable: {directory}")
    result = {
        "ok": True,
        "path": str(path),
        "name": path.name,
        "directory": str(directory),
    }
    if path.is_dir():
        import shutil
        shutil.rmtree(path)
    else:
        try:
            path.unlink()
        except PermissionError as e:
            raise HTTPException(403, f"path is not deletable: {path}") from e
        except OSError as e:
            raise HTTPException(400, f"delete failed for {path}: {e}") from e
    return result


def _local_path_rename(payload: dict) -> dict:
    path = Path(str(payload.get("path") or ""))
    name = str(payload.get("name") or "")
    if not name.strip():
        raise HTTPException(400, "name is required")
    if "/" in name or "\\" in name:
        raise HTTPException(400, "invalid name")
    if not path.exists():
        raise HTTPException(404, f"path does not exist: {path}")
    directory = path.parent
    if not os.access(directory, os.W_OK | os.X_OK):
        raise HTTPException(403, f"directory is not writable: {directory}")
    target = directory / name
    if target.exists():
        raise HTTPException(409, f"target already exists: {target}")
    path.rename(target)
    return {"ok": True, "path": str(target.resolve(strict=True)), "name": name, "directory": str(directory)}


def _local_path_move(payload: dict) -> dict:
    src = Path(str(payload.get("path") or ""))
    dst_dir = Path(str(payload.get("destination") or ""))
    if not src.exists():
        raise HTTPException(404, f"source path does not exist: {src}")
    if not dst_dir.is_dir():
        raise HTTPException(400, f"destination is not a directory: {dst_dir}")
    if not os.access(dst_dir, os.W_OK | os.X_OK):
        raise HTTPException(403, f"destination directory is not writable: {dst_dir}")
    target = dst_dir / src.name
    if target.exists():
        raise HTTPException(409, f"target already exists: {target}")
    shutil.move(str(src), str(target))
    return {"ok": True, "path": str(target.resolve(strict=True)), "name": src.name, "directory": str(dst_dir)}


def _local_path_create(payload: dict) -> dict:
    directory = str(payload.get("directory") or "")
    name = str(payload.get("name") or "")
    if not name.strip():
        raise HTTPException(400, "directory name is required")
    try:
        return path_browser.create_directory(directory or None, name)
    except FileNotFoundError as e:
        raise HTTPException(404, f"parent directory does not exist: {directory or Path.home()}") from e
    except NotADirectoryError as e:
        raise HTTPException(400, str(e)) from e
    except FileExistsError as e:
        raise HTTPException(409, str(e)) from e
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    except PermissionError as e:
        raise HTTPException(403, f"directory is not writable: {directory or Path.home()}") from e
    except OSError as e:
        raise HTTPException(400, f"create directory failed for {directory or Path.home()}: {e}") from e


def _local_file_upload(payload: dict) -> dict:
    directory = str(payload.get("directory") or "")
    name = str(payload.get("name") or "")
    encoded = str(payload.get("content_base64") or "")
    try:
        content = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as e:
        raise HTTPException(400, "content_base64 is not valid base64") from e
    try:
        return path_browser.save_uploaded_file(directory or None, name, content)
    except FileNotFoundError as e:
        raise HTTPException(404, f"directory does not exist: {directory or Path.home()}") from e
    except NotADirectoryError as e:
        raise HTTPException(400, str(e)) from e
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    except PermissionError as e:
        raise HTTPException(403, f"directory is not writable: {directory or Path.home()}") from e
    except IsADirectoryError as e:
        raise HTTPException(400, str(e)) from e
    except OSError as e:
        raise HTTPException(400, f"file upload failed for {directory or Path.home()}: {e}") from e


def _deploy_local_agent() -> dict:
    remote_dir = "~/.lucid/agent"
    agent_dir = Path(remote_dir).expanduser()
    agent_port = _pick_local_agent_port()
    url = f"http://127.0.0.1:{agent_port}"
    _stop_local_agent()
    _copy_local_agent_tree(agent_dir)
    _write_local_agent_env(agent_dir, agent_port)
    pid, log_path, pidfile = _start_local_agent_process(agent_dir)
    node = nodes.NodeConfig(
        id="local",
        kind="agent",
        name="local",
        url=url,
        agent_host="127.0.0.1",
        agent_port=agent_port,
        auto_tunnel=False,
        auto_deploy=True,
        remote_dir=remote_dir,
    )
    _wait_local_agent_ready(node, pid, log_path)
    return {
        "ok": True,
        "agent_port": agent_port,
        "url": url,
        "remote_dir": remote_dir,
        "pid": pid,
        "pidfile": str(pidfile),
        "log": str(log_path),
    }


def _pick_local_agent_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _copy_local_agent_tree(agent_dir: Path) -> None:
    source = HERE.resolve()
    target = agent_dir.expanduser().resolve()
    if target == source or source in target.parents:
        raise HTTPException(400, f"local agent directory cannot be inside the current checkout: {target}")
    shutil.rmtree(target, ignore_errors=True)
    ignore = shutil.ignore_patterns(
        ".agents",
        ".codex",
        ".git",
        ".lucid-runtime",
        ".pytest_cache",
        ".venv",
        "__pycache__",
        "*.egg-info",
        "deployment_package",
        "demo-home",
    )
    try:
        shutil.copytree(source, target, ignore=ignore)
    except OSError as e:
        raise HTTPException(500, f"local agent checkout copy failed: {e}") from e


def _write_local_agent_env(agent_dir: Path, agent_port: int) -> None:
    lines = [
        "LUCID_MODE=agent",
        "LUCID_NODE_ID=local",
        "LUCID_AGENT_HOST=127.0.0.1",
        f"LUCID_PORT={agent_port}",
        f"LUCID_PYTHON={shlex.quote(sys.executable)}",
        "LUCID_NO_VENV=1",
        'LUCID_RUNTIME_DIR="$PWD/.lucid-runtime"',
        'PATH="$PWD/.lucid-runtime/env/bin:$PATH"',
        "NO_PROXY=127.0.0.1,localhost",
        "no_proxy=127.0.0.1,localhost",
    ]
    if os.environ.get("LUCID_HOME"):
        lines.append(f"LUCID_HOME={shlex.quote(os.environ['LUCID_HOME'])}")
    try:
        (agent_dir / ".agent.env").write_text("\n".join(lines) + "\n", encoding="utf-8")
    except OSError as e:
        raise HTTPException(500, f"failed to write local agent environment: {e}") from e


def _start_local_agent_process(agent_dir: Path) -> tuple[int, Path, Path]:
    nodes.STATE_DIR.mkdir(parents=True, exist_ok=True)
    nodes.STATE_DIR.chmod(0o700)
    log_dir = nodes.STATE_DIR / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "agent-local.log"
    pidfile = nodes.STATE_DIR / "agent-local.pid"
    command = "set -a && . ./.agent.env && set +a && exec bash run.sh"
    try:
        log = log_path.open("ab")
        proc = subprocess.Popen(
            ["bash", "-lc", command],
            cwd=str(agent_dir),
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    except FileNotFoundError as e:
        raise HTTPException(500, "bash is required to start the local agent") from e
    except OSError as e:
        raise HTTPException(500, f"failed to start local agent: {e}") from e
    finally:
        with contextlib.suppress(UnboundLocalError):
            log.close()
    pidfile.write_text(f"{proc.pid}\n", encoding="utf-8")
    pidfile.chmod(0o600)
    return proc.pid, log_path, pidfile


def _stop_local_agent() -> None:
    pidfile = nodes.STATE_DIR / "agent-local.pid"
    try:
        pid = int(pidfile.read_text(encoding="utf-8").strip())
    except (FileNotFoundError, ValueError, OSError):
        pid = 0
    if pid > 0:
        with contextlib.suppress(OSError):
            os.kill(pid, 15)
        deadline = time.time() + 2
        while time.time() < deadline:
            try:
                os.kill(pid, 0)
            except OSError:
                break
            time.sleep(0.1)
        else:
            with contextlib.suppress(OSError):
                os.kill(pid, 9)
    with contextlib.suppress(OSError):
        pidfile.unlink()


def _wait_local_agent_ready(node: nodes.NodeConfig, pid: int, log_path: Path) -> None:
    last_error = ""
    deadline = time.time() + LOCAL_AGENT_READY_TIMEOUT_SECONDS
    while time.time() < deadline:
        try:
            os.kill(pid, 0)
        except OSError:
            raise HTTPException(502, f"local agent exited before becoming ready: {_tail_file(log_path)}")
        try:
            nodes.agent_get(node, "/agent/v1/snapshot", timeout_ms=1000)
            return
        except Exception as e:
            last_error = str(e)
            time.sleep(0.5)
    raise HTTPException(
        504,
        f"local agent did not become ready within {LOCAL_AGENT_READY_TIMEOUT_SECONDS}s: {last_error}; "
        f"log={_tail_file(log_path)}",
    )


def _tail_file(path: Path, limit: int = 2000) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")[-limit:]
    except OSError:
        return ""


def _local_history_session(session_id: str, platform: str | None = None) -> dict | None:
    data = history.list_sessions(limit=9999)
    for sess in data.get("sessions", []):
        if sess.get("session_id") == session_id and (not platform or sess.get("platform", "claude") == platform):
            return sess
    return None


def _agent_resume_or_fork(platform: str, session_id: str, fork: bool) -> dict:
    sess = _local_history_session(session_id, platform=platform)
    if not sess:
        return {"ok": False, "error": "session not found in index", "session_id": session_id}
    cwd = sess.get("project") or str(Path.home())
    if platform == "claude":
        args = ["--resume", session_id]
        if fork:
            args.append("--fork-session")
    elif platform == "codex":
        args = ["fork" if fork else "resume", session_id]
    else:
        return {"ok": False, "error": f"resume/fork is not supported for {platform} sessions", "session_id": session_id}
    # Wrap in bash -li so login+interactive shell sources both
    # .bash_profile and .bashrc, picking up nvm/bun PATH entries.
    cmd_str = "exec " + platform + " " + " ".join(shlex.quote(a) for a in args)
    command = ["bash", "-l", "-i", "-c", cmd_str]
    result = runner.launch_tmux(platform, command, cwd=cwd)
    result.update({"action": "forked" if fork else "resumed", "session_id": session_id, "cwd": cwd})
    return result


def _timeline_for_window_row(platform: str, pid: int, row: dict, limit: int = 2000) -> dict:
    transcript_path = row.get("transcript_path") or ""
    session_id = row.get("session_id") or ""
    if transcript_path:
        if platform == "codex":
            events = codex.codex_timeline(transcript_path, limit=limit)
            return {
                "pid": pid,
                "session_id": session_id,
                "project_name": row.get("project_name"),
                "events": events,
                "platform": "codex",
            }
        events = transcripts.timeline(transcript_path, limit=limit)
        payload = {
            "pid": pid,
            "session_id": session_id,
            "project_name": row.get("project_name"),
            "events": events,
            "platform": platform,
        }
        if platform == "claude":
            payload.update({
                "skills_used": transcripts.extract_skills_used(transcript_path),
                "memory_ops": transcripts.extract_memory_ops(transcript_path),
                "plan_history": transcripts.extract_plan_history(transcript_path),
            })
        return payload
    if session_id:
        try:
            data = localstate.timeline_for_session(platform, session_id, limit=limit)
        except FileNotFoundError as e:
            raise HTTPException(404, "transcript not found") from e
        data["pid"] = pid
        return data
    raise HTTPException(404, "transcript not found")


def _local_window_timeline(platform: str, pid: int, limit: int = 2000) -> dict:
    row = _local_managed_window(platform, pid)
    return _timeline_for_window_row(platform, pid, row, limit=limit)


def _local_managed_window(platform: str, pid: int) -> dict:
    row = registry.find_managed_window(platform, pid)
    if not row:
        raise HTTPException(404, "window not found")
    return row


def _local_managed_claude_window(pid: int) -> dict:
    return _local_managed_window("claude", pid)


def _local_window_plan(platform: str, pid: int) -> dict:
    if platform != "claude":
        raise HTTPException(400, "plan is only supported for Claude windows")
    row = _local_managed_claude_window(pid)
    name = row.get("display_name") or row.get("name") or row.get("project_name")
    plan = plans.plan_for_session(name, row.get("cwd"), row.get("transcript_path"))
    return {"pid": pid, "plan": plan}


def _local_window_focus(platform: str, pid: int) -> dict:
    w = registry.find_managed_window(platform, pid) if platform == "claude" else None
    if w and w.get("tty"):
        return actions.focus_terminal(w["tty"])
    attach_command = registry.attach_command(platform, pid)
    if attach_command:
        return {"ok": True, "attach_command": attach_command}
    return {"ok": False, "error": "no tty or tmux attachment is available for this process"}


def _local_window_close(platform: str, pid: int) -> dict:
    return registry.close_managed(platform, pid)


def _local_window_rename(platform: str, pid: int, payload: dict) -> dict:
    return registry.rename_managed(platform, pid, _clean_display_name(payload.get("name") or payload.get("display_name")))


def _run_tmux(args: list[str]) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            [runtime.tmux_bin(), *args[1:]],
            capture_output=True,
            timeout=5,
            **subprocess_text_kwargs(),
        )
    except FileNotFoundError as e:
        raise HTTPException(500, "tmux is required for managed terminal I/O") from e
    except subprocess.TimeoutExpired as e:
        raise HTTPException(504, f"tmux command timed out: {' '.join(args)}") from e


def _local_window_terminal(platform: str, pid: int, lines: int = 200) -> dict:
    session_name = registry.tmux_session_for(platform, pid)
    if not session_name:
        return {"ok": False, "error": "window is not a LUCID-managed tmux process", "platform": platform, "pid": pid}
    safe_lines = max(20, min(lines, 1000))
    proc = _run_tmux(["tmux", "capture-pane", "-p", "-e", "-t", session_name, "-S", f"-{safe_lines}"])
    if proc.returncode != 0:
        return {"ok": False, "error": proc.stderr.strip() or "tmux capture-pane failed", "tmux_session": session_name}
    return {"ok": True, "platform": platform, "pid": pid, "tmux_session": session_name, "output": proc.stdout, "ansi": True}


def _local_window_terminal_input(platform: str, pid: int, payload: dict) -> dict:
    session_name = registry.tmux_session_for(platform, pid)
    if not session_name:
        return {"ok": False, "error": "window is not a LUCID-managed tmux process", "platform": platform, "pid": pid}
    text = str(payload.get("text") or "")
    key = str(payload.get("key") or "")
    enter = bool(payload.get("enter", False))
    if text:
        proc = _run_tmux(["tmux", "send-keys", "-t", session_name, "-l", text])
        if proc.returncode != 0:
            return {"ok": False, "error": proc.stderr.strip() or "tmux send-keys failed", "tmux_session": session_name}
    if key:
        proc = _run_tmux(["tmux", "send-keys", "-t", session_name, key])
        if proc.returncode != 0:
            return {"ok": False, "error": proc.stderr.strip() or "tmux send-keys failed", "tmux_session": session_name}
    elif enter:
        proc = _run_tmux(["tmux", "send-keys", "-t", session_name, "Enter"])
        if proc.returncode != 0:
            return {"ok": False, "error": proc.stderr.strip() or "tmux send-keys failed", "tmux_session": session_name}
    return _local_window_terminal(platform, pid)


def _terminal_attach_command(session_name: str) -> list[str]:
    return [
        runtime.tmux_bin(),
        "set-option",
        "-t",
        session_name,
        "mouse",
        "off",
        ";",
        "set-option",
        "-g",
        "history-limit",
        "100000",
        ";",
        "set-option",
        "-t",
        session_name,
        "status",
        "off",
        ";",
        "attach-session",
        "-t",
        session_name,
    ]


def _scroll_tmux_history(session_name: str, direction: str, lines: int) -> bool:
    if direction not in {"up", "down"}:
        return False
    safe_lines = max(1, min(int(lines or 1), 200))
    command = "scroll-up" if direction == "up" else "scroll-down"
    if direction == "up":
        proc = _run_tmux(["tmux", "copy-mode", "-e", "-t", session_name])
        if proc.returncode != 0:
            return False
    proc = _run_tmux(["tmux", "send-keys", "-t", session_name, "-X", "-N", str(safe_lines), command])
    return proc.returncode == 0


def _set_pty_size(fd: int, rows: int, cols: int) -> None:
    if fcntl is None or termios is None:
        raise RuntimeError("PTY resizing is not available on this platform")
    safe_rows = max(5, min(int(rows or 24), 200))
    safe_cols = max(20, min(int(cols or 80), 400))
    winsize = struct.pack("HHHH", safe_rows, safe_cols, 0, 0)
    fcntl.ioctl(fd, termios.TIOCSWINSZ, winsize)


def _write_pty_all(fd: int, data: bytes, timeout: float = 5.0) -> None:
    view = memoryview(data)
    written_total = 0
    deadline = time.monotonic() + timeout
    while written_total < len(view):
        try:
            written = os.write(fd, view[written_total:])
        except BlockingIOError:
            written = 0
        except InterruptedError:
            continue
        if written > 0:
            written_total += written
            continue

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("timed out writing terminal input")
        select.select([], [fd], [], min(0.1, remaining))


def _agent_terminal_ws_connect_args(
    node: nodes.NodeConfig,
    platform: str,
    pid: int,
    cols: int = 80,
    rows: int = 24,
) -> tuple[str, dict[str, str], bool | None]:
    parsed = urllib.parse.urlparse(node.base_url)
    scheme = "wss" if parsed.scheme == "https" else "ws"
    query = urllib.parse.urlencode({"cols": max(20, min(int(cols or 80), 400)), "rows": max(5, min(int(rows or 24), 200))})
    path = f"/agent/v1/windows/{urllib.parse.quote(platform)}/{pid}/terminal/ws"
    url = urllib.parse.urlunparse((scheme, parsed.netloc, path, "", query, ""))
    headers = {"Authorization": f"Bearer {node.auth_token}"} if node.auth_token else {}
    proxy = None if node.auto_tunnel or nodes._is_loopback_url(node.base_url) else True
    return url, headers, proxy


async def _local_terminal_ws(websocket: WebSocket, platform: str, pid: int, cols: int = 80, rows: int = 24) -> None:
    await websocket.accept()
    if pty is None or fcntl is None or termios is None:
        await websocket.send_text("\r\n[LUCID] local terminal websocket requires POSIX PTY support\r\n")
        await websocket.close(code=1011)
        return
    session_name = registry.tmux_session_for(platform, pid)
    if not session_name:
        await websocket.send_text("\r\n[LUCID] window is not a managed tmux process\r\n")
        await websocket.close(code=1008)
        return

    master_fd = -1
    slave_fd = -1
    proc: subprocess.Popen | None = None
    reader_stop = threading.Event()
    output_queue: asyncio.Queue[str | None] = asyncio.Queue(maxsize=256)
    loop = asyncio.get_running_loop()

    def enqueue(text: str | None) -> None:
        if output_queue.full():
            with contextlib.suppress(asyncio.QueueEmpty):
                output_queue.get_nowait()
        with contextlib.suppress(asyncio.QueueFull):
            output_queue.put_nowait(text)

    try:
        master_fd, slave_fd = pty.openpty()
        _set_pty_size(slave_fd, rows, cols)
        env = dict(os.environ)
        env.update({"TERM": "xterm-256color", "COLORTERM": "truecolor"})
        proc = subprocess.Popen(
            _terminal_attach_command(session_name),
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            close_fds=True,
            start_new_session=True,
            env=env,
        )
        os.close(slave_fd)
        slave_fd = -1
        os.set_blocking(master_fd, False)
    except FileNotFoundError:
        if slave_fd >= 0:
            os.close(slave_fd)
        if master_fd >= 0:
            os.close(master_fd)
        await websocket.send_text("\r\n[LUCID] tmux is required for terminal websocket\r\n")
        await websocket.close(code=1011)
        return
    except Exception as e:
        if slave_fd >= 0:
            os.close(slave_fd)
        if master_fd >= 0:
            os.close(master_fd)
        await websocket.send_text(f"\r\n[LUCID] terminal websocket failed: {e}\r\n")
        await websocket.close(code=1011)
        return

    def read_pty() -> None:
        try:
            while not reader_stop.is_set():
                ready, _, _ = select.select([master_fd], [], [], 0.1)
                if not ready:
                    if proc and proc.poll() is not None:
                        break
                    continue
                try:
                    data = os.read(master_fd, 16384)
                except BlockingIOError:
                    continue
                except OSError:
                    break
                if not data:
                    break
                loop.call_soon_threadsafe(enqueue, data.decode("utf-8", "replace"))
        finally:
            loop.call_soon_threadsafe(enqueue, None)

    reader = threading.Thread(target=read_pty, name=f"terminal-ws-{platform}-{pid}", daemon=True)
    reader.start()

    async def pty_to_ws() -> None:
        while True:
            chunk = await output_queue.get()
            if chunk is None:
                break
            await websocket.send_text(chunk)

    async def ws_to_pty() -> None:
        while True:
            raw = await websocket.receive_text()
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                payload = {"type": "data", "data": raw}
            msg_type = payload.get("type")
            if msg_type == "resize":
                _set_pty_size(master_fd, int(payload.get("rows") or 24), int(payload.get("cols") or 80))
                continue
            if msg_type == "scroll":
                _scroll_tmux_history(session_name, str(payload.get("direction") or ""), int(payload.get("lines") or 1))
                continue
            if msg_type == "data":
                data = str(payload.get("data") or "")
                if data:
                    await asyncio.to_thread(_write_pty_all, master_fd, data.encode("utf-8"))

    tasks = [asyncio.create_task(pty_to_ws()), asyncio.create_task(ws_to_pty())]
    try:
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            with contextlib.suppress(WebSocketDisconnect, asyncio.CancelledError, OSError):
                task.result()
        for task in pending:
            task.cancel()
    finally:
        reader_stop.set()
        for task in tasks:
            task.cancel()
        if master_fd >= 0:
            with contextlib.suppress(OSError):
                os.close(master_fd)
        if proc and proc.poll() is None:
            with contextlib.suppress(ProcessLookupError):
                proc.terminate()
            with contextlib.suppress(subprocess.TimeoutExpired):
                proc.wait(timeout=1)
        if proc and proc.poll() is None:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()


async def _proxy_terminal_ws(websocket: WebSocket, node: nodes.NodeConfig, platform: str, pid: int, cols: int, rows: int) -> None:
    await websocket.accept()
    url, headers, proxy = _agent_terminal_ws_connect_args(node, platform, pid, cols=cols, rows=rows)
    try:
        async with websocket_connect(
            url,
            additional_headers=headers or None,
            proxy=proxy,
            max_size=None,
            open_timeout=10,
        ) as agent_ws:
            async def browser_to_agent() -> None:
                while True:
                    msg = await websocket.receive()
                    if msg.get("type") == "websocket.disconnect":
                        break
                    if msg.get("text") is not None:
                        await agent_ws.send(msg["text"])
                    elif msg.get("bytes") is not None:
                        await agent_ws.send(msg["bytes"])

            async def agent_to_browser() -> None:
                async for msg in agent_ws:
                    if isinstance(msg, bytes):
                        await websocket.send_bytes(msg)
                    else:
                        await websocket.send_text(msg)

            tasks = [asyncio.create_task(browser_to_agent()), asyncio.create_task(agent_to_browser())]
            done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in pending:
                task.cancel()
            for task in done:
                with contextlib.suppress(WebSocketDisconnect, asyncio.CancelledError):
                    task.result()
    except Exception as e:
        with contextlib.suppress(Exception):
            await websocket.send_text(f"\r\n[LUCID] remote terminal websocket failed: {e}\r\n")
    finally:
        with contextlib.suppress(Exception):
            await websocket.close()


async def _watcher() -> None:
    """Poll sessions every 3s; broadcast deltas to SSE subscribers."""
    while True:
        try:
            snap = _enriched_snapshot()
            sig = state.diff_signature(snap)
            state.last_snapshot = snap
            if sig != state.last_signature:
                state.last_signature = sig
                payload = json.dumps(snap)
                dead: list[asyncio.Queue] = []
                for q in list(state.subscribers):
                    try:
                        q.put_nowait(payload)
                    except asyncio.QueueFull:
                        dead.append(q)
                for q in dead:
                    state.subscribers.discard(q)
        except Exception as e:
            print(f"[watcher] error: {e}")
        await asyncio.sleep(3)


@asynccontextmanager
async def lifespan(app: FastAPI):
    task = asyncio.create_task(_watcher())
    try:
        yield
    finally:
        task.cancel()


app = FastAPI(title="LUCID", lifespan=lifespan)


# ---------- routes ----------

@app.get("/", response_class=HTMLResponse)
def index() -> HTMLResponse:
    html = read_utf8(STATIC_DIR / "index.html")
    return HTMLResponse(html)


@app.get("/api/windows")
def api_windows() -> dict:
    if not state.last_snapshot["windows"]:
        state.last_snapshot = _enriched_snapshot()
    return state.last_snapshot


@app.get("/api/nodes")
def api_nodes() -> dict:
    cfg = nodes.load_config(force=True)
    health_by_id = {row.get("id"): row for row in (state.last_snapshot.get("nodes") or [])}
    local_enabled = any(_is_local_enabled_node(node) for node in cfg.nodes)
    return {
        "nodes": [_public_node(node, health_by_id.get(node.id)) for node in cfg.nodes],
        "ssh_history": nodes.ssh_connection_history(cfg),
        "config_path": str(nodes.CONFIG_PATH),
        "local_enabled": local_enabled,
    }


@app.post("/api/nodes/local/enable")
def api_node_local_enable() -> dict:
    if not _is_hub_mode():
        raise HTTPException(400, "local node can only be enabled from hub mode")
    install = _deploy_local_agent()
    node = nodes.NodeConfig(
        id="local",
        kind="agent",
        name="local",
        url=str(install["url"]),
        agent_host="127.0.0.1",
        agent_port=int(install["agent_port"]),
        auto_tunnel=False,
        auto_deploy=True,
        remote_dir=str(install["remote_dir"]),
    )
    nodes.write_node_config(node)
    nodes.invalidate_snapshot_cache(node.id)
    return {
        "ok": True,
        "node": _public_node(node),
        "install": install,
        "config_path": str(nodes.CONFIG_PATH),
    }


@app.delete("/api/nodes/{node_id}")
def api_node_delete(node_id: str) -> dict:
    # For local node, stop the local agent process first
    if node_id == "local":
        _stop_local_agent()
    try:
        result = nodes.remove_node_config(node_id)
    except OSError as e:
        raise HTTPException(500, f"failed to delete node {node_id}: {e}") from e
    if not result.get("ok"):
        raise HTTPException(400, result.get("error", "delete failed"))
    return result


@app.post("/api/nodes/{node_id}/remove")
def api_node_remove(node_id: str) -> dict:
    """Remove a node: kill remote agent, clear remote dir, then delete local config."""
    node = nodes.node_by_id(node_id)
    if not node:
        raise HTTPException(404, f"unknown node {node_id}")
    # 1) Remote cleanup via SSH (kill agent + rm -rf agent dir)
    remote_ok = False
    if node.kind == "ssh":
        cleanup_result = ssh_deploy.cleanup_remote_node(node_id)
        remote_ok = cleanup_result.get("ok", False)
        if not remote_ok:
            msg = cleanup_result.get("error", "unknown")
            raise HTTPException(502, f"remote cleanup failed: {msg}")
    # 2) Remove local config (also kills any active SSH tunnel)
    try:
        result = nodes.remove_node_config(node_id)
    except OSError as e:
        raise HTTPException(500, f"failed to remove node config: {e}") from e
    if not result.get("ok"):
        raise HTTPException(400, result.get("error", "remove config failed"))
    result["remote_cleaned"] = remote_ok
    return result


@app.get("/api/nodes/{node_id}/paths")
def api_node_paths(node_id: str, path: str = "") -> dict:
    node = _configured_node(node_id)
    if node.kind == "local":
        return _local_path_list(path or None)
    query = urllib.parse.urlencode({"path": path}) if path else ""
    suffix = f"?{query}" if query else ""
    return nodes.forward(node_id, "GET", f"/agent/v1/paths{suffix}")


@app.post("/api/nodes/{node_id}/paths")
def api_node_path_create(node_id: str, payload: dict = Body(...)) -> dict:
    node = _configured_node(node_id)
    if node.kind == "local":
        return _local_path_create(payload)
    return nodes.forward(node_id, "POST", "/agent/v1/paths", payload)


@app.get("/api/nodes/{node_id}/files")
def api_node_file_read(node_id: str, path: str = "") -> dict:
    node = _configured_node(node_id)
    if node.kind == "local":
        return _local_file_read(path)
    query = urllib.parse.urlencode({"path": path})
    return nodes.forward(node_id, "GET", f"/agent/v1/files?{query}")


@app.get("/api/nodes/{node_id}/files/raw")
def api_node_file_raw(node_id: str, path: str = ""):
    node = _configured_node(node_id)
    if node.kind == "local":
        content, mime = _local_file_raw(path)
        return Response(content=content, media_type=mime,
                        headers={"Content-Disposition": "inline"})
    # For remote nodes, proxy the raw response
    query = urllib.parse.urlencode({"path": path})
    try:
        raw, mime = nodes.forward_raw(node_id, "GET", f"/agent/v1/files/raw?{query}")
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(502, str(e))
    return Response(content=raw, media_type=mime,
                    headers={"Content-Disposition": "inline"})


@app.post("/api/nodes/{node_id}/files")
def api_node_file_write(node_id: str, payload: dict = Body(...)) -> dict:
    node = _configured_node(node_id)
    if node.kind == "local":
        return _local_file_write(payload)
    return nodes.forward(node_id, "POST", "/agent/v1/files", payload)


@app.delete("/api/nodes/{node_id}/files")
def api_node_file_delete(node_id: str, path: str = "") -> dict:
    node = _configured_node(node_id)
    if node.kind == "local":
        return _local_path_delete(path)
    query = urllib.parse.urlencode({"path": path})
    return nodes.forward(node_id, "DELETE", f"/agent/v1/files?{query}")


@app.patch("/api/nodes/{node_id}/paths")
def api_node_path_rename(node_id: str, payload: dict = Body(...)) -> dict:
    node = _configured_node(node_id)
    if node.kind == "local":
        return _local_path_rename(payload)
    return nodes.forward(node_id, "PATCH", "/agent/v1/paths", payload)


@app.patch("/api/nodes/{node_id}/paths/move")
def api_node_path_move(node_id: str, payload: dict = Body(...)) -> dict:
    node = _configured_node(node_id)
    if node.kind == "local":
        return _local_path_move(payload)
    return nodes.forward(node_id, "PATCH", "/agent/v1/paths/move", payload)


@app.post("/api/nodes/{node_id}/files/upload")
def api_node_file_upload(node_id: str, payload: dict = Body(...)) -> dict:
    node = _configured_node(node_id)
    if node.kind == "local":
        return _local_file_upload(payload)
    return nodes.forward(node_id, "POST", "/agent/v1/files/upload", payload)


def _deploy_request_from_payload(payload: dict) -> DeployRequest:
    remember_raw = payload.get("remember_password", True)
    remember_password = str(remember_raw).lower() not in {"0", "false", "no", "off"}
    return DeployRequest(
        id=str(payload.get("id", "")).strip(),
        host=str(payload.get("host", "")).strip(),
        user=str(payload.get("user", "")).strip(),
        password=str(payload.get("password") or ""),
        identity_file=str(payload.get("identity_file") or ""),
        ssh_port=int(payload.get("ssh_port") or 22),
        agent_port=int(payload.get("agent_port") or 0),
        local_port=int(payload.get("local_port") or 0),
        remote_dir=str(payload.get("remote_dir") or "~/.lucid/agent"),
        node_name=str(payload.get("node_name") or ""),
        python_command=str(payload.get("python_command") or payload.get("python") or "auto").strip(),
        remember_password=remember_password,
    )


def _deploy_password_for_node(node_id: str) -> str:
    for row in nodes.load_ssh_history():
        if row.get("id") == node_id and row.get("password"):
            return str(row.get("password") or "")
    return ""


def _deploy_request_from_node(node: nodes.NodeConfig) -> DeployRequest:
    host = node.host or node.ssh_host
    if not node.id or not host or not node.user:
        raise ValueError(f"node {node.id or '<unknown>'} is missing SSH host or user")
    return DeployRequest(
        id=node.id,
        host=host,
        user=node.user,
        password=_deploy_password_for_node(node.id),
        identity_file=node.identity_file,
        ssh_port=node.ssh_port,
        agent_port=node.agent_port,
        local_port=node.local_port,
        remote_dir=node.remote_dir,
        node_name=node.name,
        python_command="auto",
    )


def _deploy_job_payload(job: DeployJob) -> dict[str, Any]:
    return {
        "ok": job.status != "failed",
        "job_id": job.id,
        "status": job.status,
        "step": job.step,
        "message": job.message,
        "next_action": job.next_action,
        "started_at": job.started_at,
        "updated_at": job.updated_at,
        "events": job.events[-20:],
        "result": job.result,
        "error": job.error,
    }


def _deploy_next_action(step: str) -> str:
    return DEPLOY_STEP_ACTIONS.get(step, "Waiting for the current deploy step to finish.")


def _store_deploy_job(job: DeployJob) -> None:
    with DEPLOY_JOBS_LOCK:
        DEPLOY_JOBS[job.id] = job
        if len(DEPLOY_JOBS) <= DEPLOY_JOB_LIMIT:
            return
        finished = sorted(
            [row for row in DEPLOY_JOBS.values() if row.status in {"succeeded", "failed"}],
            key=lambda row: row.updated_at,
        )
        for old_job in finished[:max(0, len(DEPLOY_JOBS) - DEPLOY_JOB_LIMIT)]:
            DEPLOY_JOBS.pop(old_job.id, None)


def _get_deploy_job(job_id: str) -> DeployJob | None:
    with DEPLOY_JOBS_LOCK:
        return DEPLOY_JOBS.get(job_id)


def _set_deploy_job_progress(job_id: str, status: str, step: str, message: str) -> None:
    now = time.time()
    with DEPLOY_JOBS_LOCK:
        job = DEPLOY_JOBS[job_id]
        job.status = status
        job.step = step
        job.message = message
        job.next_action = _deploy_next_action(step)
        job.updated_at = now
        job.events.append({"ts": now, "step": step, "message": message})


def _finish_deploy_job(job_id: str, result: dict[str, Any]) -> None:
    _set_deploy_job_progress(job_id, "succeeded", "complete", "Deployment complete")
    with DEPLOY_JOBS_LOCK:
        DEPLOY_JOBS[job_id].result = result
    _clear_resolution_cache()
    node_id = str(result.get("node_id") or "")
    if node_id:
        nodes.invalidate_snapshot_cache(node_id)


def _fail_deploy_job(job_id: str, error: str) -> None:
    _set_deploy_job_progress(job_id, "failed", "failed", "Deployment failed")
    with DEPLOY_JOBS_LOCK:
        DEPLOY_JOBS[job_id].error = error


def _run_deploy_job(job_id: str, req: DeployRequest) -> None:
    try:
        result = deploy_agent(
            req,
            progress=lambda step, message: _set_deploy_job_progress(job_id, "running", step, message),
        )
        _finish_deploy_job(job_id, result)
    except Exception as exc:
        _fail_deploy_job(job_id, f"{type(exc).__name__}: {exc}")


def _start_deploy_job(req: DeployRequest) -> dict:
    job_id = uuid.uuid4().hex
    now = time.time()
    job = DeployJob(
        id=job_id,
        status="queued",
        step="queued",
        message="Deployment queued",
        next_action=_deploy_next_action("queued"),
        started_at=now,
        updated_at=now,
        events=[{"ts": now, "step": "queued", "message": "Deployment queued"}],
    )
    _store_deploy_job(job)
    thread = threading.Thread(target=_run_deploy_job, args=(job_id, req), daemon=True)
    thread.start()
    return _deploy_job_payload(job)


@app.post("/api/nodes/deploy")
def api_node_deploy(payload: dict = Body(...)) -> dict:
    try:
        req = _deploy_request_from_payload(payload)
        result = deploy_agent(req)
        nodes.invalidate_snapshot_cache(req.id)
        _clear_resolution_cache()
        return result
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    except AuthenticationException as e:
        raise HTTPException(401, f"ssh authentication failed for {payload.get('user')}@{payload.get('host')}") from e
    except NoValidConnectionsError as e:
        raise HTTPException(502, f"ssh connection failed for {payload.get('host')}:{payload.get('ssh_port') or 22}: {e}") from e
    except (RuntimeError, OSError, SSHException) as e:
        raise HTTPException(502, str(e)) from e


@app.post("/api/nodes/deploy/start")
def api_node_deploy_start(payload: dict = Body(...)) -> dict:
    try:
        req = _deploy_request_from_payload(payload)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    return _start_deploy_job(req)


@app.post("/api/nodes/deploy/sync-all")
def api_node_deploy_sync_all() -> dict:
    jobs: list[dict[str, Any]] = []
    skipped: list[dict[str, str]] = []
    for node in nodes.load_config(force=True).nodes:
        if node.kind != "ssh":
            skipped.append({"id": node.id, "reason": "not an SSH node"})
            continue
        try:
            req = _deploy_request_from_node(node)
        except (ValueError, TypeError) as e:
            skipped.append({"id": node.id or "", "reason": str(e)})
            continue
        job = _start_deploy_job(req)
        job["node_id"] = node.id
        job["node_name"] = node.display_name
        jobs.append(job)
    _clear_resolution_cache()
    return {
        "ok": True,
        "jobs": jobs,
        "skipped": skipped,
    }


@app.get("/api/nodes/deploy/jobs/{job_id}")
def api_node_deploy_job(job_id: str) -> dict:
    job = _get_deploy_job(job_id)
    if job is None:
        raise HTTPException(404, f"deploy job not found: {job_id}")
    return _deploy_job_payload(job)


@app.post("/api/nodes/{node_id}/launch")
def api_node_launch(node_id: str, payload: dict = Body(...)) -> dict:
    node = _configured_node(node_id)
    launch_payload = dict(payload)
    platform = str(launch_payload.get("platform", "")).strip().lower()
    if platform == "bash":
        launch_payload["display_name"] = "Bash"
    elif not _clean_display_name(launch_payload.get("display_name") or launch_payload.get("name")):
        launch_payload["display_name"] = node.display_name
    if node.kind == "local":
        return agent_launch(launch_payload, authorization=None)
    data = nodes.forward(node_id, "POST", "/agent/v1/launch", launch_payload)
    if platform == "bash" and _is_legacy_agent_bash_platform_error(data):
        fallback_payload = dict(launch_payload)
        fallback_payload["platform"] = "claude"
        fallback_payload["display_name"] = "Bash"
        fallback_payload["name"] = "Bash"
        data = nodes.forward(node_id, "POST", "/agent/v1/launch", fallback_payload)
    if data.get("ok"):
        nodes.invalidate_snapshot_cache(node_id)
    return data


def _is_legacy_agent_bash_platform_error(data: dict) -> bool:
    if data.get("ok", True):
        return False
    error = str(data.get("error") or data.get("detail") or "")
    return "platform must be claude or codex" in error


@app.get("/api/nodes/{node_id}/windows/{platform}/{pid}/timeline")
def api_node_window_timeline(node_id: str, platform: str, pid: int, limit: int = 2000) -> dict:
    node = _configured_node(node_id)
    if node.kind == "local":
        return _local_window_timeline(platform, pid, limit=limit)
    data = nodes.forward(node_id, "GET", f"/agent/v1/windows/{platform}/{pid}/timeline?limit={limit}")
    if not data.get("ok", True) and data.get("error"):
        raise HTTPException(502, data["error"])
    return data


@app.get("/api/nodes/{node_id}/windows/{pid}/plan")
def api_node_window_plan(node_id: str, pid: int) -> dict:
    node = _configured_node(node_id)
    if node.kind == "local":
        return api_plan(pid)
    data = nodes.forward(node_id, "GET", f"/agent/v1/windows/{pid}/plan")
    if not data.get("ok", True) and data.get("error"):
        raise HTTPException(502, data["error"])
    return data


@app.post("/api/nodes/{node_id}/windows/{platform}/{pid}/focus")
def api_node_focus(node_id: str, platform: str, pid: int) -> dict:
    node = _configured_node(node_id)
    if node.kind == "local":
        return _local_window_focus(platform, pid)
    data = nodes.forward(node_id, "POST", f"/agent/v1/windows/{platform}/{pid}/focus")
    attach_command = data.get("attach_command")
    if attach_command:
        data["ssh_command"] = _ssh_attach_command(node, attach_command)
    return data


@app.post("/api/nodes/{node_id}/windows/{platform}/{pid}/rename")
def api_node_rename(node_id: str, platform: str, pid: int, payload: dict = Body(...)) -> dict:
    node = _configured_node(node_id)
    if node.kind == "local":
        data = _local_window_rename(platform, pid, payload)
    else:
        data = nodes.forward(node_id, "POST", f"/agent/v1/windows/{platform}/{pid}/rename", payload)
    if data.get("ok"):
        nodes.invalidate_snapshot_cache(node_id)
        _refresh_snapshot_cache()
    return data


def _local_window_complete(platform: str, pid: int) -> dict:
    return registry.set_completed(platform, pid)


def _local_window_uncomplete(platform: str, pid: int) -> dict:
    return registry.unset_completed(platform, pid)


@app.post("/api/nodes/{node_id}/windows/{platform}/{pid}/complete")
def api_node_complete(node_id: str, platform: str, pid: int) -> dict:
    node = _configured_node(node_id)
    if node.kind == "local":
        data = _local_window_complete(platform, pid)
    else:
        data = nodes.mark_completed(node_id, platform, pid)
    if data.get("ok"):
        nodes.invalidate_snapshot_cache(node_id)
        _refresh_snapshot_cache()
    return data


@app.post("/api/nodes/{node_id}/windows/{platform}/{pid}/uncomplete")
def api_node_uncomplete(node_id: str, platform: str, pid: int) -> dict:
    node = _configured_node(node_id)
    if node.kind == "local":
        data = _local_window_uncomplete(platform, pid)
    else:
        data = nodes.unmark_completed(node_id, platform, pid)
    if data.get("ok"):
        nodes.invalidate_snapshot_cache(node_id)
        _refresh_snapshot_cache()
    return data


@app.post("/agent/v1/windows/{platform}/{pid}/complete")
def agent_window_complete(platform: str, pid: int, authorization: str | None = Header(None)) -> dict:
    _check_agent_token(authorization)
    return _local_window_complete(platform, pid)


@app.post("/agent/v1/windows/{platform}/{pid}/uncomplete")
def agent_window_uncomplete(platform: str, pid: int, authorization: str | None = Header(None)) -> dict:
    _check_agent_token(authorization)
    return _local_window_uncomplete(platform, pid)


@app.post("/api/nodes/{node_id}/windows/{platform}/{pid}/close")
def api_node_close(node_id: str, platform: str, pid: int) -> dict:
    node = _configured_node(node_id)
    if node.kind == "local":
        data = _local_window_close(platform, pid)
    else:
        data = nodes.forward(node_id, "POST", f"/agent/v1/windows/{platform}/{pid}/close")
    if data.get("ok"):
        nodes.invalidate_snapshot_cache(node_id)
        _refresh_snapshot_cache()
    return data


@app.get("/api/nodes/{node_id}/windows/{platform}/{pid}/terminal")
def api_node_terminal(node_id: str, platform: str, pid: int, lines: int = 200) -> dict:
    node = _configured_node(node_id)
    if node.kind == "local":
        return _local_window_terminal(platform, pid, lines=lines)
    return nodes.forward(node_id, "GET", f"/agent/v1/windows/{platform}/{pid}/terminal?lines={lines}")


@app.post("/api/nodes/{node_id}/windows/{platform}/{pid}/terminal/input")
def api_node_terminal_input(node_id: str, platform: str, pid: int, payload: dict = Body(...)) -> dict:
    node = _configured_node(node_id)
    if node.kind == "local":
        return _local_window_terminal_input(platform, pid, payload)
    data = nodes.forward(node_id, "POST", f"/agent/v1/windows/{platform}/{pid}/terminal/input", payload)
    if data.get("ok"):
        nodes.invalidate_snapshot_cache(node_id)
    return data


@app.websocket("/api/nodes/{node_id}/windows/{platform}/{pid}/terminal/ws")
async def api_node_terminal_ws(websocket: WebSocket, node_id: str, platform: str, pid: int, cols: int = 80, rows: int = 24) -> None:
    node = nodes.node_by_id(node_id)
    if not node:
        await websocket.accept()
        await websocket.send_text(f"\r\n[LUCID] unknown node {node_id}\r\n")
        await websocket.close(code=1008)
        return
    if node.kind == "local":
        await _local_terminal_ws(websocket, platform, pid, cols=cols, rows=rows)
        return
    nodes.ensure_tunnels()
    await _proxy_terminal_ws(websocket, node, platform, pid, cols, rows)


@app.post("/api/nodes/{node_id}/windows/{platform}/{pid}/review")
def api_node_review(node_id: str, platform: str, pid: int) -> dict:
    node = _configured_node(node_id)
    if node.kind == "local":
        if platform != "claude":
            return {"ok": False, "error": "review is only supported for Claude windows"}
        return actions.review_session_start(pid)
    return nodes.forward(node_id, "POST", f"/agent/v1/windows/{platform}/{pid}/review")


@app.get("/api/nodes/{node_id}/windows/{platform}/{pid}/review")
def api_node_review_result(node_id: str, platform: str, pid: int) -> dict:
    node = _configured_node(node_id)
    if node.kind == "local":
        if platform != "claude":
            return {"status": "error", "error": "review is only supported for Claude windows"}
        return actions.review_session_result(pid)
    return nodes.forward(node_id, "GET", f"/agent/v1/windows/{platform}/{pid}/review")


@app.get("/api/nodes/{node_id}/sessions/{platform}/{session_id}/timeline")
def api_node_session_timeline(node_id: str, platform: str, session_id: str, limit: int = 2000) -> dict:
    node = _configured_node(node_id)
    if node.kind == "local":
        try:
            data = localstate.timeline_for_session(platform, session_id, limit=limit)
        except FileNotFoundError as e:
            raise HTTPException(404, "transcript not found") from e
    else:
        data = nodes.forward(node_id, "GET", f"/agent/v1/sessions/{platform}/{session_id}/timeline?limit={limit}")
        if not data.get("ok", True) and data.get("error"):
            raise HTTPException(502, data["error"])
    data["node_id"] = node.id
    data["node_name"] = node.display_name
    return data


@app.get("/api/nodes/{node_id}/memory/{name}")
def api_node_memory_detail(node_id: str, name: str) -> dict:
    node = _configured_node(node_id)
    if node.kind == "local":
        try:
            data = localstate.memory_detail(name)
        except FileNotFoundError as e:
            raise HTTPException(404, "memory not found") from e
    else:
        data = nodes.forward(node_id, "GET", f"/agent/v1/memory/{name}")
        if not data.get("ok", True) and data.get("error"):
            raise HTTPException(502, data["error"])
    data["node_id"] = node.id
    data["node_name"] = node.display_name
    return data


@app.post("/api/nodes/{node_id}/sessions/{platform}/{session_id}/resume")
def api_node_session_resume(node_id: str, platform: str, session_id: str) -> dict:
    node = _configured_node(node_id)
    if node.kind == "local":
        return _agent_resume_or_fork(platform, session_id, fork=False)
    data = nodes.forward(node_id, "POST", f"/agent/v1/sessions/{platform}/{session_id}/resume")
    if data.get("ok"):
        nodes.invalidate_snapshot_cache(node_id)
    return data


@app.post("/api/nodes/{node_id}/sessions/{platform}/{session_id}/fork")
def api_node_session_fork(node_id: str, platform: str, session_id: str) -> dict:
    node = _configured_node(node_id)
    if node.kind == "local":
        return _agent_resume_or_fork(platform, session_id, fork=True)
    data = nodes.forward(node_id, "POST", f"/agent/v1/sessions/{platform}/{session_id}/fork")
    if data.get("ok"):
        nodes.invalidate_snapshot_cache(node_id)
    return data


@app.get("/agent/v1/health")
def agent_health() -> dict:
    return {"ok": True, "mode": _mode(), "node": localstate.local_node_info(_local_node_id())}


@app.get("/agent/v1/snapshot")
def agent_snapshot(authorization: str | None = Header(None)) -> dict:
    _require_agent_auth(authorization)
    return localstate.wire_snapshot(_local_node_id(), _local_node_id())


@app.get("/agent/v1/history")
def agent_history(q: str = "", page: int = 1, limit: int = 30, authorization: str | None = Header(None)) -> dict:
    _require_agent_auth(authorization)
    return localstate.history_sessions(q or None, page=page, limit=limit, node_id=_local_node_id())


@app.get("/agent/v1/search")
def agent_search(q: str, limit: int = 60, authorization: str | None = Header(None)) -> dict:
    _require_agent_auth(authorization)
    if not q.strip():
        return {"hits": [], "q": q, "node": localstate.local_node_info(_local_node_id())}
    return localstate.search_hits(q, limit=limit, node_id=_local_node_id())


@app.get("/agent/v1/skills")
def agent_skills(authorization: str | None = Header(None)) -> dict:
    _require_agent_auth(authorization)
    return localstate.skills_payload(_local_node_id())


@app.get("/agent/v1/memory")
def agent_memory(project: str | None = None, authorization: str | None = Header(None)) -> dict:
    _require_agent_auth(authorization)
    return localstate.memory_payload(project=project, node_id=_local_node_id())


@app.get("/agent/v1/memory/{name}")
def agent_memory_detail(name: str, authorization: str | None = Header(None)) -> dict:
    _require_agent_auth(authorization)
    try:
        data = localstate.memory_detail(name)
    except FileNotFoundError as e:
        raise HTTPException(404, "memory not found") from e
    data["node_id"] = _local_node_id()
    data["node_name"] = _local_node_id()
    return data


@app.get("/agent/v1/paths")
def agent_paths(path: str = "", authorization: str | None = Header(None)) -> dict:
    _require_agent_auth(authorization)
    return _local_path_list(path or None)


@app.post("/agent/v1/paths")
def agent_path_create(payload: dict = Body(...), authorization: str | None = Header(None)) -> dict:
    _require_agent_auth(authorization)
    return _local_path_create(payload)


@app.get("/agent/v1/files")
def agent_file_read(path: str = "", authorization: str | None = Header(None)) -> dict:
    _require_agent_auth(authorization)
    return _local_file_read(path)


@app.get("/agent/v1/files/raw")
def agent_file_raw(path: str = "", authorization: str | None = Header(None)) -> Response:
    _require_agent_auth(authorization)
    content, mime = _local_file_raw(path)
    return Response(content=content, media_type=mime,
                    headers={"Content-Disposition": "inline"})


@app.post("/agent/v1/files")
def agent_file_write(payload: dict = Body(...), authorization: str | None = Header(None)) -> dict:
    _require_agent_auth(authorization)
    return _local_file_write(payload)


@app.delete("/agent/v1/files")
def agent_file_delete(path: str = "", authorization: str | None = Header(None)) -> dict:
    _require_agent_auth(authorization)
    return _local_path_delete(path)


@app.patch("/agent/v1/paths")
def agent_path_rename(payload: dict = Body(...), authorization: str | None = Header(None)) -> dict:
    _require_agent_auth(authorization)
    return _local_path_rename(payload)


@app.patch("/agent/v1/paths/move")
def agent_path_move(payload: dict = Body(...), authorization: str | None = Header(None)) -> dict:
    _require_agent_auth(authorization)
    return _local_path_move(payload)


@app.post("/agent/v1/files/upload")
def agent_file_upload(payload: dict = Body(...), authorization: str | None = Header(None)) -> dict:
    _require_agent_auth(authorization)
    return _local_file_upload(payload)


@app.get("/agent/v1/windows/{platform}/{pid}/timeline")
def agent_window_timeline(platform: str, pid: int, limit: int = 2000, authorization: str | None = Header(None)) -> dict:
    _require_agent_auth(authorization)
    return _local_window_timeline(platform, pid, limit=limit)


@app.get("/agent/v1/windows/{pid}/plan")
def agent_window_plan(pid: int, authorization: str | None = Header(None)) -> dict:
    _require_agent_auth(authorization)
    return _local_window_plan("claude", pid)


@app.post("/agent/v1/windows/{platform}/{pid}/focus")
def agent_window_focus(platform: str, pid: int, authorization: str | None = Header(None)) -> dict:
    _require_agent_auth(authorization)
    return _local_window_focus(platform, pid)


@app.post("/agent/v1/windows/{platform}/{pid}/close")
def agent_window_close(platform: str, pid: int, authorization: str | None = Header(None)) -> dict:
    _require_agent_auth(authorization)
    return _local_window_close(platform, pid)


@app.post("/agent/v1/windows/{platform}/{pid}/rename")
def agent_window_rename(platform: str, pid: int, payload: dict = Body(...), authorization: str | None = Header(None)) -> dict:
    _require_agent_auth(authorization)
    return _local_window_rename(platform, pid, payload)


@app.get("/agent/v1/windows/{platform}/{pid}/terminal")
def agent_window_terminal(platform: str, pid: int, lines: int = 200, authorization: str | None = Header(None)) -> dict:
    _require_agent_auth(authorization)
    return _local_window_terminal(platform, pid, lines=lines)


@app.post("/agent/v1/windows/{platform}/{pid}/terminal/input")
def agent_window_terminal_input(platform: str, pid: int, payload: dict = Body(...), authorization: str | None = Header(None)) -> dict:
    _require_agent_auth(authorization)
    return _local_window_terminal_input(platform, pid, payload)


@app.websocket("/agent/v1/windows/{platform}/{pid}/terminal/ws")
async def agent_window_terminal_ws(websocket: WebSocket, platform: str, pid: int, cols: int = 80, rows: int = 24) -> None:
    try:
        _require_agent_auth(websocket.headers.get("authorization"))
    except HTTPException:
        await websocket.close(code=1008)
        return
    await _local_terminal_ws(websocket, platform, pid, cols=cols, rows=rows)


@app.post("/agent/v1/windows/{platform}/{pid}/review")
def agent_window_review(platform: str, pid: int, authorization: str | None = Header(None)) -> dict:
    _require_agent_auth(authorization)
    if platform != "claude":
        return {"ok": False, "error": "review is only supported for Claude windows"}
    return actions.review_session_start(pid)


@app.get("/agent/v1/windows/{platform}/{pid}/review")
def agent_window_review_result(platform: str, pid: int, authorization: str | None = Header(None)) -> dict:
    _require_agent_auth(authorization)
    if platform != "claude":
        return {"status": "error", "error": "review is only supported for Claude windows"}
    return actions.review_session_result(pid)


@app.get("/agent/v1/sessions/{platform}/{session_id}/timeline")
def agent_session_timeline(platform: str, session_id: str, limit: int = 2000, authorization: str | None = Header(None)) -> dict:
    _require_agent_auth(authorization)
    try:
        data = localstate.timeline_for_session(platform, session_id, limit=limit)
    except FileNotFoundError as e:
        raise HTTPException(404, "transcript not found") from e
    data["node_id"] = _local_node_id()
    data["node_name"] = _local_node_id()
    return data


@app.post("/agent/v1/sessions/{platform}/{session_id}/resume")
def agent_session_resume(platform: str, session_id: str, authorization: str | None = Header(None)) -> dict:
    _require_agent_auth(authorization)
    return _agent_resume_or_fork(platform, session_id, fork=False)


@app.post("/agent/v1/sessions/{platform}/{session_id}/fork")
def agent_session_fork(platform: str, session_id: str, authorization: str | None = Header(None)) -> dict:
    _require_agent_auth(authorization)
    return _agent_resume_or_fork(platform, session_id, fork=True)


@app.post("/agent/v1/launch")
def agent_launch(payload: dict = Body(...), authorization: str | None = Header(None)) -> dict:
    _require_agent_auth(authorization)
    platform = str(payload.get("platform", "")).strip().lower()
    if platform not in runner.MANAGED_PLATFORMS:
        raise HTTPException(400, "platform must be claude, codex, or bash")
    command = _coerce_command(payload)
    cwd = str(payload.get("cwd") or Path.home())
    cwd_path = Path(cwd).expanduser()
    if not cwd_path.exists():
        raise HTTPException(400, f"cwd does not exist: {cwd}")
    if not cwd_path.is_dir():
        raise HTTPException(400, f"cwd is not a directory: {cwd}")
    if platform == "bash":
        resolved_command = ["bash", "-l"]
    else:
        # Wrap in bash -li so login+interactive shell sources both
        # .bash_profile and .bashrc, picking up nvm/bun PATH entries.
        cmd_str = "exec " + " ".join(shlex.quote(a) for a in command)
        resolved_command = ["bash", "-l", "-i", "-c", cmd_str]
    display_name = _clean_display_name(payload.get("display_name") or payload.get("name"))
    if payload.get("tmux", True):
        return runner.launch_tmux(platform, resolved_command, cwd=str(cwd_path), display_name=display_name)
    raise HTTPException(400, "non-tmux launch is not supported by the HTTP agent")


@app.get("/api/windows/{pid}/timeline")
def api_timeline(pid: int, limit: int = 2000) -> dict:
    return _local_window_timeline("claude", pid, limit=limit)


@app.get("/api/windows/{pid}/plan")
def api_plan(pid: int) -> dict:
    return _local_window_plan("claude", pid)


@app.get("/api/search")
def api_search(q: str, limit: int = 60) -> dict:
    if not q.strip():
        return {"hits": [], "q": q}
    if _is_hub_mode():
        return nodes.aggregate_search(q, limit=limit)
    return {"hits": search.search(q, limit=limit), "q": q}


@app.get("/api/plans")
def api_plans() -> dict:
    return {"plans": plans.list_plans()}


@app.get("/api/plans/{name}")
def api_plan_by_name(name: str) -> dict:
    p = plans.read_plan_by_name(name)
    if not p:
        raise HTTPException(404, "plan not found")
    return p


@app.post("/api/windows/{pid}/focus")
def api_focus(pid: int) -> dict:
    return _local_window_focus("claude", pid)


@app.post("/api/windows/{pid}/fork")
def api_fork(pid: int) -> dict:
    row = _local_managed_claude_window(pid)
    return _agent_resume_or_fork("claude", row.get("session_id") or "", fork=True)


@app.post("/api/windows/{pid}/close")
def api_close(pid: int) -> dict:
    data = _local_window_close("claude", pid)
    if data.get("ok"):
        _refresh_snapshot_cache()
    return data


@app.post("/api/windows/{pid}/review")
def api_review(pid: int) -> dict:
    return actions.review_session_start(pid)


@app.get("/api/windows/{pid}/review")
def api_review_result(pid: int) -> dict:
    return actions.review_session_result(pid)


@app.get("/api/history")
def api_history(q: str = "", page: int = 1, limit: int = 30, node_id: str | None = None) -> dict:
    if _is_hub_mode():
        return nodes.aggregate_history(q=q, page=page, limit=limit, node_id=node_id)
    return history.list_sessions(q=q or None, page=page, limit=limit)


@app.get("/api/history/{session_id}/timeline")
def api_history_timeline(session_id: str, limit: int = 2000) -> dict:
    # Claude Code transcripts
    from core.terminal.sessions import PROJECTS_DIR
    for proj_dir in PROJECTS_DIR.iterdir():
        if not proj_dir.is_dir():
            continue
        f = proj_dir / f"{session_id}.jsonl"
        if f.exists():
            fp = str(f)
            events = transcripts.timeline(fp, limit=limit)
            return {
                "session_id": session_id, "project_slug": proj_dir.name,
                "events": events, "platform": "claude",
                "skills_used": transcripts.extract_skills_used(fp),
                "memory_ops": transcripts.extract_memory_ops(fp),
                "plan_history": transcripts.extract_plan_history(fp),
            }
    # Codex transcripts
    from core.conversations.codex import CODEX_SESSIONS_DIR
    if CODEX_SESSIONS_DIR.exists():
        for f in CODEX_SESSIONS_DIR.rglob("*.jsonl"):
            if session_id in f.stem:
                events = codex.codex_timeline(str(f), limit=limit)
                return {"session_id": session_id, "project_slug": "codex", "events": events, "platform": "codex"}
    raise HTTPException(404, "transcript not found")


@app.post("/api/history/{session_id}/resume")
def api_history_resume(session_id: str) -> dict:
    # If the session is alive, focus it instead of opening a new window.
    for w in localstate.wire_snapshot(_local_node_id(), _local_node_id()).get("windows", []):
        if w.get("session_id") == session_id and w.get("alive") and w.get("tty"):
            result = actions.focus_terminal(w["tty"])
            return {"ok": result.get("ok", False), "action": "focused", "session_id": session_id, "pid": w.get("pid")}
    sess = _local_history_session(session_id)
    if not sess:
        return {"ok": False, "error": "session not found in index"}
    return _agent_resume_or_fork(sess.get("platform", "claude"), session_id, fork=False)


@app.post("/api/history/{session_id}/fork")
def api_history_fork(session_id: str) -> dict:
    sess = _local_history_session(session_id)
    if not sess:
        return {"ok": False, "error": "session not found in index"}
    return _agent_resume_or_fork(sess.get("platform", "claude"), session_id, fork=True)


@app.get("/api/skills/{name}/sessions")
def api_skill_sessions(name: str) -> dict:
    """Reverse lookup: which sessions touched this skill, with per-session counts."""
    data = nodes.aggregate_history(limit=9999) if _is_hub_mode() else history.list_sessions(limit=9999)
    rows = []
    for s in data["sessions"]:
        bd = s.get("skill_breakdown", {}) or {}
        inv = (bd.get("per_skill_invokes") or {}).get(name, 0)
        rd = (bd.get("per_skill_reads") or {}).get(name, 0)
        wr = (bd.get("per_skill_writes") or {}).get(name, 0)
        bash = (bd.get("per_skill_bash_refs") or {}).get(name, 0)
        total = inv + rd + wr + bash
        if total == 0:
            continue
        rows.append({
            "session_id": s["session_id"],
            "node_id": s.get("node_id", _local_node_id()),
            "node_name": s.get("node_name", _local_node_id()),
            "session_key": s.get("session_key") or f"{s.get('node_id', _local_node_id())}:{s.get('platform', 'claude')}:{s['session_id']}",
            "project_name": s["project_name"],
            "platform": s.get("platform", "claude"),
            "title": s.get("first_input", "")[:120],
            "ts": s.get("last_ts") or s.get("first_ts") or "",
            "invoke": inv,
            "reads": rd,
            "writes": wr,
            "bash_refs": bash,
            "total": total,
        })
    rows.sort(key=lambda r: -r["total"])
    return {"name": name, "sessions": rows, "session_count": len(rows)}


@app.get("/api/memory/{name}/sessions")
def api_memory_sessions(name: str) -> dict:
    """Reverse lookup: which sessions read/wrote this memory."""
    data = nodes.aggregate_history(limit=9999) if _is_hub_mode() else history.list_sessions(limit=9999)
    rows = []
    for s in data["sessions"]:
        bd = s.get("memory_breakdown", {}) or {}
        rd = (bd.get("per_memory_reads") or {}).get(name, 0)
        wr = (bd.get("per_memory_writes") or {}).get(name, 0)
        ed = (bd.get("per_memory_edits") or {}).get(name, 0)
        total = rd + wr + ed
        if total == 0:
            continue
        rows.append({
            "session_id": s["session_id"],
            "node_id": s.get("node_id", _local_node_id()),
            "node_name": s.get("node_name", _local_node_id()),
            "session_key": s.get("session_key") or f"{s.get('node_id', _local_node_id())}:{s.get('platform', 'claude')}:{s['session_id']}",
            "project_name": s["project_name"],
            "platform": s.get("platform", "claude"),
            "title": s.get("first_input", "")[:120],
            "ts": s.get("last_ts") or s.get("first_ts") or "",
            "reads": rd,
            "writes": wr,
            "edits": ed,
            "total": total,
        })
    rows.sort(key=lambda r: -r["total"])
    return {"name": name, "sessions": rows, "session_count": len(rows)}


@app.get("/api/memory/{name}")
def api_memory_detail(name: str) -> dict:
    try:
        return localstate.memory_detail(name)
    except FileNotFoundError as e:
        raise HTTPException(404, "memory not found") from e


@app.get("/api/skills")
def api_skills() -> dict:
    if _is_hub_mode():
        return nodes.aggregate_skills()
    data = history.list_sessions(limit=9999)
    session_count: dict[str, int] = {}
    invoke_count: dict[str, int] = {}
    reads_count: dict[str, int] = {}
    writes_count: dict[str, int] = {}
    bash_refs_count: dict[str, int] = {}
    for s in data["sessions"]:
        for sk in s.get("skills_used", []):
            session_count[sk] = session_count.get(sk, 0) + 1
        # Use the per-session breakdown that history index already produced
        # for Claude and Codex sessions.
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
        s["session_count"] = session_count.get(name, 0)
        s["invoke_count"] = inv
        s["reads"] = rd
        s["writes"] = wr
        s["bash_refs"] = brefs
        s["total_activity"] = inv + rd + wr + brefs
    all_skills.sort(key=lambda s: (-s["total_activity"], -s["invoke_count"], s["name"]))
    return {"skills": all_skills}


@app.get("/api/skills/hub")
def api_skills_hub() -> dict:
    """Return the list of skill directories installed on the hub."""
    return {"skills": skills.list_all_skills()}


@app.post("/api/skills/upload")
def api_skills_upload(payload: dict = Body(...)) -> dict:
    """Upload a skill folder to the hub.

    Expected payload::

        {"name": "skill-name", "files": [{"path": "SKILL.md", "content_b64": "..."}]}
    """
    name = str(payload.get("name") or "").strip()
    if not name:
        raise HTTPException(400, "name is required")
    if ".." in name or "/" in name or "\\" in name:
        raise HTTPException(400, f"invalid skill name: {name}")

    file_list = payload.get("files") or []
    if not isinstance(file_list, list) or not file_list:
        raise HTTPException(400, "files list is required")

    skills.HUB_SKILLS_DIR.mkdir(parents=True, exist_ok=True)
    target_dir = skills.HUB_SKILLS_DIR / name

    import shutil
    if target_dir.exists():
        shutil.rmtree(target_dir, ignore_errors=True)
    target_dir.mkdir(parents=True, exist_ok=True)

    written = 0
    errors = []
    for item in file_list:
        if not isinstance(item, dict):
            continue
        rel = str(item.get("path") or "").strip()
        b64 = str(item.get("content_b64") or "")
        if not rel:
            continue
        if ".." in rel:
            errors.append(f"skipped unsafe path: {rel}")
            continue
        dest = (target_dir / rel).resolve()
        if not str(dest).startswith(str(target_dir.resolve())):
            errors.append(f"skipped path escape: {rel}")
            continue
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(base64.b64decode(b64))
            written += 1
        except Exception as e:
            errors.append(f"{rel}: {e}")

    return {"ok": True, "name": name, "dir": str(target_dir),
            "written": written, "errors": errors}


def _resolve_skill_dir(name: str, dir_hint: str | None = None) -> Path:
    """Resolve a skill directory from name or an explicit dir hint."""
    if dir_hint and str(dir_hint).strip():
        d = Path(str(dir_hint).strip())
        base = skills.HUB_SKILLS_DIR.resolve()
        resolved = d.resolve()
        if str(resolved).startswith(str(base)) and resolved.is_dir():
            return resolved
        raise HTTPException(404, f"skill directory not found: {d}")

    skill_dir = skills.HUB_SKILLS_DIR / name
    if skill_dir.is_dir():
        return skill_dir
    for d in skills.HUB_SKILLS_DIR.rglob(name):
        if d.is_dir() and (d / "SKILL.md").exists():
            return d
    raise HTTPException(404, f"skill not found: {name}")


@app.get("/api/skills/{name}/files")
def api_skills_files(name: str, dir: str | None = None) -> dict:
    """Return all files in a skill as a flat list with base64 contents."""
    if ".." in name or "/" in name or "\\" in name:
        raise HTTPException(404, f"invalid skill name: {name}")

    skill_dir = _resolve_skill_dir(name, dir)

    files = []
    for fpath in sorted(skill_dir.rglob("*")):
        if fpath.is_file():
            rel = str(fpath.relative_to(skill_dir))
            try:
                content_b64 = base64.b64encode(fpath.read_bytes()).decode("ascii")
            except Exception:
                content_b64 = ""
            files.append({"path": rel, "content_b64": content_b64, "size": fpath.stat().st_size})

    return {"ok": True, "name": name, "files": files}


@app.put("/api/skills/{name}/files")
def api_skills_file_write(name: str, payload: dict = Body(...)) -> dict:
    """Write a single file inside a skill directory on the hub."""
    if ".." in name or "/" in name or "\\" in name:
        raise HTTPException(404, f"invalid skill name: {name}")
    dir_hint = payload.get("dir")
    rel = str(payload.get("path") or "").strip()
    content = str(payload.get("content") or "")

    if not rel:
        raise HTTPException(400, "path is required")
    if ".." in rel:
        raise HTTPException(400, f"invalid file path: {rel}")

    skill_dir = _resolve_skill_dir(name, dir_hint)

    dest = (skill_dir / rel).resolve()
    if not str(dest).startswith(str(skill_dir.resolve())):
        raise HTTPException(400, f"path escape: {rel}")

    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        write_utf8(str(dest), content)
        stat = dest.stat()
    except PermissionError as e:
        raise HTTPException(403, f"file is not writable: {dest}") from e
    except OSError as e:
        raise HTTPException(400, f"file write failed: {e}") from e

    return {"ok": True, "path": str(dest), "name": dest.name,
            "size": stat.st_size, "mtime_ms": int(stat.st_mtime * 1000)}


@app.get("/api/skills/{name}/tar")
def api_skills_tar(name: str, dir: str | None = None) -> Response:
    """Download a skill as a .tar.gz file."""
    if ".." in name or "/" in name or "\\" in name:
        raise HTTPException(404, f"invalid skill name: {name}")
    skill_dir = _resolve_skill_dir(name, dir)

    import tarfile as tarfile_mod
    buf = io.BytesIO()
    with tarfile_mod.open(fileobj=buf, mode="w:gz") as tar:
        for fpath in sorted(skill_dir.rglob("*")):
            if fpath.is_file():
                arcname = f"{name}/{fpath.relative_to(skill_dir)}"
                tar.add(str(fpath), arcname=arcname)
    buf.seek(0)
    return Response(content=buf.getvalue(), media_type="application/gzip",
                    headers={"Content-Disposition": f'attachment; filename="{name}.tar.gz"'})


@app.delete("/api/skills/{name}")
def api_skills_delete(name: str, dir: str | None = None) -> dict:
    """Delete a skill directory from the hub."""
    if ".." in name or "/" in name or "\\" in name:
        raise HTTPException(404, f"invalid skill name: {name}")

    skill_dir = _resolve_skill_dir(name, dir)
    import shutil
    shutil.rmtree(skill_dir, ignore_errors=True)

    return {"ok": True, "name": name, "deleted": str(skill_dir)}


@app.post("/api/nodes/{node_id}/skills/sync")
def api_node_skills_sync(node_id: str, payload: dict = Body(...)) -> dict:
    mode = str(payload.get("mode") or "append").strip().lower()
    if mode not in ("append", "replace"):
        raise HTTPException(400, "mode must be 'append' or 'replace'")
    names = payload.get("names") or None

    node = _configured_node(node_id)
    if _is_local_enabled_node(node):
        tarball = build_skills_tarball_bytes(names)
        if not tarball:
            return {"ok": False, "error": "No skills found on hub"}
        return install_skills_to_remote(tarball, mode)

    tarball = build_skills_tarball_bytes(names)
    if not tarball:
        return {"ok": False, "error": "No skills found on hub"}

    # Send raw bytes directly (not JSON-encoded base64)
    try:
        path = f"/agent/v1/skills/sync?mode={mode}"
        return nodes.post_raw(node, path, tarball,
                              content_type="application/octet-stream", timeout_ms=60000)
    except Exception:
        pass

    # Fallback: SSH
    b64 = base64.b64encode(tarball).decode("ascii")
    return sync_skills_via_ssh(node_id, b64, mode)


@app.post("/agent/v1/skills/sync")
async def agent_skills_sync(request: Request, authorization: str | None = Header(None)) -> dict:
    """Agent-side — receives raw gzipped tarball and installs to both dirs."""
    _require_agent_auth(authorization)
    raw = await request.body()
    mode = request.query_params.get("mode", "append")
    if mode not in ("append", "replace"):
        raise HTTPException(400, "mode must be 'append' or 'replace'")
    if not raw:
        raise HTTPException(400, "request body is required")
    return install_skills_to_remote(raw, mode)


@app.get("/agent/v1/skills/raw")
def agent_skills_raw(authorization: str | None = Header(None)) -> Response:
    """Return a gzipped tarball of the agent's skill directories."""
    _require_agent_auth(authorization)
    raw = build_agent_skills_tarball_raw()
    return Response(content=raw, media_type="application/gzip")


@app.post("/api/nodes/{node_id}/skills/pull")
def api_node_skills_pull(node_id: str) -> dict:
    """Pull skills from a remote node into the hub (append-only)."""
    node = _configured_node(node_id)
    if _is_local_enabled_node(node):
        tarball_bytes = build_agent_skills_tarball_raw()
        result = install_skills_append_only(tarball_bytes)
        if result.get("ok"):
            result["node_id"] = node.id
            result["summary"] = result.get("summary") or "Pulled local skills into hub."
        return result

    tarball_bytes = None
    last_error = ""
    try:
        tarball_bytes, _ = nodes.http_raw(node, "GET", "/agent/v1/skills/raw", timeout_ms=15000)
    except Exception as e:
        last_error = f"agent API: {e}"

    if tarball_bytes:
        return install_skills_append_only(tarball_bytes)

    try:
        tarball_bytes, node_info = pull_skills_via_ssh(node_id)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(502, f"SSH pull failed: {e}")

    if not tarball_bytes:
        raise HTTPException(502, f"Failed to retrieve skills from remote node: {last_error}" if last_error else "Failed to retrieve skills from remote node")

    return install_skills_append_only(tarball_bytes)


@app.get("/api/memory")
def api_memory(project: str | None = None) -> dict:
    if _is_hub_mode():
        return nodes.aggregate_memory(project)
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
            m["read_sessions"] = read_count.get(stem, 0)
            m["write_sessions"] = write_count.get(stem, 0)
    return result


@app.get("/api/perms")
def api_perms() -> dict:
    return perms.snapshot()


@app.get("/api/events")
async def api_events(request: Request) -> EventSourceResponse:
    queue: asyncio.Queue = asyncio.Queue(maxsize=32)
    state.subscribers.add(queue)

    async def event_gen():
        # Send the current snapshot once immediately.
        snap = state.last_snapshot or _enriched_snapshot()
        yield {"event": "snapshot", "data": json.dumps(snap)}
        try:
            while True:
                if await request.is_disconnected():
                    break
                try:
                    payload = await asyncio.wait_for(queue.get(), timeout=20.0)
                    yield {"event": "snapshot", "data": payload}
                except asyncio.TimeoutError:
                    yield {"event": "heartbeat", "data": str(int(time.time()))}
        finally:
            state.subscribers.discard(queue)

    return EventSourceResponse(event_gen())
