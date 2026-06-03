"""UTF-8 text helpers for filesystem, subprocess, and byte output boundaries."""
from __future__ import annotations

from os import PathLike
from pathlib import Path
from typing import TextIO

UTF8 = "utf-8"
TEXT_ERRORS = "replace"
PROCESS_ERRORS = "replace"
BYTE_ERROR_ESCAPE = "backslashreplace"


def read_utf8(path: str | PathLike[str]) -> str:
    return Path(path).read_text(encoding=UTF8, errors=TEXT_ERRORS)


def write_utf8(path: str | PathLike[str], text: str) -> None:
    Path(path).write_text(text, encoding=UTF8)


def open_utf8(path: str | PathLike[str]) -> TextIO:
    return Path(path).open("r", encoding=UTF8, errors=TEXT_ERRORS)


def decode_utf8(data: bytes | None, errors: str = TEXT_ERRORS) -> str:
    if data is None:
        return ""
    return data.decode(UTF8, errors=errors)


def subprocess_text_kwargs() -> dict[str, object]:
    return {"text": True, "encoding": UTF8, "errors": PROCESS_ERRORS}
