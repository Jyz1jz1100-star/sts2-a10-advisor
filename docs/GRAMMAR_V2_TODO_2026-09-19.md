# grammar v2 剩余缺口（分支 `wip/grammar-v2`，2026-09-19）

实现主体已在分支上：`combat_solver/logv2.py`(812) + `loggrammar.py`(540) +
`logformat/logranges/reader/evidence` 改动 + `scripts/replay_solver_grammar_v2.py`
+ `tests/fixtures/combat_solver_v2/` + `tests/test_combat_solver_grammar_v2.py`。

真机回放已达到的程度：15 会话 / 16798 记录 / 24263 消息 →
**72 快照、69 部署（全部带字节区间）、22 类型化失败（NO_ROUTE 21、CRASH 1）、0 信封错误**，
turn 绑定 72 条（v1 只有 19 条且全落在 turn=1）。对照控制组：v1 在同一批解码行上
95 事件 / 19 快照 / 7 失败。**所以 codec 已经能用，但还不能算验收过。**

`python -m unittest tests.test_combat_solver_grammar_v2` → 47 项，11 失败 + 3 报错。
按根因归类，不是同一个原因：

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

## C. 读取失败必须显式化（1 个 FAIL，安全属性）
`test_pinned_grammar_refuses_the_other_container`：期望
`['READER_DOWN']` 类型化失败，实际 `[]`。静默空列表会把"读不到"伪装成"没有事件"。

## D. 快照/事件计数口径（3 个 FAIL）
- `test_journal_tree_is_found_under_a_game_log_dir`：5 != 10
- `test_embedded_markers_are_counted_by_the_v2_scanner`：1 != 0
- `test_blocks_can_span_polls_and_ranges_stay_valid`：`[] != [1]`（跨 poll 块与字节区间一致性）

## E. 证据通道语义（3 个 FAIL，最需要想清楚的部分）
- `test_route_is_taken_from_the_trace_only_when_it_is_unambiguous`：False is not true
- `test_route_health_is_counted_but_never_substituted`：`[4, 0] != [4]`
  （`ROUTE_HEALTH` 只能计数，绝不能替代路线）
- `test_diverged_replay_suppresses_the_answer`：抑制信息里必须带 `traceId`，
  现在只带在括号里而断言找的是短形 id —— 需要决定到底是消息格式还是测试口径

## F. 重复发布去重（1 个 FAIL）
`test_identical_answer_republished_in_one_window_is_one_snapshot`：同一窗口内重复
答案被误判成 `NO_ROUTE` 失败，实际应为 0 失败、1 快照。

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
