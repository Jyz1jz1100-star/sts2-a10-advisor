"""Where do the campaign's `empty_action_mask` endings actually happen, and is the player alive?

`chained_map_deadend_fork_20260920.json` showed that an engine-empty mask never reaches the engine:
the flat env advertises one sentinel action for it and intercepts that action in its own `step`
(`v2_flat_env.py:240-250`, with `v2_run_wrapper.py:241-253` as defence in depth), returning a
truncation labelled `empty_action_mask`. That has a consequence for the campaign's most-quoted
dead-end numbers: the rejection-phase census's "0 refusals across 11,060 map decisions" is not
evidence that map states are free of dead ends -- the class is invisible to that counter by
construction. Meanwhile `dead_end_vocabulary_20260919.json` counts 50 `empty_action_mask` endings
repo-wide, and the only instance the report names is a single anecdote (seed 20000039, map phase
after a shop).

The 50 are not in one window: they sit in 35 committed metrics files, 1 to 5 per file, spread over
35 different checkpoints. Rolling any single window therefore cannot find them -- a 500-seed window
of the newest promotion checkpoint contains none. So this walks the recordings themselves: for each
metrics file that reports the label, it resolves that file's own seed window from its `seed_sha256`,
confirms the on-disk checkpoint still hashes to the `checkpoint_sha256` the metrics were produced
from, and re-rolls that exact (checkpoint, window) pair through the same contract stack. Per file it
then compares what it reproduced with what was recorded, so the artifact carries its own closure
check rather than a claim that the sample was representative.

For each located ending it records the phase, act and floor, HP, whether the player was alive, how
many legal bases the engine's own 32-bit mask had, and which contract layer labelled it. Aggregate
it and the anecdote becomes a distribution.

Shard with `--plan` then `--work-index`/`--work-count`, and join with `--merge`.
"""

from __future__ import annotations

import argparse
import collections
import dataclasses
import glob
import hashlib
import json
import sys

import numpy as np
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent / "third_party" / "slay-the-spire-2-emulator-main" / "src"))
sys.path.insert(0, str(ROOT))

DEAD_END_KEY = "simulator_dead_end"
SHORT_CIRCUIT_KEY = "synthetic_sentinel_action"
METRICS_GLOBS = ("runs/**/metrics/*.json", "runtime/**/metrics/*.json")
WINDOW_LIMITS = (50, 100, 200, 500, 1000, 10000)


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def plan_windows() -> dict[str, tuple[str, int, list[int]]]:
    """Windows declared by the runs themselves, which is how a legacy recording is traceable.

    A metrics file only stores a digest of the seeds it rolled, and reading that digest against the
    *current* config cannot resolve a run whose partitions were different when it ran -- that is how
    one file ended up labelled "window predates this config". Each run directory writes a plan.json
    naming the partitions it actually used, so the digest resolves against the recording's own
    provenance instead, and the exclusion turns out to have been an instrument limit rather than a
    fact about the file.
    """

    windows = {}
    for path in sorted(list(ROOT.glob("runs/**/plan.json")) + list(ROOT.glob("runtime/**/plan.json"))):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        from training.metrics import seed_digest

        for part in payload.get("seed_partitions") or []:
            try:
                start, count = int(part["start"]), int(part["count"])
            except (KeyError, TypeError, ValueError):
                continue
            # Only the longest window any partition could supply is materialised; a train
            # partition declares up to twenty million seeds.
            prefix = list(range(start, start + min(count, max(WINDOW_LIMITS))))
            for limit in WINDOW_LIMITS:
                if limit > count:
                    continue
                windows.setdefault(seed_digest(prefix[:limit]),
                                   (str(part.get("name", "")), limit, prefix[:limit]))
    return windows


def canonical_windows(config) -> dict[str, tuple[str, int, list[int]]]:
    """Digest -> (split, limit, seeds) for every contiguous window the trainer can ask for.

    The metrics files store only a digest of the seed list they rolled, so this is how a recording
    is traced back to a window. A digest missing here and missing from the run's own plan is
    reported as unresolved and dropped from coverage rather than guessed at.
    """

    from training.metrics import seed_digest

    windows = {}
    for stage in config.stages:
        for split in ("promotion", "checkpoint", "final", "train"):
            try:
                seeds = config.partition(stage.name, split).seeds()
            except Exception:  # noqa: BLE001 - a stage may simply not declare the split
                continue
            for limit in WINDOW_LIMITS:
                if limit > len(seeds):
                    continue
                windows[seed_digest(seeds[:limit])] = (split, limit, seeds[:limit])
    for digest, window in plan_windows().items():
        windows.setdefault(digest, window)
    return windows


def build_plan(args, windows) -> list[dict]:
    """One entry per committed metrics file that recorded a dead end worth locating."""

    from training.metrics import seed_digest  # noqa: F401 - imported for the shared digest path

    seen: set[str] = set()
    entries: list[dict] = []
    for pattern in METRICS_GLOBS:
        for path in sorted(ROOT.glob(pattern)):
            key = path.relative_to(ROOT).as_posix()
            if key in seen:
                continue
            seen.add(key)
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(payload, dict):
                continue
            reasons = payload.get("dead_end_reasons") or {}
            recorded = int(reasons.get("empty_action_mask", 0) or 0)
            if not recorded:
                continue
            stage = str(payload.get("stage") or args.stage)
            digest = str(payload.get("seed_sha256") or "")
            window = windows.get(digest)
            checkpoint = Path(str(payload.get("checkpoint") or ""))
            entry = {
                "checkpoint_exists": checkpoint.is_file(),
                "checkpoint_path": checkpoint.as_posix(),
                "checkpoint_sha256_matches": (
                    bool(checkpoint.is_file())
                    and file_sha256(checkpoint) == str(payload.get("checkpoint_sha256") or "")
                ),
                "episodes": int(payload.get("episodes") or 0),
                "metrics_file": key,
                "recorded_empty_action_mask": recorded,
                "recorded_ends": dict(sorted(
                    {k: int(v) for k, v in reasons.items() if int(v) > 0}.items())),
                "seed_digest": digest,
                "split": str(payload.get("split") or ""),
                "stage": stage,
            }
            if window is not None:
                # A config partition yields a bare split name; a plan.json declares "act1.promotion",
                # so the label carries where the window was resolved from.
                entry["window_source"] = ("the run's own plan.json seed_partitions"
                                          if "." in window[0] else "the current config's partitions")
                entry["window_split_resolved"] = window[0]
                entry["window_limit"] = window[1]
                entry["window_seeds"] = list(window[2])
                entry["window_split"] = window[0]
                entry["window_status"] = "resolved"
            else:
                entry["window_status"] = "unresolved"
            entries.append(entry)
    return sorted(entries, key=lambda e: (-e["recorded_empty_action_mask"], e["metrics_file"]))


def writable_plan(entries) -> list[dict]:
    """The plan without the seed lists, which are recomputable and would bloat every shard.

    Checkpoint paths are recorded relative to the repo when they sit inside it: the absolute
    strings in the metrics files point at whichever checkout ran the training, and an evidence
    artifact should not import that.
    """

    def relativise(value: str) -> str:
        path = Path(value)
        try:
            return path.resolve().relative_to(ROOT).as_posix()
        except ValueError:
            return value

    return [{k: (relativise(v) if k == "checkpoint_path" else v)
             for k, v in entry.items() if k != "window_seeds"} for entry in entries]


def roll_episodes(factory, model, seeds, origin, max_steps) -> list[dict]:
    rows: list[dict] = []
    for seed in seeds:
        # One env per episode, released in a finally. Without the close this instrument stops
        # measuring and starts reporting its own handle exhaustion: an earlier pass of this file
        # rolled 1,500 un-cleaned envs in one process and 1,244 of them failed at reset with
        # `Sts2Run_GetInfo failed with status -1`, while the same window in small pieces was clean.
        # That is a property of the process, not of the engine.
        env = factory(seed)
        try:
            row = _roll_one_episode(max_steps, env, seed, model)
        except Exception as error:  # noqa: BLE001 - an unnamed crash would leave no artifact at all
            row = {"detail": f"uncaught: {error}", "outcome": "instrument_error", "seed": seed,
                   "alive": False, "act": None, "floor": -1, "hp": None, "max_hp": None,
                   "phase": None, "engine_legal_bases": None, "rejections": 0, "steps": 0,
                   "short_circuit": False}
        finally:
            try:
                env.close()
            except Exception as error:  # noqa: BLE001 - a leak is worth naming, not crashing for
                row["close_failed"] = str(error)
        row["origin"] = origin
        rows.append(row)
    return rows


def _roll_one_episode(max_steps, env, seed, model) -> dict:
    state = {"detail": None, "outcome": "step_cap", "seed": seed}
    try:
        observation, info = env.reset(seed=seed)
    except RuntimeError as error:
        return {**state, "alive": False, "engine_legal_bases": None, "floor": -1,
                "detail": str(error), "outcome": "native_reset_raised", "rejections": 0,
                "short_circuit": False, "steps": 0, "act": None, "hp": None, "max_hp": None,
                "phase": None}
    steps = 0
    while steps < max_steps:
        # Snapshot the engine's own mask BEFORE the step, guarded: this read goes through to the
        # native layer past the wrapper's active-episode check, so a run the engine has already
        # closed raises. Recording that as a named outcome is the point -- a run that closes
        # without `step()` ever reporting termination is a finding, and the census must count it
        # rather than die on it.
        try:
            native = env.unwrapped.base_action_mask()
        except RuntimeError as error:
            return _finalise(state, info, steps=steps, outcome="native_run_closed_unprompted",
                             detail=str(error), engine_legal_bases=None)
        native_bases = [base for base in range(len(native)) if native[base]]
        flat_mask = env.action_masks()
        if not any(bool(value) for value in flat_mask):
            # The wrapper advertises a sentinel whenever the engine mask is empty, so an all-false
            # flat mask means the codec withheld that bit too -- a different failure than the
            # engine's empty mask, and named separately so the two cannot be conflated.
            return _finalise(state, info, steps=steps, outcome="no_advertised_action",
                             engine_legal_bases=native_bases)
        raw, _ = model.predict(observation, action_masks=flat_mask, deterministic=True)
        action = int(raw.item() if hasattr(raw, "item") else raw)
        try:
            observation, _reward, terminated, truncated, info = env.step(action)
        except RuntimeError as error:
            # The step itself raising is the engine failing to report a terminal transition.
            # Counted and attributed rather than allowed to end the census: an uncaught exception
            # here would leave no artifact, exactly like the frontier sweep that crashed on its
            # first foreign-contract checkpoint.
            return _finalise(state, info, steps=steps + 1, outcome="native_step_raised",
                             detail=str(error), engine_legal_bases=native_bases,
                             flat_action=action)
        steps += 1
        state["rejections"] = max(state.get("rejections", 0),
                                  int(info.get("rejection_events", 0) or 0))
        if info.get(DEAD_END_KEY):
            return _finalise(state, info, steps=steps, outcome=str(info[DEAD_END_KEY]),
                             engine_legal_bases=native_bases,
                             short_circuit=SHORT_CIRCUIT_KEY in info)
        if terminated:
            return _finalise(state, info, steps=steps,
                             outcome="win" if info.get("player_won") else "death",
                             engine_legal_bases=None)
        if truncated:
            # The inner env enforces its own episode horizon. When that is the horizon this census
            # was rolled with, the evaluator's vocabulary calls the ending `step_cap`
            # (training/evaluation.py:129-132), so the census uses the same name -- a row whose
            # label depends on which side of the line the truncation was reported from is not a
            # finding, it is a naming artifact.
            return _finalise(state, info, steps=steps,
                             outcome="step_cap" if steps >= max_steps else "engine_truncated")
    return _finalise(state, info, steps=steps, outcome="step_cap")


def _finalise(state, info, *, steps, outcome, engine_legal_bases="keep", **extra) -> dict:
    row = {
        **state,
        "act": info.get("act"),
        "floor": info.get("floor") if info.get("floor") is not None else -1,
        "hp": info.get("player_hp"),
        "max_hp": info.get("player_max_hp"),
        "outcome": outcome,
        "phase": info.get("phase_name"),
        "engine_legal_bases": (engine_legal_bases if engine_legal_bases != "keep"
                               else state.get("engine_legal_bases")),
        "steps": steps,
    }
    row["alive"] = bool((row["hp"] or 0) > 0)
    row.update(extra)
    return row


def summarise(entries, shard_rows, merged) -> dict:
    located = [row for row in shard_rows if row["outcome"] == "empty_action_mask"]
    # Anything that is not an ordinary win or death is kept: an unlabelled truncation is precisely
    # the class the campaign's promotion gate calls an unclassified dead end, so dropping it here
    # would hide the one row a reader needs most.
    anomalies = [row for row in shard_rows if row["outcome"] not in ("death", "win")
                 and row["outcome"] != "empty_action_mask"]
    by_file = collections.Counter(row["origin"] for row in located)
    rolled = {entry["metrics_file"] for entry in entries}
    per_file = []
    for entry in entries:
        if entry["metrics_file"] not in rolled:
            continue
        reproduced = by_file.get(entry["metrics_file"], 0)
        per_file.append({
            "metrics_file": entry["metrics_file"],
            "recorded_empty_action_mask": entry["recorded_empty_action_mask"],
            "reproduced_empty_action_mask": reproduced,
            "reproduced_equals_recorded": reproduced == entry["recorded_empty_action_mask"],
            "episodes": entry["episodes"],
        })
    rows_by_phase = collections.Counter(str(row.get("phase")) for row in located)
    rows_by_act = collections.Counter(str(row.get("act")) for row in located)
    rows_by_floor = collections.Counter(str(row.get("floor")) for row in located)
    # `short_circuit` is the presence of the field only V2RunEnvWrapper.step() writes, so it names
    # which of the two equivalent interceptions ended the episode.
    layers = collections.Counter(
        "v2_run_wrapper_short_circuit" if row["short_circuit"] else "v2_flat_env_sentinel_step"
        for row in located)
    return {
        "aggregates": {
            "alive_at_the_dead_end": sum(1 for row in located if row.get("alive")),
            "dead_ends_with_no_engine_legal_basis": sum(
                1 for row in located if row["engine_legal_bases"] == []),
            "empty_action_mask_by_act": dict(sorted(rows_by_act.items())),
            "empty_action_mask_by_floor": dict(sorted(rows_by_floor.items())),
            "empty_action_mask_by_phase": dict(sorted(rows_by_phase.items())),
            "empty_action_mask_seeds": sorted({int(row["seed"]) for row in located}),
            "empty_action_mask_total": len(located),
            "endings_across_rolled_episodes": dict(sorted(collections.Counter(
                row["outcome"] for row in shard_rows).items())),
            "episodes_rolled": len(shard_rows),
            "files_rolled": len(rolled),
            "labelling_layers_observed": dict(sorted(layers.items())),
            "per_file_closure_mismatches": [row for row in per_file
                                            if not row["reproduced_equals_recorded"]],
            "per_file_mismatch_count": sum(1 for row in per_file
                                           if not row["reproduced_equals_recorded"]),
            "recorded_total_in_rolled_files": sum(e["recorded_empty_action_mask"]
                                                  for e in entries
                                                  if e["metrics_file"] in rolled),
            "roll_anomaly_seeds": sorted({int(row["seed"]) for row in anomalies}),
            "roll_anomaly_total": len(anomalies),
        },
        "close_failures": [row for row in shard_rows if row.get("close_failed")],
        "dead_ends": located,
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "not_established": [
            "that the step horizon this census rolled with equals the one each recording used: "
            "committed metrics do not store their own `max_episode_steps`, so `horizon_used_here` "
            "names the current stage config instead. It does not move the empty-mask verdict, which "
            "is raised long before any horizon, but it is why a `step_cap` count here is not "
            "automatically the same measurement as the one in the original file",
            "that these endings are engine defects in the general case -- the phase and floor "
            "distribution is what lets a reader judge that; each row is re-derivable from the "
            "(checkpoint, seed) pair recorded beside it",
            "any overlap with the rejection-phase census: an engine-empty mask is short-circuited "
            "before the engine is asked, so the census counter cannot see this class at all",
            "anything about the shipped game",
            "that a nonzero `native_reset_raised` count would be an engine property: an earlier "
            "pass of this instrument hit 1,244 of them from one process holding 1,500 un-closed "
            "envs, which is why every episode's env is closed in a finally here",
            "that every located ending is a cleared boss: the phase/floor/act columns cover every "
            "row, but the node-type and reward-phase traces behind `explained_walls` exist only "
            "for the seeds named in `per_seed_explanations`, and each of those is tied to the one "
            "checkpoint that ended it",
            "that the empty map after a boss is the only way this emulator can end a run: "
            "committed metrics do record act-2 wins, so a terminal path exists -- what is not "
            "established is which choice at the relic or card reward takes one run down it and "
            "which leaves the player on a map with no coordinates"],
        "per_file": per_file,
        "plan": writable_plan(entries),
        "anomaly_rows": anomalies,
        "merged_from_shards": merged,
        "scope": ("every committed act1 metrics file that recorded an empty_action_mask ending, "
                  "re-rolled at its own checkpoint over its own seed window"),
    }


def explain_seed(args, config, entry, seed) -> dict:
    """Replay one located ending far enough to say what the engine was showing at the wall.

    A row says 'map phase, floor 17, zero legal bases'. Whether that is a boss already beaten with
    nowhere to go, a node type the mask cannot represent, or a map with no generated successors are
    different findings, and the per-step info plus the map blocks of the observation separate them.
    """

    import sts2_gym

    from training.v2_curriculum import _environment_factory
    from training.v2_observation import BLOCK_OFFSETS, PHASE_NAMES

    stage = next(s for s in config.stages if s.name == entry["stage"])
    horizon = args.max_steps or stage.max_episode_steps
    open_stage = dataclasses.replace(stage, max_floor=None, max_episode_steps=horizon)
    factory = _environment_factory(config, open_stage, sts2_gym)
    env = factory(seed)
    from sb3_contrib import MaskablePPO
    from stable_baselines3.common.vec_env import DummyVecEnv

    probe = DummyVecEnv([lambda: factory(seed)])
    try:
        model = MaskablePPO.load(entry["checkpoint_path"], env=probe, device="cpu")
    finally:
        probe.close()
    trace = []
    decided = {}
    try:
        observation, info = env.reset(seed=seed)
        for step in range(horizon):
            flat_mask = env.action_masks()
            bases = sorted({flat // 7 for flat in range(224) if bool(flat_mask[flat])})
            record = {
                "act": info.get("act"), "current_node_type": info.get("current_node_type"),
                "encounter_id": info.get("encounter_id"), "floor": info.get("floor"),
                "hp": info.get("player_hp"), "phase": info.get("phase_name"),
                "player_won": info.get("player_won"), "simulator_player_won":
                    info.get("simulator_player_won"),
                "step": step, "advertised_flat_bases": bases,
                "engine_legal_bases": sorted(
                    base for base, on in enumerate(env.unwrapped.base_action_mask()) if on),
            }
            trace.append(record)
            if bases == [32] or not any(bool(value) for value in flat_mask):
                # Base 32 is the sentinel: the only thing on offer is the contract's own exit.
                decided = {**record, "at_the_wall": True}
                break
            raw, _ = model.predict(observation, action_masks=flat_mask, deterministic=True)
            action = int(raw.item() if hasattr(raw, "item") else raw)
            observation, _r, terminated, truncated, info = env.step(action)
            if terminated or truncated:
                decided = {**record, "at_the_wall": False,
                           "ended_instead": str(info.get(DEAD_END_KEY)
                                                or ("win" if info.get("player_won") else "terminal"))}
                break
        # `observation` is the expanded contract vector the policy is shown -- that is where the
        # map and node-type blocks live; `raw_observation()` is the untouched native vector and
        # has no such offsets.
        block = np.asarray(observation, dtype=int)
        map_block = [int(value) for value in
                     block[BLOCK_OFFSETS["map_option_coords"]:
                           BLOCK_OFFSETS["map_option_coords"] + 8]]
        node_onehot = int(block[BLOCK_OFFSETS["current_node_type_onehot"]:
                                BLOCK_OFFSETS["current_node_type_onehot"] + 8].argmax())
        phase_onehot = int(block[BLOCK_OFFSETS["phase_onehot"]:
                                 BLOCK_OFFSETS["phase_onehot"] + len(PHASE_NAMES)].argmax())
    finally:
        env.close()
    return {
        "checkpoint": entry["checkpoint_path"],
        "deciding_state": decided,
        "metrics_file": entry["metrics_file"],
        "observation_at_the_wall": {
            "current_node_type_onehot_index": node_onehot,
            "map_option_coords": map_block,
            "phase_onehot_name": PHASE_NAMES[phase_onehot],
        },
        "recorded_empty_action_mask": entry["recorded_empty_action_mask"],
        "seed": seed,
        "tail": trace[-10:],
        "transitions_replayed": len(trace),
    }


def attach_explanations(args, payload, rows) -> None:
    """Replay the named located endings and write the traces into the payload.

    A seed is only explainable against the checkpoint that ended it, so the recording that produced
    the row chooses the checkpoint; the trace is meaningless on its own because a different policy
    would have taken different map choices on the way.
    """

    from training.v2_config import load_v2_training_config

    config = load_v2_training_config(args.config.resolve())
    entries = {entry["metrics_file"]: entry
               for entry in build_plan(args, canonical_windows(config))}
    origins: dict[int, list[str]] = {}
    for row in rows:
        if row["outcome"] == "empty_action_mask":
            origins.setdefault(int(row["seed"]), []).append(row["origin"])
    traces = []
    for seed in args.explain_seed:
        names = origins.get(seed) or ([args.explain_from] if args.explain_from else [])
        if args.explain_from:
            names = [name for name in names if name.endswith(args.explain_from)] or [
                name for name in entries if name.endswith(args.explain_from)]
        if not names:
            traces.append({"error": "no rolled recording located an ending for this seed",
                           "seed": seed})
            continue
        entry = entries[names[0]]
        if not entry["checkpoint_sha256_matches"]:
            traces.append({"error": "the on-disk checkpoint is not the one that recorded this",
                           "seed": seed})
            continue
        trace = explain_seed(args, config, entry, seed)
        trace["recorded_by"] = names[0]
        trace["seed_in_recorded_window"] = seed in entry["window_seeds"]
        traces.append(trace)
    payload["per_seed_explanations"] = traces
    # The shards wrote their plan before window provenance existed; re-resolve it here from the
    # recording's own digests rather than pretending the field cannot be filled.
    for entry in payload["plan"]:
        fresh = entries.get(entry["metrics_file"])
        if fresh:
            stage = next((s for s in config.stages if s.name == entry["stage"]), None)
            entry["horizon_used_here"] = args.max_steps or (
                stage.max_episode_steps if stage else None)
            entry["horizon_used_by_the_recording"] = "not recorded in the metrics file"
            stage = next((s for s in config.stages if s.name == entry["stage"]), None)
            entry["horizon_used_here"] = args.max_steps or (
                stage.max_episode_steps if stage else None)
            entry["horizon_used_by_the_recording"] = "not recorded in the metrics file"
            entry.update({key: fresh[key] for key in
                          ("window_source", "window_split_resolved", "window_limit")
                          if key in fresh})
    rolled_files = {row["origin"] for row in rows}
    payload["coverage_exclusions"] = [
        {
            "metrics_file": entry["metrics_file"],
            "reason": ("no window digest matched either the current config's partitions or any "
                       "run's own declared plan.json partitions"
                       if entry["window_status"] != "resolved"
                       else "the on-disk checkpoint no longer hashes to what the file recorded"),
            "recorded_empty_action_mask": entry["recorded_empty_action_mask"],
        }
        for entry in entries.values() if entry["metrics_file"] not in rolled_files]
    walls = [trace["deciding_state"] for trace in traces if trace.get("deciding_state")]
    payload["explained_walls"] = {
        "engine_offered_nothing_at_every_explained_wall": all(
            wall["engine_legal_bases"] == [] for wall in walls),
        "node_types_at_the_explained_walls": dict(sorted(collections.Counter(
            str(wall["current_node_type"]) for wall in walls).items())),
        "seeds": [trace["seed"] for trace in traces],
        # Node 6 is RunConstants.NodeBoss and the flag the engine sets after the last combat was
        # won, so their conjunction on a map state with no coordinates is the shape of a boss that
        # was cleared into a map that has nowhere to go.
        "traces": len(traces),
        "won_the_last_combat_and_still_not_a_terminal": sum(
            1 for wall in walls
            if wall["simulator_player_won"] and not wall["player_won"]),
    }


def report(payload):
    agg = payload["aggregates"]
    print(f"{agg['episodes_rolled']} episodes over {agg['files_rolled']} files; "
          f"endings {agg['endings_across_rolled_episodes']}")
    print(f"located empty_action_mask: {agg['empty_action_mask_total']} "
          f"of {agg['recorded_total_in_rolled_files']} recorded "
          f"(alive {agg['alive_at_the_dead_end']}, "
          f"labelled by {agg['labelling_layers_observed']}, "
          f"no engine legal basis {agg['dead_ends_with_no_engine_legal_basis']})")
    print(f"  by phase {agg['empty_action_mask_by_phase']}")
    print(f"  by act   {agg['empty_action_mask_by_act']}")
    print(f"  floors   {agg['empty_action_mask_by_floor']}")
    print(f"  seeds    {agg['empty_action_mask_seeds'][:12]}"
          f"{' ...' if len(agg['empty_action_mask_seeds']) > 12 else ''}")
    # Print the instrument's own health, not just the finding: the run whose result was unusable
    # looked perfectly ordinary on the dead-end line.
    print(f"closure: {agg['per_file_mismatch_count']} file(s) where reproduced != recorded; "
          f"roll anomalies {agg['roll_anomaly_total']} "
          f"(seeds {agg['roll_anomaly_seeds'][:6]}), "
          f"env-close failures {len(payload['close_failures'])}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=ROOT / "config/training_v2.toml")
    parser.add_argument("--stage", default="act1", help="stage used when a file records none")
    parser.add_argument("--max-steps", type=int, default=None,
                        help="override the horizon; default is the run's own stage config")
    parser.add_argument("--plan", action="store_true", help="write the work list and stop")
    parser.add_argument("--plan-out", type=Path, help="where --plan writes; else stdout only")
    parser.add_argument("--work-index", type=int, default=0)
    parser.add_argument("--work-count", type=int, default=1)
    parser.add_argument("--merge", type=str, default=None,
                        help="glob of shard artifacts to join instead of rolling anything")
    parser.add_argument("--explain-seed", type=int, nargs="+", default=None,
                        help="replay these located endings and record what the engine was showing "
                             "at each wall; with --merge the traces join the merged artifact")
    parser.add_argument("--explain-from", type=str, default=None,
                        help="metrics file that recorded it; default is the first that did")
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()

    if args.merge:
        shards = [json.loads(Path(p).read_text(encoding="utf-8"))
                  for p in sorted(glob.glob(args.merge))]
        entries: dict[str, dict] = {}
        rows: list[dict] = []
        for shard in shards:
            for entry in shard["plan"]:
                entries.setdefault(entry["metrics_file"], entry)
            rows.extend(shard["dead_ends"])
            rows.extend(shard["anomaly_rows"])
        payload = summarise(list(entries.values()), rows, merged=True)
        payload["shard_count"] = len(shards)
        # A shard keeps only its located and anomalous rows, not every death, so the episode total
        # and the ending breakdown come from each shard's own aggregates rather than recounting
        # rows that were never written out.
        totals = collections.Counter()
        for shard in shards:
            totals.update(shard["aggregates"]["endings_across_rolled_episodes"])
        payload["aggregates"]["endings_across_rolled_episodes"] = dict(sorted(totals.items()))
        payload["aggregates"]["episodes_rolled"] = sum(
            shard["aggregates"]["episodes_rolled"] for shard in shards)
        if args.explain_seed:
            attach_explanations(args, payload, rows)
        args.out.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                            encoding="utf-8")
        report(payload)
        return 0

    from training.v2_config import load_v2_training_config

    config = load_v2_training_config(args.config.resolve())
    entries = build_plan(args, canonical_windows(config))
    if not entries:
        raise SystemExit("no committed metrics file records an empty_action_mask ending")

    if args.explain_seed:
        if args.explain_from:
            chosen = [e for e in entries if e["metrics_file"].endswith(args.explain_from)]
            if not chosen:
                raise SystemExit(f"--explain-from matches no recording that holds the label: "
                                 f"{args.explain_from}")
        else:
            chosen = [entries[0]]
        entry = chosen[0]
        if not entry["checkpoint_sha256_matches"]:
            raise SystemExit("refusing to explain a seed against a checkpoint that is not the one "
                             "the recording was produced from")
        details = [explain_seed(args, config, entry, seed) for seed in args.explain_seed]
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(details, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                            encoding="utf-8")
        for detail in details:
            print(json.dumps({"deciding_state": detail["deciding_state"],
                              "observation_at_the_wall": detail["observation_at_the_wall"],
                              "seed": detail["seed"],
                              "transitions_replayed": detail["transitions_replayed"]},
                             ensure_ascii=False, indent=2, sort_keys=True))
        return 0

    if args.plan:
        listing = writable_plan(entries)
        if args.plan_out:
            args.plan_out.parent.mkdir(parents=True, exist_ok=True)
            args.plan_out.write_text(json.dumps(listing, indent=2, sort_keys=True) + "\n",
                                     encoding="utf-8")
        unresolved = [e for e in listing if e["window_status"] != "resolved"]
        bad_hash = [e for e in listing if not e["checkpoint_sha256_matches"]]
        print(f"{len(listing)} file(s), "
              f"{sum(e['recorded_empty_action_mask'] for e in listing)} recorded ending(s), "
              f"{sum(e['episodes'] for e in listing)} episodes to roll")
        print(f"unresolved windows {len(unresolved)}, "
              f"checkpoint hash mismatches {len(bad_hash)}")
        for entry in unresolved + bad_hash:
            print(f"  excluded: {entry['metrics_file']} "
                  f"({entry['window_status'] if entry['window_status'] != 'resolved' else 'stale checkpoint'})")
        return 0

    workable = [e for e in entries if e["window_status"] == "resolved"
                and e["checkpoint_sha256_matches"]]
    rollable = {e["metrics_file"] for e in workable}
    skipped = [e for e in entries if e["metrics_file"] not in rollable]
    shard = workable[args.work_index::args.work_count]
    if not shard:
        raise SystemExit(f"empty shard {args.work_index}/{args.work_count}")

    import sts2_gym
    from sb3_contrib import MaskablePPO
    from stable_baselines3.common.vec_env import DummyVecEnv

    from training.v2_curriculum import _environment_factory

    rows: list[dict] = []
    cached: tuple[str, object] | None = None
    for entry in shard:
        stage = next(s for s in config.stages if s.name == entry["stage"])
        horizon = args.max_steps or stage.max_episode_steps
        open_stage = dataclasses.replace(stage, max_floor=None, max_episode_steps=horizon)
        factory = _environment_factory(config, open_stage, sts2_gym)
        # MaskablePPO keeps the whole network in memory; the plan is sorted so the same
        # checkpoint rarely repeats, and holding several at once is not the point here.
        if cached is None or cached[0] != entry["checkpoint_path"]:
            probe = DummyVecEnv([lambda: factory(entry["window_seeds"][0])])
            try:
                model = MaskablePPO.load(entry["checkpoint_path"], env=probe, device="cpu")
            finally:
                probe.close()
            cached = (entry["checkpoint_path"], model)
        model = cached[1]
        found = roll_episodes(factory, model, entry["window_seeds"][:entry["episodes"]],
                              entry["metrics_file"], horizon)
        rows.extend(found)
        print(f"[shard {args.work_index}] {entry['metrics_file']}: "
              f"{sum(1 for r in found if r['outcome'] == 'empty_action_mask')} of "
              f"{entry['recorded_empty_action_mask']} located", flush=True)
    payload = summarise(shard, rows, merged=False)
    payload["excluded_files"] = writable_plan(skipped)
    payload["shard"] = {"index": args.work_index, "count": args.work_count,
                        "files": len(shard)}
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                        encoding="utf-8")
    report(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
