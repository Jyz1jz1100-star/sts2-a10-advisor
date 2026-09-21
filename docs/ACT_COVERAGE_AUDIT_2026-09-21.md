# 逐幕覆盖审计（2026-09-21）

本轮是交付目标纠偏：先审计，再修。本文只回答一个问题——**真实《杀戮尖塔2》完整三幕的自动执行链路，
哪一部分有证据，哪一部分只是模拟器里的近似，哪一部分还没有实现。**

证据基线：

| 锁 | 值 |
|---|---|
| 游戏 build | `v0.111.0` / commit `41cef1ea` / steam_build_id `24724944` / main_assembly_hash `222455745` |
| 已安装桥接 | STS2MCP `0.4.0`，DLL `CD3EA740…43A4D`（= `sts2mcp-seeded` 候选，`config/live_version.lock.json` 的 `install_provenance.reconciled: 2026-09-06`） |
| 局内求解器 | CombatSolver `0.43.1` / RitsuLib `0.6.2`（operator 所有，本轮未动） |
| 真实游戏权威源码 | `third_party/slay-the-spire-2-emulator-main/decompiled/`（游戏自身反编译程序集） |
| 真机 trace | `runs/solver_supervisor/ssb-20260920T051734Z-8ac69fbf`、`ssb-20260920T043631Z-eff91016`，两条均以 `--allow-actions --out-of-combat-only` 运行（`manifest.json` 的 `commands.autoplay`），即局外动作确由桥接器发出，不是人工 |

标记口径只有四种：**已实现且验证 / 已实现未验证 / 占位或近似 / 缺失**。
"没有证据"一律不写成"已完成"。

---

## 0. 锁定 build 究竟定义了什么叫"完整通关"

先前文档把"三幕"当成一个待商榷的范围问题。它不是。这个 build 里它是可以逐行读出来的：

- **三幕顺序 = Overgrowth → Hive → Glory。**
  `MegaCrit.Sts2.Core.Models/ActModel.cs:510-515`
  `GetDefaultList()` 返回 `new ActModel[3] { Act<Overgrowth>(), Act<Hive>(), Act<Glory>() }`；
  `MegaCrit.Sts2.Core.Runs/RunState.cs:227` 标准单人局直接实例化这份表。
- **`Underdocks` 不是第二幕，是第一幕的替代变体。**
  `ActModel.cs:498-507` 只在 `IsEpochRevealed<UnderdocksEpoch>()` 时改写 `list[0]`，从不写 `list[1..]`。
  真机侧对得上：同一条链路的 act 1 boss 出现过 `VANTOM_0`、`KIN_PRIEST_0`（Overgrowth）
  和 `LAGAVULIN_MATRIARCH_0`（Underdocks），而 act 2 boss 始终是 `THE_INSATIABLE_0`（Hive）。
- **每幕一个先古之民，且它是地图入口的固定节点，不是幕结束后的屏幕。**
  `Core.Map/StandardActMap.cs:334` `StartingMapPoint.PointType = MapPointType.Ancient;`；
  `ActModel.cs:345-348` 每幕从本幕候选里抽一个；`Core.Runs/RunManager.cs:909-916` 进这个节点时
  `PullAncient()` 而不是普通事件。候选集合：Neow（第一幕）／Orobas·Pael·Tezcatara（Hive）／
  Nonupeipe·Tanx·Vakuu（Glory），另有共享的 Darv 只会发给第二、三幕（`RunManager.cs:666-676`）。
- **`1+1+2` 就是 A10 的定义。** `DoubleBoss` 是 `AscensionLevel` 枚举第 10 项
  （`Entities.Ascension/AscensionLevel.cs:4-15`），而 `AscensionManager.cs:9` 写死
  `maxAscensionAllowed = 10`；`RunManager.cs:685-691` 是它唯一的消费点，且条件是
  `i == State.Acts.Count - 1`——只有终幕加第二个 boss。
- **第二个 boss 是 boss 节点之后多出来的一个地图节点。**
  `StandardActMap.cs:88-91` `SecondBossMapPoint = new MapPoint(col/2, rowCount + 1)`，
  `:231-234` `BossMapPoint.AddChildPoint(SecondBossMapPoint)`。
- **终幕 boss 不产出奖励集，第一个 boss 靠"终端结算屏→回地图"衔接。**
  `Rewards/RewardsSet.cs:67-74`：`RoomType == Boss && CurrentActIndex >= Acts.Count - 1` 直接返回空集；
  `NRewardsScreen.cs:473-482`：若 `SecondBossMapPoint != null` 且当前就在 boss 坐标，走
  `ProceedFromTerminalRewardsScreen()` 重新打开地图（`:1331-1332` `NMapScreen.Instance?.Open()`）；
  只有第二个 boss 的屏幕才落到 `ActChangeSynchronizer.SetLocalPlayerReady()`。
- **胜利 = 最后一幕结束后进入 `TheArchitect` 事件房。**
  `RunManager.cs:1207-1246`：`EnterNextAct` 在 `CurrentActIndex >= Acts.Count - 1` 时
  若 `currentRoom.IsVictoryRoom` 就 `WinRun()`，否则进 `EventRoom<TheArchitect>`，随后
  `TriggerVictory(); OnEnded(isVictory: true); GuaranteeKillAllPlayers()`。
  `Rooms/AbstractRoom.cs:20-29` `IsVictoryRoom => eventRoom.CanonicalEvent is TheArchitect`。
  **胜与负共用同一个 `NGameOverScreen`**，游戏自己用 `NGameOverScreen.cs:268`
  `bool win = _runState.CurrentRoom?.IsVictoryRoom ?? false;` 区分。
- **第三幕没有额外的每幕难度缩放。** `Entities.Ascension/AscensionManager.cs` 全文无按幕逻辑；
  `ToughEnemies`/`DeadlyEnemies` 是逐怪字面量。A10 给第三幕的唯一加成就是那个第二个 boss。
  → 先前"模拟器第三幕没有难度倍率所以比真机容易"这条范围声明，前提本身就偏了。

---

## 1. 真机路径（产品主链路）

| 必要流程 | 状态 | 源码位置 | 现有证据 |
|---|---|---|---|
| 开局进入第一幕 | 已实现且验证 | `bridge/trace_controller.py:704-712`（角色选择）、`bridge/autoplay.py:1216` `_start_run` | 两条 trace 各自起局，`run_identity` 事件 + `assisted` cohort；`act 1 floor 0→1` |
| 第一幕先古之民（Neow） | 已实现且验证 | `autoplay.py:767-787` `_event_choice`（NEOW 有专门分支） | `live_run_coverage_20260920.json`：`ancients {"1":"NEOW"}` 在 3 条 run 中出现 |
| 第一幕地图/普通/精英/boss | 已实现且验证 | `autoplay.py:209-213`，`live_candidate_codec.py:33-35` | boss 节点 `1:17` = `VANTOM_0` / `KIN_PRIEST_0` / `LAGAVULIN_MATRIARCH_0`，均清除 |
| 幕间切换（1→2、2→3） | 已实现且验证 | 无专用代码，由 `rewards` claim + `proceed` + 下一幕 `map` 承担（`autoplay.py:671-674`, `1218+`） | trace 记录序列 1258→1272：`proceed`(ok) → `act=3 floor=33 unknown` → `map` → `choose_map_node`(ok)。两批各 1 次，共 **2 次真机跨幕** |
| 第二幕先古之民 | 已实现且验证 | 同 `_event_choice`，走 `options[0]` 保守兜底 | `ancients {"2":"OROBAS"}`、`{"2":"PAEL"}`、`{"2":"DARV"}`——都是 Hive 的真实先古之民 |
| 第二幕 boss | 已实现且验证 | 同上 | `2:33` `THE_INSATIABLE_0`（Hive 的第一个 boss），两条 run 均清除 |
| 第三幕进入 | 已实现且验证 | 同上 | 两条 run `acts_seen` 含 3，最深 `floor 48` |
| 第三幕先古之民 | 已实现且验证 | 同上 | `ancients {"3":"TANX"}`——Glory 三选（Nonupeipe/Tanx/Vakuu）之一 |
| 第三幕普通/精英 | 已实现且验证 | 同上 | `40/44/46` 精英，含 `SOUL_NEXUS_0`（Glory 精英池），`3:40/44/46` |
| **第三幕第一个 boss** | **已实现未验证** | 遭遇路径通：`autoplay.py:209` 把 `boss` 交给求解器 | 遭遇过 `3:48 AEONGLASS_0`（Glory boss）**但两局都输在这里，0 次清除** |
| **第三幕第二个 boss** | **缺失（无证据）** | 真机侧不需要新代码（它是 `rowCount+1` 的普通 boss 节点），但本仓库从未观察到该节点 | `boss_battles_by_act_floor` 里**没有任何 `3:49`**；两条 run 都在第一个 boss 前/中途结束 |
| **终幕双 boss 之间的出口** | **缺失（无证据）** | `NRewardsScreen.cs:473-482` 是真实机制；桥接器只依赖通用 `proceed` 兜底 | 无 trace 到达该屏；未验证 |
| **最终胜利终局判定** | 已实现且验证（**败局分支**）；胜局分支已实现未验证 | `bridge/outcome.py` 读 `game_over.is_victory`；桥接侧 `McpMod.StateBuilder.cs` 已装 | 见 §8：真机终止屏实测 `is_victory=false`，读数来源 `bridge_is_victory_flag`。胜局分支需要真的清掉终幕第二个 boss，本项目在真机上还没有过一次 |

### 1.1 胜利终局是结构性不可观测，不是策略没打好

已安装的 STS2MCP 在 `McpMod.StateBuilder.cs:455-459` 把胜负两种结局压成同一份载荷：

```csharp
result["game_over"] = new Dictionary<string, object?>
{
    ["message"] = "Run ended.",
    ["options"] = new List<string> { "main_menu" }
};
```

字面量 `"Run ended."` 里既没有胜也没有负的词，因此 `bridge/outcome.py` 在这条链路上
**永远不可能返回"胜"**。本轮实测：把两条真机 trace 全量过一遍判定器，
`outcome` 全部为 `None`（不可判定），`outcome_source` 全部是 `game_over_message_wording`，
`victory_evidence_available = false`。

结论直接写清楚：**只要不装带胜负位的桥接，"真实整局胜率"这个数字在本项目里无法被观测到，
无论自动玩家变多强。** 这是当前头号阻塞项，且它是实现缺陷，不是策略强度不足。

本轮已交付修复（见 §3.2）：候选桥接 + 消费侧读取。
**2026-09-21 晚些时候 operator 授权更换桥接，已安装并在真机上验到终局读数**——见 §8。

### 1.2 一处先前被当作"阻塞"的事实其实是错的

三条近期提交（`a35f38c`、`daf89b0`、`df7…`）反复写"固定种子需要候选桥接（DLL 替换，禁区）"。
本轮实测：**候选桥接早就装着**——`config/live_version.lock.json` 的
`bridge.install_provenance` 记录 `candidate_id: sts2mcp-seeded`、`reconciled: 2026-09-06`，
`scripts/stage_sts2mcp.py` 只读预检回报 `target_hashes_match_lock: true`、
`guards.game_process_running: false`。二进制层面也确认：已安装 DLL 含
`BeginStandardSingleplayerSeededRun`、`SetSeed`、`Embarking on run (seed:`，**不含** `is_victory`。

所以 `--seed-mode fixed` 的桥接前提已满足，`docs/FIXED_SEED_FEASIBILITY.md` 的"尚未安装"已过期。
真正还缺的只有：一条 `fixed` 模式下的可审计整局，以及胜负位本身。

### 1.3 另一处先前结论也被代码否掉

`README.md:63-65` 原本写：预检（监督器 `--dry-run`）里包含"模组哈希、版本锁、种子分区"，
因此这道门不会和被验证的运行走岔。本轮审计：`supervise_solver_batch.py` 里
`VersionLock` 出现 **0 次**；`_attest_mods()` 的结果只被记录、不被判定；
`--dry-run` 走到 `run()` 就直接 `return EXIT_OK`。**这句话当时是假的。**
修复见 §3.3（把门禁真的做进预检，并如实说明哪一部分只报告不否决）。

---

## 2. 模拟器路径（`--campaign`，默认关闭）

| 项目 | 状态 | 源码位置 |
|---|---|---|
| 三阶段流程可达、可自动打完 | 已实现且验证 | `Core/Run/RunEngine.cs:1946 TryAdvanceCampaignAct`、`:2019 AdvanceToNextAct` |
| 第一幕内容 | 已实现且验证（真实内容） | `Core/Run/RunConstants.cs:111-120` Overgrowth 池 |
| **第二幕内容** | **占位/近似** | `Core/Run/RunMapGenerator.cs:19` `underdocks = state.Act != ActOvergrowth` → 第二幕用 **Underdocks** 池；真实第二幕是 **Hive** |
| **第三幕内容** | **占位/近似** | 同上，第三幕也用 Underdocks 池；真实第三幕是 **Glory** |
| 第二/三幕先古之民 | 缺失 | `RunEngine.cs:23` 开局 `RunPhase.Ancient` 一次；`AdvanceToNextAct`(`:2023`) 直接 `Phase = Map`。唯一"第二幕先古之民"是演示种子桩 `:2031`，`if (State.StringSeed != "7MS1YN8NWB") return` |
| 双 boss 之间的衔接 | 占位/近似 | `RunEngine.cs:1996-2012 StartFinalActBoss`：`Floor++` 后直接 `StartCombatWithDeck(...)`，无奖励屏、无地图、无回血。真实机制是回地图、走到 `SecondBossMapPoint` |
| 配对 boss 的选取 | 占位 | `:1984 var pool = UnderdocksBossEncounters.ToArray(); :1989 pool[(i+1)%pool.Length]`——就是第二幕 boss 池的下一个下标 |
| `campaign_cleared` 语义 | 已实现（近似语义） | `:1963-1965 RunCleared = PlayerHp > 0`，经 `info[11]` → `run_env.py:172` → `evaluation.py:121` |
| 每幕难度缩放 | 与真机一致地"没有" | 真机也无每幕缩放（§0），故此项**不算缺陷**；先前"模拟器第三幕偏容易"的说法前提有误 |

**关键发现：真实二/三幕内容在模拟器里已经写好，只是没有任何池引用它。**
本轮独立复算（不引用二手结论）：

- `CombatFactory.cs` 的 `ActOneEncounter` 共 87 项，其中 Hive/Glory 内容的下标是
  `66 SoulNexus, 70 Knights, 71 MechaKnight, 73 Aeonglass, 75 KaiserCrab, 76 KnowledgeDemon,
  78 Queen, 80 TestSubject, 81 TheInsatiable`；
- `RunConstants.cs:111-120` 所有池引用的下标集合里 **一个都没有**；
- 而真机 trace 里 act 2 boss 正是 `THE_INSATIABLE_0`(81)、act 3 boss 正是 `AEONGLASS_0`(73)。

也就是说，模拟器缺的不是内容、不是引擎能力，而是**一张把已有遭遇接到第二、三幕的池表**
（外加每幕先古之民、双 boss 之间的地图出口，以及 Glory/Hive 的事件与遗物映射）。
本轮**没有**动这张表：动它会让既有 `campaign_clears` 数字换人群，属于下一轮显式版本化的工作，
不该和本轮的定性纠正混在一次提交里。

### 2.1 需要撤回的结论

`README.md:208`（2026-09-20 段）：*"所以'能不能走完三幕'从现在起是策略强度问题，不再是表达能力问题。"*

**这句要撤回。** 它依据的是"模拟器里能走到 floor 50"，而那个 floor 50 站在
Underdocks 池上。模拟器至今不能表达 Overgrowth→Hive→Glory，也不能表达每幕先古之民与
双 boss 之间的地图出口。所以三幕**覆盖**仍然是表达能力问题；只有"三阶段流程能否自动跑完"
才是策略/可达性问题。本轮已按此改写 README，并新增 §3.4 的环境版本与内容覆盖字段。

历史结果一律不追改：旧指标、旧 `campaign_clears` 语义、旧证据文件原样保留；
新产物靠 `environment_version` / `content_coverage` 区分（缺失即视为 approx-v0）。

---

## 3. 本轮实际改动

### 3.1 `bridge/run_progress.py`（新增）——第一次把"这条 run 到底走了什么"算出来

之前没有任何工具能回答"这条真机 run 见过几个先古之民、打赢了几个 boss"。
新模块从状态流本身推导，不接受任何层数/幕号/计数器反推：

- 先古之民：只认 `event.is_ancient`，按幕记录 `event_id`；
- boss：只认 `state_type == "boss"` 的 `(act, floor)` 节点，并要求"活着离开该节点"才算清除；
- 胜利：只认 `bridge/outcome.py`，且 `run_complete` 需要 三幕 + 三先古之民 + 每幕 boss 配额
  （`{1:1, 2:1, 3:2}`）同时成立；
- 反脆弱细节：真机记录胜利时会先 `TriggerVictory()` 再杀光队伍
  （`RunManager.cs:1238-1246`），所以"玩家还活着"不能作为最后一个 boss 被清除的证据——
  终止屏本身才是。这条是写完测试后被自己的用例抓到并补上的。
- 菜单帧不计入"到过第一幕"，重复轮询的终止屏不会把一条 run 拆成多条（81 MB 真机 trace 实测会踩到）。

`scripts/audit_live_run_coverage.py`（新增）把同一套判定回放既有 trace，产出
`docs/evidence/live_run_coverage_20260920.json`（已进哈希链，58 个文件）。

### 3.2 胜负位：候选桥接 + 消费侧（真机部分**实现待验证**）

- 消费侧 `bridge/outcome.py:33-49`：有 `game_over.is_victory` 时以它为准，没有才退回措辞判定；
  新增 `outcome_source()`，把"这次读数凭什么"写成 `bridge_is_victory_flag` /
  `game_over_message_wording` / `no_victory_signal`。
- 生产侧 `third_party/STS2MCP-main/McpMod.StateBuilder.cs:452-467`：`is_victory` 取
  `currentRoom.IsVictoryRoom`，房间已卸载时为 `null`（未知≠失败），与游戏自身
  `NGameOverScreen.cs:268` 同一读法。
- 编译验证：`build.ps1` 对锁定 build 编译，**0 warning / 0 error**。
- 产物：`artifacts/sts2mcp-victory-flag/`（DLL `0A3C1158…7EBA5`）+ provenance。
  二进制自证：新 DLL 含 `is_victory`(#US) 与 `IsVictoryRoom`(memberref)，`sts2mcp-seeded` 两者都无。
- 只读预检：`scripts/stage_sts2mcp.py`（不带 `--apply`）回报 `status: ready, mutated: false`。
- **未安装。** 替换已装桥接是 operator 的决定；装完之后需要真机 health smoke 才能更新 lock。
  → 该项标 **实现待验证（被"是否允许换桥接 DLL"这一项阻塞）**。

### 3.3 预检门禁做成真机制（并修正 README 的假话）

`scripts/supervise_solver_batch.py`：`_preflight_gates()` 在 `--dry-run` 里加载
`VersionLock` 并 `verify_installed_game()`；模组字节测不出/读不到即
`EXIT_PREFLIGHT_FAILED = 10`，`play.py` 原有逻辑据此拒绝起局。
求解器版本的漂移**只报告不否决**——它是 operator 的自动更新决定，本仓库不越权拦。
结论写进 `status.json` 的 `preflight_gates`。

本机实测：`--dry-run` 退出 0，`installed_game` 回读 `v0.111.0/41cef1ea/24724944`，
`mod_attestation.all_match_lock = true`。

### 3.4 近似三幕在数据层面被隔离

`training/campaign_content.py`（新增）+ `training/metrics.py` 两个新字段 +
`training/evaluation.py` 在 `campaign=True` 时自动盖章。四级结果分层写进产物：
`simulator_single_act` / `simulator_three_act_approx` /
`simulator_three_act_content_verified`（**当前无任何产物属于这一级**）/ `live_full_run`。

端到端验证：用同一 checkpoint、同一种子 `130008177` 重跑
`scripts/probe_three_act_campaign.py`，产物落
`runtime/play_sim_first_clear_relabeled_20260921.json`：

- `rows` 与已入链的 `docs/evidence/three_act_first_clear_20260920.json` **逐字段相等**
  （`final_floor 50`, `campaign_cleared 1`, `illegal_actions 0`, `steps 668`）→ 没改动任何历史语义；
- 新产物额外带上 `environment_version: sts2sim-campaign-approx-v1` 与
  `content_coverage.verdict: approximate`，并明写 stage 2/3 的池是 Underdocks、
  `matches_real_game_act: false`。

### 3.5 不许"悄悄跳过"

`bridge/autoplay.py:671-674` 原先：任何不认识但带 `can_proceed` 的屏幕，一律发 `proceed` 走过去，
不留痕迹。改为：每次兜底都记进 `coverage.proceed_bypasses`（含幕/层/原因）并打印；
同一屏幕被兜底超过 3 次就以 `BridgeProtocolError` 停批，原因写"拒绝继续绕过未建模内容"。
另：`summary()` 现在带 `runs[]`（每条 run 的覆盖）、`runs_with_certified_clear`、
`victory_evidence_available`——`run_outcome` 此前**从未被自动驾驶路径调用过**，批结束只报动作数。

---

## 4. 普通用户现在怎么起真机自动打牌

```powershell
# 前提：见下；只自检、一个动作都不发
.\.tools\python\cpython-3.12-windows-x86_64-none\python.exe scripts\play.py --backend live --preflight-only
# 真打
.\.tools\python\cpython-3.12-windows-x86_64-none\python.exe scripts\play.py --backend live
```

必要前提（缺一不可，且都不可由本仓库代答）：

1. 游戏停在锁定 build（`v0.111.0`/`41cef1ea`/`24724944`），Steam 客户端在跑（用于 `-applaunch 2868840`）；
2. STS2MCP + RitsuLib + CombatSolver 已启用，且**局内 full_auto 由 operator 事先开好**——
   本轮不接管、不点击这个开关；
3. `config/combat_solver.lock.json` 与实机一致（预检现在真的会查）。

诚实边界：`play.py` 是入口，不是完成证据。它目前 `--max-runs 3`、默认 `observational`，
批结束会打印覆盖与"胜利是否可观测"，但**它跑不出一条被认证的整局胜利**（§1.1）。

---

## 5. 测试与验证记录

| 命令 | 结果 |
|---|---|
| `python -m unittest discover -s tests`（轻运行时） | `Ran 552 … FAILED (errors=9, skipped=3)`；9 个 error 全是基线既有的 `No module named 'numpy'/'gymnasium'`，**0 failure** |
| `scripts/test.ps1`（训练运行时，`STS2_TRAINING_PYTHON`=相邻模拟器 venv） | `Ran 132 … OK`，含 `RealSimulatorIntegrationTests`（真实 DLL）全绿 |
| `tests/test_run_coverage.py`（新增 16 项） | OK：完整局判定、缺第二个 boss、输给第二个 boss、缺先古之民、锁定桥接措辞永不算胜、显式位覆盖措辞（双向）、终止屏重复轮询不产生幻影 run、菜单帧不虚报到过第一幕、兜底跳过被记录且超限即停、`AutoPlayer.run()` 回路级集成 |
| `tests/test_solver_supervisor.PreflightGateTests`（新增 3 项） | OK：匹配→放行；`steam_build_id` 漂移→拒绝并指出字段；模组测不到→拒绝 |
| `tests/test_training.CampaignLabelTests`（新增 4 项） | OK：campaign 产物带版本+覆盖；声明点名 Hive/Glory/A10 双 boss；单幕产物保持原形状；四级不混算 |
| `tests/test_promotion_gate_absent_inputs.py` | 更新 `CLEAN_RECORD`（schema 守卫按要求把两个新字段写进"完整记录"） |
| `scripts/build_evidence_manifest.py` | 58 个证据文件，`bundle_root 40a9a540f57abef9…`，219 个 checkpoint 摘要 0 缺失，recompute mismatches 0 |
| 真机相关 | **未运行新的真机批次**（见 §6 阻塞项）；本轮真机结论全部来自 2026-09-20 已有 trace 的离线回放 |

---

## 6. 当前可运行范围与阻塞项

已具备证据：

- 真机：局外自动驱动跑过 **act 1 → 2 → 3**，含两批共 261 个成功动作；
  三条 run 覆盖到 **每幕先古之民**（NEOW / OROBAS·PAEL·DARV / TANX），
  两个真 boss 清除（Overgrowth/Underdocks boss + Hive boss `THE_INSATIABLE`）。
- 模拟器：三阶段流程可自动跑完并可逐种子复现（1 个种子的 `campaign_cleared`）。

阻塞项（按优先级，均为实现/授权问题，不是策略强度问题）：

1. ~~**胜利终局不可观测**~~ **已解除（2026-09-21，见 §8）**：带胜负位的桥接已安装，
   真机终止屏实测 `is_victory=false` 且被读成 `False`。仍然待做的不是可观测性，
   而是**真的赢一次**（第 2 项）。
2. **第三幕第二个 boss 从未被到达**：真机三局（09-20 两条 + 09-21 §8 那条）都在
   终幕第二个 boss 之前结束。现在观测能力已具备，缺的是把它跑出来一次。
3. **模拟器二/三幕内容是 Underdocks 换皮**：修法已知且很窄——把已存在但无人引用的
   `66/70/71/73/75/76/78/80/81` 接成 Hive/Glory 池，加每幕先古之民与双 boss 地图出口；
   做完才能声明 `simulator_three_act_content_verified`。
4. **先古之民与 boss 遗物选择是 `options[0]` / `index: 0` 硬编码**（`autoplay.py:659-660, 783-787`）：
   合法但无策略，属可接受的弱基线，不能当成"策略已覆盖"。
5. 一批 `status: running` 的监督器目录（`ssb-20260920T051734Z`）说明批次可以在不写
   `session_end` 的情况下消失；证据链对"批次未收尾"没有强制。

---

## 7. 一句话回答本轮之问

（本节按提交时的状态写；**"胜利终局不可观测"一条已被 §8 的授权复验解除**，其余仍然成立。）

- **已经有证据的**：真机局外自动链路能自己从开局推进到**真实第三幕**，走完**每一幕的先古之民**，
  清掉**第一、第二幕的关底 boss**（Hive 的 `TheInsatiable` 实测清除），并两次跨幕。
- **只是模拟器近似的**：所谓"三幕通关"——它站的是 Underdocks 换皮的第二、三幕，
  没有每幕先古之民，双 boss 之间没有真实出口，`campaign_cleared` 是"打完两场且活着"。
- **仍然没有实现的**：真实第三幕**第二个 boss** 的任何一次到达与清除；
  **真实胜利终局**的观测（修复已入库但未安装）；模拟器侧 Hive/Glory 池表的接线。

---

## 8. 授权后复验：换桥接 + 真机终局读数（2026-09-21 晚）

operator 授权"更新桥接 DLL 并重新进行终局胜利验证"。逐条按已入库的门禁做，没有手拷文件：

1. **安装**：`scripts/stage_sts2mcp.py --apply`（候选哈希预先钉死）。它先复核"盘上目标 == lock"、
   确认游戏进程与桥接端口都空闲，再对两个文件做**哈希校验备份**，然后 `os.replace` 原子替换，
   替换后重验哈希。备份与回滚清单：
   `runs/sts2mcp_staging/backups/sts2mcp-20260920T165128Z-595d469a/backup_manifest.json`。
   `CD3EA740…43A4D` → `0A3C1158…7EBA5`。
2. **smoke 通过后才改锁**：`GET /` → `Hello from STS2 MCP v0.4.0`；
   `GET /api/v1/singleplayer` → `state_type menu`；`GET /api/v1/compendium` → profile/current/saved 齐全；
   实机 build 仍 `24724944 / v0.111.0 / 41cef1ea`。然后只把 **STS2_MCP** 一行同步进两份锁。
   **CombatSolver 这一行留着不钉**：Steam 起客户端时把它自动更新了
   （锁 `AEF11717…` → 盘 `B66D7C05…`），求解器锁归 operator，本轮只记录漂移。
   这条漂移也正是本轮真机验证走 `bridge.autoplay` 直驱、**不走监督器批次**的原因
   （批次里的比对子进程会卡在该哈希门上）。写清楚是因为：这少了一层批次自证，不是等价条件。
3. **真机终局读数**（`docs/evidence/victory_observability_20260921.json`）：
   终止屏实测 `{"is_victory": false, "message": "Run ended.", …}`，
   `run_outcome()` 返回 `False`，`outcome_source()` = `bridge_is_victory_flag`。
   改动之前同一屏只会得到 `None` + `game_over_message_wording`。
   **头号阻塞项就此解除**：胜负现在是可观测的，不再靠措辞猜。

诚实边界，三条：

- **验到的是"败局分支"**。终止屏确实出现了、字段确实是真的 `bool`、消费链确实读到了它——
  这三件事成立。但**"赢"这个分支没有被跑过**：它要求真的清掉终幕第二个 boss，
  本项目在真机上还没有做到过一次。所以 §0 里那条"必须有真实胜利终局证据"的验收项**仍然未达成**。
- 这条 run 是**续打** operator 批准续打的一局挂起存档（standard/A10/铁甲战士，第二幕 19 层），
  不是从第 1 层开局；它最终死在 `act 2 floor 33` 的 boss，所以 `bosses_cleared` 为空、
  `run_complete=false`。死在该 boss 前是策略结果，不是这里要回答的问题。
- 驱动结束后，菜单分支按既有逻辑**开了一局新的铁甲战士 A10**（`act 1 floor 1`）才被 `max-runs` 停下。
  没有 `abandon_run`、没有删档、没有换 profile。**当前游戏里有一局新开的 run 在跑**，这是本轮的副作用，
  留给 operator 处置。

顺带被这批真机数据抓到并修掉的两个自身缺陷：

1. **续局身份守卫误判**：`continue` 之后第一帧常是没有 `player` 的过渡帧，
   守卫把"读不到"当成"不是铁甲战士"直接拒——那条真存档其实是标准 A10 铁甲战士。
   现在"读不到"在 60 秒预算内重读，真冲突仍然立刻失败关闭；预算用尽时报的数是
   "多少帧没读到角色 + 最后一屏是什么"。
2. **第二条静默跳过路径没进账本**：真机日志里出现
   `repeating identical action … falling back to proceed`，而 §3.5 只埋了 `decide()` 里那条兜底。
   这条"重复→改发 proceed"的兜底现在同样记账，并在第 7 次仍不推进时以
   `stop_reason=unmodelled_screen` 明确停批，而不是永远 proceed。

---

## 9. §8 里那句"因为求解器漂了，所以只能绕过监督器"不是可接受的稳态——已修

上一节把直驱 `bridge.autoplay` 记为一次性权宜。查下去发现**实现与 README 契约确实不一致**，
而且不是小口径问题：

- `run_solver_comparison.py:146` 的 `verify_solver_inventory()` 对**所有** `required` 模组
  一律要求哈希相等，任何不符直接 `raise VersionLockError`；
- 监督器固定起三个子进程（`fullauto_keeper` / `comparison` / `autoplay`），
  所以**验收链路被比较实验的门禁绑架了**；
- 报错文案还是错的：把"operator 允许的 Workshop 自动更新把字节换了"说成
  `"fill config/combat_solver.lock.json at installation time"`（好像锁没填）。

而 README 一直承诺求解器漂移**只报告、不否决**。两边都有道理，冲突在于**一个函数被两种目的共用**：

| 目的 | 求解器哈希的角色 | 应该怎样 |
|---|---|---|
| CombatSolver 对比实验 | 自变量（arms 之间就是换它） | 必须钉死，否则比较的不是任何东西 |
| 真机验收整局 | 执行者身份 | 必须**点名并记录**，漂移→记为验收阻塞项，但不中止批次 |

### 修法

按目的分开，而不是把门禁调松：

1. `verify_solver_inventory(lock, *, mod_gate)`：`strict`（默认，对比轨）保持必须相等；
   `attest`（验收轨）漂移只记账并入 `acceptance_blockers`（`solver_version_drift:<mod>`）。
2. **两种模式下都仍然硬失败的情况**：required 模组缺失或不可读——点不出执行者就没有 provenance。
   这条不能松，松了就等于允许"不知道自己跟谁打的"进入验收。
3. **批内漂移仍然致命**：`_moved_mods()` 比对批次首尾两次测量，中途换字节照旧
   `invalidated_by_mod_update`。放开的只是"批前就已漂移"这个既成事实。
4. 监督器加 `--track {comparison,acceptance}`，把 `--mod-gate` 传给比对子进程，并把
   `track`/`mod_gate` 写进批次 `status.json`；`scripts/play.py --backend live` 默认走
   `acceptance`（成品路径是"打一局"，不是"比两个求解器"）。
5. 报错文案改成真实原因。

`tests/test_mod_gate.py`（新增 6 项）钉住这四条：漂移→strict 拒 / attest 记；缺失→两模式都拒；
track→子进程 `--mod-gate` 正确传递；未知 track 在启动前就拒。
`verify_solver_inventory` 此前**没有任何测试覆盖**——一个会中止真机批次的安全门长期无人验证，
这本身是这次不一致能存活到今天的原因。

---

## 10. 正式监督链路已恢复，并向前推到真实第三幕——然后卡在一个新发现的关口

`ssb-20260921T102537Z-2f872726`（`--track acceptance --allow-actions`，三子进程齐全）：

- 监督链路**跑起来了**：三个子进程全部存活，一次 `game_lost → game_restored`（缺失 0.5 s）被宽限期正确吸收；
  求解器漂移只记账（`drifted_at_start: [CombatSolver]`、`moved_during_batch: []`），批次不再被它中止。
- 真机进度（本次是**从挂起存档续打**，局外动作 224 次由桥接器发出）：
  - 幕：**1 → 2 → 3**，实测推进到 `act 3 floor 46`；
  - 三个先古之民：**NEOW / PAEL / VAKUU**（PAEL 属 Hive、VAKUU 属 Glory）；
  - 关底 boss：`1:17 CEREMONIAL_BEAST`（Overgrowth）**已清**、`2:33 CRUSHER+ROCKET`（Hive）**已清**；
  - 无终局屏（`terminal payloads: []`），所以这局**仍然活着**、`run_complete=false`。

### 卡在 `fake_merchant`：一个真实的内容/能力缺口

`act 3 floor 46` 是 `FAKE_MERCHANT` 事件屏。读模组源码得到的机制是硬的：

- `McpMod.StateBuilder.cs:1487-1515`：`started_fight=false` 时 `shop.can_proceed=false`；
  打赢之后才 `can_proceed=true` 并提示 "Proceed to map"。
- `McpMod.Actions.cs:618-637`：`proceed` **确实**处理假商人——但按钮 `IsEnabled` 才点得动。
- `McpMod.Actions.cs:410-431`：唯一会去 ForceClick `MerchantButton`（即触发那场遭遇）的路径，
  藏在 `shop_purchase` 内部"顺手打开商店"的逻辑里。

也就是说这一屏只有两条出路：**花钱买**，或者**打一架**；没有"白手退出"。
驱动原先对 `fake_merchant` **没有任何规则**，且因为 `can_proceed=false` 连 proceed 都不试，
于是连续 260 帧原地空转——看起来像卡死，实际是"没有人实现这一步"。

### 本轮已改（不改验证标准，只把静默变成实名）

1. `decide()` 增加 `fake_merchant` 规则：**尝试 proceed**。这是唯一既不花钱也不替本局选遗物的动作；
   被拒绝时会在一次 POST 内变成具名错误，而不是三分钟空转。
2. 任何"无规则且无 continue 控件"的屏幕，**第一帧就记入** `coverage.unhandled_screens`（幕+层），
   并在超过 10 帧后以 `stop_reason=unhandled_screen` **具名停批**。
3. 修掉我上一轮自己的一个缺陷：兜底越界抛的是 `BridgeProtocolError`，而循环把它当瞬时故障**重试**——
   所以那个"边界"其实不会终止批次。现在走 `_classified_stop`，真正终止且落盘原因。

### 还要做的一件（需要再一次桥接授权）

要把假商人**变成可通行的流程而不是死路**，需要桥接暴露一个"点 MerchantButton 但不购买"的动作。
这跟上一轮 `is_victory` 是同一类小改动（同一套 `stage_sts2mcp` 安装/回滚路径），
但它意味着**再一次替换已安装的桥接 DLL**——上一次的授权是针对胜负位那一个目的给的，
我不把它当成对后续任意 DLL 替换的通用许可。

## 11. 真机往前推到 Act 3 floor 49 之后，闸门自身又露出四个缺陷（2026-09-21 深夜）

### 11.1 三个"动作根本不合法"的实现缺陷（已修）

| 症状（来自 trace，不是推测） | 根因 | 处置 |
| --- | --- | --- |
| `stop_reason=unhandled_screen`，`detail="no rule and no continue control for screen 'card_select' at act 1 floor 5 after 11 frames"`，`coverage.deferred_to_combat={'NDeckEnchantSelectScreen': 11}` | 驱动用一张"认识的名字"白名单判断 `card_select` 归谁：名字不认识就交给战斗层。但局外的卡牌网格**根本没有战斗层这个接手人**，于是这一屏没人动，11 帧后停批 | 改成按真实构建自己的自动出牌注册表（`MegaCrit.Sts2.Core.AutoSlay.Handlers.Screens`，13 个 handler）来判定：`NDeckEnchantSelectScreen` 在它里面，所以是我们的；整个 `card_select` 家族里只有 `NCombatPileCardSelectScreen` 不在，只有它继续弃权。另外 `select_card` 是**开关**，所以走位改成"逐个点不同下标，直到屏幕自己的 `can_confirm` 亮起" |
| 两次 `choose_event_option index 0` 被 mod 拒绝：`No event options available`（`PUNCH_OFF` act1/f11、`SLIPPERY_BRIDGE` act2/f20） | 事件的选项是**下一帧才渲染**的（同一局里 seq 37 是 0 个选项、seq 38 就变成 2 个），而驱动把"没选项"当成"选项 0" | 零个或全部锁住时不 POST，等下一帧。闸门里 `every_action_was_legal_and_acked` 不允许含这种被拒动作 |
| 上一条若不修会更糟：`_unhandled_counts` 按**整批生命周期**统计每屏无规则帧 | 那样一个正在播动画的屏幕会慢慢耗光预算，把"看一眼动画"误判成"缺处理器"而停批 | 无规则计数改成按**相同帧连续**计数（decision_id 不变才算卡住），这才是"no rule and no continue control"本来的意思 |

### 11.2 客户端自己会把整局卡死：`PunchOff` 的 VFX 空转（未修，只能命名 + 熔断）

真机事实（全部来自被卡死那个进程留下的文件，不是复盘时的说法）：

- 最后一个动作是 `choose_map_node index 0` → `Traveling to Unknown at (2,10)`，随后状态变成 `unknown`；此后 **10 秒内桥接不再响应**，两次 `GET /api/v1/singleplayer` 超时。
- 客户端自己的日志 `godot2026-09-21T20.43.18.log`：**2,301,246,672 字节 / 22,044,517 行**，实测 **16,793,363 字节/秒**（3 秒涨 50,380,091 字节）；其中 `ERROR: Parameter "particles" is null.` 出现 **708,549 次**，含 `PunchOff` 的行 **2,368,137 行**，首次报错在第 8,386 行。
- 反复出现的栈就是事件自己的循环：`PunchOff+<PunchEachOther>` ← `EventRoom.EnterInternal`，前面还夹着 `ERROR: Element limit reached. at: initialize_rid`。
- 反编译对得上：`PunchOff.cs:30` 是 `EventLayoutType.Combat`，`:40` 允许它从 `TotalFloor >= 6` 出现，`:65` 是 fire-and-forget 的 `PunchEachOther()`，只有**离开房间**才取消，`:81-99` 每圈都要实例化 `vfx/vfx_attack_blunt` 和 `NHitSparkVfx.Create`。

这不是策略死亡（HP 80/80 满血进房，一局没掉血就再也读不到状态），也不是本仓库的动作造成的。而且它**不是确定性的**：同一局后来在 act1/f11 又进了一次 `PUNCH_OFF`，这次正常返回了两个选项——所以触发条件是资源耗尽（`Element limit reached`）那一类，不能靠"避开这个事件"解决，何况事件节点本来就是交付目标要覆盖的内容。

能做的只有两件，都落地了：

1. **看门狗**：`scripts/supervise_solver_batch.py` 监测客户端自己那份 `godot.log` 的字节增速，超过 5 MB/s 且连续 4 个采样点，就以 `stop_reason=client_wedged` 停批。
2. **冻结而不是杀进程**：`NtSuspendProcess`。杀掉可能正好落在游戏自己的存档写入里，而 operator 的红线是"不碰存档/profile"；挂起只让那个失控循环停下，窗口、进程、文件都在原地，人可以自行决定关还是恢复。

### 11.3 观察者的死不再否决整局（acceptance 轨）

`comparison` 子进程 5 秒桥接超时后退出，过去会把 `autoplay` 一起停掉（`result_code=5`），于是**验收局被一个取证件的日程否决**。现在 acceptance 轨记 `attestation_gaps=["comparison_observer_exited:<code>"]` 继续打牌；比对轨照旧致命——那条实验的自变量就是求解器，battles 就是交付物。

### 11.4 最深的一局真机连续流死在 Act 3 floor 49——但它天生不是闸门 trace

- 这一局最终 `game_over`：`{"is_victory": false}` at **act 3 / floor 49**，也就是决胜幕的关底层。此前它已经清过 `1:17`（第一幕关底）。这是本项目在真机上走到的最深位置。
- 它**不能**充当 full-run 闸门证据，闸门自己就这么写：`FAIL starts_at_floor_one :: first act entry floor=10, acts=[1, 2]`；trace 里第一个动作是 `menu_select continue`。
- 为什么必须 continue：存档里还压着一局未完成时，`start fresh` 被 mod 拒绝（`Singleplayer is not currently actionable`，日志里重试了 10 次），驱动只能走身份守卫的续档路径——"绝不静默覆盖别人的存档"是对的，但代价是**闸门要的那份从 Floor 1 起手的连续 trace，只有在上一局走到终局之后才拿得到**。`abandon_run` 是红线，不碰。
- 所以上一局终局之后，驱动当场起了新的一局（act1/floor 2 起，`BURNING_BLOOD`+`PHIAL_HOLSTER`），这一局是从 Floor 1 开始的候选；但**它跑在修事件合法性之前的进程里**，所以只要再抽到一个"下一帧才出选项"的事件，它同样会被 `every_action_was_legal_and_acked` 拒绝。要不要这份 trace，取决于运气；要拿到确定的一份，就得等这个进程跑完、用已修的代码在"上一局刚终局"的时间窗里立刻起新批。

### 11.5 第五个缺陷不是驱动能修的：休息点"能看不能点"（候选桥接已备好，未安装）

真机统计（对本机全部留存 trace 逐条重算，不是印象）：`choose_rest_option` 一共 POST 80 次，
**8 次被 `Rest site room is not open` 拒绝**；今天下午的两批里 44 次中 2 次。
根因在桥接自己：`BuildRestSiteState` 的选项来自房间**模型**（`RestSiteRoom.Options`），
而 `McpMod.Actions.cs:373-375` 的动作门槛是 `NRestSiteRoom.Instance`（UI 节点）——
后者晚一点才就位。

驱动侧修不了它，这一点也是量出来的：`ssb-20260921T124324Z-045dcccc` 里
seq 4126/4127（被拒）与 seq 4130-4132（随后被接受）在状态暴露的字段上**逐字节相同**
（同样的两个选项、`is_enabled: true`、`can_proceed: false`）。
任何"多等几帧再点"的规则都是在拿时序赌，而不是在满足某个条件。

为什么这件事值得单独一节：闸门第 7 项要求"每一次动作都合法且被确认"。
一整局 A1-3 要休息 5-7 次，按上面 10% 的单次拒绝率，
**不修这一项，PASS 与否主要由客户端时序决定，而不是由打牌的人决定。**

已做（不改已安装的桥接）：

- 源码候选：`rest_site` 增加 `can_choose = NRestSiteRoom.Instance is not null`，
  就是把动作自己的门槛挂到状态上（与 `can_proceed`、`is_victory` 同一形状）。
- 产物：`artifacts/sts2mcp-actionability/`，构建 0 warning / 0 error，
  DLL `6DA7911EC0FA6838...`；程序集字符串堆核验：候选含 `can_choose`，
  **当前安装的 DLL 不含 `can_choose` 但含 `is_victory`**——说明这一支与已安装那一支同源，
  差别就是一个字段（`candidate_manifest_provenance.json`）。
- 驱动侧已经先接上：`rest_site.can_choose` 明确为 `false` 才等待；
  **字段缺失（现在的安装态）行为完全不变**，所以这是一次纯增量的候选，而不是先把驱动改成依赖一个读不到的字段。
- 安装**没有**发生，也没法发生：`stage_sts2mcp.py` 在真机在跑的时候直接拒绝
  （`SlayTheSpire2.exe is running; refusing bridge file mutation`），这正是它该有的样子。
  前两次换 DLL 的授权分别是为"胜负位"和为"不买只走"给的，我不当成通用许可——
  要装需要一次针对这个目的的授权，以及一个游戏空闲的窗口（停批 + 关游戏）。

上面那两个数字不用相信本文，`scripts/census_action_refusals.py` 逐条重算本机所有留存 trace：

```powershell
.\.tools\python\cpython-3.12-windows-x86_64-none\python.exe scripts/census_action_refusals.py
```

它按"POST 与其响应"配对来数，今天这一轮的结果是
`choose_rest_option posted=82 refused=8 (9.8%)`、`choose_map_node 583/18 (3.1%)`、
`choose_event_option 295/12 (4.1%)`、`select_card 91/2 (2.2%)`。
同一份输出里 `menu_select`、`proceed`、`set_ascension` 的拒绝率高达 74%/83%/100%，
那**不是打牌动作**，是历史批次开机引导阶段的循环（在 `run` 分段之外，闸门也不把它算进任何一局），
本文不拿它当"玩家不合法"的证据，也不把它和局内动作混在一个总数里说事。

## 12. 药水：两个动作根本不在动作空间里（一个已可修，一个卡在桥接）

产品层的说法是"算法不会丢药水，也不会局外用那些局内局外都能用的药水"。核实之后这两句都成立，
而且比"没学会"更基本：**这两个动作从来没进过驱动的动作空间**。

| 事实 | 证据 |
| --- | --- |
| 全部真机 trace 里 `use_potion` / `discard_potion` 的 POST 次数为 **0** | `scripts/census_action_refusals.py` 的 posted 清单里就没有这两个动作 |
| 局外持有可用药水却不动的帧数：**3,285** | 逐帧重算 `runs/solver_supervisor/*/autoplay_trace.jsonl`；其中 `鲜血药水` 61 帧、`果汁` 247 帧 |
| 商店里药水槽**已满**的帧：**5,179**，其中 **5,119** 帧货架上有买得起的药水 | 同上（`shop.items[].category == "potion"` + `player.max_potion_slots`） |
| 真机构建里"任何时点可用"的药水只有 **4/64** | `PotionUsage.cs` + 全树 `PotionUsage.AnyTime`：`BloodPotion`(回复最大生命20%)、`FruitJuice`(+5最大生命)、`EntropicBrew`、`FoulPotion` |
| 战斗中用药水归战斗求解器，不冲突 |  Combat Solver 日志的 `ROUTE_ACTION` 里有 `PotionSlot/PotionId`，权重含 `potion_min_hp_saved`、`final_policy=...hp_potions...` |

### 已修：丢药水→买药水（不需要换桥接）

`discard_potion` 桥接本来就接受，而且商店侧的判据全都在状态里，所以这一步能真正落地：

- `advisor_core/policy_live.py::_shop`：**满槽时的药水不再是合法候选**，除非"换"这一件事在可见信息上是严格变好的——
  否则就会 POST 一发装不下的购买（那就是又一个不合法动作）。
- `advisor_core/live_choice_policy.py::potion_to_discard_for`：只用双方都公布的效果文本比较，
  不达标就不换；换的是**最弱的一格**。
- `bridge/autoplay.py::_potion_room_to_free`：丢弃只在"这一帧的商店选择本来就是这瓶药水"时发出，
  所以它永远是既有购买决策的代价，不会是独立的损失。丢完下一帧槽空了，购买照旧。

### 卡在桥接：局外喝药水的合法性读不到

驱动需要知道"这瓶能不能在战斗外喝"。桥接目前只公布 `can_use_in_combat`，
而它对 `CombatOnly` 和 `AnyTime` **同样为 true**——所以这个信息在装着的桥上根本不存在；
`McpMod.Compendium.cs:141-147` 也自陈只给已发现的药水 id，不给规则文本。
于是规则写成**只在状态明确说可以时才动**：`usage` 缺失即不喝（不是猜，是不做没依据的动作）。
候选桥接已加上 `usage = potion.Usage.ToString()`，见 `artifacts/sts2mcp-actionability/`
（与 `rest_site.can_choose` 同一支、同样未安装）。

策略版本因此从 `conservative-visible-v1` 升到 **`conservative-visible-v2`**，
记录的规则是：+最大生命这类永久增益只要合法就喝；回复类只在生命低于 60% 且**人不站在休息点**时喝
（休息是免费的同样治疗）。分类读的是客户端自己公布的中文文本，不读药水 id——
`鲜血药水` 的文本是"回复你最大生命值的20%。"，同时含"最大生命"和"回复"，
所以判据必须是动词，不是短语；这三条真实文本已钉进 `tests/test_live_choice_policy.py`。

### 12.1 顺带发现：闸门自己有一项永远不可能通过

`three_ancients_are_this_builds` 拿 int 幕号去查一个 str 键的 dict
（`run_progress.coverage()` 为了 JSON 产物把 `ancients_by_act` 的键转成了字符串），
所以它**从来没想过**——今天下午那份已经集齐三幕先古之民的真机 trace 也被它判 FAIL。
已按 `str(act)` 读取修正，并补上 `tests/test_full_run_contract.py`：
一份合成"Floor 1→三幕→1+1+2→victory"的 trace 必须 11/11 全过，
同时把"错幕的先古之民""中途入局""最后一关战死""一处被拒动作"各判死一项。
在此之前这一项的"严格"是假的：它只是恒假。

### 12.2 提交门禁漏了四个测试模块

`scripts/test.ps1` 的光环境套件是**手写清单**，不是发现式：
`test_full_run_contract`、`test_live_choice_policy`、`test_run_coverage`、`test_mod_gate`
四个模块不在清单里，也就是说今天新写的规则测试与覆盖率测试**根本没被门禁跑过**，
而它一直在报绿。已全部补进去：光环境 539→**594**，训练环境 132，两半都 OK。

### 12.3 另外两项"永远不可能通过"，是拿真机 trace 复算时才露出来的

- `starts_at_floor_one` 要求首幕进入层**恰好等于 1**。但一整局是从 Neow 起手的，
  那一帧的 `run.floor` 还是 **0**（比第一层还早），于是"从开局就在看"反而被判成不合格；
  真正中途续档的那一局回报的是存档所在层（≥1），本来就拦得住。已改成 `<= 1`。
- `provenance_bound_to_the_locked_build` 从**每一段自己**的记录里找 `session`，
  而 `session` 一个文件只有一条（在文件头）。结果同一个批次文件里的第二局开始，
  这一项**永远**是 None → FAIL。已把 stream 的 provenance 提到文件级传给每一局：
  它描述的是"这条流在哪个 build/哪些模组上跑的"，不是"这一局有没有恰好撞上那条记录"。

复算当下的结果（`ssb-20260921T133240Z-7eb0c916`，正在跑的第二局）：
**11 项里 8 项已 PASS，含 `every_action_was_legal_and_acked`（posted=179，refused=0）**，
剩下 3 项失败就是它确实还没走到的那三件（第三幕 1+1+2 的两个关底、终局、以及由它们合成的那条整局定义）。
