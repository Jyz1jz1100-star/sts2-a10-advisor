from __future__ import annotations

import argparse
import hashlib
import json
import math
import random
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable, Sequence

import torch
from torch import Tensor, nn
from torch.nn import functional as F

from training.trace_contract import (
    CURRENT_PUBLIC_BETA_BUILD,
    DEFAULT_ASCENSION,
    DEFAULT_CHARACTER,
)
from training.validate_traces import validate_files


BC_CHECKPOINT_VERSION = 1
DEFAULT_PHASES = (
    "combat",
    "card_reward",
    "map",
    "event",
    "shop",
    "rest_site",
    "treasure",
    "boss_relic",
    "card_select",
    "other",
)


@dataclass(frozen=True)
class BCConfig:
    feature_dim: int = 256
    hidden_dim: int = 128
    phases: tuple[str, ...] = DEFAULT_PHASES
    learning_rate: float = 1e-3
    weight_decay: float = 1e-5
    epochs: int = 5
    random_seed: int = 20260831

    def __post_init__(self) -> None:
        if self.feature_dim <= 0 or self.hidden_dim <= 0:
            raise ValueError("feature_dim and hidden_dim must be positive")
        if self.epochs <= 0:
            raise ValueError("epochs must be positive")
        if not self.phases or len(set(self.phases)) != len(self.phases):
            raise ValueError("phases must be non-empty and unique")
        if "other" not in self.phases:
            raise ValueError("phases must contain an 'other' fallback head")


@dataclass(frozen=True)
class TraceExample:
    phase: str
    visible_state: dict[str, Any]
    legal_actions: tuple[dict[str, Any], ...]
    chosen_index: int


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def dataset_digest(paths: Sequence[Path]) -> str:
    digest = hashlib.sha256()
    for path in sorted((path.resolve() for path in paths), key=lambda item: str(item)):
        digest.update(str(path).encode("utf-8"))
        digest.update(b"\0")
        digest.update(sha256_file(path).encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()


def _canonical_tokens(value: object, prefix: str = "$") -> Iterable[str]:
    """Flatten visible JSON into deterministic typed tokens.

    The encoder deliberately sees only ``visible_state`` or a legal action object.
    It never reads ``result``, seed, split, or any future state.
    """

    if isinstance(value, dict):
        for key in sorted(value):
            yield from _canonical_tokens(value[key], f"{prefix}.{key}")
    elif isinstance(value, list):
        yield f"{prefix}#len={len(value)}"
        for index, item in enumerate(value):
            yield from _canonical_tokens(item, f"{prefix}[{index}]")
    elif isinstance(value, bool):
        yield f"{prefix}:bool={str(value).lower()}"
    elif value is None:
        yield f"{prefix}:null"
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        number = float(value)
        yield f"{prefix}:number:sign={0 if number == 0 else (1 if number > 0 else -1)}"
        yield f"{prefix}:number:log2={int(math.copysign(math.log2(abs(number) + 1), number))}"
        yield f"{prefix}:number:exact={number:.6g}"
    else:
        yield f"{prefix}:string={str(value).strip()}"


def hashed_features(value: object, dimension: int) -> Tensor:
    """Feature-hash arbitrary visible JSON without a learned, patch-stale vocabulary."""

    vector = torch.zeros(dimension, dtype=torch.float32)
    for token in _canonical_tokens(value):
        token_digest = hashlib.blake2b(token.encode("utf-8"), digest_size=9).digest()
        index = int.from_bytes(token_digest[:8], "little") % dimension
        sign = 1.0 if token_digest[8] & 1 else -1.0
        vector[index] += sign
    norm = vector.norm(p=2)
    if norm > 0:
        vector /= norm
    return vector


def phase_from_state(visible_state: dict[str, Any]) -> str:
    raw = visible_state.get("state_type", visible_state.get("phase", "other"))
    phase = str(raw).strip().lower().replace("-", "_")
    aliases = {
        "combat_reward": "card_reward",
        "reward": "card_reward",
        "rest": "rest_site",
        "merchant": "shop",
    }
    return aliases.get(phase, phase or "other")


def load_trace_examples(
    paths: Sequence[Path],
    *,
    split: str = "train",
    expected_build: str = CURRENT_PUBLIC_BETA_BUILD,
) -> list[TraceExample]:
    """Load only validated chosen-action demonstrations from JSONL."""

    report = validate_files(
        paths,
        expected_build=expected_build,
        expected_character=DEFAULT_CHARACTER,
        expected_ascension=DEFAULT_ASCENSION,
        require_no_save_load=True,
    )
    if not report.ok:
        preview = "\n".join(issue.render() for issue in report.issues[:20])
        raise ValueError(f"trace validation failed with {len(report.issues)} issue(s):\n{preview}")

    examples: list[TraceExample] = []
    for path in paths:
        with path.open("r", encoding="utf-8-sig") as handle:
            for raw_line in handle:
                if not raw_line.strip():
                    continue
                record = json.loads(raw_line)
                if record["split"] != split:
                    continue
                if record["result"]["status"] not in {"applied", "terminal"}:
                    continue
                if not record["result"]["observed"]:
                    continue
                chosen_id = record["chosen_action"]["action_id"]
                legal_actions = tuple(record["legal_actions"])
                chosen_index = next(
                    index
                    for index, action in enumerate(legal_actions)
                    if action["action_id"] == chosen_id
                )
                examples.append(
                    TraceExample(
                        phase=phase_from_state(record["visible_state"]),
                        visible_state=record["visible_state"],
                        legal_actions=legal_actions,
                        chosen_index=chosen_index,
                    )
                )
    if not examples:
        raise ValueError(f"no observed applied/terminal examples in split {split!r}")
    return examples


class PhaseActionScoringNetwork(nn.Module):
    """Shared visible-state encoder with an independent scorer per decision phase."""

    def __init__(self, config: BCConfig):
        super().__init__()
        self.config = config
        self.state_encoder = nn.Sequential(
            nn.Linear(config.feature_dim, config.hidden_dim),
            nn.GELU(),
            nn.LayerNorm(config.hidden_dim),
        )
        self.action_encoder = nn.Sequential(
            nn.Linear(config.feature_dim, config.hidden_dim),
            nn.GELU(),
            nn.LayerNorm(config.hidden_dim),
        )
        self.phase_heads = nn.ModuleDict(
            {
                phase: nn.Sequential(
                    nn.Linear(config.hidden_dim * 4, config.hidden_dim),
                    nn.GELU(),
                    nn.Linear(config.hidden_dim, 1),
                )
                for phase in config.phases
            }
        )

    def normalized_phase(self, phase: str) -> str:
        return phase if phase in self.phase_heads else "other"

    def score_legal_actions(
        self,
        visible_state: dict[str, Any],
        phase: str,
        legal_actions: Sequence[dict[str, Any]],
    ) -> Tensor:
        """Return scores for exactly the supplied legal actions, never a global action set."""

        if not legal_actions:
            raise ValueError("legal_actions must be non-empty")
        action_ids = [action.get("action_id") for action in legal_actions]
        if any(not isinstance(action_id, str) or not action_id for action_id in action_ids):
            raise ValueError("every legal action must have a non-empty action_id")
        if len(set(action_ids)) != len(action_ids):
            raise ValueError("legal action ids must be unique")

        device = next(self.parameters()).device
        state_features = hashed_features(visible_state, self.config.feature_dim).to(device)
        action_features = torch.stack(
            [hashed_features(action, self.config.feature_dim) for action in legal_actions]
        ).to(device)
        state_embedding = self.state_encoder(state_features).unsqueeze(0)
        state_embeddings = state_embedding.expand(len(legal_actions), -1)
        action_embeddings = self.action_encoder(action_features)
        joint = torch.cat(
            [
                state_embeddings,
                action_embeddings,
                state_embeddings * action_embeddings,
                torch.abs(state_embeddings - action_embeddings),
            ],
            dim=-1,
        )
        return self.phase_heads[self.normalized_phase(phase)](joint).squeeze(-1)

    @torch.no_grad()
    def choose_legal_action(
        self,
        visible_state: dict[str, Any],
        phase: str,
        legal_actions: Sequence[dict[str, Any]],
    ) -> tuple[dict[str, Any], Tensor]:
        self.eval()
        scores = self.score_legal_actions(visible_state, phase, legal_actions)
        index = int(torch.argmax(scores).item())
        return legal_actions[index], scores.cpu()


def train_behavior_clone(
    examples: Sequence[TraceExample],
    config: BCConfig,
    *,
    device: str = "cpu",
) -> tuple[PhaseActionScoringNetwork, list[float]]:
    """Train a small offline BC policy. This function never interacts with the game."""

    if not examples:
        raise ValueError("examples must be non-empty")
    random.seed(config.random_seed)
    torch.manual_seed(config.random_seed)
    model = PhaseActionScoringNetwork(config).to(device)
    optimizer = torch.optim.AdamW(
        model.parameters(), lr=config.learning_rate, weight_decay=config.weight_decay
    )
    order = list(range(len(examples)))
    losses: list[float] = []
    for epoch in range(config.epochs):
        random.Random(config.random_seed + epoch).shuffle(order)
        total_loss = 0.0
        model.train()
        for index in order:
            example = examples[index]
            optimizer.zero_grad(set_to_none=True)
            scores = model.score_legal_actions(
                example.visible_state, example.phase, example.legal_actions
            )
            target = torch.tensor([example.chosen_index], device=scores.device)
            loss = F.cross_entropy(scores.unsqueeze(0), target)
            loss.backward()
            optimizer.step()
            total_loss += float(loss.detach().cpu())
        losses.append(total_loss / len(examples))
    return model, losses


def save_checkpoint(
    model: PhaseActionScoringNetwork,
    output: Path,
    *,
    trace_paths: Sequence[Path],
    example_count: int,
    phase_counts: dict[str, int],
    losses: Sequence[float],
    expected_build: str,
) -> dict[str, Any]:
    output.parent.mkdir(parents=True, exist_ok=True)
    file_entries = [
        {
            "path": str(path.resolve()),
            "bytes": path.stat().st_size,
            "sha256": sha256_file(path),
        }
        for path in trace_paths
    ]
    metadata: dict[str, Any] = {
        "checkpoint_version": BC_CHECKPOINT_VERSION,
        "created_at": datetime.now(UTC).isoformat(),
        "model": "shared_visible_state_encoder_phase_action_scorers",
        "model_config": asdict(model.config),
        "target": {
            "build": expected_build,
            "character": DEFAULT_CHARACTER,
            "ascension": DEFAULT_ASCENSION,
            "save_load_used": False,
        },
        "training": {
            "random_seed": model.config.random_seed,
            "examples": example_count,
            "phase_counts": phase_counts,
            "epoch_losses": list(losses),
            "device": str(next(model.parameters()).device),
            "torch_version": torch.__version__,
        },
        "dataset": {
            "aggregate_sha256": dataset_digest(trace_paths),
            "files": file_entries,
        },
    }
    torch.save({"metadata": metadata, "state_dict": model.state_dict()}, output)
    checkpoint_sha256 = sha256_file(output)
    sidecar = dict(metadata)
    sidecar["checkpoint"] = {
        "path": str(output.resolve()),
        "bytes": output.stat().st_size,
        "sha256": checkpoint_sha256,
    }
    metadata_path = output.with_suffix(output.suffix + ".metadata.json")
    metadata_path.write_text(
        json.dumps(sidecar, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return sidecar


def load_checkpoint(path: Path, *, verify_hash: bool = True) -> PhaseActionScoringNetwork:
    if verify_hash:
        metadata_path = path.with_suffix(path.suffix + ".metadata.json")
        sidecar = json.loads(metadata_path.read_text(encoding="utf-8"))
        actual = sha256_file(path)
        if actual != sidecar["checkpoint"]["sha256"]:
            raise ValueError("checkpoint SHA-256 does not match metadata sidecar")
    payload = torch.load(path, map_location="cpu", weights_only=False)
    raw_config = payload["metadata"]["model_config"]
    raw_config["phases"] = tuple(raw_config["phases"])
    model = PhaseActionScoringNetwork(BCConfig(**raw_config))
    model.load_state_dict(payload["state_dict"])
    model.eval()
    return model


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Offline behavior cloning from validated STS2 decision traces"
    )
    parser.add_argument("--trace", action="append", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--expected-build", default=CURRENT_PUBLIC_BETA_BUILD)
    parser.add_argument("--epochs", type=int, default=5)
    parser.add_argument("--feature-dim", type=int, default=256)
    parser.add_argument("--hidden-dim", type=int, default=128)
    parser.add_argument("--seed", type=int, default=20260831)
    parser.add_argument("--device", default="cpu", choices=("cpu", "cuda"))
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    config = BCConfig(
        feature_dim=args.feature_dim,
        hidden_dim=args.hidden_dim,
        epochs=args.epochs,
        random_seed=args.seed,
    )
    examples = load_trace_examples(
        args.trace, split="train", expected_build=args.expected_build
    )
    model, losses = train_behavior_clone(examples, config, device=args.device)
    phase_counts = dict(sorted(Counter(example.phase for example in examples).items()))
    metadata = save_checkpoint(
        model,
        args.output,
        trace_paths=args.trace,
        example_count=len(examples),
        phase_counts=phase_counts,
        losses=losses,
        expected_build=args.expected_build,
    )
    print(json.dumps(metadata, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

