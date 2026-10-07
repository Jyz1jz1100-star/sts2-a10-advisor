# STS2MCP 附魔选牌模态：所有请求都返回 ok，界面永不前进（2026-10-07）

给 **STS2_MCP**（桥接 mod，appid 2868840）作者的可提交复现记录。与
`docs/COMBATSOLVER_MULTISELECT_BUG_2026-10-07.md` 是**两个不同的归属**：那份是 CombatSolver 在
开战时的多选模态；这份发生在**战斗之外**的商店楼层，CombatSolver 全程一行日志都不写（它不拥有这个界面）。

## 环境

| 项 | 值 |
|---|---|
| 游戏 | Slay the Spire 2 v0.111.0，commit `41cef1ea`，steam_build_id `24724944`，main_assembly_hash `222455745` |
| 桥接 | STS2_MCP **0.4.0**，`STS2_MCP.dll` sha256 `730D60B154626F64…`（与本方版本锁一致） |
| 同机其他 mod | CombatSolver 0.50.1（文件摘要 `832060172AA5EAE8…`）、RegentFX 0.5.1、STS2-RitsuLib 0.6.6 |
| 角色 / 进阶 | Ironclad，A10，标准单人，modded profile1 |
| 观测窗口 | 2026-10-07 `10:21:09Z`–`10:27:14Z`（批次 `ssb-20261007T100846Z`）与 `10:29:54Z`–`10:34:16Z`（批次 `ssb-20261007T102954Z`，续玩同一存档） |

## 复现（作者侧最短路径）

1. Ironclad A10 标准单人，走到**第三幕某商店楼层**（本方实例为 act 3 floor 39）。
2. 由遗物/商店效果触发附魔选牌：`GET /api/v1/singleplayer?format=json` 返回
   `state_type = "card_select"`，其中
   `screen_type = "NDeckEnchantSelectScreen"`，`prompt = "选择3张卡牌附加魔法"`，
   `cards` 共 **24** 张，`can_confirm = true`，`can_cancel = false`。
3. 按界面自己的语义依次发三次单张选择、再确认：
   ```
   POST { "action": "select_card", "index": 0 }
   POST { "action": "select_card", "index": 1 }
   POST { "action": "select_card", "index": 2 }
   POST { "action": "confirm_selection" }
   ```

## 期望 对比 实际

* **期望**：确认后附魔生效、界面关闭（同一存档在 **floor 38** 上遇到过**同一 `screen_type`**、
  `prompt = "选择1张卡牌附加魔法"`、7 张牌的模态，那时 4 次请求之内就正常推进了）。
* **实际**：四条请求全部返回 `{"status":"ok"}`，界面**一模一样地留在原地**。
  本方第二个批次把它跑成了完整循环：**363 次请求全部 `ok`，`select_card` 的 index 分布
  `0:91, 1:91, 2:91`，即 91 个「选 3 张 + 确认」周期**，跨越约 4.5 分钟，
  1,089 帧 `card_select` 状态**逐字节相同**，`decision_id` 始终是
  `local-sha256:97c6156983eb6ca8…`。
* 关键点：桥接**没有拒绝任何东西**。它持续回答 ok，而游戏侧什么都没发生——对使用者来说这比
  报错更难定位。

## 对定位有用的两条观察（请核对，本方不下结论）

1. **`card_select` 视图里没有任何"已选"状态**。该帧每张卡的字段只有
   `cost / description / id / index / is_upgraded / keywords / name / rarity / star_cost / type`；
   顶层只有 `can_cancel / can_confirm / preview_showing / prompt / screen_type`。
   所以调用方**无法从状态里判断 `select_card` 有没有落地**，`ok` 也无从校验。
   若能在这个视图里带上每张卡的 `selected`（或已选数量 / 还差几张），这类问题一眼可辨。
2. **单选可过、多选不过**（同一 `screen_type`，floor 38 的 1 张 vs floor 39 的 3 张），
   与本方给 CombatSolver 的那份报告里"`CHOICES_PARADOX` 单选正常、`GAMBLING_CHIP` 多选卡死"
   是**同一个形状**。两条归属不同，但都指向多选提交路径；请各自核对是不是共用了同一段
   `NativeChoiceSurface`/确认节点代码。本方不假设两个 mod 之间有依赖。

## 本方已经做的（不改游戏、不改 mod，只是不再无限重试）

* 该模态在批次里最初表现为"接受了 335 次请求也不前进"。本方此前两道守卫都看不见它：
  重复检测按 **payload** 计数（而调用方会正常地换卡牌下标），过期检测只在**被拒绝**时计数。
  现在增加了按"同一份未变化的状态读数被接受了几次"的硬上限（普通界面 12 次、菜单 24 次，
  由 745 个健康 decision id 的实测分布定出：正常上限是 4 次），超限即**具名自停**
  `screen_not_advancing`，原因写进 `stop_reason` 而不是靠退出码猜。
* 也就是说：这条改动让我们**不再把整批时间烧在墙上**，但没有、也不会替游戏点这个模态——
  战斗之外的界面本方本来就只按桥接公布的候选发请求。

## 本方的存档现状

两次观测之后客户端仍停在同一模态（`can_confirm = true`，24 张牌）。清除它要么靠作者修，
要么靠使用者在游戏里手点/放弃这局。**放弃存档（`abandon_run`）归运维方，本方不会代为执行。**
