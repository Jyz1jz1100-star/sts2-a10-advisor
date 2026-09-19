# 夜间 Act 1 战役评估（2026-09-19）

## 先读：目标里的"Act 1–3 全流程"在模拟器范围内**不可表示**

目标要求 Ironclad A10 的 **Act 1–3** 全流程胜利（真机第三幕有两个 boss）。这一条
不是"还没训练到"，而是**当前这台模拟器结构上没有第三幕**，四条源码证据（均为
`third_party/slay-the-spire-2-emulator-main`，可在盘上逐条打开核对）：

1. **幕的枚举只有两个值。** `src/Sts2Emulator/Core/Run/RunConstants.cs:35-36` 只有
   `ActOvergrowth = 1` 与 `ActUnderdocks = 2`，没有任何第三个 act 常量。
2. **开局按种子二选一。** `RunMapGenerator.cs:9-11`：`actRng.NextBool()` 决定
   `state.Act` 是 underdocks 还是 overgrowth——一次运行只生成**一幕**。
   （本晚普查 10000 局得到 overgrowth 5014 / underdocks 4986，正好是这次枚举的分布证据。）
3. **唯一的跨幕分支被硬编码种子门控。** `RunEngine.cs:1907-1920` 的
   `AdvanceAfterRelicReward` 只在 `StringSeed == "7MS1YN8NWB" && Floor == 17` 时把
   Act 置为 Underdocks 并重生成地图；任何其它种子在 boss 后直接
   `Phase = Complete`（同文件 1922-1923）。即"链到第二幕"这件事只对**一个演示种子**成立，
   而且只链 Act 1 → Act 2。
4. **终局层数因此也是两档。** `RunEngine.cs:1986-1990`：`terminalFloor` 对 overgrowth 是
   `MapBossRow + 1 = 17`，只有 underdocks 才是 `MapBossRow * 2 + 1 = 33`（而 underdocks
   能走到 33 的，实际只有上面那条被门控的演示种子链）。

**含义**：模拟器的可达上限 = "单幕通关"（Act 1 或 Act 2 到 boss 并取胜，floor 17），
最多加上那条**写死的** 1→2 双幕链。它不含 Act 3，也不含"第三幕两个 boss"。

**层号对齐**（目标原文写的是"16 层到终局"，本文所有读数写的是 17，这两个是同一个东西）：
`RunConstants.cs:14` 的 `MapBossRow = 16` 是**0 基的地图行号**，而
`RunEngine.cs:18` 把 `Floor` 从 **1** 起算，所以同一个 boss 节点在 Python 侧就是
`Floor == 17`。本文判"到终局"的口径是 `final_floor == 17` **且** `won` 为真
（`training/evaluation.py` 的 `player_won` + `terminated`）；只有 `final_floor == 17`
不构成通关，见下文"层号含义"与"boss 战僵持"两节。

所以本文给出的所有胜利证据，**最多只能证明目标的前两幕部分**，
按字面判定目标**未完成**；真正的 Act 1–3 全流程只能走真机路径
（见 `docs/ACCEPTANCE.md` 与任务"操作员开机后跑真机三幕批次"）。
我没有去给模拟器补一个第三幕——那等于凭空发明游戏内容，产出的"胜利"不能当作游戏的证据。

## 结论

V2 契约栈**第一次记录到 Act 1 终局胜利**，且证据可复核（检查点与种子分区均可重算）：

| 臂 | 分区 | 局数 | win_rate | max_final_floor | 非法动作 | 未分类死局 | 检查点复算 | 独立复现 |
|----|------|------|----------|-----------------|----------|------------|------------|----------|
| `b_terminal-0`（fan-out） | promotion（未见种子） | 500 | 0.004（2 胜） | 17 | 0 | 0 | 一致 | **已复现** |
| `b_terminal-1`（fan-out） | promotion（未见种子） | 500 | 0.012（6 胜） | 17 | 0 | 0 | 一致 | **已复现** |
| `c_explore-0`（fan-out） | checkpoint @1M | 100 | 0.010（1 胜） | 17 | 0 | 0 | 一致 | **已复现** |
| `b_terminal-0`（fan-out） | checkpoint @1M | 100 | 0.020（2 胜） | 17 | 0 | 0 | 一致 | **已复现** |
| `a_base`（手工臂） | checkpoint @1M | 100 | 0.010（1 胜） | 17 | 0 | 0 | 一致 | **已复现** |

战役总览（`python scripts/report_act1_campaign.py --root runtime/act1_overnight
--root runtime/fanout --root runtime/act1_overnight/fan_cont`，2026-09-19 05:58 本地
时间复核过一次、00:34 再跑一遍结果相同；**这批战役臂本身已结束**——文首那 20 个臂
无残留训练进程。00:31 起另有两臂在跑，但它们在 `runtime/act1_ab/` 下，
不在上面三个 `--root` 里，所以不影响这些数字）：**20 个臂、110 份评估文件、61 份记录到非零胜局**
——其中 **41 份来自 500 局的未见 promotion 分区**。这 61 份**全部**满足
`illegal_actions=0`、`unclassified_dead_ends=0`、`checkpoint_verified=True`、
`max_final_floor=17`；最高 `win_rate = 0.02`（100 局臂），未见 500 局分区上最高
`win_rate = 0.014`。

也就是说"能不能赢一次"已经不再依赖单点观察：**多臂、多分区、多时间点上重复出现**。
但仍未越过任何晋升门槛（act1 门槛是 35% 胜率 / Wilson 下界 31%），
所以这批证据的结论是"可达"，不是"可靠"。

**先读这条修正（分幕拆解后追加，位置在所有数字之前）**：上面表格与总览里的
`win_rate` 都是**两幕混合**的"单幕通关率"——模拟器按种子随机决定这一局生成在
Act 1 还是 Act 2，而训练栈不记录 act。把 10000 局按生成幕拆开后：
**Act 1 自己只有 3/5014（0.06%），Act 2 有 65/4986（1.30%）**。
因此严格回答"第一幕能不能通关"这个问题的证据是 **3/5014**，不是 0.4%–2%；
混合数字把 Act 1 的能力显著高估了。而且按分幕速率回算，那条被当作里程碑的
6/500 记录**很可能一局 Act 1 胜利都不含**（其前缀只有 254 个 Act 1 种子，
期望胜局 0.15）。这 3 局已点名并可逐个种子复现（见"三条可逐 seed 复核的
Act 1 终局胜利"），它们才是本文对"Act 1 能赢一次"的真正证据。详见"分幕拆解"一节。

历史对照：本仓库此前 **22 份 V2 指标文件里 win_rate 全为 0**，所以这是第一次非零。
`max_final_floor = 17` 表示这一局走到了 **boss 节点**（0 基第 16 行，在 `Floor` 里
就是 17；见下文"层号含义"），不是被步数上限中途截断——但它**不等于打赢 boss**，
能证明打赢的只有 `win_rate` / `wins` 本身。上述全部非零记录的
`illegal_actions = 0`、`unclassified_dead_ends = 0`、`defect_truncation_rate = 0`。

哈希链（可自己重算）：

- `b_terminal-0` 检查点 `final.zip` SHA-256 `1E18AAE64C2EA673…`，
  记录于 `runtime/fanout/b_terminal-0/v2curriculum-20260918T183227Z/act1/metrics/promotion.json`；
- `a_base` 1M 检查点 SHA-256 `e3b8548c8356…`，种子分区摘要 `7a4b18229719…`。

复核方式：`scripts/report_act1_campaign.py` 重新打开并复算每个被引用的检查点；
`scripts/reevaluate_checkpoint.py` 用**同一份** `training.evaluation.evaluate_policy`
（不是重写实现）在同种子分区上重跑评估，逐项比对
`win_rate / illegal_actions / unclassified_dead_ends / max_final_floor`。

这条链的边界也要说清：上面所有路径都在 `runs/`、`runtime/` 之下，而这两个目录被
gitignore——**检查点与指标文件本身不入库**。因此这份"哈希链"是"在持有这批产物的
这台机器上可复算"，不是"任何人 clone 后都能复算"。摘要值写在正文里，是为了让拿到
产物的人能验证没被改动；要让结论脱离本机成立，得把对应检查点与 metrics 显式归档
（本仓库历来不这么做，属于既定取舍，不是本轮遗漏）。

## 范围声明（必须一起读）

1. **`scope` 是 `simulator_act1`，不是真机 A10 验收**。最强的未见分区记录是
   6/500（`win_rate = 0.012`，`wilson_95_low = 0.0055`），离 act1 门槛要求的
   35% 点估计 / 31% 下界还差约一个半数量级；100 局 checkpoint 分区上的 2/100
   只说明"这个种子段里赢过"。这批数字证明"能赢一次"，不证明"能赢"。
2. **"模拟器只有第一幕"这句我写错了，正确结论更强也更有意思：模拟器一局只跑一幕，
   而它有两幕。** 实测（走 `training.v2_curriculum._environment_factory` 这条真实评估
   路径，不是读常量名猜的）：`Sts2Emulator/Core/Run/RunMapGenerator.cs:10` 用
   `actRng.NextBool()` 决定这一局生成在哪一幕，对该臂 `act1/promotion` 分区前 40 个
   种子普查得到 **20 局 `act=1`(overgrowth) / 20 局 `act=2`(underdocks)**，且 act
   严格由种子决定（重复构造逐个一致）。`RunConstants` 只定义
   `ActOvergrowth = 1`、`ActUnderdocks = 2`——**没有第三幕**；
   两幕的 boss 都放在 `RunConstants.MapBossRow`（第 16 行），`AdvanceAfterRelicReward`
   在该节点后把 `Phase` 置为 `Complete`，所以**一局无论落在哪一幕都在第 17 层结束**
   （实测两幕的 `max_final_floor` 都是 17）。`AdvanceAfterNode` 里那个
   `MapBossRow*2+1 = 33` 只服务于硬编码演示种子 `7MS1YN8NWB` 的"Act 1 打完接 Act 2"
   串联分支，与普查到的独立生成局无关。除该种子外不存在幕间串联。
   所以对"Act 1-3 全流程"的准确说法是：**模拟器既没有第三幕，也不会把多幕串成一局**。
   唯一例外是硬编码种子 `7MS1YN8NWB` 的 Act 1→Act 2 串联分支，本轮专门去试过它。
   **这里原先写的"存在但不可达"是错的，已被更大样本推翻**：当时只试了四个检查点
   （b_terminal-1 的 2M/4M/final、b_terminal-0 的 final），它们确实全死于 floor 6–8、
   没有一个到第 17 层，于是得出了"这条路不可用"。第二晚把**全部 76 个 act1 检查点**
   都跑了一遍，其中 **3 个打赢了脚本化的 Act 1 boss 并真的进入 Act 2**
   （最深 Act 2 floor 22，76 局合计 0 非法动作），所以"不可达"不成立。
   正确的说法是：**可达，但不是有效的胜利证据**——该种子的 Act 1 与 Act 2 两侧
   都被留档 trace 钉住，3 个进入 Act 2 的检查点里有 2 个在满血状态下被**环境侧**
   截断在 floor 19。细节与哈希见文末"双幕链实测"一节。
   随机合法动作在 floor 3 就死，所以随机策略连串联点都摸不到；
   这一路的进入条件是先打赢 Act 1 boss，而实测 Act 1 通关率只有 3/5014。
   三幕则是**不存在**（不是"不可达"，是没有这一档）。
   这一条是量出来的，不是推断的，写在这里以免下一棒再花一夜去试。
   更要紧的口径后果：训练栈里没有任何一处读取或校验 act（只在
   `training/v2_native_env.py:139` 把它塞进 info），因此本文所有
   `scope: simulator_act1` 的记录，真实人群是**一半 Act 1、一半 Act 2 的"单幕通关率"**，
   不是纯 Act 1 数字。这个 50/50 不是"连续整数种子的奇偶伪影"：对该臂
   `act1/train` 分区起点按步长 100003 抽 40 个种子，得到 **22 局 Act 1 / 18 局 Act 2**，
   且两种奇偶里都同时出现两幕——所以**训练分布本身也是混合的**，
   整个 "act1 课程阶段" 实际是"随机单幕"课程。分幕之后的两个数字见"分幕拆解"一节。
   真机侧**已核实的最远距离是 Act 2 floor 30**（seed `1600000001`，`game_over` +
   `hp=0`，带可归属 trace 与 assessor 的 `terminal_zero_hp`，见
   `docs/LIVE_PROGRESS_2026-09-07.md`）。也就是说"能进第二幕"有终局证据，
   "通关三幕"至今一例都没有——两者都不在本文的口径里。
3. 目标要求的"逐阶段 warm-start 阶梯"在这里体现为：所有臂都从 promoted floor6
   检查点 `--initial-checkpoint` 热启动，经 `scripts/run_curriculum_fanout.py`
   以互不重叠的种子分区并发跑（工具会拒绝落入 teacher 保留区
   `>= 1_410_000_000` 的种子，也拒绝分区重叠）。
   **这句话按审计口径要说清哪些是证据、哪些只是叙述**：核对臂的运行目录后，
   `plan.json` 只记录契约（stages / seed_partitions / observation_contract /
   emulator 等），**不记录热启动父检查点**，臂目录里也没有任何 `resume-*.json`
   或文件提到 `floor6`——也就是说这 20 个臂"从 promoted floor6 热启动"目前只能靠
   启动脚本（`runtime/act1_overnight/launch_fanout_v2.ps1`，在 gitignore 的
   runtime/ 下，临时且未入库）佐证，**不是产物级证明**；`runtime/fanout/*` 那批
   （含本文三条 Act 1 胜利所在的 b_terminal-1）连启动脚本都没留下父检查点名。
   已修并实跑验证（两处，因为我第一次只修了一半）：`training/v2_curriculum.py`
   现在把 `warm_start`（父检查点路径 + SHA-256 + 是否存在）写进每次新运行的
   `plan.json`；而**阶梯真正的每一级**——stage→stage 的接续——原先依然无处可查，
   所以每个 stage 目录另写 `origin.json`（该级初始化自哪个检查点、其 SHA-256、
   由哪一级产出、是否来自命令行热启动）。用真实两级 smoke 跑验证过链路：
   `floor3 ← CLI 父检查点（哈希一致）`、`floor6 ← floor3 的 final.zip（哈希一致，
   from_stage=floor3, cli_warm=false）`。**本文以上所有臂的血缘仍是历史声明，不回溯成立。**
   上面第 2 条的混幕问题**对阶梯各级的影响不一样**，这点也核对过：
   `training/v2_run_wrapper.py` 的边界语义只看"下一状态的 floor 是否越过此前最高的
   边界节点"，`max_floor` 是纯层号边界（且明确"是课程边界、不是胜利条件"），
   所以 floor3/6/10/13 各级的**目标语义与幕无关**——"走到第 6 层"在两幕里是同一件事，
   从这些级 promoted 出来的检查点作为"深度能力"仍然成立。
   出问题的是没有 `max_floor` 的 act1 级：它的终止条件是"自然终局"，而自然终局在
   两幕里是不同的**内容**（虽然都在第 17 层，见下文分幕拆解），于是这一级的胜率
   是两幕两种难度的混合，无法回答"Act 1 到底能不能通关"。
   混合本身确实有影响，但**方向和我的猜测相反**：不是 Act 2 更难，而是 Act 2 明显
   更容易（0.0130 对 0.0006），所以混在一起会把 Act 1 的数字抬高约一倍以上。

## 复现状态

**已复现五次，跨四个不同臂与两种不同分区（未见 promotion 500 局 ×2、checkpoint
100 局 ×3）。** 其中 `b_terminal-1` 的 6/500 是这批里最强的一个未见分区证据。
`scripts/reevaluate_checkpoint.py` 独立重跑了
`b_terminal-0` 的 500 局 promotion 评估、`c_explore-0` 与 `b_terminal-0` 的
100 局 checkpoint 评估，以及（2026-09-19 00:35，本文最后一格"待复现"）手工臂
`a_base` 的 100 局 checkpoint 评估（`runtime/a_base_reeval.log`：检查点哈希、
种子哈希、`win_rate 0.01`、`illegal 0`、`unclassified 0`、`max_final_floor 17`
逐项一致），至此**结论表五行全部独立复现**。

复核要用**装了 numpy/torch 的那个解释器**（模拟器自己的 venv），仓库 `.venv` 与
`.tools/python` 都不含评估依赖；用错解释器只会以 `ModuleNotFoundError` 崩掉，
而经过管道时退出码还会显示成 0。命令与第一例输出如下：

```
"G:/qoder/third_party/slay-the-spire-2-emulator-main/.venv/Scripts/python.exe" \
    scripts/reevaluate_checkpoint.py \
    --metrics runtime/fanout/b_terminal-0/v2curriculum-20260918T183227Z/act1/metrics/promotion.json \
    --config runtime/fanout/b_terminal-0.toml
```

```
checkpoint hash OK (1E18AAE64C2EA673…)
seed partition  500 seeds 130010000…130010499
seed hash       OK
win_rate                 recorded=  0.004  reevaluated=  0.004  OK
illegal_actions          recorded=      0  reevaluated=      0  OK
unclassified_dead_ends   recorded=      0  reevaluated=      0  OK
max_final_floor          recorded=     17  reevaluated=     17  OK
VERDICT: reproduced        (exit code 0)
```

复核的是同一条代码路径：脚本加载记录的 `final.zip`、按记录的分区重建种子并先校验
`seed_sha256` 与 `checkpoint_sha256`，再调用与训练器相同的
`training.evaluation.evaluate_policy`。它不是"又跑了一次自己的实现"，因此
`win_rate` 相等才有意义。

结论限定在：**存在可复核、可复现的 V2 Act 1 终局胜利（最强一条 6/500，五次独立复现）**。
它不等于策略能稳定赢，更不等于三幕通关（见范围声明）。

## 同一分区上的横向对照（这条比上面的记录数更重要）

上面 61 条非零记录各自跑在**不同的种子分区**上，因此"某臂 2%、另一臂 0.4%"并不是
同一把尺子量出来的排名。`scripts/evaluate_checkpoint_series.py` 把分区固定成
`act1/final`（与训练分区经校验互不相交，本晚的评估从未使用过），只换权重：

`b_terminal-1` 同一个 run 的四个检查点，200 局同一批未见种子
（分区 130020000…130020199，`seed_sha256 = d6c2abdd9243b800…`）：

| 检查点 | 胜局 | win_rate | mean_final_floor | max_final_floor | 非法 | 未分类 |
|--------|------|----------|------------------|-----------------|------|--------|
| 1M steps | 0/200 | 0.0 | 8.64 | 17 | 0 | 0 |
| 2M steps | 1/200 | 0.005 | 8.22 | 17 | 0 | 0 |
| 3M steps | 1/200 | 0.005 | 8.32 | 17 | 0 | 0 |
| 4M steps | 0/200 | 0.0 | 8.17 | 17 | 0 | 0 |

结论要说清楚，因为它和"记录数在涨"是两件事：

1. **同一分区上看不到学习曲线。** 1M→4M 步之间 win_rate 在 0 与 0.5% 之间来回，
   `mean_final_floor` 钉在 8.2–8.6 没有斜率。所以"多臂多分区反复出现胜局"证明的
   是**可达性稳定**，不是**策略在变强**；把 61 条记录读成进步，是这批证据不支持的。
2. **能到终局，但过不去。** 每个检查点的 `max_final_floor` 都是 17，
   `mean_final_floor` 稳定在 8.2–8.6。这两条合起来只能说明"有局能走到幕终局、
   平均深度很浅"，**不能**区分"普遍死在第 8 层"和"一半死在 6 层、一半死在 16 层"。
   （`by_encounter` 与 `mean_final_hp_fraction` 在这批文件里是空的，这不是 bug：
   它们只在单场遭遇评估里填，整局评估的 `episode.encounter` 按设计为 None。）
   当晚补上了缺的那次测量（`training/metrics.py` 指标 schema 升到 4，新增
   `final_floor_histogram`；v3 旧文件没有这个键，读作"未测"而非"全为零"），
   同一个 4M 检查点、同一批 200 个未见种子的终局层数分布是：

   | 终局层数 | 2 | 4 | 5 | 6 | 7 | 8 | 9 | 11 | 12 | 13 | 14 | 15 | 17 |
   |---|---|---|---|---|---|---|---|---|---|---|---|---|---|
   | 局数 | 1 | 7 | 31 | 35 | 34 | 32 | 10 | 15 | 10 | 9 | 3 | 5 | 8 |

   - **132/200 = 66% 的局死在第 5–8 层**，这是最有统计力的部分；
   - 只有 16/200（8%）走到第 14 层及以后，**8/200（4%）走到第 17 层**；
   - 这 8 局**无一获胜**（本场 win_rate 0.0）。

   层号含义按引擎代码核对过，不靠猜：`Sts2Emulator/Core/Run/RunEngine.cs:18` 让
   `Floor` 从 1 起（所以 0 基的第 16 行 boss 节点就是 `Floor == 17`），而
   `RunEngine.cs` 的 `AdvanceAfterNode` 判 `Floor >= MapBossRow + 1`（= 17）才把
   `Phase` 置为 `Complete`。Python 侧 `training/evaluation.py:108` 的 `final_floor`
   取 episode **最后一步** info 里的 `floor`。所以 **17 = boss 节点本身**，
   不是"已经打赢"：死在 boss 与打赢 boss 的局终局层数都是 17，只能靠 `won` 区分。
   （**此处不完整**：第二晚实测还有第三种——在 boss 战里**僵持**到步数上限，
   `terminated=False` 且 `truncated=True`，见文末"boss 战僵持"一节。）
   第 16 层从不出现在终局层数里也说得通——在那一层死掉的局本来就极少。

   所以瓶颈的主次是清楚的：挡住胜率的是**前中段的消耗**（66% 在 5–8 层出局），
   boss 是第二道闸——真正站到 boss 面前本就只有 4%。
   一点保留：0/8 与"boss 胜率 1%–10%"并不矛盾，样本太小，不能据此说 boss 已被排除。
3. 因此"同等配置再多跑几夜"的预期收益被压低了：曲线是平的，而瓶颈在前中段消耗，
   不在夜时长。
4. 这条负结论只在模拟器 Act 1 口径内成立（`scope: simulator_act1`）。

### 全盘扫描后的防误读一段（别把历史数字读成"我们退步了"）

把本机**所有** 274 份 V2/V1 指标文件按 `win_rate` 排序，act1 口径里最高的一批
不属于本晚战役，而属于 8 月 31 日的 V1 课程运行：
`curriculum-20260831T205351Z/act1`，`step_..._02` 是 **5/100（0.05）**，另有
`10/500（0.02）`。看上去比本晚的 0.012–0.02 更高，但**不能与本晚比较**，三条理由都可在盘上核对：

1. **契约不同。** 那份运行的 `metrics` 是 `schema_version 1`、无 `v2` 段，其
   `plan.json` 也没有 `v2_config_version` / `observation_contract` 两个键——即 V1
   观测与动作定义。V2 栈是 **1739 维扩展观测** + **`Discrete(225)`**（225 =
   `RUN_MAX_ACTIONS` 32 × `TARGET_SLOTS` 7 + 1，7 = 无目标 + 6 个敌人位；来自
   `training/v2_observation.py:80` 的 `sum(width for _name, width in _BLOCKS)` 与
   `training/v2_flat_env.py:65`）。加载 V1 检查点属于形状与语义双重错配，任何
   "同一把尺子"的假设都不成立。两个数字可一条命令复核：
   `../third_party/slay-the-spire-2-emulator-main/.venv/Scripts/python.exe -c
   "from training.v2_observation import OBS_SIZE; from training.v2_flat_env import FLAT_SIZE; print(OBS_SIZE, FLAT_SIZE)"`
   → `1739 225`。
2. **它们同样是两幕混合的，而且这一点已无法回溯核实。** 同一台模拟器、同一个按
   种子选幕的生成器，所以 5/100 大概率以 Act 2 胜利为主，而不是 Act 1 能力的证据。
   注意这只是**推断**：`schema_version 1` 既没有 `act` 字段，也不记录原始种子
   （只有 `seed_sha256`，`seed_count = 100`），因此无法像本晚那样把这 5 局拆到幕。
   顺带一个反向信号：同一份文件 `mean_final_floor = 7.24`（本晚 V2 各臂为 8.36–8.37），
   即"更高胜率"并没有伴随"走得更深"——跨契约的 `final_floor` 定义本身也未必同尺，
   这更说明两个体系的历史数字不该并排读。
3. **那份旗舰记录自身不是契约干净的。** 同一个 `step_000002000004.json` 里
   `illegal_actions = 1`（100 局中 1 次非法动作），而本晚在 10000 局上是 0。
   目标要求的是"0 非法动作、无未分类死局"，按这条口径 V1 的 5/100 连参赛资格都不满足。

结论：**目前盘面上不存在任何一个按幕拆开后的 Act 1 历史基线**，因此"V2 战役相对
V1 是进步还是退步"这个问题现在无法回答；能回答的部分只有本晚给出的
"V2 某臂 Act 1 = 3/5014"。若要把这个问题做扎实，做法是用
`scripts/split_win_rate_by_generated_act.py` 对某个 V1 时代检查点在其自己的
V1 环境里重跑并按幕拆分——那是另一次工作，不该被本晚数字顺带代替。

## 分幕拆解（结果和预期相反）

`scripts/split_win_rate_by_generated_act.py` 先普查种子生成在哪一幕，再分别用
同一份 `training.evaluation.evaluate_policy` 评估。**注意口径**：这一跑用的是该臂
`act1/promotion` 的**全部 10000 个种子**（不是本文上面那条 500 局记录的前缀），
所以它是新证据，不是把 6/500 拆开——检查点 `step_000002000016.zip`
（SHA-256 `A1ADA27AD997A425…`）。

普查：5014 局 overgrowth(Act 1) / 4986 局 underdocks(Act 2)，act 严格由种子决定
（复采 10 个种子全部一致）。分幕结果：

| 幕 | 局数 | 胜局 | win_rate | mean_floor | max_floor | 非法 | 未分类 |
|----|------|------|----------|------------|-----------|------|--------|
| Act 1 (overgrowth) | 5014 | 3 | **0.00060** | 8.37 | 17 | 0 | 0 |
| Act 2 (underdocks) | 4986 | 65 | **0.01304** | 8.36 | 17 | 0 | 0 |

三条结论，其中两条推翻了我自己几小时前的判断：

1. **本文所有"Act 1 胜利"数字，主体其实是 Act 2 的胜利。** 合并看是 68/10000
   （0.68%），但拆开是 Act 1 千分之 0.06、Act 2 百分之 1.3——相差约 22 倍。
   按目标要求逐项核对的话：**"Ironclad A10 第一幕通关"的可复核证据只有 3/5014**，
   而不是上面表里那些 0.4%–2% 的数字；那些数是把两幕混在一起的"单幕通关率"。
2. **我给出的结构性解释是错的，已被推翻。** 我猜"Underdocks 要到 33 层才算终局，
   所以一半的局天生更难"。实际 `RunMapGenerator.cs:189` 对两幕都把 boss 放在
   `MapBossRow`（第 16 行）→ **两幕都在第 17 层结束**，33 只出现在那个硬编码演示
   种子的"串联"分支里；两幕的 `mean_floor` 也几乎相同（8.37 / 8.36）。所以差别不在
   深度要求，而在**这个策略恰好把 Act 2 打得比 Act 1 好得多**——最可能是 Act 1 的
   boss 是唯一过不去的墙，而 Underdocks 的 boss 对它更友好。
3. 契约口径在这个样本量上依然干净：10000 局里 `illegal_actions=0`、
   `unclassified_dead_ends=0`，两幕各自成立。这把"V2 契约栈不产生非法动作/未分类
   死局"从千局级抬到了万局级，是本文里唯一随样本量**变强**的结论。

顺带一条统计诚实：上面那条 6/500 的 promotion 前缀落在这次 10000 分区之内，
500 局里出 6 胜，而整分区 10000 局只出 68 胜——那个前缀是**偏赢的一段**，
不能当作分区的代表性估计。今后引用该臂胜率请以 10000 局分幕数字为准。

这条修正还要再推进一步，因为它直接关系到"本文最强的那条 Act 1 证据"到底是什么：
普查那 500 个前缀种子生成在哪一幕，得到 **254 局 Act 1 / 246 局 Act 2**
（同样是种子确定、复采一致）。用上面的分幕速率回算期望：

- Act 1 期望胜局 = 254 × 0.0006 ≈ **0.15**；Act 2 期望胜局 = 246 × 0.0130 ≈ **3.2**；
- 观测到 6 胜。按 Poisson 计，这 6 胜里"至少有一局来自 Act 1"的概率只有约 14%，
  也就是说**这条被当作里程碑的记录大概率一局 Act 1 胜利都不含**。

两种解释都成立且无法从现有数据区分：要么 6 胜几乎全在 Act 2（与 0.15 的期望一致），
要么这个前缀本身就偏向 Act 2 的好种子。无论哪种，把 6/500 读作"Act 1 能通关"都是
不成立的。真正能指名道姓的 Act 1 胜利，只来自上面 5014 局里的 3 局，种子已点出并
逐个复现，见下面"三条可逐 seed 复核的 Act 1 终局胜利"。

### 三条可逐seed复核的 Act 1 终局胜利（本轮最强、也最诚实的产出）

Act-1 单独评估把胜利种子写进了 `winning_seeds`（指标 schema 6），于是"一次可复核的
胜利"第一次有了可点名的对象，而不是一个比率：

- 检查点 `step_000002000016.zip`，SHA-256
  `A1ADA27AD997A425E6AD10D33C13D11F5A2F2C3A67003732A68F27CF73E57C1E`；
- Act-1 子集（5014 个种子）的分区摘要
  `bab5c7a69e07bdcf…6c7d65`；
- 获胜种子：**130015189、130017978、130019400**。

三者逐个复现，每一局都独立验证了"该种子生成的确实是 Act 1"：

```
"G:/qoder/third_party/slay-the-spire-2-emulator-main/.venv/Scripts/python.exe" \
  scripts/split_win_rate_by_generated_act.py \
  --config runtime/fanout/b_terminal-1.toml --stage act1 --split promotion \
  --act 1 --only-seeds 130015189 \
  --checkpoint runtime/fanout/b_terminal-1/v2curriculum-20260918T182051Z/act1/checkpoints/step_000002000016.zip

  act1/promotion: 1 seeds, generated acts: {'overgrowth': 1}
    act 1 (overgrowth) 1/1 win_rate=1.0 mean_floor=17.0 max_floor=17 trunc=0.0 illegal=0 unclassified=0
```

三个种子都是这个结果（1/1、floor 17、0 非法、0 未分类）。

口径要说满：这证明的是**存在三局可指名、可确定性复现、契约干净的 Act 1 终局胜利**，
不证明策略能赢——它在 5014 局里只赢 3 局（0.06%）。另外那 5014 局里有 1 局是被
分类过的截断（`defect_truncation_rate = 0.0002`），`unclassified_dead_ends` 保持 0，
所以"无未分类死局"成立，但"零截断"不成立，别混着引用。

## 仍在进行

- 战役已结束（05:58 无残留训练进程）。3 个续训臂从已验证的 `final.zip` 热启动、
  换新种子段（400M/406M/412M），最终并入上表的 20 臂 / 110 份 / 61 条非零记录。
- **下一夜不建议照同样形状重放。** 同一分区上的曲线是平的（见上节），且终局层数
  分布显示 66% 的局死在第 5–8 层：要动的是前中段的深度，而不是同等配置的臂数与
  时长。**"等操作员定"的那条已经做了一部分**：第二晚起，"按幕过滤 act1 阶段的
  训练种子"从判断变成了预注册的 A/B（见文末第三阶段），并且顺带补上了
  1M→4M 的**配对**零效应检查。仍未做的是针对 5–8 层遭遇的奖励/采样消融。
- 真机侧的代码拦路石已经清掉：grammar v2 合入主干并进入验收套件
  （47/47；15 会话真机回放零漂移），逐批次模组自证也已落地
  （`combat_solver/modpin.py` + `scripts/supervise_solver_batch.py`）。
- 剩下的拦路石不是代码：`127.0.0.1:15526/health` 在 04:37、05:31（本地）
  与 23:42、00:42（UTC，等价本地 07:42、08:42）四次实测**均拒连、无游戏进程**。
  supervisor 设计上**永不启动游戏**（只 GET 探测然后
  `game_wait_timeout`），这个设计此刻是有意的——profile 存档树在 00:03–00:04
  被写过，冷启动新运行可能顶掉在跑的一局。三幕需要操作员本人开机后再跑：
  `python scripts/supervise_solver_batch.py --mode observational --allow-actions
  --max-battles 50`。

## 双幕链实测：模拟器能表达的最长流程（第二晚追加）

既然第三幕不存在，模拟器能表达的**最长**流程就是那条被硬编码门控的 Act 1 → Act 2 链。
它可以从现有 V2 栈直接跑到：`Sts2Run_Reset` 接受的是 **UTF-8 种子串**
（`src/sts2_gym/native.py:402`，我们的 `training/v2_native_env.py:62` 用 `str(seed)` 传入），
所以把种子写成 `"7MS1YN8NWB"` 就能进入 `RunEngine.cs:1909` 的分支。
工具：`scripts/probe_chained_act_flow.py`（终止/胜负/非法动作判定语义照抄
`training/evaluation.py`，以免与本晚口径漂移）。

把战役里 **全部 76 个 act1 检查点**各跑一遍该种子
（`runtime/chained_demo_seed_sweep.json`，`--max-steps 60000`）：

| 结果 | 计数 | 说明 |
|------|------|------|
| 打赢脚本化 Act 1、**真正进入 Act 2** | **3 / 76** | 检查点见下表 |
| 在 Act 2 里阵亡（`hp=0`，`terminated`） | 1 | 最深到 **Act 2 floor 22**（上限 33） |
| Act 2 内被**环境侧截断**（人还活着） | 2 | 都停在 floor 19 map、`hp=37/77` |
| Act 1 boss 战斗**僵持**到步数上限 | 1 | 见下一节 |
| 非法动作合计 | **0** | 76 局全为 0 |
| 双幕全胜 | **0** | — |

进入 Act 2 的三个检查点（sha256 前 16 位，可据此核对盘上文件）：

| 臂 / 检查点 | sha256 | 终局 |
|-------------|--------|------|
| `c_explore-1 / 182830Z / step_000002000016` | `7867df360a4fdf38` | act=2 floor=22 `hp=0/77` loss（真阵亡） |
| `b_terminal / 175708Z / step_000001000008` | `f7ba5c02c270931d` | act=2 floor=19 `hp=37/77` 环境侧 truncated |
| `b_terminal-1 / 182051Z / step_000001000008` | `0af9d1719f96011a` | act=2 floor=19 `hp=37/77` 环境侧 truncated |

**两件事必须说清楚，否则这张表会被读歪：**

1. **"进入第二幕"是真证据，"第二幕走到几层"不是。** 后两个检查点的 act-2 轨迹
   **逐步相同**（`17 map → 18 event → 18 map → 19 combat → 19 relic_reward →
   19 card_reward → 19 relic_reward → 19 map`），终局 HP 也都恰好是 `37/77`——两个
   不同权重的策略不该撞出同一个终局血量。这说明**该演示种子的 Act 2 一侧同样被
   留档 trace 分支钉住**（`RunEngine.cs:1957` 等一系列 `StringSeed == "7MS1YN8NWB"`
   判定），跑出留档范围就由**环境**发出 truncated。
   所以 Act 2 的 19/22 层**不能当作"策略在第二幕的能力"引用**；
   其中只有那个 `hp=0` 的 loss 是真实战死。
2. **双幕全胜今晚拿不到，是算得出来的**：脚本化 Act 1 通过率 `3/76 ≈ 0.04`，
   第二幕通过率取本文实测的 `65/4986 ≈ 0.0130`，乘积 `≈ 5×10⁻⁴`，即**期望命中 1 次需要
   约 2000 个检查点**；用 76 个检查点去撞，期望命中 0.04 次。
   因此这不是"再跑一夜"能翻的牌，而是要么策略本身变强，要么走真机三幕。

复现：

```text
../third_party/slay-the-spire-2-emulator-main/.venv/Scripts/python.exe \
  scripts/probe_chained_act_flow.py --max-steps 60000 \
  --checkpoint runtime/fanout/c_explore-1/v2curriculum-20260918T182830Z/act1/checkpoints/step_000002000016.zip \
  --out runtime/chained_demo_seed_hits.json
```

## boss 战僵持：floor 17 还有第三种结局（不是死、也不是赢）

`b_terminal / 175708Z / step_000002000016`（sha `eff8c5c51e9b897d`）在该种子上走到
floor 17（boss 节点）后 **60000 步没有结束**：每一步都通过动作掩码（`illegal=0`），
但既不赢也不死。逐项排查：

- **不是短循环**：把 combat 观测块（前 164 个整数）逐步哈希，得到
  **59930 个互不相同的 combat 状态**，没有周期。所以"检测到重复状态就跳出"的护栏救不了它。
- **不是缓慢流血**：每 2000 步采样一次，`player_hp` 全程钉在 **6/77**，
  窗口累计奖励从第 6000 步起稳定在 `-278.46`（只剩逐步惩罚），
  非零战斗整数在 20–22 之间来回——双方都杀不动对方，是**僵局**。

这条推翻本文上面一处表述：原来写"死在 boss 与打赢 boss 的局终局层数都是 17，
只能靠 `won` 区分"——实际上 floor 17 有**三种**结局：赢、死、**僵持**
（`terminated=False` 且 `truncated=True`）。规模用盘上文件界定：战役
`runtime/**` 里 `stage=act1`、`scope=simulator_act1` 的 **106 份指标、29000 局**中
`truncated` 共 **50 局（0.17%）**，且这 50 局所在的每一份文件
`max_final_floor` 都是 **17**——即这类截断**全部发生在 boss 层**。
它**不是某个臂的毛病**：`b_terminal` 系、`c_explore` 系、`a_base`、`fan_cont` 续跑臂
里都有（各臂 0–9 局，最高 `fan_cont/b_terminal-0` 的 9/1900 ≈ 0.47%），
所以这是一种与臂无关的**结局类别**，而不是某次训练的偶发。
它们已被分类为截断（不是"未分类死局"），但实质上是一类**打不完的 boss 战**，
应按死局对待。

**"那就在评估时采样"这条便宜的路已经试掉了，结论是不行。** 用同一个检查点
（`eff8c5c51e9b897d`）在同一个种子上采 **8 次**（`--sampled --sample-seed 1000`，
`torch.manual_seed(1000+r)` 逐个可复现，`runtime/chained_stalemate_sampled.json`）：

| 采样重复 | 0 | 1 | 2 | 3 | 4 | 5 | 6 | 7 |
|----------|---|---|---|---|---|---|---|---|
| 终局层数 | 8 | 6 | 8 | 8 | 8 | 4 | 8 | 5 |
| 步数 | 96 | 83 | 102 | 99 | 95 | 73 | 111 | 77 |

**8 次全部在第 4–8 层阵亡（`hp=0`），一次都没走到第 17 层**，也就根本没有复现僵持。
也就是说：僵持不是"评估模式选错了"——argmax 恰恰是**走得更深**的那个（能到 boss 还剩
6 血），采样会让策略**更早死**。因此这条僵局要靠训练侧解决（boss 层的奖励/课程/采样），
不是靠改评估口径。顺带一个可核对的细节：8 次里 `max_hp` 有 77 也有 80，说明这些采样局
在第 5 层之前就岔开了留档 trace 的分支。

## 同一分区上按幕拆分的各臂排名（step_4M，Act-1-only）

`scripts/split_win_rate_by_generated_act.py --split promotion --act 1 --episodes 1000`
（`runtime/act1_ranking.log`、`runtime/act1_ranking_explore.log`）。六个臂共用同一个
`act1.promotion` 分区（`start=130010000 count=10000`，已逐臂与该 run 的 `plan.json`
对过），其中前 1000 个种子里 504 个生成 Act 1，所以分母是 504：

| 臂（step_000004000032） | Act 1 胜/局 | mean_floor | 非法 | 未分类 |
|--------------------------|-------------|------------|------|--------|
| `b_terminal-0` | 0/504 | 7.99 | 0 | 0 |
| `b_terminal-1` | 0/504 | 7.72 | 0 | 0 |
| `b_terminal-2` | 0/504 | 7.64 | 0 | 0 |
| `c_explore-0` | 0/504 | 7.80 | 0 | 0 |
| `c_explore-1` | 0/504 | 7.32 | 0 | 0 |
| `c_explore-2` | 0/504 | 7.53 | 0 | 0 |

**`0/504` 既不是退步，也不说明臂间有差别。** 按本文的 Act 1 速率 `3/5014 ≈ 0.0006`，
504 局的**期望胜局只有 0.30**——观测到 0 局完全在噪声内（P(0 胜) ≈ e^-0.30 ≈ 0.74）。
六个臂合起来是 **3024 个 Act-1 种子、0 胜**，而按 0.0006 的速率期望值也只有 1.88 胜，
所以"这一批 step_4M 检查点上一个都没赢"与"某个臂上有 3 局赢"并不矛盾——
它们来自不同的检查点/种子切片。
要在这把尺子上分辨两个臂，每臂大约需要 `1/0.0006 ≈ 1700` 个 Act-1 种子
（两幕混排时即约 3400 局）。这是"下一夜该测什么"的具体答案：
不是更多臂，而是**把 Act-1 种子数按这个量级加上去**，否则各臂排名永远读不出差别。

**同一批种子上的采样对照**（`--sampled --sample-seed 1234`，
`runtime/act1_sampled.log`）也没有翻出任何东西：

| 臂 | argmax（上表） | 采样 | argmax trunc | 采样 trunc |
|----|----------------|------|--------------|------------|
| `b_terminal-0` | 0/504，mean_floor 7.99 | 0/504，mean_floor 7.63 | 0.0 | 0.0 |
| `b_terminal-2` | 0/504，mean_floor 7.64 | 0/504，mean_floor 7.49 | 0.00198（1 局） | 0.00198（1 局） |

采样把平均深度**拉低**，胜率仍是 0，而且 `b_terminal-2` 的那 1 局 boss 僵持
**在两种模式下计数相同**（1/504）。这进一步说明僵持是策略/内容的问题，
不是"argmax 评估口径"造成的假象。

## 第三阶段：把"act1 阶段其实不是 Act 1"变成一次受控实验（预注册，写在结果之前）

前提已被两次独立测量钉死：

1. 对该臂自己 `act1.train` 分区（`258000000`，count 80000）普查 80000 个种子，
   **40010 个生成 Act 1（50.01%）**——与本文分幕拆解里 10000 局的 5014/4986 一致；
2. 直接用训练期的环境工厂各起 12 个 worker、每个跑 6 局：
   **对照臂 `a1mix` 得到 31 局 Act 1 / 41 局 Act 2，过滤臂 `a1filt` 得到 72 局全是 Act 1。**
   这一条是运行时实测，不是从配置推出来的。

也就是说，此前 20 个臂的"act1 课程阶段"确实有一半梯度花在 Act 2 上，
而 Act 2 对同一策略的通关率是 Act 1 的约 22 倍。

**实验设计**（两臂只差一个变量，`runtime/act1_ab/`）：

| 项 | 取值（两臂相同） |
|----|------------------|
| 热启动父检查点 | `b_terminal-1 / 182830Z / act1 / step_000004000032.zip` |
| 训练种子窗口 | `act1.train = 258000000 × 80000` |
| 唯一差别 | `a1filt` 加 `train_seeds_file`（该窗口里 40010 个 Act-1 种子）；`a1mix` 不加 |
| timesteps / parallel_envs | 2,000,000 / 12 |
| 评估分区 | `act1.promotion = 130010000 × 10000`（与本文六个臂完全相同） |

`plan.json` 里 `train_seeds.seed_sha256 = fa37aed8ac40d0ea…`、
`source_sha256 = 9dd45bd8e878a558…`、`per_worker = 3335`（2M 步只需约 1424 局/worker，
不会耗尽），过滤臂的清单为 40010 个种子，对照臂该字段为 `null`。
两臂都用 `--only-stage act1` 直接起，**没走 `run_curriculum_fanout.py`**：
该工具的职责是给每个 job 互不重叠的训练区间，而本实验要的正是共用同一个窗口，
硬套会被自己的子集守卫拒绝（这条交互已写进 `docs/TRAINING.md`）。

**预注册判定指标（在看任何结果之前写下）**：

- 主要：两臂各自 `step_000002000000` 检查点，在同一批 **504 个 Act-1 promotion 种子**
  上的 `mean_final_floor`；基线是父检查点本身的 **7.72**（本文排名表）。
  判据：`a1filt − a1mix` 的方向与大小；两个分布都是 504 局，
  因此只承认**明显**的差（|Δ| ≳ 0.5 层）为信号，更小就记为"读不出"。
- 次要（不作判据）：Act-1 胜局数——按 0.0006 的速率 504 局的期望只有 0.30，
  今晚的任何胜负差都不具判定力；`truncation_rate` 与 `illegal_actions` 只作守卫，
  要求过滤臂不劣于对照（僵持类不应被过滤训练放大）。
- 若主要指标读不出差别：结论就是"按幕过滤在本量级上不改变第一幕深度"，
  而不改口去用别的指标。

结果落在 `runtime/act1_ab/{a1filt,a1mix}/v2curriculum-20260919T003139Z/`，
两臂 2M 步预计 01:15 前后完成（实测约 880 fps/臂）。

### 先做一次"空检查"：同一臂 1M→4M 的配对差（也用来校准判据）

在等两臂检查点之前，用**同一条臂的两个检查点**跑了一遍配对分析
（`scripts/compare_act1_seed_filter_arms.py`，`runtime/nullcheck_paired.json`）——
这一步既是脚本自检，也是对"系列是平的"这条结论的一次独立检验：

| 检查点 | mean_final_floor（同一批 101 个 Act-1 种子） | 胜 | 截断 | 非法 | 未分类 |
|--------|---------------------------------------------|----|------|------|--------|
| `step_000001000008` | 8.277 | 0/101 | 0.0 | 0 | 0 |
| `step_000004000032` | 8.307 | 0/101 | 0.0 | 0 | 0 |

**Δ = +0.030 层，95% 自助法区间 [-0.653, +0.723]，p ≈ 0.95 → "读不出"。**
也就是说 3M 步的额外训练在第一幕深度上造成的变化，与零在统计上不可区分——
这与本文"同一分区上的横向对照是平的"一致，但这次是**配对**检验（同一批种子、
同一张地图），比对两个独立比率更有鉴别力。

顺带把预注册判据校准了一次：n=101 时区间半宽 ≈ 0.69，按 √(101/504)≈0.448 缩放到
A/B 用的 n≈504，半宽 ≈ **0.31 层**。一个真为 0.5 层的效应会得到约 [0.19, 0.81] 的区间，
既能越过 0.5 的幅度门、区间也不含 0——**所以 0.5 层这个阈值在 504 个种子上是探测得到的，
不是拍脑袋定的**。反过来也说明：如果两臂差落在 ±0.3 附近，那就必须报"读不出"，
不能因为方向好看就说成进展。

### A/B 的判定命令（结果出来之前就固定，不允许事后改）

```
"G:/qoder/third_party/slay-the-spire-2-emulator-main/.venv/Scripts/python.exe" \
  scripts/compare_act1_seed_filter_arms.py \
  --config runtime/act1_ab/a1filt.toml \
  --arm a1mix  runtime/act1_ab/a1mix/v2curriculum-20260919T003139Z/act1/checkpoints/step_000002000000.zip \
  --arm a1filt runtime/act1_ab/a1filt/v2curriculum-20260919T003139Z/act1/checkpoints/step_000002000000.zip \
  --episodes 1000 --out runtime/act1_ab/paired_ab.json
```

（左臂为基线，Δ = `a1filt − a1mix`；`a1mix` 在前，正的 Δ 就代表过滤有增益。）
两臂各自的 `plan.json` 已经带 `warm_start` 与 `train_seeds` 两条自证，
所以"同一父检查点 + 同一窗口 + 只差一个过滤器"这件事是产物级的，不是叙述。

### 这个 A/B 无论如何都不能完成目标（写在前头，防明早过度解读）

即便 `a1filt` 在 504 个 Act-1 种子上把 `mean_final_floor` 抬高 0.5 层以上，
它改变的也只是"混合训练是不是 Act 1 薄弱的原因"这个**诊断**问题：

- 它不会产出一次 Act 1 终局胜利——504 局在该速率下的期望胜局是 0.30；
- 它更不会产出 Act 1–3 全流程胜利——模拟器没有第三幕（见文首范围天花板）；
- 因此**两臂的任何一种结果都不构成目标达成**，只构成"下一步该往哪投"的证据：
  有增益 → 下一棒把阶梯的 act1 级正式改为按幕过滤并加深；
  无增益 → Act 1 的瓶颈就要归到 5–8 层消耗与 boss 僵持这两条上，
  而不是训练人群里混了第二幕。

## "哈希链证据"现在到底覆盖到什么程度（一条命令可查）

新增 `scripts/verify_run_artifact_hashes.py`：扫 `runs/` 与 `runtime/` 下所有 JSON 产物，
把里面写过的 `checkpoint_sha256` / `source_sha256` / `initialized_from_sha256` /
`resumed_from_sha256` 对着盘上文件重算一遍。2026-09-19 00:55 实测（`runtime/hash_verify_all.log`）：

```
scanned 605 json artifacts under runs, runtime
  checks       379
  verified     369
  unresolvable 10
  seed digests: {'unverifiable': 275, 'none': 325, 'ok:json-list': 5}
```

要说满三点，否则这段会被读成"全都验过了"：

1. **0 个哈希对不上**，369/379 一致。
2. 那 10 个 unresolvable **不是丢失或篡改**，而是全部指向
   `G:\ds harness\sts2-a10-advisor\…`——那是历史工作副本的绝对路径，
   属于"冻结的溯源记录"，按既定口径**不重写**（见 `docs/STATUS.md` 环境审计一节）。
   工具刻意把"文件不在"和"哈希不符"分成两类，就是因为这两个结论完全不同。
3. **`seed_sha256` 在库里有两套编码**而键名相同：规范做法
   `training/seeds.seed_digest`（对 `"1,2,3"` 求摘要），以及
   `scripts/evaluate_teacher_strength.py` 的 `_seed_digest`（对 `json.dumps(seeds)` 求摘要）。
   那 5 份 `runs/teacher_v3/strength-*.json` 用的是后者。
   校验器现在两种都认，**历史产物不改写**。
   另有 275 处只记了摘要、没记种子本身（尤其按幕筛选后的**非连续**子集，
   无法从 `start/count` 重建），这部分只能靠重跑普查复现，**不算已验证**。

所以本文说"哈希链"时，成立的是：**每一个被记录的、盘上仍在的文件摘要都对得上**；
不成立的是"每个种子集都能独立重建"。

### 保留分割（final）没有被用过——这条是查出来的，不是假设的

第二晚所有工作（战役复核、双幕链扫描、采样对照、A/B）写出的评估产物共
**154 份**：`split = promotion` 83 份、`split = checkpoint` 71 份、
**`split = final` 0 份**。脚本声明四个分区是设计如此（`plan.json` 里必然出现
`act1.final`），所以"提到"不等于"用过"——查的是**实际评估记录**的 `split` 字段
（`runtime`/`runs` 下 2026-09-18 16:00 UTC 之后写入的 JSON）。

含义：本文所有数字都来自 promotion / checkpoint 两个可见评估分区，
`130020000` 起的那 10000 个种子仍然是一个干净的 holdout，留给真机验收之前的最后一次
防过拟合检查。`train_seeds_file` 只作用于训练种子，不会、也不应该去碰评估分区。

## "能不能给模拟器补一个第三幕"——查过了，答案是不行（不是麻烦，是没有原料）

文首我说不去给模拟器造第三幕。这次把"到底有没有现成原料可以接线"查清了：

- **战斗工厂只有一个遭遇枚举**：`Core/CombatFactory.cs:27` 的
  `ActOneEncounter`，共 **87 个成员**，两幕都用它（名字是历史遗留，
  `Underdocks*Encounters` 也从这个枚举里取值）。整个文件里
  **没有 `Act3` / `ActThree` / `ThirdAct` 任何字样**。
- **每一场战斗是手写的**：`ActOneEncounter.Cultists => …`、
  `ActOneEncounter.Vantom => [CreateVantom(rng)]` 这种一一对应的 switch，
  所以"第三幕的怪"不是打开某个开关就能出现的——它需要新写敌人行为。
- **`RunConstants.cs:35-36` 也只有两个 act 常量**，没有第三幕的地图规格、
  没有第三幕的遭遇池、也没有第三幕的奖励表。

结论：第三幕在模拟器里**每一层都缺**（常量、池、战斗内容、地图），
不是"有数据没接线"。因此"给模拟器补一幕再拿它当验收证据"这条路，
本质上是拿我编的内容冒充游戏，**不能作为目标达成**——这一点现在有正面证据了。

### 顺带一个会咬人的坑：两套敌人 id 不是一回事

审计"哪些敌人真的可达"时，**不要**把 `Generated/Enemies.g.cs` 的 106 条
`EnemyDef(Id: n)` 与 `RunConstants.*Encounters` 引用的 id 直接比对：
运行期遭遇 id 走的是 `ActOneEncounter` 枚举值（`CombatFactory.cs`），
那是**另一个 id 空间**。我第一次这么比，得出"69 个敌人从未被任何幕使用"
以及"池里引用了不存在的 id 0"两条——都是跨 id 空间造成的假发现，
实际含义是枚举值与生成表编号并不重合。留在这里，避免下一棒再照着它开 bug。

## 第四个具名 Act 1 胜利（来自另一个评估分区，非重复计数）

把父检查点 `b_terminal-1 / 182830Z / step_000004000032`
（`checkpoint_sha256 F514FFA3FFBE62C0…`）在 **`act1/checkpoint` 分区的全部 10000 个种子**
上跑了一遍并按生成幕拆分（`runtime/act1_on_checkpointsplit.json`，01:17）：

| 幕 | 种子数 | 胜 | win_rate | mean_floor | max | trunc | 非法 | 未分类 |
|----|--------|----|----------|------------|-----|-------|------|--------|
| Act 1 (overgrowth) | 5006 | **1** | 0.000200 | 7.8214 | 17 | 0.00060 | 0 | 0 |
| Act 2 (underdocks) | 4994 | 未评估 | — | — | — | — | — | — |

（这一跑用了 `--act 1`，所以只评估 overgrowth 那一半；4994 这个数字来自同一次
种子普查，**不是**测出来的 Act 2 成绩。普查整体是 5006 overgrowth / 4994 underdocks。）

Act 1 的那个胜局是 **seed 130008177**，并且已经单独逐个复现过
（`runtime/wide_win1_verify.log`：`generated acts: {'overgrowth': 1}`，
`1/1`、`floor 17`、`illegal 0`、`unc 0`）。

**为什么这条有意义**：此前三个具名胜局全部来自 `promotion` 分区，
这是第一个来自**另一个评估分区**（`checkpoint`）的 Act 1 胜利——
不是同一批种子上的重复计数，而是新增的独立支持。

**同时要修正 headline 的速率**（各分区数字各说各的，不交叉除）：

| 人群 | Act-1 局数 | 胜 | 速率 |
|------|-----------|----|------|
| `b_terminal-1 step_2M` × promotion | 5014 | 3 | 0.00060 |
| `b_terminal-1 step_4M` × checkpoint | 5006 | 1 | 0.00020 |
| 合起来（10020 个 Act-1 局） | 10020 | **4** | **0.00040** |

也就是说，**0.06% 是偏乐观的**：把它当成 Act 1 通关节律时应当用 **4/10020 ≈ 0.04%**。
顺带第三次独立观察到 boss 僵持类（这批里 3/5006 = 0.06% 截断，全部 `max_floor=17`）。
