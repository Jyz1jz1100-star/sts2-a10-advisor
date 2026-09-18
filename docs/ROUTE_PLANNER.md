# 地图路线前瞻与离线策略对照

`advisor_core/route_planner.py` 是一个只读的地图策略模块。它消费当前
STS2MCP 状态中已经公开的 `map.next_options` 和 `map.nodes`，为每个当前合法
选项计算一个可解释的即时分、可见未来分和总分，并返回现有
`Recommendation` 契约。它不读取 seed，不请求下一屏，不模拟战斗、卡组、商店
或未来 RNG，也不执行 POST。

当前固定接口是：

```python
from advisor_core.route_planner import RoutePlannerPolicy

policy = RoutePlannerPolicy(
    max_nodes=256,
    max_depth=4,
    future_discount=0.85,
)
recommendation = policy.recommend(map_state)
wire = policy.wire_action_for(map_state, recommendation.primary.action)
```

`RoutePlannerPolicy` 只负责 `state_type == "map"`。生产 autoplay 和 live advisor
默认仍使用 `LiveHeuristicPolicy`；本轮没有把路线策略接入正在运行的控制循环，
也没有把训练 checkpoint 作为入口。需要实机接入时，应先单独完成小批整局验证，
再使用显式的 opt-in policy factory，并保持 Combat Solver 作为战斗唯一 owner。

## 可见深度和预算

完整地图时，`map.nodes` 的 `children` 是可见图。规划器对所有当前选项使用同一
个深度，最多展开 `max_depth` 个后继层；`max_nodes` 是共享预算，预算不足时
所有候选统一降深，避免用深路线和浅路线比较。`search_nodes` 是实际展开的
可见节点数，`primary.metrics` 至少包含：

| 字段 | 含义 |
| --- | --- |
| `immediate_score` | 当前选中节点按现有生命阈值规则得到的分数 |
| `future_score` | 可见后继路径的折扣分，不是生存概率 |
| `total_score` | 即时分加折扣后的未来分 |
| `lookahead_depth` | 本次所有候选共享的实际前瞻深度 |
| `known_path_nodes` | 返回的可见代表路径节点数 |

如果 `map.nodes` 不存在或为空，规划器只使用每个候选自身的
`leads_to` 一层预览，并在 `warnings` 中标明。缺失的后继不被当成安全节点。
未知节点类型按现有 `Unknown` 分档；循环和冲突坐标会被显式处理或拒绝。

规划器在评分前会做节点类型归一化：`RestSite`/`rest_site` 归为 `rest`，
`Merchant` 归为 `shop`，`Question` 归为 `unknown`；结构性的 `Ancient`/`Start`
和 `Boss` 计 0 分。旧 `LiveHeuristicPolicy` 的 map 规则按原始小写字符串查表，
所以 `RestSite`、`Ancient`、`Boss` 等名称在两者间可能已经产生分数差异；路线
前瞻只是额外差异来源，不能把每个 divergence 都归因于 lookahead。未来路径
沿用当前状态的 HP 分档，不推演战斗掉血、治疗或卡组变化，因此这些分数不代表
净生存收益。

这些分数用于固定输入下的策略对照，不能解释为通关率、战斗损失预测、全局
最优性或胜率改进。

## 离线对照器

`scripts/compare_route_policies.py` 读取 `bridge.trace_controller` 或
`bridge.autoplay` 写出的原始 JSONL 事件流。它逐行处理文件，只保留
`event_type == "state"` 且 `state_type == "map"` 的状态：

1. 用共享的 visible filter 计算稳定状态哈希，去掉轮询产生的重复状态；优先
   以局内标识和 decision id 归组，缺失时使用状态哈希。状态事件缺少
   `session_id` 时继承同一源文件最近的 session/run identity；完全没有局内
   标识时按文件隔离，避免两个 trace 的相同占位状态互相去重。同一 run 的
   `decision_id` 若再次对应不同状态哈希，会保留 `decision_state_conflict`
   excluded 行并计入错误，避免静默丢失身份冲突。
2. 在同一当前状态上调用 `LiveHeuristicPolicy` 和显式构造的
   `RoutePlannerPolicy`。
3. 输出两者的主选项、备选项、精确 wire action、合法候选、分数分解、搜索节点
   数及是否分歧。
4. JSON 损坏、缺失 map、候选契约错误等会以紧凑 `excluded` 行保留来源和原因；
   非 map 状态只计入 summary，不写入输出行，避免把战斗轮询复制到报告。

示例：

```powershell
& .tools\python\cpython-3.12-windows-x86_64-none\python.exe `
  scripts\compare_route_policies.py `
  runs\live_traces\clean-a10-path-left-heuristic.jsonl `
  --out runs\route_comparison\pilot\comparison.jsonl `
  --expected-character IRONCLAD --expected-ascension 10
```

命令会同时生成 `comparison.jsonl.summary.json`。输出、自动 summary 以及可选的
`--summary` 目标都必须是不存在的新文件，且不能指向任何输入；校验在创建输出
之前完成，文件使用独占创建，因此原始 trace 和已有报告不会被覆盖。报告只写
匿名 `run_key`（原始 run/profile id 只在内存中用于去重），来源使用项目相对文件名、
行号和 sequence，不复制原始 compendium、player id 或完整 state。

summary 还记录实际的 `max_nodes`、`max_depth`、`future_discount`，每个已读输入
的字节级 SHA-256，以及 `git_commit`/`git_dirty` 说明，方便复核同一输入和配置。
输入文件按行读取并边读边计算 hash，不把巨型 JSONL 一次性载入内存。状态 envelope
的外层 `build` 会保留给候选 codec 校验；外层旧 build 不能被内层当前 build
掩盖。

报告中的 `claims` 明确标记：没有游戏 I/O、没有未来结果输入、没有 checkpoint，
并且不是胜率评估。`divergences` 只回答“同一当前状态下两个策略是否给出不同
合法动作”，不能据此声称路线策略提高了胜率；要验证胜率，必须另行注册固定
环境、锁定战斗组件并进行成对整局实验。

## 已完成的离线复核

2026-09-07 对一份历史真实 `ssb` autoplay trace 做了逐行离线对照：
[comparison.jsonl](../runs/route_comparison/ssb-20260905T193903Z-4e8b9eb1-20260907/comparison.jsonl)
及其 [summary](../runs/route_comparison/ssb-20260905T193903Z-4e8b9eb1-20260907/comparison.jsonl.summary.json)。
输入约 43.6 MiB，读取 7,450 行，得到 75 个 map 事件、30 个唯一 map 状态、20
个两策略均成功的比较和 3 个合法动作分歧；45 个重复状态被丢弃，10 个无合法
候选的 map 状态被保留为 excluded，未发生 comparator error。报告只使用当前
state，明确忽略后续 `result` 事件，不能解释为胜率或整局收益证据。

同日对照最新失败批次的真实 `ssb` trace 也完成了流式读取，但只有 122 个 menu
state、没有 map state；它能证明读取器面对失败批次会收敛，但不能贡献路线分歧
样本。两份结果都没有启动游戏或发送 POST。

## 设计参考

公开的 [STS Route Plan](https://github.com/dice-c/Slay-the-Spire-route-plan)
把完整路线特征与规则安全约束分开；
[spire-agent](https://github.com/AttemorySystem/spire-agent) 将地图、构筑和战斗
决策分层，并保存可 replay 的 run 记录。这些项目支持“局外路线”和“局内战斗”
分层的工程方向，但不为本项目的分数或胜率提供证据。
