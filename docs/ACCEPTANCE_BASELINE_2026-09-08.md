# 验收基线（2026-09-08）

本文档是下一阶段（可中止、可追溯的最小实机试验）的基线快照与执行手册。
基线整理本身完全离线：只读文件与端口表，没有启动、停止或以任何方式
操作游戏，没有发送 HTTP 请求，没有修改任何版本锁。

> **权威副本（2026-09-19 起）**：本仓库的权威工作副本是
> `G:\qoder\sts2-a10-advisor`；本文档中的命令路径已改为该副本。
> `G:\ds harness\sts2-a10-advisor` 是同 commit 的旧副本，仅作存档，不再
> 运行批次（种子 ledger 两边字节一致，续跑只认 `G:\qoder` 这一份）。
> `data/teacher/*/manifest.json` 与 `models/*.metadata.json` 里仍写着旧
> 绝对路径，那是已冻结的构建/数据溯源记录，**不得**为了路径整洁而改写。

## 1. 代码版本与工作区状态

- 基线 commit：`57e6182`（2026-09-06 15:26 +0800，"Orphan reservation
  self-heal…"）。本轮全部修改以未提交工作区差异存在，**没有 git commit**。
- 工作区差异（`git status --short`，18 项）：
  - 本轮（生命周期修复+证据归属）修改：`bridge/trace_controller.py`、
    `bridge/autoplay.py`、`scripts/run_solver_comparison.py`、
    `scripts/supervise_solver_batch.py`、`tests/test_autoplay.py`、
    `tests/test_solver_supervisor.py`、`tests/test_combat_solver_compare.py`；
    新增 `scripts/report_solver_log_attribution.py`、
    `docs/BATCH_LIFECYCLE_FIX_2026-09-07.md`、
    `runs/evidence_attribution_20260907/`、本基线文档与
    `runs/evidence_baseline_20260908/`。
  - **本轮之前已存在的用户/上轮会话差异（未触碰）**：
    `config/combat_solver.lock.json`（用户修改，记录 0.31.0）、
    `scripts/test.ps1`、`docs/STATUS.md` 的早前小节，以及未跟踪的
    `advisor_core/route_planner.py`、`docs/ROUTE_PLANNER.md`、
    `scripts/compare_route_policies.py`、`tests/test_compare_route_policies.py`、
    `tests/test_route_planner.py`、`docs/LIVE_PROGRESS_2026-09-07.md`。

## 2. 测试结果（本轮代码的验证状态）

- 最终全量：`powershell -ExecutionPolicy Bypass -File scripts\test.ps1`
  → **395 small-runtime + 99 training = 494 全部 OK（exit 0）**，无失败、
  无跳过（Windows 实测，含真实子进程 CTRL_BREAK 测试）。
- 关键新增回归（16+5 项）：
  - 冻结菜单死角状态 → ≤72s 分类停止、零 POST、种子不消耗；
  - 桥断连/协议失败/重复失败的分类停止与有上限重试；
  - comparison 的 SIGBREAK 协作收尾（处理器安装 + **真实子进程**
    CTRL_BREAK → `status=partial, stopped_reason=signal`）；
  - supervisor 全部终止路径（含 comparison-exit-0 三个分支）执行终态
    复核；`combat_solver_logs` 开始/结束快照落盘；
  - trace 撕裂尾行容错（`trace_tail_corrupt` 标记，退出原因保留）；
  - 归属报告的纯哈希观察语义（`hash_observed_at_start` /
    `hash_observed_at_end_only`），从哈希缺席不推断创建时间。
- 边界声明：以上全部是离线契约/夹具验证；**没有任何实机整局结果**，
  不构成胜率或全自动通关证据。

## 3. 版本核对（只读；有漂移记录差异，未放宽任何校验）

| 项 | 锁定值 | 实测值 | 结论 |
| --- | --- | --- | --- |
| 游戏 | v0.111.0 / build 24724944 / commit 41cef1ea / assembly 222455745 | 相同（release_info.json + appmanifest 实测） | ✅ 与 `live_version.lock.json` 一致 |
| 桥 STS2MCP | v0.4.0，DLL `CD3EA740…` | 相同，DLL 与 manifest 哈希均匹配 | ✅ 一致 |
| STS2_MCP（solver 清单） | `CD3EA740…` | 相同 | ✅ |
| STS2-RitsuLib（solver 清单） | `189DC61B368C…` | 实测 `94557414E1EE3432…` | ⚠️ **漂移**（Workshop 自动更新） |
| CombatSolver（solver 清单） | `E97879185E85…`（0.31.0） | 实测 `118E5FB2322C…` | ⚠️ **漂移**（版本待操作员确认，可能 >0.31.0） |
| RegentFX（solver 清单，非必需） | `0F0262B3…` | 相同 | ✅ |

漂移的含义与处置（**不放宽**）：

1. `run_solver_comparison.py` 的 fail-closed inventory 校验在当前磁盘
   状态下**拒绝启动** comparison 子进程（已由真实子进程测试观察到）。
   在操作员重新对账 `config/combat_solver.lock.json`（记录新 DLL 的
   SHA-256 与版本来源）之前，第 2、3 步无法开始。
2. A10 验收白名单 `[STS2_MCP]` 不受影响，也不因此放宽；但物理模组目录
   含额外模组的已知边界依旧存在（见 2026-09-07 审计），正式验收仍不可
   在该物理布局下计分。
3. 端口 15526 当前**无监听**（游戏未运行），基线时点无活动批次。

## 4. 日志数量差异更正（点时快照语义）

历史数字差异的根因是**游戏日志目录本身在两次报告之间被游戏改写**，
不是报告缺陷：

- 2026-09-07 审计时点：5 个日志（15.13.35 / 17.47.12 / 17.47.59 /
  18.25.02 / godot.log，后二者为 0.31.0）。
- 2026-09-08 基线时点：`godot2026-09-06T15.13.35.log` 已被轮转删除；
  `godot.log` 从 32KB 增至 7.9MB（2026-09-07 23:06 → 09-08 00:18 的
  一次外部游戏会话，非本轮任何操作；内容为 CombatSolver 0.31.0 初始化
  + 1 次 SEARCH_REQUEST，0 RESULT / 0 DEPLOY_END，无 run_id）；
  新增 `godot2026-09-07T23.06.30.log`（仅初始化）。
- 本基线权威清单：
  [`runs/evidence_baseline_20260908/attribution.json`](../runs/evidence_baseline_20260908/attribution.json)
  ——5 个日志全部"无法归属"（无 run 绑定、无批次清单观察），旧报告
  （`runs/evidence_attribution_20260907/`）按约定不覆盖、保留为历史。
  归属报告从此按"生成时点快照"理解；跨时点对比必须以目录现状为准。

## 5. 第 2 步：验证一次真实停止收尾（精确手册）

**前置条件 0（操作员执行，本轮不代做）**：重新对账
`config/combat_solver.lock.json` 的 `mod_dll_inventory`——把
STS2-RitsuLib / CombatSolver 的 `sha256` 更新为实测值并把
`installed_mod_version` 改为 Workshop manifest 实际版本，注明对账时间
与来源。否则 comparison 子进程会按设计 fail closed，第 2 步无法观察
"运行中受控停止"。

**启动命令**（固定种子、小预算、独立小批次；复用生产 ledger 使种子
按序推进，不新建旁路 ledger）：

```powershell
cd "G:\qoder\sts2-a10-advisor"
python scripts/supervise_solver_batch.py --mode fixed --allow-actions `
  --batch-id ssb-stoprehearsal-<UTC时间戳> `
  --max-battles 2 --max-runs 1 --max-actions 300 `
  --game-wait-seconds 60 --run-timeout-seconds 900 `
  --seed-ledger "G:\qoder\sts2-a10-advisor\runs\solver_supervisor\ssb-20260905T193903Z-4e8b9eb1\seed_allocation.ledger.json"
```

**受控停止动作**：确认批次目录 `status.json` 出现
`combat_solver_logs.at_start`（非空、带 SHA-256）且 `game_ready` 后，
在 supervisor 控制台按 **Ctrl+C**（SIGINT → 优雅停子进程）。

**检查清单（全过才算通过）**：

1. `status.json`：`status=stopped`、`stop_reason=interrupt`、
   `completed_at_utc` 非空；三个子进程 `exit_codes` 均有值。
2. comparison sibling：`summary.json`/`manifest.json` 的
   `status=partial` 且 `stopped_reason=signal`——**不得**残留 `running`，
   **不得**是 `complete`。
3. supervisor manifest：`comparison_result.classification=partial`；
   `combat_solver_logs.at_end` 非空且与磁盘文件哈希一致；
   `autoplay_summary=null`（autoplay 被 CTRL_BREAK 强停、无
   session_end 属预期，不得伪造）。
4. seed ledger：index 3 的 reservation 要么被消费（带完整 run_id/
   identity），要么按 reconcile 规则回滚（`aborted_reservations` 有
   原因）；绝不出现 skip、重复 index 或无 run_id 的 consumed。
5. autoplay trace：零 POST 或全部 POST 均有 trace 记录与决策 id。

**任一项不过 → 结束本阶段**，不进入第 3 步；把失败清单与工件路径
原样记录。

## 6. 第 3 步：固定种子完整游戏（精确手册）

**前置条件**：第 2 步全部通过；solver lock 已对账；游戏/桥版本与锁
一致（见 §3，若再漂移先说明差异并重新对账，不放宽）。

**启动命令**（与既有生产口径一致）：

```powershell
cd "G:\qoder\sts2-a10-advisor"
python scripts/supervise_solver_batch.py --mode fixed --allow-actions `
  --max-battles 200 --max-runs 9 --max-actions 9000 `
  --run-timeout-seconds 43200 `
  --seed-ledger "G:\qoder\sts2-a10-advisor\runs\solver_supervisor\ssb-20260905T193903Z-4e8b9eb1\seed_allocation.ledger.json"
```

**启动前 ledger 核对（防重复启动/误消耗）**：读
`ssb-20260905T193903Z-4e8b9eb1/seed_allocation.ledger.json`——若仍有
index 3 的 active reservation：先读 compendium 判断保存局；有可验证
保存局（Ironclad/A10/standard、seed 一致）走 Continue 路径，无保存局
则菜单 reconcile 自动回滚后复用同一种子；若 compendium 显示的是别的
seed/身份 → 立即停止并人工裁决，绝不跳过或覆盖。

**中止条件（任一触发即停，不带病续跑）**：

- 桥健康探测 60s 未通过（supervisor 自带 game_wait_timeout）；
- autoplay 以 exit 3 分类停止（`stale_state`/`bridge_unavailable`）；
- comparison fail closed（inventory/identity/seed contract）；
- 任何 `RunIdentityError` / `SeedAllocationError`（设计即硬停）；
- 需要任何人工恢复一次以上（第 3 步验收目标为零人工恢复）；
- `--run-timeout-seconds` 到期。

**验收清单（目标是"从开始到明确终局，全程无需人工恢复且证据完整"；
死亡同样可用于验证链路，但胜率不做验收声明）**：

1. manifest：终态 complete/partial 且 `stop_reason` 明确；
   `combat_solver_logs.at_start/at_end` 完整；`autoplay_summary`/
   `comparison_result` 与子进程工件一致（无残留 running）。
2. ledger：本批每一局 consumed 记录含 `run_id`、seed、character、
   ascension、game_mode；无重复种子、无跳号。
3. trace：session provenance（版本锁、执行 owner、观察模组）+ 每局
   machine-verified run identity + 每局 `game_over` 明确终局（胜利或
   hp=0 死亡）。
4. comparison：battles/checkpoint 完整、与 trace 的 run_id 对得上；
   战斗部署证据绑定到具体 run（第 4 步机制就位后由 assessor 消费）。
5. `scripts/assess_full_run.py` 对该批次出报告：证据项齐全；缺失项
   保持 blockers 并如实记录，不补造。

## 7. 第 4～5 步衔接（2026-09-14 更新）

- 已就位：批次的 `combat_solver_logs` 哈希观察清单；归属报告工具；
  assessor 对缺失证据 fail closed。
- 第 4 步离线机制已就位：比较记录内嵌 LF 对齐的半开字节区间、SHA-256
  与 SEARCH_REQUEST/DEPLOY 计数，并绑定 `run_id`、`battle_id`、
  `decision_id`、turn。`assess_full_run.py` 仅在重新读取原始日志并通过
  路径、区间、哈希、顺序语法、动作逐项一致性和跨局重叠检查后接受；
  缺失、旧格式、篡改、冲突或序列化后丢失内存信任均保持不通过。
- 第 4 步仍待实机验收：在当前版本锁重新核对后跑受控小批，确认真实日志
  语法、轮换与保留周期能够端到端生成并由 assessor 消费上述关联；离线
  测试通过不等于实机或胜率验收通过。
- 第 5 步（路线策略比较）在第 1～4 步链路稳定前不启动；届时先小批
  基线（完成率/卡死率/人工干预/决策覆盖率），再固定环境用相同种子
  比较 baseline 与新路线前瞻。
