"""Directory browsing helpers for terminal launch working directories."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Literal, TypedDict


class PathEntry(TypedDict):
    name: str
    path: str
    kind: Literal["directory", "file", "unknown"]
    hidden: bool
    readable: bool
    symlink: bool
    mtime_ms: int
    error: str


class PathListPayload(TypedDict):
    ok: bool
    path: str
    parent: str
    home: str
    entries: list[PathEntry]


class UploadResult(TypedDict):
    ok: bool
    directory: str
    path: str
    name: str
    size: int
    overwritten: bool


def _resolve_directory(raw_path: str | None) -> Path:
    target = Path(raw_path).expanduser() if raw_path else Path.home()
    resolved = target.resolve(strict=True)
    if resolved.is_file():
        return resolved.parent
    if not resolved.is_dir():
        raise NotADirectoryError(f"path is not a directory: {resolved}")
    return resolved


def _entry_payload(entry: os.DirEntry[str]) -> PathEntry | None:
    try:
        if entry.is_dir(follow_symlinks=True):
            kind: Literal["directory", "file"] = "directory"
            readable = os.access(entry.path, os.R_OK | os.X_OK)
        elif entry.is_file(follow_symlinks=True):
            kind = "file"
            readable = os.access(entry.path, os.R_OK)
        else:
            return None
        stat_result = entry.stat(follow_symlinks=True)
        return {
            "name": entry.name,
            "path": str(Path(entry.path).resolve(strict=True)),
            "kind": kind,
            "hidden": entry.name.startswith("."),
            "readable": readable,
            "symlink": entry.is_symlink(),
            "mtime_ms": int(stat_result.st_mtime * 1000),
            "error": "",
        }
    except OSError as exc:
        return {
            "name": entry.name,
            "path": entry.path,
            "kind": "unknown",
            "hidden": entry.name.startswith("."),
            "readable": False,
            "symlink": entry.is_symlink(),
            "mtime_ms": 0,
            "error": str(exc),
        }


def list_directories(raw_path: str | None = None) -> PathListPayload:
    directory = _resolve_directory(raw_path)
    entries: list[PathEntry] = []
    with os.scandir(directory) as iterator:
        for entry in iterator:
            payload = _entry_payload(entry)
            if payload:
                entries.append(payload)
    entries.sort(key=lambda item: (not item["readable"], item["kind"] != "directory", item["hidden"], item["name"].lower()))
    parent = "" if directory.parent == directory else str(directory.parent)
    return {
        "ok": True,
        "path": str(directory),
        "parent": parent,
        "home": str(Path.home().resolve(strict=True)),
        "entries": entries,
    }


def _clean_upload_name(filename: str) -> str:
    name = str(filename or "")
    if not name:
        raise ValueError("file name is required")
    if name in {".", ".."} or "/" in name or "\\" in name or "\0" in name:
        raise ValueError(f"invalid file name: {filename}")
    if Path(name).name != name:
        raise ValueError(f"invalid file name: {filename}")
    return name


def save_uploaded_file(raw_directory: str | None, filename: str, content: bytes) -> UploadResult:
    directory = _resolve_directory(raw_directory)
    if not os.access(directory, os.W_OK | os.X_OK):
        raise PermissionError(f"directory is not writable: {directory}")
    name = _clean_upload_name(filename)
    destination = directory / name
    overwritten = destination.exists()
    if overwritten and not destination.is_file():
        raise IsADirectoryError(f"destination is not a file: {destination}")
    destination.write_bytes(content)
    return {
        "ok": True,
        "directory": str(directory),
        "path": str(destination.resolve(strict=True)),
        "name": name,
        "size": len(content),
        "overwritten": overwritten,
    }
