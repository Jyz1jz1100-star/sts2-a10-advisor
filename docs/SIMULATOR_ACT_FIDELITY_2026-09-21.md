# 模拟器 Act 2 / Act 3 保真度矩阵（2026-09-21）

回答的问题只有一个：**在这个模拟器里训练出来的局外策略，它的决策在真实 build 里是不是仍然正确。**
不是"有没有第三幕"，也不是"复刻了百分之多少"。

分级口径（逐机制、逐幕）：

| 级别 | 含义 |
|---|---|
| **A** | 等价，且有证据 |
| **B** | 近似，但大概率不改变最优决策，可用于训练 |
| **C** | 明显失真，会改变最优决策 |
| **D** | 完全缺失 |
| **E** | 尚无法验证 |

真实侧权威来源：`third_party/slay-the-spire-2-emulator-main/decompiled/`（锁定 build v0.111.0 / `41cef1ea`）。
模拟器侧：`src/Sts2Emulator/**` 与 `src/sts2_gym/**`。

---

## 1. 先说三个会改变结论的发现

**(1) 真实 build 根本没有"boss 遗物选择"。**
`Rewards/RewardsSet.cs:243` 的 `RelicReward` **只出现在 `case RoomType.Elite:`**；
`:245-261` 的 boss 分支给的是 gold + rolled potion + `CardReward(..., 3, player)`；
`RewardsSet.cs:67-74` 终幕 boss 连这个都不给（空集）。全树搜索 `BossRelicReward` **0 命中**。
精英给的也是 `RelicReward.cs:45-46`  rarity=None → `PullNextRelicFromFront` 的**单件自动发放**，宝箱同理
（`Game/TreasureRoomRelicSynchronizer.cs:87-98` 每人 1 件）。
→ 本项目长期写的"boss 胜局只在遗物屏出口才判定"、以及建立在它之上的
"遗物屏有一个未取动作值 +21/+16 胜局"分析，**描述的是模拟器的产物，不是真机机制**。
该结论只能在 `simulator_*` scope 下引用，不得再作为真机策略依据。

**(2) 真实第三幕不是"更难的同一套池子"，是**不同**的内容和**更短**的地图。**
`Acts/Glory.cs:75-100` 的 18 个遭遇、`:50,52` 的 `NumberOfWeakEncounters=2 / BaseNumberOfRooms=13`，
`:111` 的 `restCount = mapRng.NextInt(5, 7)`——**全游戏休息最少的一幕**。
模拟器三幕一律 `MapBossRow=16`、`restCount` 6–7、`Shop 3 / Elite 8` 固定
（`RunConstants.cs:12-18`、`RunMapGenerator.cs:1123-1124`）。
→ 在模拟器里，"多走休息点"这一策略在第三幕不会被惩罚；在真机里第三幕休息稀缺，
**同一策略会把资源算错**。这是直接改变路线决策的失真，不是外观差异。

**(3) 卡牌升级概率在真机里按幕递增，模拟器里恒为 0。**
`Factories/CardFactory.cs:393-397` 把 `CurrentActIndex * UpgradedCardOddScaling`
（A8 下 0.125，否则 0.25）加到升级概率上，基础值为 0 → **真机 Act1≈0%、Act2≈25%、Act3≈50%，且稀有牌永不升级**；
模拟器 `RunRewardGenerator.cs:1127-1131` `RollCardUpgrade(...) { _ = rng.NextDouble(); return false; }`。
→ "选这张卡还是那张卡"在真机后期价值判断不同，直接影响选牌策略。

---

## 2. 覆盖矩阵

图例：`A/B/C/D/E`。**"影响决策"** = 若在这里算错，策略学到的取舍在真机不成立。

### 2.1 敌人 / boss

| 机制 | 真实证据 | 模拟器位置 | A1 | A2 | A3 | 影响决策 |
|---|---|---|---|---|---|---|
| 普通敌池 | `Acts/Overgrowth.cs:79-108`(22) · `Underdocks.cs:72-99`(20) · `Hive.cs:79-106`(20) · `Glory.cs:75-100`(18) | `RunConstants.cs:113-116` | B | **C** | **C** | 是（强度/WithTag 反重） |
| 弱敌占比 | `ActModel.cs:309-331`，OG/UD 3弱+12、Hive 2+12、Glory 2+9 | 同上（12 与 10 项） | B | C | C | 是 |
| 精英池 | 每幕 3 种：`Bygone/Byrdonis/PhrogParasite`、`Phantasmal/Skulking/TerrorEel`、`Decimillipede/Entomancer/InfestedPrisms`、`Knights/MechaKnight/SoulNexus` | `RunConstants.cs:117-118`（每幕仅 2） | C | C | C | 是（精英风险估值） |
| 精英数量 | `MapPointTypeCounts.cs:15-21` 5，A1(SwarmingElites)→**8** | `RunMapGenerator.cs:1123` 固定 8 | B | B | B | 是（A1 才正确） |
| boss 池 | OG `Vantom/CeremonialBeast/TheKin`、UD `WaterfallGiant/SoulFysh/LagavulinMatriarch`、Hive `TheInsatiable/KnowledgeDemon/KaiserCrab`、Glory `Queen/TestSubject/Aeonglass` | `RunConstants.cs:119-120`（只有前两幕） | B | **C** | **C** | 是 |
| **未见 boss 优先** | `ActModel.cs:368-377` `HasSeenEncounter` 决定 boss，非随机 | 无（`RunMapGenerator.cs:93` 纯随机） | E | E | E | 否（不影响单局最优） |
| **终幕双 boss** | `RunManager.cs:685-691` 从**剩余 2 个 Glory boss** 均匀抽；`RoomSet.cs:64-70` 按访问计数；是 boss 节点之上**额外一行**（`StandardActMap.cs:86-91,223-230`） | `RunEngine.cs:1982-1993` 取 Underdocks boss 池下一项；`:1996-2012` 直接开打，无地图/奖励/回血 | — | — | **C** | 是（两场连打的资源规划完全不同） |

### 2.2 地图与路线

| 机制 | 真实证据 | 模拟器位置 | A1 | A2 | A3 | 影响决策 |
|---|---|---|---|---|---|---|
| 行数/列数 | 7 列；行 = rooms+1 → 16/15/14（`StandardActMap.cs:81-83`） | `RunConstants.cs:12-18` 一律 17 行 | B | **C** | **C** | 是（路线长度=资源预算） |
| 每幕休息数 | OG/UD 6–7、Hive 6–7、**Glory `NextInt(5,7)`={5,6}**；另有倒数第二行强制休息（`StandardActMap.cs:264-272`） | `RunMapGenerator.cs:201` 一律 6–7 | B | B | **C** | **是（休息=回血/升级取舍）** |
| 未知节点 | `UnknownMapPointOdds.cs:23-29` Monster .1 / Treasure .02 / Shop .03 → **≈85% 事件**；`:107` 精英**永不**从未知出；`:117-132` 保底递增 | `RunEngine.cs:298-324` Monster .1 / Elite −1 / Treasure .02 / Shop .03 | A | A | A | 是（风险定价一致） |
| 放置约束 | 行<6 无休息/精英（`:477-483`）；末 3 行无休息；同类不得父子相邻（`:495-513`）；失败类型回队，余量转 Monster（`:436-449,325-332`） | `RunMapGenerator.cs:1180-1226` 同形约束 | A | A | A | 是 |
| 精英可回避 | 是，无任何强制精英 | 是，但可见选项上限 4（`:973-977`） | B | B | B | 是 |
| 先古之民入口 | `StandardActMap.cs:334` **每幕第 0 行固定 Ancient 节点** | `RunEngine.cs:23` 仅开局一次 | B | **D** | **D** | **是（每幕开局一次资源置换）** |

### 2.3 事件 / 商店 / 奖励

| 机制 | 真实证据 | 模拟器位置 | A1 | A2 | A3 | 影响决策 |
|---|---|---|---|---|---|---|
| 事件池 | OG `Overgrowth.cs:36-48+` 与 epoch 门控（`ActModel.cs:289-306`）；UD/Hive/Glory 各异，另 +18 共享（`ModelDb.cs:121-125`） | `RunMapGenerator.cs:125-184`（A2 与 A3 **同一份**25 项） | B | C | C | 是 |
| 事件去重 | `RoomSet.cs:106-121` 每局不重复，耗尽才允许重复 | 洗牌序列，无跨局去重语义 | B | B | C | 是（后期事件分布不同） |
| 事件结果 | 改 HP/gold/relic/deck（真机侧由事件模型自身） | `RunEngine.cs:2171-3433` 约 50 个分支真实改状态 | B | B | B | 是（方向对，细节不同） |
| 商店库存 | `MerchantInventory.cs:15-28,154-180` 5 职业牌+2 无色+3 遗物(1 个 Shop 稀有)+3 药水，1 张半价 | `RunRewardGenerator.cs:914-987` 同结构 | A | A | A | 是 |
| 商店价格 | `MerchantCardEntry.cs:37-46,146-152` 50/75/150 ×±5%；遗物 `RelicModel.cs:290-299` 175/225/275/200 ×±15%；药水 50/75/100 | `RunRewardGenerator.cs:1182-1213` 同 | A | A | A | 是 |
| 移除涨价 | `MerchantCardRemovalEntry.cs:17-31` 基础 75（A6 起 100）+每次 +25（A6 +50） | `:985` `100 + 50 * removalsUsed` | B | B | B | 是 |
| **跨幕价格变化** | **无任何 act/floor 项**（已穷举搜索） | 无 | A | A | A | — |
| 卡牌奖励稀有度 | `CardRarityOdds.cs:111-151` 普通 0.6/0.37/0.03、精英 0.5/0.4/0.1、**boss 恒定 3 张稀有** | `RunRewardGenerator.cs:1060-1077` 按节点类型 | B | B | C | 是 |
| 稀有度保底 | `:59-73` 全局累计，出稀有后归 −0.05 | `:1087-1101` 同形 | A | A | A | 是 |
| **奖励升级概率** | `CardFactory.cs:393-397` **随幕号线性增加** | `:1127-1131` **恒 false** | C | C | C | **是** |
| 遗物奖励 | `RelicFactory.cs:68-78` 50/33/17；`RelicGrabBag.cs:69-70` 按角色、不放回 | `RunRewardGenerator.cs:1029-1038` 24 件均匀，`RollRelicRarity` 结果被丢弃 | C | C | C | 是 |
| **精英/宝箱=单件自动发放** | `RelicReward.cs:45-46,68-78`；`TreasureRoomRelicSynchronizer.cs:87-98` | 精英与 boss **同一发放路径**（`:394-399`） | C | C | C | 是（含虚构的 boss 遗物选择） |
| 地板前 41 层限制遗物 | `RelicModel.cs:416-420` 18 件仅 `TotalFloor < 41` 可得 | 无此门控 | — | — | C | 是（第三幕宝箱价值不同） |

### 2.4 休息 / 状态携带 / 终局

| 机制 | 真实证据 | 模拟器位置 | A1 | A2 | A3 | 影响决策 |
|---|---|---|---|---|---|---|
| 休息选项 | `RestSiteOption.cs:41-57` 单人**恰好 Heal + Smith**，Smith 需有可升级牌 | `RunEngine.cs:703-711` 同（heal/upgrade/skip） | A | A | A | 是 |
| 休息回血 | `HealRestSiteOption.cs:105-108` 最大 HP 的 30%，与幕/飞升无关 | `:3513` 30% | A | A | A | 是 |
| HP/金币/牌组/遗物跨房携带 | `RunState` 全程携带；金币无上限（`EncounterModel.cs:48-53` 10/35/100） | `RunEngine.cs:1471-1492` | A | A | A | 是 |
| **A1 开局减血** | `AncientEventModel.cs:160-170` Neow 置 0 再满血；`WearyTraveler`(A2) 时只回 80% | **无飞升建模** | C | C | C | 是（开局血量决定前期路线激进度） |
| 药水槽 | `Player.cs:28` 3；`AscensionManager.cs:29-32` A4 起 2 | `RunRewardGenerator.cs:1015` 恒 2 | B | B | B | 是 |
| 药水效果 | 全量实现 | `PotionEffects.cs:5-33` 63 种中**仅 3 种**有效果 | C | C | C | 是 |
| 遗物战斗效果 | 全量实现 | `RelicEffects.cs:7-22` 296 种中**仅 14 种**参战 | C | C | C | 是 |
| **A10 全套** | `AscensionLevel.cs` 1–10 全实现（含 `DoubleBoss`） | **NOT IMPLEMENTED**（仅起始牌含 `AscendersBane`） | C | C | C | **是** |
| 死亡判定 | `RunState.cs:111-119` 仅"全员死亡"；`RewardsSet.cs:130-135` 死者不再发奖励 | `RunEngine.cs:1285-1298` | A | A | A | 是 |
| 截断 | 真机无步数上限概念 | 仅 Python `run_env.py:117`（C# `truncated` 恒 false） | B | B | B | 是（不可把没打完算成输） |
| 胜利 | `RunManager.cs:1207-1246` → `TheArchitect` | `:1963-1965` `RunCleared = PlayerHp > 0` | — | — | C | 是（语义不同：真机是事件房，模拟器是"活着"） |

---

## 3. Minimum training fidelity gate（允许正式大规模 full-run RL 之前）

**判据不是"补了多少文件"，而是：策略在这个环境里学到的取舍，在真机上会不会被反向惩罚。**
按此排序，前 6 项为硬门，后 3 项为软门（可与训练并行推进）。

| 优先级 | 缺口 | 为什么它会教坏策略 | 现状 |
|---|---|---|---|
| **G1** | 把 A2/A3 池换成 **Hive / Glory 真池** | 敌人强度、精英组成、boss 组成全是错的 → 学到的"这条路线安全"在真机不安全 | 内容已在 `CombatFactory.cs`，缺 `RunConstants` 池表 |
| **G2** | **每幕先古之民**（含各自候选） | 每幕开局一次"现在付代价换永久收益"的决策完全不存在 → 少学一整类取舍 | 事件处理器已有（`RunEngine.cs:3088-3390`），不可达 |
| **G3** | **终幕双 boss 的真实结构**（回地图→额外一行→连打两场） | 现在两场之间不回血、不发奖励、不选路 → 学不到终幕的资源规划 | `RunEngine.cs:1996-2012` |
| **G4** | **按幕的地图形状**：行 16/15/14、A3 休息 5–6 | 路线长度与休息预算恒定 → 第三幕"多休息"这一错误选择在真机致命 | `RunConstants.cs:12-18` |
| **G5** | **奖励升级概率按幕递增** + boss 恒定 3 稀有 | 选牌价值函数在后期系统性偏低 | `RollCardUpgrade => false` |
| **G6** | **去掉虚构的 boss 遗物发放**，改成真机的"精英/宝箱单件自动发放" | 策略被教成"为了遗物屏去打 boss"，真机没有这个收益 | `:394-399` |
| G7（软） | A10 飞升效果（至少 `WearyTraveler`/`SwarmingElites`/`TightBelt`/`Inflation`/`Scarcity`/`DoubleBoss`） | 开局血量、精英密度、药水槽、移除价、稀有与升级概率、双 boss——全是决策变量 | 完全缺失 |
| G8（软） | 遗物/药水的战斗效果覆盖面（14/296、3/63） | 遗物价值被算成噪声 | 部分 |
| G9（软） | 前 41 层限制遗物与第三幕宝箱关系 | 终幕宝箱估值偏乐观 | 缺失 |

**G1–G6 未清完之前**：允许 smoke、单元测试、集成测试、小规模探索性 PPO、已验证的 Act 1 scope 训练、
以及为找环境 bug 的短跑；**不允许**把 Underdocks 换皮 A2/A3 上的大规模训练称为正式 full-run 模型，
不允许把 `campaign_clears` 当作真机 A10 胜率。此约束已写入产物字段
（`environment_version=sts2sim-campaign-approx-v1`、`content_scope=simulator_three_act_approx`）。

---

## 4. 同时被本轮纠正的既有文档结论

1. **"boss 胜局只在遗物屏出口才判定"及其 +21/+16 估算**：该屏在真机不存在（§1.1）。
   保留原始数据与旧语义不追改，但引用时**必须标注为 simulator-scope 结论**。
2. 本会话早些时候我把 `relic_select` 从 `index: 0` 改成显式保守规则：
   按上面的证据，真机侧**没有 `relic_select` 状态**（09-21 两条真机 trace 的状态全集里就没有它），
   因此那次改动对真机存活率**没有可宣称的作用**；真正生效的是事件/先古之民那条规则
   （真机确实以 `event` + `is_ancient` 出现）。这条自我更正一并记录，避免以后把无关改动当成改进。
3. 模拟器 `max_floors` 是死参数（`run_env.py:59/66` 存而不用），`config/training.toml:100 max_floors = 49`
   也是惰性的，且比 campaign 终局层 50 少 1。

## 5. 这一版之后，G1-G6 由一台仪器判定，不再由本文判定

`scripts/verify_campaign_fidelity_gates.py` 读引擎自己的 C# 表与函数体（不是读本文），
可选地用可复现 checkpoint 走真 campaign 采样，逐门给出：真实来源 / 模拟器实现位置 /
当前判据 / 是否通过 / 尚存差异。`--out` 落 JSON，退出码 0 只在六个硬门全过时出现。
**没测到 = 不通过**（`not_measured` 不算绿）。

冻结机制：`training/campaign_content.py` 里 approx-v1 的内容声明被钉了一个 sha256，
带这个名字改内容会直接抛 `EnvironmentVersionError`；跨 environment 合并被
`assert_single_environment` 与 `merge_reward_rule_slices.py` 拒掉。
下一版保真环境必须叫 `sts2sim-campaign-fidelity-v2`（已在本模块预留）。
2026-09-22 已按下文 §6 发布，approx-v1 的声明与摘要原样保留。

2026-09-21 首次运行（静态判据）：**六门全 FAIL**，并且每台仪器都报告它读到了什么——
例如 G3 的 `paired_final_act_boss_body` 直接印出第二 boss 是
`UnderdocksBossEncounters` 里的"下一个 id"，G5 印出 `RollCardUpgrade` 的函数体
`_ = rng.NextDouble(); return false;`。`tests/test_campaign_environment_versions.py`
把"锚点必须还能在引擎里找到"钉成测试：正则失配时先红，不会把门判绿。

### 本文两处需要按真机/真源码更正的地方

1. 升级概率的豁免条件是 **`CardRarity.Rare` 不参与幕递增**（`CardFactory.cs:393-397`）：
   普通与 uncommon 都吃 `CurrentActIndex * UpgradedCardOddScaling`（0.25，A8 Scarcity 下 0.125），
   本文先前写成"稀有牌永不升级"过头了——Rare 只是不吃递增项。
2. Hive/Glory 的弱怪在模拟器 id 空间里**并非都有独立条目**（例如实机
   `ExoskeletonsWeak` 与 `ExoskeletonsNormal` 都落到 id 4），
   所以即使 G1 把池子接对，弱/正常两档在部分怪上仍不可区分——这是接完 G1 之后
   仍然存在的差异，必须留在 `still_differs` 里而不是被"池子接上了"一句话盖掉。

## 6. fidelity-v2 的落地记录（2026-09-22）

这一版只做 G6 + G1 + G3，改动全在引擎里，不在文档里：

* **G6**：`RunRewardGenerator.GenerateCombatRewards` 的遗物发放收窄成
  `state.CurrentNodeType is RunConstants.NodeElite`。真实 build 里 `BossRelicReward`
  一次都没出现（`RewardsSet.cs:245-261` 给 boss 的是金币 + 一次药水 roll + 三张稀有卡），
  所以原来那枚 boss 遗物是本引擎发明的屏幕。两处保留 trace 夹具（固定种子的历史复现）
  仍按原样发它们自己的遗物，这是刻意的，写在函数注释里。
* **G1**：`RunMapGenerator.SelectActAndGenerateRooms` 的池子选择从"`Act != 1` 就是
  Underdocks"的二分，改成 Overgrowth / Hive / Glory / Underdocks 四档，判定用
  `RunConstants.IsHiveAct` / `IsGloryAct`（两者都要求 `state.Campaign`，因为非战役下
  act 数字含义是抛硬币出来的第一幕）。`Underdocks` 只剩它本来的身份：Alternate Act 1。
* **G3**：配对 boss 不再"从池子里取下一个 id 并立刻开打"。现在第二 boss 在**生成这一幕
  房间时**就从本幕 boss 池里抽定（排除已定的第一 boss，走同一 up-front 流，
  `RunManager.cs:685-690` 的形状），打赢第一 boss 后引擎回到地图、在 boss 之后另开一行
  （`OpenSecondBossRow`，坐标 `MapBossRow + 1`，等价于 `SecondBossMapPoint`），
  走常规路径 travel 过去。`Floor` 由 travel 递增，两战之间 HP/药水/卡组/遗物/金币连续。

判据也一起改了，因为"源码里写了"不等于"状态机走到了"：

* G3 与 G6 现在要求 `engine_scenario_tests()` 跑引擎自己的场景测试（`dotnet test --filter`），
  并先用 `--list-tests` 核对过滤器点名的测试确实存在——一个匹配不到任何测试的 filter 会退 0，
  那是最便宜的假绿。G6 的静态探针也不再在整文件里找 `PendingRelicReward = true`
  （事件处理器里本来就有两处合法的遗物发放），而是锚在 `GenerateCombatRewards` 的函数体上。
* G1 是两条腿：走查腿（10,000 个种子跑真 campaign，只统计**在战斗中**的帧，
  因为地图/过渡帧的 info 里还挂着上一个节点的 encounter id）与生成器腿
  （`EveryCampaignActDrawsFromItsOwnPools`，60 个种子 × 3 幕，逐房间/精英/boss 断言
  落在本幕池子里、地图节点携带的 id 与之一致、每幕至少 6 个不同遭遇、
  第二幕与第三幕的可见集合不相交）。

实测结果（`runtime/fidelity_gates_v2.json`，`environment_version=sts2sim-campaign-fidelity-v2`）：

* G1 PASS：act 1 21/21 全在 Overgrowth 池（含三幕 boss 74/82/83），act 2 2/2 全在 Hive 池，
  act 3 在走查腿里是 `unreached_acts=["act_3"]`（没有任何策略活到那里），由生成器腿覆盖；
  `illegal_actions_total=0`、`dead_ends=[]`。
* G3 PASS、G6 PASS；G2 / G4 / G5 仍然 FAIL，六门退出码仍是 1，`hard_gate_passed=false`。

代价被一起量了出来，而不是被藏起来：同一个 act1 checkpoint（approx-v1 训出来的）在
fidelity-v2 上走过 10,000 个声明过的种子，只有 **2** 局进入第二幕、**0** 局进入第三幕，
最深 **floor 19**。approx-v1 下同一批种子有 25 局到达第二幕 boss、1 局打通三幕。
差出来的那一枚遗物就是 G6 删掉的那个屏幕发的——也就是说旧环境用一件真游戏不发的遗物
把策略"送"进了后两幕。因此：

1. `scripts/smoke_full_run_pipeline.py` 的第 11 项 `episode_crosses_act_boundaries`
   现在**红**（10/11）。标准没有被下调：它要求有一局真 campaign 跨到第三幕，
   而今天没有任何 checkpoint 做得到。分类是**策略强度 + 产物归属**
   （checkpoint 属于上一个环境），不是流水线缺陷——跨幕状态机本身由 G3 的场景测试证明。
2. `scripts/probe_boss_reward_rule.py` 那条 "+21 / +16 局" 的规则只在 approx-v1 有意义，
   它键在 `phase=relic_reward 且 current_node_type=boss` 上，而这个状态在新环境里不存在。
   产物现在带 `result_applies_to_environment`，重跑只会量到 0，不会量到"少赢了 21 局"。
3. 下一版（G2/G4/G5）不能拿 v2 的任何强度数字当结论，也不能拿 v1 的数字当 v2 的对照，
   两边产物由 `assert_single_environment` 分开。
