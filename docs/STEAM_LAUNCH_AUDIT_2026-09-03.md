# Steam / Slay the Spire 2 启动链只读审计（2026-09-03）

## 结论

本次阻塞发生在 Steam 客户端启动前，不能归因于候选 STS2MCP DLL。最可能是两个条件叠加：

1. Steam 客户端有一个已下载但尚未应用的客户端更新；
2. 冒烟进程由 CodexSandboxOffline token 发起，而 Steam 的实际登录档案属于 Administrator 的活动桌面会话。

“未登录”不是当前主要证据。账号缓存显示自动登录和记住密码已开启，历史日志也有成功登录记录；但 Steam 客户端必须在 Administrator 的真实交互桌面中完成一次正常启动/更新。一次性用户动作见下文。

## 只读证据

证据均为读取，不启动/停止应用，也未修改 Steam 或游戏配置。

| 项目 | 观察 | 判断 |
|---|---|---|
| 冒烟结果 | `smoke-20260903T033115Z/smoke_report.json` 为 `blocked / steam_session_unavailable`；bridge 未到达；seed smoke 未开始 | 阻塞在 launcher precondition |
| 直接 EXE | `godot.log` 报 `Steam is running: False`、`Could not determine Steam client install directory`，并重试 2 次 | 直接运行 Steam 游戏 EXE 不是可靠的启动方式；必须由已运行的 Steam 拉起 |
| Steam 客户端 | 冒烟报告记录启动后只有一个 PID、没有 `steamwebhelper`；当前注册表 `HKLM...\Valve\Steam\SteamPID=0` | 没有进入正常 Steam client/IPC 状态 |
| Steam 日志 | `C:\Program Files (x86)\Steam\logs\bootstrap_log.txt`、`console_log.txt` 最后写入为 2026-09-02 23:04 左右，冒烟时段没有新记录 | `steam.exe -silent` 没有到达正常客户端日志链；报告中的 timeout 判断成立 |
| 客户端更新 | `package\steam_client_win64` 版本 `1788291500`；已安装 manifest/注册表版本 `1785799196`。bootstrap 日志记录新 manifest、约 212,309 KB 下载完成，并反复出现 `existing pending version 1788291500` / `uninstalled manifest found` | 明确存在未应用的 Steam 客户端更新；应先在真实桌面让 Steam 完成更新/重启 |
| 账号状态 | `config\loginusers.vdf` 有缓存账号，`AutoLogin=1`、`RememberPassword=1`、`WantsOfflineMode=0`；`steamui_login.txt` 在 2026-08-31、2026-09-02 有 `Received logon success response` | 没有“密码缺失/必须重新登录”的证据；网络或 Steam Guard 仍可能在真实启动时要求一次交互 |
| 执行身份 | 当前 shell 的 `whoami` 为 `...\CodexSandboxOffline`（SID 1003），而 `query session` 显示活动 console 为 Administrator；环境变量却指向 `C:\Users\Administrator` | 用户目录/登录 token 不一致，足以使 Steam 单实例、IPC 和登录档案不绑定到活动桌面；中高置信度风险 |
| 路径与权限 | HKLM Steam 安装路径为 `C:\Program Files (x86)\Steam`；游戏 EXE 和 appmanifest 存在；游戏 build `24724944`、branch `public-beta`、`StateFlags=4`、`TargetBuildID=0`。Steam 目录对 Users 有 FullControl，游戏目录对 Authenticated Users 有 Modify | 未发现路径不存在、分支错误或 ACL 导致的启动阻塞 |
| 游戏版本 | `G:\SteamLibrary\steamapps\common\Slay the Spire 2\SlayTheSpire2.exe` 存在，版本日志为 v0.111.0 / commit `41cef1ea` | 与验收锁一致；不是候选桥接的版本问题 |

## 证据归属注意

`runs/sts2mcp_staging/smoke-20260903T033115Z/godot.log` 是本次失败的日志。报告中的 `godot-direct-launch.log` 虽然文件名看似本次日志，但其内容包含 `Steamworks initialization succeeded`，内部 Godot 时间为 2026-09-02 19:26:21，并对应 Steam 历史 console 中 19:26 的成功启动记录；它不能单独作为本次失败/成功的有效证明。冒烟采集器应保存原始日志路径、文件 hash 和内部启动时间，并要求其与本次 attempt 时间一致。

## 排除项与置信度

- 高：直接 EXE 在 Steam 未运行时失败；这解释 `godot.log` 的 Steamworks 错误，不涉及候选 DLL。
- 高：Steam 客户端处于“已下载待应用更新”状态，不能继续把当前机器当作健康 launcher。
- 中高：CodexSandboxOffline 与 Administrator 活动桌面/配置档案不一致，是 `-silent` 只有一个进程且没有 webhelper 的最可能运行环境原因。
- 低：纯粹的“未登录”或游戏更新 pending。缓存登录成功；游戏 appmanifest 的 TargetBuildID 为 0，当前 build 与验收目标一致。
- 低：文件权限、Steam 安装路径或 public-beta 分支错误。

## 唯一必要的用户动作

请用户在实际 Windows Administrator 活动桌面中手动打开 Steam 一次，并等待 Steam 客户端更新完成、自动重启后保持客户端运行。若出现 Steam Guard/登录窗口，只需在这一次启动中完成确认；现有缓存状态表明通常不需要重新输入密码。

之后自动化可以从同一活动会话调用：

```text
"C:\Program Files (x86)\Steam\steam.exe" -applaunch 2868840
```

不要直接运行 `SlayTheSpire2.exe`，也不要把 CodexSandboxOffline 进程的 `steam.exe -silent` 当作 Steam 已就绪。开始桥接 smoke 前应确认：`steam.exe` 与 `steamwebhelper.exe` 同时存在、Steam 日志有本次启动的新时间戳、游戏日志出现 `Steamworks initialization succeeded`，再确认 STS2MCP `127.0.0.1:15526` listener、state/compendium 响应。

如果 Steam 必须每次重新登录或弹出 Steam Guard，则该交互无法由当前无凭据的自动化安全地绕过；应把自动化运行器部署到 Administrator 的已登录活动会话中，而不是尝试伪造 Steam 登录状态。

## 对项目的直接建议

1. 把 Steam client update pending 和 `steamwebhelper`/新日志时间戳设为 launcher precondition；未满足时直接 fail closed。
2. 启动器使用同一活动用户会话的 `steam.exe -applaunch 2868840`，不再先启动直接 EXE 作为主路径。
3. 把“发起命令的 token、活动 console 用户、SteamPID、webhelper PID、日志启动时间和 hash”写入 smoke manifest，避免把旧的 `godot` 日志重命名后误归因。
4. Steam 客户端更新完成且真实会话健康后，再做候选桥接 health/state/compendium smoke；本次未产生任何 fixed-seed 或胜率证据。
