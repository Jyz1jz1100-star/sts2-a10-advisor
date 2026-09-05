# 真机桥接与 Trace 录制

当前验收版本固定为 Steam `buildid 24724944`、游戏 `v0.111.0`
（commit `41cef1ea`，`public-beta`）和 STS2MCP `v0.4.0`。模组 DLL 与
manifest 也按 SHA-256 锁定，防止同版本号的不同构建混入。锁文件在
`config/live_version.lock.json`。版本不一致时控制器会在任何动作前终止。

STS2MCP 的本地协议是：

- `GET /`：健康检查；
- `GET /api/v1/singleplayer?format=json`：读取原始单人状态；
- `GET /api/v1/compendium`：读取活动档案与当前运行保存（只读）；
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

## 真实 `run.seed` 读取边界

部分 STS2MCP 版本的 `singleplayer` state 会把活动运行的 `run.seed` 留空。
在真正加载运行时，项目的只读 `BridgeClient.get_state()` 会再读取
`GET /api/v1/compendium` 的 `current_run.seed`；该字段由 STS2MCP 从活动的
`current_run.save` 读取。客户端把原始 seed 和 `current_run.save` 来源标记合并
进返回 state，绝不 hash、猜测或伪造 seed；compendium 不可用时仍返回原始空值，
由 fixed-seed runner fail closed。

### 主菜单 Continue 与 `current_run: null`

上游 `BuildCurrentRunContext` 的语义是 active-only：当
`RunManager.IsInProgress` 为 false 时，它在解析文件路径之前直接返回 `null`。
因此主菜单可以同时暴露启用的 `continue` 按钮和
`GET /api/v1/compendium` 的 `current_run: null`；这不是 `profile1` 路径或
`current_run.save` Schema 不匹配。主菜单的 `continue` 由 `NMainMenu` 的按钮
可见且启用来决定，不能由 `current_run` 是否为 null 推断。

源码候选在保持上述 `current_run` 契约不变的前提下，另加
`saved_run`：仅当运行未加载且当前活动档案的 `saves/current_run.save` 存在时，
返回 `is_saved=true`、`profile_id`、`save_scope`、不含账号根目录的文件名、
保存元数据、原始 seed 和派生 `run_id`。磁盘存在不是 UI 动作保证；继续控制器
仍必须先确认菜单含 `continue`，验证 standard/A10，再 POST 一次 Continue。首个
非菜单 live state 到达后，控制器还要重新 GET compendium，要求 active
`current_run`，通过 `merge_verified_run_identity` 将其与 live state 合并，并逐项
校验 pre-Continue 的同一 `run_id`/seed、Ironclad、单人和 A10。任何身份缺失或
冲突都 fail closed，绝不使用旧 `saved_run` 继续动作、abandon、切换 profile、
启动新 run 或覆盖未知存档。
该扩展已在独立 staging 目录编译，但尚未替换已安装 DLL 或版本锁。

当前预注册批次仍是整数 partition。runner 只把规范十进制字符串作为同一整数
partition 的比较键；游戏常见的字母数字 seed（例如 `2450ZAR9EF`）会原样保留，
但不会被强行转换，因此在现有整数 partition 下仍只能走 observational。已有
当前已安装 DLL 的 `menu_select(seed=...)` 入口只对暴露真实 seeded flow 的
lobby 生效；标准单人 character select 仍明确拒绝该参数并且不启动 run，不能
用已安装桥接宣称 fixed-seed。源码副本已加入一个候选 v0.111.0 分支：标准单人
会走公开 `NCharacterSelectScreen.BeginRun`，并在控制器层回读
`current_run.seed`；该 seeded 候选与本次 saved-run 扩展都未安装或写入版本锁。

### 安装候选桥接前验收

本轮没有覆盖已安装 DLL，也没有启动正式批次。源码候选位于
`third_party/STS2MCP-main/McpMod.Actions.cs`；若以后更新桥接 DLL，必须先在
staging 输出目录构建并完成：

1. `dotnet build` 成功，且输出目录不是游戏 `mods` 目录；
2. 检查候选 DLL/manifest 的版本、路由和 SHA-256，再更新对应 lock；
3. 用官方 compendium fixture 验证 `current_run.is_in_progress`、原始 `seed`、
   `run_id` 及缺失/损坏 save 的 fail-closed 行为；
4. 运行 Python 回归套件并做一次只读 health/state/compendium smoke；
5. 只有人工确认上述结果后才复制到游戏 mods，并重新执行版本锁与健康检查。

对固定 seed 的 guarded start 可在候选 DLL 安装并更新 lock 后使用：

```powershell
.\.tools\python\cpython-3.12.14-windows-x86_64-none\python.exe -m bridge.trace_controller `
  --allow-actions start-ironclad-a10 --seed 1600000000
```

控制器会保留传入的原始字符串，不做整数转换；只有 authoritative
`current_run.seed` 与游戏 canonical seed 一致时才返回成功。
启动后的 `run_identity` 会把已验证 live state 的 `run`/`player` 与
compendium `current_run` 合并，记录 `run_id`、`seed`、`character`、
`ascension`、`game_mode`（及可用的 `save_scope`）；同一字段冲突或两边都
缺少 run_id 会 fail-closed。刚创建运行时 save 尚未落盘的短窗口只在 guarded
timeout 内重试，非空 seed mismatch 立即停止。

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
