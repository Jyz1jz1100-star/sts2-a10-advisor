# Contributing to STS2 A10 Advisor

这是一个**同时包含只读观测与真机执行**的仓库。它早先是 [Spire Oracle](https://github.com/bnipper-creator/sts2-advisor)
的一份源码副本（那时确实只读），后来长出了驾驶轨——所以如果你在这里看到的旧描述说"本项目纯 observe-only"，
那是历史状态，不是当前契约。下面第一节就是这个区分本身。

对 STS2 模组 / STS2MCP / Combat Solver 的**实测观察**写在 [README.md](README.md)，本项目自身的推进状态写在
[docs/STATUS.md](docs/STATUS.md)。这份文件只讲贡献规则。

---

## 1. 先认清你在哪一轨

| | 只读轨 | 驾驶轨 |
| --- | --- | --- |
| 入口 | `advisor_core/`、`bridge/client.py`、`scripts/run_solver_comparison.py` | `bridge/autoplay.py`、`bridge/trace_controller.py`、`scripts/supervise_solver_batch.py`、`scripts/play.py` |
| 对游戏 | 只有 `GET` | 有 `POST /api/v1/singleplayer`，还有一个会点击 UI 的 watchdog |
| 授权 | 无需（结构上发不出动作） | 必须显式 `--allow-actions`；`--dry-run` 会压制它 |
| 允许改动 | 任何能让读数更准的事 | 见 §3 的硬不变量 |

`bridge/client.py` 的**文件头声明它按构造只读**，而且这个性质确实是构造性的：里面只有 `_get` /
`get_state` / `get_compendium` / `is_up`，没有任何 POST 方法（`bridge/client.py:1-4,35-51,104-121`）。
**把 POST 加进这个文件仍然是禁止的**——驾驶轨有它自己的控制器（`STS2MCPController`），要发动作就走那里，
带着闸门一起走。

驾驶轨存在的前提是"闸门比人可靠"。它同时服务两种目的，闸门严格度不同，别把它们混成一个门：

- **验收轨**（`--track acceptance`）打真实整局：求解器的自动更新漂移**只记录、不否决**（写进
  `acceptance_blockers`）。
- **比较轨**（`--track comparison`）的自变量就是求解器本身，所以**必须钉死**全部模组字节
  （`--mod-gate attest` vs `strict`，见 `scripts/supervise_solver_batch.py:362-363`）。

曾经两者共用一个 `required` 哈希门，结果是验收整局被比较实验的门禁绑架，与文档承诺相反（2026-09-21 纠正）。
**新加门禁时先问它服务哪种目的。**

---

## 2. 上手

```bash
python -m pip install -r requirements.txt     # 轻量运行时：requests + PyYAML
python data/fetch_data.py                     # 把卡/遗物数据集抓到本地（gitignored）
python -m bridge.model_client --selftest       # 需要 `claude` CLI，只测模型链路
python -m unittest discover -s tests           # 纯契约测试，离线，不碰游戏
```

**没有 pytest**：仓库里没有 `pytest.ini` / `pyproject.toml` / `conftest.py`，也没有 `pytest.mark`，官方
runner 是 stdlib `unittest`。写新测试请沿用 `unittest.TestCase`。

训练层（NumPy / PyTorch / Gymnasium）不属于运行时环境，它是可选的：装
`requirements-training.txt` 到另一个解释器（`sts2_gym` 是相邻 emulator checkout 的本地源码，不是 PyPI
包），然后把 `STS2_TRAINING_PYTHON` 指过去，用 `scripts/test.ps1` 跑双解释器分段套件。它会在跑训练测试**之前**
探测那五个包——这条探测是有意的：没有它，缺一个包会伪装成一串各模块自己的 import 错误。

没有游戏也能验链路：`config.yaml` 里设 `dev.use_sample_state: true`，桥会读 `data/sample_state.json`。

---

## 3. 硬不变量（改代码之前）

这些不是风格偏好，每一条都对应一次真实故障。

**执行权**

- **一局只有一个动作拥有者。** `--execution-owner ∈ {http_route_executor, combat_solver_full_auto,
  manual_player}`，并且与 `--out-of-combat-only` 互为条件——两个方向各有 `ValueError`
  （`bridge/autoplay.py:2104-2117`）。full-auto watchdog 只能记成 watchdog，**永远不能**是第二拥有者。
- **只发 loopback。** 控制器构造时校验 host ∈ {`127.0.0.1`, `localhost`, `::1`} 且协议为 `http`
  （`bridge/trace_controller.py:414-420`）。不要加"远程主机"选项。
- **POST 前必须重读状态并比对身份**，不符就抛 stale-decision（`bridge/trace_controller.py:495-499`）。
  身份目前只能是整份状态的规范化 SHA-256（`bridge/trace_controller.py:57-75`），因为 mod 不暴露
  decision id。

**红线动作（结构性，不是配置）**

- **不存在 `abandon_run` 这条代码路径。** 菜单分支只会点 `main_menu`、`back`（且仅当菜单自己列出）、
  `continue`（先做存档身份校验）。存档身份不匹配是**硬停**，不是重试，更不是"放弃这局"
  （`bridge/autoplay.py:1628,1642-1646,1652-1682,1836-1839`）。
- **开局路径只点** `singleplayer` / `standard` / `IRONCLAD` / `set_ascension` / `confirm`；不受支持的
  菜单屏直接报错停下（`bridge/trace_controller.py:701-778`）。
- 硬上限 `--max-runs` / `--max-actions` 的存在是为了让一个 bug 不可能变成整晚连打；不要放宽默认值。

**读数**

- **每个屏的 JSON 形状只在一处读**：`bridge/screens.py::extract_options`
  （`bridge/screens.py:67,74` 的注释就是这条）。mod 补丁改了字段，在那里修，并同步
  `data/sample_state.json`。
- **候选 codec 只枚举合法动作，不做策略、不发明动作**，解释层不许重排候选
  （`docs/LIVE_CANDIDATE_CODEC.md`）。缺字段一律 fail closed：`state_type` 缺失拒绝推断、布尔缺失报错、
  重复 index 判歧义（`advisor_core/live_candidate_codec.py:446-455,599,614`）。
- **读数来源要一起记。** 终止结果必须带 `outcome_source`（结构位 vs 文案猜测，`bridge/outcome.py:70-84`），
  否则两种证据在数据里长得一样。
- **不同的拒绝类别不能相加。** 菜单类拒绝（"点了一个菜单没提供的选项"）与局中拒绝（"Rewards screen is
  not open"）是不同的失效模式，历史上把两者加过一次，导致一个红项看起来像另一件事
  （`docs/STATUS.md:3124,3649`）。

**样本与可比性**

- **种子分区必须唯一且互不重叠**（`training/seeds.py:48-54`），train / validation / test 之间不许泄漏；
  固定种子只在 `--seed-mode fixed` + `--seed-file` 下成立，观察性样本不许冒充固定种子验收
  （`scripts/supervise_solver_batch.py:1036-1050` 的 blocker 名就是为这件事准备的）。
- **`assisted` 与 `no-sl` 是两个 cohort**，模拟器与真机是两个人群，不同 `environment_version` 的产物**永远
  不得平均或互判**。环境语义变了就要 bump 版本，并且旧版本的声明按**字面摘要**冻结
  （`training/campaign_content.py:589-606` 解释了为什么不能重算——重算会让一次内容编辑"验证"自己）。

**版本与字节**

- 新增会改变可复现性的东西，就写进版本锁（schema_version 化，`bridge/trace_controller.py:266-377`）：
  实机 build、桥 dll、manifest、允许的 mod id，缺一或多一都要报错而不是"看起来能用"。
- **字节自证要现算，不要信 mtime**（`combat_solver/modpin.py:113-148`）：缺文件或读不到记为 error，
  **不算匹配**。
- 换 mod 文件要求客户端进程没在跑；工具会拒绝，不要绕过。

---

## 4. 改战役报告 / 引擎引用之前

`docs/ACT1_CAMPAIGN_2026-09-19.md` 里每个 `Foo.cs:123-125` 这样的行号都由锚点表
`tests/test_emulator_provenance.py::CITATION_ANCHORS` 守，门禁是 `scripts/verify_engine_citations.py`
（`scripts/test.ps1` 的第二个门）。

- **这条门禁不可省略**：普通的引用快照只会重算"报告已经写明的行号"，所以指针可以滑出自己的主张之外而
  其余全绿。
- **漂移出现时，不要把两边的数字对齐。** 必须逐条读那段散文，确认代码到底还在不在那里，然后**同时**移动
  报告与表。历史上有一次就是"重新钉号"把保真 v2 的数字钉到了错误的代码上。
- 截至本文更新时这条门禁**是红的**。它是待处置项，不是你的新 bug；如果你没打算处理它，别让它挡住你判断
  自己的改动是否引入了回归——请先记录改动前后的差值。

---

## 5. 报告问题需要什么

一条 issue 至少要有：当时在哪一屏、出现了什么建议（或什么都没出现）、`runtime/advisor.log` 的片段，以及
如果是数据形状问题，对应的 `/api/v1/singleplayer` JSON。涉及批次的话再加上
`runs/solver_supervisor/<batch-id>/status.json` 与 `acceptance_blockers` 列表。

两个会让人误判归因的点：

- 本次会话的日志文件**就叫 `godot.log`**，要等客户端下次启动才改名成带时间戳的文件；按文件名归属批次不可靠
  （`scripts/supervise_solver_batch.py:714-727`）。
- 桥不响应 ≠ 游戏死了：求解器深搜 / 预载 / 存档都会占住 mod 的 HTTP 线程，实测最长 17.9 秒自愈。

---

## 6. 提交前自查

```bash
python -m unittest discover -s tests
python scripts/verify_engine_citations.py
python -c "import py_compile,glob; [py_compile.compile(f,doraise=True) for f in glob.glob('bridge/*.py')]"
git status --porcelain            # 确认没有把 runtime/、runs/、data/cards.json 带进来
```

**不要提交游戏数据**：`data/cards.json` / `relics.json` / `version.json` 与 `runtime/`、`runs/`、
`checkpoints/`、`models/`、`disabled_mods/` 都已 gitignore。Mega Crit 的内容不能随仓库再分发；本项目的
代码是 MIT（见 [LICENSE](LICENSE)），上游组件与第三方数据集保持各自条款（见 [UPSTREAM.md](UPSTREAM.md)）。

改动请保持聚焦。同时触碰"策略"与"契约"的 PR 请拆开——契约部分需要的是逐字段的出处，策略部分需要的是样本
与区间，两者的验收方式完全不同。
