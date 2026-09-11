"""Install the latest Node.js and npm for local or SSH-backed nodes."""
from __future__ import annotations

import re
import shlex
import subprocess
from typing import Callable

import paramiko

from core.common.text_encoding import subprocess_text_kwargs
from core.hub import ssh_deploy
from core.hub.nodes import NodeConfig


NVM_INSTALL_URL = "https://raw.githubusercontent.com/nvm-sh/nvm/master/install.sh"
CommandRunner = Callable[[str, int], str]
DeployProgress = Callable[[str, str], None]
_RESULT_RE = re.compile(r"^LUCID_NPM_RESULT nvm=(\S+) node=(\S+) npm=(\S+)$", re.MULTILINE)


def _report(progress: DeployProgress | None, step: str, message: str) -> None:
    if progress is not None:
        progress(step, message)


def _nvm_install_command() -> str:
    return f"""set -eo pipefail
export NVM_DIR="${{NVM_DIR:-$HOME/.nvm}}"
export PROFILE="$HOME/.bashrc"
if command -v curl >/dev/null 2>&1; then
  curl -fsSL {NVM_INSTALL_URL} | PROFILE="$PROFILE" NVM_DIR="$NVM_DIR" bash
elif command -v wget >/dev/null 2>&1; then
  wget -qO- {NVM_INSTALL_URL} | PROFILE="$PROFILE" NVM_DIR="$NVM_DIR" bash
else
  echo "curl or wget is required to install nvm" >&2
  exit 1
fi
test -s "$NVM_DIR/nvm.sh"
"""


def _load_nvm_command() -> str:
    return """export NVM_DIR="${NVM_DIR:-$HOME/.nvm}"
if [ -f "$HOME/.bashrc" ]; then
  source "$HOME/.bashrc"
fi
if [ ! -s "$NVM_DIR/nvm.sh" ]; then
  echo "nvm.sh was not installed at $NVM_DIR/nvm.sh" >&2
  exit 1
fi
source "$NVM_DIR/nvm.sh"
"""


def _node_npm_install_command() -> str:
    return "set -eo pipefail\n" + _load_nvm_command() + """nvm install node --latest-npm
nvm alias default node
nvm use node --silent
npm install --global npm@latest
"""


def _version_command() -> str:
    return "set -eo pipefail\n" + _load_nvm_command() + """nvm use node --silent
printf 'LUCID_NPM_RESULT nvm=%s node=%s npm=%s\n' "$(nvm --version)" "$(node --version)" "$(npm --version)"
"""


def deploy_npm_with_runner(
    run: CommandRunner,
    progress: DeployProgress | None = None,
) -> dict[str, object]:
    _report(progress, "npm_install_nvm", "Installing or updating nvm")
    run(_nvm_install_command(), 600)
    _report(progress, "npm_install_node", "Loading .bashrc and installing the latest Node.js and npm")
    run(_node_npm_install_command(), 1800)
    _report(progress, "npm_verify", "Verifying nvm, Node.js, and npm versions")
    output = run(_version_command(), 60)
    match = _RESULT_RE.search(output)
    if match is None:
        raise RuntimeError("npm deployment did not return installed versions")
    return {
        "ok": True,
        "nvm_version": match.group(1),
        "node_version": match.group(2),
        "npm_version": match.group(3),
    }


def _run_local(command: str, timeout: int) -> str:
    try:
        result = subprocess.run(
            ["bash", "-lc", command],
            capture_output=True,
            timeout=timeout,
            **subprocess_text_kwargs(),
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"local npm deployment timed out after {timeout} seconds") from exc
    if result.returncode != 0:
        raise RuntimeError(
            f"local npm command failed rc={result.returncode}\n"
            f"stdout={result.stdout[-4000:]}\nstderr={result.stderr[-4000:]}"
        )
    return result.stdout


def _deploy_npm_remote(node: NodeConfig, progress: DeployProgress | None) -> dict[str, object]:
    host = node.host or node.ssh_host
    if not host or not node.user:
        raise ValueError(f"node {node.id} is missing SSH host or user")
    request = ssh_deploy.DeployRequest(
        id=node.id,
        host=host,
        user=node.user,
        identity_file=node.identity_file,
        ssh_port=node.ssh_port,
    )
    _report(progress, "npm_connect", f"Connecting to {node.user}@{host}:{node.ssh_port}")
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(**ssh_deploy._ssh_connect_kwargs(request))
    try:
        return deploy_npm_with_runner(
            lambda command, timeout: ssh_deploy._run(
                client,
                f"bash -lc {shlex.quote(command)}",
                timeout=timeout,
            ),
            progress,
        )
    finally:
        client.close()


def deploy_npm_for_node(
    node: NodeConfig,
    progress: DeployProgress | None = None,
) -> dict[str, object]:
    if node.kind == "ssh":
        result = _deploy_npm_remote(node, progress)
    elif node.id == "local" and node.kind in {"local", "agent"}:
        _report(progress, "npm_prepare_local", "Preparing local npm deployment")
        result = deploy_npm_with_runner(_run_local, progress)
    else:
        raise ValueError(f"node {node.id} does not support npm deployment")
    return {**result, "node_id": node.id, "node_name": node.display_name}
