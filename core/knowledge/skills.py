"""Global skill catalog from ~/.claude/skills/ + ~/.codex/skills/ + usage stats."""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from core.common.text_encoding import read_utf8
from core.terminal.sessions import CLAUDE_HOME, HOME_BASE

SKILLS_DIR = CLAUDE_HOME / "skills"
CODEX_SKILLS_DIR = HOME_BASE / ".codex" / "skills"


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


def _walk_skills(base: Path, origin: str, seen: set[str]) -> list[dict]:
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
            # Still recurse into .system so built-in skills are discovered
            if d.name != ".system":
                continue
        skill_md = d / "SKILL.md"
        if skill_md.exists():
            info = _parse_skill_md(skill_md)
            if info and info["name"] not in seen:
                info["origin"] = origin
                info["is_system"] = d.parent.name == ".system" or d.name == ".system"
                found.append(info)
                seen.add(info["name"])
        # Recurse deeper in case skills live under nested directories
        found.extend(_walk_skills(d, origin, seen))
    return found


def list_all_skills() -> list[dict]:
    skills: list[dict] = []
    seen: set[str] = set()

    if SKILLS_DIR.exists():
        skills.extend(_walk_skills(SKILLS_DIR, "claude", seen))

    if CODEX_SKILLS_DIR.exists():
        skills.extend(_walk_skills(CODEX_SKILLS_DIR, "codex", seen))

    return skills
