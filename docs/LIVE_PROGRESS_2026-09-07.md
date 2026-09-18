# 真机局外自动化进度审计（2026-09-07）

本审计只读检查了 2026-09-05 至 2026-09-06 的
`runs/solver_supervisor`、`runs/combat_solver_compare`、autoplay trace、
Combat Solver 日志、seed ledger、运行快照和版本锁。没有启动、重启或停止
游戏，没有发送 POST，也没有修改现有运行工件、业务源码或版本锁。为复核
完整 trace，`scripts/assess_full_run.py` 的输出只写入新的
`runs/read_only_audit_20260907/` 目录。

## 可确认的真实局结果

以下是按 `run_id` 去重后、具有终局状态的三个运行。死亡结论同时依赖
`state_type=game_over`、`game_over.message=Run ended.`、可返回
`main_menu` 的终局选项以及该终局状态中的 `player.hp=0`；没有把某个普通
战斗状态的 `hp=0` 单独当作死亡证据。

| run_id | seed | 终局证据 | 最高进度 | 结论 | 主要证据 |
| --- | --- | --- | --- | --- | --- |
| `modded:profile1:1788631649` | `1600000000` | `game_over`, `hp=0` | Act 1 floor 9 | 死亡 | `ssb-20260905T193903Z-4e8b9eb1` trace，assessor 的 `terminal_zero_hp` |
| `modded:profile1:1788637161` | `1600000001` | `game_over`, `hp=0` | Act 2 floor 30 | 死亡 | `ssb-20260906T050556Z-c3bb72a1` trace，assessor 的 `terminal_zero_hp` |
| `modded:profile1:1788672433` | `1600000002` | `game_over`, `hp=0` | Act 1 floor 14 | 死亡 | `ssb-20260906T062712Z-b99d3cbe` trace，assessor 的 `terminal_zero_hp` |

第一个 trace 还包含一段开始于 Act 1 floor 8、没有新鲜 run identity 的
未归因状态流；它被 assessor 单独列为
`UNATTRIBUTED:autoplay_trace.jsonl:3`，不能并入 seed 0 的合法运行，也不能
算作第四局。

seed `1600000003` 的 `current_run` 快照显示为进行中，曾经记录到 Act 2
floor 30、`hp=59` 的状态，但没有 `game_over`、胜利或死亡终局。其状态和
compendium 文件的更新时间早于最后一次进程退出，不能据此称为当前运行健康，
也不能把它计入胜率。18:25 的 0.31.0 Combat Solver 日志虽有搜索和部署记录，
但没有与该 seed 的 `run_id`、PID、bridge trace 和终局状态形成可验证连接，
所以仍是“有进展、无终局证据”。

## 最新 supervisor 和桥状态

最新批次是 `ssb-20260906T102557Z-183d0b05`：

- supervisor `status=failed`、`result_code=5`、`stop_reason=autoplay_exited`；
- autoplay `exit_code=1`，日志反复尝试
  `seed[3]='1600000003'`，随后在
  `bridge/trace_controller.py:689` 抛出
  `BridgeProtocolError: Singleplayer is not currently actionable`；
- comparison 和 full-auto keeper 是被 supervisor 以
  `child_unexpected_exit` 联动停止的子进程，退出码为 `3221225786`；
- 对应的 comparison sibling summary 仍写着 `status=running`，这只是子进程
  被提前停止后没有收敛的残留状态，不能当作正在运行或有新样本；
- 随后的只读检查中 `GET /`、`GET /api/v1/singleplayer` 和
  `GET /api/v1/compendium` 均为 connection refused。当前没有 STS2 游戏进程；
  `godot.log` 的最后一次 0.31.0 启动只包含初始化和 18:28 左右的退出。

9 月 6 日后半段的 071455、071815、072140、072624 和 102557 批次均以
autoplay 退出收尾。它们的 comparison 目录多保留 `running` manifest，
因此不能以目录存在或文件更新时间判断批次仍在工作。

## assessor 的实际口径

以下四份只读报告已经生成，分别对应已有 trace：

- [seed 0 trace assessment](../runs/read_only_audit_20260907/assess-ssb-20260905T193903Z-4e8b9eb1.json)
- [seed 1 trace assessment](../runs/read_only_audit_20260907/assess-ssb-20260906T050556Z-c3bb72a1.json)
- [seed 2 trace assessment](../runs/read_only_audit_20260907/assess-ssb-20260906T062712Z-b99d3cbe.json)
- [latest failed batch assessment](../runs/read_only_audit_20260907/assess-ssb-20260906T102557Z-183d0b05.json)

它们全部 `accepted=false`、未达到 20 局 pilot 门槛，也未达到 500 局 formal
门槛。主要原因如下：

- seed 0 报告识别出 3 个 run record，但只有 2 个 identity 有效，1 个死亡、
  2 个未知或未完成；原 trace 有 seed 缺失和未归因 floor 8 段；
- seed 1 报告为 1 个有效 identity 的 Act 2 死亡和 1 个未知运行；由于游戏/桥
  阻塞后的重复尝试，assessor 记录 2,753 个 illegal/retry action 以及 1 个
  缺失结果；原始 autoplay 日志对应的是无合法候选、stale decision、超时和
  bridge 不可用的反复重试；
- seed 2 报告为 1 个有效 identity 的 Act 1 死亡和 1 个未知运行，并记录
  2,626 个 illegal/retry action；
- 最新批次没有任何 run record，只有 menu 状态和失败的 start 尝试。

三个含战斗的 trace 都以 `combat_solver_full_auto` 作为声明的执行 owner，
但 assessor 没有取得与这些 trace 同批次、同 run identity 绑定的 Combat Solver
deploy log，因此 `combat_deploy_log_missing` 仍是阻塞项。决策覆盖率也很低，
不能把“HTTP 局外动作执行过”解释为“整局执行证据完整”。

## Combat Solver 版本和日志分层

当前磁盘 Workshop 工件是 CombatSolver 0.31.0：

- manifest：`G:\SteamLibrary\steamapps\workshop\content\2868840\3790899961\CombatSolver.json`；
- DLL SHA-256：`E97879185E853D678D632A16695D06D3A0AF60E2ABD4582C75FB1C02D36512E`；
- 未提交的 `config/combat_solver.lock.json` 也记录 0.31.0。这是用户现有修改，
  本审计没有覆盖或更新它。

已保存的游戏日志按实际启动内容区分为：

| 日志 | 观察到的版本 | 内容范围 |
| --- | --- | --- |
| `godot2026-09-06T15.13.35.log` | 0.29.1 | 14 个 parser snapshot、40 个 deploy record，无 typed failure；属于历史会话 |
| `godot2026-09-06T17.47.12.log` | 0.29.1 | 只有初始化，无战斗块 |
| `godot2026-09-06T17.47.59.log` | 0.30.0 | 只有初始化，无战斗块 |
| `godot2026-09-06T18.25.02.log` | 0.31.0 | 39 个 `SEARCH_REQUEST`、191 个 `RESULT`、189 个 `DEPLOY_END`，流式 parser 观察到 72 个 snapshot、189 个 deploy、1 个 `NO_ROUTE` |
| `godot.log` | 0.31.0 | 只有初始化和退出，没有可绑定的战斗块 |

0.31.0 日志中的事件数量与当前 solver lock note 中记录的
“161 events / 45 snapshots / 115 deploys”不是同一套可直接互换的计数；当前
没有把日志选择、去重规则、PID、run identity 和 trace 绑定成一份完整证据，
因此不能仅凭 parser 重放数量宣称 0.31.0 已通过协议验收。历史 0.29.1
trace 也不能自动升级为 0.31.0 证据。

## 联合 solver 的协议缺口

Combat Solver 对比锁的 `evaluation_environment.allowed_mod_ids` 允许
`STS2_MCP`、`STS2-RitsuLib`、`CombatSolver` 和 `RegentFX`；而 A10 acceptance
的 `config/live_version.lock.json` 没有 solver 身份，项目验收说明仍把正式
acceptance whitelist 设为只允许 `STS2_MCP`。这两个用途目前没有一个明确的
计分转换协议：在四模组环境中得到的联合 solver 结果不能直接塞进只允许
`STS2_MCP` 的正式胜率，也不能由本审计自行放宽 whitelist。

要证明“局外 bridge + 局内 Combat Solver”整局闭环，至少还需在一次固定版本、
固定模组清单的运行中同时保存：

1. 启动时的 game/bridge/mod hash 和唯一 execution owner；
2. 同一 `run_id` 的 authoritative seed、Ironclad、A10、standard identity；
3. 局外 trace 与 Combat Solver 0.31.0 deploy/search log 的 PID/时间窗口关联；
4. 每次局外选择和每次战斗部署的完整结果；
5. `game_over` 胜利或死亡终局，并让 assessor 对该批次收敛为非残留状态。

在这些条件完成前，当前最准确的状态是：三局已被终局证据确认死亡，seed3
曾经推进但没有终局证据；全自动 supervisor 已因 bridge/游戏不可行动而失败；
局外决策器和 Combat Solver 的分层方向仍可行，但联合系统尚未形成可验收的
整局胜率。

---

## 后记（同日第二轮，修复轮）

上文审计之后进行了一轮只改代码/测试/文档的修复：本文件记录的
`ssb-20260906T102557Z-183d0b05` 失败模式（autoplay 对冻结菜单 61 次重复
尝试后崩溃、comparison 残留 `running`、`comparison_result: null`）已按
[`BATCH_LIFECYCLE_FIX_2026-09-07.md`](BATCH_LIFECYCLE_FIX_2026-09-07.md)
收敛为分类停止+终态复核；历史 godot 日志的归属结论以
[`runs/evidence_attribution_20260907/attribution.json`](../runs/evidence_attribution_20260907/attribution.json)
为准（5 个日志全部"无法归属"，含原因）。本文件其余审计结论不变。
