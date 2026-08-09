# New Bash 终端响应式布局设计

## 背景

LUCID 的 `New Bash` 会把一个 Bash tmux 会话作为 Editor 标签页打开。后端 PTY/WebSocket 已支持通过 `resize` 消息更新终端的 `rows` 和 `cols`，但前端 New Bash 当前复用了通用 `.terminal-frame` 的固定最小高度，并且浏览器窗口 resize 处理主要面向普通会话终端。因此 New Bash 在窗口、Zoom 状态或侧栏尺寸变化后可能继续使用旧的显示尺寸。

## 目标

- New Bash 终端填满 Editor 内容区的可用宽高。
- 浏览器窗口宽高变化时，xterm 视图和后端 Bash PTY 同步调整。
- Zoom/Unzoom、侧栏展开/收起以及标签页切换导致的布局变化也能触发调整。
- 保持普通会话终端现有行为和后端 WebSocket 协议不变。
- 终端关闭或标签页切换时清理 resize 观察器和防抖定时器。

## 非目标

- 不修改 PTY 创建、tmux 会话创建或 WebSocket `resize` 消息格式。
- 不重新设计 Terminal/Editor 的页面布局。
- 不为该问题引入新的前端依赖。

## 方案

采用 CSS 与 resize 生命周期结合的前端修复，范围限定在 `static/index.html`。

### 布局

为 Editor 中的 New Bash 容器添加独立的响应式样式类，覆盖通用 `.terminal-frame` 的 `min-height: 30rem` 固定约束，同时保留 `flex-1 min-h-0 min-w-0`。这样终端会使用父级 Editor 面板分配的空间，在小窗口下不会被固定最小高度撑开，在大窗口下也不会停留在固定尺寸。

### 尺寸同步

保留 New Bash 终端已有的 `ResizeObserver`，并将其回调统一到一个经过防抖的 Editor 终端适配流程：

1. 调用 FitAddon 的 `fit()`，让 xterm 根据容器像素尺寸重新计算行列数。
2. 读取 xterm 的 `cols` 和 `rows`。
3. WebSocket 已连接且行列签名发生变化时，发送现有格式的 `{ type: 'resize', cols, rows }` 消息。

同时扩展现有的窗口/方向变化处理，使它在普通终端和 New Bash 终端存在时分别调度对应的适配流程。Zoom/Unzoom 和侧栏开关沿用同一调度入口；标签页切换后的 `$nextTick` 适配确保 xterm 只在容器已可见并完成布局后计算尺寸。

### 生命周期与清理

Editor 终端增加独立的 resize 防抖定时器状态。关闭 Bash 标签页、切换到其他 Editor 标签页、关闭终端面板时，清除定时器、断开 `ResizeObserver`，并保持现有 WebSocket/xterm 清理逻辑。

## 数据流

```text
浏览器/面板尺寸变化
        │
        ├─ window resize / orientationchange
        ├─ Zoom 或侧栏状态变化
        └─ Editor 终端 ResizeObserver
        │
        ▼
Editor 终端防抖适配
        │
        ├─ FitAddon.fit()
        └─ 读取 xterm cols/rows
                │
                ▼
WebSocket resize 消息
                │
                ▼
后端 PTY TIOCSWINSZ → Bash/tmux 使用新尺寸
```

## 错误处理

- FitAddon 不可用或 `fit()` 抛错时沿用现有安全忽略行为，不影响 WebSocket 输入输出。
- WebSocket 未连接时只更新 xterm 本地尺寸；连接建立后现有 `open` 回调会发送当前尺寸。
- 相同行列签名不重复发送 resize 消息，避免窗口拖动过程产生无意义的请求。
- 容器尚未可见或已被销毁时，适配流程安全返回；后续标签页切换或观察器回调会再次适配。

## 测试与验收

### 静态检查

- 检查修改后的内嵌 JavaScript 语法。
- 检查 Git diff，确认只涉及响应式 New Bash 终端相关布局、调度和清理逻辑。

### 浏览器验收

打开一个会话并点击 `New Bash`，验证：

- 初始终端填满 Editor 内容区，而不是固定为 80×24 或固定最小高度。
- 调整浏览器宽度和高度后，终端可视区域同步变化，Shell 的行列布局随之更新。
- 切换 Zoom/Unzoom、展开/收起左侧栏后，终端仍填满剩余区域。
- 切换到文件标签页再切回 Bash，终端尺寸正确且没有重复 WebSocket/resize 处理器。
- 关闭 Bash 标签页或终端面板后，不再触发旧终端的 resize 回调。

## 变更范围

- 新增本设计文档。
- 后续实现仅修改 `static/index.html`，不修改后端代码或 API。
