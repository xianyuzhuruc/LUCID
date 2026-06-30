# LUCID — Cross-Server Terminal Monitor & Alert Dashboard

<div align="center">
  <a href="README.md">English</a> | <a href="README.zh-CN.md">中文</a>
</div>

LUCID is a web dashboard that gives you a single pane of glass over every
**Claude Code**, **OpenAI Codex**, and **Bash** session running across your
machines. It watches terminal output in real time, detects when a session
**stops making progress** or **needs your input**, and surfaces those alerts so
you never miss a permission prompt or a stalled agent.

If you juggle multiple agent sessions across local and remote servers, LUCID
tells you which ones are busy, which are stuck, and which are waiting for you —
and lets you jump into any of them instantly.

---

## Capabilities at a Glance

### Real-Time Terminal Monitoring

Each session card shows at a glance:
- **Name**, **platform** (Claude / Codex / Bash), and **working directory**
- Current **triage status** with a colour dot (Working, Waiting, Stalled, Completed, or Bash)
- Idle time and process PID
- Skills used and memory operations performed
- An expandable detail view with full metadata and actions

### Intelligent Status Classification

LUCID classifies every terminal into one of five states:

| Status      | Colour  | Meaning |
|-------------|---------|---------|
| **Working** | Green   | The session is actively making progress. |
| **Waiting** | Red     | The session needs your attention — an interactive prompt or error. |
| **Stalled** | Amber   | No new output for a while. Worth checking. |
| **Completed** | Blue | You manually marked this session as done. Excluded from alerts. |
| **Bash**    | Grey    | A plain shell session — not monitored for prompts. |

### ATTENTION Bar

Sessions that need you appear in a sticky bar at the top of the page. Each
entry shows the terminal name with a colour-coded status dot. Click any entry
to open that terminal immediately — even while zoomed into a different session.

When you zoom into a session, it is temporarily hidden from the ATTENTION bar
so you can work undisturbed.

### Full Terminal Emulation

Click **Terminal** on any session card to open a live terminal embedded in the
dashboard. You can type directly, copy/paste, send control sequences (Ctrl-C,
Enter), scroll through history, and drag-and-drop file paths from the sidebar.

From an open terminal, click **New Bash** to start a Bash shell in the same
current directory. The new shell opens as an **Editor** tab, so files and
ad-hoc Bash sessions are managed together in one tab strip instead of creating
another session card.

### File Browser & Text Editor

- **Browse** directories on any connected server.
- **Upload** files from your local machine.
- **Create** directories.
- **Edit** text files with the built-in editor, with change tracking.
- **Delete** files directly from the editor.
- **Manage Bash tabs** launched from an active terminal alongside editor tabs.

In Zoom mode you can switch between the file browser and a terminal list view
that lets you jump between active sessions without leaving the zoom.

### Zoom Mode

Click **Zoom** to expand the terminal panel to fill the entire browser viewport.
Click **Unzoom** or **×** to return to the normal layout.

### Multi-Server Architecture

LUCID can reach **any server accessible via SSH** — no special firewall rules,
no VPN, no public ports. As long as you can `ssh user@host`, LUCID can manage
sessions on that machine.

A lightweight agent runs on each server and communicates with the hub over
SSH tunnels. Deploying and updating remote agents is a single click from the
Nodes panel.

### Node Management

Open the **Nodes** panel to see all configured servers. Each node card shows
its name, connection details, health status, and window count.

- **Deploy / update agent** — Fill in host, user, and optional password. If a
  password is provided, LUCID generates an SSH key, copies the public key to
  the remote server, then switches to key-based authentication. The password
  is **never** written to disk.
- **Sync all nodes** — Re-deploy the runtime to every SSH node at once.
- **Delete** — Remove the node from your local config without touching the
  remote server.
- **Remove** — Kill the remote agent and delete its files, then remove the
  local config. Irreversible; requires SSH access to the node and refuses
  obviously unsafe remote directories such as `/`, `$HOME`, or system paths.
- **Click a node card** to select it as the default target in the Launch panel.

### Launch Managed Sessions

From the Launch panel on any node you can start a **Codex**, **Claude**, or
**Bash** session. You can pick a working directory via the built-in path
browser, optionally name the terminal, and customise the launch command.

Sessions automatically find tools installed through **nvm**, **bun**, and
other version managers — no manual PATH configuration needed.

### Session Actions

Every managed session supports these actions:

| Action      | What It Does |
|-------------|-------------|
| **Focus**   | Attach to the tmux session (gives you the attach command). |
| **Terminal** | Open the embedded terminal for live interaction. |
| **Close**    | Kill the managed process. |
| **Resume**   | Continue a previous Claude/Codex session from where it left off. |
| **Fork**     | Clone a session into a new one that inherits the conversation history. |
| **Complete** | Mark the session as done (excluded from alerts and triage). |
| **Start**    | Re-activate a completed session. |
| **Review**   | Run a background Claude review of the session's recent work and get a verdict. |
| **Rename**   | Give the session a memorable display name. |

History sessions also offer **Focus** / **Resume** and **Fork** buttons.

### Session History & Full-Text Search

- **Search** across transcript content from all nodes.
- **Filter by node** to see only one server's history.
- **Refresh** to re-scan for new transcripts.
- Pagination with configurable page size.
- Each entry shows the node, platform, first user input, model, date, size,
  and skill / memory tags.
- **Match snippets** highlight where your search term appears in each
  transcript.

Click any history entry to open its **Timeline** — a scrollable log of user
messages, assistant responses, and tool calls.

The global **Search** box in the header searches across all transcripts on all
nodes and returns hit excerpts with context lines.

### Knowledge Overview

The dashboard surfaces knowledge artifacts extracted from agent sessions:
- **Skills** — which skills were invoked, how many times, and across how many
  sessions.
- **Memories** — which memory files were read or written, with full content
  previews.
- **Plans** — plan history with version diffs and copyable content.

Click a skill tag in the timeline to jump to its invocation. Click a memory
tag to see the full memory content in a popup.

### Deep Linking

Append `#search=<query>` or `#timeline=<session_id>` to the dashboard URL to
deep-link directly to a search or a specific session timeline.

---

## Getting Started

### Prerequisites

- Python 3.10+
- tmux
- Bash (Linux / macOS) or `run.bat` (Windows)

### Start the Hub

```bash
git clone https://github.com/tianyilt/LUCID
cd LUCID
bash run.sh
```

Open **http://127.0.0.1:21893** in your browser.

`run.sh` creates a virtual environment, installs dependencies, and starts the
server. To use a different port:

```bash
LUCID_PORT=21894 bash run.sh
```

To keep the hub running in the background:

```bash
nohup bash run.sh > run.log &
```

### Add Your Local Machine

The hub starts without monitoring any sessions by default. To include the
local machine:

1. Open the dashboard → **Nodes** panel.
2. Click **Enable local node**.

### Launch a Managed Session

From the Launch panel in the Nodes view, or from the command line:

```bash
python -m core.terminal.runner run claude --tmux --cwd /your/project --display-name "my-task"
python -m core.terminal.runner run codex --tmux --cwd /your/project
python -m core.terminal.runner run bash   --tmux --cwd /your/project
```

### Deploy a Remote Agent

LUCID can reach **any server you can SSH into** — no special firewall rules
or VPN required.

Set up SSH key access first (optional — LUCID will do this for you during
deployment if you provide a password):

```bash
ssh-copy-id user@server
ssh user@server   # verify no password prompt
```

Then deploy from the Nodes panel:

1. Fill in **node id**, **host**, **SSH user**, and **SSH port**.
2. Enter a **password** only when first-time key setup is needed.
3. Leave **agent port** empty — LUCID picks a free port automatically.
4. Keep **remote dir** as `~/.lucid/agent` unless you need a custom install
   location.
5. Click **Deploy / update agent** and watch the queued deploy status.

Saved SSH connections appear as clickable shortcuts in the **Recent SSH** list.
Use **Sync all nodes** after updating the checkout when remote agents need the
new backend/API code. Hub-only changes only require restarting the hub and
refreshing the browser.

### Manual Agent Setup

If you prefer to manage deployment yourself, start the agent on the remote
machine:

```bash
cd /path/to/LUCID
LUCID_MODE=agent LUCID_AGENT_HOST=127.0.0.1 LUCID_PORT=7879 bash run.sh
```

Create an SSH tunnel from your local machine:

```bash
ssh -N -L 17879:127.0.0.1:7879 user@server
```

Add to `~/.lucid/nodes.toml`:

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

---

## Security

- All remote access goes through SSH, the same mechanism you already use to
  log into your servers.
- **SSH keys are used for all connections.** If you provide a password during
  deployment, it is used exclusively for a one-shot connection to install a
  public key on the remote server. The password is **never** persisted to disk.
- If you bind the hub to a non-loopback address, add authentication before
  exposing it to other users.

---

## Ports

| Component     | Default Port | Notes                                      |
|---------------|-------------|--------------------------------------------|
| Hub           | `21893`     | Override with `LUCID_PORT`.                |
| Remote agent  | auto        | UI deployment picks a free port.           |
| Manual agent  | any         | Any free port works (e.g. `7879`).         |

If the default hub port is already in use, set a different one:

```bash
LUCID_PORT=21894 bash run.sh
```

For manually deployed agents, make sure the agent port on the remote machine
and the local tunnel port are both free before starting.

---

## Demo Mode

Explore the dashboard with fixture data (no real sessions):

```bash
python3 fixtures/seed.py
LUCID_HOME=fixtures/demo-home bash run.sh
python3 fixtures/seed.py --stop
```

---

## License

MIT — see [LICENSE](LICENSE).
