#!/usr/bin/env python3
"""Pre-download agent runtime bundles into deployment_package."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.hub import ssh_deploy


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Pre-download LUCID agent runtime bundles into deployment_package.",
    )
    parser.add_argument(
        "--platform",
        action="append",
        choices=ssh_deploy.SUPPORTED_AGENT_RUNTIME_PLATFORMS,
        help="Target platform to pre-download. Repeat this flag to select multiple platforms.",
    )
    return parser.parse_args()


def _print_progress(step: str, message: str) -> None:
    print(f"[LUCID] {step}: {message}", flush=True)


def main() -> int:
    args = _parse_args()
    platforms = tuple(args.platform or ssh_deploy.DEFAULT_AGENT_RUNTIME_PLATFORMS)
    print(f"[LUCID] deployment package dir: {ssh_deploy.DEPLOY_CACHE}", flush=True)
    bundles = ssh_deploy.prepare_agent_runtime_bundles(platforms, progress=_print_progress)
    for platform, bundle in bundles.items():
        print(f"[LUCID] ready {platform}: {bundle}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
