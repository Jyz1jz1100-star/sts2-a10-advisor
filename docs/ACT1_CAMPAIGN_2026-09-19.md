# 夜间 Act 1 战役评估（2026-09-19）

## 结论

V2 契约栈**第一次记录到 Act 1 终局胜利**，且证据可复核（检查点与种子分区均可重算）：

| 臂 | 分区 | 局数 | win_rate | max_final_floor | 非法动作 | 未分类死局 | 检查点复算 |
|----|------|------|----------|-----------------|----------|------------|------------|
| `b_terminal-0`（fan-out） | promotion（未见种子） | 500 | 0.004（2 胜） | 17 | 0 | 0 | 一致 |
| `a_base`（手工臂） | checkpoint | 100 | 0.010（1 胜） | 17 | 0 | 0 | 一致 |

历史对照：本仓库此前 **22 份 V2 指标文件里 win_rate 全为 0**，所以这是第一次非零。
`max_final_floor = 17` 表示越过第 16 层（`RunConstants.MapBossRow = 16`）到达幕终局，
不是中途截断；两场的 `illegal_actions = 0`、`unclassified_dead_ends = 0`、
`defect_truncation_rate = 0`。

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
3. 目标要求的"逐阶段 warm-start 阶梯"在这里体现为：所有臂都从 promoted floor6
   检查点 `--initial-checkpoint` 热启动，经 `scripts/run_curriculum_fanout.py`
   以互不重叠的种子分区并发跑（工具会拒绝落入 teacher 保留区
   `>= 1_410_000_000` 的种子，也拒绝分区重叠）。

## 复现状态

- 检查点哈希：已复算一致（两例）。
- 独立重跑评估：`scripts/reevaluate_checkpoint.py` 对 `b_terminal-0` 的 500 局
  promotion 分区重跑中；结论以脚本输出的 `VERDICT: reproduced / NOT reproduced`
  为准，**未复现则本节的胜利不成立**，届时须回退为"记录存在但未复现"。

## 仍在进行

- 3 个续训臂从已验证的 `final.zip` 热启动，换新种子段（400M/406M/412M），
  目的不是刷胜率，而是检验"未见分区上是否仍能赢"。
- 真机侧两个拦路石仍未完成：grammar v2（WIP 分支 `wip/grammar-v2`，
  47 项测试里 11 失败 + 3 错误）与逐批次模组自证（任务 #6）。在它们完成前，
  真机三幕即使打完也无法判定为验收通过。
