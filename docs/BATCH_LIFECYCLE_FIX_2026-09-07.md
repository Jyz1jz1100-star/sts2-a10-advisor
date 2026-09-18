# 批次生命周期修复与证据归属（2026-09-07）

> **评审修订（同日，第三轮）**：按评审意见修正三处——(1) comparison
> 退出码 0 的 complete/partial/工件无效三个分支现在也执行
> `_finalize_stop()`，正常完成的批次同样落盘
> `combat_solver_logs.at_end` 与 autoplay summary；
> (2) `read_trace_session_end` 改为逐行二进制读取并容错解码，进程被杀
> 留下的截断 UTF-8 尾行会跳过并标记 `trace_tail_corrupt: true`，不再让
> 收尾本身崩溃，原始退出原因保留；
> (3) 归属报告不再把开始/结束清单的哈希并集表述为"批次窗口绑定"：
> 语义改为**清单观察**（`pre_existing` / `created_during_window`），
> 并明确"created_during_window"也只是绑定输入，运行级归属需要日志内
> 内容范围与运行身份关联。相关测试分别覆盖（共 491 项通过）。
>
> **评审修订（同日，第四轮）**：(4) 观察字段进一步去推断化——
> `pre_existing`/`created_during_window` 改为
> `hash_observed_at_start` / `hash_observed_at_end_only`：哈希在开始
> 清单缺席**不推断创建时间**（旧文件追加内容会变哈希；开始清单可能
> 整体缺失或对某文件拒绝哈希），并新增两个回归场景固定该语义；
> (5) 新增真实 Windows 子进程测试：以临时锁夹具（不触碰真实版本锁）
> + 测试本地假桥启动真实 `run_solver_comparison.py` 子进程（不启动
> 游戏），在轮询循环运行中发送 `CTRL_BREAK_EVENT`，断言子进程协作
> 收尾并写出 `status=partial`、`stopped_reason=signal`；
> (6) 归属报告与文档措辞同步。最终全量 395+99=494 项通过。
> （注：报告快照期间游戏日志目录出现新的 `godot2026-09-07T23.06.30.log`
> ——非本轮任何操作所为；报告如实纳入并保持"无法归属"。）

本轮只处理两件事：失败批次的生命周期处理（以
`ssb-20260906T102557Z-183d0b05` 为复现样本），以及整局证据归属的补齐。
没有启动/操作游戏，没有部署模组，没有更新版本锁，没有修改生产路线策略，
没有提交 commit。Combat Solver 仍是战斗动作的唯一执行者；
`config/combat_solver.lock.json` 的既有用户修改保持原样。

## 一、离线故障复现证据（全部来自既有工件，只读）

批次 `ssb-20260906T102557Z-183d0b05`（`runs/solver_supervisor/.../`）：

1. **autoplay 日志**（`logs/autoplay.log`）：61 次完全相同的
   `[autoplay] starting fresh Ironclad A10 run (menu) seed[3]='1600000003'`
   + `action failed (Singleplayer is not currently actionable); retrying`，
   随后 `bridge/trace_controller.py:689` 抛出裸 traceback，exit code 1。
2. **autoplay trace**（`autoplay_trace.jsonl`，247 行）：122 条
   `state` GET 全部返回**逐字节相同**的状态
   `{menu_screen: "main", options: ["continue","abandon_run","multiplayer",
   "compendium","timeline","settings","quit"]}`，decision_id
   （可见状态内容哈希）122 次全部为
   `local-sha256:c07eab…`；122 条 compendium 记录全部
   **无 `current_run`**。全部请求都是 GET，**POST 数为 0**——失败循环里
   从未发送过任何动作，也没有消耗第二个种子。
3. **supervisor.log**：10:25:57 游戏健康探测通过（HTTP 200），10:27:09
   autoplay exit 1 → `child_unexpected_exit` → 向 comparison/keeper 发
   `CTRL_BREAK_EVENT`，两者以 `3221225786`（0xC000013A，
   STATUS_CONTROL_C_EXIT）退出。
4. **残留状态**：comparison sibling 的 `summary.json` 仍是
   `status=running`；supervisor manifest 里 `comparison_result: null`。
5. **seed ledger**（复用 `ssb-20260905T193903Z-4e8b9eb1` 的 ledger）：
   index 3 处于 active reservation，`consumed` 仍为 3 条（index 0–2），
   没有错误消耗。

### 根因（三层，不是单一超时问题）

- **游戏/模组侧状态冻结且自相矛盾**：主菜单处于"有进行中局"变体
  （提供 continue/abandon_run、不提供 singleplayer），而 compendium 证明
  没有可验证的保存局。autoplay 按既有安全规则正确拒绝不可验证的
  continue（真实存档必须 fail closed）、绝不点击 abandon_run（破坏性）、
  又无法从该菜单变体开始新局——这是一个**无合法动作的死角状态**。
- **autoplay 层**：同一 decision_id 连续失败只按次数封顶，超出后裸
  `raise` → traceback exit 1。传输断连（connection refused）与协议错误、
  状态陈旧在异常类型上不可区分；"桥断连"的重试无时间上限。
- **supervisor/comparison 层**：comparison runner 只安装了 SIGTERM 处理；
  Windows 上 supervisor 停进程组用的是 `CTRL_BREAK_EVENT`（SIGBREAK），
  OS 直接终止进程，`finally` 里的最终 summary 没有执行 →
  `status=running` 残留。supervisor 的异常退出路径不复核 comparison
  工件，manifest 停留在 `comparison_result: null`。此外 autoplay 以
  **exit 0** 正常结束（额度/种子耗尽）也会被误报为
  `child_unexpected_exit`。

## 二、修改后的行为

- `bridge/trace_controller.py`：新增 `BridgeConnectionError`
  （`BridgeProtocolError` 的子类），传输层失败（URLError/Timeout/OSError）
  单独分类；HTTP 状态错误仍为 `BridgeProtocolError`。
- `bridge/autoplay.py`：
  - 新增 `AutoplayClassifiedStop` 与退出码 `EXIT_CLASSIFIED_STOP = 3`
    （supervisor 侧镜像为 `AUTOPLAY_CLASSIFIED_STOP_EXIT`）。
  - 同一 decision_id 连续失败 60 次 → `stale_state` 分类停止（带最后
    错误与 decision id）；总失败 600 次 → `repeated_failures`；状态读
    协议失败 → `repeated_state_failures`；每次重试前都在循环头重新
    GET 并验证状态（既有行为保留并明确化）。
  - 桥不可达（`BridgeConnectionError`）按**时间窗**重试（默认 180s，
    覆盖已知的 Combat Solver 深度搜索阻塞 >15s 与 120s 身份读取窗口），
    超时 → `bridge_unavailable` 分类停止；成功读到状态即重置计时。
  - 分类停止会向 trace 追加 `session_end` 事件（含 summary 与原因），
    并以退出码 3 结束——不是崩溃，也不伪装完成。
  - 安全硬停（`SeedAllocationError`/`RunIdentityError`）语义不变：
    继续立即失败，绝不重试、不跳过、不放弃存档。
  - 失败的启动保留 active reservation，同一 reservation 被后续重试/
    恢复复用，不重复消耗（回归测试断言所有启动尝试使用同一种子、
    `consumed`/`next_index` 不变）。
- `scripts/run_solver_comparison.py`：Windows 下同时安装 SIGBREAK
  处理（`install_stop_signal_handlers`/`restore_stop_signal_handlers`），
  被停止时走既有协作停止路径，写出最终
  `status=partial, stopped_reason=signal`，不再残留 `running`。
- `scripts/supervise_solver_batch.py`：
  - autoplay exit 0 → `autoplay_completed`：优雅停掉其余子进程后复核
    comparison 工件，分类 complete → `EXIT_OK`，否则 `EXIT_PARTIAL`。
  - autoplay exit 3 → `autoplay_classified_stop`：读取 trace 的
    `session_end`，把子进程自己的原因（含 `stop_reason`）写入 manifest
    的 `autoplay_summary`，再复核并记录 comparison 工件分类，返回
    `EXIT_CHILD_FAILED`（批次确实未完成，但原因明确、分层一致）。
  - 任何终止路径（comparison 失败、游戏丢失、超时、操作员停止、异常）
    都会执行 `_finalize_stop()`：只读复核 comparison 工件并把分类
    （包括 `running` 残留 → classification `partial` + issues）写入
    manifest。**不改写子进程工件，不把强制停止伪装为完成。**
- 回归测试：`tests/test_autoplay.py::AutoplayClassifiedStopTests`（冻结
  菜单复现、不点击 continue/abandon、种子不消耗、桥断连分类、瞬时断连
  恢复、session_end 事件、协议失败封顶）、
  `tests/test_solver_supervisor.py::FailureConvergenceTests`（exit 0 完成、
  exit 3 原因保留+残留分类、exit 1 崩溃仍记录工件分类）、
  `tests/test_combat_solver_compare.py::StopSignalHandlerTests`
  （SIGBREAK 处理器安装/恢复）。

## 三、证据归属（任务二）

- **既有机制梳理**：run identity 由 trace 的
  `run_identity`/compendium 记录承载；seed→run_id 绑定在
  `seed_allocation.ledger.json` 的 `consumed` 记录；版本锁
  （`live_version.lock.json` + `combat_solver.lock.json`）记录
  game/bridge/mod 哈希；Combat Solver 战斗证据在 comparison 的
  battles/checkpoint；终局证据由 assessor 依
  `game_over`+`hp=0`/胜利终局判定（`scripts/assess_full_run.py`）。
  缺口是：**没有任何批次记录过"哪个 godot*.log 覆盖本批次"**，deploy log
  与 run_id 之间因此永远缺一环。
- **新运行的观察清单（评审修订后的语义）**：supervisor manifest 新增
  `combat_solver_logs` 字段——批次开始与结束各做一次游戏日志目录
  （`godot*.log`）的只读清单（path/size/mtime/SHA-256）。语义是**哈希
  观察**而非绑定，且不从哈希缺席推断创建时间：哈希在 `at_start` 出现
  → `hash_observed_at_start`；哈希不在 `at_start`、只在 `at_end` 出现
  → `hash_observed_at_end_only`（**不是**"窗口内新建"：已存在文件被
  追加内容后哈希会变，开始清单也可能整体缺失或对某文件拒绝哈希——
  两个回归场景均有测试固定）。两者都不归属到具体运行——现有 Combat
  Solver 日志格式不含 run identity，运行级绑定需要日志内的内容范围与
  运行身份关联，当前任何格式都不提供。归属判定由
  `scripts/report_solver_log_attribution.py` 输出，绝不用时间相近推断。
- **历史日志归属报告**：
  `scripts/report_solver_log_attribution.py`（离线、只读、拒绝覆盖已有
  报告）对每个历史 godot 日志给出明确结论。本次生成于
  [`runs/evidence_attribution_20260907/attribution.json`](../runs/evidence_attribution_20260907/attribution.json)：
  5 个日志（0.29.1×2、0.30.0、0.31.0×2）**全部"无法归属"**，原因：
  日志内无 run identity 绑定 + 无任何批次清单观察过该哈希。0.31.0 日志
  里观察到的战斗活动不因时间接近而归属到 seed 1600000003。
- **缺证据=不通过（已验证）**：对失败批次 trace 重跑
  `assess_full_run.py`（输出
  `runs/evidence_attribution_20260907/assess-failed-batch-recheck.json`）
  → `accepted=false, decidable=false`，blockers 含
  `no_run_records` 与 provenance 缺失；声明了 comparison 证据而
  `battles.jsonl` 缺失时 assessor 直接拒绝出报告（exit 1）。没有补造
  任何字段。
- **模组锁边界（未放宽）**：comparison 锁
  `evaluation_environment.allowed_mod_ids = [STS2_MCP, STS2-RitsuLib,
  CombatSolver, RegentFX]`；正式验收
  `config/live_version.lock.json` 保持 `[STS2_MCP]`。solver lock 文件里
  现有 note 已声明 comparison 清单结果永不计入验收协议；本轮未改动
  任何锁，也没有引入计分转换。

## 四、尚需实机验证的事项

1. 冻结菜单/矛盾菜单（有 continue/abandon_run 但 compendium 无
   current_run）在真机上的成因（模组菜单缓存 vs 存档加载失败）仍需
   现场日志确认；离线只能证明 autoplay 侧行为正确收敛。
2. `session_end` trace 事件、退出码 3、SIGBREAK 收尾、
   `combat_solver_logs` 快照都需要一次真实批次验证落盘形态。
3. comparison 被强停后的 `status=partial/stopped_reason=signal` 汇总
   需真机确认。
4. 归属闭环（快照哈希 → deploy log → run_id）需要在下一次固定种子
   批次上端到端走通后，才可能消除 `combat_deploy_log_missing` 阻塞。

## 五、下一轮最小实机验证步骤

1. 启动游戏与桥，确认 `GET /` 健康。
2. 按现有入口启动批次（示例）：
   `python scripts/supervise_solver_batch.py --mode fixed --allow-actions
   --max-battles 200 --seed-ledger
   runs/solver_supervisor/ssb-20260905T193903Z-4e8b9eb1/seed_allocation.ledger.json`。
   复用现有 ledger 时先处理 index 3 的 active reservation：若 compendium
   证明无保存局会自动回滚并重用同一种子；若存在真实保存局则必须走
   可验证的 Continue 路径，不得跳过。
3. 观察 manifest 的 `combat_solver_logs.at_start` 出现且带 SHA-256；
   若批次再次遇到不可行动菜单，应在 ≤72s 内以
   `autoplay_classified_stop` 收场，manifest 带具体 `autoplay_summary`，
   comparison summary 为 `partial` 而非 `running`。
4. 批次结束后对 trace 跑 `scripts/assess_full_run.py`，确认
   `combat_deploy_log_missing` 是否因新快照+deploy log 绑定而可判定。
