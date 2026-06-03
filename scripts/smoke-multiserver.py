#!/usr/bin/env python3
"""Smoke-test hub/agent aggregation with two synthetic remote agents."""
from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]


def _json_get(url: str, token: str = "", timeout: float = 5.0) -> dict[str, Any]:
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, headers=headers)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", errors="replace"))


def _wait_json(url: str, token: str = "", timeout_s: float = 10.0) -> dict[str, Any]:
    deadline = time.time() + timeout_s
    last_error: Exception | None = None
    while time.time() < deadline:
        try:
            return _json_get(url, token=token, timeout=1.0)
        except Exception as e:
            last_error = e
            time.sleep(0.2)
    raise RuntimeError(f"timed out waiting for {url}: {last_error}")


def _post_json(url: str, token: str = "", timeout: float = 5.0) -> dict[str, Any]:
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=b"{}", headers=headers, method="POST")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", errors="replace"))


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")


def _project_slug(cwd: str) -> str:
    return cwd.replace("/", "-").replace("_", "-").replace(".", "-")


def _make_home(base: Path, node_id: str) -> tuple[Path, subprocess.Popen[bytes], int, str]:
    home = base / node_id
    claude = home / ".claude"
    sessions_dir = claude / "sessions"
    sessions_dir.mkdir(parents=True)

    proc = subprocess.Popen(["sleep", "120"])
    pid = int(proc.pid)
    session_id = f"{node_id}-session"
    repo_root = base / f"repo-{node_id}"
    (repo_root / "src").mkdir(parents=True)
    (repo_root / ".config").mkdir()
    cwd = str(repo_root)
    slug = _project_slug(cwd)
    now_ms = int(time.time() * 1000)
    session = {
        "pid": pid,
        "sessionId": session_id,
        "cwd": cwd,
        "name": f"{node_id}-work",
        "status": "busy",
        "waitingFor": None,
        "startedAt": now_ms - 10000,
        "updatedAt": now_ms,
        "version": "smoke",
    }
    (sessions_dir / f"{session_id}.json").write_text(json.dumps(session), encoding="utf-8")
    transcript = [
        {
            "type": "user",
            "timestamp": "2026-05-31T00:00:00Z",
            "message": {
                "role": "user",
                "content": [{"type": "text", "text": f"hello from {node_id}"}],
            },
        },
        {
            "type": "assistant",
            "timestamp": "2026-05-31T00:00:01Z",
            "message": {
                "role": "assistant",
                "model": "smoke",
                "content": [{"type": "text", "text": f"ack {node_id}"}],
                "stop_reason": "end_turn",
            },
        },
    ]
    _write_jsonl(claude / "projects" / slug / f"{session_id}.jsonl", transcript)
    _write_jsonl(claude / "history.jsonl", [{
        "sessionId": session_id,
        "display": f"hello from {node_id}",
        "timestamp": "2026-05-31T00:00:00Z",
        "project": cwd,
    }])
    memory_dir = claude / "projects" / slug / "memory"
    memory_dir.mkdir(parents=True)
    (memory_dir / "smoke-note.md").write_text(
        "---\nname: smoke-note\ndescription: smoke memory\ntype: project\n---\n"
        f"memory on {node_id}\n",
        encoding="utf-8",
    )
    return home, proc, pid, session_id


def _start_app(env: dict[str, str], port: int) -> subprocess.Popen[bytes]:
    full_env = os.environ.copy()
    full_env.update(env)
    full_env["NO_PROXY"] = "127.0.0.1,localhost"
    return subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app:app", "--host", "127.0.0.1", "--port", str(port)],
        cwd=str(ROOT),
        env=full_env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )


def _stop(proc: subprocess.Popen[bytes]) -> None:
    if proc.poll() is not None:
        return
    proc.send_signal(signal.SIGTERM)
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)


def _assert(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def _run_hub_agent_smoke(tmp: Path) -> None:
    node_a_home, sleep_a, pid_a, session_a = _make_home(tmp, "node-a")
    node_b_home, sleep_b, _pid_b, _session_b = _make_home(tmp, "node-b")
    procs: list[subprocess.Popen[bytes]] = [sleep_a, sleep_b]
    try:
        agent_a = _start_app({
            "LUCID_MODE": "agent",
            "LUCID_HOME": str(node_a_home),
            "LUCID_NODE_ID": "node-a",
            "LUCID_AGENT_TOKEN": "token-a",
        }, 18881)
        agent_b = _start_app({
            "LUCID_MODE": "agent",
            "LUCID_HOME": str(node_b_home),
            "LUCID_NODE_ID": "node-b",
            "LUCID_AGENT_TOKEN": "token-b",
        }, 18882)
        procs.extend([agent_a, agent_b])
        _wait_json("http://127.0.0.1:18881/agent/v1/health")
        _wait_json("http://127.0.0.1:18882/agent/v1/health")
        try:
            _json_get("http://127.0.0.1:18881/agent/v1/snapshot")
            raise AssertionError("agent snapshot accepted missing token")
        except urllib.error.HTTPError as e:
            _assert(e.code == 401, f"expected 401 for missing token, got {e.code}")

        config = tmp / "nodes.toml"
        config.write_text(
            """
[hub]
poll_interval_ms = 2000
request_timeout_ms = 1200
stale_after_ms = 8000
offline_after_ms = 30000

[[nodes]]
id = "node-a"
kind = "ssh"
url = "http://127.0.0.1:18881"
token = "token-a"

[[nodes]]
id = "node-b"
kind = "ssh"
url = "http://127.0.0.1:18882"
token = "token-b"
""".strip(),
            encoding="utf-8",
        )
        hub = _start_app({
            "LUCID_MODE": "hub",
            "LUCID_NODES": str(config),
            "LUCID_HOME": str(tmp / "hub-home"),
        }, 18880)
        procs.append(hub)
        _wait_json("http://127.0.0.1:18880/api/nodes")

        windows = _json_get("http://127.0.0.1:18880/api/windows")
        node_ids = {row["node_id"] for row in windows["windows"]}
        _assert(node_ids == {"node-a", "node-b"}, f"unexpected node ids: {node_ids}")
        _assert(all(row.get("window_key") for row in windows["windows"]), "missing window_key")

        history = _json_get("http://127.0.0.1:18880/api/history?limit=10")
        _assert(history["total"] == 2, f"unexpected history total: {history['total']}")
        _assert(all(row.get("session_key") for row in history["sessions"]), "missing session_key")

        search = _json_get("http://127.0.0.1:18880/api/search?q=node-a&limit=10")
        _assert(search["hits"], "expected search hit for node-a")
        _assert(search["hits"][0]["node_id"] == "node-a", "search hit lost node id")

        timeline = _json_get(
            f"http://127.0.0.1:18880/api/nodes/node-a/sessions/claude/{session_a}/timeline"
        )
        _assert(timeline["node_id"] == "node-a", "timeline lost node id")
        _assert(timeline["events"], "timeline events missing")

        memory = _json_get("http://127.0.0.1:18880/api/nodes/node-a/memory/smoke-note")
        _assert(memory["node_id"] == "node-a", "memory detail lost node id")
        _assert("memory on node-a" in memory["content"], "memory detail returned wrong content")

        path_list = _json_get(
            "http://127.0.0.1:18880/api/nodes/node-a/paths?path="
            + urllib.parse.quote(str(tmp))
        )
        _assert(path_list["path"] == str(tmp.resolve()), "path browser returned wrong path")
        _assert(
            any(row["name"] == "repo-node-a" and row["kind"] == "directory" for row in path_list["entries"]),
            "path browser did not return node-a repo directory",
        )

        close = _post_json(f"http://127.0.0.1:18880/api/nodes/node-a/windows/claude/{pid_a}/close")
        _assert(close.get("ok") is True, f"remote close failed: {close}")
    finally:
        for proc in reversed(procs):
            _stop(proc)


def _run_deploy_config_smoke(tmp: Path) -> None:
    os.environ["LUCID_STATE_DIR"] = str(tmp / "state")
    os.environ["LUCID_NODES"] = str(tmp / "nodes-deploy.toml")
    from core.hub import nodes, ssh_deploy
    from app import api_nodes

    key = tmp / "state" / "id_ed25519"
    key.parent.mkdir(parents=True, exist_ok=True)
    key.write_text("private", encoding="utf-8")
    key.with_suffix(".pub").write_text("ssh-ed25519 fake", encoding="utf-8")
    runtime_bundle = tmp / "runtime-linux-64.tar.gz"
    runtime_bundle.write_bytes(b"runtime")

    class _SftpFile:
        def __enter__(self) -> "_SftpFile":
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def write(self, _text: str) -> None:
            return None

    uploads: list[str] = []

    class _Sftp:
        def __enter__(self) -> "_Sftp":
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def putfo(self, _file: object, remote_path: str) -> None:
            uploads.append(remote_path)
            return None

        def file(self, *_args: object) -> _SftpFile:
            return _SftpFile()

    connect_calls: list[dict[str, object]] = []

    class _Channel:
        def exit_status_ready(self) -> bool:
            return True

        def recv_exit_status(self) -> int:
            return 0

        def close(self) -> None:
            return None

    class _Stream:
        channel = _Channel()

        def read(self) -> bytes:
            return b""

    class _Client:
        def set_missing_host_key_policy(self, *_args: object) -> None:
            return None

        def connect(self, **kwargs: object) -> None:
            connect_calls.append(kwargs)
            _assert(kwargs["username"] == "alice", "deploy did not use requested username")

        def open_sftp(self) -> _Sftp:
            return _Sftp()

        def exec_command(self, command: str, timeout: int | None = None) -> tuple[None, _Stream, _Stream]:
            commands.append(command)
            return None, _Stream(), _Stream()

        def close(self) -> None:
            return None

    commands: list[str] = []

    def fake_run(_client: object, command: str, check: bool = True, timeout: int = 180) -> str:
        commands.append(command)
        if "remote_path=" in command:
            return "/remote/lucid\n"
        if "import socket" in command:
            return "18888\n"
        if "/agent/v1/health" in command or "urllib.request" in command:
            return '{"ok": true}'
        return ""

    ssh_deploy.DEFAULT_KEY = key
    ssh_deploy._ensure_local_key = lambda: key
    ssh_deploy._build_archive = lambda: b"archive"
    ssh_deploy._pick_local_port = lambda: 19001
    ssh_deploy._remote_agent_platform = lambda _client: "linux-64"
    ssh_deploy._prepare_agent_runtime_bundle = lambda _platform, _progress=None: runtime_bundle
    ssh_deploy._run = fake_run
    ssh_deploy.ensure_tunnels = lambda _config: None
    ssh_deploy.agent_get = lambda _node, _path, timeout_ms=1200: {"windows": [], "counts": {}}
    ssh_deploy.paramiko.SSHClient = _Client

    _assert(ssh_deploy._tail_process_output(None) == "", "missing process output should format as empty text")
    _assert(
        ssh_deploy._tail_process_output(b"micromamba \x96 output") == r"micromamba \x96 output",
        "invalid process output bytes should be escaped",
    )
    linux_env = ssh_deploy._micromamba_download_env(tmp / "mamba-root", "linux-64")
    _assert(linux_env["CONDA_OVERRIDE_LINUX"], "linux runtime download should override __linux")
    _assert(linux_env["CONDA_OVERRIDE_GLIBC"], "linux runtime download should override __glibc")
    osx_env = ssh_deploy._micromamba_download_env(tmp / "mamba-root", "osx-arm64")
    _assert("CONDA_OVERRIDE_LINUX" not in osx_env, "non-linux runtime download should not override __linux")
    _assert("CONDA_OVERRIDE_GLIBC" not in osx_env, "non-linux runtime download should not override __glibc")
    _assert(osx_env["CONDA_OVERRIDE_OSX"], "macOS runtime download should override __osx")
    _assert(
        Path(ssh_deploy.DEPLOY_CACHE).name == "deployment_package",
        "default deploy cache should live in deployment_package",
    )
    pkgs_dir = tmp / "pkgs"
    package_dir = pkgs_dir / "https" / "conda.anaconda.org" / "conda-forge" / "linux-64"
    package_dir.mkdir(parents=True)
    (package_dir / "python-3.11.0-hfake_0.conda").write_bytes(b"package")
    extracted_dir = package_dir / "cryptography-48.0.0-py311hfake_0"
    (extracted_dir / "info").mkdir(parents=True)
    (extracted_dir / "info" / "index.json").write_text("{}", encoding="utf-8")
    pycache_dir = extracted_dir / "lib" / "python3.11" / "site-packages" / "cryptography" / "hazmat" / "asn1" / "__pycache__"
    pycache_dir.mkdir(parents=True)
    (pycache_dir / "ignored.pyc").write_bytes(b"ignored")
    package_cache_bundle = tmp / "package-cache.tar.gz"
    with tarfile.open(package_cache_bundle, "w:gz") as tar:
        package_count = ssh_deploy._add_package_cache_to_tar(tar, pkgs_dir)
    _assert(package_count == 1, "package cache bundle should include one package archive")
    with tarfile.open(package_cache_bundle, "r:gz") as tar:
        bundle_names = tar.getnames()
    _assert(any(name.endswith("python-3.11.0-hfake_0.conda") for name in bundle_names), "package archive missing from runtime bundle")
    _assert(not any("__pycache__" in name for name in bundle_names), "runtime bundle should skip extracted package caches")

    cfg = nodes.load_config(force=True)
    _assert(cfg.nodes == (), "default hub config should not enable local")

    req = ssh_deploy.DeployRequest(
        id="remote-a",
        host="10.0.0.10",
        user="alice",
        password="secret",
        ssh_port=2222,
        agent_port=0,
    )
    result = ssh_deploy.deploy_agent(req)
    _assert(result["ok"] is True, f"deploy smoke failed: {result}")
    history = nodes.load_ssh_history()
    _assert(history, "deploy should save reusable SSH history")
    _assert(history[0]["id"] == "remote-a", "SSH history should keep the node id")
    _assert(history[0]["host"] == "10.0.0.10", "SSH history should keep the host")
    _assert(history[0]["user"] == "alice", "SSH history should keep the user")
    _assert(history[0]["ssh_port"] == 2222, "SSH history should keep the SSH port")
    _assert(history[0]["password"] == "secret", "SSH history should keep the remembered password")
    nodes_payload = api_nodes()
    _assert(nodes_payload["ssh_history"][0]["id"] == "remote-a", "nodes API should expose SSH history")
    _assert(nodes_payload["ssh_history"][0]["password"] == "secret", "nodes API should expose remembered passwords")
    _assert(connect_calls[-1]["password"] == "secret", "deploy did not use requested password")
    _assert(connect_calls[-1]["look_for_keys"] is False, "password deploy should not look for local keys")
    _assert(connect_calls[-1]["allow_agent"] is False, "password deploy should not use ssh agent")
    _assert(result["python"] == "/remote/lucid/.lucid-runtime/env/bin/python", "deploy did not use bundled Python")
    _assert(result["agent_port"] == 18888, "deploy did not use auto-selected agent port")
    _assert(any("lucid-runtime-linux-64" in path for path in uploads), "deploy did not upload runtime bundle")
    _assert(any("micromamba\" create" in command for command in commands), "deploy did not install runtime offline")
    _assert(
        any("--file \"$bundle_dir/runtime-explicit.txt\"" in command for command in commands),
        "deploy runtime install should use an explicit package spec",
    )
    _assert(not any("scripts/install-system-deps.sh agent" in command for command in commands), "deploy should not run remote system dependency installer")
    _assert(not any("pip install" in command for command in commands), "deploy should not install Python packages from the remote server")
    cfg = nodes.load_config(force=True)
    remote = next(row for row in cfg.nodes if row.id == "remote-a")
    _assert(remote.auto_tunnel is True, "deploy did not enable tunnel")
    _assert(remote.token, "deploy did not persist agent token")
    _assert(remote.local_port == 19001, "deploy did not persist local port")
    _assert(remote.agent_port == 18888, "deploy did not persist selected agent port")
    _assert(all(row.id != "local" for row in cfg.nodes), "remote deploy should not add local")

    key_req = ssh_deploy.DeployRequest(
        id="remote-key",
        host="10.0.0.11",
        user="alice",
        ssh_port=2222,
        agent_port=0,
    )
    key_result = ssh_deploy.deploy_agent(key_req)
    _assert(key_result["ok"] is True, f"key deploy smoke failed: {key_result}")
    _assert("password" not in connect_calls[-1], "key deploy should not pass an empty password")
    _assert(connect_calls[-1]["look_for_keys"] is True, "key deploy should look for local keys")
    _assert(connect_calls[-1]["allow_agent"] is True, "key deploy should use ssh agent")

    commands.clear()
    ssh_deploy._install_remote_runtime_bundle(object(), "/remote/lucid", "/tmp/runtime.tar.gz")
    runtime_install_command = commands[-1]
    _assert(
        "runtime_dir_input=" not in runtime_install_command,
        "runtime installer should receive an already-expanded remote directory",
    )

    def fail_local_key() -> Path:
        raise RuntimeError("local key failure")

    ssh_deploy._ensure_local_key = fail_local_key
    failed_req = ssh_deploy.DeployRequest(
        id="remote-failed",
        host="10.0.0.12",
        user="alice",
        password="bad-secret",
        ssh_port=2200,
        remote_dir="~/custom-agent",
    )
    try:
        ssh_deploy.deploy_agent(failed_req)
        raise AssertionError("deploy should fail after saving SSH history")
    except RuntimeError as e:
        _assert("local key failure" in str(e), f"unexpected deploy failure: {e}")
    history = nodes.load_ssh_history()
    _assert(history[0]["id"] == "remote-failed", "failed deploy should still save SSH history")
    _assert(history[0]["remote_dir"] == "~/custom-agent", "failed deploy history should keep remote dir")
    _assert(history[0]["password"] == "bad-secret", "failed deploy history should keep the remembered password")


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="lucid-smoke-") as tmp_dir:
        tmp = Path(tmp_dir)
        _run_hub_agent_smoke(tmp)
        _run_deploy_config_smoke(tmp)
    print("multi-server smoke passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
