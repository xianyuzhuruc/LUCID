# LUCID — 跨服务器终端监控与告警面板

<div align="center">
  <a href="README.md">English</a> | <a href="README.zh-CN.md">中文</a>
</div>

LUCID 是一个 Web 管理面板，让你在单个浏览器页面中**同时监控所有服务器上的
Claude Code、OpenAI Codex 和 Bash 会话**。它实时观察每个终端的输出，自动检测
会话是**正常运行**、**需要你的输入**还是**已经卡住**，并将告警信息置顶显示，
确保你不会错过任何权限确认或异常报错。

如果你在本地和远端服务器上同时运行着多个 AI Agent 会话，LUCID 能让你一眼看清
哪些在忙、哪些卡住了、哪些在等你操作，并支持一键切入任意会话。

---

## 功能概览

### 实时终端监控

每个会话卡片清晰展示：
- **名称**、**平台**（Claude / Codex / Bash）、**工作目录**
- 带颜色圆点的**分诊状态**（Working / Waiting / Stalled / Completed / Bash）
- 空闲时长与进程 PID
- 涉及的 Skills 和 Memory 操作
- 可展开的详情面板，包含完整元数据和操作按钮

### 智能状态分类

LUCID 将每个终端划分为五种状态：

| 状态         | 颜色   | 含义 |
|-------------|--------|------|
| **Working** | 绿色   | 会话正常工作中。 |
| **Waiting** | 红色   | 会话需要你处理 — 交互确认或错误。 |
| **Stalled** | 琥珀色 | 一段时间没有新输出。值得检查。 |
| **Completed** | 蓝色 | 你手动标记为已完成。不参与告警和状态检测。 |
| **Bash**    | 灰色   | 普通 Shell 会话 — 不参与提示符检测。 |

### ATTENTION 告警栏

需要你关注的会话出现在页面顶部的状态栏中。每个条目显示终端名称和颜色状态
圆点。点击任意条目可直接打开对应终端，全屏模式下也能无缝切换。

Zoom 进入某个会话后，该会话会暂时从告警栏中隐藏，让你专心工作。

### 全功能终端模拟

点击任意会话卡片的 **Terminal** 按钮，即可在页面内打开实时终端。你可以在
终端中直接敲键盘、复制粘贴、发送控制序列（Ctrl-C、Enter）、滚动浏览历史，
以及从侧边栏拖拽文件路径到终端中。

在已打开的终端中点击 **New Bash**，可以用当前目录启动一个 Bash。这个 Bash
不会新建会话卡片，而是作为 **Editor** 标签页打开，与文件编辑窗口统一管理。

### 文件浏览器与文本编辑器

- **浏览**任意已连接服务器上的目录。
- **上传**本地文件到远端服务器。
- **新建**文件夹。
- **编辑**文本文件，带有修改标记。
- **删除**文件。
- **管理 Bash 标签页**：从终端启动的新 Bash 会和编辑器标签页放在一起。

Zoom 模式下可在文件浏览器和终端列表之间切换，点击列表中的终端即可直接跳转，
无需退出全屏。

### Zoom 全屏模式

点击 **Zoom** 将终端面板扩展至整个浏览器视口。点击 **Unzoom** 或 **×** 返回
正常布局。

### 多服务器架构

LUCID 能够访问**任何可通过 SSH 连接的服务器** — 不需要特殊的防火墙规则，
不需要 VPN，不需要开放公网端口。只要能 `ssh user@host`，LUCID 就能管理
那台机器上的会话。

每台服务器上运行一个轻量 Agent，通过 SSH 隧道与 Hub 通信。在 Nodes 面板中
一键部署和更新。

### 节点管理

打开 **Nodes** 面板查看所有已配置的服务器。每个节点卡片展示名称、连接细节、
健康状态和窗口数量。

- **Deploy / update agent** — 填写 host、user 和密码（可选）。如果提供了
  密码，LUCID 会现场生成 SSH 密钥、将公钥拷贝到远端，然后切换到密钥认证。
  密码**绝不**写入磁盘。
- **Sync all nodes** — 一键重新部署运行时到所有 SSH 节点。
- **Delete** — 仅删除本地配置，不动远端服务器。
- **Remove** — 杀掉远端 Agent 并清除其文件，然后移除本地配置。不可逆；
  需要能够 SSH 到该节点，并会拒绝 `/`、`$HOME`、系统目录等明显危险路径。
- **点击节点卡片**可直接选中该节点作为 Launch 面板的默认目标。

### 启动受管会话

在 Launch 面板中可以在任意节点上启动 **Codex**、**Claude** 或 **Bash**
会话。可通过内置路径浏览器选择工作目录，可选命名终端，也支持自定义启动命令。

会话会自动找到通过 **nvm**、**bun** 等版本管理器安装的工具，无需手动配置
PATH。

### 会话操作

每个受管会话支持以下操作：

| 操作         | 说明 |
|-------------|------|
| **Focus**   | 附着到 tmux 会话（返回 attach 命令）。 |
| **Terminal** | 打开嵌入式终端进行实时交互。 |
| **Close**    | 终止受管进程。 |
| **Resume**   | 从上次中断处继续之前的 Claude/Codex 会话。 |
| **Fork**     | 克隆会话，继承对话历史创建新会话。 |
| **Complete** | 标记会话为已完成（排除出告警和状态检测）。 |
| **Start**    | 重新激活已完成的会话。 |
| **Review**   | 后台审查会话最近的工作并返回判定结果。 |
| **Rename**   | 给会话起一个易记的显示名称。 |

历史记录中也有对应的 **Focus / Resume** 和 **Fork** 按钮。

### 会话历史与全文搜索

- **搜索**所有节点的转录内容。
- **按节点过滤**仅查看某台服务器的历史。
- **Refresh** 按钮重新扫描新转录。
- 分页浏览，可配置每页数量。
- 每条记录展示节点、平台、首次输入、模型、日期、大小和 Skill / Memory 标签。
- **匹配片段**高亮显示搜索词在转录中出现的位置。

点击任意历史条目可打开其 **Timeline** — 以可滚动时间线形式展示用户消息、
助手回复和工具调用。

头部全局 **Search** 框跨所有节点搜索转录，返回命中摘录及上下文行。

### 知识总览

面板展示从 Agent 会话中提取的知识产物：
- **Skills** — 哪些 Skill 被调用、调用次数及涉及多少会话。
- **Memories** — 哪些 Memory 文件被读取或写入，可展开查看完整内容。
- **Plans** — Plan 历史版本，带差异对比和可复制内容。

点击时间线中的 Skill 标签可跳转到对应调用事件。点击 Memory 标签可查看完整内容。

### 深度链接

在面板 URL 后添加 `#search=<关键词>` 或 `#timeline=<session_id>` 即可直接
定位到搜索结果或特定会话时间线。

---

## 快速开始

### 前置条件

- Python 3.10+
- tmux
- Bash（Linux / macOS）或 Windows 上的 `run.bat`

### 启动 Hub

```bash
git clone https://github.com/tianyilt/LUCID
cd LUCID
bash run.sh
```

在浏览器中打开 **http://127.0.0.1:21893**。

`run.sh` 会自动创建虚拟环境、安装依赖并启动服务。使用其他端口：

```bash
LUCID_PORT=21894 bash run.sh
```

如需后台运行 Hub：

```bash
nohup bash run.sh > run.log &
```

### 添加本地机器

Hub 默认不监控任何会话。要纳入本地机器：

1. 打开面板 → **Nodes** 面板。
2. 点击 **Enable local node**。

### 启动受管会话

从 Nodes 面板的 Launch 区域启动，或从命令行：

```bash
python -m core.terminal.runner run claude --tmux --cwd /your/project --display-name "my-task"
python -m core.terminal.runner run codex --tmux --cwd /your/project
python -m core.terminal.runner run bash   --tmux --cwd /your/project
```

### 部署远端 Agent

LUCID 可以部署到**任何 SSH 可达的服务器**，无需额外配置防火墙或 VPN。

可以先配置 SSH 密钥（可选 — 如果提供密码，LUCID 会在部署过程中自动完成
这一步）：

```bash
ssh-copy-id user@server
ssh user@server   # 验证免密登录
```

然后在 Nodes 面板中部署：

1. 填写 **node id**、**host**、**SSH user** 和 **SSH port**。
2. 仅在首次配置密钥时填写 **password**。
3. **agent port** 留空 — LUCID 自动选择空闲端口。
4. **remote dir** 默认保持 `~/.lucid/agent`，除非需要自定义安装位置。
5. 点击 **Deploy / update agent**，并在部署状态区域查看队列和进度。

已保存的 SSH 连接会出现在 **Recent SSH** 列表中，点击即可快速填充表单。
更新代码后，如果远端 Agent 需要新的后端/API 能力，使用 **Sync all nodes**；
如果只是 Hub 或前端变更，只需重启 Hub 并刷新浏览器。

### 手动部署 Agent

如果你希望自行管理部署，在远端机器上启动 Agent：

```bash
cd /path/to/LUCID
LUCID_MODE=agent LUCID_AGENT_HOST=127.0.0.1 LUCID_PORT=7879 bash run.sh
```

在本地机器上建立 SSH 隧道：

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

## 安全性

- 所有远端访问均通过 SSH 进行，与你日常登录服务器的方式一致。
- **所有连接使用 SSH 密钥。** 如果在初始部署时提供了密码，该密码仅用于一次
  SSH 连接，将本地公钥安装到远端。密码**永不**持久化到磁盘。
- 如果将 Hub 绑定到非回环地址，请在对外暴露前添加认证机制。

---

## 端口

| 组件          | 默认端口   | 说明 |
|--------------|-----------|------|
| Hub          | `21893`   | 通过 `LUCID_PORT` 覆盖。 |
| 远端 Agent   | 自动选择   | UI 部署自动选择空闲端口。 |
| 手动 Agent   | 任意      | 任意空闲端口均可（如 `7879`）。 |

如果 Hub 默认端口已被占用，可通过环境变量指定其他端口：

```bash
LUCID_PORT=21894 bash run.sh
```

手动部署 Agent 时，请确保远端机器上的 Agent 端口和本地隧道端口均为空闲状态。

---

## Demo 模式

使用测试数据浏览面板（无需真实会话）：

```bash
python3 fixtures/seed.py
LUCID_HOME=fixtures/demo-home bash run.sh
python3 fixtures/seed.py --stop
```

---

## License

MIT — 详见 [LICENSE](LICENSE)。
