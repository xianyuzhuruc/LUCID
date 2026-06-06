"""Global skill catalog from the hub's own ~/.lucid/skills/ directory."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

from core.common.text_encoding import read_utf8

_STATE_DIR = Path(os.environ.get("LUCID_STATE_DIR", "~/.lucid")).expanduser()
HUB_SKILLS_DIR = _STATE_DIR / "skills"


def _parse_skill_md(path: Path) -> Optional[dict]:
    try:
        text = read_utf8(path)[:4000]
    except Exception:
        return None
    lines = text.splitlines()
    name = path.parent.name
    description = ""
    trigger = ""
    for line in lines:
        line_s = line.strip()
        if line_s.lower().startswith("# ") and not description:
            description = line_s[2:].strip()
        if "trigger" in line_s.lower() or "use when" in line_s.lower():
            trigger = line_s[:200]
            break
    if not description:
        description = name
    return {
        "name": name,
        "dir": str(path.parent),
        "description": description[:200],
        "trigger": trigger[:200],
        "path": str(path),
    }


def _walk_skills(base: Path, seen: set[str]) -> list[dict]:
    """Recursively walk *base* for directories that contain a SKILL.md."""
    found: list[dict] = []
    try:
        entries = sorted(base.iterdir())
    except (PermissionError, OSError):
        return found

    for d in entries:
        if not d.is_dir():
            continue
        if d.name.startswith("."):
            if d.name != ".system":
                continue
        skill_md = d / "SKILL.md"
        if skill_md.exists():
            info = _parse_skill_md(skill_md)
            if info and info["name"] not in seen:
                info["origin"] = "hub"
                info["is_system"] = d.parent.name == ".system" or d.name == ".system"
                found.append(info)
                seen.add(info["name"])
        found.extend(_walk_skills(d, seen))
    return found


def _migrate_from_old_dirs() -> None:
    """One-time: if hub skills dir is empty except for old claude/codex subdirs,
    move their contents up to the flat structure."""
    import shutil
    old_claude = HUB_SKILLS_DIR / "claude"
    old_codex = HUB_SKILLS_DIR / "codex"
    if not old_claude.is_dir() and not old_codex.is_dir():
        return
    # Check if there are already skills in the flat dir
    for item in HUB_SKILLS_DIR.iterdir():
        if item.is_dir() and item.name not in ("claude", "codex"):
            return  # already migrated

    seen = set()
    for src in (old_claude, old_codex):
        if not src.is_dir():
            continue
        for item in src.iterdir():
            dest = HUB_SKILLS_DIR / item.name
            if item.name in seen or dest.exists():
                continue
            seen.add(item.name)
            try:
                if item.is_dir():
                    shutil.copytree(item, dest, symlinks=True)
                elif item.is_symlink():
                    dest.symlink_to(item.readlink())
                else:
                    shutil.copy2(item, dest)
            except (OSError, shutil.Error):
                pass

    shutil.rmtree(old_claude, ignore_errors=True)
    shutil.rmtree(old_codex, ignore_errors=True)


def list_all_skills() -> list[dict]:
    HUB_SKILLS_DIR.mkdir(parents=True, exist_ok=True)
    _migrate_from_old_dirs()
    return _walk_skills(HUB_SKILLS_DIR, set())
