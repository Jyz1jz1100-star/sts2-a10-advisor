# 本地决策 Trace 数据契约

当前默认目标版本是 2026-08-31 时的 `public-beta-v0.111.0`。每次游戏更新后，
应先重新完成状态协议与模拟器一致性测试，再通过 `--expected-build` 显式切换版本。
不同 build 的记录不得静默混合。

## 每行必须包含

JSONL 的每个非空行是一条玩家可见决策记录，必须包含：

- `trace_version`: 当前固定为 `1`；
- `run_id`, `decision_id`, `step`: 可追踪且稳定的局、决策和顺序标识；
- `split`: `train`、`validation` 或 `test`；
- `build`: 精确游戏分支和版本，当前为 `public-beta-v0.111.0`；
- `seed`: 游戏种子，推荐保留游戏显示的原始字符串；
- `character`: 当前 A10 语料必须为 `IRONCLAD`；
- `ascension`: 当前语料必须为 `10`；
- `save_load_used`: 是否使用过 SL；正式 NOSL 语料必须为 `false`；
- `visible_state`: 决策时玩家能够看到的完整状态，不得加入未来抽牌、隐藏 RNG 或
  事后胜负等特权信息；
- `legal_actions`: 当时所有合法动作，每项至少有稳定 `action_id` 和
  `action_type`；
- `chosen_action`: 实际选择，ID 必须出现在 `legal_actions` 且类型一致；
- `result`: 至少包含 `status` 和 `observed`。状态可为 `applied`、`rejected`、
  `terminal` 或 `error`。训练标签通常只取已观察的 `applied`/`terminal` 记录；
  其余状态保留用于桥接与人工依从性审计。

建议在 `result` 中额外保存下一可见状态哈希、HP/金币/牌组变化、战斗终局和奖励，
但不得把这些字段回填到同一决策的 `visible_state`。

结构化约束另见 [`data/trace_record.schema.json`](../data/trace_record.schema.json)。
Python 校验器还会执行 JSON Schema 不表达的约束：当前 build、战士 A10、NOSL、
chosen/legal 一致性、合法动作 ID 唯一性及跨数据集 seed 泄漏。

## 示例

```json
{"trace_version":1,"run_id":"run-001","decision_id":"run-001:42","step":42,"split":"train","build":"public-beta-v0.111.0","seed":"AB12CD34","character":"IRONCLAD","ascension":10,"save_load_used":false,"visible_state":{"state_type":"combat","round":3,"player":{"hp":61,"energy":3},"enemies":[{"id":"CULTIST","hp":31,"intent":"ATTACK"}],"hand":[{"id":"BASH","instance_id":"c-17"}]},"legal_actions":[{"action_id":"play:c-17:CULTIST","action_type":"play_card"},{"action_id":"end_turn","action_type":"end_turn"}],"chosen_action":{"action_id":"play:c-17:CULTIST","action_type":"play_card"},"result":{"status":"applied","observed":true,"hp_delta":0}}
```

## 校验命令

```powershell
& .\.tools\python\cpython-3.12-windows-x86_64-none\python.exe `
  -m training.validate_traces data\local\train.jsonl data\local\test.jsonl
```

校验通过退出码为 `0`，契约或 seed 泄漏失败为 `1`，文件不存在为 `2`。使用
`--json` 可生成机器可读报告。只有在审计历史数据时才使用 `--allow-save-load`；
该选项产生的数据不能进入 NOSL 验收集。

seed 泄漏检查采用保守规则：种子会去除首尾空白并转为大写，同一个归一化种子
不得同时出现在 train、validation 或 test 的任意两个集合中，即使 build 或角色不同。

## 原始录制流 → 契约记录转换器

`bridge/trace_controller.py` 录制的 JSONL 是未裁剪的原始事件流。
`bridge/convert_traces.py` 负责把每个可配对的策略性 POST 转成契约记录：

```powershell
& .\.tools\python\cpython-3.12-windows-x86_64-none\python.exe `
  -m bridge.convert_traces runs\live_traces `
  --split train --out data\local\train.jsonl `
  --report runs\live_traces\conversion-report-train.json
```

转换器的硬性规则：

- `visible_state` 经过确定性可见信息过滤：有序 `draw_pile` 等牌堆被替换为
  `count + sorted composition`，任何顺序信息不得进入训练输入；
- `legal_actions` 从决策时刻的原始状态枚举，动作 id 与 STS2MCP v0.4.0 的
  wire payload 一一对应（参数名按 `McpMod.Actions.cs` 源码锁定：
  `play_card.card_index`、`select_card.index` 等）；
- `result` 由 POST 响应 + 第一个 decision id 变化的后续状态归因；
  `game_over` 记为 `terminal`，HTTP/桥错误记为 `error` 且 `observed=false`；
- 录制会话内 `observed_mods` 与版本锁白名单不一致 → 整场判为污染并排除
  （`--allow-contaminated` 仅供人工审计）；
- 状态里看不到 `character=IRONCLAD` 且 `ascension=10` 的记录一律排除；
- 菜单导航与 `set_ascension` 属于开局的引导动作，默认跳过（`--include-menu`
  保留）；
- 正规语料的 seed 必须来自 compendium `run_identity` 事件（由
  `start-ironclad-a10` 自动写入）。没有真实 seed 的会话会退化为
  `RUNID:<run_id>` 代理 seed，转换器默认**拒绝**这种输出用于正式 split，
  只有显式 `--allow-seed-less`（仅限链路 smoke）才放行。

转换后的文件必须再通过上一节的 `validate_traces` 才算入库。

## 屏状态 Schema Fixtures

`tests/fixtures/screens/*.json` 锁定每个可达界面的 v0.4.0 协议形状：
其中 `monster`、`map`、`event`、`card_select`、`menu_main` 来自版本锁内的
真机捕获（`runs/live_traces/`），其余为按上游 `docs/raw-full.md` 文档合成的
占位（`"captured": false`），等待真机整局录制后替换。
`tests/test_screen_fixtures.py` 对每个 fixture 跑合法动作枚举与可见信息过滤，
锁定动作 id / 动作类型集合。游戏补丁后用新录制的原始状态与这些 fixture 做
diff，即可发现桥接协议漂移。

重新生成：

```powershell
& .\.tools\python\cpython-3.12-windows-x86_64-none\python.exe `
  scripts\make_screen_fixtures.py
```

## 离线行为克隆基线

校验通过后，可运行独立的 CPU 行为克隆基线：

```powershell
& .\.tools\python\cpython-3.12-windows-x86_64-none\python.exe `
  -m training.behavior_clone `
  --trace data\local\train.jsonl `
  --output models\bc_public_beta_v01110.pt `
  --device cpu --epochs 5
```

模型只读取 `visible_state`，使用共享状态编码器，并为 combat、map、event、shop、
rest site 等 phase 保留独立动作打分头。动作不是全局分类标签：每次只对该记录的
`legal_actions` 打分，交叉熵目标是其中的 `chosen_action`，因此训练和推理均执行
严格合法动作 mask。未知 phase 使用 `other` 头。

Checkpoint 旁会生成 `.metadata.json`，记录精确 build、角色、A10/NOSL、随机种子、
模型结构、epoch loss、PyTorch 版本、每个输入文件和聚合数据集 SHA-256，以及
checkpoint 自身 SHA-256。加载时默认校验 checkpoint 哈希。

这是可复现的离线训练骨架，不是已达成 A10 胜率的成品策略。哈希特征编码器适合
早期数据链路和基线；正式模型应在字段稳定后换成实体感知的集合/序列编码器，并保留
相同的可见信息边界、phase 分头和 legal-action mask 契约。
