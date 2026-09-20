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
| **最终胜利终局判定** | **缺失（结构性阻塞）** | `bridge/outcome.py:33-62` | 见下 |

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

本轮已交付修复（见 §3.2）：候选桥接 + 消费侧读取，**未安装**。

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

1. **胜利终局不可观测**：已装桥接无胜负位。修复已编译入库（§3.2），
   **等 operator 决定是否换 DLL**。不换则真实整局胜率永远拿不到分母。
2. **第三幕第二个 boss 从未被到达**：真机两局都在 `3:48` 及其之前结束。
   需要 1 修好后跑固定种子整局去撞 `3:49` 节点。
3. **模拟器二/三幕内容是 Underdocks 换皮**：修法已知且很窄——把已存在但无人引用的
   `66/70/71/73/75/76/78/80/81` 接成 Hive/Glory 池，加每幕先古之民与双 boss 地图出口；
   做完才能声明 `simulator_three_act_content_verified`。
4. **先古之民与 boss 遗物选择是 `options[0]` / `index: 0` 硬编码**（`autoplay.py:659-660, 783-787`）：
   合法但无策略，属可接受的弱基线，不能当成"策略已覆盖"。
5. 一批 `status: running` 的监督器目录（`ssb-20260920T051734Z`）说明批次可以在不写
   `session_end` 的情况下消失；证据链对"批次未收尾"没有强制。

---

## 7. 一句话回答本轮之问

- **已经有证据的**：真机局外自动链路能自己从开局推进到**真实第三幕**，走完**每一幕的先古之民**，
  清掉**第一、第二幕的关底 boss**（Hive 的 `TheInsatiable` 实测清除），并两次跨幕。
- **只是模拟器近似的**：所谓"三幕通关"——它站的是 Underdocks 换皮的第二、三幕，
  没有每幕先古之民，双 boss 之间没有真实出口，`campaign_cleared` 是"打完两场且活着"。
- **仍然没有实现的**：真实第三幕**第二个 boss** 的任何一次到达与清除；
  **真实胜利终局**的观测（修复已入库但未安装）；模拟器侧 Hive/Glory 池表的接线。
