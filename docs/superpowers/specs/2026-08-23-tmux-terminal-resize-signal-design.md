# tmux 终端尺寸通知修复设计

## 背景

LUCID 浏览器终端会通过 xterm FitAddon 计算 `cols`/`rows`，再经 WebSocket 把 `resize` 消息发送给 Hub 或 Agent。现有后端收到消息后只调用 `TIOCSWINSZ` 更新 PTY；tmux attach 进程使用 `start_new_session=True` 启动，没有把该 PTY 注册为控制终端，因此不会自动收到内核的 `SIGWINCH`。实测中 PTY 已从 `80x24` 变为 `137x41`，tmux 仍保持 `80x24`；手动发送 `SIGWINCH` 后 tmux 立即同步。

## 目标

- 普通 Terminal 和 Editor 内嵌 New Bash 在浏览器、面板、Zoom/Unzoom 或侧栏尺寸变化后同步调整 tmux 行列数。
- 保持现有 WebSocket `resize` 消息格式不变。
- Reconnect 后使用当前可见容器尺寸，不继续沿用旧尺寸。
- 进程已经退出或信号发送发生竞态时不导致 WebSocket 会话崩溃。

## 方案

新增一个后端尺寸同步函数，顺序执行：

1. 使用现有 `_set_pty_size()` 安全裁剪并更新 PTY 行列数。
2. 若 tmux attach 子进程仍存活，向其独立进程组发送 `SIGWINCH`。
3. 忽略进程刚好退出导致的 `ProcessLookupError`，但保留 PTY ioctl 本身的真实错误。

WebSocket 的动态 `resize` 分支统一调用该函数。初始尺寸仍在启动 tmux attach 前写入 slave PTY，因为子进程启动时会直接读取该尺寸。

前端保留已实现的 `ResizeObserver` 与尺寸签名去重，同时让 FitAddon 缺失或 `fit()` 失败变得可见，并在布局切换后的多个动画帧内重试。普通 Terminal 与 New Bash 使用相同的策略；WebSocket 打开时强制发送最终尺寸。

## 测试

- 使用独立 tmux socket 创建临时会话，以与生产相同的 `start_new_session=True` 和 PTY attach 方式启动客户端。
- 调用新的尺寸同步函数，把 PTY 从 `80x24` 调整为 `137x41`，断言 tmux window 最终也是 `137x41`。
- 测试退出中的 attach 进程不会因发送 `SIGWINCH` 的竞态抛错。
- 运行完整 Python 测试、JavaScript 语法检查，并确认没有遗留临时 tmux 会话。

## 部署范围

该逻辑同时运行在 Hub 本地终端入口和每个 Agent 的终端入口。源码修复后，当前 Hub 需要刷新/重启，已经部署的本地及远端 Agent 需要同步并重启才能使用新逻辑。
