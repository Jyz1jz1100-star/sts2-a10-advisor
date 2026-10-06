# STS2 A10 Advisor — 写给 STS2 模组开发者的观察笔记

这是一个《杀戮尖塔2》(Slay the Spire 2) 的局外自动决策器：它读 STS2MCP 暴露的实时状态，替玩家做
地图路线 / 卡牌奖励 / 商店 / 火堆 / 事件 / 先古之民的选择，把战斗整屏交还给局内的 Combat Solver mod。

本仓库真正的产品不是那个决策器，而是**为了让它能跑而必须搞清楚的那些事实**——这个游戏的 mod API 在
真实载荷里长什么样、动作名的参数为什么四处不一致、哪些屏的候选顺序会重新编号、mod 怎么被静默自更新、
客户端会在哪个房间被自己的特效循环打死。这些结论大多只能靠把整局打完再逐帧读回来才能获得，而它们对
**任何一个写 STS2 mod、STS2MCP、Combat Solver、模拟器或分析工具的人**都有直接价值。

所以这份 README 不汇报进度。它是一份观察笔记 + 一个可运行的参考实现。

```text
读这部分 → §1 跑起来看   §2 STS2MCP HTTP 面   §3 动作契约   §4 屏与状态的坑
           §5 胜负判定    §6 固定种子          §7 mod 身份   §8 观测无接口的 mod
           §9 客户端卡死  §10 游戏内容结构     §11 可搬走的工程做法   §12 请求清单
```

---

## 1. 三十秒跑起来看

只需要 Python 3.11+ 和两个 pip 包（`requests`、`PyYAML`；overlay 另需 stdlib `tkinter`）：

```bash
python -m pip install -r requirements.txt
python -m advisor_core.live --once --output out.txt   # 单次只读，一个 POST 都不发
```

没有游戏也能验完整链路——把 `config.yaml` 的 `dev.use_sample_state` 设成 `true`，桥会读
`data/sample_state.json` 而不是真机：

```bash
python -m bridge.main              # 轮询 + 屏分类 + grounding + 写 runtime/latest_advice.txt
python overlay/overlay.py          # 半透明窗口
python -m unittest discover -s tests    # 纯契约测试（离线，不碰游戏）
```

**安全边界（结构性，不是配置项）**：只读链路的 `bridge/client.py` 在构造上就没有 POST 方法；驾驶链路
必须显式 `--allow-actions`，否则只会打印 `would_post` 预览；控制器强制 base URL 为 loopback
（`127.0.0.1` / `localhost` / `::1`）否则直接 `ValueError`；每个 POST 前重读状态并比对身份，不符就抛
stale-decision。默认端口只有两个：STS2MCP 的 `15526`，以及自启动单实例锁用的 `15580`。

---

## 2. STS2MCP 的 HTTP 面：实测和文档不一样

我们用到的端点只有四个，全部围绕单人对局：

| 用途 | 请求 |
| --- | --- |
| 健康 | `GET /` → `Hello from STS2 MCP v0.4.0`（版本判定靠这句里的子串，没有结构化字段） |
| 状态 | `GET /api/v1/singleplayer?format=json`（`format` 要自己按 `?`/`&` 拼） |
| 图鉴 | `GET /api/v1/compendium` |
| 动作 | `POST /api/v1/singleplayer` |

出处：`bridge/client.py:104-113`、`bridge/trace_controller.py:329-335,463-509`。

**真机载荷与上游文档的偏差**（这一节是本文档对 mod 作者最有用的部分）：

- **`run` 里永远没有 `seed`。** 277,993 帧真机状态里 `run.seed` 出现 **0 次**；种子的唯一权威来源是
  compendium 的 `current_run.seed`（它读的是 `current_run.save`）。`run` 实际只带 `act` / `floor` /
  `ascension`（`data/sample_state.json:2`、`docs/LIVE_BRIDGE.md:52`、`bridge/client.py:53-102`）。
  任何依赖 `run.seed` 的设计都得改写。
- **`current_run` 是 active-only。** 主菜单上可以同时出现"启用的 `continue`"和 `current_run: null`，
  这不是 schema bug，别把它当路径错误去"修"（`docs/LIVE_BRIDGE.md:59-66`）。
- **`options` 有两种形状。** 主菜单是字符串数组，子菜单是 `{name, enabled}` 对象。直接 `set(options)`
  会抛 unhashable（`bridge/trace_controller.py:543-550`）。
- **文档有、真机 0 帧：** `state_type: relic_select`（`bridge/screens.py:8-10` 是从上游 `raw-full.md`
  抄来的）。反过来真机给的休息项 id 是 `HEAL` / `SMITH`，文档写的是 `rest/smith`。
- **三个字段是本项目自己补进 mod 的，上游没有**：`rest_site.can_choose`、`potions[].usage`
  （`CombatOnly` / `AnyTime` …）、`game_over.is_victory`（见 §5）。清单在
  `config/live_version.lock.json:32-36`。
- **缺字段一律 fail closed，别猜。** `state_type` 缺失时拒绝推断、布尔缺失直接报错、重复 index 判歧义
  （`advisor_core/live_candidate_codec.py:446-455,599,614`）。这条纪律来自一个真实教训：把"零项"当
  "第 0 项"会得到一句毫无意义的 `No event options available`（§4）。

---

## 3. 动作契约：能发什么，参数名为什么四处不一致

内部候选名 → mod dispatch 名的映射集中在 `bridge/autoplay.py:8-12,555-584`，候选到 wire 的对照表在
`docs/LIVE_CANDIDATE_CODEC.md:33-42`。实测可用的动作集（含参数名，**照抄自代码，不要四舍五入**）：

| 动作 | 参数 | 备注 |
| --- | --- | --- |
| `choose_map_node` | `index` | 地图选点 |
| `select_card_reward` | **`card_index`** | 参数名和别处不统一 |
| `claim_reward` | `index` | |
| `select_relic` / `claim_treasure_relic` | `index` | |
| `select_card` + `confirm_selection` | `index` | `select_card` 是**开关(toggle)**，载荷里没有 per-card selected 标志 |
| `select_bundle` + `confirm_bundle_selection` | `index` | |
| `choose_event_option` | `index` | 事件里的 Proceed 也走它 |
| `rest` / `smith` | — | 真机 id 为大写语义名 |
| `menu_select` | **`option`** | `seed` 可选，且必须原串传入、不许转 int |
| `set_ascension` | **`level`** | |
| `use_potion` / `discard_potion` | **`slot`** | |
| `advance_dialogue` | — | |
| `crystal_sphere_click_cell` / `crystal_sphere_proceed` | `x`,`y` / — | Neow 的水晶球 |

出处：`bridge/autoplay.py:559-584,875-1202`、`bridge/trace_controller.py:573,729`。

给 mod 作者的两句话：**参数命名请统一**（`index` 一个词就够了），**并暴露 decision id**（见 §4 第一
条——我们目前只能用整份状态的规范化 SHA-256 当身份，`bridge/trace_controller.py:57-75`）。

还有一件事值得写进 mod 文档：**Neow 不是一个独立屏**，它是 `state_type: "event"` +
`event_id: "NEOW"` + `is_ancient`（`docs/LIVE_CANDIDATE_CODEC.md:23-27,44-46`）。先古之民同理，靠
`is_ancient` 而不是靠屏类型识别。

---

## 4. 屏与状态的坑：只有把整局打完才会遇到

- **领奖后 mod 会删掉已领按钮并给剩余项重新编号**，index 必须逐帧重读，不能缓存
  （`bridge/autoplay.py:1019-1031`）。
- **事件的 `options` 下一帧才渲染**：同一局里 seq37 是零项、seq38 才是两项。轮询到零项时的正确反应是
  **等**，不是报错（`docs/ACT_COVERAGE_AUDIT_2026-09-21.md:452`）。
- **旅行动画期间客户端仍然报同一张 map**，而 mod 已经 ack 了你的选择；此时再发一次 `choose_map_node`
  就是"状态不再提供的移动"（`bridge/autoplay.py:764-767`）。判定"这一屏还在不在"不能靠 map 载荷本身。
- **`can_confirm` 在附魔(选牌)网格上会提前亮**——它接的是 `PreviewSelection`。只看它会连发四次空提交。
  卡牌网格的屏身份只能用 `(screen_type, act, floor, len(cards))`（`bridge/autoplay.py:1052-1061,1084`）。
- **`state_type: "unknown"` 有两种含义**：只带 `run`/`player` 的是两个房间之间的轮询缝（应 hold）；
  带任何容器的是**未建模内容**（应记账）。把它们混为一谈会让每个房间切换都变成"缺处理器"
  （`bridge/autoplay.py:680-690`）。
- **无规则帧的计数必须按同一 decision id 的连续帧**。按批次生命周期统计会把动画中的屏误判成没有处理器
  （`docs/ACT_COVERAGE_AUDIT_2026-09-21.md:453`）。

---

## 5. 胜负判定：一个只需要一位布尔的缺口

原版 mod 把胜利和失败压成**同一份载荷**：

```json
{"message": "Run ended.", "options": ["main_menu"]}
```

于是任何消费侧都不可能读出"胜"——这不是策略问题，是接口缺失
（`docs/ACT_COVERAGE_AUDIT_2026-09-21.md:83-99`、`bridge/outcome.py:37-42`）。

**修法只需要一位布尔。** 我们的候选桥接在游戏自己的 `game_over` 分支发布
`is_victory = CurrentRoom.IsVictoryRoom`，并且**房间已卸载时给 `null`**（未知 ≠ 败）。消费侧同时交出
读数来源标签 `bridge_is_victory_flag` / `game_over_message_wording` / `no_victory_signal`，让"我们是从
结构读到的"和"我们从文案猜的"永远可区分（`bridge/outcome.py:70-84`、
`docs/evidence/victory_observability_20260921.json`）。

顺带三条会被误用的游戏事实：

- 胜与负共用同一个 `NGameOverScreen`；胜利的定义是终幕之后进入 `TheArchitect`。
- **`TriggerVictory()` 先于队伍被杀光**，所以"还活着"绝不是通关证据。
- 靠文案判胜有两个具体坑：`incomplete` 里含 `complete`，而整词匹配又会漏掉 `Victory`，只能词首锚定
  （`bridge/outcome.py:10-20`、`docs/ACT_COVERAGE_AUDIT_2026-09-21.md:50-56,179-181`）。

装上带 `is_victory` 的桥之后，真机终止屏的结构读数就已经能区分两个分支了；`docs/evidence/`
里的 `live_first_victory_20260921.json` 记录了带两位终幕 boss 清除与 `outcome_source` 标签的首个胜利
终端。也就是说：**这一位布尔是 mod 生态缺的最便宜的一块拼图。**

---

## 6. 固定种子开局：能用、不能用、为什么

- **能用的路径**：`menu_select` 携带 `seed`（在 confirm 那一步），候选走公开的
  `NCharacterSelectScreen.BeginRun`（`bridge/trace_controller.py:573-578`）。
- **不能用的路径**：`StartRunLobby.SetSeed` —— 标准模式会抛
  `Seed should not be changed in standard mode!`（`docs/FIXED_SEED_FEASIBILITY.md:13-28`）。
- **已经有 run 在活动时会直接拒绝注入**（`bridge/trace_controller.py:686-690`）。
- **同一个种子在不同账号上不是同一张图**：幕列表由解锁态决定
  （`GetRandomList(rng, GetUnlockState(), …)`，Underdocks 只在 epoch 揭示后才替换第一幕），
  所以跨机器的"固定种子复现"必须把账号解锁态也钉住（`docs/FIXED_SEED_FEASIBILITY.md:17-19`）。
- **字母数字种子的规范化只有四步**：去空白、转大写、`O→0`、`I→1`
  （`bridge/trace_controller.py:78-90`）。它不会进入整数分区，所以只能算观察性样本，回读不匹配就要停
  （`docs/COMBAT_SOLVER.md:184-190`）。

---

## 7. mod 身份、静默自更新，以及"怎么让结果可复现"

- **游戏读 mod 的两个根**：`…/Slay the Spire 2/mods` 与 `steamapps/workshop/content/2868840`。
  枚举方式是对根下所有 `*.json` manifest 取 `id`，然后和白名单双向比较——**多装一个就判污染**
  （"would contaminate evaluation"），**少一个也报错**（`bridge/trace_controller.py:352-377`、
  `config/live_version.lock.json:45-54`）。
- **manifest 字段**（观测到的 mod 格式，代码里被消费的是 `id`）：`id` / `name` / `author` / `version` /
  `has_pck` / `has_dll` / `affects_gameplay`。纯装饰 mod 靠 `affects_gameplay: false` 被比较轨容忍，
  验收轨仍然排除它——这条判定在锁里有逐模组注释（`config/combat_solver.lock.json:108-118`），
  `id` 的消费点在 `bridge/trace_controller.py:352-377`。
- **Workshop 会静默自更新，且无法非侵入地阻止**：`appworkshop_2868840.acf` 里没有按项的 autoupdate
  开关（`docs/COMBAT_SOLVER_0_41_DRIFT_2026-09-19.md:111-117`）。实际后果很具体：7 天内求解器走了 8 个
  版本，而锁还钉在旧版本上。
- **游戏构建的可用标识来自两处，且 app id 不在其中之一**：`version` / `commit` /
  `main_assembly_hash` 来自 `release_info.json`；`buildid` 与 `branch`（`BetaKey` 存在则 `Beta`，否则
  `public`）来自 `appmanifest_2868840.acf`；**app id 只能由你自己的锁携带**
  （`bridge/trace_controller.py:284-316`）。
- **字节级自证要现算，不要信 mtime**：`combat_solver/modpin.py:113-148` 对锁里声明的每个 dll 现算
  SHA-256，缺文件或读不到按 error 记账而**不算匹配**（fail closed）。
- **换 mod 文件要求进程没在跑**，否则工具直接拒绝：`SlayTheSpire2.exe is running; refusing bridge file
  mutation`（`docs/ACT_COVERAGE_AUDIT_2026-09-21.md:509-510`）。

漂移这件事我们最后按**目的**拆成两条轨，因为它们本来就不该共用一个门：一条轨只需要"知道字节是什么"
（漂移记录进 blocker、批次照跑），另一条轨的自变量就是那个 mod 本身（必须钉死）。两条轨都仍然拒绝缺失
或不可读的必需模组。

---

## 8. 观测一个没有接口的 mod：Combat Solver

这个 mod **没有任何机器接口**——没有 HTTP、pipe、socket 或文件 watcher；候选路线只存在于内存，落盘只
发生在手动导出的 problem package 里（`docs/COMBAT_SOLVER.md:100-110`）。我们唯一的观测面是它的日志：

- **两代日志格式**：v1 把 `[CombatSolver/Test]` 块混在 `%APPDATA%\SlayTheSpire2\logs\godot*.log`
  里；0.35+ 的 v2 搬到 `logs\CombatSolver\<pid>-<guid>\`，一场战斗一个 `combat-<guid>.jsonl`，
  `process.jsonl` 只镜像性能白名单（`docs/COMBAT_SOLVER_0_41_DRIFT_2026-09-19.md:32-48`）。
- **语法必须按内容嗅探，不能按文件名**：首个非空行以 `{` 开头且能解出信封才判 v2，解不出就返回 `None`
  让调用方 fail closed。`combat-notes.jsonl` 这类名字不构成战斗身份；只有 `process.jsonl` 里的
  `COMBAT_LOG_BEGIN id=<32hex>` 才把一次搜索升格成一场战斗（`combat_solver/loggrammar.py:210-235,325-357`）。
  钉错语法会产出 typed `READER_DOWN`，而不是"重新解释一遍"。
- **v1 的块机在 v2 日志上产 0 快照**；v2 的 `RESULT` 永远自带从 turn=1 起的整条路线，而 v1 的"reused 包
  按首个动作重绑 turn"会把每一回合都标成 turn 1（`combat_solver/logv2.py:13-19`）。
- **v2 里没有 `SEARCH_FAILURE` / `SEARCH_ERROR` / `STALE` / `exception=`**（13 个会话实测 0 次），所以
  这两个失败类不许凭空映射（`combat_solver/loggrammar.py:70-75`）。
- **`ROUTE_ACTION` 是模组的预测，不是执行证明**。只有当它的 trace 在本次答案窗口内被完整重放、且签名
  等于最终发布的那个 `ACTION` 时才可引用（`combat_solver/logv2.py:36-43`）。
- **全自动开关怎么确认真的生效**：`SEARCH_REQUEST turn=1` + `UI_STATE state=ready` 开一个战斗 epoch；
  5 秒内没等到 `FULL_AUTO_DEPLOY turn=1` 才点一次、并以日志回执为准。`FULL_AUTO enabled=false` 只是辅助
  信号——关掉时它会静默切到 `manual_plus_solver`，所以真正的判据是"回合 1 的部署事件"。点击坐标是校准比例
  (0.2495, 0.5313) 加纵向 offset 表；拿不到可信面板位置就**拒绝点击**
  （`bridge/fullauto_keeper.py:1-24,38-60`）。0.41 起那个标记只出现在 `combat-*.jsonl`。

**给这个 mod 的接口请求**：一个 `GET` 状态端点、或至少一个"部署成功/失败"的结构化回执。现在所有外部观测
都在读它的调试日志，而调试日志会随版本换格式（已经换过一次）。

---

## 9. 客户端会怎么卡死，以及为什么不能 kill

- **`PunchOff`**（Combat 布局 + fire-and-forget 的 `PunchEachOther()`，只有离开房间才取消）会把循环打成
  每帧 VFX 失败：实测 2.3 GB / 2,204 万行、**16.8 MB/s**，10 秒内桥接就不响应。它是**非确定性**的——
  同一局第二次进入正常（`docs/ACT_COVERAGE_AUDIT_2026-09-21.md:455-464`）。
- **看门狗阈值**：一整天的批次基线只写 9.2 MB，所以"连续 4 个采样点 >5 MB/s"判 `client_wedged`
  （`scripts/supervise_solver_batch.py:340-349,1985-2035`）。处置是 `NtSuspendProcess` **冻结**而不是
  kill——kill 有可能正落在存档写入中间（`scripts/supervise_solver_batch.py:772-809`）。
- **桥不响应 ≠ 游戏死了**。求解器深搜 / 预载 / 存档都会占住 mod 的 HTTP 线程，实测最长 17.9 秒自愈。
  所以我们给 300 秒上限，并且意识到：用它挂钟计时意味着**主机睡眠超过 5 分钟一定会杀掉一个正在跑的验收
  批次**（`scripts/supervise_solver_batch.py:335-343`、`docs/STATUS.md:3976-3979`）。
- **"从第 1 层起手的连续 trace"只存在于上一局刚刚终局的那个窗口**：还压着未完成存档时，fresh start 会被
  mod 拒绝（`Singleplayer is not currently actionable`，重试 10 次）
  （`docs/ACT_COVERAGE_AUDIT_2026-09-21.md:478-480`）。
- **本次会话的日志就叫 `godot.log`**，要等客户端**下次启动**才改名成带时间戳的文件，所以按文件名归属批次
  不可靠（`scripts/supervise_solver_batch.py:714-727`）。

---

## 10. 游戏内容的结构事实（A10，铁甲战士）

这一节是"写模拟器 / 分析工具 / 平衡 mod 的人需要、但游戏里没有文档"的数值。来源是引擎侧提取 + 真机
trace 双向对照，机器可读版在 `training/campaign_content.py:431-528`：

- **三幕 progression 是 Overgrowth → Hive → Glory**（`ActModel.cs:510-515`）。Underdocks 只替换
  `list[0]`，它不是第二幕。第一幕 boss 会抽 VANTOM / KIN_PRIEST / LAGAVULIN_MATRIARCH，第二幕恒为
  THE_INSATIABLE。
- **boss 行 16 / 15 / 14 ⇒ 终局层 17 / 33 / 48**，与真机 trace 的 `1:17` / `2:33` / `3:48` 逐幕相等。
- **A10 本身就是那个双 boss**：`DoubleBoss` 是 AscensionLevel 的第 10 项，`maxAscensionAllowed = 10`，
  唯一消费点条件为 `i == Acts.Count-1`。第二个 boss 是 `BossMapPoint` 的子节点
  （`col/2, rowCount+1`），打完第一个由 `NRewardsScreen.cs:473-482` 回地图，所以它在 **floor 49**。
  第三幕因此要求清掉 **两个** boss。
- **每幕入口是固定的 Ancient 节点**，候选来自本幕池 roll。池子：Neow（Overgrowth）/ Orobas·Pael·
  Tezcatara（Hive）/ Nonupeipe·Tanx·Vakuu（Glory），共享的 Darv 只出现在第 2、3 幕。**恰好三选一**，
  三个池各出一张。第二幕在未揭示 Orobas epoch 时会摘掉 Orobas——所以没有解锁进度的模拟器只能把第二幕
  声明成 partial，而不是假装对齐。
- **每幕地图结构（7 列固定）**：weak 3/2/2、normal 12/12/11、精英各 5、商店各 3、休息
  `N(7,1)[6,7]` / `N(6,1)[6,7]` / `{5,6}`、未知 10-14 / 9-13 / 9-13；宝藏行 9/8/7、强制休息行 15/14/13；
  事件池是本幕自有 + 共享 18（epoch 门控的三个不发）。已知偏差：**放置数量可以少于队列**。
- **升级概率按幕走 0% / 12.5% / 25%**（A10 用 `Scarcity = 0.125`），**抽签先于判定**，每张卡占一次抽签，
  **Rare 永不参与升级**；商店卡的基率低到等于不升级。稀有度基率：regular `.0149/.37`、elite `.05/.4`、
  boss 恒定 3 张稀有。A10 金币：Monster 7–15、Elite 26–33、Boss 75。
- **真实游戏不发 boss 遗物**：`RelicReward` 只出现在 `case RoomType.Elite`；boss 给 gold + 药水 roll +
  3 张卡，全树 `BossRelicReward` 0 命中。**终幕 boss 什么都不发**，连药水那一抽都不消耗（生成前就
  return）。任何"打完最终 boss 选遗物"的实现都是发明出来的。
- **模拟器里的战斗数值已经是 A10 口径**：C# 侧不存在 ascension 字段，敌人数值由提取脚本取
  `AscensionHelper.GetValueIfAscension` 的 ASCENDED 分支（Fogmog 78 而非兜底的 74，intents 9/16 而非
  8/14）。**"不加 ascension 所以比游戏简单"是反的**，再补一个 ascension 条件等于降到 A0。
- **`empty_action_mask` 这堵墙是地图几何公式造成的**：50 例全在模拟器（49 例在第二幕 17 层、phase 全是
  map、50 例全都还活着、引擎掩码 0 个合法基元），定位路径是 boss → relic_reward → card_reward → map 的
  8 个 `map_option_coords` 全为 `-1`。旧公式 `terminalFloor = MapBossRow * Act + 1` 会要求一个 17 层的
  非战役局去够 33 层；改成 `ActStartFloor + ActBossRow` 后同一批种子的第二幕进入数从 2 变 4。
- **如果你在写幕池，注意这些敌人 id 曾在 `CombatFactory.cs:676-737` 里存在但没被 `RunConstants.cs:111-120`
  引用**：66 SoulNexus、70 Knights、71 MechaKnight、73 Aeonglass、75 KaiserCrab、76 KnowledgeDemon、
  78 Queen、80 TestSubject、81 TheInsatiable。

---

## 11. 可以直接搬走的四个工程做法

这四条与本项目的胜率无关，任何一个 mod 附带的自动化/评测工具都用得上。

1. **候选 codec：只枚举、不排序，身份稳定。** 从当前可见状态导出带 identity 的合法候选与精确 wire
   动作；解释层不许重排候选，也不许发明一个状态里没有的动作。契约见
   `docs/LIVE_CANDIDATE_CODEC.md`。
2. **一局只有一个动作拥有者。** `--execution-owner ∈ {http_route_executor, combat_solver_full_auto,
   manual_player}`，且与 `--out-of-combat-only` 互为条件——两个方向各有 `ValueError`。一个"日志 watchdog"
   永远不能被记成第二拥有者。并发执行者的症状是两个都以为自己在打，日志里出现互相矛盾的路线。
3. **版本锁 + 字节自证。** 把游戏 build、桥 dll、manifest、允许的 mod id 全部钉进一份
   `schema_version` 化的锁，启动时现算校验；缺文件是 error 而不是 match。漂移按目的分轨（§7）。
4. **环境版本号 + 摘要冻结。** 任何"模拟环境"的语义变化都必须反映在版本字符串里，并且**旧版本的声明按
   字面摘要冻结**。这样带旧名字的产物永远表示它当时的内容，不同环境的数字不会被平均到一起。
   `training/campaign_content.py:589-606` 解释了为什么摘要必须是字面常量：从字典重算的话，一次内容编辑会
   "验证"自己。

另一条纪律值得单独列出：**证据标签和读数来源要一起写下来。** 每次终止都记
`outcome_source`，每个指标都记它来自哪个字段——否则"我们从结构读到了败"和"我们从文案猜了败"在数据里
长得一模一样。

---

## 12. 我们希望 mod 侧提供、目前还没有的东西

给 STS2MCP / Combat Solver 作者的具体请求，按"便宜且有用"排序：

1. **decision id**（哪怕一个单调整数）：现在只能用整份状态的规范化 SHA-256 当身份。
2. **`is_victory` 进上游**（我们已经证明一位布尔够用，见 §5）。
3. **`run.seed` 直接进状态载荷**，省掉每次都要绕 compendium。
4. **参数名统一为 `index`**，以及 `select_card` 的**已选标志**随状态返回（现在只能靠 toggle 语义推断）。
5. **`state_type` 的权威枚举 + 每屏字段表**，并与实际载荷对齐（现在文档里有真机从不出现的
   `relic_select`）。
6. **Combat Solver 的结构化回执**（部署成功/失败、搜索失败原因），替代"读调试日志"这个隐式契约。
7. **按 mod 项禁用 Workshop 自动更新的官方途径**（现在只能靠把漂移如实记录下来）。

---

## 13. 随仓库发布的小数据与夹具

都不大，都可以直接拿去用；**游戏数据本体不随仓库分发**（`data/cards.json` / `relics.json` /
`version.json` 由 `python data/fetch_data.py` 从公开数据集抓下来，已在 `.gitignore` 里）。

| 文件 | 是什么 |
| --- | --- |
| `data/sample_state.json` | 一份与真机形状一致的 STS2MCP 响应，离线开发桥接的起点（配合 `dev.use_sample_state`） |
| `data/trace_record.schema.json` | 决策 trace 的必填字段与 A10/NOSL 条件 |
| `data/starter_decks.json` | 各角色初始牌组的近似值，只用于首战之前的 deck tracker；观测到战斗就被真实牌组覆盖 |
| `data/seeds/act_boundary_crossing_v5.json` | 会跨越幕边界的种子清单（冒烟测试用） |
| `data/combat_solver/fixed_battle_seeds.json` | 固定战斗种子的分区（`1600000000` 起 12 个，估算每种子 9 场） |
| `tests/fixtures/screens/*.json` | 真实屏幕载荷夹具（boss、card_reward、`event_the_architect_closing` 等），生成器在 `scripts/make_screen_fixtures.py` |
| `combat_solver/loggrammar.py`、`logv2.py` | 两代日志语法的解析器，含"按内容嗅探"的判据 |

---

## 14. 完整运行说明（要真的动游戏时读这里）

打真机需要 Windows + 运行中的《杀戮尖塔2》+ STS2MCP + Steam 客户端（用于
`-applaunch 2868840`）；把战斗交给求解器还需 Combat Solver 与 RitsuLib。安装与自检：

```bash
python -m pip install -r requirements.txt
python data/fetch_data.py                    # 抓卡/遗物数据集到本地（gitignored，不要提交）
python -m bridge.model_client --selftest     # 需要 `claude` CLI；只测模型链路
curl http://127.0.0.1:15526/api/v1/singleplayer?format=json
```

`.\install.bat`（Windows）/ `bash install.sh`（macOS、Linux）走同一套步骤；两者都不装训练栈、不碰 mod。

**局外驾驶**（会真发 POST，必须授权）：

```bash
python -m bridge.autoplay --dry-run                              # 只看 would_post
python -m bridge.autoplay --allow-actions --out-of-combat-only \
  --execution-owner combat_solver_full_auto --max-runs 1 --trace runs/scratch/t.jsonl
```

常用参数默认值：`--base-url http://127.0.0.1:15526`、`--max-runs 30`、`--max-actions 3000`、
`--poll 1.0`、`--lock-file config/live_version.lock.json`、`--cohort assisted`、
`--seed-mode observational`、`--log-dir %APPDATA%\SlayTheSpire2\logs`（传 `''` 关闭战斗执行）。
`--seed-file` 只在 `--seed-mode fixed` 下允许，且 fixed 必须给；带预注册分配的真授权还必须给
`--seed-ledger` 或 `--batch-dir` 之一。结构性红线：**没有 `abandon_run` 这条代码路径**，存档身份不匹配是
硬停而不是重试。

**一条命令打完整局**（分发器，不是第二套实现）：

```bash
python scripts/play.py --backend live [--preflight-only] [--track acceptance|comparison]
python scripts/play.py --backend sim --config <训练时那份.toml> --checkpoint <ckpt.zip> --seeds <…> --output out.json
```

`--preflight-only` 用**监督器自己的 `--dry-run`** 回答"能不能跑"，闸门与实跑同源；被拒时返回 2 而不是
假装成功。`--backend sim` 需要装了训练栈（`requirements-training.txt`）的解释器和相邻的
[`slay-the-spire-2-emulator`](https://github.com/Zamiell/slay-the-spire-2-emulator) checkout，
`--checkpoint` 与 `--seeds` 都必填。

**批次与证据**：`python scripts/supervise_solver_batch.py --mode observational --dry-run` 会拉起
逐战斗比对、局外驾驶和 full-auto watchdog 三个子进程，状态与自证落在
`runs/solver_supervisor/<batch-id>/status.json`。

**测试**：官方 runner 是 stdlib `unittest`（仓库里没有 pytest 配置，也没有任何 `pytest.mark`）。
`python -m unittest discover -s tests` 覆盖纯契约层；要连训练层一起跑就装 `requirements-test.txt`，或用
`powershell -ExecutionPolicy Bypass -File scripts\test.ps1`（双解释器：轻量契约层 + `STS2_TRAINING_PYTHON`
指向的 torch 层，原生模拟器集成测试在相邻 checkout 缺失时自动跳过）。

**配置**：`config.yaml` 只服务于桥，顶层 7 个键（`bridge` / `debounce` / `model` / `paths` / `overlay` /
`autostart` / `dev`），加载器不做默认值合并、文件缺失即抛错、只在启动时读一次。`config/` 下另有训练
toml、比较轨的 `combat_solver.toml`，以及两份版本锁（`live_version.lock.json` 服务 A10 验收轨、只允许
`STS2_MCP`；`combat_solver.lock.json` 是比较轨的模组清单，多模组组合会被判污染）。

---

## 文档索引

- [真机版本锁定、trace 录制与动作门禁](docs/LIVE_BRIDGE.md) — §2 的完整版
- [局外候选与 wire action 契约](docs/LIVE_CANDIDATE_CODEC.md) — §3、§4 的完整版
- [Combat Solver 分层与运行审计](docs/COMBAT_SOLVER.md) — §8 的完整版
- [逐幕覆盖审计：真机 vs 模拟器](docs/ACT_COVERAGE_AUDIT_2026-09-21.md) — §5、§9、§10 的证据与逐条出处
- [模拟器幕内容保真度](docs/SIMULATOR_ACT_FIDELITY_2026-09-21.md) — §10 的度量与判据
- [固定种子可行性](docs/FIXED_SEED_FEASIBILITY.md) — §6 的完整版
- [本地决策 Trace 数据契约](docs/TRACE_DATA.md) · [架构与训练课程](docs/ARCHITECTURE.md) ·
  [A10 验收协议](docs/ACCEPTANCE.md) · [阶段式训练操作说明](docs/TRAINING.md) ·
  [现有方案调研](docs/RESEARCH.md) · [贡献指南](CONTRIBUTING.md)
- 本项目自身的推进状态、失败账目与撤回记录不在本文件里：见 [当前真实状态](docs/STATUS.md)。

## 上游与许可

本项目从 [Spire Oracle](https://github.com/bnipper-creator/sts2-advisor) 的源码副本起步，实时状态来自
[STS2MCP](https://github.com/Gennadiyev/STS2MCP)，战斗由
[Combat Solver](https://github.com/Torch1230/CombatSolver) 执行，模拟器基线是
[slay-the-spire-2-emulator](https://github.com/Zamiell/slay-the-spire-2-emulator)。各组件的用途与许可见
[UPSTREAM.md](UPSTREAM.md)。本仓库的 advisor 代码为 MIT，见 [LICENSE](LICENSE)；Slay the Spire 2 及其
数据、美术归 Mega Crit 所有，第三方数据集保持各自条款。
