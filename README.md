# LUCID - Your next LLM-Unified Coding & Intelligent Development Platform

LUCID is a web dashboard for monitoring and operating Claude Code,
Codex, and managed shell sessions across one or more machines.

It is designed for developers who keep several agent sessions running at the
same time and need one place to see what is busy, waiting, idle, or ready for
attention. A local hub serves the browser UI, while optional remote agents run
on servers and report local session state back through SSH tunnels.

[中文 README](README.zh-CN.md)

## What It Does

- Shows managed Claude, Codex, and Bash sessions in one dashboard.
- Aggregates multiple remote servers through a hub-and-agent architecture.
- Keeps remote agents private by binding them to `127.0.0.1` and reaching them
  through SSH local port forwarding.
- Supports remote actions such as launch, close, review, resume, fork, terminal
  output, terminal input, and file operations.
- Reads local conversation/history data from existing agent directories such as
  `~/.claude` and `~/.codex`.
- Avoids a frontend build pipeline: the UI is a single static Alpine/Tailwind
  page served by FastAPI.

## Quickstart

### Run The Local Hub

Requirements:

- Python 3.10+
- Bash on Linux/macOS, or `run.bat` on Windows

Start the dashboard:

```bash
git clone https://github.com/tianyilt/LUCID
cd LUCID
bash run.sh
```

Open:

```text
http://127.0.0.1:21893
```

`run.sh` creates `.venv`, installs the Python package in editable mode, checks
that the configured port is bindable, and starts FastAPI/Uvicorn. The default
port is `21893`; override it with:

```bash
LUCID_PORT=21894 bash run.sh
```

On Windows:

```bat
run.bat
```

### Enable The Local Node

The hub starts with remote-node support. To include sessions from the machine
where the hub is running:

1. Open the dashboard.
2. Open the `Nodes` panel.
3. Click `Enable local node`.

This adds a local node to `~/.lucid/nodes.toml`.

### Deploy A Remote Agent

Recommended setup before deploying:

```bash
ssh-copy-id user@server
ssh user@server
```

Make sure SSH key login works before using the dashboard deployment flow.

Then deploy from the UI:

1. Start the local hub with `bash run.sh`.
2. Open `http://127.0.0.1:21893`.
3. Open the `Nodes` panel.
4. Fill in node id, host, SSH user, SSH port, optional agent port, and remote
   directory.
5. Leave agent port empty to let LUCID choose a free remote port.
6. Click `Deploy / update agent`.

The hub will:

- create or reuse a local SSH key under `~/.lucid`;
- connect to the remote server;
- detect the remote runtime platform;
- build or reuse a bundled agent runtime package;
- upload the current checkout and runtime to the remote directory;
- start the remote agent on `127.0.0.1:<agent_port>`;
- create a local SSH tunnel to that agent;
- write the node configuration to `~/.lucid/nodes.toml`.

After deployment, the hub pulls snapshots and forwards actions through:

```text
browser -> local hub -> 127.0.0.1:<local_port> -> SSH tunnel -> remote agent
```

### Manual Agent Start

If you manage deployment yourself, run this on the remote machine:

```bash
cd /path/to/LUCID
LUCID_MODE=agent \
LUCID_AGENT_HOST=127.0.0.1 \
LUCID_PORT=7879 \
bash run.sh
```

Then create an SSH tunnel from the hub machine:

```bash
ssh -N -L 17879:127.0.0.1:7879 user@server
```

Configure the node in `~/.lucid/nodes.toml`:

```toml
[[nodes]]
id = "server-a"
kind = "ssh"
host = "server.example.com"
user = "user"
ssh_port = 22
local_port = 17879
agent_host = "127.0.0.1"
agent_port = 7879
url = "http://127.0.0.1:17879"
auto_tunnel = false
```

## Architecture

```text
Browser
  |
  v
local LUCID hub :21893
  |
  +-- HTTP over SSH tunnel --> server-a agent :127.0.0.1:<auto>
  +-- HTTP over SSH tunnel --> server-b agent :127.0.0.1:<auto>
```

The core rule is that process state belongs to the machine where the process is
running. The hub never treats remote PIDs, TTYs, or process liveness as local
state. It asks each agent for a node-aware wire model and then merges those
models into one dashboard.

## Main Parts

### `app.py`

FastAPI entry point. It serves the static dashboard, exposes hub `/api/*`
routes, exposes agent `/agent/v1/*` routes, and provides SSE/WebSocket endpoints
for live updates and terminal streaming.

Runtime mode is selected with `LUCID_MODE`:

- `hub` or unset: serve the browser UI and aggregate configured nodes.
- `agent`: expose only the agent API for local state and local actions.

### `static/index.html`

Single-page frontend built with Alpine.js and Tailwind from CDNs. It has no npm
build step. The UI polls hub APIs, receives event updates, displays node health,
and sends actions back to the hub.

### `core/hub/nodes.py`

Hub-side node configuration, SSH tunnel supervision, remote HTTP client, and
snapshot aggregation. It reads `~/.lucid/nodes.toml`, starts `ssh -N
-L` tunnels when `auto_tunnel = true`, calls each agent, tracks stale/offline
nodes, and forwards actions to the node that owns the window or session.

### `core/hub/ssh_deploy.py`

Remote bootstrap and update logic. It connects through SSH, uploads the current
checkout, prepares the bundled runtime, chooses a free remote agent port, starts
the agent, verifies health, opens the local tunnel, and persists node config.

### `core/dashboard/localstate.py`

Converts local machine state into the same node-aware format used by remote
agents. This lets the hub treat local and remote nodes consistently.

### `core/terminal/*`

Process and terminal operations. These modules launch managed Claude/Codex/Bash
sessions through tmux, record process metadata in
`~/.lucid/registry.sqlite`, inspect live state, focus or close
windows, capture terminal output, send terminal input, and track permissions.

### `core/conversations/*`

Conversation and transcript readers for Claude and Codex. These modules
implement history listing, search, and timeline extraction from local agent
data.

### `core/knowledge/*`

Skill, memory, and plan extraction. These modules summarize project knowledge
stored in agent transcripts and local memory files.

### `scripts/check_port.py`

Preflight port validation used by `run.sh` and `run.bat`. If the configured
port is already occupied, startup fails early with a clear error instead of
waiting for Uvicorn to fail later.

## Implementation Principles

- The agent owns local process truth. It reads local files, local registries,
  local tmux panes, and local process metadata.
- The hub owns aggregation and routing. It merges snapshots and forwards actions
  to the owning node.
- Remote access uses HTTP over SSH tunnels. Remote agents do not need public
  HTTP ports.
- Stable keys include `node_id`, because different machines can have the same
  PID or session id.
- Managed sessions are preferred for reliable operations. The managed runner
  records enough metadata to support terminal attach, resume, fork, review, and
  close actions.

## Ports

- Hub default port: `21893`
- Manual agent example port: `7879`
- UI deployment can auto-select a free remote agent port.
- UI deployment can auto-select a free local tunnel port unless a specific
  `local_port` is provided.

For hub or manual agent startup, port conflicts are not auto-recovered. Set a
different port:

```bash
LUCID_PORT=21894 bash run.sh
```

## Security Notes

Prefer SSH key login before deploying agents. Direct SSH password deployment can
store the password in plaintext in:

```text
~/.lucid/ssh-history.json
```

The file is written with `0600` permissions, but it is still plaintext. For
production or shared machines, configure SSH keys first and avoid saving
passwords.

The default remote posture is:

- agent binds to `127.0.0.1`;
- hub reaches the agent through SSH local forwarding;
- remote actions are available only through the local hub or the forwarded local
  agent port;
- optional bearer-token auth is supported through node config and agent env.

If you bind the hub outside `127.0.0.1`, add authentication before exposing it
to other users or networks.

## Useful Commands

Run with demo data:

```bash
python3 fixtures/seed.py
LUCID_HOME=fixtures/demo-home bash run.sh
python3 fixtures/seed.py --stop
```

Compile-check Python files:

```bash
python -m py_compile app.py core/**/*.py
```

Prepare a deployment package:

```bash
python scripts/prepare-deployment-package.py
```
