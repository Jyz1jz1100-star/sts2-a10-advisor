# STS2MCP 保存运行身份调查（2026-09-03）

## 结论

已证实主菜单显示 `continue` 而 compendium 返回 `current_run: null` 的根因是
生命周期语义，不是保存路径或 JSON Schema 错误。上游和本地
`McpMod.Compendium.cs` 的 `BuildCurrentRunContext` 在
`RunManager.Instance?.IsInProgress != true` 时立即返回；只有已加载的运行才进入
`ResolveCurrentRunPath` 并读取 `current_run.save`。

本机只读观察同时证明活动档案解析到了 `modded/profile1/saves/progress.save`，
对应的 `modded/profile1/saves/current_run.save` 存在且为有效 JSON；根目录下的
vanilla `profile1` 不是当前 modded 档案。主菜单状态由 `NMainMenu` 的
`continue` 按钮可见/启用决定，所以两个观测并不矛盾。

## 边界与修改

保持 `current_run` active-only，避免把磁盘上可能过期的文件误绑定成 live run。
候选源码 `third_party/STS2MCP-main/McpMod.Compendium.cs` 增加独立的
`saved_run` block：

- 只在 `RunManager` 未加载运行、活动 profile 的 `current_run.save` 存在时出现；
- 提供 profile/save-scope、文件名、标准模式/ascension、时间、act/schema/platform、
  原始 `rng.seed` 与 `{save_scope}:profile{profile_id}:{start_time}` `run_id`；
- 不返回账号根路径；解析失败只保留通用错误标记，消费者必须 fail closed；
- 明确声明磁盘存在不等于 UI 允许 Continue。

`bridge/autoplay.py` 仅在 `current_run` 没有 active marker 时消费
`saved_run.is_saved=true`。既有 standard/A10/单人 guard 保持，Continue POST 前
要求可验证身份，POST 后首个非菜单 live state 还会重新 GET compendium，要求
active `current_run`，通过 `merge_verified_run_identity` 合并并把已验证的 run
ID/seed 与 live/active-save 字段逐项比较；冲突或 active block 缺失不会使用旧
`saved_run` fallback，也不会重试、abandon 或另起 run。

## 证据与验证

- 上游参考：[STS2MCP](https://github.com/Gennadiyev/STS2MCP) 的 compendium 文档
  将 `current_run` 描述为运行 active 时提供的块，并说明 main-menu 可用；
- live GET 只读观察：`singleplayer` 为 main menu，options 含 `continue` 和
  `abandon_run`；同一时刻 compendium 的 `profile_id` 为 1、`current_run` 为
  `null`；未发出 POST，未切 profile，未启动游戏进程；
- 复核时发现既有 live lock drift：lock 记录的 DLL SHA-256 为
  `095EE091F6D20D17FC0FC09AF53B46DE092F95B29FB970FF0D8714D14CC5B124`，
  游戏 mods 路径实际读取到 `CD3EA7409F5AC6973DF3D1B4A5EDC0D5C9A1B6555A8C448F276824D9CB943A4D`；
  manifest hash 仍匹配、health 仍为 v0.4.0。该漂移不是本轮构建造成的，本轮没有
  修复、覆盖或更新 lock；任何后续 smoke 必须先由主审处理这一前置条件；
- 新增测试覆盖：`current_run=null + saved_run` 可通过 Continue 身份门禁；缺少
  `run_id`/seed、保存解析错误、以及 active `current_run` 与 `saved_run` 同时出现
  都 fail closed；既有 active identity 与 post-Continue 冲突测试保持；定向
  autoplay/bridge/seed 合约套件为 46/46 通过，项目纯合约套件为 278/278，
  可选训练环境套件为 99/99；
- staging 编译成功（.NET 9.0.317，0 warning / 0 error）。候选输出目录为
  `artifacts/sts2mcp-save-identity/`；DLL SHA-256 为
  `AE2DC1697370AD2570C4D1A79726EB1762827EF74D1855114645684375693C5F`，manifest
  SHA-256 为 `F64EE11EBFA9F3CC9DFA89D99FA6125C594E3DE4A46AC5E1776327F7A80AA3C9`。
  候选仍未安装。
- 已安装 DLL、游戏目录、存档和 `config/live_version.lock.json` 均未由本轮修改；
  但上述 live DLL/lock hash drift 已如实记录。

## 后续安全 smoke 路径（需另行人工授权）

1. 只在新 staging 输出目录构建候选 DLL，核对 DLL/manifest hash 与源码，不写入
   游戏 `mods`，不更新 live lock。
2. 用 fixture 验证三种响应：active run 只出 `current_run`、主菜单可恢复存档出
   `saved_run`、缺失/损坏存档 fail closed；跑 Python 合约测试。
3. 如需真机验证，先在可恢复的专用 profile/全新测试存档上人工确认目标，记录
   pre-Continue `saved_run` identity；只允许一次 `menu_select=continue`，不使用
   unknown existing save，不发 `abandon_run`，随后必须匹配首个 live identity。
4. 只有验收通过并获得明确部署授权，才考虑复制候选到游戏 mods、更新 lock；本轮
   不执行这些动作。
