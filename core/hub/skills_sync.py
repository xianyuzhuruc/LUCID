"""Sync hub skills (~/.lucid/skills/) to/from remote agent nodes."""
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
from core.knowledge.skills import HUB_SKILLS_DIR
from core.terminal.sessions import CLAUDE_HOME, HOME_BASE

# On remote agent nodes, the same skills are installed to both directories.
REMOTE_SKILL_DIRS = (CLAUDE_HOME / "skills", HOME_BASE / ".codex" / "skills")


def _normalized_tar_member_name(name: str) -> str:
    """Return a safe relative tar member name, or an empty string to skip it."""
    clean = str(name or "").replace("\\", "/").lstrip("/")
    while clean.startswith("./"):
        clean = clean[2:]
    parts = [part for part in clean.split("/") if part not in ("", ".")]
    if not parts or any(part == ".." for part in parts):
        return ""
    return "/".join(parts)


def _load_raw_ssh_history() -> list[dict]:
    try:
        raw = json.loads(SSH_HISTORY_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return []
    if not isinstance(raw, list):
        return []
    return [item for item in raw if isinstance(item, dict)]


def _ssh_connect(node: NodeConfig, password: str = "") -> paramiko.SSHClient:
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
    stdin, stdout, stderr = client.exec_command(command, timeout=timeout)
    rc = stdout.channel.recv_exit_status()
    out = decode_utf8(stdout.read(), errors=BYTE_ERROR_ESCAPE)
    err = decode_utf8(stderr.read(), errors=BYTE_ERROR_ESCAPE)
    if rc != 0:
        raise RuntimeError(f"remote command failed rc={rc}: {command}\nstdout={out[-2000:]}\nstderr={err[-2000:]}")
    return out + err


# ---------------------------------------------------------------------------
# Tarball builders
# ---------------------------------------------------------------------------

def build_skills_tarball_b64(names: list[str] | None = None) -> str:
    """Base64-encoded gzipped tarball (for SSH fallback)."""
    raw = build_skills_tarball_bytes(names)
    return base64.b64encode(raw).decode("ascii") if raw else ""


def build_skills_tarball_bytes(names: list[str] | None = None) -> bytes:
    """Raw gzipped tarball of hub skill directories (for direct binary transfer)."""
    allowed: set[str] | None = None
    if names is not None:
        allowed = set()
        for n in names:
            name = str(n).strip() if isinstance(n, str) else str(n.get("name", "") if isinstance(n, dict) else n).strip()
            if name:
                allowed.add(name)
        if not allowed:
            return ""

    def iter_skill_dirs() -> list[Path]:
        skill_dirs: list[Path] = []
        if not HUB_SKILLS_DIR.exists():
            return skill_dirs
        for skill_md in HUB_SKILLS_DIR.rglob("SKILL.md"):
            skill_dir = skill_md.parent
            rel = skill_dir.relative_to(HUB_SKILLS_DIR)
            if any(part.startswith(".") and part != ".system" for part in rel.parts):
                continue
            if allowed is not None and skill_dir.name not in allowed:
                continue
            skill_dirs.append(skill_dir)
        return sorted(skill_dirs)

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        if not HUB_SKILLS_DIR.exists():
            return ""
        if allowed is not None:
            for skill_dir in iter_skill_dirs():
                arcname = str(skill_dir.relative_to(HUB_SKILLS_DIR))
                tar.add(skill_dir, arcname=arcname, recursive=True)

        else:
            # Only include skill directories (those containing SKILL.md) or
            # parent directories of nested skills.  Collect the set of paths
            # that are needed.
            needed: set[str] = set()
            for skill_dir in iter_skill_dirs():
                rel = skill_dir.relative_to(HUB_SKILLS_DIR) / "SKILL.md"
                # Add every prefix directory
                for p in rel.parents:
                    needed.add(str(p))
            # Also add any loose files at the root
            for item in HUB_SKILLS_DIR.iterdir():
                if item.is_file() and not item.name.startswith("."):
                    needed.add("")  # marker for root files

            for item in sorted(HUB_SKILLS_DIR.iterdir()):
                if item.name.startswith(".") and item.name not in (".system",):
                    continue
                # Only include if this item or something under it is needed
                if item.is_dir():
                    rel = str(Path(item.name))
                    if not any(n == rel or n.startswith(rel + "/") for n in needed):
                        continue
                    tar.add(item, arcname=item.name, recursive=True)
                elif item.is_file() and "" in needed:
                    tar.add(item, arcname=item.name)
    return buf.getvalue()


def build_skills_tarball_b64(names: list[str] | None = None) -> str:
    """Base64-encoded gzipped tarball (for SSH fallback)."""
    raw = build_skills_tarball_bytes(names)
    return base64.b64encode(raw).decode("ascii") if raw else ""


def build_agent_skills_tarball_raw() -> bytes:
    """Build a tarball of the **agent's** actual skill directories.

    Called by the agent-side /agent/v1/skills/raw endpoint —
    it tars up ~/.claude/skills/ and ~/.codex/skills/, NOT the hub dir.
    """
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for skills_dir in REMOTE_SKILL_DIRS:
            if not skills_dir.exists():
                continue
            for item in sorted(skills_dir.iterdir()):
                if item.name.startswith(".") and item.name not in (".system",):
                    continue
                arcname = item.name
                if item.is_dir():
                    tar.add(item, arcname=arcname, recursive=True)
                elif item.is_file():
                    tar.add(item, arcname=arcname)
    return buf.getvalue()


def build_skills_tarball_raw() -> bytes:
    b64 = build_skills_tarball_b64()
    return base64.b64decode(b64) if b64 else b""


# ---------------------------------------------------------------------------
# Install on remote agent (hub → remote)
# ---------------------------------------------------------------------------

def install_skills_to_remote(tarball_bytes: bytes, mode: str) -> dict:
    """Extract a tarball to both remote skill directories.

    Called by the agent-side ``/agent/v1/skills/sync`` endpoint.
    Accepts raw gzipped tarball bytes.
    """
    if mode not in ("append", "replace"):
        return {"ok": False, "error": f"Invalid mode: {mode}"}
    if not tarball_bytes:
        return {"ok": False, "error": "Empty tarball"}

    import shutil

    if mode == "replace":
        for d in REMOTE_SKILL_DIRS:
            if d.exists():
                shutil.rmtree(d, ignore_errors=True)

    for d in REMOTE_SKILL_DIRS:
        d.mkdir(parents=True, exist_ok=True)

    synced = []
    with tarfile.open(fileobj=io.BytesIO(tarball_bytes), mode="r:gz") as tar:
        for member in tar.getmembers():
            member_name = _normalized_tar_member_name(member.name)
            if not member_name or member_name.startswith(".") and "/" not in member_name:
                continue
            member.name = member_name
            # Extract into each target dir
            for target_dir in REMOTE_SKILL_DIRS:
                target_path = (target_dir / member_name).resolve()
                if not str(target_path).startswith(str(target_dir.resolve())):
                    continue
                tar.extract(member, path=str(target_dir), set_attrs=False)
            if str(REMOTE_SKILL_DIRS[0]) not in synced:
                synced.append(str(REMOTE_SKILL_DIRS[0]))
            if str(REMOTE_SKILL_DIRS[1]) not in synced:
                synced.append(str(REMOTE_SKILL_DIRS[1]))

    return {
        "ok": True,
        "mode": mode,
        "synced_dirs": synced,
        "summary": f"{'Replaced' if mode == 'replace' else 'Appended'} hub skills → ~/.claude/skills/ and ~/.codex/skills/",
    }


# ---------------------------------------------------------------------------
# Install on hub (remote → hub, "pull")
# ---------------------------------------------------------------------------

def install_skills_append_only(tarball_bytes: bytes) -> dict:
    """Extract a tarball to the hub, skipping already-existing skills."""
    existing = _hub_skill_names()
    if not tarball_bytes:
        return {"ok": False, "error": "Empty tarball"}

    HUB_SKILLS_DIR.mkdir(parents=True, exist_ok=True)

    installed: list[str] = []
    skipped: list[str] = []

    try:
        with tarfile.open(fileobj=io.BytesIO(tarball_bytes), mode="r:gz") as tar:
            members = [(member, _normalized_tar_member_name(member.name))
                       for member in tar.getmembers()]
            # Find which skills are new
            new_skills: set[str] = set()
            for _member, member_name in members:
                if not (member_name.endswith("/SKILL.md") or
                        (member_name.endswith("SKILL.md") and "/" in member_name)):
                    continue
                skill_name = member_name.split("/")[0]
                if not skill_name or skill_name.startswith("."):
                    continue
                if skill_name not in existing:
                    new_skills.add(skill_name)
                else:
                    if skill_name not in skipped:
                        skipped.append(skill_name)

            if not new_skills:
                return {
                    "ok": True, "mode": "pull",
                    "installed": [], "skipped": skipped,
                    "summary": f"All {len(skipped)} remote skills already exist on hub — nothing to pull.",
                }

            # Extract only files belonging to new skills
            for member, member_name in members:
                skill_name = member_name.split("/")[0] if member_name else ""
                if not skill_name or skill_name not in new_skills:
                    continue
                member.name = member_name
                target_path = (HUB_SKILLS_DIR / member_name).resolve()
                if not str(target_path).startswith(str(HUB_SKILLS_DIR.resolve())):
                    continue
                tar.extract(member, path=str(HUB_SKILLS_DIR), set_attrs=False)

            installed = sorted(new_skills)
    except Exception as e:
        return {"ok": False, "error": f"Tarball extraction failed: {e}"}

    return {
        "ok": True, "mode": "pull",
        "installed": installed, "skipped": skipped,
        "summary": (
            f"Pulled {len(installed)} new skill(s) from remote → hub "
            f"({', '.join(installed[:5])}{'...' if len(installed) > 5 else ''})"
            if installed else f"No new skills — {len(skipped)} already on hub."
        ),
    }


def _hub_skill_names() -> set[str]:
    """Return all skill and parent-directory names present on the hub.

    Includes both skill names (directories containing SKILL.md) and
    intermediate directory names that sit between SKILL.md-containing
    dirs and the skills root.  This prevents empty parent dirs from
    being falsely installed as new skills during a pull.
    """
    names: set[str] = set()
    if not HUB_SKILLS_DIR.exists():
        return names
    for d in HUB_SKILLS_DIR.rglob("*"):
        if d.is_dir() and (d / "SKILL.md").exists():
            names.add(d.name)
            # Also register all ancestor directories up to HUB_SKILLS_DIR
            for parent in d.relative_to(HUB_SKILLS_DIR).parents:
                if str(parent) != ".":
                    names.add(str(parent))
    return names


# ---------------------------------------------------------------------------
# SSH direct sync (hub → remote)
# ---------------------------------------------------------------------------

def sync_skills_via_ssh(node_id: str, tarball_b64: str, mode: str) -> dict:
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

    client = _ssh_connect(node, password)
    try:
        remote_tmp = "/tmp/lucid-skills-sync.tar.gz"
        with client.open_sftp() as sftp:
            sftp.putfo(io.BytesIO(raw), remote_tmp)

        _claude = "$HOME/.claude/skills"
        _codex = "$HOME/.codex/skills"

        if mode == "replace":
            _run(client, f"rm -rf {_claude} {_codex}")
        _run(client, f"mkdir -p {_claude} {_codex}")
        # Extract to both directories
        _run(client, f"cd $HOME && tar xzf {remote_tmp} -C {_claude} && tar xzf {remote_tmp} -C {_codex} && rm -f {remote_tmp}")

        try:
            _run(client, f"rm -f {remote_tmp}")
        except Exception:
            pass

        return {
            "ok": True, "mode": mode, "node_id": node_id,
            "summary": f"{'Replaced' if mode == 'replace' else 'Appended'} hub skills on {node_id} ({node.host or node.ssh_host}) → ~/.claude/skills/ and ~/.codex/skills/",
        }
    finally:
        try:
            client.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# SSH pull (remote → hub)
# ---------------------------------------------------------------------------

def pull_skills_via_ssh(node_id: str) -> tuple[bytes, dict]:
    """Download and merge skills from both remote directories into a single tarball."""
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
        # Merge skills from both dirs while stripping the root skills path so
        # the hub receives entries like "skill-name/SKILL.md".
        _run(client, (
            f"rm -f {remote_tmp} && cd $HOME && "
            f"mkdir -p .lucid/skill-pull-tmp && "
            f"cp -a .claude/skills/. .lucid/skill-pull-tmp/ 2>/dev/null || true; "
            f"cp -a .codex/skills/. .lucid/skill-pull-tmp/ 2>/dev/null || true; "
            f"tar czf {remote_tmp} -C .lucid/skill-pull-tmp . 2>/dev/null || true; "
            f"rm -rf .lucid/skill-pull-tmp"
        ))

        buf = io.BytesIO()
        with client.open_sftp() as sftp:
            sftp.getfo(remote_tmp, buf)
        _run(client, f"rm -f {remote_tmp}")

        node_info = {"id": node.id, "name": node.name or node.id,
                     "host": node.host or node.ssh_host, "user": node.user}
        return buf.getvalue(), node_info
    finally:
        try:
            client.close()
        except Exception:
            pass
