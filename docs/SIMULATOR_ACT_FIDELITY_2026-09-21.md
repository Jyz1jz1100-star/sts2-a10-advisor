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
2026-09-22 已按下文 §7 发布，approx-v1 的声明与摘要原样保留。

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

## 6. G2 的实现依据（已按源码钉死，尚未实现）

先把"要造什么"写清楚，避免下一轮凭印象发明：

* **每幕起点就是先古之民节点**：`StandardActMap.cs:333-338` 在同一段里同时给出
  `BossMapPoint = Boss`、`StartingMapPoint = Ancient`、`SecondBossMapPoint = Boss`；
  `ActMap.cs:88-92` 又把 Ancient 与 Boss 一起当作"不受网格边界约束的点"。
  也就是说真实游戏不是"某一幕里随机一个事件房是先古之民"，而是**进场必会遇见它**。
* **每幕抽哪一个**：`ActModel.cs:345-348`
  `_rooms.Ancient = rng.NextItem(GetUnlockedAncients(unlockState).Concat(_sharedAncientSubset ?? []))`，
  与 boss 抽取同流同位置。池子：Overgrowth 只有 `Neow`（`Overgrowth.cs:29-32`）；
  Hive 是 `Orobas / Pael / Tezcatara`（`Hive.cs:27-33`），且 `Hive.cs:110-115`
  在 `OrobasEpoch` 未揭示时把 Orobas 摘掉；Glory 是 `Nonupeipe / Tanx / Vakuu`
  （`Glory.cs:26-32`，`Glory.cs:104` 无额外解锁裁剪）；共享子集（Darv）只给 2/3 幕
  （`RunManager.cs:669-676` 跳过第一幕）。模拟器没有 unlock/epoch 状态，所以接入时
  必须把"Orobas 是否可能出现在 2 幕"写进 residual，而不是悄悄全给或悄悄全不给。
* **候选是什么**：六个先古之民的 `AllPossibleOptions` 全部以
  `RelicOption<X>` 为主体（`Orobas.cs:19-47`、`Pael.cs:16-30`、`Tezcatara.cs:25-39`、
  `Nonupeipe.cs:29-43`、`Tanx.cs:27-41`、`Vakuu.cs:18-29`），
  另有少量专属项（`PrismaticGem`、`SeaGlass`、`PaelsClaw/Tooth/Legion/Growth`、
  `BeautifulBracelet`、`TriBoomerang`）。这意味着模拟器现有的先古屏
  （`RunPhase.Ancient` + `State.NeowOptions[]` + `ApplyAncientChoice(relicId)`，
  `RunEngine.cs:23/904-912/1412`）足以承载"候选=遗物 id、效果=给遗物"这一半；
  专属项需要各自的实现，属于 G2 的第二半。
* **一次给几个候选、怎么选**：`AncientEventModel.cs:179-198` 把
  `GenerateInitialOptions()` 的结果作为 `GeneratedOptions`，而各先古自己给出**恰好 3 个**：
  `Orobas.cs:167-195` 把 `OptionPool1`（3 件遗物）与"海玻璃/棱镜宝石"特殊项合成一袋后
  `Rng.NextItem`，再各自 `NextItem(OptionPool2)`、`NextItem(OptionPool3)`；
  `Vakuu.cs:119-137` 是三池各自 `UnstableShuffle` 后取 `[0]`。
  也就是说"每幕先古之民 = 从三个池子里各出一个候选的三选一"，与模拟器已有的开局 Neow
  三选一（`State.NeowOptions[]`）同形——G2 可以沿这条既有通道实现，不必新造屏幕。
* **两条必须留痕的越界风险**：`Hook.ShouldAllowAncient`（`AncientEventModel.cs:181`）为假时
  真实游戏只给一个 PROCEED；`NeowsBones` 之类的解锁门槛也会改变候选。模拟器没有 hook/unlock
  状态，接入时这两点要写进 `not_modelled`，不能让"三选一永远成立"被当成已核实。

G2 的验收不变：三幕各自的先古之民真的可遇见、候选合法、效果进入观测；
不会因为"名字改了"算通过。

## 7. fidelity-v2 的落地记录（2026-09-22）

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

## 8. fidelity-v3 的落地记录（2026-09-22）

这一版只做 G2，外加一件在发布卫生检查里挖出来的事（8.3），那件比 G2 本身重要。

### 8.1 G2 实现成了什么

* **每一幕在生成自己房间时就抽定自己的先古**（`RunMapGenerator.GenerateActAncient`，
  走 up-front 流——形状来自 `StandardActMap.cs:333-338` 把 Ancient 与 boss、配对 boss 放在
  同一个块里，以及 `ActModel.cs:344-348` 在 boss 旁放下本幕先古）。act 1 仍是开局的 Neow，
  act 2 是 Pael / Tezcatara，act 3 是 Nonupeipe / Tanx / Vakuu 三选一。
* **打赢 boss、进入下一幕时先见到先古，再拿到地图**：`RunEngine.EnterActAncient()` 挂在
  `AdvanceToNextAct()` 末尾并置 `Phase = Ancient`。四个原有引擎测试从"跨幕后应是 Map"改成
  "跨幕后应是 Ancient，选完才是 Map"——屏幕顺序本身就是这一门的判据。
* **候选项按 build 的三个池各出一张**（`RunAncientChoices.Offer`），并且真的收钱/收血：
  DistinguishedCape 扣 9 点 HP 上限、且不允许把健康的局压到 1 点以下。
* **Orobas 被排除，并写明为什么**：`Hive.cs:110-115` 要 epoch 解锁才把它放进池子，
  而模拟器没有解锁进度，抽它等于谎报"这一局打过谁"。所以 act 2 在声明里是
  `ancient_matches_real_game_act = "partial"` 而不是 `true`；`ShouldAllowAncient` 可能把三选一
  塌成单个 PROCEED、以及卡组相关的候选（NutritiousSoup / PrismaticGem）没建模，也都写在
  `not_modelled` 里。

### 8.2 判据与实测

* G2 静态腿四项（每幕抽定、入口先于地图、Hive/Glory 都有 dispatch、池子是 build 的那六个）
  之外，场景腿跑 3 个引擎测试：`ClearingAnActBossMeetsTheNextActsAncientBeforeItsMap`、
  `GloryActsAncientOffersThreeOfItsOwnCandidates`、
  `DistinguishedCape_ChargesNineMaxHealth_WhenAnAncientOffersIt`（3/3 通过，`--list-tests`
  先确认过滤器点得到它们）。
* 一万个声过名的种子走完真 campaign：**G1 / G2 / G3 / G6 PASS，G4 / G5 仍 FAIL**，
  `published_gates_passed=true`、`hard_gate_passed=false`，六门退出码仍是 1。
  产物 `docs/evidence/campaign_fidelity_gates_20260922_v3.json`，
  环境摘要 `6e84c9e7…`（`sts2sim-campaign-fidelity-v3`）。
* **强度一点没变**：进入第二幕的仍是 2 局、第三幕 0 局、最深 floor 19，
  `scripts/smoke_full_run_pipeline.py` 第 11 项仍红（10/11）。G2 只多了一扇门，
  不会让旧 checkpoint 变得更能打——把"屏幕多了"读成"策略变强"是这类发布最容易犯的错。

### 8.3 引用完整性：一次全绿里的 25 处坏指针

做 v3 的 provenance 重算时，把新快照与 `emulator_source_provenance_20260920.json`
逐条对齐，发现 **66 条引用里 25 条的行号已经不在它自己声称的那段代码上**。其中至少三处
正文与钉到的完全不是同一段：`RunMapGenerator.cs` 的 189 行（正文说"boss 所在那一行"、
钉到事件 id 列表）、`RunEngine.cs` 的 727 行（正文说 `hasPotionSlot`、钉到一个 `}`）、
以及裸写的 `:2263`（正文说某个事件臂调 `AddPotion`、那行早就不是了）。

机制很清楚：**快照是拿正文现有行号重新哈希的**。引擎长了几十行而正文没跟着挪时，
哈希记到新行上，六项检查全绿。`emulator_source_provenance` 抓的是"引擎换了"，
抓不到"引用滑了"——所以 v2 发布是带着 25 处坏指针出去的，而 `verify_report_claims.py`
当时报的是 61/61。

修法与约束：

* 33 处引用（53 个出现点）按 09-20 记录的文本重钉；**每一处都要通过一个锚点**——
  目标行必须真的含正文声称的那段代码，任一处不满足就整批拒写。
  规则落在 `tests/test_emulator_provenance.py` 的 `CitationAnchorTests`，
  因为 `runtime/` 在 gitignore 里，脚本本身不算交付物。
* 裸行号（`:2263`、`同文件 1922-1923`）连 `*.cs:行` 的正则都不匹配，是这批滑动里最安静的一类；
  两处改写成了带文件名的形式（进快照受管集合），其余留在表里、由测试核对它上下文借用的文件名没变。
* 语义真的被 v2/v3 改掉的四处（`AdvanceAfterRelicReward` 的 boss 分支、`terminalFloor` 的表达式、
  开局抛硬币外层的战役覆盖、`AdvanceAfterNode`）不是"挪数字"，正文已按现在的代码改写并注明
  是哪一版动的，approx-v1 的读法保留在句子里当历史。

**已知没修的**：`docs/STATUS.md`、`docs/ACT_COVERAGE_AUDIT_2026-09-21.md` 与本文自身的
`*.cs:行` 引用（合计约一百七十处）没有摘要校验，v3 挪动的那些行同样影响它们；本轮只逐条核对了
被快照钉住的那一份报告。这条不是"以后再说"的客套：下一次发布前，谁引用了引擎行号，
就得先按同样带锚点的办法走一遍。

## 9. fidelity-v4 的落地记录（2026-09-22）：G4 + G5，六门全绿

`sts2sim-campaign-fidelity-v4`（声明摘要 `d7c9dece…`，tier
`simulator_three_act_campaign_shape_and_rewards`）。这一版把最后两门关掉，
所以 `hard_gate_passed` 第一次为 `True`；下面按"真规则 → 引擎改成什么 → 量到什么"记。

### 9.1 G4：每一幕生成自己的地图

读到的真规则推翻了三处旧假设：**列数固定 7**（`StandardActMap.cs:19`），只有行数按幕变；
所谓"weak 3/2/2、normal 12/12/11"是**队列长度**而不是池子大小；`elites` 是 5 而不是引擎里的 8。

* 行数：`BaseNumberOfRooms` 15/14/13（`Overgrowth.cs:56`、`Hive.cs:56`、`Glory.cs:52`，
  Underdocks 15）→ boss 行 16/15/14，宝藏行 −7、强制休息行 −1 全部跟着本幕深度走
  （`RunConstants.MapRoomsForAct/MapBossRowFor/MapWeakDrawsFor`）。
* 队列：精英 5、商店 3（`MapPointTypeCounts.cs:17-24`）、休息 N(7,1)[6,7] / N(6,1)[6,7] /
  `{5,6}`（`Overgrowth.cs:156`、`Hive.cs:122`、`Glory.cs:111`）、未知 N(12,1)[10,14] 且 2/3 幕各减一
  （`Hive.cs:123`、`Glory.cs:112`）。
* 事件池：每幕 `自有 + 共享 18` 洗牌（`ActModel.cs:288,307`）。Act 1 原来还少 4 个共享事件；
  2/3 幕原来直接走 Underdocks 列表。epoch 未解锁的 `ColorfulPhilosophers` / `Reflections` /
  `TrashHeap` 与 Orobas 同样**不发**，理由写进声明。
* 层数账：`terminalFloor` 从 `MapBossRow * Act + 1` 改成 `ActStartFloor + ActBossRow`，
  起始层在跨幕时累加。

**差分证据**：改完之后引擎几何推算出的 boss 层号是 **17 / 33 / 48**，与真机 trace 实测的
`boss_battles_by_act_floor`（同一份 `live_run_coverage_20260920.json`）**逐幕相等**——
旧乘式在第三幕给 49。这一条是 G4 静态腿里唯一的"对客户端"检查，其余判据仍是对 C# 源码：
门的场景腿跑 `EachCampaignAct_GeneratesItsOwnMapRows`、
`EnteringTheNextAct_StartsItOneFloorAfterTheActJustCleared`、
`EachAct_QueuesItsOwnRestsAndOnlyFiveElitesAndThreeShops`、
`HiveAndGlory_DrawTheirOwnEventsAndNotTheUnderdocksList`。

**顺手被解释掉的一件事**：旧乘式要求一个 17 层的非战役 Underdocks 局去够 33 层，
这正是 `empty_action_mask` 那堵墙的来源——记录在案的 49 个 Act 2 floor 17 截断，
不是策略打不过，也不是地图真没路，而是终止条件写错。改完之后同一批种子里进第二幕从 2 局变成 4 局。

### 9.2 G5：升级概率与奖励构成

* **升级概率按幕**（`CardFactory.cs:387-409`）：战斗奖励传进去的基率是 0，每幕加
  `UpgradedCardOddScaling`（A10 走 Scarcity = 0.125，`:23-24`）乘**本 run 内的 0 基幕序**，
  所以 0% / 12.5% / 25%；抽卡**先于**一切判定（`:389`），一张不可能升级的卡也要占一次抽签；
  **稀有卡不参与**（`:393`）；商店卡传的是一个极低的基率，等于永不升级（`:81,101`）。
  幕序取"本 run 内序号"而不是 act 枚举：Underdocks 是替换 `list[0]` 的**另一条第一幕**
  （`ActModel.cs:498-507`），把枚举当幕序会给每个单幕 Underdocks 局第二幕的概率，
  悄悄给旧产物所在环境重新定价。
* **最终幕 boss 什么都不发**：`RewardsSet.cs:68-74` 在生成任何奖励之前 return，
  连药水那一抽都不消耗。引擎原来在这里照发金币+药水+三张卡+遗物屏，与 G6 删掉的 boss 遗物同类。
  现在 `HasPendingRewards` 为空即当场结算节点，不再开屏。
* **A10 金币**：Monster 7–15、Elite 26–33、Boss 75（`EncounterModel.cs:44-80` 的
  10-20/35-45/100 乘 Poverty 的 0.75）。引擎此前精英与普通已是缩水后的值，boss 却仍发 100。
* 稀有度概率（regular .0149/.37、elite .05/.4、boss 全稀有）与漂移偏移本就已经实现，
  本轮只是被逐条核对过；同时把一张手抄的 144 条"卡 id → 稀有度"表删了，改读生成的卡表
  （核对过：144 条与生成表**零不一致**，奖励池 84 张全覆盖）。
* 场景腿：`CampaignFinalActBoss_DealsNoRewardsAndConsumesNoRewardDraw`（含"连抽签都不消耗"这条
  最尖的判据）、`EarlierActCampaignBoss_DealsRareCardsNoRelicAndTheAscensionTenGold`、
  `CampaignRewardCards_UpgradeAtTheActsOwnOdds`、`RareRewardCards_NeverUpgrade_EvenInAnActThatRollsUpgradeOdds`、
  `SingleActRunRewardCards_NeverUpgrade_BecauseEverySingleActRunIsActOne`。

### 9.3 六门全绿不等于验证等价

tier 特意停在 `content_verified` 下面，`not_modelled` 里留着：没有 Ascension 模型（配对 boss
在这里是常量）、Unknown 节点进场再抽签未建模、TheArchitect 结局未建模、两个 weak 遭遇变体缺数据、
**放置可以少于队列**（24 个种子里第三幕出现过 4 精英、第二幕出现过 5 休息——剪枝/补点只从可改的
Monster 节点补，这条是量出来的，不是从 build 推的）。所以"6/6 PASS"说的是六项硬内容判据齐了，
不代表模拟器与客户端逐节点相同；`live A10 win rate` 依旧在 `must_not_be_quoted_as` 里。

强度侧照实记：一万个声明过的种子，进第二幕 4 局、第三幕 0 局、最深 floor 19、非法动作 0，
冒烟第 11 项仍红。六门齐了不等于策略能打。

### 9.4 §8 那条规则第一次被执行

v4 的引擎改动又挪走了全部引用行号，这次按 §8 的规矩走：锚点表成为**唯一真值**
（`tests/test_emulator_provenance.py::CITATION_ANCHORS`），报告跟着它挪。
先试"就近配对"时它把两条指错了位置（就近不是同一性），于是 35 对映射改成显式写出、
由锚点判对错——
一条 `GetOrCreate` 的锚点把我引到的 284 行纠正成 234 行，正是这套办法该有的样子。
`emulator_source_provenance` 六项在 71 条引用上重算相符，声明检查 61/61。


