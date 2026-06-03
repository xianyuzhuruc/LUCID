"""Validate that a TCP address is bindable before starting the web server."""
from __future__ import annotations

import argparse
import errno
import socket
import sys
from dataclasses import dataclass
from typing import Sequence


@dataclass(frozen=True)
class PortConfig:
    host: str
    port: int


class PortUnavailableError(RuntimeError):
    """Raised when the configured TCP address cannot be bound."""


def _parse_port(raw_port: str) -> int:
    try:
        port = int(raw_port)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"port must be an integer: {raw_port!r}") from exc

    if port < 1 or port > 65535:
        raise argparse.ArgumentTypeError(f"port must be between 1 and 65535: {port}")

    return port


def _parse_args(argv: Sequence[str]) -> PortConfig:
    parser = argparse.ArgumentParser(description="Check whether a TCP address is bindable.")
    parser.add_argument("--host", required=True)
    parser.add_argument("--port", required=True, type=_parse_port)
    namespace: argparse.Namespace = parser.parse_args(argv)
    return PortConfig(host=str(namespace.host), port=int(namespace.port))


def _family_for_host(host: str) -> socket.AddressFamily:
    if ":" in host:
        return socket.AF_INET6
    return socket.AF_INET


def _format_os_error(config: PortConfig, error: OSError) -> str:
    winerror = getattr(error, "winerror", None)
    fields: list[str] = [
        "[LUCID] cannot bind configured address.",
        f"[LUCID] host={config.host}",
        f"[LUCID] port={config.port}",
        f"[LUCID] errno={error.errno}",
    ]
    if winerror is not None:
        fields.append(f"[LUCID] winerror={winerror}")
    if error.strerror:
        fields.append(f"[LUCID] message={error.strerror}")

    if winerror == 10013 or error.errno == errno.EACCES:
        fields.extend(
            (
                "[LUCID] Windows denied this port. It may be reserved by the OS or blocked by policy.",
                "[LUCID] Try another port: set LUCID_PORT=21894",
                "[LUCID] Inspect reserved ranges: netsh interface ipv4 show excludedportrange protocol=tcp",
            )
        )
    elif error.errno == errno.EADDRINUSE:
        fields.append("[LUCID] Another process is already using this port.")
        fields.append("[LUCID] Try another port: set LUCID_PORT=21894")

    return "\n".join(fields)


def check_port_available(config: PortConfig) -> None:
    family = _family_for_host(config.host)
    try:
        with socket.create_server((config.host, config.port), family=family):
            return
    except OSError as exc:
        raise PortUnavailableError(_format_os_error(config, exc)) from exc


def main(argv: Sequence[str]) -> int:
    config = _parse_args(argv)
    try:
        check_port_available(config)
    except PortUnavailableError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
