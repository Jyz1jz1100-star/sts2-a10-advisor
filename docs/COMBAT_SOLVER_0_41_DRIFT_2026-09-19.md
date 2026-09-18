# Combat Solver 自动更新漂移与 grammar v1 失效（2026-09-19）

## 结论

`config/combat_solver.lock.json` 钉住的是 CombatSolver **0.31.0**。实际磁盘上
（`G:\SteamLibrary\steamapps\workshop\content\2868840\3790899961\CombatSolver.dll`，
文件时间 2026-09-18 16:31）是 **0.41.0**，SHA-256
`41BDB5DA3CD90115965B1CFDA55CB2E899F346F6366680BE589E5E757E6E5783`；
STS2-RitsuLib 同期升到 **0.6.2**（`E3959F1746FCB7AA404CB9CD861443DC540E8488B50F7D156EACBE79925156B6`，
锁里是旧哈希）。STS2_MCP（`CD3EA740…`）与 RegentFX（`0F0262B3…`）仍与各锁一致，
游戏本体未更新（build `24724944` / v0.111.0 / commit `41cef1ea` / assembly `222455745`）。

`%APPDATA%\SlayTheSpire2\logs\CombatSolver\*\process.jsonl` 的 `INIT mod=` 行显示
09-11～09-18 之间该模组被连续自动更新了 **8 次**：

| 版本 | 会话数 |
|------|--------|
| 0.35.5 | 2 |
| 0.36.4 | 1 |
| 0.38.1 | 2 |
| 0.38.6 | 1 |
| 0.39.0 | 3 |
| 0.40.1 | 1 |
| 0.40.2 | 1 |
| 0.41.0 | 4 |

**日志语法在 0.41.0 上已经失效**（见 §2）。因此 comparison 子进程按设计 fail closed
是正确行为，不是待绕过的故障。

## 1. 容器搬家

`CombatSolver.Entry.Logger` 现在是 `CombatSolverLog`，其
`CombatDiagnosticJournal` 写到 `logs\CombatSolver\<pid>-<guid>\`，
不再写 Godot 日志（反编译证据：`Entry.cs` 中
`Logger = new CombatSolverLog(Path.Combine(OS.GetUserDataDir(), "logs", "CombatSolver"))`；
`CombatDiagnosticJournal.WriteCore` 中
`(owner?.Log ?? _process).TryAppend(...)`）。

会话内（有 `owner`）的标记只进**每场战斗一个**的
`combat-<guid>.jsonl`；`process.jsonl` 只镜像一份固定的性能白名单
（`HEAP_RECLAIM` / `GC_SEARCH_ALLOCATION_LIMIT` / `GC_ALLOCATION_CAPACITY` /
`GC_FRAGMENTATION_COMPACTION` / `POTION_GRADIENT_MEMORY_DECISION` /
`MAIN_THREAD_FRAMES`）。每行是 JSON 信封
`{"Time":<unix ms>,"Level":"info","Message":"[CombatSolver/Test] TAG k=v …"}`。

实测：`%APPDATA%\SlayTheSpire2\logs\godot.log` 里 `[CombatSolver/Test]` 命中 **0**，
`CombatSolver` 只有加载期的 13 行；`process.jsonl` 里 `SEARCH_REQUEST`、
`FULL_AUTO_DEPLOY`、`UI_STATE` 全为 **0**（因为它们在 `combat-*.jsonl`）。

## 2. 语法本身也变了

把 13 个真实 `combat-*.jsonl` 的 `Message` 抽出、按 v1 的容器假设喂给现有
`LogTailSource(replay=True)`：

```
session     bytes   msgs  events  snaps  fails
  107700    279877    949       9      0      0
  161804       880      6       0      0      0
  163664       672      5       0      0      0
  18284-    125851    261       1      0      0
  209616     40912    117       1      0      0
  27016-    477047    757      10      0      0
  30544-     65536    144       1      0      0
  31344-    693182   1310       9      0      0
  33944-    836065   1187      15      0      0
  34380-    774111   1191      15      0      0
  36048-    325857    566      10      0      0
  44496-    143566    314       5      0      0
  60300-   2359133   3986      19      0      0
sessions: 13  total events: 95  total snapshots: 0  total failures: 0
```

**0 快照**：不只是要换个文件后缀，v1 的块结构匹配不上任何东西。

0.41.0 的实际标记词表（同上 13 个文件计数）：`ACTION` 4765、
`[CombatSolver/Evidence] ROUTE_HEALTH` 3098、`[CombatSolver/Evidence] ROUTE_ACTION` 2352、
`COVERAGE` 1269、`UI_DEPLOYMENT_STEP` 950、`TURN_OUTCOME` 706、`FORECAST` 610、
`DEPLOY_ACTION` 406、`[CombatSolver/Debug] DEPLOY_STATE` 406、`DEFERRED_FRONTIER` 360、
`UI_STATE` 231、`RESULT` 73、`FULL_AUTO_DEPLOY` 70、`DEPLOY_START` 70、
`SEARCH_REQUEST` 15。`SEARCH_ERROR` **0**：v1 的类型化失败枚举在 v2 无对应项。

## 3. 新增的两个只读接口

1. `[CombatSolver/Evidence] ROUTE_ACTION`：逐动作结构化 JSON，带 `traceId`、`index`、
   `action.{Kind,Turn,CardId,CardOccurrence,TargetIndex,TargetCombatId,CardTitle,TargetName,Choice,…}`。
   这是模组的**预测路线**，不是执行证明——标签语义必须继续区分
   `source="deploy_log"`（模组自称部署过）与状态增量推断。
2. `%APPDATA%\SlayTheSpire2\combat-solver-routes\<64hex>.json`：0.41.0 起每场战斗导出
   一个 177 键的结果文件（现有仓库代码零引用）。含
   `PortfolioTelemetry.Members[].{BeamWidth,NodeBudget,Ran,Selected,ExpandedNodes,Termination,Terminal,Won,BattleHpLost,ElapsedMilliseconds,AllocatedBytes}`、
   `ProjectedBattleHpLost`、`HpLostByTurn`、`SearchedTurns`、`CombatEndedTurn`、
   `TurnSetupPlayState.StateText`、`Snapshot`。Phase B 要的预测 HP 损失、求解耗时、
   内存、失败率都在里面；但文件**不含运行身份**（无 seed/profile/class 字段，只有
   `IsActEndingBoss`），所以单独不足以做验收绑定。

## 4. 09-11～09-18 会话的证据价值

这些场次没有 `ssb-*` 批次目录、没有 seed ledger 消耗，`ROUTE_ACTION` 里同时出现
Silent（`STRIKE_SILENT`/`LEG_SWEEP`）、Defect（`DEFEND_DEFECT`/`MIND_BLAST`/`VOID_FORM`）
与 Ironclad（`BURNING_PACT`/`DARK_EMBRACE`）卡牌，即多角色手工游玩，不在
Ironclad/A10/standard 协议内。按
`scripts/report_solver_log_attribution.py` 的口径它们仍是 **无法归属**，
不得用于任何胜率或 comparison 结论。它们的唯一合法用途是本文的语法取证样本。

## 5. 处置

- 本目录下的 `config/combat_solver.lock.json` 工作区改动（0.29.1 → 0.31.0）**保持未提交**：
  它描述的对象已经不存在了，提交它等于把一个过期状态固化成"已对账"。
- v2 codec 单独实现，v1 路径保留（历史工件仍须按 v1 解析），版本按内容嗅探或显式参数
  选择，绝不按文件名猜。
- 只要 Steam 继续静默更新 Workshop，锁就会持续漂移，验收门槛永远不可能满足。
  实机批次开始前必须先固定这两个 Mod 的更新来源（关闭该 DLC/Workshop 项自动更新，
  或把 DLL 从 Workshop 目录里隔离出来锁住），并把这条前置写进 `docs/ACCEPTANCE.md`。
