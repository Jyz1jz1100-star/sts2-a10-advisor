# STS2 A10 Local Advisor

本项目当前目标是联合已有的局内 Combat Solver，构建《杀戮尖塔2》的局外自动决策器：地图路线、卡牌奖励、商店、火堆、事件与其他可见选择由桥接器执行，战斗由 Combat Solver 的 full-auto 执行。局外策略使用启发式规则和严格的 live candidate codec；每局只声明一种执行模式，`combat_solver_full_auto` 模式下由 Mod 负责战斗、桥接器只负责局外动作，禁止并发战斗执行者。事件/Neow 目前只有可见候选枚举与保守兜底，尚无经过验证的效用评分策略。

最终研究验收目标仍是锁定游戏版本、战士、A10、标准单人和新随机种子，完成正式验收所需的至少 500 局真实游戏，并在 `assisted A10` 与 `no-SL A10` 两个 cohort 中分别报告结果及 Wilson 95% 置信区间。按现有协议，50% 目标默认评估 `assisted A10`，`no-SL A10` 始终并列展示；具体门槛以 [验收协议](docs/ACCEPTANCE.md) 为准。当前没有可部署的局外训练模型，也没有经过验收的实机整局结果；仓库不会把模拟器、单战斗或离线 fixture 成绩冒充真实整局成绩。

## 当前已可运行部分

- GET-only 建议器：`python -m advisor_core.live`，战斗屏交给 Combat Solver
- 局外自动驾驶：`bridge/autoplay.py`，在显式 `--allow-actions` 后执行局外 POST
- live candidate codec：从已观察状态提取带稳定 identity 的合法候选和精确 wire action
- Combat Solver 日志解析、路线执行和 full-auto keeper 的离线契约
- trace/版本锁/执行 owner/种子分区/哈希链的验收账本
- 离线行为克隆和 PPO 骨架，以及独立的 NativeAOT/Gymnasium 模拟器基线
- 模拟器三幕战役模式（2026-09-20 起，需显式开启；关闭时与历史单幕人群逐种子一致）。
  **它是近似三幕**：第二、三幕复用 Underdocks 池，真实 progression 的 Hive / Glory 未被接线
- 一条命令的自动打牌入口 `scripts/play.py`：`--backend live` 打真机，`--backend sim` 打模拟器三幕
- 真机 run 的流程覆盖判定：`scripts/audit_live_run_coverage.py` 从 trace 读出每幕先古之民、
  每个 boss 节点与终局读数（`bridge/run_progress.py`）

当前 `LiveHeuristicPolicy` 是局外可运行基线，不是已经训练或部署的全局最优策略；`SmokeBaselinePolicy` 仅用于验证状态链路，输出不得计入胜率。

## 架构

```text
游戏 -> STS2MCP 状态
     ├─ 局外屏 -> 可见信息过滤/候选 codec -> heuristic/live policy -> 局外 POST
     └─ 战斗屏 -> Combat Solver full-auto -> 战斗日志
两条路径共同写入 trace/验收账本，且每局只声明一种执行模式。
```

不采用截图 OCR 作为主输入，也不让大语言模型临场决定动作。Combat Solver 与局外策略分层；候选 codec 只枚举合法动作，不负责策略选择，解释器不能重新排序候选动作。

详见：

- [架构与训练课程](docs/ARCHITECTURE.md)
- [现有方案调研](docs/RESEARCH.md)
- [A10 50% 验收协议](docs/ACCEPTANCE.md)
- [阶段式训练操作说明](docs/TRAINING.md)
- [本地决策 Trace 数据契约](docs/TRACE_DATA.md)
- [局外候选与 wire action 契约](docs/LIVE_CANDIDATE_CODEC.md)
- [Combat Solver 分层与运行审计](docs/COMBAT_SOLVER.md)
- [当前真实状态](docs/STATUS.md)
- [逐幕覆盖审计：真机 vs 模拟器，2026-09-21](docs/ACT_COVERAGE_AUDIT_2026-09-21.md)
- [Act 1 战役报告（含撤回账目）](docs/ACT1_CAMPAIGN_2026-09-19.md)
- [三幕模拟器扩展与全自动打牌器设计](docs/superpowers/specs/2026-09-20-three-act-emulator-and-auto-player-design.md)
- [真机版本锁定、trace 录制与动作门禁](docs/LIVE_BRIDGE.md)
- [上游与许可证](UPSTREAM.md)

## 一条命令打牌

```powershell
# 真机：游戏在跑就直接打；没跑则要求本机 Steam 客户端在跑，由它 -applaunch 2868840
& ..\third_party\slay-the-spire-2-emulator-main\.venv\Scripts\python.exe `
  scripts\play.py --backend live
# 只想先自检、一个动作都不发：加 --preflight-only

# 模拟器：三幕战役，逐种子落可复核证据。--config 必须是该 checkpoint 训练时用的那份，
# 否则阶段配置与观测契约对不上，跑出来的数字不属于那个人群
& ..\third_party\slay-the-spire-2-emulator-main\.venv\Scripts\python.exe `
  scripts\play.py --backend sim --config runtime\fanout\b_terminal-1.toml `
    --checkpoint <ckpt.zip> --seeds 130008177 --out <json>
```

`play.py` 是分发器，不是第二套实现。战斗仍然完全归局内 Combat Solver，局外动作归
`bridge.autoplay`，一批真机的运行与自证归 `scripts/supervise_solver_batch.py`。它补的是没人
该记住的那几步：把游戏拉起来、开跑之前先让**监督器自己的 `--dry-run`** 回答"能不能跑"
（自 2026-09-21 起这条是真的：`_preflight_gates()` 加载 `VersionLock` 并核对实机 build，
不匹配就以 `EXIT_PREFLIGHT_FAILED` 拒绝起局；模组字节测不出/读不到同样拒绝），
以及把结论读回来打印。preflight 不过时它以 exit 2 明确拒绝，不会假装成功。

**求解器的版本漂移按目的分两条轨**（2026-09-21 纠正：此前一个 `required` 哈希门同时服务两种目的，
于是验收整局被比较实验的门禁绑架，与本文"漂移只报告、不否决"的承诺不一致）：
`--track acceptance`（`play.py --backend live` 的默认）只点名并记录求解器字节，漂移写进
`acceptance_blockers`，批次照跑；`--track comparison` 仍必须钉死，因为那条实验的自变量就是求解器本身。
**两条轨都仍然拒绝缺失或不可读的必需模组**，且**批内**字节变化一律
`invalidated_by_mod_update`（放开的只有"批前就已漂移"这个既成事实）。求解器锁本身归 operator。

`--backend sim` 转发到 `scripts/probe_three_act_campaign.py`，scope 是
`simulator_three_act`，并自动带上 `environment_version` 与 `content_coverage`。
2026-09-22 起当前版本是 **`sts2sim-campaign-fidelity-v2`**（verdict 仍是 `approximate`）：
第二、三幕不再抽 Underdocks 换皮池，而是各抽自己那一幕 —— Overgrowth → Hive → Glory
（`ActModel.cs:510-515`），最终幕的配对 boss 改成"先回地图、再走到 boss 之后那一行"，
并且删掉了真实游戏根本不发的 boss 遗物奖励。这关闭了 G6/G1/G3；**G2（每幕先古之民）、
G4（每幕地图形状）、G5（升级概率与 boss 奖励分布）仍然不真实**，模拟器也没有任何一幕
有先古之民。旧版本号 `sts2sim-campaign-approx-v1` 已被摘要冻结：带这个标签的历史产物
永远表示"二、三幕是换皮"，两种环境的产物不得平均、不得互判、不得续档。
幕区间与真机的对照只用已核实的坐标：真机 act 1/2/3 的 boss 节点分别在 **floor 17 / 33 / 48**
（2026-09-20 trace 重算，`docs/evidence/live_run_coverage_20260920.json`），模拟器的幕带是
1–17 / 18–33 / 34–50（`RunConstants.cs` 的 `MapBossRow = 16` 推出，第三幕多的 1 层是配对 boss）。
它是模拟器结论，不是实机 A10 成绩，也不替代 `full_run` 的真实整局验收。逐幕证据见
[逐幕覆盖审计](docs/ACT_COVERAGE_AUDIT_2026-09-21.md)，六门保真度的度量与判据见
[模拟器幕内容保真度](docs/SIMULATOR_ACT_FIDELITY_2026-09-21.md)。

## 本机环境

运行时工具链完全放在本项目的 `.tools/` 下，缓存和检查点放在 G 盘，不依赖已损坏的系统 Python：

- uv 0.12.7
- CPython 3.12.14
- .NET SDK 9.0.317

重编模拟器原生库用 `scripts\build_emulator.cmd`。三条前提值得写在这里，因为它们各自看起来像
另一种故障：系统 `C:\Program Files\dotnet` 只有运行时、**没有 SDK**；本机没有 HTTPS 出口，装不了
SDK；NativeAOT 的链接步骤要 `vcvars64.bat` 提供 `VCToolsInstallDir`，否则 `link.rsp` 里会全是空的
`/LIBPATH:`，报出来的却是一句 VS 自己的 `'Analysis' 不是内部或外部命令`。AOT 编译器包已离线缓存在
`.cache/nuget`。另外，导出符号是在 `Sts2Emulator.csproj` 里用 `/EXPORT:` 手工挂的——漏挂时版本门
照样通过、`ctypes` 才报找不到符号。

训练/模拟器使用相邻的 `third_party/slay-the-spire-2-emulator-main/.venv`，其
Gymnasium、NumPy、PyTorch、Stable-Baselines3 和 sb3-contrib 版本由
`requirements-training.txt` 与上游 `uv.lock` 对齐。本机 RTX 4070 12GB 的
训练 smoke 验证使用该训练环境完成；轻量运行时环境不要求这些包。

## 测试

```powershell
powershell -ExecutionPolicy Bypass -File scripts\test.ps1
```

`scripts\test.ps1` 先在轻量运行时环境执行纯契约测试，再通过
`STS2_TRAINING_PYTHON` 执行 NumPy/PyTorch/Gymnasium 测试；未设置时默认使用上面
相邻模拟器的 `.venv\Scripts\python.exe`。如需指定其他训练环境：

```powershell
$env:STS2_TRAINING_PYTHON = 'D:\envs\sts2-training\Scripts\python.exe'
powershell -ExecutionPolicy Bypass -File scripts\test.ps1
```

也可以在单一环境中复现完整发现：

```powershell
python -m pip install -r requirements-test.txt
python -m unittest discover -s tests
```

`requirements.txt` 只安装实时运行时依赖；训练环境的 `sts2_gym` 是相邻模拟器
checkout 的本地源码，不从 PyPI 伪装安装。模拟器或其原生 DLL 不可用时，相关
集成测试会自动跳过。

## 实时只读联调

先按 [STS2MCP](https://github.com/Gennadiyev/STS2MCP) 的说明安装并启用模组，确认：

```text
http://127.0.0.1:15526/api/v1/singleplayer?format=json
```

然后运行：

```powershell
.\.tools\python\cpython-3.12-windows-x86_64-none\python.exe -m advisor_core.live
.\.tools\python\cpython-3.12-windows-x86_64-none\python.exe overlay\overlay.py
```

第一条命令只发 GET 请求并写 `runtime/advice.txt`，不会向游戏发送动作。

## 训练监督

所有正式训练都必须通过监督器启动，并固定游戏 build：

```powershell
.\.tools\python\cpython-3.12-windows-x86_64-none\python.exe -m training.supervisor `
  --run-dir runs `
  --game-build BUILD_ID `
  --character IRONCLAD `
  --ascension 10 `
  -- <训练命令及参数>
```

每次运行生成独立 manifest、心跳和完整日志。模拟器评测与真实游戏评测必须分开保存。

三阶段 MaskablePPO 基线编排可先做只读检查：

```powershell
& ..\third_party\slay-the-spire-2-emulator-main\.venv\Scripts\python.exe `
  -m training.curriculum --config config\training.toml --dry-run
```

配置中的 `combat -> act1 -> full_run` 每阶段均保存 checkpoint、使用独立评测
seed、生成 JSON metrics，并且只有达到胜率、Wilson 下界、截断率、非法动作数和
楼层门槛后才晋级。`full_run` 当前是显式 opt-in 的实验阶段；本地模拟器尚未证明
多 Act A10 等价，因此其结果永远不会标记为真实 A10 成绩。自 2026-09-20 起模拟器确实有第三幕，
用 `python -m training.evaluate_checkpoint --campaign` 走：它把 scope 换成
`simulator_three_act`、并额外报 `campaign_clears`，与既有的 `wins` 并列而不合并——
把一次 Act 1 boss 胜利改叫三幕通关，会让改动前的所有胜率失去可比性。

## Trace 数据校验

当前 public-beta 数据默认锁定为 `public-beta-v0.111.0`。正式训练前必须校验每条
记录的可见状态、合法动作、所选动作、结果、战士 A10/NOSL 条件，并排除
train/validation/test 之间的 seed 泄漏：

```powershell
.\.tools\python\cpython-3.12-windows-x86_64-none\python.exe -m training.validate_traces `
  data\local\train.jsonl data\local\validation.jsonl data\local\test.jsonl
```

完整字段清单和示例见 [Trace 数据契约](docs/TRACE_DATA.md)。

校验后的 train trace 可用于小型离线行为克隆基线；训练命令及 checkpoint 的
版本、seed 和 SHA-256 记录方式也见该文档。该基线不连接或操作游戏。

## 已测基线

- Gymnasium 环境检查：通过。
- 窄范围开局普通战斗启发式：100/100；这只说明开局样例很容易，不能外推。
- 完整局 `first-valid`：0/20，平均约 90.8 步。
- 6,144 步 CUDA MaskablePPO smoke：约 839 FPS；3 个快速未见种子为 0/3。

因此下一阶段不是继续把模拟器 PPO 当作局外部署模型，而是先修正并接通 live 候选/奖励闭环，固定 Combat Solver 版本，完成少量可审计的实机整局，再比较路线/商店/火堆策略对整局结果的影响。

## 当前实验结论（截至 2026-09-20）

2026-09-03 的 Act 1 模拟器 bulk 实验确实完成了 20,000,000 steps；最终评测为
`1/500 = 0.2%`，平均最终楼层 `7.67`，checkpoint 未晋级（`promoted=false`）。
该结果的 scope 是 `simulator_act1`，不能作为实机整局或 Combat Solver 联合系统的胜率。
详见 [`experiment-final.json`](runs/bulk_training/act1-pretrained-r3-bulk-20260903T0640Z/act1/metrics/experiment-final.json)。

离线候选、autoplay、版本锁和 full-run ledger 测试可以证明契约与拒绝非法状态；它们不能证明真实游戏从开局运行到胜利。当前仍缺少经过锁定版本、单一执行 owner、完整 provenance 和终局证据的实机整局样本。

2026-09-05 的离线小运行时集合为 `337/337` 通过；`scripts/test.ps1` 另有
训练环境单元测试 `99/99` 通过。这些是契约/模拟器测试，不是实机整局验收。

2026-09-20：模拟器长出第三幕（战役模式，默认关闭）。同一晚第一次出现可复核的模拟器内
Act 1-3 全清——种子 `130008177` 走到 floor 50、`campaign_cleared=1`、`illegal_actions=0`，
两次独立复跑同结果（上面那条 `--backend sim` 示例是照原样执行过的，即第三次），
证据文件 `docs/evidence/three_act_first_clear_20260920.json` 已进哈希链。
同一晚也量清了它的边界：战役关闭时既有 9 个具名 Act 1 胜局仍逐种子复现（回归门），而一个
500 连续种子的 promotion 窗口里 18 局打到 Act 1 boss、**0 局打赢**。

**上一段曾经接着写"所以能不能走完三幕从现在起是策略强度问题，不再是表达能力问题"。这句撤回。**
它依据的是"模拟器能走到 floor 50"，而那个 50 站在 Underdocks 池上：锁定 build 的真实 progression
是 Overgrowth → Hive → Glory，模拟器的第二、三幕都不是它们的池，也没有任何一幕有先古之民，
终幕双 boss 之间也没有真实的地图出口。所以**三幕覆盖仍是表达能力问题**；
只有"三阶段流程能否自动跑完"才已被模拟器证明。分级见 `training/campaign_content.py`。

过程中被数据抓到并修掉的一个真缺陷值得记下来：幕推进最初只挂在遗物领奖那一个出口，于是
两个种子打赢第一幕 boss 却停在第 17 层——boss 的奖可能从遗物屏、也可能从选卡屏结掉，
这与本文早已记录的"boss 胜局只在遗物屏出口才判定"是同一个**出口清单不全**的坑。

2026-09-21 交付目标纠偏，逐项见 [逐幕覆盖审计](docs/ACT_COVERAGE_AUDIT_2026-09-21.md)。三条需要点名的更正：

1. **真机已经走过真实第三幕，比本文此前承认的多。** 离线回放 2026-09-20 两条 trace
   （`--allow-actions --out-of-combat-only`，动作由桥接器发出）得到
   `docs/evidence/live_run_coverage_20260920.json`：一条 run 连续覆盖三幕的先古之民
   （NEOW / OROBAS / TANX），Hive 的 boss `THE_INSATIABLE` 实测清除，最深到 `act 3 floor 48`
   的 Glory boss `AEONGLASS`——**在那里战死**。
2. **"固定种子需要候选桥接（换 DLL，禁区）"这条前提已过期。** 2026-09-06 起已装的就是
   `sts2mcp-seeded` 候选，二进制里确有 `BeginStandardSingleplayerSeededRun`；本轮之后装的是
   带胜负位的候选（它也保留 seed 注入能力）。
3. **"真实胜利不可观测"已在 2026-09-21 授权复验中解除。** 原先 STS2MCP 把胜负压成同一句
   `"Run ended."`（`McpMod.StateBuilder.cs:455-459`），判定器在这条链上永远不会返回"胜"——
   这是实现缺陷，不是策略不够强。带 `is_victory` 的桥接经 `scripts/stage_sts2mcp.py --apply`
   安装（哈希校验备份 + 回滚可用），health/state/compendium smoke 通过后才同步两份锁里的
   **STS2_MCP** 一行；**CombatSolver 的自动更新漂移只记录、不代钉**。
   真机终止屏实测 `is_victory=false`，读数来源 `bridge_is_victory_flag`
   （证据 `docs/evidence/victory_observability_20260921.json`）。
   **注意边界：验到的是败局分支。** "赢"这个分支仍未跑过——它需要真的清掉终幕第二个 boss。
   所以"真实整局胜利"这条验收项**还没达成**，只是不再被观测能力卡住。

仍未完成：真实第三幕的**第二个 boss** 没有任何一次到达记录，因此也没有一次真实胜利；
模拟器的 Hive/Glory 池表未接线（内容其实已在 `CombatFactory.cs` 里，下标
`66/70/71/73/75/76/78/80/81` 无人引用）；三幕引用的行号仍需按新构建重钉。
另需知道：本轮真机验证结束时，驱动按既有菜单逻辑**另起了一局铁甲战士 A10**（第 1 幕第 1 层），
没有 `abandon_run`、没有删档；那局留在机器上待处置。

## License

Advisor code is MIT licensed; see [LICENSE](LICENSE). Slay the Spire 2 and its data/assets belong to Mega Crit. Third-party code and datasets keep their own licenses.
