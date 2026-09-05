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
- [真机版本锁定、trace 录制与动作门禁](docs/LIVE_BRIDGE.md)
- [上游与许可证](UPSTREAM.md)

## 本机环境

运行时工具链完全放在本项目的 `.tools/` 下，缓存和检查点放在 G 盘，不依赖已损坏的系统 Python：

- uv 0.12.7
- CPython 3.12.14
- .NET SDK 9.0.317

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
多 Act A10 等价，因此其结果永远不会标记为真实 A10 成绩。

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

## 当前实验结论（2026-09-05）

2026-09-03 的 Act 1 模拟器 bulk 实验确实完成了 20,000,000 steps；最终评测为
`1/500 = 0.2%`，平均最终楼层 `7.67`，checkpoint 未晋级（`promoted=false`）。
该结果的 scope 是 `simulator_act1`，不能作为实机整局或 Combat Solver 联合系统的胜率。
详见 [`experiment-final.json`](runs/bulk_training/act1-pretrained-r3-bulk-20260903T0640Z/act1/metrics/experiment-final.json)。

离线候选、autoplay、版本锁和 full-run ledger 测试可以证明契约与拒绝非法状态；它们不能证明真实游戏从开局运行到胜利。当前仍缺少经过锁定版本、单一执行 owner、完整 provenance 和终局证据的实机整局样本。

2026-09-05 的离线小运行时集合为 `337/337` 通过；`scripts/test.ps1` 另有
训练环境单元测试 `99/99` 通过。这些是契约/模拟器测试，不是实机整局验收。

## License

Advisor code is MIT licensed; see [LICENSE](LICENSE). Slay the Spire 2 and its data/assets belong to Mega Crit. Third-party code and datasets keep their own licenses.
