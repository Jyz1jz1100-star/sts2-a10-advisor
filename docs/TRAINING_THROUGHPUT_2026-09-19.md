# 训练吞吐实测与提速方向（2026-09-19）

回应"加快训练"：先测清瓶颈在哪，再决定改什么。全部为离线模拟器测量，
未启动游戏、未发 HTTP、未占用 promotion/final 种子分区，探针只读
`config/training_v2_smoke.toml` 的种子区间。

## 1. 单机吞吐基线

`training.v2_curriculum --only-stage floor3`，12 并行环境，102400 步，
每轮打印的 `time/fps` 稳定在 **369–374**，取 **~370 步/秒** 作为 V2 栈的
单跑稳态值。外推：

| 预算 | 单跑墙钟 |
|------|----------|
| 1M 步 | ~45 min |
| 4M 步 | ~3 h |
| 8M 步 | ~6 h |
| 100M 步 | ~75 h（3.1 天） |

历史文档里的 ~1180 fps 属于 V1/战斗阶段那套栈，与 V2 的 1739 维扩展观测 +
225 维 (action,target) 平坦空间不可直接比较，不能拿来估 V2 阶段耗时。

## 2. 分层成本（`scripts/probe_v2_step_cost.py`）

同一台机器、单环境、随机合法动作、reset 计入速率：

```
        layer    steps  resets   steps/s   ms/step
          raw     6390     147     531.2      1.88
        stack     3797     153     316.4      3.16
stack+ppo cpu     3837     155     319.0      3.13
```

- 原生 env（含重开一局）1.88 ms/步；V2 契约栈再加 1.28 ms/步（观测编码 +
  平坦动作 + 奖励/楼层包装），即 **约 40% 的时间在 Python 侧的 V2 包装层**。
- 未训练 MaskablePPO 的逐步 `predict` 在 CPU 上测不出额外开销（3.13 vs 3.16），
  所以**学习器不是瓶颈**。

## 3. 在单个 run 内加并行，收益很小（`scripts/probe_vecenv_scaling.py`）

24000 步、随机合法动作、reset 计入：

```
   kind  envs    steps   seconds   steps/s  resets/s  vs 4 envs
  dummy     4    24000      52.3     458.8     18.20      1.00
  dummy    12    24000      49.6     484.2     19.19      1.06
subproc     4    24000      49.3     486.5     19.30      1.00
subproc    12    24000      44.5     539.9     21.39      1.11
```

`DummyVecEnv`（课程表现在用的）12 环境只比 4 环境快 6%；换成
`SubprocVecEnv`（各进程独立、绕开 GIL 与 native 进程内共享态）也只到 +11%。
**因此改写 vec-env 类型不是提速方向**——尽管 §5 会说它仍有非性能上的价值。

一次自我更正值得记录：本探针初版没有在终止时 reset，步进"死环境"几乎免费，
于是错误地报出 ~8000 步/秒并得出"env 不是瓶颈"。当前版本按真实栈重开计数，
结论才成立。教训：**测量代码本身要作为可疑对象复核**，短预算尤其会把假象
当成结论。

## 4. 真正的杠杆：同机并发多个独立 run

每份配置只改 `run_dir` 与 train 种子起点（分区互不重叠），并发跑：

| 并发 run 数 | 每 run fps | 合计 fps | 相对单跑 |
|-------------|------------|----------|----------|
| 1 | ~370 | 370 | 1.0× |
| 4 | 290–360 | 1238 | 3.3× |
| 8 | 266–331 | 2332 | 6.3× |
| 12 | ~287 | **3445** | **9.3×** |

12 并发时 GPU 利用率 11%、显存 2449/12282 MiB（i5-13600KF，14 核 / 20 线程，
32 GB）。也就是说单个 run 远吃不满这台机器，**"把 8M 步做快"的正确问法是
"把 8 个独立 run 同时做"**：零代码改动，聚合吞吐 ~9×，边际收益在 12 并发改
配置时仍未见拐点。

适用形态（都是彼此独立的 job，天然可并发）：

- 多 PPO 种子的消融（STATUS 记录过 floor10 退化正是单种子结论）；
- 课程阶段内的多分段并行推进；
- teacher batch 生成本来就按 `--shards 10` 分片，同理并发；
- BC/预训练的多种子重跑。

限制与前提：并发数受 14 核与内存约束，且这台机器**与真人用户共用**
（`docs/STATUS.md` 记录过 keeper 不得抢前台的约束）；批量并发前要留出交互余量，
不要把 20 个线程全占满。

落地入口是 `scripts/run_curriculum_fanout.py`：它按模板复制出 N 份配置（只移动
各阶段 `train` 分区的种子起点并保留阶段间原有偏移，评估分区保持共享以便横向比较），
在启动前用 `training.teacher_v3.assert_seeds_outside_frozen_lineages` 拒绝任何落入
冻结 R2 谱系或 `>= 1_410_000_000` 保留 holdout 的种子，再并发拉起并汇总退出码。
自检：`--seed-base 1700000000` 被拒（保留区），`--seed-base 1200000000 --jobs 2
--steps 2048` 两个 job 均 exit 0（29 s 墙钟）。

## 5. 顺带澄清的一个正确性问题

`DummyVecEnv` 让 12 个 env 实例共享同一进程里的 native C# 引擎状态，这正是
STATUS 里"跨进程复现不一致"（同 checkpoint + 同种子，训练进程内评估 5 胜、
新进程评估 0 胜）的头号嫌疑。§3 说明改成 `SubprocVecEnv` **不解决速度**，
但它把 native 态隔到各自进程，属于该正确性问题的候选缓解手段——要作为
*复现性*修复来评估（需要独立验证同种子跨进程一致性），不要作为性能优化来上。

## 6. 复现

```powershell
# 分层成本
& "<emulator venv>\python.exe" scripts/probe_v2_step_cost.py --seconds 12
# vec-env 扩展性（务必保留"终止即 reset"的计数口径）
& "<emulator venv>\python.exe" scripts/probe_vecenv_scaling.py `
  --steps-per-env 24000 --counts 4 12 --kinds dummy subproc
```

并发吞吐测试用的临时配置在 `runtime/fps_probe/`（gitignore 内，可再生）。
