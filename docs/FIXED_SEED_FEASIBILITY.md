# STS2 v0.111.0 固定 Seed 可行性备忘录

更新时间：2026-09-02。范围为标准单人 Ironclad A10；本备忘录只验证开局注入链路，未启动正式 50-battle，也未覆盖已安装 DLL。

## 结论

固定 seed 在当前游戏版本存在可复用的公开内部入口，原来的阻塞来自 STS2MCP 动作实现，而不是游戏引擎不支持标准单人 seed。源码副本已提交最小候选补丁并完成 staging 编译，但它尚未安装或通过真机启动验收，因此当前 `config/live_version.lock.json` 仍代表旧的已安装桥接，不能据此宣称 Phase B 已解锁。`fixed_battle_seeds.json` 明确记录 `candidate_bridge_supported=true`、`installed_bridge_supported=false`；只有候选桥接安装并更新 lock，且 authoritative `current_run.seed` 回读匹配后，fixed run 才有效。

## 代码/反编译证据

在已安装的 `data_sts2_windows_x86_64/sts2.dll` 与 XML 文档中确认：

1. `NCharacterSelectScreen.BeginRun(string seed, List<ActModel> acts, IReadOnlyList<ModifierModel> modifiers)` 是公开入口。
2. 该入口的标准单人状态机最终调用
   `NGame.Instance.StartNewSingleplayerRun(character, true, acts, modifiers, seed, GameMode.Standard, ascension, null)`。
3. `RunState.CreateForNewRun` 把 seed 传给 `RunRngSet(string seed)`；`RunRngSet.StringSeed` 保存传入的原始字符串，seed 不会被强转为整数或以 hash 替代显示值。
4. 原生 `StartRunLobby.BeginRunLocally` 的 Act 选择流为：
   `new Rng(StringHelper.GetDeterministicHashCode(canonicalSeed), "act_selection")`，再调用
   `ActModel.GetRandomList(rng, GetUnlockState(), NetService.Type.IsMultiplayer())`，并以 lobby 的 Act1 覆盖第一个 act。`GetUnlockState`/`GetAct` 在游戏 DLL 内是 private；候选补丁用公开 `lobby.Players`、`UnlockState.FromSerializable`、`new UnlockState(...)` 及 `ModelDb.Act<Overgrowth/Underdocks>()` 逐字复现 v0.111.0 的两个小方法，不依赖反射。
5. 标准单人确实拥有本地 `StartRunLobby`，但调用 `SetSeed` 会触发 character-select 的 `SeedChanged()`；v0.111.0 该回调直接抛出 `Seed should not be changed in standard mode!`。因此旧版 `menu_select(seed=...)` 的拒绝是合理的防护，但不是最终技术边界。

## 候选补丁

源码副本 `third_party/STS2MCP-main/McpMod.Actions.cs` 的 seeded standard-SP 分支：

- 用游戏自己的 `SeedHelper.CanonicalizeSeed` 规范化输入；只接受非空 ASCII 字母数字结果。
- 不调用标准单人的 `StartRunLobby.SetSeed`，而是复现原生 Act 选择流后调用公开 `NCharacterSelectScreen.BeginRun`。
- 响应同时保留 `seed_requested`（调用者原文）、`seed_canonical`（游戏使用值）和 `seed_injection`，并明确 `seed_verified=false`；随后必须读取 authoritative `current_run.seed`。
- Multiplayer 和无 seed 的原有 UI 路径不变。

候选源码已用随项目提供的 .NET SDK 针对当前游戏 DLL 在 staging 输出目录完成编译：
`G:\ds harness\sts2-a10-advisor\artifacts\sts2mcp-seeded\STS2_MCP.dll`，0 warning / 0 error。候选 DLL SHA-256 为 `CD3EA7409F5AC6973DF3D1B4A5EDC0D5C9A1B6555A8C448F276824D9CB943A4D`；线上已安装 DLL 仍为 `095EE091F6D20D17FC0FC09AF53B46DE092F95B29FB970FF0D8714D14CC5B124`，未被覆盖。

Python 控制器 `bridge/trace_controller.py` 增加了 `start-ironclad-a10 --seed <raw>`：POST 保留原始字符串，并在返回“started”前于有界超时内轮询 `current_run.save`。它把已验证 live state 的 `run`/`player` 与 compendium 的 `current_run` 合并为完整 `run_identity`（`run_id`、`seed`、`character`、`ascension`、`game_mode`，附带 `save_scope`/字符来源）；双方同时出现的字段不一致立即失败，缺失字段到超时失败。seed mismatch 立即停止，不能把未设置 seed 当作成功；compendium 缺 `run_id` 时会回退到 live state 的真实 `run_id`，两边都缺失则 fail-closed。

## Seed allocation 规则

当前预注册文件 `data/combat_solver/fixed_battle_seeds.json` 是 12 个十进制 run seed：`1600000000`–`1600000011`。生成脚本 `scripts/make_solver_comparison_seeds.py` 使用固定的、无 RNG 的连续区间，因此 allocation 可复现且不与已登记区间重叠。调用时必须把 seed 作为字符串传入（例如 `"1600000000"`），不能在桥接层转为整数或从 run id 推测。

字母数字 seed 的规则是：allocation 保存调用者原文；游戏按 v0.111.0 `SeedHelper.CanonicalizeSeed` 去首尾空白、转大写，并把显示字母 `O`/`I` 消歧为 `0`/`1`。评估 partition 应同时登记 `requested` 与 `canonical`，以 canonical 作为与 authoritative `current_run.seed` 的比较键；两者都必须进入 trace。不能把字母数字 seed hash/转 int 后塞回当前整数 partition。当前 runner 仍只接受注册的整数 partition，所以字母数字 allocation 在 runner 扩展前只能 observational，不能产生 fixed-seed claim。`SeedHelper.CanonicalizeSeed` 的 `string -> string` 与 `NCharacterSelectScreen.BeginRun(string, List<ActModel>, IReadOnlyList<ModifierModel>)` 均被上述 staging 编译实际解析。

## 安装前验收条件

源码候选补丁只有完成以下步骤后才能替换版本锁中的已安装桥接：

1. 在 staging 输出目录构建成功；禁止直接写入游戏 `mods`。
2. 对一个预注册十进制 seed 和一个字母数字 seed 各执行一次全新标准单人 Ironclad A10 启动。
3. 从 `current_run.save`/`GET /api/v1/compendium` 回读非空 seed：十进制必须与原文一致；字母数字必须与游戏 canonical 一致，trace 同时留存原文。
4. 证明缺 seed、空 seed、旧 DLL（仍返回“不支持标准单人 seed”）以及回读 mismatch 都会失败关闭，不产生 fixed-seed claim。
5. 更新桥接 DLL hash、seed-injection capability 和版本锁后，才可启动正式 Phase B；本轮没有执行该批次。

## 2026-09-19 冲突记录：已安装桥接的二进制里有种子注入代码

`installed_bridge_supported: false` 这条记录，与**当前实际安装的那个 DLL** 的静态证据
相矛盾。核对过程（全程只读，未启动游戏、未发任何 POST）：

- 安装路径上的文件 `G:\SteamLibrary\steamapps\common\Slay the Spire 2\mods\STS2_MCP.dll`
  的 SHA-256 前缀是 `CD3EA7409F5AC697…`，**与 `config/live_version.lock.json` 里锁定的
  就是同一个文件**（不是候选构建，也不是 staging 目录里的副本）。
- 该文件的字符串表里存在这些条目（UTF-16 与 ASCII 各扫一遍）：
  `seed`、`seed_requested`、`seed_canonical`、`seed_injection`、`seed_verified`、
  `/api/v1/singleplayer`，以及一整套只会在**已实现**的分支里出现的报错文案：
  `Seeded embark requires a non-empty alphanumeric seed`、
  `Seeded embark requires a selected character`、
  `Seeded embark requires an active start-run lobby`、
  `Seeded embark could not resolve the standard act list`、
  `Seeded embark failed before starting the run: `，和日志行 `Embarking on run (seed: `。

**这不等于 Phase B 已解锁。** 字符串在二进制里，只说明代码路径被编进去了，
**不能证明运行期那个端点真的接受 `seed` 并回填 authoritative `current_run.seed`**。
本项目的一贯口径是：没有真机回读匹配，就不写"支持"。所以：

- **我没有改 `installed_bridge_supported` 的值**，也没有改断言它的
  `tests/test_seed_allocation.py`。在无真机证据的情况下把 false 翻成 true，
  正是这个仓库最不能犯的那种错。
- 但这条记录现在**已知与静态证据冲突**，不该再被当作"已核实为 false"引用。
  它的准确状态是"未安装验收、且二进制证据倾向于可用"。

一次真机调用就能判定（需要操作员先把游戏开着；这一步会**启动一局**，
所以不由我在无人值守下做）：

```
# 1) 只读：确认桥接活着、看当前字段形状
python scripts/supervise_solver_batch.py --mode observational
# 2) 判定：用已注册的整数 seed 起一局，并核对回读
python scripts/run_solver_comparison.py --dry-run          # 先看它是否接受该 seed
python scripts/run_solver_comparison.py --max-battles 1    # 实跑一局
```

判定标准（任一不满足就仍是 false）：响应里 `seed_requested` / `seed_canonical`
都等于请求值、`seed_injection` 为真、随后 authoritative
`compendium.current_run.seed` 与 canonical 值一致。三者齐了才可以把
`installed_bridge_supported` 改为 true 并更新该测试。

**为什么值得操作员花这一分钟**：`docs/COMBAT_SOLVER.md` 把 Phase B 正式阶段
标记为 BLOCKED 的理由就是"没有种子注入端点"；而 Phase B 的判定结论，是
`docs/FREEZE_2026-09-02.md` 解冻 Combat PPO 路线的**第一个**条件。
也就是说，这一条静态矛盾同时卡着"固定种子真机对比"和"PPO 训练能否继续"两件事。
