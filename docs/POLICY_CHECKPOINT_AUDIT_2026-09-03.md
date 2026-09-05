# POLICY_CHECKPOINT_AUDIT_2026-09-03

审计范围：`sts2-a10-advisor` 当前工作区内全部 `.pt`、`.zip` 训练/检查点产物，以及现有 metrics/metadata、live policy 与 autoplay contract。审计为只读；未训练、未启动游戏、未修改实现。版本基线为 Ironclad A10、目标游戏 `v0.111.0`；V2 emulator native API v8，native hash `bcd623ce3ca02c5fc310a0e1249ee3f3a95694b757f18d00e321d414a9b34bc4`。

## 结论（Go / No-Go）

**实时局外训练策略：NO-GO。** 当前没有任何训练 checkpoint 能直接接入 `advisor_core.policy_live` 或 `bridge.autoplay`，也没有证据支持“战士 A10 约 50% 胜率”。可运行的局外建议仍是 `LiveHeuristicPolicy` 与 autoplay 的手写 fallback；它们不是训练模型，confidence 为 0，不能作为最优性或胜率证明。

最佳离线候选是 `models/bc_v2_r3_leakfree.pt`（无泄漏分割 holdout top-1 57.25%），但它是 **Act 1 emulator teacher distillation**，不是实机 A10 策略；其 live action/state contract 不兼容，严禁直接部署。`bc_v2_r2` 的 60.32% 已被标为历史泄漏数字，不可用于当前决策。

## 1. 训练模型证据

| 产物 | SHA-256 | 数据/范围 | 验证指标 | 动作族/兼容性 | 判定 |
|---|---|---|---|---|---|
| `models/bc_pipeline_smoke.pt` | `e72affb4f26d9c872be5e90743aa0e94c4654218d550ba126c55588c8f274401` | `data/local/pipeline_smoke_train.jsonl`，8 条；旧 v1，CPU 3 epochs；无 holdout | 无有效评估 | 旧 visible-state/variable legal action；不等于 live JSON | **不可接入**：仅管线 smoke |
| `models/bc_v2_batch0.pt` | `745becaed6a8374069eb96a5b9ba262e5f4e1b8e709729b7b7a195d617f656cb` | `data/teacher/batch0/v2_batch0_bc.jsonl`，训练 8,301、holdout 432；`simulator_act1` | overall top-1 56.02%、top-3 78.24%；combat 51.33%、noncombat 61.17% | V2 obs 1739 / flat action 225；仿真加载接口 | **不可接入**：无 live adapter |
| `models/bc_v2_r2.pt` | `63ed48e817396fc227c668c9bf22f05cecab08dd207b23ece1fb377f874b8a01` | `data/teacher/combined_bc_r2.jsonl`，47,297 条（teacher 43,509 + DAgger 3,788） | top-1 60.32%、top-3 81.86%；combat 58.26%、noncombat 63.00% | V2 obs 1739 / action 225；历史 prefix split | **禁止使用**：85.9% holdout seed 与 train 重叠，指标作废 |
| `models/bc_v2_r3_leakfree.pt` | `aa38e3e2ac553f6d9ead8cdd705f1e1690537ed47916f33eeac08258042f4b4e` | 同上；run-grouped：训练 41,399/15,813 runs，holdout 2,110/844 runs，shared 0；reserved test seeds 未训练 | top-1 57.25%、top-3 79.05%；combat 55.28%(n=1212)、map 62.40%(125)、event 45.96%(272)、shop 34.29%(35)、rest 66.67%(6)、reward 69.35%(460) | 六 family heads：combat/map/reward/shop/rest/event；仍为 `simulator_act1` | **不可直接接入**：仅离线仿真 BC |
| `models/bc_pretrain_r3/pretrained_actor.zip` | `b0c93aedee043b974929592c5b975def9bccfd37a5be63507d3db12f4f0e5e99` | 由 r3 BC 初始化 MaskablePPO；同一 V2 数据/范围 | holdout top-1 52.84%、top-3 76.11%；combat 48.93%、event 48.90%；100-seed 仿真无完整胜利 | SB3 MaskablePPO，obs 1739/action 225；需 V2 env 才能 load | **不可直接接入**：无 live codec，且不是实机胜率证据 |

### 指标与数据解释

- `combined_bc_r2.summary.json`：47,297 merged samples，528 duplicate states resolved，0 label conflicts；冻结 teacher 是 24-step greedy rollout，replay-consistent 但未做 optimality certification。
- r3 的 holdout 是按 `(source, seed)` 整 run 分组，`shared_runs=0`、leakage=0；这使 57.25% 可作为离线诊断，但不改变仿真/实机边界。
- teacher v3 记录仍有 `expert_labels_approved:false` / `label_status:candidate`；不能把候选标签或 BC top-1 解释为最优动作。

## 2. Checkpoint 与 metrics 审计

当前 hash inventory 为 4 个 `.pt`、99 个 `.zip`（含下方所有 SB3/Smoke checkpoint 与 actor），metrics/评估 JSON 约 124 个。所有二进制相对路径与 SHA-256 见本文附录 A；未列为“通过”的 checkpoint 均不得提升为成品。

| checkpoint 家族 | 数量 | 已验证结果/范围 | 结论 |
|---|---:|---|---|
| V1 curriculum：combat smoke/official、Act1 旧 runs | 17 | 修正 `player_won` 后 100-seed checkpoint 终局胜率 0–1%；promotion 0/500 true wins | 负基线；不可用 |
| V2 smoke floor3/floor6 | 5 | floor3 boundary 约 0.95（20 seeds）；floor6 final boundary 0.5667；非完整 Act1 | 仅管线诊断 |
| V2 aborted pre-filter floor6 | 6 | 旧 mask 语义 defect 约 0.44 等；已归档 | 禁止提升 |
| V2 official floor3 | 2 | 500 untouched promotion seeds boundary 1.00，mean floor 3.0；只是阶段边界完成 | 不代表胜率 |
| V2 official floor6 | 2 | 500 seeds boundary 0.86，mean floor 5.852；通过旧 .80 gate，但不通过当前 point .93 / Wilson low .90 gate | 不可提升 |
| V2 floor10 | 4 | boundary 0.26 → 0.22 → 0.18 → 0.15；agent 学会存活/截断，未学会推进 | 失败 |
| floor6 ablation A/B/C × 3 seeds | 45 | aggregate boundary A .8933 (Wilson .8533)、B/C .9033 (Wilson .8646)，均低于 .93/.90；无完整胜利 | 诊断实验，不可提升 |
| PPO pretrain vs vanilla/warm | 15 | 100-seed floor6：pretrained .83 (Wilson .7445)、vanilla .87、warm .88；均无完整胜利；单 seed screen | 仅比较实验 |
| top-level smoke archives | 2 | smoke artifact，无可迁移指标 | 不可用 |

关键 metrics 文件：`runs/pretrain_eval/comparison-20260901T162910Z.json`（BC/actor 100-run 均无完整胜利）；`runs/ablations/floor6-20260901-review/summary.json`；V2 official `floor3/metrics/promotion.json` 与 `floor6/metrics/promotion.json`；V1 `*-reevaluated-run-win-fix.json`。这些结果的 scope 均是 simulator（`simulator_act1`/`simulator_combat`），不是实机 A10。

## 3. Live state/action contract 兼容性

| 层 | 当前实际 contract | 对 checkpoint 的影响 |
|---|---|---|
| `advisor_core.live.build_policy()` | 只接受 `heuristic` 或 `smoke`；默认 heuristic；没有 checkpoint selector / loader | V2 `.pt`/SB3 `.zip` 无入口 |
| `advisor_core.policy_live.LiveHeuristicPolicy` | 覆盖 `card_reward`、`shop`、`rest_site`、`map`；combat 主动拒绝；未知 event/Neow 抛错 | 现有局外“模型”是规则，不是训练产物；event/Neow 无训练决策 |
| `bridge.autoplay` | 直接实例化 `LiveHeuristicPolicy`；event 等有 first-option/状态机 fallback；屏幕族含 map/shop/reward/rest | fallback 不等于最优策略；不能把自动执行通过误记为模型胜率 |
| V2 emulator | 固定 1739-int observation；225 flat actions（32 base × no-target/6-enemy aliases + empty sentinel）；family 为 combat/map/reward/shop/rest/event | 必须有 live JSON→1739、225→live wire action 的严格 codec；当前不存在 |
| live wire actions | `choose_map_node(index)`, `choose_event_option`, `choose_rest_option`, `shop_purchase`, `select_card_reward(card_index)`, `skip_card_reward`, `proceed` 等 | V2 flat action index 不能直接当 live action dict，也不能直接生成 `Recommendation`/中文注释 |

版本号相同并不代表 contract 相同：训练目标是 `v0.111.0` emulator API v8，但 live 通过 STS2MCP/mod JSON；native observation 还缺部分 relic counters 与 enemy definition identities。live event/Neow 当前缺少可供策略排序的 option text。结论：**所有 checkpoint 与 live policy 均为 NO-GO，直到完成 parity adapter + gold-state tests。**

## 4. 已知失败与风险

1. R2 prefix split 的 60.32% 存在严重 run/seed leakage，已退休；不能在宣传、选模或 gate 中引用。
2. BC/actor 在 simulator floor6/floor10 仍未产生 full-run true win；floor10 出现 reward exploitation（低推进、高存活/截断）。
3. V1 的早期 2–5% 是陈旧 `player_won`（最后一场战斗）标志；修正终局定义后为 0–1% 或 0/500。
4. teacher v3 尚未获得 expert approval，样本是 candidate；现有 teacher 也不是 beam/long-horizon optimal oracle。
5. 当前实机局外策略的 card/shop/map/rest 是启发式：按稀有度/类型、删除/遗物/高稀有卡优先、HP 阈值与地图 band；不包含 run-level utility、完整事件语义或可校准置信度。

## 5. 最小后续实验（先做此项才可进入 live）

1. 复用现有 STS2MCP / AI MCP bridge 与 `TraceRecorder`，采集当前 `v0.111.0` live JSON 的 whole-run decision corpus；必须覆盖 Neow、event、card reward、shop、rest、map、treasure/relic/removal，记录 option text、合法 wire action、chosen action、run/seed provenance。
2. 两条路线择一：优先做 live-shaped variable-candidate scorer（输入原始 live state + 候选项，输出 `Recommendation`）；或实现经 gold corpus 验证的 `LiveState→1739` 与 `225→wire action` codec。禁止把现有 r3 直接接桥。
3. 先做每个 action family 的 offline top-1/top-k、合法率、覆盖率、校准和未知状态拒答；再做 20 局 current-build Ironclad A10 assisted pilot。任何 illegal/unclassified/contract mismatch 立即停止接入。
4. 通过 pilot 后预注册至少 500 个 fresh current-build full runs，报告 terminal true wins、Wilson 区间、全局与各屏幕族指标；未完成该门槛前不能声称 50%，也不能称“训练好的成品算法”。

## 6. 现有方案对照（用于复用，不重复造轮子）

- [STS2 AI MCP](https://github.com/BMingSY/sts2-ai-mcp)：AI-safe MCP + 本地 HTTP、决策/动作端点与 replay；可复用 bridge/状态采集，但不是本项目的已验证局外策略。
- [STS2MCP](https://github.com/Gennadiyev/STS2MCP)：把游戏状态/动作暴露给本地 agent；可复用 transport，不能替代策略模型。
- [sts2-combat-ai](https://github.com/ing-gom/sts2-combat-ai)：planner/simulator/scorer 的 combat 路线；适合战斗求解，不覆盖本项目所需的实机卡牌/事件/商店/火堆/地图最优策略。
- [Random Foreseer](https://steamcommunity.com/sharedfiles/filedetails/?id=3747531952) 与用户提供的 [Combat Solver](https://steamcommunity.com/sharedfiles/filedetails/?id=3790899961)：可预测 RNG/战斗路线的现成方向；不能把 combat solver 的能力外推为已训练 live out-of-combat policy。

## 附录 A：当前全部二进制产物 SHA-256

以下清单由审计时工作区直接计算，路径相对 `sts2-a10-advisor`；未列入的 sidecar/metrics JSON 不属于模型二进制。
| 路径 | SHA-256 |
|---|---|
| `checkpoints/smoke_full_run_cuda.zip` | `16476db3cb5e80f8aabeadea26b7555f81843b491599906df31887e34dc2228e` |
| `checkpoints/smoke_full_run.zip` | `66f0ef19d9079aab5b868b30d03765c91408c3e7fd106492f52e2da891b2d763` |
| `models/bc_pipeline_smoke.pt` | `e72affb4f26d9c872be5e90743aa0e94c4654218d550ba126c55588c8f274401` |
| `models/bc_pretrain_r3/pretrained_actor.zip` | `b0c93aedee043b974929592c5b975def9bccfd37a5be63507d3db12f4f0e5e99` |
| `models/bc_v2_batch0.pt` | `745becaed6a8374069eb96a5b9ba262e5f4e1b8e709729b7b7a195d617f656cb` |
| `models/bc_v2_r2.pt` | `63ed48e817396fc227c668c9bf22f05cecab08dd207b23ece1fb377f874b8a01` |
| `models/bc_v2_r3_leakfree.pt` | `aa38e3e2ac553f6d9ead8cdd705f1e1690537ed47916f33eeac08258042f4b4e` |
| `runs/ablations/floor6-20260901-review/A_seed91001/checkpoints/final.zip` | `5ee7128d0c12ffe36deba7fc7cc19c67956f85f95aefc320268700b03418f8ad` |
| `runs/ablations/floor6-20260901-review/A_seed91001/checkpoints/step_000000062508.zip` | `0a2356de86db2e7e019c44643b33ddfdf42af0723fe4ea6af9fbe6ebdb879997` |
| `runs/ablations/floor6-20260901-review/A_seed91001/checkpoints/step_000000125016.zip` | `38e8e5fd7447b06a07325679de0741b1135c4412573c4726df5353d71b61b2b3` |
| `runs/ablations/floor6-20260901-review/A_seed91001/checkpoints/step_000000187524.zip` | `0d62f1acf618e08ce6f26f66e497de526f444764b713f405c23fa3fd820fed0c` |
| `runs/ablations/floor6-20260901-review/A_seed91001/checkpoints/step_000000250032.zip` | `2325983054b4cc19f14f9f3bb4f514d5c99c8146a02e6d22c546fd92a378e38d` |
| `runs/ablations/floor6-20260901-review/A_seed91002/checkpoints/final.zip` | `03d0dec250dc107f120a7639f060dafb7bd937cd9d06ff4d0648e496a15c0801` |
| `runs/ablations/floor6-20260901-review/A_seed91002/checkpoints/step_000000062508.zip` | `46e16c82235a58c5386b41ebda5552b2e40388210d25395c0e27b20835886178` |
| `runs/ablations/floor6-20260901-review/A_seed91002/checkpoints/step_000000125016.zip` | `f369a02bfc1cc2494995769ffb7617e79cd0ef6b71987687a9a5fe60adf305b4` |
| `runs/ablations/floor6-20260901-review/A_seed91002/checkpoints/step_000000187524.zip` | `6f23d49ecbc92a2910b4bffa175b445e6f223bb13010cd6acf5618783864d252` |
| `runs/ablations/floor6-20260901-review/A_seed91002/checkpoints/step_000000250032.zip` | `a46bef03dccf1f95e12e4ed1c9380bd75fd2fb6c6c5f3c9e898114b0a5c5eb50` |
| `runs/ablations/floor6-20260901-review/A_seed91003/checkpoints/final.zip` | `9943d69550a88730b41e6050599522882c1d1c27e7ff7502e347b73c1d85fa18` |
| `runs/ablations/floor6-20260901-review/A_seed91003/checkpoints/step_000000062508.zip` | `24d378cb356737cf96670620c98d000eda58090bda4d4249e833a3c23ac93112` |
| `runs/ablations/floor6-20260901-review/A_seed91003/checkpoints/step_000000125016.zip` | `247da84cc235b92bb5a7127870fad2fa0bddf2d125466c1744f13ada3f8ccf9a` |
| `runs/ablations/floor6-20260901-review/A_seed91003/checkpoints/step_000000187524.zip` | `1cd94369f14ee1fc13ddf0e367138de4c10f8dfad9c0b48e4ad031eb51c6c5d4` |
| `runs/ablations/floor6-20260901-review/A_seed91003/checkpoints/step_000000250032.zip` | `2b0057fb220a7463226227b7678a5c2622dba745c1423cdf4afbed0b36960066` |
| `runs/ablations/floor6-20260901-review/B_seed91001/checkpoints/final.zip` | `2dd24acaeaa33f9b1973bac2d80ccc6613a3f6ddf6a38a969807f1d54af94d01` |
| `runs/ablations/floor6-20260901-review/B_seed91001/checkpoints/step_000000062508.zip` | `8a004c625b370fffbf9b0f3ec97ea31a2fd4f42ce878b936a6614c186b1412e6` |
| `runs/ablations/floor6-20260901-review/B_seed91001/checkpoints/step_000000125016.zip` | `0831580397b235007aa9f6b11cdaedbd821a863b738324efb214828c25717d4d` |
| `runs/ablations/floor6-20260901-review/B_seed91001/checkpoints/step_000000187524.zip` | `b4f97515d851e6bfb8e779fcfeea60afda47d71890bc896e589174b150c86f4c` |
| `runs/ablations/floor6-20260901-review/B_seed91001/checkpoints/step_000000250032.zip` | `43939862f0f84a8ffe97bb7465d755f82db17e75836da884f83132ae084f35f2` |
| `runs/ablations/floor6-20260901-review/B_seed91002/checkpoints/final.zip` | `3538a76015c9327ec6d016e2a5f5b9d5d72ed1c90de8abfbc5fdd8c346a59589` |
| `runs/ablations/floor6-20260901-review/B_seed91002/checkpoints/step_000000062508.zip` | `afaccb842eaf37d0719ec38231d0fdec96a997a01c921a5655d6c6c0b64f1b3d` |
| `runs/ablations/floor6-20260901-review/B_seed91002/checkpoints/step_000000125016.zip` | `75eb4b1209a8496bb9ce5037f0e5101a3901031c9be355a85fbec557484e303d` |
| `runs/ablations/floor6-20260901-review/B_seed91002/checkpoints/step_000000187524.zip` | `42c3bd881cec188c1c25ec8d83defe3d8ee69abfd2715fad1ccb1da8501569a6` |
| `runs/ablations/floor6-20260901-review/B_seed91002/checkpoints/step_000000250032.zip` | `0481258bc1d3c437202c5553dc9d589c953fb67fb6aeaea379ca389ce01ac421` |
| `runs/ablations/floor6-20260901-review/B_seed91003/checkpoints/final.zip` | `cf9abc96bf717c6bb3e17ab06d5f5084e5ea9fa4248205f72ebda95de1d57fcf` |
| `runs/ablations/floor6-20260901-review/B_seed91003/checkpoints/step_000000062508.zip` | `8d5300773be4a8479f834f43b0f1b100cc93d628459668e56472147a2a421df0` |
| `runs/ablations/floor6-20260901-review/B_seed91003/checkpoints/step_000000125016.zip` | `9a426c3201f390905ab73bd4734f54e365d1678c6f7f0c2a8c537b0e022cc64d` |
| `runs/ablations/floor6-20260901-review/B_seed91003/checkpoints/step_000000187524.zip` | `ec02f66be925f819316d13fb8475b3031b28cccd703125b8cd947bc17f6741d1` |
| `runs/ablations/floor6-20260901-review/B_seed91003/checkpoints/step_000000250032.zip` | `66ebf062fb728c8a6167eaccaf60116a2212f6a9318c123e937c84c1d25ba1bb` |
| `runs/ablations/floor6-20260901-review/C_seed91001/checkpoints/final.zip` | `98817be0ae0247a73cf8f7a413df05128591fafed593367694e5e81e00d0e807` |
| `runs/ablations/floor6-20260901-review/C_seed91001/checkpoints/step_000000062508.zip` | `7e047d63fe2079ccd2ab7fff2a7f509d4edc80d9f20f3558ecc78b6b9d8478eb` |
| `runs/ablations/floor6-20260901-review/C_seed91001/checkpoints/step_000000125016.zip` | `c8de0adfb779aa34789e85a2b0623e1903aaf8fc17743787aecec6a834199151` |
| `runs/ablations/floor6-20260901-review/C_seed91001/checkpoints/step_000000187524.zip` | `8a4002c4a30f8b9c2e072130f5653e627fbbeae66b33c14380ccade3544c495b` |
| `runs/ablations/floor6-20260901-review/C_seed91001/checkpoints/step_000000250032.zip` | `7ab37acef7995f94d0f3f932587dac3dbdfe78fb71e850a98c07e54ef803d59f` |
| `runs/ablations/floor6-20260901-review/C_seed91002/checkpoints/final.zip` | `bae6bdfa8a69c1c92219b4656c9aa7ffa76c2c504149f906efcf352eae3099fd` |
| `runs/ablations/floor6-20260901-review/C_seed91002/checkpoints/step_000000062508.zip` | `05bdfdaa3d90fbb17eeccf1de20944824e7ddc776c6f9b7c07043108aad8d802` |
| `runs/ablations/floor6-20260901-review/C_seed91002/checkpoints/step_000000125016.zip` | `b8e063ac536d5d694c169872aad5af4c37db5027fd76c3e518c92f67f3126f1a` |
| `runs/ablations/floor6-20260901-review/C_seed91002/checkpoints/step_000000187524.zip` | `c347a3ed52d67e3693954bc2eaf49b10894ae8c5c89af0663c5a8c8af979bdff` |
| `runs/ablations/floor6-20260901-review/C_seed91002/checkpoints/step_000000250032.zip` | `cbb4cf13f7f1590de14914a646145b5cf4bc4a78e82b1a7fda37f0e1e0823f73` |
| `runs/ablations/floor6-20260901-review/C_seed91003/checkpoints/final.zip` | `71f7732d3872b344510a5f12a01a04c84d954b78168db06751302cff38aa6c9b` |
| `runs/ablations/floor6-20260901-review/C_seed91003/checkpoints/step_000000062508.zip` | `82f57200db64c72eadab19e916eaa00eacd5cab9d6c95534a3756847ac4f46d7` |
| `runs/ablations/floor6-20260901-review/C_seed91003/checkpoints/step_000000125016.zip` | `39b2244a89b20ed6455cd4c6b4cbe7e3f47f234f168cb6101edf80e42a1a4e38` |
| `runs/ablations/floor6-20260901-review/C_seed91003/checkpoints/step_000000187524.zip` | `6dee1739b043284361b6b5c47edc0a1e50e73fe7f18fd843200457a645fbd326` |
| `runs/ablations/floor6-20260901-review/C_seed91003/checkpoints/step_000000250032.zip` | `36b9c8243beaddbab24ae8586e9986050682c898851332f7a928e06206a3ee79` |
| `runs/curriculum_v2_smoke/v2curriculum-20260901T050230Z/floor3/checkpoints/final.zip` | `7b1d52ee95a75e95441ddbf851f321f85943081062623976a2c533af3764e14d` |
| `runs/curriculum_v2_smoke/v2curriculum-20260901T050230Z/floor3/checkpoints/step_000000001024.zip` | `91b45795e94704444094ec628ba061626af1bdc548fbf90fea9a272797f16bd4` |
| `runs/curriculum_v2_smoke/v2curriculum-20260901T050230Z/floor3/checkpoints/step_000000002048.zip` | `bab84bd50a9a73bd60136c2866e2b231749fdeea36c997c7205d63250ec49f4f` |
| `runs/curriculum_v2_smoke/v2curriculum-20260901T050230Z/floor6/checkpoints/final.zip` | `df5b88c684858596be24a751fc145e58236bd5cfcf16d9003403b74ea01669bb` |
| `runs/curriculum_v2_smoke/v2curriculum-20260901T050230Z/floor6/checkpoints/step_000000001024.zip` | `e94d2578634e8e634b9323998d6876e8fe686c34f5ee28fe7f8fcd3bc75bbf38` |
| `runs/curriculum_v2/aborted-pre-filter-floor6-095635Z/floor6/checkpoints/step_000000500004.zip` | `9dda45ca9c2f13b8eb195a2012ebdcdebfe38c448b0abb5d2e729d25b8acf98d` |
| `runs/curriculum_v2/aborted-pre-filter-floor6-095635Z/floor6/checkpoints/step_000001000008.zip` | `fce8104147402b15cec466c06e67749516e86ed07d84afc8dd298c820e8c9bd5` |
| `runs/curriculum_v2/aborted-pre-filter-floor6-095635Z/floor6/checkpoints/step_000001500012.zip` | `b6693141ff4e71478136a8244b7d1c63bbd63e9b92621462a772ce9d62730f69` |
| `runs/curriculum_v2/aborted-pre-filter-floor6-095635Z/floor6/checkpoints/step_000002000016.zip` | `7a242d1e36bba2e9e606d2279af59b97de123b14984040ee9329d35a8a077c7b` |
| `runs/curriculum_v2/aborted-pre-filter-floor6-095635Z/floor6/checkpoints/step_000002500020.zip` | `3d52dc02bd473a913daffac6e8bcdfca9ed45b56a520d8f39c4877314427aa9c` |
| `runs/curriculum_v2/aborted-pre-filter-floor6-095635Z/floor6/checkpoints/step_000003000024.zip` | `ff3c867b1a8fda075007104ce7921b0a0360fc05fc27ff4bac857f0130e25352` |
| `runs/curriculum_v2/v2curriculum-20260901T091856Z/floor3/checkpoints/step_000000250008.zip` | `07ec858bbeca9ccc6b61913851bd2ed5f0e3bae7f89acf0eea037e0995a4df4e` |
| `runs/curriculum_v2/v2curriculum-20260901T091856Z/floor3/checkpoints/step_000000500016.zip` | `69c6c32d693f51be14e78e549b3892ee22747d84338fdb58b7cfd0449a0528db` |
| `runs/curriculum_v2/v2curriculum-20260901T115142Z/floor6/checkpoints/step_000000500004.zip` | `f325559d44f9c7431d30d17e31ec1dfaee102d3e2b79d755b534dd74f2f3e1c3` |
| `runs/curriculum_v2/v2curriculum-20260901T115142Z/floor6/checkpoints/step_000001000008.zip` | `b8deab061798daeb65bb681eebece3d54a53229bb7e6767f22c71e082e355ce4` |
| `runs/curriculum_v2/v2curriculum-20260901T122903Z/floor10/checkpoints/step_000001000008.zip` | `63f62dbc72555b01b849498d70719ae2ef4c2e50343cd91db832b778e0c489e3` |
| `runs/curriculum_v2/v2curriculum-20260901T122903Z/floor10/checkpoints/step_000002000016.zip` | `239e20ab4c48512f3a18bbae922328bb941601fa2761082c028ad571bcf5bb8c` |
| `runs/curriculum_v2/v2curriculum-20260901T122903Z/floor10/checkpoints/step_000003000024.zip` | `2d112dafa790543c0daf778f71781be6c3fc94f6aabb0a2ae40f994759b45731` |
| `runs/curriculum_v2/v2curriculum-20260901T122903Z/floor10/checkpoints/step_000004000032.zip` | `538fbac62458b36bf4f461ae5c92ec39739720611041037388feb0fbbf8af10c` |
| `runs/curriculum-smoke/curriculum-20260831T135922Z/combat/checkpoints/final.zip` | `0512b3af1d93726134790334f8259fb9be916c1aef31d028ba86f31d4ed21327` |
| `runs/curriculum-smoke/curriculum-20260831T135922Z/combat/checkpoints/step_000000000128.zip` | `8d90b3c0863cac508b6ff3e7ec60eff75711b8aca9f5f099a56008c9163da87a` |
| `runs/curriculum-smoke/curriculum-20260831T135922Z/combat/checkpoints/step_000000000256.zip` | `fa8403699660e0fc83b8c15cb0cd482651f4eb49f0fcf7efc333ce984a56fa26` |
| `runs/curriculum/curriculum-20260831T140450Z/combat/checkpoints/step_000000500004.zip` | `f851b42bb2962d92e44245cf5b08ea343fd0738ee6aa9eac03031714b729a684` |
| `runs/curriculum/curriculum-20260831T140450Z/combat/checkpoints/step_000001000008.zip` | `1ba2032442ed1e7a0515b2bd2f6fc9ad170d7e35063cecc1a78ad022cf5f24b5` |
| `runs/curriculum/curriculum-20260831T140450Z/combat/checkpoints/step_000001500012.zip` | `57d31bd18aaabf2d4d1e99bebf856be00e4fffd831f1b7e05b1cca22ce839842` |
| `runs/curriculum/curriculum-20260831T140450Z/combat/checkpoints/step_000002000016.zip` | `7b5c3e6a49fa6b6a05a701d40e8ab7b1759e3c2de08f13cd0c1d1cfe453b0ad5` |
| `runs/curriculum/curriculum-20260831T163118Z/act1/checkpoints/step_000002000004.zip` | `ee8c9bcf719343d3c3fb7427af431f15e03f621598a4a1003053c97471eb95a0` |
| `runs/curriculum/curriculum-20260831T205351Z/act1/auto-restart-artifacts/step_000006000000.zip` | `041e4d186388e74daf372f32b2c62cd5ce224476c1b5baeb1b6334a0358d9335` |
| `runs/curriculum/curriculum-20260831T205351Z/act1/checkpoints/step_000002000004.zip` | `b2f442464e959b4778e87781a6839666ceea96d04f44ba9e071d2ea330819b51` |
| `runs/curriculum/curriculum-20260831T205351Z/act1/checkpoints/step_000004000008.zip` | `c62dfdaac93bb8210940571c0728e7a5358097f0ae274263eb263e7c28542576` |
| `runs/curriculum/curriculum-20260831T205351Z/act1/checkpoints/step_000006000012.zip` | `205186459f75bd813bbcdbe2f5c1ae9038ee269f6f4a948845e738981f747345` |
| `runs/curriculum/curriculum-20260831T205351Z/act1/checkpoints/step_000008000016.zip` | `b7801a091ecc7a4f4953c0c2ad317a801a82ac34822a7fd2345f1ef7a6f140d5` |
| `runs/curriculum/curriculum-20260831T205351Z/act1/checkpoints/step_000010000020.zip` | `3c4dc5d75c61210659bafbf143a719b22f69d7172e8e703d26c4891602f0fd5b` |
| `runs/curriculum/curriculum-20260831T205351Z/act1/checkpoints/step_000012000000.zip` | `694a77abf78f2c45cc84fc2d2bb065f64e3fa9278636ab42f4e3db72f8736f2e` |
| `runs/curriculum/curriculum-20260831T205351Z/act1/checkpoints/step_000014000004.zip` | `8cf52b408e9e25d4c58def81a3beaf294f95ae637a504b339f08d67de239d70f` |
| `runs/curriculum/curriculum-20260831T205351Z/act1/checkpoints/step_000016000008.zip` | `5659512bdfbf2e9747451a51fa7acf41dd6976fee1dca1862a8c66a9d7d7f868` |
| `runs/ppo_compare/ppo-pretrain-vs-vanilla/pretrained_seed91001/checkpoints/final.zip` | `2c6ca22dece80859c9d600332385d8e36b9eaf5642865e62718ef84f4a0c0589` |
| `runs/ppo_compare/ppo-pretrain-vs-vanilla/pretrained_seed91001/checkpoints/step_000000062508.zip` | `9b0322baddad34cf2b1b8edc4a11e6bfa9ab0a320f5b6f7e769d33e7dbd9db98` |
| `runs/ppo_compare/ppo-pretrain-vs-vanilla/pretrained_seed91001/checkpoints/step_000000125016.zip` | `332e2e10d943c4dfce40b80a95e193db09f83eebbe05c3d515d91b37b7183879` |
| `runs/ppo_compare/ppo-pretrain-vs-vanilla/pretrained_seed91001/checkpoints/step_000000187524.zip` | `534ab9bd38d54606e923bc2adabb50c4a8766958dfb22ab228f4e3857fae1f57` |
| `runs/ppo_compare/ppo-pretrain-vs-vanilla/pretrained_seed91001/checkpoints/step_000000250032.zip` | `b8fed57f2b2d766f703e34f56c587b3ec6f4f9b3cd74faaf759f34179e00dc20` |
| `runs/ppo_compare/ppo-pretrain-vs-vanilla/vanilla_seed91001/checkpoints/final.zip` | `b74013c4e9c6cbe3b37a1b2122491b8bd765efbd26732bb0f67c83ee3f326e33` |
| `runs/ppo_compare/ppo-pretrain-vs-vanilla/vanilla_seed91001/checkpoints/step_000000062508.zip` | `9de61e4d5aa081a21f491d2e344e2885fab84a291c91ad6ccd7c730fb93fc5a0` |
| `runs/ppo_compare/ppo-pretrain-vs-vanilla/vanilla_seed91001/checkpoints/step_000000125016.zip` | `6285e2f8510a096820c6fb2572d254c5098ec6a6b47832a8fad6d985aaa6ab0d` |
| `runs/ppo_compare/ppo-pretrain-vs-vanilla/vanilla_seed91001/checkpoints/step_000000187524.zip` | `9e8b3b88bb985d3efdbbc2533827e3331d79135e649d5955c8c12f9983acec55` |
| `runs/ppo_compare/ppo-pretrain-vs-vanilla/vanilla_seed91001/checkpoints/step_000000250032.zip` | `677013adc0ca5e0fff6c307693980a52be16e6b63944cee1e27c63377a5a7db1` |
| `runs/ppo_compare/ppo-pretrain-vs-vanilla/warm_seed91001/checkpoints/final.zip` | `c825ae7f6485b3fafc551eeac8ee41e361b882615a36e7141f9eb2c7922b660a` |
| `runs/ppo_compare/ppo-pretrain-vs-vanilla/warm_seed91001/checkpoints/step_000000062508.zip` | `88977217a24dd38eeda79f61fd1e9f5efe850b4193d22fffe05c7fe438054a4c` |
| `runs/ppo_compare/ppo-pretrain-vs-vanilla/warm_seed91001/checkpoints/step_000000125016.zip` | `eec6f45a7d70f111357a679489b266d8016d4bb8353fe51fd915e8a61300ab53` |
| `runs/ppo_compare/ppo-pretrain-vs-vanilla/warm_seed91001/checkpoints/step_000000187524.zip` | `b5fd8e44ac41fe143f41f1ebd4fe29a38baa18a00513201ddcaeb53cd0234881` |
| `runs/ppo_compare/ppo-pretrain-vs-vanilla/warm_seed91001/checkpoints/step_000000250032.zip` | `f382e6333ae9ce48adecd1ba8703ae62613735b46b90df352a25d371211adbac` |


