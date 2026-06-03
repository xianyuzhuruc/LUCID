"""Hub-side node configuration, SSH tunnels, and remote agent aggregation."""
from __future__ import annotations

import json
import os
import shlex
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python 3.10 fallback
    import tomli as tomllib

from core.common.text_encoding import decode_utf8
from core.dashboard import localstate


CONFIG_PATH = Path(os.environ.get("LUCID_NODES", "~/.lucid/nodes.toml")).expanduser()
STATE_DIR = Path(os.environ.get("LUCID_STATE_DIR", "~/.lucid")).expanduser()
SSH_HISTORY_PATH = Path(
    os.environ.get("LUCID_SSH_HISTORY", str(STATE_DIR / "ssh-history.json"))
).expanduser()
SSH_HISTORY_LIMIT = 20
REMOTE_SNAPSHOT_TTL_SECONDS = int(os.environ.get("LUCID_REMOTE_SNAPSHOT_TTL_SECONDS", "0"))


@dataclass(frozen=True)
class NodeConfig:
    id: str
    kind: str = "local"
    url: str = ""
    name: str = ""
    ssh_host: str = ""
    host: str = ""
    user: str = ""
    ssh_port: int = 22
    local_port: int = 0
    agent_host: str = "127.0.0.1"
    agent_port: int = 7879
    auto_tunnel: bool = False
    auto_deploy: bool = False
    remote_dir: str = "~/.lucid/agent"
    identity_file: str = ""
    token: str = ""
    token_env: str = ""

    @property
    def display_name(self) -> str:
        return self.name or self.id

    @property
    def auth_token(self) -> str:
        if self.token_env:
            return os.environ.get(self.token_env, "")
        return self.token

    @property
    def base_url(self) -> str:
        if self.url:
            return self.url.rstrip("/")
        if self.local_port:
            return f"http://127.0.0.1:{self.local_port}"
        return f"http://{self.agent_host}:{self.agent_port}"


@dataclass(frozen=True)
class HubConfig:
    poll_interval_ms: int = 3000
    request_timeout_ms: int = 1200
    stale_after_ms: int = 8000
    offline_after_ms: int = 30000
    nodes: tuple[NodeConfig, ...] = ()


_config_cache: Optional[HubConfig] = None
_config_mtime: Optional[float] = None
_snapshot_cache: dict[str, dict] = {}
_snapshot_cache_ts: dict[str, float] = {}
_tunnel_procs: dict[str, subprocess.Popen] = {}
_tunnel_specs: dict[str, tuple[str, int, str, int, str, int]] = {}
_tunnel_errors: dict[str, str] = {}
_tunnel_lock = threading.Lock()
_DIRECT_AGENT_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _default_config() -> HubConfig:
    return HubConfig(nodes=())


def load_config(force: bool = False) -> HubConfig:
    global _config_cache, _config_mtime
    if not CONFIG_PATH.exists():
        _config_cache = _default_config()
        _config_mtime = None
        return _config_cache
    mtime = CONFIG_PATH.stat().st_mtime
    if not force and _config_cache and _config_mtime == mtime:
        return _config_cache
    with CONFIG_PATH.open("rb") as f:
        raw = tomllib.load(f)
    hub = raw.get("hub", {})
    node_rows = raw.get("nodes", [])
    configs: list[NodeConfig] = []
    for row in node_rows:
        configs.append(NodeConfig(
            id=str(row["id"]),
            kind=str(row.get("kind", "local")),
            url=str(row.get("url", "")),
            name=str(row.get("name", "")),
            ssh_host=str(row.get("ssh_host", "")),
            host=str(row.get("host", "")),
            user=str(row.get("user", "")),
            ssh_port=int(row.get("ssh_port", 22)),
            local_port=int(row.get("local_port", 0)),
            agent_host=str(row.get("agent_host", "127.0.0.1")),
            agent_port=int(row.get("agent_port", 7879)),
            auto_tunnel=bool(row.get("auto_tunnel", False)),
            auto_deploy=bool(row.get("auto_deploy", False)),
            remote_dir=str(row.get("remote_dir", "~/.lucid/agent")),
            identity_file=str(row.get("identity_file", "")),
            token=str(row.get("token", "")),
            token_env=str(row.get("token_env", "")),
        ))
    _config_cache = HubConfig(
        poll_interval_ms=int(hub.get("poll_interval_ms", 3000)),
        request_timeout_ms=int(hub.get("request_timeout_ms", 1200)),
        stale_after_ms=int(hub.get("stale_after_ms", 8000)),
        offline_after_ms=int(hub.get("offline_after_ms", 30000)),
        nodes=tuple(configs),
    )
    _config_mtime = mtime
    return _config_cache


def node_by_id(node_id: str) -> Optional[NodeConfig]:
    for node in load_config().nodes:
        if node.id == node_id:
            return node
    return None


def load_ssh_history() -> list[dict[str, Any]]:
    try:
        raw = json.loads(SSH_HISTORY_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return []
    if not isinstance(raw, list):
        return []
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in sorted(raw, key=lambda row: float(row.get("updated_at") or 0), reverse=True):
        if not isinstance(item, dict):
            continue
        row = _clean_ssh_history_row(item)
        if row is None or row["id"] in seen:
            continue
        seen.add(row["id"])
        rows.append(row)
        if len(rows) >= SSH_HISTORY_LIMIT:
            break
    return rows


def ssh_connection_history(config: Optional[HubConfig] = None) -> list[dict[str, Any]]:
    rows = load_ssh_history()
    seen = {row["id"] for row in rows}
    cfg = config or load_config(force=True)
    for node in cfg.nodes:
        if node.kind != "ssh" or node.id in seen:
            continue
        row = _clean_ssh_history_row({
            "id": node.id,
            "node_name": node.name,
            "host": node.host or node.ssh_host,
            "user": node.user,
            "ssh_port": node.ssh_port,
            "agent_port": node.agent_port,
            "local_port": node.local_port,
            "remote_dir": node.remote_dir,
            "python_command": "auto",
            "updated_at": 0,
        })
        if row is None:
            continue
        rows.append(row)
        seen.add(row["id"])
        if len(rows) >= SSH_HISTORY_LIMIT:
            break
    return rows


def record_ssh_history(row: dict[str, Any]) -> None:
    clean = _clean_ssh_history_row({**row, "updated_at": time.time()})
    if clean is None:
        return
    existing_rows = load_ssh_history()
    existing = next((item for item in existing_rows if item["id"] == clean["id"]), None)
    if row.get("forget_password"):
        clean.pop("password", None)
    elif not clean.get("password") and existing and existing.get("password"):
        clean["password"] = existing["password"]
    rows = [clean]
    rows.extend(item for item in existing_rows if item["id"] != clean["id"])
    rows = rows[:SSH_HISTORY_LIMIT]
    SSH_HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
    SSH_HISTORY_PATH.write_text(json.dumps(rows, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    SSH_HISTORY_PATH.chmod(0o600)


def _clean_ssh_history_row(row: dict[str, Any]) -> dict[str, Any] | None:
    node_id = str(row.get("id") or "").strip()
    host = str(row.get("host") or row.get("ssh_host") or "").strip()
    user = str(row.get("user") or "").strip()
    if not node_id or not host or not user:
        return None
    cleaned = {
        "id": node_id,
        "node_name": str(row.get("node_name") or row.get("name") or "").strip(),
        "host": host,
        "user": user,
        "ssh_port": _int_or_default(row.get("ssh_port"), 22),
        "agent_port": _int_or_default(row.get("agent_port"), 0),
        "local_port": _int_or_default(row.get("local_port"), 0),
        "remote_dir": str(row.get("remote_dir") or "~/.lucid/agent").strip(),
        "python_command": str(row.get("python_command") or "auto").strip() or "auto",
        "updated_at": float(row.get("updated_at") or 0),
    }
    password = str(row.get("password") or "")
    if password:
        cleaned["password"] = password
    return cleaned


def _int_or_default(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def ensure_tunnels(config: Optional[HubConfig] = None) -> None:
    cfg = config or load_config()
    for node in cfg.nodes:
        if node.kind != "ssh" or not node.auto_tunnel:
            continue
        _ensure_tunnel(node)


def tunnel_error(node_id: str) -> str:
    return _tunnel_errors.get(node_id, "")


def _ensure_tunnel(node: NodeConfig) -> None:
    if not node.local_port:
        return
    target = node.ssh_host or _ssh_target(node)
    spec = (target, int(node.ssh_port or 22), node.agent_host, int(node.agent_port), str(node.identity_file), int(node.local_port))
    with _tunnel_lock:
        proc = _tunnel_procs.get(node.id)
        if proc and proc.poll() is None:
            if _tunnel_specs.get(node.id) == spec and _local_port_open(node.local_port):
                _tunnel_errors.pop(node.id, None)
                return
            _terminate_tunnel(node.id, proc)
            proc = None
        if proc and proc.poll() is not None:
            _tunnel_errors[node.id] = _tunnel_exit_error(proc)
            _tunnel_procs.pop(node.id, None)
            _tunnel_specs.pop(node.id, None)
        if proc is None:
            _cleanup_orphan_tunnels(node, target)
            cmd = [
                "ssh",
                "-N",
                "-L", f"127.0.0.1:{node.local_port}:{node.agent_host}:{node.agent_port}",
                "-o", "ExitOnForwardFailure=yes",
                "-o", "BatchMode=yes",
                "-o", "StrictHostKeyChecking=accept-new",
                "-o", "ConnectTimeout=10",
                "-o", "ServerAliveInterval=30",
                "-o", "ServerAliveCountMax=3",
            ]
            if node.identity_file:
                cmd.extend(["-i", str(Path(node.identity_file).expanduser())])
            if node.ssh_port:
                cmd.extend(["-p", str(node.ssh_port)])
            cmd.append(target)
            _tunnel_procs[node.id] = subprocess.Popen(
                cmd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )
            _tunnel_specs[node.id] = spec
            _wait_for_tunnel(node)


def _terminate_tunnel(node_id: str, proc: subprocess.Popen) -> None:
    try:
        proc.terminate()
        proc.wait(timeout=2)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=2)
    except OSError:
        pass
    finally:
        _tunnel_procs.pop(node_id, None)
        _tunnel_specs.pop(node_id, None)
        _tunnel_errors.pop(node_id, None)


def _cleanup_orphan_tunnels(node: NodeConfig, target: str) -> None:
    identity = str(Path(node.identity_file).expanduser()) if node.identity_file else ""
    if not identity:
        return
    try:
        result = subprocess.run(["ps", "-eo", "pid=,args="], capture_output=True, timeout=2)
    except (OSError, subprocess.TimeoutExpired):
        return
    pids: list[int] = []
    for line in decode_utf8(result.stdout).splitlines():
        try:
            pid_text, args = line.strip().split(None, 1)
            pid = int(pid_text)
        except (ValueError, IndexError):
            continue
        if _is_matching_orphan_tunnel(args, node, target, identity):
            pids.append(pid)
    for pid in pids:
        try:
            os.kill(pid, 15)
        except OSError:
            pass
    deadline = time.time() + 1.0
    while pids and time.time() < deadline:
        pids = [pid for pid in pids if _pid_exists(pid)]
        if pids:
            time.sleep(0.1)
    for pid in pids:
        try:
            os.kill(pid, 9)
        except OSError:
            pass


def _is_matching_orphan_tunnel(args: str, node: NodeConfig, target: str, identity: str) -> bool:
    try:
        tokens = shlex.split(args)
    except ValueError:
        return False
    if not tokens or Path(tokens[0]).name != "ssh":
        return False
    if "-N" not in tokens or target not in tokens or identity not in tokens:
        return False
    if node.ssh_port and not _tokens_have_value(tokens, "-p", str(node.ssh_port)):
        return False
    agent_marker = f":{node.agent_host}:"
    for index, token in enumerate(tokens):
        if token == "-L" and index + 1 < len(tokens):
            forward = tokens[index + 1]
        elif token.startswith("-L") and len(token) > 2:
            forward = token[2:]
        else:
            continue
        if forward.startswith("127.0.0.1:") and agent_marker in forward:
            return True
    return False


def _tokens_have_value(tokens: list[str], flag: str, value: str) -> bool:
    for index, token in enumerate(tokens):
        if token == flag and index + 1 < len(tokens) and tokens[index + 1] == value:
            return True
    return False


def _pid_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _ssh_target(node: NodeConfig) -> str:
    if node.user and node.host:
        return f"{node.user}@{node.host}"
    if node.host:
        return node.host
    return node.ssh_host


def _local_port_open(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.2):
            return True
    except OSError:
        return False


def _wait_for_tunnel(node: NodeConfig, timeout_s: float = 3.0) -> None:
    proc = _tunnel_procs.get(node.id)
    if proc is None:
        return
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if proc.poll() is not None:
            _tunnel_errors[node.id] = _tunnel_exit_error(proc)
            return
        if _local_port_open(node.local_port):
            _tunnel_errors.pop(node.id, None)
            return
        time.sleep(0.1)
    _tunnel_errors[node.id] = (
        f"ssh tunnel did not open local port {node.local_port} "
        f"for {node.user}@{node.host or node.ssh_host}:{node.ssh_port} within {timeout_s:.0f}s"
    )


def _tunnel_exit_error(proc: subprocess.Popen) -> str:
    stderr = ""
    if proc.stderr:
        try:
            stderr = decode_utf8(proc.stderr.read())
        except Exception:
            stderr = ""
    detail = stderr.strip() or f"ssh exited with status {proc.returncode}"
    return f"ssh tunnel failed: {detail[-1000:]}"


def _http_json(node: NodeConfig, method: str, path: str, payload: Optional[dict] = None, timeout_ms: int = 1200) -> dict:
    url = node.base_url + path
    data = None
    headers = {"Accept": "application/json"}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    token = node.auth_token
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with _open_agent_request(node, req, timeout=timeout_ms / 1000) as resp:
            raw = decode_utf8(resp.read())
    except urllib.error.HTTPError as e:
        body = decode_utf8(e.read())[:2000]
        raise RuntimeError(f"node {node.id} HTTP {e.code}: {body}") from e
    except Exception as e:
        raise RuntimeError(f"node {node.id} request failed: {e}") from e
    return json.loads(raw) if raw else {}


def _open_agent_request(node: NodeConfig, req: urllib.request.Request, timeout: float) -> Any:
    if node.auto_tunnel or _is_loopback_url(node.base_url):
        return _DIRECT_AGENT_OPENER.open(req, timeout=timeout)
    return urllib.request.urlopen(req, timeout=timeout)


def _is_loopback_url(url: str) -> bool:
    host = (urllib.parse.urlparse(url).hostname or "").lower()
    return host == "localhost" or host == "::1" or host.startswith("127.")


def agent_get(node: NodeConfig, path: str, timeout_ms: int = 1200) -> dict:
    return _http_json(node, "GET", path, timeout_ms=timeout_ms)


def agent_post(node: NodeConfig, path: str, payload: Optional[dict] = None, timeout_ms: int = 5000) -> dict:
    return _http_json(node, "POST", path, payload=payload, timeout_ms=timeout_ms)


def agent_delete(node: NodeConfig, path: str, timeout_ms: int = 5000) -> dict:
    return _http_json(node, "DELETE", path, timeout_ms=timeout_ms)


def aggregate_snapshot() -> dict:
    cfg = load_config()
    ensure_tunnels(cfg)
    windows: list[dict] = []
    nodes_out: list[dict] = []
    counts = {"total": 0, "busy": 0, "waiting": 0, "idle": 0, "bash": 0}

    with ThreadPoolExecutor(max_workers=max(1, len(cfg.nodes))) as pool:
        futs = {pool.submit(_snapshot_for_node, node, cfg.request_timeout_ms): node for node in cfg.nodes}
        for fut in as_completed(futs):
            node = futs[fut]
            try:
                snap = fut.result()
            except Exception as e:
                snap = _stale_snapshot(node, str(e), cfg)
            node_windows = snap.get("windows", [])
            windows.extend(node_windows)
            node_info = snap.get("node") or {"id": node.id}
            health = snap.get("node_health", "healthy")
            nodes_out.append({
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
                "health": health,
                "error": snap.get("error"),
                "window_count": len(node_windows),
                "hostname": node_info.get("hostname", ""),
            })
            for key in counts:
                counts[key] += int((snap.get("counts") or {}).get(key, 0))

    windows.sort(key=lambda w: (
        {"waiting": 0, "stalled": 1, "working": 2, "bash": 3}.get(w.get("triage"), 4),
        w.get("node_id", ""),
        -(w.get("updated_at") or 0),
    ))
    return {
        "windows": windows,
        "counts": counts,
        "nodes": sorted(nodes_out, key=lambda n: n["id"]),
        "ts": int(time.time() * 1000),
    }


def _snapshot_for_node(node: NodeConfig, timeout_ms: int) -> dict:
    if node.kind == "local":
        snap = localstate.wire_snapshot(node.id, node.display_name)
    else:
        tunnel_error = _tunnel_errors.get(node.id)
        if tunnel_error:
            raise RuntimeError(tunnel_error)
        cached = _fresh_cached_snapshot(node.id)
        if cached is not None:
            return cached
        snap = agent_get(node, "/agent/v1/snapshot", timeout_ms=timeout_ms)
    _normalize_node_payload(node, snap, "windows")
    _snapshot_cache[node.id] = snap
    _snapshot_cache_ts[node.id] = time.time()
    snap["node_health"] = "healthy"
    return snap


def _fresh_cached_snapshot(node_id: str) -> dict | None:
    cached = _snapshot_cache.get(node_id)
    cached_ts = _snapshot_cache_ts.get(node_id, 0)
    if not cached or not cached_ts:
        return None
    if time.time() - cached_ts >= REMOTE_SNAPSHOT_TTL_SECONDS:
        return None
    snap = dict(cached)
    snap["node_health"] = "healthy"
    snap.pop("error", None)
    for w in snap.get("windows", []):
        w["node_health"] = "healthy"
    return snap


def invalidate_snapshot_cache(node_id: str | None = None) -> None:
    if node_id:
        _snapshot_cache.pop(node_id, None)
        _snapshot_cache_ts.pop(node_id, None)
        return
    _snapshot_cache.clear()
    _snapshot_cache_ts.clear()


def _stale_snapshot(node: NodeConfig, error: str, cfg: HubConfig) -> dict:
    cached = _snapshot_cache.get(node.id)
    cached_ts = _snapshot_cache_ts.get(node.id, 0)
    age_ms = int((time.time() - cached_ts) * 1000) if cached_ts else cfg.offline_after_ms + 1
    health = "stale" if cached and age_ms <= cfg.offline_after_ms else "offline"
    if not cached:
        return {
            "node": {"id": node.id, "hostname": node.host or node.ssh_host},
            "windows": [],
            "counts": {"total": 0, "busy": 0, "waiting": 0, "idle": 0, "bash": 0},
            "node_health": health,
            "error": error,
        }
    snap = dict(cached)
    snap["node_health"] = health
    snap["error"] = error
    for w in snap.get("windows", []):
        w["node_health"] = health
    return snap


def _normalize_node_payload(node: NodeConfig, payload: dict, list_key: str) -> None:
    for item in payload.get(list_key, []):
        platform = item.get("platform", "claude")
        sid = item.get("session_id", "")
        pid = item.get("pid")
        if _is_legacy_bash_window(item):
            item["route_platform"] = platform
            item["platform"] = "bash"
            display_name = str(item.get("display_name") or item.get("name") or "").strip()
            item["name"] = display_name or "Bash"
            item["display_name"] = display_name
            item["status"] = "bash"
            item["triage"] = "bash"
            item["triage_reason"] = "Bash"
            item["triage_suggestion"] = ""
            item["waiting_for"] = None
            item["permission_msg"] = None
            item["permission_ts"] = None
            item["current_task"] = ""
            item["actions"] = ["focus", "close", "rename", "terminal"]
            platform = "bash"
        item["node_id"] = node.id
        item["node_name"] = node.display_name
        if platform == "codex" and not item.get("display_name") and str(item.get("name") or "").startswith("codex-"):
            item["name"] = node.display_name
        if pid is not None:
            item["window_key"] = item.get("window_key") or f"{node.id}:{platform}:{pid}"
        item["session_key"] = item.get("session_key") or f"{node.id}:{platform}:{sid or 'unknown'}"
        item["node_health"] = item.get("node_health") or "healthy"


def _is_legacy_bash_window(item: dict) -> bool:
    if item.get("platform", "claude") != "claude":
        return False
    return str(item.get("current_task") or "").strip() == "bash -l"


def aggregate_history(q: str = "", page: int = 1, limit: int = 30) -> dict:
    cfg = load_config()
    ensure_tunnels(cfg)
    rows: list[dict] = []
    errors: list[dict] = []
    query = urllib.parse.urlencode({"q": q, "page": 1, "limit": 9999})
    with ThreadPoolExecutor(max_workers=max(1, len(cfg.nodes))) as pool:
        futs = {}
        for node in cfg.nodes:
            if node.kind == "local":
                futs[pool.submit(localstate.history_sessions, q or None, 1, 9999, node.id)] = node
            else:
                futs[pool.submit(agent_get, node, f"/agent/v1/history?{query}", cfg.request_timeout_ms)] = node
        for fut in as_completed(futs):
            node = futs[fut]
            try:
                data = fut.result()
                _normalize_node_payload(node, data, "sessions")
                rows.extend(data.get("sessions", []))
            except Exception as e:
                errors.append({"node_id": node.id, "error": str(e)})
    rows.sort(key=lambda r: r.get("transcript_mtime") or 0, reverse=True)
    total = len(rows)
    start = (page - 1) * limit
    return {"total": total, "page": page, "limit": limit, "sessions": rows[start:start + limit], "errors": errors}


def aggregate_search(q: str, limit: int = 60) -> dict:
    cfg = load_config()
    ensure_tunnels(cfg)
    hits: list[dict] = []
    errors: list[dict] = []
    query = urllib.parse.urlencode({"q": q, "limit": limit})
    with ThreadPoolExecutor(max_workers=max(1, len(cfg.nodes))) as pool:
        futs = {}
        for node in cfg.nodes:
            if node.kind == "local":
                futs[pool.submit(localstate.search_hits, q, limit, node.id)] = node
            else:
                futs[pool.submit(agent_get, node, f"/agent/v1/search?{query}", 15000)] = node
        for fut in as_completed(futs):
            node = futs[fut]
            try:
                data = fut.result()
                _normalize_node_payload(node, data, "hits")
                hits.extend(data.get("hits", []))
            except Exception as e:
                errors.append({"node_id": node.id, "error": str(e)})
    hits.sort(key=lambda h: h.get("ts") or "", reverse=True)
    return {"hits": hits[:limit], "q": q, "errors": errors}


def aggregate_skills() -> dict:
    cfg = load_config()
    ensure_tunnels(cfg)
    rows: dict[str, dict] = {}
    for node in cfg.nodes:
        try:
            data = localstate.skills_payload(node.id) if node.kind == "local" else agent_get(node, "/agent/v1/skills", cfg.request_timeout_ms)
        except Exception:
            continue
        for sk in data.get("skills", []):
            name = sk["name"]
            row = rows.get(name)
            if row is None:
                row = dict(sk)
                for key in ("session_count", "invoke_count", "reads", "writes", "bash_refs", "total_activity"):
                    row[key] = 0
                row["nodes"] = []
                rows[name] = row
            for key in ("session_count", "invoke_count", "reads", "writes", "bash_refs", "total_activity"):
                row[key] = int(row.get(key, 0)) + int(sk.get(key, 0))
            row["nodes"].append({"node_id": node.id, "total_activity": sk.get("total_activity", 0)})
    skills = list(rows.values())
    skills.sort(key=lambda s: (-s.get("total_activity", 0), -s.get("invoke_count", 0), s["name"]))
    return {"skills": skills}


def aggregate_memory(project: str | None = None) -> dict:
    cfg = load_config()
    ensure_tunnels(cfg)
    groups: dict[str, list[dict]] = {}
    total = 0
    for node in cfg.nodes:
        try:
            path = "/agent/v1/memory" + (f"?project={urllib.parse.quote(project)}" if project else "")
            data = localstate.memory_payload(project, node.id) if node.kind == "local" else agent_get(node, path, cfg.request_timeout_ms)
        except Exception:
            continue
        for typ, mems in (data.get("groups") or {}).items():
            for m in mems:
                m["node_id"] = m.get("node_id") or node.id
                m["node_name"] = node.display_name
                groups.setdefault(typ, []).append(m)
                total += 1
    return {"groups": groups, "total": total}


def forward(node_id: str, method: str, path: str, payload: Optional[dict] = None) -> dict:
    node = node_by_id(node_id)
    if not node:
        return {"ok": False, "error": f"unknown node {node_id}", "node_id": node_id}
    if node.kind == "local":
        return {"ok": False, "error": "local forwarding path is not implemented for this action", "node_id": node_id}
    try:
        timeout_ms = 15000 if "/timeline" in path else 5000
        if method == "POST":
            return agent_post(node, path, payload=payload, timeout_ms=timeout_ms)
        if method == "DELETE":
            return agent_delete(node, path, timeout_ms=timeout_ms)
        return agent_get(node, path, timeout_ms=timeout_ms)
    except Exception as e:
        return {"ok": False, "error": str(e), "node_id": node_id}


def write_node_config(node: NodeConfig) -> None:
    cfg = load_config(force=True) if CONFIG_PATH.exists() else _default_config()
    nodes = [n for n in cfg.nodes if n.id != node.id and not (n.id == "local" and node.id == "local")]
    nodes.append(node)
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    STATE_DIR.chmod(0o700)
    lines = [
        "[hub]",
        f"poll_interval_ms = {cfg.poll_interval_ms}",
        f"request_timeout_ms = {cfg.request_timeout_ms}",
        f"stale_after_ms = {cfg.stale_after_ms}",
        "",
    ]
    for n in nodes:
        lines.extend(_node_to_toml(n))
        lines.append("")
    CONFIG_PATH.write_text("\n".join(lines), encoding="utf-8")
    CONFIG_PATH.chmod(0o600)
    load_config(force=True)


def remove_node_config(node_id: str) -> dict:
    """Delete a node from the config file and clean up its SSH tunnel."""
    if not CONFIG_PATH.exists():
        return {"ok": False, "error": f"no config file at {CONFIG_PATH}", "node_id": node_id}
    cfg = load_config(force=True)
    node = next((n for n in cfg.nodes if n.id == node_id), None)
    if node is None:
        return {"ok": False, "error": f"node not found: {node_id}", "node_id": node_id}
    if node.kind == "local":
        return {"ok": False, "error": "local node cannot be deleted", "node_id": node_id}
    # Kill any active SSH tunnel for this node
    with _tunnel_lock:
        proc = _tunnel_procs.get(node_id)
        if proc:
            _terminate_tunnel(node_id, proc)
    nodes_kept = [n for n in cfg.nodes if n.id != node_id]
    lines = [
        "[hub]",
        f"poll_interval_ms = {cfg.poll_interval_ms}",
        f"request_timeout_ms = {cfg.request_timeout_ms}",
        f"stale_after_ms = {cfg.stale_after_ms}",
        "",
    ]
    for n in nodes_kept:
        lines.extend(_node_to_toml(n))
        lines.append("")
    CONFIG_PATH.write_text("\n".join(lines), encoding="utf-8")
    CONFIG_PATH.chmod(0o600)
    load_config(force=True)
    invalidate_snapshot_cache(node_id)
    _clear_ssh_history_for_node(node_id)
    return {"ok": True, "node_id": node_id, "name": node.display_name, "kind": node.kind}


def _clear_ssh_history_for_node(node_id: str) -> None:
    rows = load_ssh_history()
    filtered = [r for r in rows if r.get("id") != node_id]
    if len(filtered) == len(rows):
        return
    SSH_HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
    SSH_HISTORY_PATH.write_text(json.dumps(filtered, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    SSH_HISTORY_PATH.chmod(0o600)


def _node_to_toml(node: NodeConfig) -> list[str]:
    fields = {
        "id": node.id,
        "kind": node.kind,
        "name": node.name,
        "url": node.url,
        "ssh_host": node.ssh_host,
        "host": node.host,
        "user": node.user,
        "ssh_port": node.ssh_port,
        "local_port": node.local_port,
        "agent_host": node.agent_host,
        "agent_port": node.agent_port,
        "auto_tunnel": node.auto_tunnel,
        "auto_deploy": node.auto_deploy,
        "remote_dir": node.remote_dir,
        "identity_file": node.identity_file,
        "token": node.token,
        "token_env": node.token_env,
    }
    out = ["[[nodes]]"]
    for key, value in fields.items():
        if value in ("", 0, False):
            continue
        if isinstance(value, bool):
            out.append(f"{key} = {'true' if value else 'false'}")
        elif isinstance(value, int):
            out.append(f"{key} = {value}")
        else:
            escaped = str(value).replace("\\", "\\\\").replace('"', '\\"')
            out.append(f'{key} = "{escaped}"')
    return out
