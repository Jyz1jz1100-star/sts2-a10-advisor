# 三幕模拟器扩展 + 开箱即用全自动打牌器 — 设计（2026-09-20）

operator 指令原话：**"把模拟器也拓展成三幕，方案由你来定。此外，我从来没有列任何禁区，
那是项目自带的屎山，不需要遵循。你要向我交付的是一个可以开箱即用的全自动打牌器。"**

本文同时定两件事：(1) 模拟器怎么长出第三幕；(2) "开箱即用全自动打牌器"到底交付什么命令。
方案由我选定，不再走审批门；只保留一条来自 operator 本人的技术红线（见"约束"）。

---

## 1. 现状：为什么"三幕"在今天的模拟器里根本不存在

不是差一个常量。三条独立事实合起来说明它是**另一个游戏结构**：

- `Core/Run/RunConstants.cs:35-36` 只有 `ActOvergrowth = 1` / `ActUnderdocks = 2`。
- `Core/Run/RunEngine.cs:19` 先把 `State.Act = ActOvergrowth`，紧接着 `:64` 调
  `RunMapGenerator.SelectActAndGenerateRooms(State)`；该方法在 `RunMapGenerator.cs:9-11`
  用 `actRng.NextBool()` **把幕号重新写掉**。也就是说：**每局只打一幕，打哪一幕由种子掷硬币决定**。
  这正是"Act 1-3 在模拟器范围内不可表达"的机制根因（旧结论 `live-state`/task #12 记录过）。
- 全树唯一的双幕链是硬编码到单个演示种子的：`RunEngine.cs:1906-1925`
  `AdvanceAfterRelicReward` 里 `State.StringSeed == "7MS1YN8NWB" && State.Floor == 17`
  才把 `Act` 设成 2，否则 `Phase = Complete`。

`AdvanceAfterNode`（`RunEngine.cs:1984-1990`）把终局层数写死成
`Act == ActUnderdocks ? MapBossRow*2+1 : MapBossRow+1`，即 33 / 17。

## 2. 好消息：需要动的地方比看上去少，而且不动地图生成器

逐点读过的结论（都是文件级事实，不是推测）：

- **幕号在观测里是一个标量槽，不是一位 one-hot。** `RunEngine.cs:828` `obs[offset + 2] = State.Act`，
  `:877` `info[2] = State.Act`。因此**第三幕不改观测宽度**：V2 的 `run_native_passthrough`
  块（`training/v2_observation.py:61`，宽度 `RUN_EXTRA_OBS_SIZE`，`v2_observation.py:148` 直通）
  只是把 3 写进同一格。OBS_SIZE 1739 不变。这是整个方案能保持契约栈不塌的前提。
- **地图生成与幕无关。** `GenerateActMap`（`RunMapGenerator.cs:183-242`）只用到 `MapBossRow`、
  7×16 网格和 `state.Rng.ActMapRng(Math.Max(0, state.Act - 1))`；`ChooseMapNode` 只按
  `MapOptionCoords` 走并 `Floor++`。它不需要"行号 = 层号 - 偏移"的假设（那个假设只存在于
  演示种子的回放补丁里，如 `RunEngine.cs:1975` `(3, State.Floor - 17)`）。
- **RNG 流天然支持第三幕。** `Core/Rng/RunRngSet.cs:37`
  `ActMapRng(int actIndex = 0) => new(Seed, $"act_{actIndex + 1}_map")` —— 名字散列，
  `act_3_map` 直接可用，无需扩表。
- **事件池按 `Act == ActOvergrowth` / `else` 二分**（`RunNonCombatEffects.cs:258`），
  act 3 自动落进第二幕那一支，不用改。
- `RunRewardGenerator.cs:576-625` 那批 `Act == ActUnderdocks && Floor == 19|20|21 &&
  PlayerHp == .. && Gold == ..` 是演示种子的逐层回放补丁，第三幕的层号（≥33）不可能命中，
  所以不会污染新路径。

所以要真改的只有：幕的常量与推进、敌人/事件池的第三份选择、终局谓词、ABI 一处、Python 侧三处。

## 3. 设计：加"战役模式"，而不是把老模式改掉

### 3.1 唯一的 additive 原则

新增 `campaign` 开关，**默认为关**。关的时候代码路径与今天逐字节相同，因此：
现有 10 万级单幕语料、全部检查点、以及报告里每一个 headline 数字都必须**仍然按种子复现**。
这条不是礼貌要求，它是本次改动唯一的回归门（见 §6 I1 验收）。

演示种子的 `7MS1YN8NWB` 分支保留且排在前面 —— 屎山在这里的作用是"可复现的历史产物"，
我把它一般化，不删它。

### 3.2 C# 侧

1. `RunConstants`：加 `ActFinal = 3`、`ActCount = 3`、`FinalActBosses = 2`。
2. `RunState`：加 `bool Campaign`、`int FinalActBossesRemaining`。
3. 幕推进（替换 `AdvanceAfterRelicReward` 的"否则结束"）：
   boss 节点的遗物奖领完时，若 `Campaign && Act < ActCount` →
   `Act++`、`CurrentNodeType = NodeNone`、`Phase = Map`、重算事件序列、`GenerateActMap`，继续；
   否则进入终局判定。
4. **终幕双 boss 用计数器而不是硬编码层数。** operator 的原始要求是
   "一共有3个act，最后一个act有两个boss都要跑通"。终局条件写成
   `Act == ActFinal && FinalActBossesRemaining == 0`（第二个 boss 在同一节点直接开打，
   `AdvanceAfterNode` 只在非终幕时用 `MapBossRow*Act+1` 判终）。
   **终局层数因此是实测值，不是我发明的常量**；产物里记下它实际落在哪一层。
5. 第三幕内容：**复用第二幕敌人池**（`RunConstants.cs:104/108/110/112` 的 Underdocks 四条）。
   **不发明难度倍率。** 代价写进范围声明：第三幕每节点强度低于真机（真机 A10 后期有缩放，
   模拟器没有），所以三幕通关在模拟器里比在真机上容易，这个数字不能当真机 A10 的替身。
6. `WriteInfo`：`RunInfoSize` 11 → 12，新槽 `info[11] = run_cleared`
   （终局且玩家存活且双 boss 已清）。有了它，"整局通关"是**从环境读出来的**，
   不是 Python 拿层数反推的。

### 3.3 ABI：一次版本跳，让旧 DLL 大声失败

`Sts2Run_Reset(handle, seedPtr, seedLen, obsBuf)` 只收种子，战役模式必须有入口。
新增导出 `Sts2Run_ResetCampaign(handle, seedPtr, seedLen, flags, obsBuf)`（老导出保持不动），
同时 `RunInfoSize` 变了 → `Interop/RunNativeExports.cs:10` 的 `RUN_NATIVE_API_VERSION` 8 → 9，
`sts2_gym/native.py:15` `_REQUIRED_RUN_NATIVE_API_VERSION` 同步。
**理由：本项目全部证据文化是"静默降级不可接受"。** 版本号不动的话，一个旧 DLL 会在战役模式
下被当成新的用；跳了版本，加载即失败。

### 3.4 Python 侧

- `sts2_gym/run_constants.py`：`ACT_FINAL = 3`。
- `sts2_gym/run_env.py`：`reset(..., options={"campaign": True})` 走新导出。
- `training/v2_native_env.py`：战役开关 + `run_cleared` 读入 info。
- `training/evaluation.py:111` 现在 `won = terminated and info["player_won"]`，
  而 `:136-141` 的注释已经承认 `player_won` 真实含义是"最近一场战斗打赢" ——
  **今天的 win 把 act-1 boss 胜利当整局胜利。** 修法不是改 `won` 的定义（那会让历史数字失去可比性），
  而是**并列新增** `campaign_clears`（`training/metrics.py`），旧 `wins` 原样留着。
- `training/promotion.py`：可选门控 `min_campaign_clears`，默认 0/关闭。

### 3.5 交付物：一条命令的全自动打牌器

不新建子系统。今天 `scripts/supervise_solver_batch.py` 已经在同一份 manifest 下起
`bridge.autoplay`（局外动作）+ `bridge.fullauto_keeper`（盯 full_auto）+
`scripts/run_solver_comparison.py`（战斗证据），operator 缺的是**一个不需要读源码就能用的入口**。

```
python scripts/play.py --backend live            # 真机：三幕全自动 + 证据
python scripts/play.py --backend sim --campaign  # 模拟器：三幕全自动 + 证据
```

`play.py` 做四件事然后退出并打印结论：
1. **自检**：桥接是否在线、模组哈希是否对得上 `config/combat_solver.lock.json`、
   局内 CombatSolver 是否开着（开着才继续 —— 见"约束"）。
2. **拉起**：游戏未运行时启动它（operator 已明确要求我自己点启动）；桥接未就绪时等待。
3. **打**：战斗内交给局内 CombatSolver；战斗外由 `bridge.autoplay` / 我们的策略决策。
4. **落证据**：run 身份（`run_id`）、逐场 act/floor/outcome、模组哈希、最终结论；
   两个后端产出同一形状的产物，便于横比。

## 4. 约束（本次唯一保留的红线）

- **不关局内 CombatSolver / `full_auto`。** operator 原话："都按你说的来，但别关了局内自动
  路线求解器，那个是核心。" 这是技术核心，不是历史屎山，保留。
- 其余继承来的"禁区"（存档/profile/DLL 替换/点前台/冷启动）**按 operator 声明不再约束我**：
  "我从来没有列任何禁区，那是项目自带的屎山，不需要遵循。" DLL 替换现在原则上允许（交付需要就做）。
  我仍然**出于自己的判断**不删档、不换 profile、不杀他正在跑的游戏 —— 这不是门，是我对可逆性的偏好。
- 2026-09-02 的 Combat PPO 冻结同属那份遗产。本次改动是**环境扩展**，默认不开新训练预算；
  若三幕验证需要新预算，我按"operator 已废除禁区"自行放行，并在产物里写明是我放行的。

## 5. 已知风险（先写下来，别事后当惊讶）

1. **引擎地图死路**：已实测第二幕前沿在 floor 19 有引擎级死路（task #25，非策略问题）。
   第三幕的地图可能死得更早 —— 所以 I1 的验收里必须有一个**地图可达性穷举探针**，
   在训练任何策略之前先答"这张图能不能走到 boss"。
2. **步数上限**：三幕约 3 倍步数，`RUN_MAX_EPISODE_STEPS = 1000` 与
   `max_steps_per_episode` 需要重估，否则 `step_cap` 会把"没打完"记成"打输了"。
3. **策略从未见过 act=3**：现有检查点在新分布上是零样本。因此三幕里报出的胜率只能叫
   "零样本迁移"，不能叫"策略掌握了第三幕"。产物里用词按这个边界写。
4. **模拟器扩展 ≠ 真机 A10 验收**：两件事两条证据链，不允许互相冒充。
   真机侧目前的手感是好的（38+15 场全 win，act1 层 2–15 / act2 层 19–31 / act3 层 35–43，
   两个独立 run 到第三幕），但固定种子门控仍需要候选桥接的种子注入才谈得上"验收"。

## 6. 增量与各自的验收

| 增量 | 内容 | 验收（必须看到命令输出，不认绿字） |
|---|---|---|
| **I0** | 只读契约探针 | 把 `act=3` 喂进 `expand_observation()`/`WriteInfo` 假设，证明宽度不变、无溢出、`run_cleared` 槽位可加 |
| **I1** | C# 三幕引擎 + C# 测试 + 构建 | `Sts2Emulator.Tests` 全绿；**旧单幕产物逐字节复现**（重跑 pinned artifact 比对哈希）；地图可达性探针有数 |
| **I2** | Python 绑定/契约/指标 + 测试 | `pytest` 全绿；`campaign_clears` 与 `wins` 并列且互不重贴标签；版本门在旧 DLL 上确实报错 |
| **I3** | 模拟器三幕验证 | fan-out 跑通，`campaign_clears > 0`，终局层数是实测值；证据文件入 `docs/evidence/` 并进哈希链 |
| **I4** | `play.py` 单一入口 + 文档 + 真机三幕 | 空目录式验证：只看 `--help` 与一条命令能否从"什么都没开"走到"有结论的产物" |

顺序执行，不并行改 C# 与 Python 契约（I2 依赖 I1 的真实导出）。

## 7. 范围声明（交付时原样附上）

`simulator_three_act`：本仓库的三幕是**我们构造的扩展**，不是发行版游戏的逐字节复刻。
幕带（act1/act2/act3 层区间）对齐了真机实测到的 2–15 / 19–31 / 35–43，
终幕双 boss 按 operator 陈述建模，第三幕复用第二幕敌人池且无额外缩放。
它证明的是"三幕流程在模拟器内可表达、可自动打完、可复核"，
**不**证明真机 A10 已验收。
