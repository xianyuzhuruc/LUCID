# LUCID — 跨服务器终端监控与状态告警面板

LUCID 是一个 Web 控制台，用来**在一个浏览器页面上集中监控多台服务器上的
Claude Code、Codex 和 Shell 会话**。它会实时观察每个终端的状态，自动检测
会话是否**卡住不动了**、是否**需要你确认**，并把需要关注的会话置顶提醒。

如果你同时在多台机器上跑着好几个 agent，LUCID 让你一眼就能看清：谁在干活、
谁卡住了、谁在等你操作 — 并且可以直接在面板上处理。

---

## 能做什么

### 跨服务器终端监控

- 一个面板看**所有 Claude Code 和 Codex 会话**，不管是本机的还是远程的。
- 每个会话实时显示当前状态：**工作中**、**等待你操作**、**已停滞**、**Bash 终端**。
- 通过 tmux 每隔几秒抓取终端尾部内容，不用登到每台机器上就能看到进度。
- 面板通过 SSE 实时推送更新，不需要手动刷新页面。

### 状态告警 — 不再错过需要你处理的情况

LUCID 把每个受管终端归类为三种状态：

| 状态 | 含义 |
|------|------|
| **Working（工作中）** | 会话正在持续输出内容，一切正常。 |
| **Waiting（等待中）** | 会话需要你介入 — 可能是权限确认、yes/no 询问、或者遇到了无法自动解决的错误。**该去看看了。** |
| **Stalled（已停滞）** | 会话终端不再产生新输出。可能卡住了、跑完了、或者在静默等待 — 值得检查一下。 |

检测原理：每隔 3 秒抓取一次 tmux pane 的最后 30 行，跟前一次对比：

- **内容变了** → Working，会话还在产出。
- **内容没变 + 包含 "yes" 和 "no"** → Waiting，很可能在问你 yes/no。
- **内容没变 + 包含 "error"** → Waiting，出错了可能在等你指示。
- **内容没变 + 没有特殊标记** → Stalled，没动静了。

此外，LUCID 还会读取 `/tmp/claude-focus.log` 中的权限事件（由 Claude Code
的通知 hook 产生），作为会话需要授权的补充信号。

等待中的会话会自动排到面板最前面，浏览器标签页标题也会显示待处理数量。

### 多服务器架构

```
浏览器
  |
  v
本机 LUCID hub :21893
  |
  +-- HTTP over SSH tunnel --> 服务器A agent (127.0.0.1)
  +-- HTTP over SSH tunnel --> 服务器B agent (127.0.0.1)
```

- **Hub** 跑在你的本机，提供浏览器界面，汇总所有节点的状态。
- **Agent** 跑在每台服务器上，读取本机的 tmux pane 和进程状态，上报给 hub。
- Agent 只监听 `127.0.0.1`，不暴露公网端口。Hub 通过 SSH 本地端口转发访问。
- 在 UI 上一键部署：填好服务器信息，点部署，hub 自动完成 SSH 配置、上传运行
  时、启动 agent、建立隧道，全部搞定。

### 支持的操作

对任意服务器上的任意会话，都可以直接操作：

| 操作 | 说明 |
|------|------|
| **Focus（聚焦）** | 把终端窗口/标签页切到前台（支持 Terminal.app、iTerm2、tmux）。 |
| **Terminal（终端）** | 查看实时终端输出，发送按键 — 不用 SSH 进去。 |
| **Close（关闭）** | 终止受管会话及其 tmux pane。 |
| **Resume（继续）** | 从上次中断的地方恢复 Claude/Codex 会话。 |
| **Fork（克隆）** | 基于已有会话的对话历史创建新会话。 |
| **Review（审查）** | 后台调用 Claude 审查会话最近的工作，给出 PASS / FAIL / PARTIAL 结论。 |
| **Rename（重命名）** | 给会话起个好记的名字。 |
| **Launch（启动）** | 启动新的 Claude、Codex 或 Bash 受管会话，可选在远程服务器上启动。 |

### 文件浏览器

在任意已连接的服务器上浏览、查看、上传、删除文件 — 全在面板里完成。查看
输出、拿日志、改配置，不用另开终端。

### 会话历史 & 搜索

- 浏览所有服务器上的 Claude 和 Codex 历史对话。
- 跨会话搜索，找到某个话题在哪里讨论过。
- 查看用户消息、助手回复、工具调用的时间线。

### 知识总览

- 查看从 agent 会话中提取的 **skills（技能）**、**memories（记忆）**、**plans（计划）**。
- 了解 agent 学到了什么、各个项目在进行什么工作。

---

## 快速开始

### 环境要求

- Python 3.10+
- tmux（受管终端会话需要）
- Linux/macOS 使用 Bash；Windows 使用 `run.bat`

### 启动 Hub

```bash
git clone https://github.com/tianyilt/LUCID
cd LUCID
bash run.sh
```

打开：

```
http://127.0.0.1:21893
```

`run.sh` 会自动创建虚拟环境、安装依赖、启动服务。换端口：

```bash
LUCID_PORT=21894 bash run.sh
```

### 添加本机节点

Hub 默认不监控任何会话。要把跑 hub 的这台机器也纳入监控：

1. 打开面板 → **Nodes** 面板。
2. 点击 **Enable local node**。

会在 `~/.lucid/nodes.toml` 中添加一个本地节点，面板上就能看到本机的受管会话了。

### 启动受管会话

可以通过面板的 Launch 功能启动，也可以命令行：

```bash
# 在 tmux 中启动 Claude 受管会话
python -m core.terminal.runner run claude --tmux --cwd /your/project --display-name "我的任务"

# 启动 Codex 受管会话
python -m core.terminal.runner run codex --tmux --cwd /your/project

# 启动 Bash 受管会话
python -m core.terminal.runner run bash --tmux --cwd /your/project
```

受管会话跑在 tmux 里，这样 LUCID 才能抓取终端输出、检测状态变化、提供远程终端操作。

### 部署远程 Agent

建议先配好 SSH 密钥：

```bash
ssh-copy-id user@server
ssh user@server   # 确认可以无密码登录
```

然后在 UI 上部署：

1. 打开面板 → **Nodes** 面板。
2. 填写：node id、host、SSH user、SSH port。
3. agent port 留空即可，LUCID 会自动选空闲端口。
4. 点击 **Deploy / update agent**。

Hub 会自动上传运行时、在远程启动 agent、建立 SSH 隧道、写入节点配置。之后
那台服务器上的受管会话就会自动出现在面板里。

### 手动部署 Agent

如果倾向于自己管部署，在远程机器上：

```bash
cd /path/to/LUCID
LUCID_MODE=agent LUCID_AGENT_HOST=127.0.0.1 LUCID_PORT=7879 bash run.sh
```

本机创建 SSH 隧道：

```bash
ssh -N -L 17879:127.0.0.1:7879 user@server
```

在 `~/.lucid/nodes.toml` 中添加：

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

## 安全说明

- 远程 agent 只绑定 `127.0.0.1`，不对外暴露。
- 所有远程访问走 SSH 本地端口转发。
- 可配置 bearer token 做节点级鉴权。
- **建议使用 SSH 密钥**部署 — 密码部署会把密码明文存在
  `~/.lucid/ssh-history.json`（文件权限 `0600`，但仍是明文）。
- 如果 hub 绑定了 `127.0.0.1` 以外的地址，加认证后再暴露给其他用户。

---

## 端口说明

| 组件 | 默认端口 | 备注 |
|------|---------|------|
| Hub | `21893` | 通过 `LUCID_PORT` 修改。 |
| 远程 agent | 自动分配 | UI 部署时自动选空闲端口。 |
| 手动 agent | `7879`（示例） | 任意空闲端口均可。 |

---

## Demo 模式

用测试数据体验面板，不需要真实会话：

```bash
python3 fixtures/seed.py
LUCID_HOME=fixtures/demo-home bash run.sh
python3 fixtures/seed.py --stop
```

---

## License

MIT — 详见 [LICENSE](LICENSE)。
