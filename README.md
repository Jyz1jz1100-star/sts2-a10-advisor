# STS2 A10 Local Advisor

《杀戮尖塔2》(Slay the Spire 2) 铁甲战士 / ascension 10 的**局外自动决策器**：地图路线、卡牌奖励、
商店、火堆、事件等局外屏由本仓库的桥接器执行；战斗屏交还给局内 Combat Solver mod 的 full-auto。
分层原则是硬性的——**一局只有一个动作拥有者**，候选 codec 只枚举合法动作、不做策略选择，解释器
不能重排候选。

> TL;DR (EN): an out-of-combat auto-player for Slay the Spire 2 (Ironclad, A10). It reads live game
> state over the STS2MCP loopback HTTP API, executes only out-of-combat choices, and delegates combat
> to the in-game Combat Solver mod. This is research code with an acceptance protocol attached, not a
> "beat the game for me" tool: see [当前状态](#当前状态一句话) before you trust any number in here.

---

## 当前状态一句话

| 能力 | 状态 |
| --- | --- |
| 只读建议器（GET-only，不发动作） | 可运行 |
| 桥接器 + overlay（轮询、屏分类、建议落盘） | 可运行 |
| 局外自动驾驶（真机 POST） | 可运行，但需显式 `--allow-actions` 授权 |
| 一条命令打完整局（真机 / 模拟器） | 可运行（`scripts/play.py`） |
| 模拟器三幕战役 | 可运行，环境版本 `sts2sim-campaign-fidelity-v5`，六项硬保真门 G1–G6 已 PASS，verdict 仍是 `approximate` |
| **经验收的真实整局胜利** | **没有。** 尚无任何满足版本锁 + 单一动作拥有者 + 完整 provenance + 终局证据的实机整局样本 |
| 可部署的局外训练模型 | 没有。当前局外基线是 `LiveHeuristicPolicy`（规则），`SmokeBaselinePolicy` 只用于验证状态链路，其输出不得计入胜率 |

本仓库不会把模拟器、单战斗或离线 fixture 的成绩冒充真实整局成绩。研究验收目标（锁定游戏版本、
战士、A10、标准单人、新随机种子，至少 500 局真实游戏，`assisted A10` 与 `no-SL A10` 两个 cohort
分别报告 Wilson 95% 区间）见 [docs/ACCEPTANCE.md](docs/ACCEPTANCE.md)；其中 500 是下限，1,000 更稳。

---

## 你需要知道的三件事（读代码之前）

1. **默认什么都不会发生。** 只读链路（`advisor_core`、`bridge/client.py`）在构造上就没有 POST 方法。
   驾驶链路必须显式给 `--allow-actions`，否则只会打印 `would_post` 预览然后退出。
2. **只有 loopback。** 控制器构造时强制 base URL 为 `http` 且 host ∈ {`127.0.0.1`, `localhost`, `::1`}，
   否则直接 `ValueError`。没有任何远程/公网模式。
3. **训练产物不在仓库里，也不会在。** `runs/`、`checkpoints/`、`models/`、`runtime/` 与抓来的游戏数据
   (`data/cards.json` 等) 全部 gitignore。所以本仓库可复现的是**契约与流程**，不是"下载即得同一个策略"。
   需要复现某个 checkpoint 的结果，必须由持有该 checkpoint 的人连同其 `--config` 一起提供。

---

## 环境要求

| 层 | 要求 |
| --- | --- |
| 运行时（建议器 / 桥 / 驾驶） | Python 3.11+，`pip install -r requirements.txt`（只有 `requests`、`PyYAML` 两个包）。overlay 需要 stdlib `tkinter`，缺 tcl/tk 要重跑 Python 安装器勾上 |
| 模型建议（`bridge.main` 的 LLM 轨） | 需要 `claude` CLI（Claude Code，订阅制，无需 API key）。`setup.ps1` 会阻塞直到它可用或你选择跳过 |
| 模拟器 / 训练层（可选） | 独立环境，`pip install -r requirements-training.txt`（精确 pin：gymnasium / numpy / torch / stable-baselines3 / sb3-contrib），再加相邻的 `slay-the-spire-2-emulator` checkout——`sts2_gym` 是它的本地源码，不是 PyPI 包 |
| 打真机 | Windows + 已安装并运行的《杀戮尖塔2》+ [STS2MCP](https://github.com/Gennadiyev/STS2MCP) mod + Steam 客户端（用于 `-applaunch 2868840`） |
| 战斗交给 Combat Solver | 另需 [Combat Solver](https://github.com/Torch1230/CombatSolver) mod 与 RitsuLib；它对外的唯一接口是游戏诊断日志 |

**平台边界**：只读建议器 + 桥 + overlay 这一层是纯 stdlib/requests，`autostart/` 也提供了 Linux
`.desktop` 与 macOS LaunchAgent 安装器，可跨平台。真机驾驶层是 **Windows-only**：Steam 路径
(`C:\Program Files (x86)\Steam\steam.exe`)、`tasklist` 探测、游戏日志目录
(`%APPDATA%\SlayTheSpire2\logs`)、原生模拟器 (`win-x64` self-contained) 都是硬前提。

---

## 安装

```bash
git clone https://github.com/Jyz1jz1100-star/sts2-a10-advisor.git
cd sts2-a10-advisor
python -m pip install -r requirements.txt
python data/fetch_data.py                    # 把卡/遗物数据集抓到本地（gitignored，不要提交、不要再分发）
python -m bridge.model_client --selftest     # 真调一次模型，校验 warm-session 与 stdin JSON 形状
```

Windows 可一条命令走完上面这几步并附带探测 STS2MCP 是否在线：

```powershell
.\install.bat          # 只是 setup.ps1 的包装
```

macOS / Linux：

```bash
bash install.sh
```

两个安装脚本都不装训练栈，也不碰 mod——STS2MCP 请按其上游说明单独安装。装好后自检：

```bash
curl http://127.0.0.1:15526/api/v1/singleplayer?format=json   # 游戏在跑 + mod 在跑才有响应
```

---

## 运行说明

下面所有命令都在仓库根目录执行，`python` 指你装好 `requirements.txt` 的那个解释器。

### A. 只读建议器（安全，先跑这个）

```bash
python -m advisor_core.live                 # 轮询循环
python -m advisor_core.live --once          # 读一次就退出
python -m advisor_core.live --output out.txt --poll 0.5 --policy heuristic
```

- 它**只发 GET**（`Accept: application/json`，超时 1s），不写游戏、不 POST。
- 默认地址 `http://127.0.0.1:15526/api/v1/singleplayer?format=json`，默认输出
  `runtime/latest_advice.txt`，默认心跳 `runtime/heartbeat`（父目录自动创建）。
- `--policy heuristic`（默认）= A10 局外规则；战斗屏保持沉默，交给 Combat Solver 面板。
  `--policy smoke` 只是联调基线，**输出不得计入胜率**。
- 状态指纹不变就不重写文件；遇到不支持的屏（战斗 / event / Neow）写入固定说明文案。
- 网络失败被静默吞掉（设计上就让它继续轮询），所以"没输出"通常意味着连不上桥，而不是崩了。
- 这个入口**不读 `config.yaml`**，改配置请用下面的桥。

### B. 桥 + overlay（把建议显示到屏幕上）

```bash
python -m bridge.main        # 轮询 + 屏分类 + grounding + 调模型 + 写 runtime/latest_advice.txt
python overlay/overlay.py    # 读取建议并画在半透明窗口上
```

```powershell
.\start.ps1 ; .\stop.ps1     # 起 / 停上面两个进程（stop 按命令行模式匹配，会一并停 autostart supervisor）
```

没有游戏也能验链路：把 `config.yaml` 的 `dev.use_sample_state` 设为 `true`，桥会读
`data/sample_state.json` 而不是真机。`dev.log_prompts: true` 会把实际发出的 prompt 记进日志。

### C. 局外自动驾驶（会真发指令，需要授权）

```bash
# 1) 先看它打算做什么——一个 POST 都不发
python -m bridge.autoplay --dry-run

# 2) 只打局外动作，战斗归 mod，并留可复核 trace
python -m bridge.autoplay --allow-actions --out-of-combat-only \
  --execution-owner combat_solver_full_auto \
  --trace runs/scratch/autoplay_trace.jsonl --max-runs 1

# 3) 完全手动（局外动作也由本仓库执行）
python -m bridge.autoplay --allow-actions --execution-owner http_route_executor --max-runs 1
```

关键参数（默认值即代码默认）：

| 参数 | 默认 | 作用 |
| --- | --- | --- |
| `--base-url` | `http://127.0.0.1:15526` | 必须是 loopback HTTP |
| `--allow-actions` | 关 | 唯一的 POST 授权开关；`--dry-run` 会把它压制掉 |
| `--dry-run` | 关 | 只轮询 + 打印最多 10 屏的 `would_post`，然后返回 0 |
| `--out-of-combat-only` | 关 | 永不执行战斗动作；必须与 `--execution-owner combat_solver_full_auto` 成对出现，两个方向都有 `ValueError` |
| `--execution-owner` | 推导 | `http_route_executor` / `combat_solver_full_auto` / `manual_player`；给了 `--out-of-combat-only` 时默认后者 |
| `--max-runs` / `--max-actions` | 30 / 3000 | 硬上限，防止失控连打 |
| `--lock-file` | `config/live_version.lock.json` | 启动即校验实机 build、桥字节、已装 mod 清单，任一不符就拒绝 |
| `--trace` | 不记 | 给定路径才建 TraceRecorder；验收链路的证据来源 |
| `--cohort` | `assisted` | `assisted` / `no-sl` |
| `--seed-mode` | `observational` | `fixed` 必须给 `--seed-file`；`observational` 永远拒绝 `--seed-file` |
| `--seed-ledger` / `--batch-dir` | 无 | 预注册种子分区，防 train/validation/test 泄漏；带分配 + 真授权时必须给其一 |
| `--log-dir` | `%APPDATA%\SlayTheSpire2\logs` | 战斗路由来源；传 `''` 表示禁用战斗执行 |
| `--fullauto-keeper-active` | 关 | 把 keeper 记为 watchdog，**永不**当作第二动作拥有者 |

退出码：`3` = `EXIT_CLASSIFIED_STOP`，按分类原因主动收尾（例如分配用尽），不是故障。

**结构性红线**（不是配置项，是代码路径本身）：

- 菜单分支只会点 `main_menu` / `back`（且仅当菜单自己列出）/ `continue`（先做存档身份校验）；
  **不存在 `abandon_run` 这条代码路径**，存档身份不匹配是硬停而不是重试。
- 开局路径只点 `singleplayer` / `standard` / `IRONCLAD` / `set_ascension` / `confirm`，
  遇到不受支持的菜单屏直接报错停下。
- 每个 POST 前重读状态并比对 decision-id，不符就是 `Stale decision` 异常。

**唯一的例外要说清楚**：`python -m bridge.fullauto_keeper` 会在观察到"回合 1 没有
`FULL_AUTO_DEPLOY`"时**点击一次游戏 UI** 的 full-auto 开关（坐标按 1026x768 校准）。它是 watchdog，
不是策略执行者，默认**不是** dry-run——调试它请用 `--dry-run`（只打印不点击），可用参数为
`--log-dir` / `--poll` / `--battle-timeout`。

### D. 一条命令打完整局

```bash
# 真机：游戏在跑就直接打；没跑则要求 Steam 客户端在跑，由它 -applaunch 2868840
python scripts/play.py --backend live
python scripts/play.py --backend live --preflight-only     # 只过闸门，一个动作都不发

# 模拟器：三幕战役，逐种子落可复核证据
python scripts/play.py --backend sim \
  --config config/training_v2.toml --checkpoint <ckpt.zip> --seeds 130008177 --output out.json
```

`play.py` 是**分发器，不是第二套实现**：战斗仍归局内 Combat Solver，局外动作归 `bridge.autoplay`，
一批真机的运行与自证归 `scripts/supervise_solver_batch.py`。它只补三件没人该记住的事——把游戏拉起来、
开跑前用**监督器自己的 `--dry-run`** 回答"能不能跑"（闸门与实跑同源，不匹配就以
`EXIT_PREFLIGHT_FAILED` 拒绝起局；模组字节测不出/读不到同样拒绝）、把结论读回来打印。

- `--backend sim` 会转发给 `scripts/probe_three_act_campaign.py`，因此**必须**用装了训练栈的解释器跑，
  且 `--checkpoint` 与 `--seeds` 都必填（种子拒绝重复）。
- `--config` 必须是**该 checkpoint 训练时用的那一份**，否则阶段配置与观测契约对不上，跑出来的数字不属于
  那个人群。
- 模拟器结论不是实机 A10 成绩，也不替代 `full_run` 的真实整局验收。

**求解器的版本漂移按目的分两条轨**（此前一个 `required` 哈希门同时服务两种目的，于是验收整局被"比较实验"
的门禁绑架，与"漂移只报告、不否决"的承诺不一致，2026-09-21 已纠正）：

- `--track acceptance`（`--backend live` 的默认）：只点名并记录求解器字节，漂移写进
  `acceptance_blockers`，批次照跑。
- `--track comparison`：必须钉死全部模组，因为那条实验的自变量就是求解器本身。

**两条轨都仍然拒绝缺失或不可读的必需模组**，且**批内**字节变化一律判
`invalidated_by_mod_update`（放开的只有"批前就已漂移"这个既成事实）。求解器锁本身归 operator。

注意：`play.py` 的 live 路径**总是**以 `--mode observational` 起监督器，因此它的结论天然带
`observational_mode` 等 blocker——这是**故意的**：观察模式不等于固定种子验收，两者是不同的 cohort，
不能互相冒充。它自己用 `2` 表示"游戏起不来"或"预检被拒"（不会假装成功），监督器侧的预检失败码是
`EXIT_PREFLIGHT_FAILED`。

### E. 批次监督（要证据链时用这个）

```bash
python scripts/supervise_solver_batch.py --mode observational --dry-run
python scripts/supervise_solver_batch.py --mode fixed --dry-run
python scripts/supervise_solver_batch.py --mode observational --allow-actions --max-battles 50
```

它会拉起三个子进程：`scripts/run_solver_comparison.py`（逐战斗比对，`--automated` + `--mod-gate`）、
`python -m bridge.autoplay … --allow-actions --out-of-combat-only --lock-file
config/combat_solver.lock.json --log-dir ''`、以及 `python -m bridge.fullauto_keeper`。
`--mod-gate` 由 track 决定：`acceptance -> attest`，`comparison -> strict`。
批次的运行状态、模组自证与 blocker 落在 `runs/solver_supervisor/<batch-id>/status.json`。

纯只读的比较轨（不 POST、不模拟输入）：

```bash
python scripts/run_solver_comparison.py --batch-id csb-001 --dry-run
```

---

## 配置

`config.yaml` 是**桥**用的（顶层 7 个键：`bridge` / `debounce` / `model` / `paths` / `overlay` /
`autostart` / `dev`），加载器不做默认值合并、文件缺失即抛错，且只在启动时读一次。

| 键 | 默认 | 说明 |
| --- | --- | --- |
| `bridge.base_url` | `http://127.0.0.1:15526` | 端口在 mod 自己的 `STS2_MCP.conf` 里设 |
| `bridge.state_path` / `compendium_path` / `health_path` | `/api/v1/singleplayer` / `/api/v1/compendium` / `/` | 全部 GET；advisor 从不 POST |
| `bridge.poll_interval_s` / `request_timeout_s` / `reconnect_backoff_s` | `0.3` / `4.0` / `3.0` | |
| `debounce.settle_s` | `0.5` | 状态稳定前不出手 |
| `model.default` / `model.per_screen` | `sonnet`；`ancient` / `boss_card_select` / `relic_select` 用 `opus` | 逐屏路由 |
| `model.cli.executable` | `claude` | `base_flags` 含 `-p --output-format stream-json --verbose --disallowed-tools *`——**工具被硬禁用**，模型只能出文本 |
| `model.thinking_budget_tokens` / `max_output_tokens` / `recycle_after_turns` | `0` / `600` / `40` | |
| `paths.runtime_dir` / `advice_file` / `log_file` | `runtime` / `runtime/latest_advice.txt` / `runtime/advisor.log` | `runtime/` 已 gitignore |
| `overlay.*` | 0.4s 轮询、心跳 5s 判陈旧、460x320、opacity 0.92、Consolas 11 | `x/y: null` 表示自动定位 |
| `autostart.mutex_port` | `15580` | 单实例锁用的回环端口，不是服务端口 |
| `dev.use_sample_state` / `sample_state_file` / `log_prompts` | `false` / `data/sample_state.json` / `false` | 离线验链路的开关 |

`config/` 下的其余文件按用途分三类：

- **训练配置**（`training*.toml`、`pilot_campaign.toml`、`production_campaign_v5.toml`）：给
  `python -m training.curriculum` / `training.v2_curriculum` 用 `--config` 传入；里面写死
  `[target] character/ascension`、阶段序列、晋级门（胜率、Wilson 下界、截断率、非法动作数、楼层）。
- **比较轨配置** `combat_solver.toml`：`[reader] mode = jsonl|logtail|directory`、日志与设置路径、
  `[gates]` 的 Wilson 界与绝对 HP 误差、`[automated_gates]` 的 deploy-log 覆盖率要求。
- **两份版本锁**（schema_version 1，`bridge/trace_controller.py::VersionLock` 读取）：
  - `live_version.lock.json`——**A10 验收轨**的"游戏 + 桥"身份（app id / build id / version / commit /
    主程序集哈希 / 桥 dll 与 manifest 的 SHA-256），并且 `allowed_mod_ids` 只允许 `STS2_MCP`：多装的模组
    报 "would contaminate evaluation"，缺的报 "Required live mods are missing"。
  - `combat_solver.lock.json`——**比较轨**的模组清单（求解器 + RitsuLib 等），文件里自己写明"这份清单
    下的结果永远不计入验收协议"。字节级自证由 `combat_solver/modpin.py::attest_lock_mods()` 逐个现算
    SHA-256，缺文件或读不到按 error 记账而不是算匹配（fail closed）。

---

## 测试与自检

**没有 pytest。** 仓库里没有 `pytest.ini` / `pyproject.toml` / `conftest.py`，也没有任何 `pytest.mark`；
官方 runner 是 stdlib `unittest`。

```powershell
# 本项目的双解释器 runner（推荐；见下一节的"当前未通过"）
powershell -ExecutionPolicy Bypass -File scripts\test.ps1
```

`scripts/test.ps1` 分三段，顺序不能换：

1. 轻量运行时解释器跑**纯契约**测试模块（不依赖 numpy/torch）。
2. `python scripts/verify_engine_citations.py`——引擎引用锚点门。它单独跑，因为"引用快照只会重算报告
   已经写明的行号"，指针可以滑到自己的主张之外而其余全绿。
3. 训练侧模块用 `STS2_TRAINING_PYTHON` 指向的解释器跑；未设置时回落到相邻模拟器的
   `.venv\Scripts\python.exe`；开跑前先探测 `gymnasium, numpy, torch, stable_baselines3, sb3_contrib`
   五个包，缺任一个就报明确错误（把失败留在环境边界，而不是让每个模块各自炸出 import 错误）。
   整个 venv 不存在时打印跳过说明。

单环境全量发现（把训练栈一起装进来才成立）：

```bash
python -m pip install -r requirements-test.txt
python -m unittest discover -s tests
```

原生模拟器的集成测试在相邻 checkout 或其 `out/Sts2Emulator.dll` 不可用时自动跳过。

自检脚本清单（`scripts/`，共 6 个 `verify_*`）：

| 脚本 | 用途 | 调用 |
| --- | --- | --- |
| `verify_engine_citations.py` | 战役报告里每条引擎引用的行号是否仍坐在它主张的代码上 | 无参数；非 0 即漂移 |
| `verify_campaign_fidelity_gates.py` | 用引擎自己的 C# 表 + 可复现 checkpoint 逐门（G1–G6）度量战役保真度；测不出来报 `not_measured` 且视为未通过 | `<训练解释器> scripts/verify_campaign_fidelity_gates.py --seeds 6 --out runtime/fidelity_gates.json` |
| `verify_full_run_contract.py` | 判定一条真机 trace 是否"契约完整的真实整局"，每项只从 trace 自身回答，无证据即失败 | `python scripts/verify_full_run_contract.py <trace.jsonl> …` |
| `verify_report_claims.py` | 把报告标题里的数字还原成具名计算并逐项重算，DRIFT = 报告与磁盘不一致 | `python scripts/verify_report_claims.py [--expect FILE] [--quiet]`（需模拟器 venv 解释器） |
| `verify_run_artifact_hashes.py` | 重算 run 产物记录的 checkpoint/seed/origin/warm-start 哈希，分 verified / mismatched / unresolvable | `python scripts/verify_run_artifact_hashes.py --root runtime --root runs [--strict]` |
| `verify_eval_guard.py` | 针对真实挂死种子验证步数上限守卫（诊断用，路径与种子硬编码） | `python scripts/verify_eval_guard.py` |

其余常用一次性自检：

```bash
python -m bridge.model_client --selftest                  # 模型 CLI 与 warm session
python scripts/run_solver_comparison.py --batch-id csb-001 --dry-run
python scripts/play.py --backend live --preflight-only
python -c "import py_compile,glob; [py_compile.compile(f,doraise=True) for f in glob.glob('bridge/*.py')]"
```

---

## 模拟器与训练层（可选）

三幕战役模拟器不是本仓库的一部分，而是相邻 checkout
[`slay-the-spire-2-emulator`](https://github.com/Zamiell/slay-the-spire-2-emulator)（NativeAOT +
Gymnasium）。约定路径是 `<repo>/../third_party/slay-the-spire-2-emulator-main`，配置里由
`[runtime] emulator_root` 指定；原生库产物为 `<emulator_root>/out/Sts2Emulator.dll`，其 SHA-256 会被
写进训练产物做 provenance。

```powershell
.\scripts\build_emulator.cmd     # 需要 .NET SDK 与 vcvars64.bat 提供的 VCToolsInstallDir
```

三条前提各自看起来像另一种故障：系统 `dotnet` 只有运行时、没有 SDK 时构建会失败；NativeAOT 链接步骤缺
`VCToolsInstallDir` 会让 `link.rsp` 全是空 `/LIBPATH:`，但报出来的是 VS 自己的 `'Analysis' 不是内部或
外部命令`；导出符号是在 `Sts2Emulator.csproj` 里用 `/EXPORT:` 手工挂的，**漏挂时版本门照样通过、`ctypes`
才报找不到符号**。

环境版本 `sts2sim-campaign-fidelity-v5` 的含义（`training/campaign_content.py`）：

- 六项硬保真门 G1–G6 全部关闭（`hard_gate_passed=true`，见
  `docs/evidence/campaign_fidelity_gates_20260923_v5.json`）：三幕各抽自己的遭遇池
  （Overgrowth → Hive → Glory）、进幕先站到**那一幕自己的先古之民**面前（第二幕只在 Pael / Tezcatara 之间
  抽——Orobas 需要 epoch 解锁，模拟器没有解锁进度，因此第二幕按 `partial` 声明）、每幕生成自己的地图深度
  与事件池、升级概率与奖励构成按幕/按房间类型走、终幕配对 boss 是"走到 boss 行之后那一行"、并且删掉了
  真实游戏根本不发的 boss 遗物奖励。
- v5 相对 v4 **没有新增能力**：它删掉的是引擎里那套"保留 trace 回放"覆盖子系统——过去会拿实时状态和某一条
  录制对局比字节，然后改写战斗结果、奖励、路线和事件。删除它移除了一个混淆因素，因此 v4 的强度数字仍然可用。
- 幕区间两边都是推出来的并且相等：真机 act 1/2/3 的 boss 节点在 floor **17 / 33 / 48**，第三幕要求清掉
  **两个** boss（配对 boss 在 49）；模拟器 `ACT_BANDS = (1-17, 18-33, 34-50)`。
- verdict 仍是 `approximate`，tier 停在 `content_verified` 之下：没有 Ascension 模型、Unknown 进场不再
  抽签、没有 TheArchitect 结局、缺两个 weak 遭遇变体、短地图里放置可以少于队列。
- `sts2sim-campaign-approx-v1` / `-fidelity-v2/v3/v4/v5` 的名字都已被摘要冻结：带某个名字的产物**永远**
  表示它当时的内容。不同环境的产物不得平均、不得互判、不得随意续档（每版的 `checkpoint_rule` 写明了
  哪一次版本 bump 续档是可辩护的）。

正式训练必须经监督器启动并固定游戏 build：

```bash
python -m training.supervisor --run-dir runs --game-build BUILD_ID \
  --character IRONCLAD --ascension 10 -- <训练命令及参数>
```

三阶段 MaskablePPO 基线可先做只读检查：

```bash
python -m training.curriculum --config config/training.toml --dry-run
```

模拟器评测与真实游戏评测**必须分开保存**。`full_run` 是显式 opt-in 的实验阶段，其结果永远不会被标记为
真实 A10 成绩；三幕战役自 2026-09-20 起需显式开启，关闭时与历史单幕人群逐种子一致。

Trace 数据契约校验：

```bash
python -m training.validate_traces data/local/train.jsonl data/local/validation.jsonl data/local/test.jsonl
```

---

## 当前未通过的自检（不藏）

诚实报告，写给任何一个想拿这个数字说话的人：

- **`scripts/verify_engine_citations.py` 现在是红的**（退出码 1）。它报的是
  `docs/ACT1_CAMPAIGN_2026-09-19.md` 与锚点表 `tests/test_emulator_provenance.py::CITATION_ANCHORS`
  之间的漂移，两种形态都有：锚点表里有而报告不再携带的行号，以及行号读到的代码与散文主张对不上的条目。
  连带 `python -m unittest discover -s tests` 会在 `test_emulator_provenance.CitationAnchorTests` 上出现
  同一批失败（`scripts/test.ps1` 的第二个门因此也会抛）。**修法不是把数字对齐**：这类漂移正是"保真 v2
  曾拿着错误代码报数"的成因，必须逐条读散文、把报告与表**一起**移动。本 README 不做这个决定。
- 在只装了 `requirements.txt` 的裸环境里跑全量发现，会额外出现一批 import 错误——那是 gymnasium/torch
  缺失，不是缺陷；训练层要么用 `scripts/test.ps1` 的分段 runner，要么先装 `requirements-test.txt`。
- 历史数字的可复核边界：`docs/ACCEPTANCE.md` 记录的 Act 1 bulk 结论是
  `1/500 = 0.2%`、平均最终楼层 `7.67`、checkpoint 未晋级；它的原始产物在 `runs/`（未随仓库发布），
  所以这条只能按文档引用，不能在 clone 上重跑。

## 已知未完成

- 真实第三幕的**第二个 boss** 没有任何一次到达记录，因此也没有一次真实胜利。
- `is_victory` 桥接已安装并实测，但**验到的是败局分支**；"赢"这个分支仍未跑过。
- 模拟器的 Hive / Glory 池表内容已在引擎里（`CombatFactory.cs` 的一些下标无人引用），但尚未接线。
- 事件 / Neow 只有可见候选枚举与保守兜底，没有经过验证的效用评分策略。
- 三幕引用的部分行号仍需按新构建重钉。

## 数据、许可与再分发

- 不提交、不再分发游戏数据。`data/cards.json`、`data/relics.json`、`data/version.json` 是
  `python data/fetch_data.py` 从公开数据集抓下来的产物，已在 `.gitignore` 里；`data/*.json` 中随仓库发布的
  只有本仓库自己写的小 fixture（种子清单、starter deck 近似值、样例状态、trace schema）。
- 训练产物、run 输出、`runtime/`、隔间的第三方 mod 二进制（`disabled_mods/`）都不进版本库。
- Slay the Spire 2 及其数据、美术归 Mega Crit。本仓库的 advisor 代码是 MIT，见 [LICENSE](LICENSE)；
  上游组件与各自许可证见 [UPSTREAM.md](UPSTREAM.md)，第三方数据集保持其原有条款。

## 文档索引

- [架构与训练课程](docs/ARCHITECTURE.md)
- [现有方案调研](docs/RESEARCH.md)
- [A10 50% 验收协议](docs/ACCEPTANCE.md)
- [阶段式训练操作说明](docs/TRAINING.md)
- [本地决策 Trace 数据契约](docs/TRACE_DATA.md)
- [局外候选与 wire action 契约](docs/LIVE_CANDIDATE_CODEC.md)
- [Combat Solver 分层与运行审计](docs/COMBAT_SOLVER.md)
- [真机版本锁定、trace 录制与动作门禁](docs/LIVE_BRIDGE.md)
- [当前真实状态](docs/STATUS.md)
- [逐幕覆盖审计：真机 vs 模拟器，2026-09-21](docs/ACT_COVERAGE_AUDIT_2026-09-21.md)
- [模拟器幕内容保真度](docs/SIMULATOR_ACT_FIDELITY_2026-09-21.md)
- [Act 1 战役报告（含撤回账目）](docs/ACT1_CAMPAIGN_2026-09-19.md)
- [批次生命周期修复记录](docs/BATCH_LIFECYCLE_FIX_2026-09-07.md)
- [三幕模拟器扩展与全自动打牌器设计](docs/superpowers/specs/2026-09-20-three-act-emulator-and-auto-player-design.md)
- [贡献指南](CONTRIBUTING.md)

## 关于本文档的一次重写

本 README 于 2026-10-06 重写为"公开仓库的前门 + 运行说明"。旧版把安装与运行信息埋在结论之间、并按本机
专有路径（`.tools\python\cpython-3.12…`）给命令，clone 之后无法照抄执行；两处已核对出的失真也已改正：
只读建议器的落盘文件是 `runtime/latest_advice.txt`（旧文写作 `runtime/advice.txt`），战役环境版本自
2026-09-23 起是 `sts2sim-campaign-fidelity-v5`（旧文停在 v4）。

旧审计文档里形如 `README.md:63-65`、`README.md:208` 的**行号**引用指的是重写前的版本；它们把当时那句原话
抄在了自己正文里，所以证据本身仍然自足，只是行号指针会指到别处。被撤回的那句结论（"所以能不能走完三幕是
策略强度问题，不再是表达能力问题"）依然以撤回形式保留在
[逐幕覆盖审计](docs/ACT_COVERAGE_AUDIT_2026-09-21.md) 与 [当前真实状态](docs/STATUS.md) 中——那句撤回是
本项目最重要的一次自我纠错，不要因为本文档不再逐字复述它就当作没发生过。

## License

Advisor code is MIT licensed; see [LICENSE](LICENSE). Slay the Spire 2 and its data/assets belong to
Mega Crit. Third-party code and datasets keep their own licenses.
