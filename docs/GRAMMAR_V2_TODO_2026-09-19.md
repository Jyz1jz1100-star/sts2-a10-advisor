# grammar v2 剩余缺口（分支 `wip/grammar-v2`，2026-09-19）

实现主体已在分支上：`combat_solver/logv2.py`(812) + `loggrammar.py`(540) +
`logformat/logranges/reader/evidence` 改动 + `scripts/replay_solver_grammar_v2.py`
+ `tests/fixtures/combat_solver_v2/` + `tests/test_combat_solver_grammar_v2.py`。

真机回放已达到的程度：15 会话 / 16798 记录 / 24263 消息 →
**72 快照、69 部署（全部带字节区间）、22 类型化失败（NO_ROUTE 21、CRASH 1）、0 信封错误**，
turn 绑定 72 条（v1 只有 19 条且全落在 turn=1）。对照控制组：v1 在同一批解码行上
95 事件 / 19 快照 / 7 失败。**所以 codec 已经能用，但还不能算验收过。**

`python -m unittest tests.test_combat_solver_grammar_v2` → 47 项，当前 8 失败 + 1 报错（原 11+3）。
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
