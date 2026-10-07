"""Watch the training batch that is running right now, from its own artifacts.

What a live batch leaves on disk is small: a stable-baselines3 console dump (one block per
rollout iteration) on stdout, a ``plan.json`` at the run root, and -- *only at a checkpoint* --
an evaluation payload under the stage directory. Between checkpoints there is no on-disk episode
signal at all. So this panel separates three things it must never blur: what the log states,
what it derives from those stated fields, and what it cannot see (which it lists on the page).

Gate verdicts are produced by ``training.promotion.decide_promotion``, the code that owns the
judgement, rather than re-implemented here -- a monitor that rounds its own thresholds is how a
green panel and a red run started disagreeing about the same file.

Stdlib-only, deliberately: the training interpreter is busy, and the runtime environment this
repo installs is requests + PyYAML, so a dashboard must not be a reason to install anything.

    python scripts/train_dashboard.py --serve              # panel on http://127.0.0.1:8899
    python scripts/train_dashboard.py --once               # one state document, no server
"""

from __future__ import annotations

import argparse
import csv
import http.server
import io
import json
import re
import subprocess
import sys
import threading
import time
import tomllib
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from training.config import PromotionConfig  # noqa: E402
from training.metrics import EvaluationMetrics  # noqa: E402
from training.promotion import decide_promotion  # noqa: E402

DASH_RULE = re.compile(r"^-{6,}\s*$")
NUMBER_RULE = re.compile(r"^-?(\d+\.\d*|\.\d+|\d+)([eE][-+]?\d+)?$")
TAIL_BYTES = 8_000_000


def parse_number(text: str) -> int | float | None:
    if not NUMBER_RULE.match(text):
        return None
    value = float(text)
    return int(value) if value.is_integer() and "." not in text and "e" not in text.lower() else value


def parse_console_dump(text: str) -> dict:
    """Read sb3 ``| key | value |`` blocks into one record per iteration.

    Sections (``time/``, ``train/``, anything a future callback adds) are kept as named groups
    instead of being flattened into a whitelist, so a new logged field shows up in the panel
    rather than being silently dropped by a parser that only knows yesterday's fields.
    """
    blocks: list[dict[str, dict[str, object]]] = []
    rows_skipped = 0
    section = ""
    current: dict[str, dict[str, object]] = {}
    for line in text.splitlines():
        stripped = line.strip()
        if DASH_RULE.match(stripped):
            if current:
                blocks.append(current)
            current = {}
            section = ""
            continue
        if not stripped.startswith("|"):
            continue
        cells = [cell.strip() for cell in stripped.strip("|").split("|")]
        if len(cells) == 2 and cells[0].endswith("/") and cells[1] == "":
            section = cells[0][:-1].rstrip("/")
            continue
        if len(cells) == 1 and cells[0].endswith("/"):
            section = cells[0][:-1].rstrip("/")
            continue
        if len(cells) == 2 and section and cells[0]:
            value = parse_number(cells[1])
            if value is None:
                value = cells[1]
            current.setdefault(section, {})[cells[0]] = value
            continue
        rows_skipped += 1
    return {"blocks": blocks, "rows_skipped": rows_skipped,
            "trailing_block_open": bool(current)}


def _flat(block: dict[str, dict[str, object]]) -> dict[str, object]:
    return {key: value for group in block.values() for key, value in group.items()}


def series_from_blocks(blocks: list[dict]) -> list[dict[str, object]]:
    """Progress points: only blocks that state a timestep count are part of the curve."""
    points = []
    for index, block in enumerate(blocks):
        row = _flat(block)
        if not isinstance(row.get("total_timesteps"), (int, float)):
            continue
        points.append({
            "index": index,
            "total_timesteps": row["total_timesteps"],
            "time_elapsed": row.get("time_elapsed"),
            "iterations": row.get("iterations"),
            "fps": row.get("fps"),
            "approx_kl": row.get("approx_kl"),
            "clip_fraction": row.get("clip_fraction"),
            "entropy_loss": row.get("entropy_loss"),
            "explained_variance": row.get("explained_variance"),
            "value_loss": row.get("value_loss"),
            "policy_gradient_loss": row.get("policy_gradient_loss"),
            "loss": row.get("loss"),
            "n_updates": row.get("n_updates"),
            "learning_rate": row.get("learning_rate"),
        })
    return points


def progress(points: list[dict[str, object]], stage: dict[str, object]) -> dict[str, object]:
    """Turn the log's own stated fields into position and an explicitly-labelled ETA range."""
    out: dict[str, object] = {
        "blocks_with_timesteps": len(points),
        "total_timesteps": None,
        "target_timesteps": stage.get("timesteps"),
        "percent_of_target": None,
        "time_elapsed_s": None,
        "fps_last": None,
        "fps_mean_stated_run": None,
        "fps_recent": None,
        "remaining_steps": None,
        "eta_s_from_mean_fps": None,
        "eta_s_from_recent_fps": None,
        "next_checkpoint_at": None,
        "eta_s_to_next_checkpoint": None,
        "iterations_logged": None,
        "steps_per_iteration": None,
        "notes": [],
    }
    if not points:
        out["notes"].append("还没有解析到任何迭代块；所有位置字段都是空，不是 0")
        return out
    last = points[-1]
    steps = last["total_timesteps"]
    elapsed = last.get("time_elapsed")
    out["total_timesteps"] = steps
    out["time_elapsed_s"] = elapsed
    out["iterations_logged"] = last.get("iterations")
    n_steps, envs = stage.get("n_steps"), stage.get("parallel_envs")
    if isinstance(n_steps, int) and isinstance(envs, int):
        out["steps_per_iteration"] = n_steps * envs
    if isinstance(last.get("fps"), (int, float)) and last["fps"]:
        out["fps_last"] = last["fps"]
    target = stage.get("timesteps")
    if isinstance(target, int) and target > 0:
        out["percent_of_target"] = round(100.0 * steps / target, 2)
    if isinstance(elapsed, (int, float)) and elapsed > 0:
        out["fps_mean_stated_run"] = round(steps / elapsed, 2)
    window = [p["fps"] for p in points[-10:] if isinstance(p.get("fps"), (int, float)) and p["fps"]]
    if len(window) >= 2:
        span = [p for p in points[-10:] if isinstance(p.get("time_elapsed"), (int, float))]
        if len(span) >= 2 and span[-1]["time_elapsed"] > span[0]["time_elapsed"]:
            gained = span[-1]["total_timesteps"] - span[0]["total_timesteps"]
            seconds = span[-1]["time_elapsed"] - span[0]["time_elapsed"]
            if seconds > 0 and gained >= 0:
                out["fps_recent"] = round(gained / seconds, 2)
    if isinstance(target, int) and target > 0 and isinstance(steps, (int, float)):
        remaining = max(0, target - steps)
        out["remaining_steps"] = remaining
        mean = out["fps_mean_stated_run"]
        recent = out["fps_recent"]
        if isinstance(mean, (int, float)) and mean > 0:
            out["eta_s_from_mean_fps"] = round(remaining / mean)
        if isinstance(recent, (int, float)) and recent > 0:
            out["eta_s_from_recent_fps"] = round(remaining / recent)
        every = stage.get("checkpoint_every_steps")
        if isinstance(every, int) and every > 0:
            nxt = ((int(steps) // every) + 1) * every
            if isinstance(target, int) and nxt <= target:
                out["next_checkpoint_at"] = nxt
                rate = out["fps_recent"] or out["fps_mean_stated_run"]
                if isinstance(rate, (int, float)) and rate > 0:
                    out["eta_s_to_next_checkpoint"] = round((nxt - steps) / rate)
                else:
                    out["notes"].append(
                        "下一个 checkpoint 已定位，但没有可测的速度来给它定时")
    out["notes"].append(
        "ETA 是拿剩余步数除以本次运行自己的 fps 外推出来的，日志里并没有这个估计")
    return out


def iteration_period(points: list[dict[str, object]]) -> float | None:
    """Median wall seconds between logged iterations, measured from the log's own clock."""
    stamps = [p["time_elapsed"] for p in points
              if isinstance(p.get("time_elapsed"), (int, float))]
    deltas = [b - a for a, b in zip(stamps, stamps[1:]) if b > a]
    if not deltas:
        return None
    deltas.sort()
    return deltas[len(deltas) // 2]


def liveness(log_path: Path | None, points: list[dict[str, object]], now: float) -> dict:
    out: dict[str, object] = {
        "log_path": None,
        "log_bytes": None,
        "log_mtime_s": None,
        "seconds_since_log_write": None,
        "median_iteration_period_s": iteration_period(points),
        "stale_threshold_s": None,
        "threshold_basis": None,
        "state": "unknown",
        "reason": "未提供日志路径",
    }
    if log_path is None:
        return out
    out["log_path"] = _rel(log_path)
    try:
        stat = log_path.stat()
    except OSError as exc:
        out["reason"] = f"读不到日志属性：{exc}"
        return out
    out["log_bytes"] = stat.st_size
    out["log_mtime_s"] = stat.st_mtime
    out["seconds_since_log_write"] = round(now - stat.st_mtime, 1)
    period = out["median_iteration_period_s"]
    if isinstance(period, (int, float)):
        threshold = max(2.5 * period, 300.0)
        out["threshold_basis"] = (
            f"实测迭代周期中位数 {period:.0f} 秒的 2.5 倍"
            if 2.5 * period >= 300.0 else
            f"300 秒下限：实测周期 {period:.0f} 秒的 2.5 倍还不到它")
    else:
        threshold = 900.0
        out["threshold_basis"] = "900 秒兜底值：这份日志目前还测不出迭代间隔"
    out["stale_threshold_s"] = round(threshold, 1)
    if (seconds := out["seconds_since_log_write"]) is not None:
        if seconds <= threshold:
            out["state"] = "writing"
            out["reason"] = "日志在停滞阈值内写过"
        else:
            out["state"] = "stale"
            out["reason"] = f"日志已沉默 {seconds:.0f} 秒，阈值是 {threshold:.0f} 秒"
    return out


PROCESS_MATCH = "training.v2_curriculum"
_process_cache: dict[str, object] = {"state": "not-probed-yet", "matches": [], "detail": None}
_process_lock = threading.Lock()


def probe_processes() -> dict:
    """List the interpreter processes running this module, from the OS, not from our own log."""
    if not sys.platform.startswith("win"):
        return {"state": "unsupported", "detail": "process probe is written for Windows",
                "matches": []}
    script = (
        "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | "
        "Where-Object { $_.CommandLine -like '*v2_curriculum*' } | "
        "ForEach-Object { $p = Get-Process -Id $_.ProcessId -ErrorAction SilentlyContinue; "
        "[pscustomobject]@{Pid=$_.ProcessId; CpuSeconds=$p.CPU; WorkingSetMiB="
        "[math]::Round($p.WorkingSet64/1MB,1); Started=$p.StartTime; CommandLine=$_.CommandLine} } "
        "| ConvertTo-Csv -NoTypeInformation"
    )
    try:
        done = subprocess.run(["powershell", "-NoProfile", "-Command", script],
                              capture_output=True, text=True, timeout=45)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"state": "probe-failed", "detail": f"{type(exc).__name__}: {exc}", "matches": []}
    if done.returncode != 0:
        return {"state": "probe-failed", "detail": done.stderr.strip()[:400], "matches": []}
    body = "\r\n".join(done.stdout.splitlines())
    try:
        rows = list(csv.DictReader(io.StringIO(body)))
    except csv.Error as exc:
        return {"state": "probe-failed", "detail": f"unparseable probe output: {exc}",
                "matches": []}
    matches = []
    for row in rows:
        try:
            pid = int(row.get("Pid") or 0)
        except ValueError:
            pid = 0
        matches.append({
            "pid": pid,
            "cpu_seconds": _num(row.get("CpuSeconds")),
            "working_set_mib": _num(row.get("WorkingSetMiB")),
            "started": (row.get("Started") or "").split(".")[0],
            "command_line": (row.get("CommandLine") or "").strip()[:240],
        })
    matches.sort(key=lambda row: (-(row["cpu_seconds"] or 0.0), row["pid"]))
    return {"state": "ok", "detail": None, "matches": matches}


def refresh_processes(interval_s: float, stop: threading.Event) -> None:
    while not stop.is_set():
        with _process_lock:
            global _process_cache
            _process_cache = probe_processes()
            _process_cache["probed_at"] = time.time()
        stop.wait(interval_s)


def _num(text: object) -> float | None:
    try:
        return float(text)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _rel(path: Path) -> str:
    try:
        return path.resolve().relative_to(ROOT).as_posix()
    except ValueError:
        return str(path)


def read_json(path: Path) -> tuple[dict | None, str | None]:
    try:
        return json.loads(path.read_text(encoding="utf-8")), None
    except FileNotFoundError:
        return None, f"{_rel(path)} does not exist"
    except (OSError, json.JSONDecodeError) as exc:
        return None, f"{_rel(path)}: {type(exc).__name__}: {exc}"


def load_stage_config(config_path: Path) -> dict:
    """Read the TOML the running process was handed, and say whether it can be read at all."""
    try:
        raw = tomllib.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        return {"readable": False, "detail": f"{type(exc).__name__}: {exc}", "path": _rel(config_path)}
    # [curriculum].stages names the stage; the stage's own knobs live in the [stages.<name>]
    # table, so the two halves have to be read apart from each other.
    curriculum = raw.get("curriculum", {}) or {}
    named = curriculum.get("stages") or []
    name = named[0] if isinstance(named, list) and named else None
    table = (raw.get("stages", {}) or {}).get(name, {}) if name else {}
    runtime = raw.get("runtime", {}) or {}
    target = raw.get("target", {}) or {}
    return {
        "readable": True,
        "path": _rel(config_path),
        "run_dir": runtime.get("run_dir"),
        "emulator_root": runtime.get("emulator_root"),
        "character": target.get("character"),
        "ascension": target.get("ascension"),
        "save_load": target.get("save_load"),
        "game_branch": target.get("game_branch"),
        "stage_name": name,
        "timesteps": table.get("timesteps"),
        "parallel_envs": table.get("parallel_envs"),
        "checkpoint_every_steps": table.get("checkpoint_every_steps"),
        "checkpoint_eval_episodes": table.get("checkpoint_eval_episodes"),
        "promotion_eval_episodes": table.get("promotion_eval_episodes"),
        "max_episode_steps": table.get("max_episode_steps"),
        "n_steps": (raw.get("algorithm", {}) or {}).get("n_steps"),
        "combat_executor": table.get("combat_executor"),
        "combat_executor_checkpoint": table.get("combat_executor_checkpoint"),
        "campaign": table.get("campaign"),
        "promotion": {
            "min_episodes": table.get("min_episodes"),
            "min_win_rate": table.get("min_win_rate"),
            "min_wilson_lower": table.get("min_wilson_lower"),
            "max_truncation_rate": table.get("max_truncation_rate"),
            "max_illegal_actions": table.get("max_illegal_actions"),
            "min_boundary_rate": table.get("min_boundary_rate"),
        },
        "seeds": table.get("seeds", {}),
    }


def find_run_root(explicit: Path | None, stage_config: dict) -> tuple[Path | None, str]:
    """Pick the run directory the batch is writing, without guessing across arms."""
    if explicit is not None:
        return (explicit if explicit.is_dir() else None), f"由 --run-root 指定：{_rel(explicit)}"
    run_dir = stage_config.get("run_dir")
    if not run_dir:
        return None, "config 里没有 runtime.run_dir，无从搜索"
    base = ROOT / run_dir
    if not base.is_dir():
        return None, f"{_rel(base)} 不存在"
    candidates = sorted((path for path in base.iterdir()
                        if path.is_dir() and (path / "plan.json").is_file()),
                        key=lambda path: path.name)
    if not candidates:
        return None, f"{_rel(base)} 下没有带 plan.json 的 v2curriculum-* 目录"
    chosen = candidates[-1]
    return chosen, (f"{_rel(base)} 下 {len(candidates)} 个 run 目录里取最新的一个"
                    f"（run id 是 UTC 时间戳，名字序即时间序）；"
                    f"未取的：{[path.name for path in candidates[:-1]] or '无'}")


def arm_identity(plan: dict | None, plan_error: str | None, stage_config: dict) -> dict:
    """What the artifacts prove about which arm this is, stated as what each file does/does not say."""
    out: dict[str, object] = {
        "from_config": {
            "combat_executor": stage_config.get("combat_executor"),
            "combat_executor_checkpoint": stage_config.get("combat_executor_checkpoint"),
            "path": stage_config.get("path"),
        },
        "plan_records_executor": None,
        "plan_identity": None,
        "warnings": [],
    }
    if plan_error or plan is None:
        out["warnings"].append(f"plan.json 读不到，本次运行的身份只剩 config 单方面支撑：{plan_error}")
        return out
    stage = (plan.get("stages") or [{}])[0]
    out["plan_records_executor"] = "combat_executor" in stage
    out["plan_identity"] = {
        "character": plan.get("character"),
        "ascension": plan.get("ascension"),
        "save_load": plan.get("save_load"),
        "game_branch": plan.get("game_branch"),
        "scope": stage.get("scope"),
        "campaign": stage.get("campaign"),
        "timesteps": stage.get("timesteps"),
        "parallel_envs": stage.get("parallel_envs"),
        "emulator": plan.get("emulator"),
        "observation_contract": {
            "size": (plan.get("observation_contract") or {}).get("size"),
            "v2_observation_schema": (plan.get("observation_contract") or {}).get("v2_observation_schema"),
            "card_vocab_size": (plan.get("observation_contract") or {}).get("card_vocab_size"),
            "relic_vocab_size": (plan.get("observation_contract") or {}).get("relic_vocab_size"),
            "potion_vocab_size": (plan.get("observation_contract") or {}).get("potion_vocab_size"),
            "known_gaps": (plan.get("observation_contract") or {}).get("known_gaps"),
        },
        "seed_partitions": plan.get("seed_partitions"),
        "warm_start": plan.get("warm_start"),
    }
    if not out["plan_records_executor"]:
        out["warnings"].append(
            "plan.json 里没有 combat_executor 字段，所以这个 run 目录自身证明不了它是 G1 堆栈 —— "
            "那段自证代码（ec1432d）是在本进程启动之后才提交的。冻结执行器的证据来自交付给进程的"
            "那份 config，而不是 plan。")
    return out


def promotion_config(stage_plan: dict) -> object:
    gate = stage_plan.get("promotion") or {}
    return PromotionConfig(
        min_episodes=gate.get("episodes"),
        min_win_rate=gate.get("min_win_rate"),
        min_wilson_lower=gate.get("min_wilson_lower"),
        max_truncation_rate=gate.get("max_truncation_rate"),
        max_illegal_actions=gate.get("max_illegal_actions"),
        min_boundary_rate=gate.get("min_boundary_rate"),
        min_boundary_wilson_lower=gate.get("min_boundary_wilson_lower"),
    )


def score_metrics(metrics_path: Path, requirements) -> dict:
    """Read one evaluation payload and let the gate code decide, without pre-filtering fields."""
    row: dict[str, object] = {"path": _rel(metrics_path), "name": metrics_path.name}
    payload, error = read_json(metrics_path)
    if error or not isinstance(payload, dict):
        row["status"] = "unreadable"
        row["detail"] = error
        return row
    shown = ("schema_version", "generated_at", "stage", "split", "scope", "deterministic",
             "experimental", "checkpoint", "checkpoint_sha256", "seed_count", "seed_sha256",
             "episodes", "wins", "win_rate", "wilson_95_low", "wilson_95_high", "truncations",
             "truncation_rate", "defect_truncation_rate", "illegal_actions", "boundary_rate",
             "boundary_wilson_95_low", "boundary_hits", "mean_steps", "mean_return",
             "mean_final_floor", "max_final_floor", "mean_final_hp_fraction",
             "unclassified_dead_ends", "rejection_events", "campaign_clears",
             "environment_version", "dead_end_reasons", "by_act", "winning_seeds",
             "final_floor_histogram", "episodes_that_crossed_an_act_boundary")
    row["fields"] = {key: payload.get(key, "<absent>") for key in shown}
    row["keys_absent_from_file"] = [key for key in shown if key not in payload]
    try:
        decision = decide_promotion(EvaluationMetrics.from_payload(payload), requirements)
    except Exception as exc:  # a payload that does not fit the schema is a finding, not a crash
        row["status"] = "not-scoreable"
        row["detail"] = f"{type(exc).__name__}: {exc}"
        return row
    row["status"] = "scored"
    row["promoted"] = decision.promoted
    row["reasons"] = list(decision.reasons)
    row["required"] = decision.required
    return row


def checkpoint_trend(scored: list[dict]) -> list[dict[str, object]]:
    """One point per evaluation artifact, ordered by the step it was taken at.

    A single checkpoint evaluation answers "where is the policy now"; the trend across them is the
    only thing that can answer "is it moving".  Points keep ``episodes`` because a 200-episode
    checkpoint read and a 500-episode promotion read are different sample sizes and must not be
    averaged together on the client.
    """
    points = []
    for row in scored:
        if row.get("status") != "scored":
            continue
        fields = row["fields"]
        step = re.match(r"step_(\d+)", row["name"])
        points.append({
            "step": int(step.group(1)) if step else None,
            "name": row["name"],
            "split": fields.get("split"),
            "episodes": fields.get("episodes"),
            "wins": fields.get("wins"),
            "win_rate": fields.get("win_rate"),
            "wilson_95_low": fields.get("wilson_95_low"),
            "mean_final_floor": fields.get("mean_final_floor"),
            "max_final_floor": fields.get("max_final_floor"),
            "boundary_hits": fields.get("boundary_hits"),
            "crossed_an_act_boundary": fields.get("episodes_that_crossed_an_act_boundary"),
            "illegal_actions": fields.get("illegal_actions"),
            "promoted": row.get("promoted"),
        })
    points.sort(key=lambda point: (point["step"] is None, point["step"]))
    return points


def scan_stage_dir(stage_dir: Path | None, requirements) -> dict:
    out: dict[str, object] = {
        "stage_dir": None, "exists": False, "checkpoints": [], "metrics": [], "trend": [],
        "promotion_decision": None, "other_files": [], "inference": [],
    }
    if stage_dir is None:
        out["inference"].append("未定位到 run 目录，所以没有去搜 stage 目录")
        return out
    out["stage_dir"] = _rel(stage_dir)
    if not stage_dir.is_dir():
        out["inference"].append(f"{_rel(stage_dir)} 还不存在")
        return out
    out["exists"] = True
    for path in sorted((stage_dir / "checkpoints").glob("step_*.zip")):
        stat = path.stat()
        out["checkpoints"].append({"name": path.name, "bytes": stat.st_size,
                                   "mtime": datetime.fromtimestamp(stat.st_mtime, UTC)
                                   .strftime("%Y-%m-%dT%H:%M:%SZ")})
    metrics_dir = stage_dir / "metrics"
    scored = []
    for path in sorted(metrics_dir.glob("*.json")):
        scored.append(score_metrics(path, requirements))
    out["metrics"] = scored
    out["trend"] = checkpoint_trend(scored)
    decision_path = stage_dir / "promotion_decision.json"
    if decision_path.is_file():
        payload, error = read_json(decision_path)
        out["promotion_decision"] = {"path": _rel(decision_path), "payload": payload,
                                     "error": error}
    for path in sorted(stage_dir.iterdir()):
        if path.is_file() and path.name != "plan.json":
            stat = path.stat()
            out["other_files"].append({"name": path.name, "bytes": stat.st_size,
                                       "mtime": datetime.fromtimestamp(stat.st_mtime, UTC)
                                       .strftime("%Y-%m-%dT%H:%M:%SZ")})
    if out["checkpoints"]:
        latest = out["checkpoints"][-1]
        scored_names = {row["name"] for row in scored}
        stem = latest["name"][:-4]
        if f"{stem}.json" not in scored_names:
            out["inference"].append(
                f"checkpoint {latest['name']} 已存在但没有对应的 metrics 文件——从外面看这正是"
                "checkpoint 评估还在跑的样子；这是由两个产物推断的，不是日志里的状态")
    return out


def build_state(run_root: Path | None, run_note: str, log_path: Path | None,
                stage_config: dict, plan: dict | None, plan_error: str | None,
                process_state: dict, now: float) -> dict:
    text = ""
    read_note = None
    tail_truncated = False
    if log_path is not None:
        try:
            size = log_path.stat().st_size
            with log_path.open("rb") as handle:
                if size > TAIL_BYTES:
                    handle.seek(size - TAIL_BYTES)
                    tail_truncated = True
                text = handle.read().decode("utf-8", errors="replace")
        except OSError as exc:
            read_note = f"log could not be read: {type(exc).__name__}: {exc}"
    parsed = parse_console_dump(text)
    points = series_from_blocks(parsed["blocks"])
    stage_plan = (plan or {}).get("stages", [{}])[0] if (plan or {}).get("stages") else {}
    merged_stage = {**stage_config, **{k: v for k, v in stage_plan.items()
                                       if k in ("timesteps", "checkpoint_every_steps")}}
    return {
        "as_of": datetime.fromtimestamp(now, UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "run": {"root": _rel(run_root) if run_root else None, "selection": run_note,
                "plan_error": plan_error},
        "arm": arm_identity(plan, plan_error, stage_config),
        "config": stage_config,
        "progress": progress(points, merged_stage),
        "series": points[-400:],
        "series_start_index": max(0, len(points) - 400),
        "log": {
            "blocks_parsed": len(parsed["blocks"]),
            "rows_skipped": parsed["rows_skipped"],
            "trailing_block_open": parsed["trailing_block_open"],
            "tail_truncated": tail_truncated,
            "read_error": read_note,
            "sections_seen": sorted({section for block in parsed["blocks"] for section in block}),
        },
        "liveness": liveness(log_path, points, now),
        "process": process_state,
        "stage": scan_stage_dir(run_root / (stage_config.get("stage_name") or "full_run")
                                if run_root else None,
                                promotion_config(stage_plan)),
        "blind_spots": [
            "checkpoint 之间的局数与胜率：磁盘上没有任何地方记它，所以面板显示的是下一个 checkpoint "
            "何时到，而不是编一条实时胜率曲线",
            "G1 的两条证伪判据 absorbed_combat_steps 与 executor_step_capped_transitions：它们出自 "
            "scripts/phase_action_audit.py，不在这份日志里",
            "分环境统计：这份 dump 是 12 个世界聚合后的结果，拆不出单个世界",
            "真机侧的事实：本面板读的是模拟器，这里没有任何东西在看真实客户端或 Combat Solver",
        ],
    }


def resolve_paths(args: argparse.Namespace) -> tuple[Path | None, str, Path, dict, dict | None, str | None, Path | None]:
    config_path = Path(args.config)
    if not config_path.is_absolute():
        config_path = ROOT / config_path
    stage_config = load_stage_config(config_path)
    run_root, note = find_run_root(
        Path(args.run_root) if args.run_root else None, stage_config)
    log_path = Path(args.log) if args.log else None
    if args.log and log_path is not None and not log_path.is_absolute():
        log_path = ROOT / log_path
    plan, plan_error = (None, "no run root resolved")
    if run_root is not None:
        plan, plan_error = read_json(run_root / "plan.json")
    return run_root, note, config_path, stage_config, plan, plan_error, log_path


HTML = """
<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>STS2 训练监视</title>
<style>
:root{--bg:#0b0e14;--panel:#141a23;--panel2:#0f141c;--line:#232c39;--txt:#dbe2ea;--dim:#8494a8;
--ok:#43b581;--bad:#e5534b;--warn:#d9a04d;--accent:#5aa2f7;--accent2:#8b7cf6}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--txt);
font:14px/1.6 -apple-system,"Segoe UI","Microsoft YaHei","Noto Sans CJK SC",sans-serif}
header{position:sticky;top:0;z-index:20;background:#0b0e14f2;border-bottom:1px solid var(--line);
padding:10px 18px;display:flex;gap:8px;align-items:center;flex-wrap:wrap}
header h1{font-size:15px;margin:0 10px 0 0;font-weight:700;letter-spacing:.3px}
.pill{border:1px solid var(--line);border-radius:999px;padding:2px 11px;font-size:12.5px;color:var(--dim)}
.pill.ok{border-color:#2b5f48;color:var(--ok)}.pill.bad{border-color:#5f2b2b;color:var(--bad)}
.pill.warn{border-color:#5f4a2b;color:var(--warn)}
main{padding:16px 18px 60px;display:grid;gap:14px;grid-template-columns:repeat(12,1fr);max-width:1680px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:14px 16px}
.c12{grid-column:span 12}.c8{grid-column:span 8}.c6{grid-column:span 6}
.c4{grid-column:span 4}.c3{grid-column:span 3}
@media(max-width:1240px){.c8,.c6,.c4,.c3{grid-column:span 12}}
h2{font-size:13px;margin:0 0 2px;letter-spacing:.6px;color:var(--accent);font-weight:700}
.hint{font-size:12.5px;color:var(--dim);margin:0 0 10px}
.hero{display:flex;align-items:baseline;gap:14px;flex-wrap:wrap}
.hero b{font-size:38px;font-weight:800;letter-spacing:-1px;font-variant-numeric:tabular-nums}
.hero span{font-size:13px;color:var(--dim);font-variant-numeric:tabular-nums}
.bar{position:relative;height:12px;background:var(--panel2);border:1px solid var(--line);
border-radius:6px;overflow:hidden;margin:12px 0 6px}
.bar>i{position:absolute;left:0;top:0;bottom:0;background:linear-gradient(90deg,#2f6fd0,var(--accent));
border-radius:6px;transition:width .5s}
.bar>u{position:absolute;top:-2px;bottom:-2px;width:2px;background:var(--warn);text-decoration:none}
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px;margin-top:12px}
.stat{background:var(--panel2);border:1px solid var(--line);border-radius:8px;padding:9px 11px}
.stat b{display:block;font-size:20px;font-weight:700;font-variant-numeric:tabular-nums;line-height:1.25}
.stat i{font-style:normal;font-size:12px;color:var(--dim)}
svg{display:block;width:100%;height:120px;overflow:visible}
.chart .now{float:right;font-size:12.5px;color:var(--dim);font-variant-numeric:tabular-nums}
table{border-collapse:collapse;width:100%;font-size:12.5px;font-variant-numeric:tabular-nums}
th,td{border-bottom:1px solid var(--line);padding:5px 8px;text-align:left}
th{color:var(--dim);font-weight:600;font-size:12px}
.pass{color:var(--ok)}.fail{color:var(--bad)}.unk{color:var(--warn)}
ul{margin:6px 0;padding-left:20px}li{margin:3px 0}
.err{border:1px solid #5f2b2b;background:#1a1013;color:#f19a94;padding:10px 12px;
border-radius:8px;font-size:13px;margin:8px 0}
.note{border:1px solid var(--line);background:var(--panel2);border-radius:8px;padding:10px 12px;
font-size:13px;color:var(--dim)}
.kv{display:grid;grid-template-columns:210px 1fr;gap:3px 12px;font-size:12.5px}
.kv>b{color:var(--dim);font-weight:600}
.kv>div{font-variant-numeric:tabular-nums;word-break:break-all}
code{background:#0b0e14;padding:1px 5px;border-radius:4px;font-size:11.5px}
details{margin-top:4px}
summary{cursor:pointer;color:var(--dim);font-size:13px;padding:4px 0;user-select:none}
.bars{display:flex;align-items:flex-end;gap:4px;height:130px;margin-top:6px}
.bars>div{flex:1;background:linear-gradient(180deg,var(--accent2),#4a3f99);border-radius:3px 3px 0 0;
position:relative;min-height:2px}
.bars>div>b{position:absolute;top:-15px;left:0;right:0;text-align:center;font-size:10.5px;
font-weight:600;color:var(--dim)}
.bars>div>i{position:absolute;bottom:-17px;left:0;right:0;text-align:center;font-size:10.5px;
font-style:normal;color:var(--dim)}
.mono{font-variant-numeric:tabular-nums}
</style></head><body>
<header>
<h1>STS2 训练监视</h1>
<span class=pill id=p-run>—</span>
<span class=pill id=p-log>—</span>
<span class=pill id=p-proc>—</span>
<span class=pill id=p-next>—</span>
<span class=pill id=p-time>—</span>
<label class=pill><input type=checkbox id=pause> 暂停</label>
<label class=pill><input type=checkbox id=hintbox checked> 显示读法</label>
</header>
<main id=main></main>
<script>
const $=id=>document.getElementById(id);
const esc=v=>String(v==null?'':v).replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));
const n=v=>{if(v==null)return '—';if(typeof v!=='number')return esc(v);
 if(Number.isInteger(v))return v.toLocaleString('en-US');
 return Math.abs(v)>=1000?Math.round(v).toLocaleString('en-US')
 :(Math.abs(v)>=1?v.toFixed(2):v.toExponential(2));};
const hm=s=>{if(typeof s!=='number')return '—';s=Math.round(s);
 const h=(s/3600)|0,m=((s%3600)/60)|0;return h?h+' 小时 '+String(m).padStart(2,'0')+' 分':m+' 分';};
const CHARTS=[
 {id:'fps',title:'每秒步数 (fps)',hint:'越高越快。长期贴 0 或骤降 = 卡住，不是"变好了"。',color:'#5aa2f7',
  pick:r=>[r.iterations,r.fps]},
 {id:'steps',title:'累计环境步数',hint:'日志自己报的数，不是外推。斜率=速度。',color:'#43b581',
  pick:r=>[r.iterations,r.total_timesteps]},
 {id:'kl',title:'策略更新幅度 (approx_kl)',hint:'每次更新把策略挪多远。经验上持续 >0.02~0.03 说明步子太大（这是经验区间，不是本项目门禁）。',color:'#d9a04d',
  pick:r=>[r.iterations,r.approx_kl]},
 {id:'clip',title:'被裁剪样本比例 (clip_fraction)',hint:'一直贴 0 = 几乎没在动；长期很高 = 步子迈太大。',color:'#d9a04d',
  pick:r=>[r.iterations,r.clip_fraction]},
 {id:'ent',title:'策略熵（取负，entropy_loss）',hint:'数值越接近 0，策略越确定、探索越少。太快贴 0 = 过早收敛。',color:'#8b7cf6',
  pick:r=>[r.iterations,r.entropy_loss]},
 {id:'ev',title:'价值解释力 (explained_variance)',hint:'1=完全解释回报，0=没有信号，<0=比瞎猜还差。',color:'#39c5cf',
  pick:r=>[r.iterations,r.explained_variance]},
 {id:'vf',title:'价值损失 (value_loss)',hint:'应随训练下降。它不降不代表策略差，只代表价值头没拟合好。',color:'#f0883e',
  pick:r=>[r.iterations,r.value_loss]},
 {id:'pg',title:'策略梯度损失',hint:'量级小、正负都正常，不要单独当好坏指标。',color:'#f778ba',
  pick:r=>[r.iterations,r.policy_gradient_loss]}];

const R={};
function card(cls,title,hint){const c=document.createElement('div');c.className='card '+cls;
 if(title){const h=document.createElement('h2');h.textContent=title;c.appendChild(h);}
 if(hint){const p=document.createElement('p');p.className='hint';p.textContent=hint;c.appendChild(p);}
 $('main').appendChild(c);return c}
function svgIn(host){const s=document.createElementNS('http://www.w3.org/2000/svg','svg');
 host.appendChild(s);return s}
function drawChart(s,pts,color){
 const W=Math.round(s.getBoundingClientRect().width||s.parentNode.clientWidth||380),H=120,P=6;
 if(pts.length<2){s.innerHTML='<text x=4 y=16 font-size=11 fill=#8494a8>数据点不足（还没到第二次记录）</text>';return}
 const ys=pts.map(p=>p[1]).filter(v=>typeof v==='number');
 if(ys.length<2){s.innerHTML='<text x=4 y=16 font-size=11 fill=#8494a8>该字段还没出现在日志里</text>';return}
 const xs=pts.map(p=>p[0]);let x0=Math.min(...xs),x1=Math.max(...xs);if(x1===x0)x1=x0+1;
 let y0=Math.min(...ys),y1=Math.max(...ys);if(y1===y0){y1=y0+1;y0=y0-1}
 const X=v=>P+(v-x0)/(x1-x0)*(W-2*P),Y=v=>H-P-(v-y0)/(y1-y0)*(H-2*P-16);
 const d=pts.filter(p=>typeof p[1]==='number')
  .map((p,i)=>(i?'L':'M')+X(p[0]).toFixed(1)+','+Y(p[1]).toFixed(1)).join('');
 const lx=X(xs[xs.length-1]).toFixed(1),ly=Y(ys[ys.length-1]).toFixed(1);
 s.innerHTML='<path d="'+d+'" fill=none stroke='+color+' stroke-width=1.7 stroke-linejoin=round/>'
  +'<circle cx='+lx+' cy='+ly+' r=2.8 fill='+color+'/>'
  +'<text x=4 y=11 font-size=10.5 fill=#8494a8>最高 '+n(Math.max(...ys))+' · 最低 '+n(Math.min(...ys))+'</text>'
  +'<text x=4 y='+(H-2)+' font-size=10 fill=#4d5763>第 '+n(x0)+' 次 → 第 '+n(x1)+' 次</text>'}

function build(){
 $('main').innerHTML='';
 // ---- hero
 const hero=card('c12',null,null);
 hero.innerHTML='<h2>这一臂跑到哪了</h2><p class=hint>正在看的 batch：'+
  'training.v2_curriculum，配置 config/production_campaign_v5_g1.toml（G1：战斗交给冻结权重，'+
  '策略只学幕外决策）。</p>'+
  '<div class=hero><b id=h-pct>—</b><span id=h-steps>—</span>'+
  '<span class=pill id=h-state>—</span></div>'+
  '<div class=bar><i id=h-fill style=width:0%></i></div>'+
  '<div class=stats id=h-stats></div>';
 R.pct=$('h-pct');R.steps=$('h-steps');R.state=$('h-state');R.fill=$('h-fill');R.stats=$('h-stats');
 R.ticks={};
 // ---- what it means (static prose, not measurement)
 card('c12','怎么读这个进度','G1 之后"一步"= 一个幕外决策：战斗照打，但不进损失。'+
  '所以同样的步数买到的局数比改造前多（约 5 倍），比较两条臂只能按局数比、不能按步数比。'+
  '1,500,000 步是 G5 预先登记的预算上限，不是"跑满就该成功"。').innerHTML+=
  '<div class=note style="margin-top:8px">这条 batch 是在 G4 读"否"之后按业主指示启动的，'+
  '所以它是一次<b>偏差记录</b>，不是门禁通过。就算它全绿，也不授权多日训练：'+
  'G5 的后半段要求真机客户端上、11 项整局契约下的配对比较。</div>';
 // ---- evaluation results
 const ev=card('c12','评估结果：目前有多强','每个 checkpoint 会在"没参与训练"的种子上跑一批评估并落一个 '+
  'metrics/*.json。门禁判定由仓库自己的 training/promotion.py 做，面板不另写阈值。'+
  'checkpoint 评估是 200 局、promotion 判定是 500 局，两者不能混着看。');
 ev.innerHTML+='<div id=ev-body></div>';R.ev=$('ev-body');
 // ---- charts
 CHARTS.forEach(c=>{const host=card('c3 chart',c.title,c.hint);
  host.innerHTML+='<div class=now id=now-'+c.id+'></div>';
  c.node=svgIn(host);c.now=$('now-'+c.id)});
 // ---- details blocks
 const d1=card('c6',null,null);
 d1.innerHTML='<details open><summary>身份与告警（这块决定"你看到的数是谁产生的"）</summary>'+
  '<div id=id-warn></div><div class=kv id=id-kv></div></details>';
 R.idWarn=$('id-warn');R.idKv=$('id-kv');
 const d2=card('c6',null,null);
 d2.innerHTML='<details><summary>门禁阈值与种子分区</summary><div class=kv id=gate-kv></div>'+
  '<div id=seed-kv style="margin-top:8px"></div></details>';
 R.gateKv=$('gate-kv');R.seedKv=$('seed-kv');
 const d3=card('c6',null,null);
 d3.innerHTML='<details><summary>这块面板看不见什么（别把"没有红灯"当成"没问题"）</summary>'+
  '<ul id=blind></ul></details>';R.blind=$('blind');
 const d4=card('c6',null,null);
 d4.innerHTML='<details><summary>数据来源（每个数是从哪个文件读来的）</summary>'+
  '<div class=kv id=src></div></details>';R.src=$('src');
 R.err=card('c12',null,null);R.err.style.display='none';
}

let lastSig='';
function patch(s){
 const p=s.progress||{},lv=s.liveness||{},pr=s.process||{},log=s.log||{},cfg=s.config||{},
  st=s.stage||{},arm=s.arm||{},plan=arm.plan_identity||{};
 $('p-run').textContent='run '+((s.run&&s.run.root)?s.run.root.split('/').pop():'(未定位)');
 const lg=$('p-log');
 lg.className='pill '+(lv.state==='writing'?'ok':lv.state==='stale'?'bad':'warn');
 lg.textContent='日志 '+(lv.seconds_since_log_write==null?'未知':Math.round(lv.seconds_since_log_write)+' 秒前')
  +'（阈值 '+n(lv.stale_threshold_s)+' 秒）';
 const pc=$('p-proc'),m=pr.matches||[];
 pc.className='pill '+(pr.state==='ok'?(m.length?'ok':'bad'):pr.state==='unsupported'?'':'warn');
 pc.textContent='进程 '+(pr.state==='ok'?m.length+' 个 · 最忙 '+n(m[0]&&m[0].cpu_seconds)+' 秒 CPU':pr.state);
 $('p-next').textContent='下一个 checkpoint '+(p.next_checkpoint_at?'@ '+n(p.next_checkpoint_at)
  +(p.eta_s_to_next_checkpoint?'（约 '+hm(p.eta_s_to_next_checkpoint)+'后）':''):'（已到顶）');
 $('p-time').textContent='读数 '+s.as_of;

 R.pct.textContent=(typeof p.percent_of_target==='number'?p.percent_of_target.toFixed(1):'—')+'%';
 R.steps.textContent=n(p.total_timesteps)+' / '+n(p.target_timesteps)+' 步';
 const ok=lv.state==='writing'&&(!pr.state||pr.state==='ok'&&m.length);
 R.state.className='pill '+(ok?'ok':'bad');
 R.state.textContent=ok?'训练中':(lv.state==='stale'?'日志停滞':'读数不确定');
 R.fill.style.width=Math.min(100,p.percent_of_target||0)+'%';
 // checkpoint ticks
 const every=cfg.checkpoint_every_steps,target=p.target_timesteps;
 if(every&&target){const bar=R.fill.parentNode;
  for(let v=every;v<target;v+=every){const key='t'+v;
   if(!R.ticks[key]){const u=document.createElement('u');
    u.style.left=(v/target*100).toFixed(2)+'%';u.title=n(v);bar.appendChild(u);R.ticks[key]=u}}}
 R.stats.innerHTML='';
 [['距跑完（按全程均速）',hm(p.eta_s_from_mean_fps),'外推值，不是日志里的估计'],
  ['距跑完（按最近 10 次）',hm(p.eta_s_from_recent_fps),'外推值'],
  ['实测速度',n(p.fps_mean_stated_run)+' 步/秒','全程 = 步数 ÷ 已用时间'],
  ['已训练时长',hm(p.time_elapsed_s),'来自日志 time_elapsed'],
  ['每次迭代步数',n(p.steps_per_iteration),'n_steps × 并行环境数'],
  ['并行环境',n(cfg.parallel_envs),'同时跑的世界数']].forEach(([k,v,sub])=>{
   const d=document.createElement('div');d.className='stat';
   d.innerHTML='<b>'+v+'</b><i>'+esc(k)+'</i><i style=opacity:.7>'+esc(sub)+'</i>';
   R.stats.appendChild(d)});

 CHARTS.forEach(c=>{const pts=(s.series||[]).map(c.pick).filter(r=>r[0]!=null);
  drawChart(c.node,pts,c.color);
  const vals=pts.map(r=>r[1]).filter(v=>typeof v==='number');
  c.now.textContent=vals.length?'当前 '+n(vals[vals.length-1]):''});

 // ---- evaluation block, rebuilt only when the artifacts actually changed
 const sig=JSON.stringify([st.trend,(st.metrics||[]).map(r=>[r.name,r.status,r.promoted])]);
 if(sig!==lastSig){lastSig=sig;renderEval(st)}

 const iw=$('id-warn');
 iw.innerHTML=(arm.warnings||[]).map(w=>'<div class=err>'+esc(w)+'</div>').join('');
 R.idKv.innerHTML=rows([
  ['角色 / 进阶',esc(plan.character)+' / A'+esc(plan.ascension)],
  ['游戏分支',esc(plan.game_branch)],
  ['存档读档',esc(plan.save_load)+'（false = 每局从头跑）'],
  ['战斗由谁打',esc(cfg.combat_executor)+'（来自 '+esc(cfg.path)+'）'],
  ['冻结权重',esc(cfg.combat_executor_checkpoint)],
  ['plan.json 能否自证',arm.plan_records_executor?'能':'不能 —— 见上方红框'],
  ['模拟器 native 摘要',esc(plan.emulator&&plan.emulator.native_sha256)],
  ['观测维度 / schema',n(plan.observation_contract&&plan.observation_contract.size)+' / '
   +esc(plan.observation_contract&&plan.observation_contract.v2_observation_schema)],
  ['环境版本',esc((st.metrics||[]).map(r=>r.fields&&r.fields.environment_version).filter(Boolean)[0]||'（还没有评估文件）')]]);
 R.gateKv.innerHTML=rows(Object.entries(cfg.promotion||{}).map(([k,v])=>[esc(k),n(v)]));
 R.seedKv.innerHTML='<div class=kv>'+((plan.seed_partitions)||[]).map(sp=>
  '<b>'+esc(sp.name)+'</b><div>'+n(sp.start)+' … '+n(sp.start+sp.count-1)+
  '（'+n(sp.count)+' 枚）</div>').join('')+'</div>';
 R.blind.innerHTML=(s.blind_spots||[]).map(b=>'<li class=mono>'+esc(b)+'</li>').join('');
 R.src.innerHTML=rows([
  ['日志',esc(lv.log_path||'（无）')+' · '+n(lv.log_bytes)+' 字节 · '+n(log.blocks_parsed)+' 个迭代块'],
  ['解析跳过的行',n(log.rows_skipped)+'（>0 说明日志格式变了，值得看一眼）'],
  ['日志里的段',esc((log.sections_seen||[]).join(', '))],
  ['run 目录',esc((s.run||{}).root||'（未定位）')],
  ['定位方式',esc((s.run||{}).selection||'')],
  ['plan.json',esc(s.run&&s.run.plan_error||'读取正常')],
  ['进程探针',esc(pr.state)+(pr.detail?' · '+esc(pr.detail):'')],
  ['停滞阈值怎么来的',esc(lv.threshold_basis||'（还没算出来）')]]);
 if(log.read_error){R.err.style.display='';R.err.innerHTML='<div class=err>日志读不到：'+
  esc(log.read_error)+'</div>'}else R.err.style.display='none';
 applyHints()}

function rows(list){return list.map(([k,v])=>'<b>'+k+'</b><div>'+v+'</div>').join('')}

function renderEval(st){
 const trend=st.trend||[],metrics=st.metrics||[];
 if(!metrics.length){R.ev.innerHTML='<div class=note>还没有任何评估文件落在 '+
  esc(st.stage_dir||'（未定位）')+' 下。第一个 checkpoint 到点时才会写；'+
  '在那之前"胜率"这个数在这台机器上不存在，面板不会替你猜。</div>';return}
 const last=trend[trend.length-1];
 let html='';
 // headline: where the episodes actually die
 if(last&&last.mean_final_floor!=null){
  const row=(metrics.find(m=>m.name===last.name)||{}),f=row.fields||{};
  const hist=f.final_floor_histogram||{};
  const floors=Object.keys(hist).map(Number).sort((a,b)=>a-b);
  const maxCount=Math.max(1,...floors.map(k=>hist[k]||0));
  html+='<div class=stats style="margin-bottom:12px">'
   +'<div class=stat><b>'+n(last.wins)+' / '+n(last.episodes)+'</b><i>胜局数</i></div>'
   +'<div class=stat><b>'+n(last.mean_final_floor)+'</b><i>平均终局层数</i></div>'
   +'<div class=stat><b>'+n(last.max_final_floor)+'</b><i>最深到达层</i></div>'
   +'<div class=stat><b>'+n(last.crossed_an_act_boundary)+'</b><i>跨过幕界的局数</i></div>'
   +'<div class=stat><b>'+n(last.illegal_actions)+'</b><i>非法动作数</i></div></div>';
  if(floors.length){
   html+='<div class=hint style="margin:10px 0 0">每一层死了多少局（'+n(last.episodes)+
    ' 局评估的终局层数分布；17 层是本幕 Boss 所在层）</div><div class=bars>'+
    floors.map(k=>{const c=hist[k]||0;return '<div style="height:'+Math.max(2,c/maxCount*100)+
     '%"><b>'+c+'</b><i>'+k+'</i></div>'}).join('')+'</div><div style=height:20px></div>'}
 }
 metrics.forEach(m=>{
  if(m.status!=='scored'){html+='<div class=err>'+esc(m.name)+'：'+esc(m.status)+' '+
   esc(m.detail||'')+'</div>';return}
  const f=m.fields;
  html+='<div style="margin-top:12px"><b class='+(m.promoted?'pass':'fail')+'>'+esc(m.name)+
   '</b> <span class=hint>split '+esc(f.split)+' · '+n(f.episodes)+' 局 · 生成于 '+esc(f.generated_at)+'</span>'+
   '<div class='+(m.promoted?'pass':'fail')+'>'+(m.promoted?'按 promotion 门禁读：通过':'按 promotion 门禁读：未通过')+'</div>';
  if(f.split==='checkpoint')html+='<div class=hint>注意：这是 checkpoint 抽查（200 局），'+
   '不是晋升判定（500 局）。它必然因"局数不够"而未通过 —— 这条红不代表策略退步，只代表它不是那一次判定。</div>';
  html+='<ul>'+(m.reasons||[]).map(r=>'<li class=fail>'+esc(r)+'</li>').join('')+
   (m.promoted?'<li class=hint>没有一条判据拒绝它</li>':'')+'</ul>';
  if((m.keys_absent_from_file||[]).length)html+='<div class=hint>该文件里没有这些键（不是 0，是没有）：'+
   esc(m.keys_absent_from_file.join(', '))+'</div>';
  html+='<div class=kv>'+rows([
   ['终局层数分布',esc(JSON.stringify(f.final_floor_histogram||{}))],
   ['按幕拆分',esc(JSON.stringify(f.by_act||{}))],
   ['截断 / 缺陷截断',n(f.truncation_rate)+' / '+n(f.defect_truncation_rate)],
   ['边界到达率',n(f.boundary_rate)+'（Wilson 下界 '+n(f.boundary_wilson_95_low)+'）'],
   ['平均每局步数 / 平均回报',n(f.mean_steps)+' / '+n(f.mean_return)],
   ['模拟器拒绝事件',n(f.rejection_events)+'（引擎异常量，不计入策略好坏）'],
   ['胜局种子',esc(JSON.stringify(f.winning_seeds||[]))],
   ['检查点摘要',esc(f.checkpoint_sha256)]])+'</div></div>'});
 if(trend.length>1)html+='<div style="margin-top:14px"><div class=hint>各 checkpoint 的横向对比（同一批门禁、不同权重）</div>'+
  '<table><tr><th>步数</th><th>split</th><th>局数</th><th>胜</th><th>胜率</th>'+
  '<th>Wilson 下界</th><th>平均层</th><th>最深</th><th>跨幕</th></tr>'+
  trend.map(t=>'<tr><td>'+n(t.step)+'</td><td>'+esc(t.split)+'</td><td>'+n(t.episodes)+
   '</td><td>'+n(t.wins)+'</td><td>'+n(t.win_rate)+'</td><td>'+n(t.wilson_95_low)+
   '</td><td>'+n(t.mean_final_floor)+'</td><td>'+n(t.max_final_floor)+'</td>'+
   '<td>'+n(t.crossed_an_act_boundary)+'</td></tr>').join('')+'</table></div>';
 if(st.promotion_decision&&st.promotion_decision.payload)
  html+='<div style="margin-top:12px"><b>promotion_decision.json</b><pre class=mono>'+
   esc(JSON.stringify(st.promotion_decision.payload,null,1)).slice(0,1500)+'</pre></div>';
 (st.inference||[]).forEach(i=>{html+='<div class=hint style="margin-top:8px">· '+esc(i)+'</div>'});
 R.ev.innerHTML=html}

let paused=false,hideHints=false;
function applyHints(){document.querySelectorAll('.hint').forEach(h=>h.style.display=hideHints?'none':'')}
$('pause').onchange=e=>paused=e.target.checked;
$('hintbox').onchange=e=>{hideHints=!e.target.checked;applyHints()};
async function tick(){
 if(paused||document.hidden)return;
 try{const r=await fetch('/api/state',{cache:'no-store'});
  if(!r.ok)throw new Error('HTTP '+r.status);
  const s=await r.json();window.__last=s;patch(s)}
 catch(e){const lg=$('p-log');lg.className='pill bad';
  lg.textContent='读数失败：'+e.message}}
build();tick();setInterval(tick,4000);
window.addEventListener('resize',()=>{if(window.__last)patch(window.__last)});
</script></body></html>
"""


def route_for(path: str) -> str:
    """One page and one endpoint. A pasted URL that picked up a stray suffix must still show the
    panel -- a 404 on a monitor reads as 'the run died', which is a claim about the batch, not
    about the URL. Only the /api/ namespace is allowed to say 'no such endpoint'."""
    clean = path.split("?", 1)[0].rstrip("/") or "/"
    if clean == "/api":
        return "missing"
    if clean.startswith("/api/"):
        return "state" if clean == "/api/state" else "missing"
    return "page"


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "STS2TrainWatch/1"

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler contract
        route = route_for(self.path)
        if route == "missing":
            self.send_error(404, "no such endpoint")
            return
        if route == "state":
            run_root, note, _config, stage_config, plan, plan_error, log_path = resolve_paths(
                self.server.args)
            with _process_lock:
                process_state = dict(_process_cache)
            state = build_state(run_root, note, log_path, stage_config, plan, plan_error,
                                process_state, time.time())
            body = json.dumps(state, ensure_ascii=False).encode("utf-8")
            ctype = "application/json; charset=utf-8"
        else:
            body = HTML.encode("utf-8")
            ctype = "text/html; charset=utf-8"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        if self.server.args.verbose:
            sys.stderr.write("%s - %s\n" % (self.address_string(), format % args))


class Server(http.server.ThreadingHTTPServer):
    """Refuse a second bind. On Windows SO_REUSEADDR lets a new listener take a port that is
    already serving, so two panels would answer the same URL with two different reads."""

    allow_reuse_address = False
    daemon_threads = True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", default="config/production_campaign_v5_g1.toml",
                        help="the TOML the batch was handed; used for run_dir and the stage knobs")
    parser.add_argument("--log", default="runtime/g1_arm.log",
                        help="stdout dump to read (the sb3 console block per rollout iteration)")
    parser.add_argument("--run-root", default=None,
                        help="override the run directory instead of taking the newest under run_dir")
    parser.add_argument("--process-interval", type=float, default=20.0)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8899)
    parser.add_argument("--once", action="store_true", help="print one state document and exit")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    if not sys.platform.startswith("win"):
        _process_cache.update({"state": "unsupported", "matches": [],
                               "detail": "process probe is written for Windows"})
    if args.once:
        run_root, note, _config, stage_config, plan, plan_error, log_path = resolve_paths(args)
        state = build_state(run_root, note, log_path, stage_config, plan, plan_error,
                            probe_processes(), time.time())
        state["series"] = state["series"][-3:]
        print(json.dumps(state, ensure_ascii=False, indent=2))
        return 0

    if args.host not in ("127.0.0.1", "localhost", "::1"):
        raise SystemExit(f"refusing to bind {args.host}: this panel is loopback-only")
    stop = threading.Event()
    threading.Thread(target=refresh_processes, args=(args.process_interval, stop),
                     daemon=True).start()
    try:
        httpd = Server((args.host, args.port), Handler)
    except OSError as exc:
        stop.set()
        raise SystemExit(f"cannot bind {args.host}:{args.port}: {exc} -- a watch is already "
                         "listening there; use one panel rather than a second read")
    httpd.args = args
    print(f"training watch on http://{args.host}:{args.port}/ "
          f"(config {args.config}, log {args.log})", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
