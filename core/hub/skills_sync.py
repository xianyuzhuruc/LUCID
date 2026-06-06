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


def build_skills_tarball_b64(names: list[dict] | None = None) -> str:
    """Create a base64-encoded gzipped tarball of the hub's skill directories.

    If *names* is provided, only include skills matching ``{name, origin}`` entries.

    Includes ``~/.claude/skills/*`` and ``~/.codex/skills/*`` (skipping
    dot-prefixed entries).
    """
    # Build allow-list if names filter is provided (empty list = all skills)
    allowed: set[tuple[str, str]] | None = None
    if names is not None:
        allowed = set()
        for item in names:
            n = str(item.get("name") or "").strip()
            o = str(item.get("origin") or "claude").strip().lower()
            if n:
                allowed.add((n, o))
        if not allowed:
            return ""  # no skills selected

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for skills_dir, origin_tag in ((SKILLS_DIR, "claude"), (CODEX_SKILLS_DIR, "codex")):
            if not skills_dir.exists():
                continue
            for item in sorted(skills_dir.iterdir()):
                if item.name.startswith(".") and item.name not in (".system",):
                    continue
                # If filtering, skip skills not in the allowed set
                if allowed is not None and item.is_dir():
                    skill_name = item.name
                    if (skill_name, origin_tag) not in allowed:
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


def build_skills_tarball_raw(names: list[dict] | None = None) -> bytes:
    """Return a gzipped tarball of hub skill directories as raw bytes."""
    allowed: set[tuple[str, str]] | None = None
    if names is not None:
        allowed = set()
        for item in names:
            n = str(item.get("name") or "").strip()
            o = str(item.get("origin") or "claude").strip().lower()
            if n:
                allowed.add((n, o))
        if not allowed:
            return b""  # no skills selected

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for skills_dir, origin_tag in ((SKILLS_DIR, "claude"), (CODEX_SKILLS_DIR, "codex")):
            if not skills_dir.exists():
                continue
            for item in sorted(skills_dir.iterdir()):
                if item.name.startswith(".") and item.name not in (".system",):
                    continue
                if allowed is not None and item.is_dir():
                    if (item.name, origin_tag) not in allowed:
                        continue
                arcname = f"{skills_dir.name}/{item.name}"
                if item.is_dir():
                    tar.add(item, arcname=arcname, recursive=True)
                elif item.is_file():
                    tar.add(item, arcname=arcname)
    return buf.getvalue()


def hub_skill_names() -> set[str]:
    """Return the set of skill names already installed on the hub."""
    names: set[str] = set()
    for base in (SKILLS_DIR, CODEX_SKILLS_DIR):
        if not base.exists():
            continue
        for d in base.iterdir():
            if d.is_dir() and (d / "SKILL.md").exists():
                names.add(d.name)
            # Recurse one extra level for .system etc
            if d.is_dir() and d.name.startswith("."):
                for sd in d.iterdir():
                    if sd.is_dir() and (sd / "SKILL.md").exists():
                        names.add(sd.name)
    return names


def install_skills_append_only(tarball_bytes: bytes) -> dict:
    """Extract a tarball to hub, skipping skills that already exist.

    Only installs skill directories that are *not* already present on the hub
    (checked by directory name + existence of SKILL.md).
    """
    existing = hub_skill_names()
    if not tarball_bytes:
        return {"ok": False, "error": "Empty tarball"}

    claude_skills = SKILLS_DIR
    codex_skills = CODEX_SKILLS_DIR
    claude_skills.mkdir(parents=True, exist_ok=True)
    codex_skills.mkdir(parents=True, exist_ok=True)

    installed: list[str] = []
    skipped: list[str] = []

    try:
        with tarfile.open(fileobj=io.BytesIO(tarball_bytes), mode="r:gz") as tar:
            # First pass: collect the top-level skill names from the tarball
            skill_names_in_tar: dict[str, str] = {}  # name -> dir_tag
            for member in tar.getmembers():
                parts = member.name.split("/", 1)
                if len(parts) < 2:
                    continue
                dir_tag, rest = parts[0], parts[1]
                skill_name = rest.split("/")[0]
                if not skill_name:
                    continue
                skill_names_in_tar[skill_name] = dir_tag

            # Filter to only new skills
            new_skills: set[str] = set()
            for name, dir_tag in skill_names_in_tar.items():
                if name not in existing:
                    new_skills.add(name)
                else:
                    skipped.append(name)

            if not new_skills:
                return {
                    "ok": True,
                    "mode": "pull",
                    "installed": [],
                    "skipped": skipped,
                    "summary": f"All {len(skipped)} remote skills already exist on hub — nothing to pull.",
                }

            # Second pass: extract only files belonging to new skills
            # Re-open the tarball for a fresh read
            tar.fileobj.seek(0)
            for member in tar.getmembers():
                parts = member.name.split("/", 1)
                if len(parts) < 2:
                    continue
                dir_tag, rest = parts[0], parts[1]
                skill_name = rest.split("/")[0]
                if skill_name not in new_skills:
                    continue

                if dir_tag == "skills":
                    target_dir = claude_skills
                elif dir_tag == "skills.codex":
                    target_dir = codex_skills
                else:
                    continue

                if not rest:
                    continue

                member.name = rest
                target_path = (target_dir / rest).resolve()
                if not str(target_path).startswith(str(target_dir.resolve())):
                    continue
                tar.extract(member, path=str(target_dir), set_attrs=False)

            installed = sorted(new_skills)
    except Exception as e:
        return {"ok": False, "error": f"Tarball extraction failed: {e}"}

    return {
        "ok": True,
        "mode": "pull",
        "installed": installed,
        "skipped": skipped,
        "summary": f"Pulled {len(installed)} new skill(s) from remote → hub ({', '.join(installed[:5])}{'...' if len(installed) > 5 else ''})" if installed else f"No new skills — {len(skipped)} already on hub.",
    }


def pull_skills_via_ssh(node_id: str) -> tuple[bytes, dict]:
    """Download a skills tarball from a remote SSH node.

    Returns (tarball_bytes, node_info).
    """
    cfg = load_config()
    node = None
    for n in cfg.nodes:
        if n.id == node_id:
            node = n
            break
    if node is None:
        raise ValueError(f"unknown node: {node_id}")

    password = ""
    for entry in _load_raw_ssh_history():
        if entry.get("id") == node_id:
            password = str(entry.get("password") or "")
            break

    client = _ssh_connect(node, password)
    try:
        remote_tmp = "/tmp/lucid-skills-pull.tar.gz"
        # Build tarball on remote and download it
        _run(client, (
            f"rm -f {remote_tmp} && "
            f"cd $HOME && "
            f"tar czf {remote_tmp} .claude/skills .codex/skills 2>/dev/null || true"
        ))

        buf = io.BytesIO()
        with client.open_sftp() as sftp:
            sftp.getfo(remote_tmp, buf)
        _run(client, f"rm -f {remote_tmp}")

        node_info = {
            "id": node.id,
            "name": node.name or node.id,
            "host": node.host or node.ssh_host,
            "user": node.user,
        }
        return buf.getvalue(), node_info
    finally:
        try:
            client.close()
        except Exception:
            pass
