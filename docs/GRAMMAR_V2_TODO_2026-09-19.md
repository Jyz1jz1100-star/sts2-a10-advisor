# grammar v2 剩余缺口（分支 `wip/grammar-v2`，2026-09-19）

实现主体已在分支上：`combat_solver/logv2.py`(812) + `loggrammar.py`(540) +
`logformat/logranges/reader/evidence` 改动 + `scripts/replay_solver_grammar_v2.py`
+ `tests/fixtures/combat_solver_v2/` + `tests/test_combat_solver_grammar_v2.py`。

真机回放已达到的程度：15 会话 / 16798 记录 / 24263 消息 →
**72 快照、69 部署（全部带字节区间）、22 类型化失败（NO_ROUTE 21、CRASH 1）、0 信封错误**，
turn 绑定 72 条（v1 只有 19 条且全落在 turn=1）。对照控制组：v1 在同一批解码行上
95 事件 / 19 快照 / 7 失败。**所以 codec 已经能用，但还不能算验收过。**

`python -m unittest tests.test_combat_solver_grammar_v2` → 47 项，**已全部通过**
（2026-09-19 04:57 本地时间；随后 `scripts/test.ps1` 415 + 99 全绿）。
结案见文末"结案"一节；下面按根因的原始归类保留，因为它记录了每一条当时是怎么判断的。

## A. 汇总字典缺键（3 个 ERROR，纯机械）
- `SessionBoundaryTests.test_combat_log_boundaries_are_counted_not_guessed`
  → 缺 `error_level_records`、`combat_log_end`、`combat_log_end_reasons`
- `EvidenceChannelTests.test_two_identical_traces_leave_the_scraped_route_in_place`
  → 缺 `evidence_adopted`
- `EvidenceChannelTests.test_unusable_route_action_is_counted_not_invented`
  → 缺 `unusable_route_actions`

进程级汇总与单文件级汇总产出的键集合不一致，需要统一（这些计数是证据口径本身，
不能用 `.get(k, 0)` 糊过去）。

## B. 文件选择没有 fail-closed（1 个 FAIL，安全属性）
`test_non_journal_files_are_not_sources`：`combat-not-a-guid.jsonl` 被当成源。
要求是只接受 `combat-<32 hex>.jsonl`；名字不合法就不是战斗日志。

## C. 读取失败必须显式化（已完成，安全属性）
`test_pinned_grammar_refuses_the_other_container`：期望
`['READER_DOWN']` 类型化失败，实际 `[]`。静默空列表会把"读不到"伪装成"没有事件"。

## D. 快照/事件计数口径（3 个 FAIL）
- `test_journal_tree_is_found_under_a_game_log_dir`：5 != 10
- `test_embedded_markers_are_counted_by_the_v2_scanner`：1 != 0
- `test_blocks_can_span_polls_and_ranges_stay_valid`：`[] != [1]`（跨 poll 块与字节区间一致性）

## E. 证据通道语义（3 个 FAIL，最需要想清楚的部分）
- `test_route_is_taken_from_the_trace_only_when_it_is_unambiguous`：False is not true。
  已排除"逐动作混用来源"这一猜测：`_route_for` 本来就整条选用 trace 或整条退回扫描，
  且 `group_route` 会传递 `note`。真实原因是该 fixture 的 trace 与扫描结果**签名不相等**
  （走了 evidence_no_match 分支）。下一步是打印两侧签名差异，判定是扫描侧缺字段
  （如药水 PotionId）还是 trace 侧多/少动作；不得为了让 note 都以 evidence: 开头而
  放宽相等条件——签名相等是采纳 trace 的唯一理由。
- `test_route_health_is_counted_but_never_substituted`：已完成。`route_health`
  改为按 trace 计数（原按行，等于在数 beam width）；`[4, 0] != [4]` 一条**已判明是
  断言写错**：fixture 里没有独立的 `TURN_OUTCOME` 行，逐回合对（`turn=1 hp_lost=4`
  与 `turn=2 hp_lost=0`）嵌在 RESULT 记录内，模组确实报了两个被搜索回合，
  所以解析器产出 `[4, 0]` 是忠实的，只期望 `[4]` 反而会丢掉模组已发布的值。
  断言已按记录改正并补注来源。
  （更正：此前一条提交把这里判成"解析器发明数据"，本段是核清记录顺序后的结论。）
- `test_diverged_replay_suppresses_the_answer`：抑制信息里必须带 `traceId`，
  现在只带在括号里而断言找的是短形 id —— 需要决定到底是消息格式还是测试口径

## F. 重复发布去重（部分完成）
`test_identical_answer_republished_in_one_window_is_one_snapshot`。已实现跨窗口的
RESULT 回显抑制（`suppressed_echoes`）：块外再次出现、且身份与上次已消费答案完全
相同、中间没有新请求的 RESULT，计为 echo 并丢弃。该断言的幻影失败从 **3 条降到
1 条**，仍未归零——剩下那条来自追加记录之后仍挂着的一个"有请求无 RESULT"的块，
需要顺着 `_flush_block` 的块生命周期与 fixture 实际顺序再查，不能靠放宽
NO_ROUTE 判定来凑绿。

## G. reader 分类与复用（2 个 FAIL）
- `test_default_reader_classifies_each_file_separately`：godot.log 与 jsonl 混在
  一个流里嗅探，要求**每个文件独立判定语法**（顺序也不同）
- `test_reused_answer_keeps_the_full_route_and_the_new_turn`：`[1, 2] != [1]`

## 边界（不要为了过测试而放宽）
- 不许把 v1 数据当 v2 解释；版本按内容嗅探或显式参数，绝不按文件名猜。
- `ROUTE_ACTION` 是模组自己的**预测**路线，不能当成执行证明；
  `source="deploy_log"` 的语义必须仍是"生产者日志声称部署过"。
- 字节区间必须指向真实读到的源文件偏移，标记计数与区间覆盖同一批字节。
- 无法表达的数据要产出显式类型化失败，不许猜值。

## 结案（2026-09-19 05:00 本地时间，提交 `32d1771`）

决定性的一条实测：本机含 RESULT 记录的 **10 个真机 0.41.0 战斗日志**，每一个的
`ROUTE_ACTION` 都只在**该战斗第一个 `reused=False` 的 RESULT 之前**成批出现一次，
之后每个 RESULT 的窗口里 `ROUTE_ACTION` 记录数都是 0（那些答案全部带
`SEARCH_REUSED from_turn=…` + `reused=True`）。也就是说"每个答案都自带 trace"
不是生产者的行为。

由此：

- **E 第一条的旧诊断是错的**。它写着"trace 与扫描签名不相等、走了
  `evidence_no_match`，下一步打印两侧签名差异"。实测两侧签名**相等**，被采纳的
  答案 1 确实拿到了 `evidence:53d1…#0..3`；失败的原因是**其余答案的窗口根本没有
  trace**，所以 `note` 是 `None`，而旧断言要求所有快照的 note 都以 `evidence:` 开头。
  断言已改成生产者真正满足、且对验收更有用的形式：只有自带记录窗口的答案可以引用
  trace；turn=2 扫描到与 turn=1 **完全相同**的四动作路线，也**不得**继承 trace id
  （这正是防伪造的那一条）。
- **D 的三条**：`journal_tree` 是测试自败——`copy_session` 固定用 fixture 目录名，
  第二个"会话"根本没被创建，改为 `copytree` 并新增分组与顺序断言；
  `embedded_markers` 是 **v1 扫描器真缺陷**——同一行上第二个 `[CombatSolver/…]`
  前缀之后的标记会被计入 v1 字节区间（等于把 v2 记录的内嵌载荷洗进 v1 证据），
  现要求 v1 标记必须来自该行第一个前缀；`diverged_replay` 的失败详情现在会写出
  它属于哪场战斗（`SolverFailure` 除 `detail` 外没有别的身份通道）。
- **F**：幻影失败的真正原因是回显抑制只记得**上一个**已消费答案。重述更早的答案会
  穿过抑制、落到 `_emit_snapshot` 的"无 turn 标记请求"分支，凭空生成一条
  `PARSE_ERROR`——而失败记录正是验收门读的字段。现在记住本场战斗全部已消费身份
  （`frozenset` 化的 `result_identity`）。测试改为对照式：追加回显后的快照列表与
  失败列表必须与不追加时**逐条相同**，且 `suppressed_echoes` 恰为 1。
  （顺带纠正：本文原先说"干净 fixture 的失败应该为 0"是不成立的——fixture 本身就
  合理地产出 `NO_ROUTE@3` 与 `TIMEOUT@4`。）
- **G 第二条**：`[1, 2] != [1]` 是断言字面量写错。生产者自己的 `Turn` 字段就把
  EndTurn 标在 turn 2（`ROUTE_ACTION` index 3 = `"Turn": 2`），RESULT 的 ACTION 行
  一致。测试的意图（复用答案保留整条战斗长度路线、同时报自己的 turn）保持不变。

仍然没有做的事，别把这一节读成"真机链路已通"：

1. 用改动后的扫描器对**全部真机日志**重跑
   `scripts/replay_solver_grammar_v2.py`，核对快照/失败口径有没有因为 v1 锚定收紧
   而漂移（fixture 全绿不等于 15 会话真机回放不变）。
2. 真机批次本身。04:37 实测 `127.0.0.1:15526/health` 连接被拒、无游戏进程，
   要冷启动 Steam 与游戏窗口；三幕只能在真机完成。
3. `SolverFailure` 没有结构化的战斗 id 字段，只有 `detail` 文本。目前够用
   （人读 + 关键字匹配），但按战斗聚合失败率时需要机器可读字段。

### 已测得（签名对比，非猜测）
在 BATTLE_A fixture 上打印两侧 `route_signature`：`(1,play,FEEL_NO_PAIN,None),
(1,play,BASH,0), (1,potion,SKILL_POTION,None), (2,end_turn,None,None)` 出现 4 次，
另有 `(3,play,DEFEND,None)` 1 次。**这段当时读错了一步**：出现 4 次不等于"4 个答案
都采纳了 trace"——`_route_for` 只在**本块自带 trace** 时才可能采纳，而 turn=2/3/5 的
窗口里没有 ROUTE_ACTION 记录（实测 `evidence_adopted = 1`、
`answers_without_replay_validation = 3`，turn 2/3 的 `note` 全是 `None`）。签名相等
只说明"扫描结果与 trace 描述同一条路线"，不构成引用它的理由。结论以"结案"一节为准。
