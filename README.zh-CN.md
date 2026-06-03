# LUCID - Your next LLM-Unified Coding & Intelligent Development Platform

LUCID 是一个用于集中监控和操作 Claude Code、Codex
以及托管 Shell 会话的 Web 控制台。

它适合同时在多台机器上跑多个 agent 会话的场景：本地运行一个 hub 提供浏览器
界面，远程服务器上可选运行 agent；hub 通过 SSH tunnel 拉取各个 agent 的本地
状态，并把操作请求转发到对应机器。

[English README](README.md)

## 这个框架是干嘛的

- 在一个 Dashboard 中查看 Claude、Codex、Bash 等会话状态。
- 支持多服务器聚合：本地 hub 统一展示多个远程 agent 的状态。
- 远程 agent 默认只监听 `127.0.0.1`，通过 SSH 本地端口转发访问，不需要暴露公网
  HTTP 端口。
- 支持 launch、close、review、resume、fork、terminal 输出、terminal 输入、
  文件读写等远程操作。
- 读取已有 agent 的本地数据，例如 `~/.claude` 和 `~/.codex`。
- 前端不需要构建流程：FastAPI 直接服务一个 Alpine.js + Tailwind 的静态页面。

## Quickstart

### 本地部署 Hub

依赖：

- Python 3.10+
- Linux/macOS 使用 Bash；Windows 使用 `run.bat`

启动：

```bash
git clone https://github.com/tianyilt/LUCID
cd LUCID
bash run.sh
```

打开：

```text
http://127.0.0.1:21893
```

`run.sh` 会创建 `.venv`、安装依赖、检查端口是否可绑定，然后启动
FastAPI/Uvicorn。默认端口是 `21893`，可以这样改端口：

```bash
LUCID_PORT=21894 bash run.sh
```

Windows：

```bat
run.bat
```

### 启用本机节点

hub 默认支持远程节点。如果希望把运行 hub 的这台机器也纳入监控：

1. 打开 Dashboard。
2. 打开 `Nodes` 面板。
3. 点击 `Enable local node`。

这会在 `~/.lucid/nodes.toml` 中添加一个本地节点。

### 部署远程 Agent

强烈建议先配置 SSH 密钥登录：

```bash
ssh-copy-id user@server
ssh user@server
```

确认无需输入密码即可 SSH 登录后，再使用 Dashboard 的部署流程。

UI 部署步骤：

1. 在本地启动 hub：`bash run.sh`。
2. 打开 `http://127.0.0.1:21893`。
3. 打开 `Nodes` 面板。
4. 填写 node id、host、SSH user、SSH port、可选 agent port、remote directory。
5. agent port 可以留空，让 LUCID 自动选择一个远程空闲端口。
6. 点击 `Deploy / update agent`。

hub 会执行这些事情：

- 在本地 `~/.lucid` 下创建或复用 SSH key；
- 连接远程服务器；
- 检测远程运行时平台；
- 构建或复用 bundled agent runtime；
- 上传当前代码和运行时到远程目录；
- 在远程启动监听 `127.0.0.1:<agent_port>` 的 agent；
- 创建本地 SSH tunnel；
- 写入或更新 `~/.lucid/nodes.toml`。

部署完成后，通信路径是：

```text
browser -> local hub -> 127.0.0.1:<local_port> -> SSH tunnel -> remote agent
```

### 手动启动 Agent

如果你希望自己管理部署，可以在远程机器上手动启动 agent：

```bash
cd /path/to/LUCID
LUCID_MODE=agent \
LUCID_AGENT_HOST=127.0.0.1 \
LUCID_PORT=7879 \
bash run.sh
```

然后在 hub 所在机器创建 SSH tunnel：

```bash
ssh -N -L 17879:127.0.0.1:7879 user@server
```

再配置 `~/.lucid/nodes.toml`：

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

## 架构

```text
Browser
  |
  v
local LUCID hub :21893
  |
  +-- HTTP over SSH tunnel --> server-a agent :127.0.0.1:<auto>
  +-- HTTP over SSH tunnel --> server-b agent :127.0.0.1:<auto>
```

核心原则：进程状态属于进程所在的机器。hub 不直接解释远程 PID、TTY 或进程存活
状态；它只向各个 agent 请求已经转换好的 node-aware 数据模型，然后合并成一个
Dashboard。

## 每个部分的作用

### `app.py`

FastAPI 入口。负责服务静态 Dashboard，提供 hub 的 `/api/*` 路由，提供 agent 的
`/agent/v1/*` 路由，并提供 SSE/WebSocket 用于实时更新和终端流式输出。

运行模式由 `LUCID_MODE` 控制：

- `hub` 或未设置：启动浏览器 UI，并聚合已配置节点。
- `agent`：只暴露 agent API，用于本机状态读取和本机操作。

### `static/index.html`

单页前端，使用 CDN 版 Alpine.js 和 Tailwind，不需要 npm build。它负责轮询 hub
API、接收事件更新、显示节点健康状态，并把用户操作发回 hub。

### `core/hub/nodes.py`

hub 侧节点配置、SSH tunnel 管理、远程 HTTP client 和 snapshot 聚合。它读取
`~/.lucid/nodes.toml`，在 `auto_tunnel = true` 时启动 `ssh -N -L`，
调用各个 agent，标记 stale/offline 节点，并把操作转发给窗口或会话所属的节点。

### `core/hub/ssh_deploy.py`

远程 bootstrap 和更新逻辑。它通过 SSH 连接远程服务器，上传当前 checkout，准备
bundled runtime，选择远程空闲 agent 端口，启动 agent，检查健康状态，打开本地
tunnel，并持久化节点配置。

### `core/dashboard/localstate.py`

把本机状态转换成和远程 agent 一样的 node-aware 格式。这样 hub 可以用同一套逻辑
处理本地节点和远程节点。

### `core/terminal/*`

进程和终端操作模块。负责通过 tmux 启动托管 Claude/Codex/Bash 会话，把进程元数据
记录到 `~/.lucid/registry.sqlite`，检查 live state，focus/close 窗口，
抓取终端输出，发送终端输入，并跟踪权限等待状态。

### `core/conversations/*`

Claude、Codex 的会话和 transcript 读取模块。负责 history、search、timeline
提取。

### `core/knowledge/*`

技能、memory、plan 相关的数据提取模块。它们从 agent transcript 和本地 memory
文件中整理项目知识。

### `scripts/check_port.py`

启动前端口检查脚本，被 `run.sh` 和 `run.bat` 调用。如果配置端口已被占用，会在
启动 Uvicorn 前直接报错退出。

## 实现原理

- agent 负责本机事实：读取本机文件、本机 registry、本机 tmux pane、本机进程状态。
- hub 负责聚合和路由：合并 snapshot，并把操作转发到对应节点。
- 远程访问使用 HTTP over SSH tunnel：远程 agent 不需要暴露公网 HTTP 端口。
- 稳定 key 带 `node_id`，因为不同机器可能存在相同 PID 或 session id。
- 推荐使用托管会话。托管 runner 会记录足够的元数据，以支持 terminal attach、
  resume、fork、review、close 等操作。

## 端口行为

- hub 默认端口：`21893`
- 手动 agent 示例端口：`7879`
- UI 部署远程 agent 时，可以自动选择远程空闲 agent 端口。
- UI 部署远程 agent 时，如果没有指定 `local_port`，也会自动选择本地 tunnel 端口。

hub 或手动 agent 启动时，如果端口冲突，不会自动换端口，会直接失败退出。可以改端口：

```bash
LUCID_PORT=21894 bash run.sh
```

## 注意事项

最好先配置好 SSH 密钥登录，再部署远程 agent。直接使用 SSH 密码部署时，密码可能会
以明文保存到：

```text
~/.lucid/ssh-history.json
```

该文件权限会设置为 `0600`，但内容仍然是明文。生产环境或多人共享机器上，建议先配置
SSH key，并避免保存密码。

默认远程安全策略：

- agent 只绑定 `127.0.0.1`；
- hub 通过 SSH 本地端口转发访问 agent；
- 远程操作只能通过本地 hub 或转发后的本地 agent 端口访问；
- 支持通过 node config 和 agent env 配置可选 bearer token 鉴权。
