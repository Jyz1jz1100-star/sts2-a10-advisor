# 真机桥接与 Trace 录制

当前验收版本固定为 Steam `buildid 24724944`、游戏 `v0.111.0`
（commit `41cef1ea`，`public-beta`）和 STS2MCP `v0.4.0`。模组 DLL 与
manifest 也按 SHA-256 锁定，防止同版本号的不同构建混入。锁文件在
`config/live_version.lock.json`。版本不一致时控制器会在任何动作前终止。

STS2MCP 的本地协议是：

- `GET /`：健康检查；
- `GET /api/v1/singleplayer?format=json`：读取原始单人状态；
- `POST /api/v1/singleplayer`：发送 `{"action": ...}`；
- 菜单导航使用 `{"action":"menu_select","option":"..."}`。

上游协议来源：[STS2MCP](https://github.com/Gennadiyev/STS2MCP)、
[MCP 工具表](https://github.com/Gennadiyev/STS2MCP/blob/main/mcp/README.md)、
[HTTP 路由源码](https://github.com/Gennadiyev/STS2MCP/blob/main/McpMod.cs) 和
[动作实现](https://github.com/Gennadiyev/STS2MCP/blob/main/McpMod.Actions.cs)。

## 默认只读探测

```powershell
.\.tools\python\cpython-3.12-windows-x86_64-none\python.exe -m bridge.trace_controller probe
```

该命令只发 GET，并把健康信息与主菜单原始 state 写入
`runs/live_traces/*.jsonl`。连续录制：

```powershell
.\.tools\python\cpython-3.12-windows-x86_64-none\python.exe -m bridge.trace_controller record --poll 0.25
```

每条 JSONL 都带 session、顺序号、UTC 时间、decision id 和未经裁剪的
state/action/result。v0.4.0 没有服务端 decision id，因此使用完整原始状态的
规范化 SHA-256；任何可见状态变化都会使旧动作失效。

## 明确授权后的动作

只有进程级 `--allow-actions` 才启用 POST：

```powershell
.\.tools\python\cpython-3.12-windows-x86_64-none\python.exe -m bridge.trace_controller `
  --allow-actions action --decision-id "local-sha256:..." `
  --json '{"action":"menu_select","option":"singleplayer"}'
```

`--decision-id` 可防止对已变化的界面执行过期动作。

## 战士 A10 开始门禁

项目的兼容构建在 STS2MCP v0.4.0 上补充了 `set_ascension`，并让角色选择
状态返回 `ascension` 与 `max_ascension`。控制器会选择 `IRONCLAD`、把进阶设为
10，再确认 Embark：

```powershell
.\.tools\python\cpython-3.12-windows-x86_64-none\python.exe -m bridge.trace_controller `
  --allow-actions start-ironclad-a10
```

如果本机档案尚未解锁 A10，桥接会在 Embark 前拒绝动作。Embark 后控制器
会再次读取真机状态，并要求 `player.character=IRONCLAD` 且
`run.ascension=10`；不匹配会立即报错并保留完整 trace。
