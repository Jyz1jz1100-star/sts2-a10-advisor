# CombatSolver 0.50.1：遗物「赌博筹码」的开战多选页面会导致战斗永久卡死

## 环境

| 项 | 值 |
|---|---|
| 游戏 | Slay the Spire 2 v0.111.0，commit `41cef1ea`，steam_build_id `24724944`，main_assembly_hash `222455745` |
| Mod | CombatSolver **0.50.1**（workshop id 3790899961，`CombatSolver.dll` mtime `2026-10-06T13:12:23Z`）——已是当前最新版 |
| 其他 mod | STS2MCP 0.4.0（只读状态桥接；不执行任何战斗动作，见文末） |
| 局面 | 铁甲战士，飞升 10（A10），Windows |
| full_auto | 全程开启，由本方 keeper 维持（keeper 只允许恢复该开关，不代为出牌） |

## 症状

持有遗物 **`GAMBLING_CHIP`（赌博筹码：在每场战斗开始时，丢弃任意张牌，然后抽相同数量张牌）** 时，
在 **boss 战开局**，游戏停在「选择任意张牌来替换」这个多选模态上，mod 不再有任何动作，战斗永不开始。
玩家侧在此之前无法继续（持续 7 分钟以上无任何进展，见「停顿的长度」一节）。
本方不会代为点击战斗内的页面，原因见文末。

桥接看到的状态（逐字，Act 3 floor 49）：

```json
{
  "state_type": "hand_select",
  "hand_select": {
    "mode": "simple_select",
    "prompt": "选择任意张牌来替换。",
    "cards": [
      {"id": "EVIL_EYE",         "name": "邪眼+",    "type": "Skill", "index": 0},
      {"id": "TRUE_GRIT",        "name": "坚毅+",    "type": "Skill", "index": 1},
      {"id": "SETUP_STRIKE",     "name": "预备打击+", "type": "Attack", "index": 2},
      {"id": "CRIMSON_MANTLE",   "name": "绯红披风",  "type": "Power",  "index": 3}
    ],
    "can_confirm": true
  },
  "battle": {
    "round": 1, "turn": "player", "is_play_phase": false,
    "enemies": [
      {"entity_id": "TORCH_HEAD_AMALGAM_0", "hp": 211, "max_hp": 211},
      {"entity_id": "QUEEN_0",              "hp": 419, "max_hp": 419}
    ]
  },
  "run": {"act": 3, "floor": 49, "ascension": 10},
  "player": {"hp": 18, "max_hp": 80}
}
```

注意：敌人满血、round 1 —— 战斗**一场都没打**，卡死发生在开场选择上。该页面 `can_confirm: true`，
也就是说「不选任何牌、直接确认」是合法的零效果答案。

## 两个可区分的签名

### 签名 A：进入选牌流程后抛 `NativeChoiceSurfaceMismatchException`

mod 自己的日志（逐字，保留原始堆栈帧）：

```
[CombatSolver/Test] DEPLOY_CHOICE_PAUSED turn=1
exception=CombatSolver.NativeChoiceSurfaceMismatchException: 原生三选一页面在计划提交前发生变化。
   at CombatSolver.NativeChoiceSurface.SelectChooseCardAsync(NGame host, Node surface, NativeChoiceRequest request, IReadOnlyList`1 selected, CancellationToken token)
   at CombatSolver.NativeChoiceSurface.SelectAsync(NGame host, NativeChoiceSurfaceLock surfaceLock, NativeChoiceRequest request, IReadOnlyList`1 selected, CancellationToken token)
   at CombatSolver.NativeChoiceSession.DriveAsync(NGame host, CancellationToken token)
   at CombatSolver.NativeChoiceSession.WaitForAllPlansConsumedAsync(CancellationToken token)
   at CombatSolver.SolverController.DeployCurrentTurn(NGame host, CombatState state, SolverResult result, SolverSettingsSnapshot deploymentSettings, SolverDeploymentSession deployment, CancellationToken token)
```

发生记录（同一进程一次，重启后另一次）：

| UTC | 客户端进程 | 位置 | 事件 |
|---|---|---|---|
| 2026-10-07T06:11:11Z | 76640 | Act 2 floor 33 boss（KNOWLEDGE_DEMON） | `DEPLOY_CHOICE_PAUSED turn=1` |
| 2026-10-07T06:24:55Z | 82984 | 同一存档续玩同一场 | `DEPLOY_CHOICE_PAUSED turn=5` |

日志文件（可直接附上，5,046,272 字节 / 9,684 行）：
`%APPDATA%\SlayTheSpire2\logs\CombatSolver\76640-3fcbfabe5af54c1cb874169b2cc342dd\combat-3997d7eee17945098920fe0eca1c5985.jsonl`

**一个很具体的线索**：异常文案说的是「**三**选一页面」，而上面这个页面提供 **4** 个可选项，
且遗物效果本身是「任意张」（数量不定，可为 0）。看起来 `NativeChoiceSurface` 对页面选项数
有隐含假设（或按某次读到的选项数排计划、提交时页面已按另一个数量重排），因此计划校验必然失败、
随后暂停且不重试。

### 签名 B（更严重）：mod 完全没有介入这个模态，战斗永不开始

Act 3 floor 49 同一次卡死（客户端新进程 79136）期间，mod **为这场战斗创建了日志文件，
但文件是 0 字节**——整场卡死期间没有任何一行输出：

```
%APPDATA%\SlayTheSpire2\logs\CombatSolver\79136-4517276bce1f4ff0a06e8640fab18d50\process.jsonl
    0 bytes   mtime 2026-10-07T07:49:48Z
%APPDATA%\SlayTheSpire2\logs\CombatSolver\79136-4517276bce1f4ff0a06e8640fab18d50\combat-b82e00b0ed9b430bb6a259b863628f19.jsonl
    0 bytes   mtime 2026-10-07T07:50:08Z
```

桥接状态连续 180 秒逐字节不变（本方自动机据此在 3 分钟内自停并报
`combat_no_progress`；此前两次都是等到 15 分钟以上由人工停批）。也就是说：既没有
`SEARCH_REQUEST`，也没有 `UI_STATE state=ready`，也没有 `FULL_AUTO_DEPLOY`，也没有签名 A 那条
`DEPLOY_CHOICE_PAUSED` ——mod 从未进入这场的回合部署循环。

推断（请核对）：开场模态出现在 **第一次寻路之前**。签名 A 说明 mod 具备处理该模态的代码路径
（`NativeChoiceSession` / `NativeChoiceSurface`），但签名 B 说明**开局那一次没有触发它**：
full_auto 的驱动似乎挂在「回合部署」上，而部署要等模态关闭；模态又在等部署。若成立，则是二者
互相等待的死锁，且因为 mod 一行日志都不写，对使用者完全不透明。

## 已经排除的解释（这部分对定位最有用）

* **不是重启能冲掉的偶发竞态**（作者最容易先试的一条，所以单独写明）：杀掉客户端、重新拉起、
  **续玩同一存档**，结果**同一位置、同一 round 1、同一状态**再次卡死——`06:11Z`、`06:24Z`、
  `07:19Z`、`07:50Z` 四次独立观测。重启之所以有时"看起来好了"，是因为那局**重开了**：新开的局
  此刻既没拿到 `GAMBLING_CHIP` 也还没进 boss 层，触发条件没被碰到，而不是被解除。
  2026-10-07 的实际例子：卡死后重启客户端并续玩 → 再次卡死（`07:50–07:59Z`，我方守卫 180 秒自报
  `combat_no_progress`）；随后另起客户端**开新局** → 一路正常（`08:13Z` 起，一幕 14 层精英战，
  5 件遗物，其中不含赌博筹码与选择悖论）。
* **停顿的长度与解除者（这一条仍待确认，请勿按"永久死锁"理解）**：
  `07:50–07:59Z` 我方批次因 180 秒无进展守卫而自停并退出，**此后本方没有任何自动化在运行**
  （autoplay / keeper / supervisor 全部随批次结束）。而 mod 自己的日志显示 `08:06:12Z`
  它已经在为**某场战斗**做回合 1 寻路（`TURN_SETUP_ROOT_CAPTURE turn=1` →
  `UI_STATE state=searching` → `08:06:23 UI_STATE state=ready`，642 行里
  **零次** `DEPLOY_CHOICE_PAUSED`、**零条** error），即那个模态在 `07:59–08:06Z` 之间被答掉了。
  同一份会话日志里有 29 次 `FocusIn received`（窗口被点击/切前），且该客户端随后退出
  （`ERROR: 384 resources still in use at exit`），`08:09:33Z` 出现第二个游戏实例并开了新局。
  所以两种可能都成立且都还是要修：**人工点了一下才过**（→ mod 明明拥有该页面的处理代码却不自己点），
  或者**静默挂起 6–9 分钟后自行恢复且一行日志都不写**（→ 对使用者完全不透明）。
  本方无法从自己的日志区分这两者，故按最保守写法呈现。
* **不是「持有该遗物就每场都卡」**：同一局持有 `GAMBLING_CHIP` 期间正常打完约 30 场普通战斗；
  两次卡死都在 **boss 开局**。
* **不是所有开战选牌模态都有问题**：另一件遗物 `CHOICES_PARADOX`（选择悖论：每场战斗开始时从 5 张
  随机牌中选择 **1** 张）在同一局中连续约 10 层完全正常。差别是 **单选** 对 **多选/任意张**。
* **不是本方代为出牌能解决的事**：见下。

## 复现步骤（作者侧最短路径）

1. 铁甲战士 A10 开一局，在任意遗物奖励中拿 **赌博筹码**。
2. 推进到任意 **boss 层**（Act 1 floor 17 亦可；本次证据来自 Act 2 floor 33 与 Act 3 floor 49）。
3. 观察：战斗是否停在「选择任意张牌来替换」，mod 日志是否只有 0 字节的新 combat 文件，
   或是否出现 `DEPLOY_CHOICE_PAUSED` + `NativeChoiceSurfaceMismatchException`。

补充观察点：若在**普通战斗**层不复现、只在 boss 层复现，可能与 boss 开场的额外模态/动画
（例如 Act 3 双王、或女王开场的侵蚀类效果）在多选页面之后又改动页面有关 —— 即签名 A 的「计划提交
前页面发生变化」。

## 期望 对比 实际

* 期望：mod 能把这个模态回答掉（**空选择 + 确认**就是零效果的合法答案），或者在页面变化后
  重新读取、重新排计划并重试；失败时至少留下明确的日志。
* 实际：要么抛 `NativeChoiceSurfaceMismatchException` 后**暂停且不重试**（签名 A），
  要么根本不进入该路径、**不写任何日志**并把整场战斗锁死（签名 B）。两种情况都让 run 不可继续。

## 建议改动点（供参考，不代替作者的判断）

1. `NativeChoiceSurface.SelectChooseCardAsync`：把「页面选项数」从隐含假设改为**提交前重新读取并
   以其为准**；不一致时**重新规划并重试有限次数**，而不是 `DEPLOY_CHOICE_PAUSED` 后不再前进。
2. 文案与判定核对：异常写「三选一」，实际页面可为任意数量（本例 4 项）。
3. 开局模态的可达性：确认 full_auto 的驱动循环会在**第一次寻路之前**的选牌模态上启动
   `NativeChoiceSession`（否则就是签名 B 的互等死锁）。
4. 兜底可观测性：任何选择类模态停留超过 N 秒时输出一行 `Level=error` 的说明。本方 keeper
   目前只能看见「搜过路但没部署」的情形，对签名 B 这种「一行都没写」的静默阻塞需要另加触发条件
   （这是我们自己的待办，不是要求你们配合我们）。

## 本方的边界（为什么我们不自己点掉它）

我们的自动玩家**从不执行战斗动作**：验收契约规定每局只能有一个动作执行者，真机跑的是
`combat_solver_full_auto`。我们的桥接侧确实能表达 `combat_confirm_selection`，一发就能把页面过掉，
但那等于用「代为出战斗牌」把 run 推过去——正是那套验收标准要拒绝的东西。
因此这个问题只能由 mod 侧修复；修复后我们可以立刻复测（现有卡死会被
`combat_no_progress` / `solver_choice_paused` 两个探针自动判定，不靠人盯）。

日志与状态快照可提供：上面两个 `%APPDATA%` 路径、`combat_no_progress` 的三次停批记录
（批次 `ssb-20261007T071936Z-86be5479` 与 `ssb-20261007T075006Z-7584530b`，均在 Act 3 floor 49 同一状态；更早 06:05-06:19Z 那次是人工停批，那时守卫还不存在）。
