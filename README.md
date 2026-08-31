# STS2 A10 Local Advisor

本项目目标是为《杀戮尖塔2》提供本地、只读、可解释的实时决策建议：战斗中给出逐张牌、目标、药水与结束回合顺序；局外给出卡牌、路线、事件、商店、遗物和火堆建议。玩家始终在游戏界面中手动操作。

最终研究验收目标是：锁定游戏版本、战士、A10、标准单人、新随机种子、禁止 SL，真实游戏至少 500 局的点估计胜率达到 50%，并报告 Wilson 95% 置信区间。当前尚未达到该目标；仓库不会把模拟器或单战斗成绩冒充真实整局成绩。

## 当前已可运行部分

- GET-only 本地状态轮询：`python -m advisor_core.live`
- 合法动作枚举：战斗出牌/目标/药水/结束回合及主要局外决策
- 模板化中文解释：只展示模型已计算的事实、分差和置信度
- 本地训练监督器：保存命令、版本、角色、难度、心跳、日志和退出码
- 离线行为克隆骨架：共享可见状态编码、phase 分头动作打分、严格合法动作 mask
- 统计验收工具：Wilson 区间
- 复用的 overlay/autostart 骨架
- 独立下载并已构建的 NativeAOT/Gymnasium 模拟器基线

当前 `SmokeBaselinePolicy` 只用于验证状态链路，明确不是训练模型，输出不得计入胜率。

## 架构

```text
游戏 -> STS2MCP 只读状态 -> 可见信息过滤/合法动作
     -> 战术或战略策略价值网络 -> 信息集搜索
     -> 结构化中文解释 -> 置顶浮窗 -> 玩家手动操作
```

不采用截图 OCR 作为主输入，也不让大语言模型临场决定动作。战斗与局外策略分层训练，解释器不能重新排序候选动作。

详见：

- [架构与训练课程](docs/ARCHITECTURE.md)
- [现有方案调研](docs/RESEARCH.md)
- [A10 50% 验收协议](docs/ACCEPTANCE.md)
- [阶段式训练操作说明](docs/TRAINING.md)
- [本地决策 Trace 数据契约](docs/TRACE_DATA.md)
- [当前真实状态](docs/STATUS.md)
- [真机版本锁定、trace 录制与动作门禁](docs/LIVE_BRIDGE.md)
- [上游与许可证](UPSTREAM.md)

## 本机环境

工具链完全放在本项目的 `.tools/` 下，缓存和检查点放在 G 盘，不依赖已损坏的系统 Python：

- uv 0.12.7
- CPython 3.12.14
- .NET SDK 9.0.317
- PyTorch 2.12.0 + CUDA 13.0、Stable Baselines3、sb3-contrib

本机 RTX 4070 12GB 已通过 `torch.cuda.is_available()` 和一次 12 环境 smoke 训练验证。

## 测试

```powershell
powershell -ExecutionPolicy Bypass -File scripts\test.ps1
```

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

因此下一阶段不是盲目拉长 PPO，而是先安装状态桥、采集真实 trace、收敛模拟器一致性，再按战斗 -> 单 Act -> A0 -> A3/A6/A9 -> A10 的课程训练。

## License

Advisor code is MIT licensed; see [LICENSE](LICENSE). Slay the Spire 2 and its data/assets belong to Mega Crit. Third-party code and datasets keep their own licenses.
