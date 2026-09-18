# 夜间 Act 1 战役评估（2026-09-19）

## 结论

V2 契约栈**第一次记录到 Act 1 终局胜利**，且证据可复核（检查点与种子分区均可重算）：

| 臂 | 分区 | 局数 | win_rate | max_final_floor | 非法动作 | 未分类死局 | 检查点复算 | 独立复现 |
|----|------|------|----------|-----------------|----------|------------|------------|----------|
| `b_terminal-0`（fan-out） | promotion（未见种子） | 500 | 0.004（2 胜） | 17 | 0 | 0 | 一致 | **已复现** |
| `b_terminal-1`（fan-out） | promotion（未见种子） | 500 | 0.012（6 胜） | 17 | 0 | 0 | 一致 | **已复现** |
| `c_explore-0`（fan-out） | checkpoint @1M | 100 | 0.010（1 胜） | 17 | 0 | 0 | 一致 | **已复现** |
| `b_terminal-0`（fan-out） | checkpoint @1M | 100 | 0.020（2 胜） | 17 | 0 | 0 | 一致 | **已复现** |
| `a_base`（手工臂） | checkpoint @1M | 100 | 0.010（1 胜） | 17 | 0 | 0 | 一致 | 待复现 |

战役总览（`python scripts/report_act1_campaign.py --root runtime/act1_overnight
--root runtime/fanout --root runtime/act1_overnight/fan_cont`，2026-09-19 05:58 本地
时间，**战役已结束**：无残留训练进程）：**20 个臂、110 份评估文件、61 份记录到非零胜局**
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
   唯一例外是硬编码种子 `7MS1YN8NWB` 的 Act 1→Act 2 串联分支，本轮专门去试过它，
   结论是**这条路对我们的策略不可用**：
   - V2 栈能吃字符串种子（`reset(seed="7MS1YN8NWB")` 正常，act/floor 读数正确），
     随机合法动作在 floor 3 就死，所以随机策略也摸不到串联点；
   - 四个检查点（b_terminal-1 的 2M/4M/final、b_terminal-0 的 final）在该种子上
     全部死于 floor 6–8，**没有一个能到第 17 层**，因此那个串联分支根本没被触发过；
     这一路的进入条件是先打赢 Act 1 boss，而实测 Act 1 通关率只有 3/5014。
   也就是说"两幕串联"在模拟器里是**存在但不可达**，三幕则是**不存在**。
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

**已复现四次，跨三个不同臂与两种不同分区（未见 promotion 500 局 ×2、checkpoint
100 局 ×2）。** 其中 `b_terminal-1` 的 6/500 是这批里最强的一个未见分区证据。
`scripts/reevaluate_checkpoint.py` 独立重跑了
`b_terminal-0` 的 500 局 promotion 评估与 `c_explore-0` 的 100 局 checkpoint 评估，
逐项一致。

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

结论限定在：**存在可复核、可复现的 V2 Act 1 终局胜利（最强一条 6/500，四次独立复现）**。
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
   第 16 层从不出现在终局层数里也说得通——在那一层死掉的局本来就极少。

   所以瓶颈的主次是清楚的：挡住胜率的是**前中段的消耗**（66% 在 5–8 层出局），
   boss 是第二道闸——真正站到 boss 面前本就只有 4%。
   一点保留：0/8 与"boss 胜率 1%–10%"并不矛盾，样本太小，不能据此说 boss 已被排除。
3. 因此"同等配置再多跑几夜"的预期收益被压低了：曲线是平的，而瓶颈在前中段消耗，
   不在夜时长。
4. 这条负结论只在模拟器 Act 1 口径内成立（`scope: simulator_act1`）。

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
  时长。可选的具体方向（都还没做，等操作员定）：把 warm-start 阶梯的某一级设在
  floor 6→10 之间继续加深，或针对 5–8 层遭遇的奖励/采样做一轮消融。
- 真机侧的代码拦路石已经清掉：grammar v2 合入主干并进入验收套件
  （47/47；15 会话真机回放零漂移），逐批次模组自证也已落地
  （`combat_solver/modpin.py` + `scripts/supervise_solver_batch.py`）。
- 剩下的拦路石不是代码：04:37 与 05:31 两次实测 `127.0.0.1:15526/health` 均拒连、
  无游戏进程。supervisor 设计上**永不启动游戏**（只 GET 探测然后
  `game_wait_timeout`），这个设计此刻是有意的——profile 存档树在 00:03–00:04
  被写过，冷启动新运行可能顶掉在跑的一局。三幕需要操作员本人开机后再跑：
  `python scripts/supervise_solver_batch.py --mode observational --allow-actions
  --max-battles 50`。
