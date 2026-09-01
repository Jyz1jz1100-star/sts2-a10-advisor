"""V2 behaviour cloning from hash-verified teacher BC samples.

Review corrections implemented here (2026-09-01):

* **Run-grouped splitting.** The original prefix-hash split leaked: two
  decisions from the same simulator *run* could land on opposite sides
  (measured 85.9% of holdout runs also appeared in train), so its 0.603
  top-1 is a flagged historical number only.  Samples are now grouped by
  ``(source, seed)`` — one whole run always lands on one side — and the
  split function asserts 0 shared runs (a leakage regression is fatal, not
  silently reported).
* **Per-phase heads.** Six decision-family heads (combat, map, reward,
  shop, rest, event) replace the combat/noncombat binary, per the plan's
  phase-split scorer requirement.
* **Reserved final-test range.** Seeds 1,410,000,000+ are reserved for a
  dedicated teacher-era BC test corpus (generated only by the upgraded
  beam teacher, never trained on).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import random
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Sequence

import torch
from torch import Tensor, nn
from torch.nn import functional as F

from .v2_constants import (
    PHASE_ANCIENT,
    PHASE_CARD_REWARD,
    PHASE_COMBAT,
    PHASE_EVENT,
    PHASE_MAP,
    PHASE_RELIC_REWARD,
    PHASE_REST,
    PHASE_SHOP,
    PHASE_TRANSFORM_SELECT,
    PHASE_TREASURE,
)
from .v2_flat_env import FLAT_SIZE
from .v2_observation import OBS_SIZE, observation_contract

BC_CHECKPOINT_VERSION = 2
HOLDOUT_BUCKETS = 20  # bucket < 1 of the (source|seed) digest => ~5% holdout

#: Reserved seed range for the FINAL BC test corpus (upgraded-teacher era).
#: Nothing may train on seeds >= this value; the batch generators are
#: configured to stop below it until the reserved corpus exists.
RESERVED_TEACHER_TEST_SEED_START = 1_410_000_000

#: Decision families with their own scoring head (review item 3/6).
BC_PHASES: tuple[str, ...] = ("combat", "map", "reward", "shop", "rest", "event")
PHASE_SLOT = {name: index for index, name in enumerate(BC_PHASES)}
FAMILY_BY_PHASE: dict[int, str] = {
    PHASE_COMBAT: "combat",
    PHASE_MAP: "map",
    PHASE_CARD_REWARD: "reward",
    PHASE_RELIC_REWARD: "reward",
    PHASE_TREASURE: "reward",
    PHASE_SHOP: "shop",
    PHASE_REST: "rest",
    PHASE_EVENT: "event",
    PHASE_ANCIENT: "event",
    PHASE_TRANSFORM_SELECT: "event",
}


def family_for_phase(phase: int) -> str:
    return FAMILY_BY_PHASE.get(int(phase), "event")


def family_slot_for_phase(phase: int) -> int:
    return PHASE_SLOT[family_for_phase(phase)]


@dataclass(frozen=True)
class BCv2Config:
    input_dim: int = OBS_SIZE
    action_dim: int = FLAT_SIZE
    hidden_dim: int = 512
    depth: int = 3
    dropout: float = 0.05
    learning_rate: float = 3e-4
    weight_decay: float = 1e-4
    batch_size: int = 128
    epochs: int = 30
    random_seed: int = 20260901
    grad_clip: float = 1.0

    def __post_init__(self) -> None:
        if self.input_dim != OBS_SIZE:
            raise ValueError("V2 BC input width must equal the observation contract")
        if self.action_dim != FLAT_SIZE:
            raise ValueError("V2 BC action width must equal the flat action space")
        if self.depth < 1 or self.batch_size < 1 or self.epochs < 1:
            raise ValueError("depth, batch_size, and epochs must be positive")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def run_key_of(sample: dict[str, Any]) -> tuple[str, int]:
    """Whole-run grouping key: one simulator run, one split side.

    DAgger samples from the *same student run id* share the seed key; the
    source tag keeps the teacher/DAgger lineage separate in the key but the
    leakage guarantee below is enforced on seeds within each source, which is
    the same generation process, so no cross-side trajectory mixing is
    possible either way.
    """

    return (str(sample.get("source", "teacher")), int(sample["seed"]))


def holdout_bucket(run_key: tuple[str, int]) -> int:
    digest = hashlib.blake2b(f"{run_key[0]}|{run_key[1]}".encode(), digest_size=8)
    return int.from_bytes(digest.digest(), "big") % HOLDOUT_BUCKETS


class PhaseHeadActionScorer(nn.Module):
    """Shared trunk + one linear scoring head per decision family."""

    def __init__(self, config: BCv2Config, phases: tuple[str, ...] = BC_PHASES):
        super().__init__()
        self.config = config
        self.phases = tuple(phases)
        layers: list[nn.Module] = []
        width = config.input_dim
        for _ in range(config.depth):
            layers += [
                nn.Linear(width, config.hidden_dim),
                nn.GELU(),
                nn.LayerNorm(config.hidden_dim),
            ]
            if config.dropout:
                layers.append(nn.Dropout(config.dropout))
            width = config.hidden_dim
        self.trunk = nn.Sequential(*layers)
        self.heads = nn.ModuleList(
            nn.Linear(config.hidden_dim, config.action_dim) for _ in self.phases
        )

    @property
    def family_index(self) -> bool:
        return True

    def forward(self, observation: Tensor, family: Tensor) -> Tensor:
        """Route every row of a batch to its decision-family head.

        ``family`` is a long tensor of head slots (see ``PHASE_SLOT``).
        Bool inputs keep the v1 convention (True = combat) and map
        True->combat, False->event so no silent head misassignment occurs.
        """

        if family.dtype == torch.bool:
            family = torch.where(
                family,
                torch.zeros_like(family, dtype=torch.long),          # combat
                torch.full_like(family, PHASE_SLOT["event"], dtype=torch.long),
            )
        embedding = self.trunk(observation.float())
        logits = torch.zeros(
            embedding.shape[0], self.config.action_dim, device=embedding.device
        )
        for slot in range(len(self.phases)):
            rows = family == slot
            if bool(rows.any()):
                logits[rows] = self.heads[slot](embedding[rows])
        return logits


class _LegacyScorerV1(nn.Module):
    """The original two-head (combat/noncombat) scorer architecture.

    Retained only so already-saved ``checkpoint_version: 1`` artifacts load
    and can still be *evaluated* on the corrected harness; it must not be
    used for new training runs.
    """

    def __init__(self, config: BCv2Config):
        super().__init__()
        self.config = config
        layers: list[nn.Module] = []
        width = config.input_dim
        for _ in range(config.depth):
            layers += [
                nn.Linear(width, config.hidden_dim),
                nn.GELU(),
                nn.LayerNorm(config.hidden_dim),
            ]
            if config.dropout:
                layers.append(nn.Dropout(config.dropout))
            width = config.hidden_dim
        self.trunk = nn.Sequential(*layers)
        self.combat_head = nn.Linear(config.hidden_dim, config.action_dim)
        self.noncombat_head = nn.Linear(config.hidden_dim, config.action_dim)

    @property
    def family_index(self) -> bool:
        return False

    def forward(self, observation: Tensor, family: Tensor) -> Tensor:
        if family.dtype != torch.bool:
            # Family slot 0 is combat; every other slot used the noncombat
            # head in v1, which is exactly what this maps to.
            family = family != 0
        embedding = self.trunk(observation.float())
        return torch.where(
            family.view(-1, 1),
            self.combat_head(embedding),
            self.noncombat_head(embedding),
        )


def masked_scores(logits: Tensor, legal: Tensor) -> Tensor:
    """Reject illegal candidates with a large negative (never renormalized)."""

    return logits.masked_fill(~legal, -1e9)


def split_samples(
    samples: Sequence[dict[str, Any]], *, holdout_bucket_size: int = 1
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    """Run-grouped train/holdout split with a hard no-leakage assertion."""

    train: list[dict[str, Any]] = []
    holdout: list[dict[str, Any]] = []
    for sample in samples:
        seed = int(sample["seed"])
        if seed >= RESERVED_TEACHER_TEST_SEED_START:
            continue  # reserved final-test range is never a training input
        if holdout_bucket(run_key_of(sample)) < holdout_bucket_size:
            holdout.append(sample)
        else:
            train.append(sample)
    train_runs = {run_key_of(s) for s in train}
    holdout_runs = {run_key_of(s) for s in holdout}
    shared = train_runs & holdout_runs
    statistics = {
        "train_samples": len(train),
        "holdout_samples": len(holdout),
        "train_runs": len(train_runs),
        "holdout_runs": len(holdout_runs),
        "shared_runs": len(shared),
        "holdout_run_leakage_rate": (
            round(len(shared) / len(holdout_runs), 6) if holdout_runs else 0.0
        ),
        "skipped_reserved_test_seeds": sum(
            1 for s in samples if int(s["seed"]) >= RESERVED_TEACHER_TEST_SEED_START
        ),
        "split": f"blake2b(source|seed) bucket < {holdout_bucket_size}/{HOLDOUT_BUCKETS} (whole-run grouped)",
    }
    if statistics["shared_runs"] != 0:
        raise AssertionError(
            f"run-grouping violated: {statistics['shared_runs']} (source, seed) "
            "runs appear on both sides of the split"
        )
    return train, holdout, statistics


def load_samples(
    paths: Sequence[Path], *, holdout_bucket_size: int = 1
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    """Load BC samples and produce a leak-free run-grouped split."""

    all_samples: list[dict[str, Any]] = []
    for path in paths:
        for line in path.read_text(encoding="utf-8-sig").splitlines():
            if not line.strip():
                continue
            sample = json.loads(line)
            if sample["scope"] != "simulator_act1":
                raise ValueError("V2 BC accepts simulator_act1 samples only")
            if len(sample["observation"]) != OBS_SIZE:
                raise ValueError("sample observation violates the contract width")
            all_samples.append(sample)
    train, holdout, statistics = split_samples(
        all_samples, holdout_bucket_size=holdout_bucket_size
    )
    if not train or not holdout:
        raise ValueError(
            "need train and holdout samples (empty side means the dataset is "
            "too small for a run-grouped split)"
        )
    return train, holdout, statistics


def _tensors(samples: Sequence[dict[str, Any]], device: str):
    observations = torch.tensor(
        [sample["observation"] for sample in samples], dtype=torch.int32, device=device
    )
    legal = torch.zeros(
        (len(samples), FLAT_SIZE), dtype=torch.bool, device=device
    )
    for row, sample in enumerate(samples):
        legal[row, sample["legal_flat_actions"]] = True
    labels = torch.tensor(
        [sample["label_flat_action"] for sample in samples],
        dtype=torch.long,
        device=device,
    )
    family = torch.tensor(
        [family_slot_for_phase(int(sample["phase"])) for sample in samples],
        dtype=torch.long,
        device=device,
    )
    gaps = torch.tensor(
        [float(sample["score_gap"]) for sample in samples],
        dtype=torch.float32,
        device=device,
    )
    return observations, legal, labels, family, gaps


def evaluate(model: nn.Module, samples, device: str) -> dict[str, Any]:
    model.eval()
    observations, legal, labels, family, gaps = _tensors(samples, device)
    with torch.no_grad():
        masked = masked_scores(model(observations, family), legal)
        predicted = masked.argmax(dim=-1)
        correct = (predicted == labels).float()
        k = min(3, int(legal.sum(dim=-1).min().item()))
        top3 = masked.topk(k, dim=-1).indices
        in_top3 = (top3 == labels.view(-1, 1)).any(dim=-1).float()
    result: dict[str, Any] = {
        "episodes": len(samples),
        "top1": float(correct.mean()),
        "top3": float(in_top3.mean()),
    }
    for name, slot in PHASE_SLOT.items():
        rows = family == slot
        if bool(rows.any()):
            result[f"{name}_top1"] = float(correct[rows].mean())
            result[f"{name}_n"] = int(rows.sum())
    for label, low, high in (("gap_lt1", 0.0, 1.0), ("gap_1_2", 1.0, 2.0),
                             ("gap_ge2", 2.0, math.inf)):
        bucket = (gaps >= low) & (gaps < high)
        if bool(bucket.any()):
            result[f"top1[{label}]"] = float(correct[bucket].mean())
            result[f"n[{label}]"] = int(bucket.sum())
    return result


def train_behavior_clone_v2(
    train: Sequence[dict[str, Any]],
    holdout: Sequence[dict[str, Any]],
    config: BCv2Config,
    *,
    device: str = "cpu",
    log: Any = print,
) -> tuple[nn.Module, dict[str, Any]]:
    random.seed(config.random_seed)
    torch.manual_seed(config.random_seed)
    model = PhaseHeadActionScorer(config).to(device)
    optimizer = torch.optim.AdamW(
        model.parameters(), lr=config.learning_rate, weight_decay=config.weight_decay
    )
    observations, legal, labels, family, _gaps = _tensors(train, device)
    n = observations.shape[0]
    history: list[dict[str, Any]] = []
    best = {"top1": -1.0, "state": None, "epoch": 0, "metrics": {}}
    for epoch in range(1, config.epochs + 1):
        model.train()
        permutation = torch.randperm(n, device=device)
        total_loss = 0.0
        batches = 0
        for start in range(0, n, config.batch_size):
            index = permutation[start : start + config.batch_size]
            optimizer.zero_grad(set_to_none=True)
            masked = masked_scores(model(observations[index], family[index]),
                                   legal[index])
            loss = F.cross_entropy(masked, labels[index])
            loss.backward()
            if config.grad_clip:
                torch.nn.utils.clip_grad_norm_(model.parameters(), config.grad_clip)
            optimizer.step()
            total_loss += float(loss.detach().cpu())
            batches += 1
        metrics = evaluate(model, holdout, device)
        metrics["epoch"] = epoch
        metrics["train_loss"] = total_loss / max(batches, 1)
        history.append(metrics)
        log(
            f"epoch {epoch:02d} loss {metrics['train_loss']:.4f} "
            f"holdout top1 {metrics['top1']:.4f} top3 {metrics['top3']:.4f}"
        )
        if metrics["top1"] > best["top1"]:
            best = {
                "top1": metrics["top1"],
                "state": {key: value.detach().cpu()
                          for key, value in model.state_dict().items()},
                "epoch": epoch,
                "metrics": dict(metrics),
            }
    if best["state"] is not None:
        model.load_state_dict(best["state"])
    final = {
        "best_epoch": best["epoch"],
        "holdout": evaluate(model, holdout, device),
        "history": history,
    }
    return model, final


def save_checkpoint(
    model: nn.Module,
    result: dict[str, Any],
    config: BCv2Config,
    *,
    sample_paths: Sequence[Path],
    split_statistics: dict[str, Any],
    output: Path,
) -> dict[str, Any]:
    output.parent.mkdir(parents=True, exist_ok=True)
    metadata: dict[str, Any] = {
        "checkpoint_version": BC_CHECKPOINT_VERSION,
        "created_at": datetime.now(UTC).isoformat(),
        "model": "shared_trunk_phase_head_flat_action_scorer",
        "phases": list(BC_PHASES),
        "model_config": asdict(config),
        "scope": "simulator_act1",
        "disclaimer": "Act 1 emulator teacher distillation; not a real-game A10 policy.",
        "teacher_status_note": (
            "labels come from the frozen R2 greedy-rollout teacher (replay-"
            "consistent, not optimality-certified); strength-gated beam "
            "teacher replaces them in v3"
        ),
        "observation_contract": observation_contract(),
        "dataset": {
            "files": [
                {"path": str(path.resolve()), "bytes": path.stat().st_size,
                 "sha256": sha256_file(path)}
                for path in sample_paths
            ],
            **split_statistics,
        },
        "training": result,
        "torch_version": torch.__version__,
    }
    torch.save({"metadata": metadata, "state_dict": model.state_dict()}, output)
    checkpoint_sha = sha256_file(output)
    sidecar = dict(metadata)
    sidecar["checkpoint"] = {
        "path": str(output.resolve()),
        "bytes": output.stat().st_size,
        "sha256": checkpoint_sha,
    }
    output.with_suffix(output.suffix + ".metadata.json").write_text(
        json.dumps(sidecar, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    return sidecar


def load_model(path: Path | str, *, verify_hash: bool = True) -> nn.Module:
    """Load any BC checkpoint; v1 artifacts load into the legacy architecture.

    The returned module always has ``forward(observation, family_long)``, so
    callers are architecture-agnostic.
    """

    path = Path(path)
    if verify_hash:
        sidecar_path = path.with_suffix(path.suffix + ".metadata.json")
        sidecar = json.loads(sidecar_path.read_text(encoding="utf-8"))
        if sha256_file(path) != sidecar["checkpoint"]["sha256"]:
            raise ValueError("checkpoint SHA-256 does not match metadata sidecar")
    payload = torch.load(path, map_location="cpu", weights_only=False)
    raw_config = payload["metadata"]["model_config"]
    config = BCv2Config(**raw_config)
    version = int(payload["metadata"].get("checkpoint_version", 1))
    if version >= BC_CHECKPOINT_VERSION:
        phases = tuple(payload["metadata"].get("phases", BC_PHASES))
        model = PhaseHeadActionScorer(config, phases)
    else:
        model = _LegacyScorerV1(config)
    model.load_state_dict(payload["state_dict"])
    model.eval()
    return model


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--samples", nargs="+", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--epochs", type=int, default=30)
    parser.add_argument("--batch-size", type=int, default=128)
    parser.add_argument("--hidden-dim", type=int, default=512)
    parser.add_argument("--depth", type=int, default=3)
    parser.add_argument("--learning-rate", type=float, default=3e-4)
    parser.add_argument("--seed", type=int, default=20260901)
    parser.add_argument("--device", default="cpu", choices=("cpu", "cuda", "auto"))
    args = parser.parse_args(argv)
    device = args.device
    if device == "auto":
        device = "cuda" if torch.cuda.is_available() else "cpu"

    config = BCv2Config(
        hidden_dim=args.hidden_dim,
        depth=args.depth,
        learning_rate=args.learning_rate,
        batch_size=args.batch_size,
        epochs=args.epochs,
        random_seed=args.seed,
    )
    train, holdout, statistics = load_samples(args.samples)
    print(json.dumps({"device": device, **statistics}, ensure_ascii=False))
    model, result = train_behavior_clone_v2(train, holdout, config, device=device)
    sidecar = save_checkpoint(
        model, result, config,
        sample_paths=args.samples,
        split_statistics=statistics,
        output=args.output,
    )
    print(json.dumps(sidecar["training"]["holdout"], ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "BC_CHECKPOINT_VERSION",
    "BC_PHASES",
    "PHASE_SLOT",
    "RESERVED_TEACHER_TEST_SEED_START",
    "BCv2Config",
    "PhaseHeadActionScorer",
    "evaluate",
    "family_for_phase",
    "family_slot_for_phase",
    "load_model",
    "load_samples",
    "masked_scores",
    "run_key_of",
    "save_checkpoint",
    "split_samples",
    "train_behavior_clone_v2",
]
