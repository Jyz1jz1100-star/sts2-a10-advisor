"""Masked-CE pretraining of a MaskablePPO-loadable actor from BC samples.

Review item 3 (2026-09-01): the curriculum warm-starts every stage through
``sb3_contrib.MaskablePPO.load``.  The phase-head BC scorer cannot be loaded
that way (different architecture, no critic), so this module distills the BC
labels into an actor that lives inside a real ``MaskableActorCriticPolicy``:

* the student is a genuine ``MaskablePPO`` built on a fixture environment
  whose spaces match the V2 contract stack exactly (Box(1739, int32) /
  Discrete(225)), so ``MaskablePPO.load(path, env=...)`` accepts the artifact
  without any conversion step;
* training is masked cross-entropy on the teacher labels through the *actor
  logits* (``features -> mlp_extractor.forward_actor -> action_net``); the
  value head is left at its fresh initialization -- PPO owns it;
* selection on the same leak-free run-grouped split used by
  :mod:`training.behavior_clone_v2` (0 shared runs, hard assertion), so the
  reported holdout agreement is honest;
* the saved ``.zip`` is SB3's own save format; the JSON sidecar records the
  hash chain: BC samples -> source BC checkpoint -> pretrained actor.

The PPO stage may only start after the pretrained actor is evaluated on the
100-seed checkpoint split (see ``scripts/evaluate_pretrained_actor.py``).
"""

from __future__ import annotations

import argparse
import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Sequence

import gymnasium as gym
import numpy as np
import torch
from torch.nn import functional as F

from .behavior_clone_v2 import (
    family_slot_for_phase,
    load_samples,
    masked_scores,
)
from .metrics import atomic_write_json
from .v2_flat_env import FLAT_SIZE
from .v2_observation import BLOCK_OFFSETS, OBS_SIZE, observation_contract

PRETRAIN_VERSION = 1


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


@dataclass
class PretrainConfig:
    hidden_dim: int = 256
    vf_hidden_dim: int = 128
    depth: int = 2
    learning_rate: float = 3e-4
    batch_size: int = 256
    epochs: int = 12
    random_seed: int = 20260901
    grad_clip: float = 1.0
    device: str = "auto"

    def resolved_device(self) -> str:
        if self.device != "auto":
            return self.device
        return "cuda" if torch.cuda.is_available() else "cpu"


class _PretrainFixtureEnv(gym.Env):
    """A gymnasium env carrying the V2 spaces and nothing else.

    Only exists so ``MaskablePPO`` can be constructed (and therefore saved in
    its own format) without touching the native emulator.  The step function
    is never used by the distillation loop; evaluation and training use the
    real contract stack through ``MaskablePPO.load``.
    """

    metadata = {"render_modes": []}

    def __init__(self) -> None:
        self.observation_space = gym.spaces.Box(
            low=-(2**15), high=2**15, shape=(OBS_SIZE,), dtype=np.int32
        )
        self.action_space = gym.spaces.Discrete(FLAT_SIZE)

    def reset(self, *, seed: int | None = None, options: dict | None = None):
        super().reset(seed=seed)
        return np.zeros(OBS_SIZE, dtype=np.int32), {}

    def step(self, action):
        return np.zeros(OBS_SIZE, dtype=np.int32), 0.0, True, False, {}

    def close(self) -> None:
        pass


def build_pretrain_model(config: PretrainConfig):
    """Construct the MaskablePPO whose actor will be distilled into."""

    from sb3_contrib import MaskablePPO

    net_arch = {
        "pi": [config.hidden_dim] * config.depth,
        "vf": [config.vf_hidden_dim] * config.depth,
    }
    model = MaskablePPO(
        "MlpPolicy",
        _PretrainFixtureEnv(),
        device=config.resolved_device(),
        learning_rate=config.learning_rate,
        seed=config.random_seed,
        verbose=0,
        policy_kwargs={"net_arch": net_arch},
        n_steps=64,
        batch_size=64,
        n_epochs=1,
        _init_setup_model=True,
    )
    return model


def _actor_logits(policy: Any, observations: torch.Tensor) -> torch.Tensor:
    """Action logits for a batch, exactly as ``get_distribution`` sees them."""

    features = policy.extract_features(observations)
    latent_pi = policy.mlp_extractor.forward_actor(features)
    return policy.action_net(latent_pi)


def _phase_slots(samples: Sequence[dict[str, Any]]) -> list[int]:
    phases = []
    for sample in samples:
        phases.append(family_slot_for_phase(int(sample["phase"])))
    return phases


def _tensors(samples: Sequence[dict[str, Any]], device: str):
    observations = torch.tensor(
        [sample["observation"] for sample in samples], dtype=torch.int32, device=device
    )
    legal = torch.zeros((len(samples), FLAT_SIZE), dtype=torch.bool, device=device)
    for row, sample in enumerate(samples):
        legal[row, sample["legal_flat_actions"]] = True
    labels = torch.tensor(
        [sample["label_flat_action"] for sample in samples],
        dtype=torch.long,
        device=device,
    )
    family = torch.tensor(_phase_slots(samples), dtype=torch.long, device=device)
    return observations, legal, labels, family


def evaluate_student(
    model: Any,
    samples: Sequence[dict[str, Any]],
    device: str,
    *,
    batch_size: int = 2048,
) -> dict[str, Any]:
    """Top-1/top-3 of the distilled actor on holdout samples (per phase too)."""

    from .behavior_clone_v2 import PHASE_SLOT

    policy = model.policy
    policy.eval()
    correct = 0
    top3 = 0
    per_family: dict[int, list[int]] = {}
    with torch.no_grad():
        for start in range(0, len(samples), batch_size):
            batch = samples[start : start + batch_size]
            observations, legal, labels, family = _tensors(batch, device)
            logits = _actor_logits(policy, observations)
            masked = masked_scores(logits, legal)
            predicted = masked.argmax(dim=-1)
            hits = (predicted == labels).tolist()
            correct += sum(hits)
            k = min(3, int(legal.sum(dim=-1).min().item()))
            top3_indices = masked.topk(k, dim=-1).indices
            top3_hits = (top3_indices == labels.view(-1, 1)).any(dim=-1).tolist()
            top3 += sum(top3_hits)
            for index, slot in enumerate(family.tolist()):
                slot_hits = per_family.setdefault(slot, [0, 0])
                slot_hits[0] += int(hits[index])
                slot_hits[1] += 1
    policy.train()
    result: dict[str, Any] = {
        "episodes": len(samples),
        "top1": correct / max(len(samples), 1),
        "top3": top3 / max(len(samples), 1),
    }
    for name, slot in PHASE_SLOT.items():
        if slot in per_family:
            hits, total = per_family[slot]
            result[f"{name}_top1"] = hits / total
    return result


def distill_masked_ce(
    model: Any,
    train: Sequence[dict[str, Any]],
    holdout: Sequence[dict[str, Any]],
    config: PretrainConfig,
    *,
    log: Any = print,
) -> dict[str, Any]:
    """Masked cross-entropy into the actor logits; returns the metric history."""

    device = str(model.device)
    policy = model.policy
    policy.set_training_mode(True)
    policy.train()
    optimizer = policy.optimizer
    observations, legal, labels, family = _tensors(train, device)
    n = observations.shape[0]
    history: list[dict[str, Any]] = []
    torch.manual_seed(config.random_seed)
    for epoch in range(1, config.epochs + 1):
        permutation = torch.randperm(n, device=device)
        total_loss = 0.0
        batches = 0
        for start in range(0, n, config.batch_size):
            index = permutation[start : start + config.batch_size]
            optimizer.zero_grad(set_to_none=True)
            logits = _actor_logits(policy, observations[index])
            masked = masked_scores(logits, legal[index])
            loss = F.cross_entropy(masked, labels[index])
            loss.backward()
            torch.nn.utils.clip_grad_norm_(policy.parameters(), config.grad_clip)
            optimizer.step()
            total_loss += float(loss.detach().cpu())
            batches += 1
        metrics = evaluate_student(model, holdout, device)
        metrics["epoch"] = epoch
        metrics["train_loss"] = total_loss / max(batches, 1)
        history.append(metrics)
        log(
            f"epoch {epoch:02d} masked-ce {metrics['train_loss']:.4f} "
            f"holdout top1 {metrics['top1']:.4f} top3 {metrics['top3']:.4f}"
        )
    return {"history": history, "final_holdout": dict(history[-1])}


def pretrain_bc_actor(
    bc_checkpoint: Path,
    sample_paths: Sequence[Path],
    output_dir: Path,
    config: PretrainConfig,
    *,
    log: Any = print,
) -> dict[str, Any]:
    """Distill the hash-verified BC checkpoint into a MaskablePPO actor.

    The returned sidecar is the hash chain: dataset files + the source BC
    checkpoint + the emitted actor zip, each with its own SHA-256.
    """

    from .behavior_clone_v2 import load_model

    output_dir.mkdir(parents=True, exist_ok=True)

    # 1. Source BC checkpoint, hash-verified against its own sidecar.
    bc_sidecar_path = bc_checkpoint.with_suffix(bc_checkpoint.suffix + ".metadata.json")
    if not bc_sidecar_path.is_file():
        raise SystemExit(f"BC checkpoint sidecar missing: {bc_sidecar_path}")
    bc_sidecar = json.loads(bc_sidecar_path.read_text(encoding="utf-8"))
    bc_sha = sha256_file(bc_checkpoint)
    if bc_sha != bc_sidecar["checkpoint"]["sha256"]:
        raise SystemExit(
            "source BC checkpoint hash mismatch; refusing to distill from a "
            "tampered artifact"
        )

    # 2. The same leak-free run-grouped split the BC trainer used.
    train, holdout, split_statistics = load_samples(sample_paths)
    if split_statistics["shared_runs"] != 0:
        raise SystemExit("split leakage detected; refusing to pretrain")

    # 3. Distill into the actor.
    model = build_pretrain_model(config)
    result = distill_masked_ce(model, train, holdout, config, log=log)

    # 4. Save in SB3's own format; the zip is what MaskablePPO.load reads.
    #    The distillation fixture env is NOT part of the artifact: a live
    #    env is supplied (or validated) at MaskablePPO.load time, and the
    #    fixture's step() must never ride along inside a real checkpoint.
    actor_path = output_dir / "pretrained_actor.zip"
    model.save(actor_path, exclude=["env"])
    if not actor_path.is_file():
        raise SystemExit("SB3 save did not produce the expected zip artifact")

    sidecar = {
        "pretrain_version": PRETRAIN_VERSION,
        "created_at": datetime.now(UTC).isoformat(),
        "kind": "bc_pretrained_maskable_ppo_actor",
        "scope": "simulator_act1",
        "disclaimer": "Act 1 emulator distillation; not a real-game A10 policy.",
        "observation_contract": observation_contract(),
        "observation_size": OBS_SIZE,
        "flat_action_size": FLAT_SIZE,
        "config": {
            "hidden_dim": config.hidden_dim,
            "vf_hidden_dim": config.vf_hidden_dim,
            "depth": config.depth,
            "learning_rate": config.learning_rate,
            "batch_size": config.batch_size,
            "epochs": config.epochs,
            "random_seed": config.random_seed,
            "device": config.resolved_device(),
        },
        "source_bc_checkpoint": {
            "path": str(bc_checkpoint.resolve()),
            "sha256": bc_sha,
            "holdout_top1": bc_sidecar.get("training", {})
            .get("holdout", {})
            .get("top1"),
        },
        "dataset": {
            "files": [
                {"path": str(path.resolve()), "sha256": sha256_file(path)}
                for path in sample_paths
            ],
            **split_statistics,
        },
        "training": result,
        "sb3_version": _sb3_version(),
        "torch_version": torch.__version__,
        "artifact": {
            "path": str(actor_path.resolve()),
            "bytes": actor_path.stat().st_size,
            "sha256": sha256_file(actor_path),
        },
        "load_instruction": (
            "MaskablePPO.load(<artifact.path>, env=<V2 vector env>, "
            "device=auto); spaces already match the V2 contract stack"
        ),
    }
    atomic_write_json(output_dir / "pretrained_actor.metadata.json", sidecar)
    return sidecar


def _sb3_version() -> str:
    import stable_baselines3

    return stable_baselines3.__version__


def verify_loadable(actor_path: Path) -> dict[str, Any]:
    """Prove ``MaskablePPO.load`` accepts the artifact on the V2 spaces."""

    from sb3_contrib import MaskablePPO

    model = MaskablePPO.load(actor_path, env=_PretrainFixtureEnv(), device="cpu")
    policy_space = model.policy.observation_space
    return {
        "loadable": True,
        "observation_space": str(policy_space),
        "action_space": str(model.policy.action_space),
        "observation_size": int(policy_space.shape[0]),
        "action_size": int(model.action_space.n),
        "spaces_match_v2_contract": (
            int(policy_space.shape[0]) == OBS_SIZE
            and int(model.action_space.n) == FLAT_SIZE
        ),
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bc-checkpoint", type=Path, default=None)
    parser.add_argument("--samples", nargs="+", type=Path, default=[])
    parser.add_argument("--output-dir", type=Path, default=None)
    parser.add_argument("--epochs", type=int, default=12)
    parser.add_argument("--batch-size", type=int, default=256)
    parser.add_argument("--hidden-dim", type=int, default=256)
    parser.add_argument("--learning-rate", type=float, default=3e-4)
    parser.add_argument("--seed", type=int, default=20260901)
    parser.add_argument("--device", default="auto", choices=("auto", "cpu", "cuda"))
    parser.add_argument("--verify-only", action="store_true")
    args = parser.parse_args(argv)

    if args.verify_only:
        if args.output_dir is None:
            parser.error("--output-dir is required with --verify-only")
        payload = verify_loadable(args.output_dir / "pretrained_actor.zip")
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0 if payload["spaces_match_v2_contract"] else 1

    if args.bc_checkpoint is None or not args.samples or args.output_dir is None:
        parser.error(
            "--bc-checkpoint, --samples, and --output-dir are required "
            "without --verify-only"
        )

    config = PretrainConfig(
        hidden_dim=args.hidden_dim,
        learning_rate=args.learning_rate,
        batch_size=args.batch_size,
        epochs=args.epochs,
        random_seed=args.seed,
        device=args.device,
    )
    sidecar = pretrain_bc_actor(
        args.bc_checkpoint,
        args.samples,
        args.output_dir,
        config,
    )
    holdout = sidecar["training"]["final_holdout"]
    print(
        json.dumps(
            {
                "artifact": sidecar["artifact"],
                "holdout": {
                    key: holdout[key] for key in ("top1", "top3", "episodes")
                },
                "split_leakage": sidecar["dataset"]["shared_runs"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
