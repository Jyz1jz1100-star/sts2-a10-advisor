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

战役总览（`python scripts/report_act1_campaign.py --root runtime/act1_overnight --root runtime/fanout`，
2026-09-19 04:36 本地时间）：**20 个臂、59 份评估文件、28 份记录到非零胜局**
——其中 **15 份来自 500 局的未见 promotion 分区**、13 份来自 100 局 checkpoint 分区。
这 28 份**全部**满足 `illegal_actions=0`、`unclassified_dead_ends=0`、
`checkpoint_verified=True`、`max_final_floor=17`；最高 `win_rate = 0.02`（100 局臂），
未见 500 局分区上最高 `win_rate = 0.012`。

也就是说"能不能赢一次"已经不再依赖单点观察：**多臂、多分区、多时间点上重复出现**。
但仍未越过任何晋升门槛（act1 门槛是 35% 胜率 / Wilson 下界 31%），
所以这批证据的结论是"可达"，不是"可靠"。

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

1. **`scope` 是 `simulator_act1`，不是真机 A10 验收**。两场的统计含义是 1/100 与
   2/500，Wilson 下界千分之二级别——它证明"能赢一次"，不证明"能赢"。
2. **模拟器只有第一幕**：`RunConstants.MapBossRow = 16`、单 boss 节点、遭遇枚举
   只有 `ActOneEncounter`、README 自称 "Seeded Act 1 selection"。因此
   **Act 1-3（含最终幕两个 boss）在模拟器内不可达成**，本文任何数字都不能被当作
   三幕通关的证据。三幕只能在真机上完成，走 `docs/ACCEPTANCE.md` 的验收链路。
   真机侧**已核实的最远距离是 Act 2 floor 30**（seed `1600000001`，`game_over` +
   `hp=0`，带可归属 trace 与 assessor 的 `terminal_zero_hp`，见
   `docs/LIVE_PROGRESS_2026-09-07.md`）。也就是说"能进第二幕"有终局证据，
   "通关三幕"至今一例都没有——两者都不在本文的口径里。
3. 目标要求的"逐阶段 warm-start 阶梯"在这里体现为：所有臂都从 promoted floor6
   检查点 `--initial-checkpoint` 热启动，经 `scripts/run_curriculum_fanout.py`
   以互不重叠的种子分区并发跑（工具会拒绝落入 teacher 保留区
   `>= 1_410_000_000` 的种子，也拒绝分区重叠）。

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

结论限定在：**存在一次可复核、可复现的 V2 Act 1 终局胜利（2/500）**。
它不等于策略能稳定赢，更不等于三幕通关（见范围声明）。

## 同一分区上的横向对照（这条比上面的记录数更重要）

上面 35 条非零记录各自跑在**不同的种子分区**上，因此"某臂 2%、另一臂 0.4%"并不是
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
   是**可达性稳定**，不是**策略在变强**；把 44 条记录读成进步，是这批证据不支持的。
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

## 仍在进行

- 3 个续训臂从已验证的 `final.zip` 热启动，换新种子段（400M/406M/412M），
  目的不是刷胜率，而是检验"未见分区上是否仍能赢"。
- 战役仍在写入：05:31 复核为 20 臂 / 89 份评估 / **44 份非零胜局**（25 份在 500 局
  未见分区），仍然 0 非法、0 未分类、0 检查点复算不一致。上面的表与结论按
  04:36 的 28 份快照撰写，只增不减，不改口径。
- 真机侧的代码拦路石已经清掉：grammar v2 合入主干并进入验收套件
  （47/47；15 会话真机回放零漂移），逐批次模组自证也已落地
  （`combat_solver/modpin.py` + `scripts/supervise_solver_batch.py`）。
- 剩下的拦路石不是代码：04:37 与 05:31 两次实测 `127.0.0.1:15526/health` 均拒连、
  无游戏进程。supervisor 设计上**永不启动游戏**（只 GET 探测然后
  `game_wait_timeout`），这个设计此刻是有意的——profile 存档树在 00:03–00:04
  被写过，冷启动新运行可能顶掉在跑的一局。三幕需要操作员本人开机后再跑：
  `python scripts/supervise_solver_batch.py --mode observational --allow-actions
  --max-battles 50`。
