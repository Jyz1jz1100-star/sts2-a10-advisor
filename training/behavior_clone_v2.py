"""V2 behaviour cloning from hash-verified teacher BC samples.

Unlike the V1 trace converter (which hash-encoded arbitrary JSON), V2 consumes
the *fixed-width expanded observation* that the student will see at inference:

* input:  the 1739-int expanded observation (verified during materialization
  against the teacher's replayed state hashes),
* output: a masked score per flat ``(action, target)`` candidate -- one
  combat head and one non-combat head (the plan's phase-split scorer),
* labels: the teacher's best action, cross-entropy restricted to the legal
  candidate set, so every gradient step lives in the same space the
  MaskablePPO policy will inherit.

The train/holdout split is by *record prefix hash*, so two decisions from the
same run can never leak across the boundary, and the holdout is a fresh
generalization estimate, not a memorization score.  Metrics are explicitly
``simulator_act1``-scoped.
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
from typing import Any, Iterator, Sequence

import torch
from torch import Tensor, nn
from torch.nn import functional as F

from .v2_flat_env import FLAT_SIZE
from .v2_observation import OBS_SIZE, observation_contract

BC_V2_CHECKPOINT_VERSION = 1
HOLDOUT_BUCKETS = 20  # bucket 0 of prefix-hash mod 20 => ~5% holdout


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


def _hash_bucket(prefix_sha: str) -> int:
    return int(prefix_sha[:8], 16) % HOLDOUT_BUCKETS


class PhaseSplitActionScorer(nn.Module):
    """Shared trunk + one linear scoring head per decision family."""

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
        # Phase heads: combat (phase 0) versus every non-combat decision.
        self.combat_head = nn.Linear(config.hidden_dim, config.action_dim)
        self.noncombat_head = nn.Linear(config.hidden_dim, config.action_dim)

    def forward(self, observation: Tensor, combat: Tensor) -> Tensor:
        """Return unmasked logits; callers apply the legal-action mask."""

        embedding = self.trunk(observation.float())
        combat_logits = self.combat_head(embedding)
        noncombat_logits = self.noncombat_head(embedding)
        select = combat.view(-1, 1)
        return torch.where(select, combat_logits, noncombat_logits)


def masked_scores(logits: Tensor, legal: Tensor) -> Tensor:
    """Reject illegal candidates with a large negative (never renormalized)."""

    return logits.masked_fill(~legal, -1e9)


def load_samples(paths: Sequence[Path]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Split BC samples into deterministic train/holdout lists by prefix hash."""

    train: list[dict[str, Any]] = []
    holdout: list[dict[str, Any]] = []
    for path in paths:
        for line in path.read_text(encoding="utf-8-sig").splitlines():
            if not line.strip():
                continue
            sample = json.loads(line)
            if sample["scope"] != "simulator_act1":
                raise ValueError("V2 BC accepts simulator_act1 samples only")
            if len(sample["observation"]) != OBS_SIZE:
                raise ValueError("sample observation violates the contract width")
            if _hash_bucket(sample["prefix_sha256"]) == 0:
                holdout.append(sample)
            else:
                train.append(sample)
    if not train or not holdout:
        raise ValueError(
            "need train and holdout samples (empty side means the dataset is "
            "too small for a hash-based split)"
        )
    return train, holdout


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
    combat = torch.tensor(
        [int(sample["phase"]) == 0 for sample in samples],
        dtype=torch.bool,
        device=device,
    )
    gaps = torch.tensor([float(sample["score_gap"]) for sample in samples],
                        dtype=torch.float32, device=device)
    return observations, legal, labels, combat, gaps


def evaluate(model: PhaseSplitActionScorer, samples, device: str) -> dict[str, Any]:
    model.eval()
    observations, legal, labels, combat, gaps = _tensors(samples, device)
    with torch.no_grad():
        logits = model(observations, combat)
        masked = masked_scores(logits, legal)
        predicted = masked.argmax(dim=-1)
        correct = (predicted == labels).float()
        # Top-3 accuracy over the legal set.
        k = min(3, int(legal.sum(dim=-1).min().item()))
        top3 = masked.topk(k, dim=-1).indices
        in_top3 = (top3 == labels.view(-1, 1)).any(dim=-1).float()
        # Unmasked argmax would pick an illegal action: the mask's value.
        raw_predicted = logits.argmax(dim=-1)
        raw_illegal = (~legal.gather(1, raw_predicted.view(-1, 1)).view(-1)).float()
    result: dict[str, Any] = {
        "episodes": len(samples),
        "top1": float(correct.mean()),
        "top3": float(in_top3.mean()),
        "unmasked_illegal_rate": float(raw_illegal.mean()),
    }
    for family, mask in (("combat", combat), ("noncombat", ~combat)):
        if bool(mask.any()):
            result[f"{family}_top1"] = float(correct[mask].mean())
            result[f"{family}_n"] = int(mask.sum())
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
    log: callable = print,
) -> tuple[PhaseSplitActionScorer, dict[str, Any]]:
    random.seed(config.random_seed)
    torch.manual_seed(config.random_seed)
    model = PhaseSplitActionScorer(config).to(device)
    optimizer = torch.optim.AdamW(
        model.parameters(), lr=config.learning_rate, weight_decay=config.weight_decay
    )
    observations, legal, labels, combat, _gaps = _tensors(train, device)
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
            logits = model(observations[index], combat[index])
            masked = masked_scores(logits, legal[index])
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
                "state": {k: v.detach().cpu() for k, v in model.state_dict().items()},
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
    model: PhaseSplitActionScorer,
    result: dict[str, Any],
    config: BCv2Config,
    *,
    sample_paths: Sequence[Path],
    train_count: int,
    holdout_count: int,
    output: Path,
) -> dict[str, Any]:
    output.parent.mkdir(parents=True, exist_ok=True)
    metadata: dict[str, Any] = {
        "checkpoint_version": BC_V2_CHECKPOINT_VERSION,
        "created_at": datetime.now(UTC).isoformat(),
        "model": "shared_trunk_phase_split_flat_action_scorer",
        "model_config": asdict(config),
        "scope": "simulator_act1",
        "disclaimer": "Act 1 emulator teacher distillation; not a real-game A10 policy.",
        "observation_contract": observation_contract(),
        "dataset": {
            "files": [
                {"path": str(path.resolve()), "bytes": path.stat().st_size,
                 "sha256": sha256_file(path)}
                for path in sample_paths
            ],
            "train_samples": train_count,
            "holdout_samples": holdout_count,
            "split": f"prefix_sha256 bucket 0 of {HOLDOUT_BUCKETS}",
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


def load_model(path: Path | str, *, verify_hash: bool = True) -> PhaseSplitActionScorer:
    path = Path(path)
    if verify_hash:
        sidecar_path = path.with_suffix(path.suffix + ".metadata.json")
        sidecar = json.loads(sidecar_path.read_text(encoding="utf-8"))
        if sha256_file(path) != sidecar["checkpoint"]["sha256"]:
            raise ValueError("checkpoint SHA-256 does not match metadata sidecar")
    payload = torch.load(path, map_location="cpu", weights_only=False)
    raw_config = payload["metadata"]["model_config"]
    model = PhaseSplitActionScorer(BCv2Config(**raw_config))
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
    train, holdout = load_samples(args.samples)
    print(json.dumps({"train": len(train), "holdout": len(holdout),
                      "device": device}))
    model, result = train_behavior_clone_v2(train, holdout, config, device=device)
    sidecar = save_checkpoint(
        model, result, config,
        sample_paths=args.samples,
        train_count=len(train), holdout_count=len(holdout),
        output=args.output,
    )
    print(json.dumps(sidecar["training"]["holdout"], ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
