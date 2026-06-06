"""Sync hub skills to remote agent nodes via SSH or the agent API."""
from __future__ import annotations

import base64
import io
import json
import tarfile
from pathlib import Path

import paramiko

from core.common.text_encoding import BYTE_ERROR_ESCAPE, decode_utf8
from core.hub.nodes import (
    STATE_DIR,
    SSH_HISTORY_PATH,
    NodeConfig,
    load_config,
)
from core.terminal.sessions import CLAUDE_HOME, HOME_BASE

SKILLS_DIR = CLAUDE_HOME / "skills"
CODEX_SKILLS_DIR = HOME_BASE / ".codex" / "skills"


def _load_raw_ssh_history() -> list[dict]:
    """Load raw SSH history entries (including passwords)."""
    try:
        raw = json.loads(SSH_HISTORY_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return []
    if not isinstance(raw, list):
        return []
    return [item for item in raw if isinstance(item, dict)]


def _ssh_connect(node: NodeConfig, password: str = "") -> paramiko.SSHClient:
    """Connect to a remote node via SSH using stored config + password."""
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    kwargs: dict = {
        "hostname": node.host or node.ssh_host,
        "port": node.ssh_port or 22,
        "username": node.user,
        "timeout": 20,
    }
    if password:
        kwargs["password"] = password
        kwargs["look_for_keys"] = False
        kwargs["allow_agent"] = False
    else:
        identity_file = Path(node.identity_file).expanduser() if node.identity_file else (STATE_DIR / "id_ed25519")
        if identity_file.exists():
            kwargs["key_filename"] = str(identity_file)
        kwargs["look_for_keys"] = True
        kwargs["allow_agent"] = True
    client.connect(**kwargs)
    return client


def _run(client: paramiko.SSHClient, command: str, timeout: int = 120) -> str:
    """Execute a remote command and return combined stdout+stderr."""
    stdin, stdout, stderr = client.exec_command(command, timeout=timeout)
    rc = stdout.channel.recv_exit_status()
    out = decode_utf8(stdout.read(), errors=BYTE_ERROR_ESCAPE)
    err = decode_utf8(stderr.read(), errors=BYTE_ERROR_ESCAPE)
    if rc != 0:
        raise RuntimeError(f"remote command failed rc={rc}: {command}\nstdout={out[-2000:]}\nstderr={err[-2000:]}")
    return out + err


def build_skills_tarball_b64() -> str:
    """Create a base64-encoded gzipped tarball of the hub's skill directories.

    Includes ``~/.claude/skills/*`` and ``~/.codex/skills/*`` (skipping
    dot-prefixed entries).
    """
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for skills_dir in (SKILLS_DIR, CODEX_SKILLS_DIR):
            if not skills_dir.exists():
                continue
            for item in sorted(skills_dir.iterdir()):
                # Skip common hidden files/dirs, but keep .system skills
                if item.name.startswith(".") and item.name not in (".system",):
                    continue
                arcname = f"{skills_dir.name}/{item.name}"
                if item.is_dir():
                    tar.add(item, arcname=arcname, recursive=True)
                elif item.is_file():
                    tar.add(item, arcname=arcname)
    return base64.b64encode(buf.getvalue()).decode("ascii")


def install_skills_from_b64(tarball_b64: str, mode: str) -> dict:
    """Extract a base64 skills tarball to the local filesystem.

    Args:
        tarball_b64: base64-encoded gzipped tarball produced by
            :func:`build_skills_tarball_b64`.
        mode: ``"append"`` or ``"replace"``.

    Returns a result dict.
    """
    if mode not in ("append", "replace"):
        return {"ok": False, "error": f"Invalid mode: {mode}"}

    try:
        raw = base64.b64decode(tarball_b64)
    except Exception as e:
        return {"ok": False, "error": f"Invalid tarball base64: {e}"}

    if not raw:
        return {"ok": False, "error": "Empty tarball"}

    # Prepare directories
    claude_skills = SKILLS_DIR
    codex_skills = CODEX_SKILLS_DIR

    if mode == "replace":
        import shutil
        for d in (claude_skills, codex_skills):
            if d.exists():
                shutil.rmtree(d, ignore_errors=True)

    claude_skills.mkdir(parents=True, exist_ok=True)
    codex_skills.mkdir(parents=True, exist_ok=True)

    # Extract — tar entries are named "skills/<skill_name>" or "skills.codex/<skill_name>"
    # We need to map "skills/" → ~/.claude/skills/ and "skills.codex/" → ~/.codex/skills/
    try:
        with tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz") as tar:
            for member in tar.getmembers():
                parts = member.name.split("/", 1)
                if len(parts) < 2:
                    continue
                dir_tag, rest = parts[0], parts[1]
                if not rest:
                    continue

                if dir_tag == "skills":
                    target_dir = claude_skills
                elif dir_tag == "skills.codex":
                    target_dir = codex_skills
                else:
                    continue

                # Rewrite member name to strip the dir_tag prefix
                member.name = rest
                # Resolve target path safely
                target_path = (target_dir / rest).resolve()
                if not str(target_path).startswith(str(target_dir.resolve())):
                    continue  # safety: skip paths that escape the skills dir
                tar.extract(member, path=str(target_dir), set_attrs=False)
    except Exception as e:
        return {"ok": False, "error": f"Tarball extraction failed: {e}"}

    synced = []
    if claude_skills.exists():
        synced.append(str(claude_skills))
    if codex_skills.exists():
        synced.append(str(codex_skills))

    return {
        "ok": True,
        "mode": mode,
        "synced_dirs": synced,
        "summary": f"{'Replaced' if mode == 'replace' else 'Appended'} hub skills → ~/.claude/skills/ and ~/.codex/skills/",
    }


def sync_skills_via_ssh(node_id: str, tarball_b64: str, mode: str) -> dict:
    """Sync skills to an SSH-configured node via direct SSH (no agent required)."""
    cfg = load_config()
    node = None
    for n in cfg.nodes:
        if n.id == node_id:
            node = n
            break
    if node is None:
        return {"ok": False, "error": f"unknown node: {node_id}"}

    password = ""
    for entry in _load_raw_ssh_history():
        if entry.get("id") == node_id:
            password = str(entry.get("password") or "")
            break

    try:
        raw = base64.b64decode(tarball_b64)
    except Exception as e:
        return {"ok": False, "error": f"Invalid tarball: {e}"}

    try:
        client = _ssh_connect(node, password)
    except Exception as e:
        return {"ok": False, "error": f"SSH connection failed: {e}"}

    try:
        remote_tmp = "/tmp/lucid-skills-sync.tar.gz"
        with client.open_sftp() as sftp:
            sftp.putfo(io.BytesIO(raw), remote_tmp)

        claude_skills_remote = "$HOME/.claude/skills"
        codex_skills_remote = "$HOME/.codex/skills"

        if mode == "replace":
            _run(client, f"rm -rf {claude_skills_remote} {codex_skills_remote}")
            _run(client, f"mkdir -p {claude_skills_remote} {codex_skills_remote}")

        _run(client, f"mkdir -p {claude_skills_remote} {codex_skills_remote}")
        _run(client, f"cd $HOME && tar xzf {remote_tmp} && rm -f {remote_tmp}")

        try:
            _run(client, f"rm -f {remote_tmp}")
        except Exception:
            pass

        return {
            "ok": True,
            "mode": mode,
            "node_id": node_id,
            "summary": f"{'Replaced' if mode == 'replace' else 'Appended'} hub skills on {node_id} ({node.host or node.ssh_host}) → ~/.claude/skills/ and ~/.codex/skills/",
        }
    finally:
        try:
            client.close()
        except Exception:
            pass
