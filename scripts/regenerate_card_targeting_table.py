"""Regenerate advisor_core/card_targeting_v2.py from the emulator sources.

Classification rules (kept deliberately conservative):

* A card is *single-target* when its native effect routes damage or a debuff
  through a single-enemy helper:
    - generic path: ``Type: Attack`` with printed damage > 0 (the
      ``ApplyBaseDamageAndBlock`` path calls ``DealDamage`` -> ``FirstEnemy``,
      which honours ``TargetEnemyIndex``);
    - explicit ``case IC.<Name>:`` blocks in ``CardEffects.cs`` that call
      ``FirstEnemy`` / ``ApplyEnemyDebuffToTarget`` / ``TemporaryStrength`` on
      an enemy.
* A card is *excluded* when its explicit case only hits every enemy or random
  enemies (``DealDamageToAll*`` / ``DealUnpoweredDamageToAll`` /
  ``...RandomEnemies...``): an explicit target would be ignored by the engine,
  so exposing per-enemy aliases would create duplicate transitions.
* Unplayable cards (curses) never enter the table.

Usage:

    python scripts/regenerate_card_targeting_table.py \
        [--emulator-root ../third_party/slay-the-spire-2-emulator-main] \
        [--check]

``--check`` exits non-zero when the committed module disagrees with the
currently checked-out emulator, which is how tests detect emulator drift.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

CARD_DEF = re.compile(
    r"new CardDef\(Id: (?P<id>-?\d+), Name: \"(?P<name>[^\"]+)\", Cost: (?P<cost>-?\d+), "
    r"BaseDamage: (?P<bd>-?\d+), BaseBlock: (?P<bb>-?\d+), UpgradeDamage: (?P<ud>-?\d+), "
    r"UpgradeBlock: (?P<ub>-?\d+), Type: CardType\.(?P<type>\w+)"
    r"(?:, Rarity: CardRarity\.(?P<rarity>\w+))?(?P<flags>[^\n]*)\),?"
)
IC_CONST = re.compile(r"public const int (?P<name>\w+) = (?P<id>-?\d+);")
CASE_START = re.compile(r'(?m)^[ \t]{4,}case (?:"(?P<string>\w+)"|(?:IC|CL)\.(?P<name>\w+)):')
SINGLE_HELPERS = re.compile(
    r"FirstEnemy|ApplyEnemyDebuffToTarget|ApplyTemporaryStrengthDownToEnemy"
)
AOE_HELPERS = re.compile(
    r"DealDamageToAll|DealUnpoweredDamageToAll|RandomEnem|ToRandomEnemy"
)


def parse_card_defs(cards_source: str) -> dict[int, dict[str, object]]:
    defs: dict[int, dict[str, object]] = {}
    for match in CARD_DEF.finditer(cards_source):
        card_id = int(match["id"])
        defs[card_id] = {
            "name": match["name"],
            "cost": int(match["cost"]),
            "type": match["type"],
            "damage": int(match["bd"]) + int(match["ud"]),
            "unplayable": "Unplayable: true" in match["flags"],
        }
    return defs


def classify_effect_cases(
    effects_source: str, name_to_id: dict[str, int]
) -> tuple[set[int], set[int]]:
    """Return (explicit single-target ids, explicit AoE/random ids) from cases."""

    constants = {m["name"]: int(m["id"]) for m in IC_CONST.finditer(effects_source)}
    lookup = {**name_to_id, **constants}
    starts = list(CASE_START.finditer(effects_source))
    single: set[int] = set()
    aoe: set[int] = set()
    for index, match in enumerate(starts):
        name = match["name"] or match["string"]
        card_id = lookup.get(name)
        if card_id is None:
            continue
        chunk_end = (
            starts[index + 1].start() if index + 1 < len(starts) else len(effects_source)
        )
        chunk = effects_source[match.end() : chunk_end]
        if AOE_HELPERS.search(chunk):
            aoe.add(card_id)
        elif SINGLE_HELPERS.search(chunk):
            single.add(card_id)
    return single, aoe


VOCAB_DEF = re.compile(r"new (?:Card|Relic|Potion|Enemy)Def\(Id: (?P<id>-?\d+)")
EXTRA_RELIC_CONST = re.compile(r"public const int Relic\w+ = (?P<id>-?\d+);")
BUFF_ENUM = re.compile(r"public enum BuffId\s*\{(?P<body>.*?)\n\}", re.DOTALL)


def compute_vocabularies(emulator_root: Path) -> dict[str, list[int]]:
    """Dense, sorted id vocabularies for observation multi-hot blocks."""

    generated = emulator_root / "src/Sts2Emulator/Generated"
    vocab: dict[str, list[int]] = {}
    for key, filename in (
        ("CARDS", "Cards.g.cs"),
        ("RELICS", "Relics.g.cs"),
        ("POTIONS", "Potions.g.cs"),
        ("ENEMIES", "Enemies.g.cs"),
    ):
        source = (generated / filename).read_text(encoding="utf-8")
        vocab[key] = sorted({int(m["id"]) for m in VOCAB_DEF.finditer(source)})
    # Event-granted relics exist above the generated id range (RunConstants).
    run_constants = (
        emulator_root / "src/Sts2Emulator/Core/Run/RunConstants.cs"
    ).read_text(encoding="utf-8")
    extras = {int(m["id"]) for m in EXTRA_RELIC_CONST.finditer(run_constants)}
    vocab["RELICS"] = sorted(set(vocab["RELICS"]) | extras)
    # Curse/token placeholders must exist even if absent from generated defs.
    vocab["CARDS"] = sorted(
        set(vocab["CARDS"])
        | {
            int(m["id"])
            for m in re.finditer(
                r"public const int \w+Card = (?P<id>\d+);", run_constants
            )
        }
    )
    return vocab


def compute_buff_count(emulator_root: Path) -> int:
    """Number of BuffId enum members (implicit positional ordinals).

    The combat observation reports buffs as ``(id, magnitude)`` pairs, so the
    expanded observation needs one fixed slot per BuffId member.
    """

    source = (emulator_root / "src/Sts2Emulator/Core/BuffState.cs").read_text(
        encoding="utf-8"
    )
    body = BUFF_ENUM.search(source)
    if body is None:
        raise ValueError("could not locate the BuffId enum to derive its size")
    names = [
        line.split("//", 1)[0].strip().rstrip(",")
        for line in body.group("body").splitlines()
    ]
    members = [name for name in names if name and re.fullmatch(r"\w+", name)]
    if not members:
        raise ValueError("BuffId enum parse produced no members")
    return len(members)


def compute_tables(emulator_root: Path) -> tuple[list[int], list[int]]:
    cards_path = emulator_root / "src/Sts2Emulator/Generated/Cards.g.cs"
    effects_path = emulator_root / "src/Sts2Emulator/Core/Effects/CardEffects.cs"
    defs = parse_card_defs(cards_path.read_text(encoding="utf-8"))
    name_to_id = {str(spec["name"]): card_id for card_id, spec in defs.items()}
    explicit_single, explicit_aoe = classify_effect_cases(
        effects_path.read_text(encoding="utf-8"), name_to_id
    )
    unplayable = {card_id for card_id, spec in defs.items() if spec["unplayable"]}

    # Generic ApplyBaseDamageAndBlock -> DealDamage -> FirstEnemy path.
    generic_damage = {
        card_id
        for card_id, spec in defs.items()
        if spec["type"] == "Attack" and int(spec["damage"]) > 0
    }
    single = (generic_damage | explicit_single) - explicit_aoe - unplayable
    return sorted(single), sorted(explicit_aoe)


TEMPLATE = '''"""Static emulator-derived V2 data: targeting + observation vocabularies.

GENERATED FILE -- do not edit by hand.
Regenerate with:  python scripts/regenerate_card_targeting_table.py

Two facts drive this file:

* ``is_single_target`` decides which hand slots get per-enemy action aliases.
  The native combat engine resolves single-enemy effects through
  ``CardEffects.FirstEnemy``, which honours ``targetEnemyIndex``.  A card id is
  listed when its damage/debuff routes through that path, so an explicit
  target genuinely changes the transition.  Engine-level AoE and random-enemy
  cases are excluded: per-enemy aliases there only duplicate identical
  transitions, violating the codec's alias contract.
* the ``*_VOCAB`` tables give a stable dense index for every emulator object
  id so the expanded observation is a fixed-width multi-hot vector whose
  meaning never shifts between emulator builds of the same version.

Keep this the single source of truth for search, behavior cloning, training
action spaces, and the live advisor so their vocabularies never diverge.  The
``--check`` mode of the generator detects emulator drift against these tables.
"""

from __future__ import annotations

import bisect

#: Emulator card def ids whose combat effect honours an explicit enemy target.
SINGLE_TARGET_CARD_IDS: frozenset[int] = frozenset(
    {single_ids}
)

#: Emulator card def ids handled by the engine as all-enemy or random-enemy.
EXCLUDED_AOE_CARD_IDS: frozenset[int] = frozenset(
    {aoe_ids}
)

#: Sorted emulator ids used to size the expanded-observation multi-hot blocks.
CARD_VOCAB: tuple[int, ...] = {card_vocab}
RELIC_VOCAB: tuple[int, ...] = {relic_vocab}
POTION_VOCAB: tuple[int, ...] = {potion_vocab}
ENEMY_VOCAB: tuple[int, ...] = {enemy_vocab}

#: Number of ``BuffId`` enum members; combat buffs are one slot each by ordinal.
BUFF_COUNT: int = {buff_count}

assert not (SINGLE_TARGET_CARD_IDS & EXCLUDED_AOE_CARD_IDS), (
    "single-target and AoE card tables must not overlap"
)


def is_single_target(card_def_id: int) -> bool:
    """Whether playing this card def honours an explicit enemy target."""

    return int(card_def_id) in SINGLE_TARGET_CARD_IDS


def dense_index(vocab: tuple[int, ...], object_id: int) -> int:
    """Return the multi-hot slot for ``object_id``, or -1 when unknown.

    Unknown ids are the expected case when the emulator's generated data gains
    a card/relic/potion after this file was produced; callers must treat -1 as
    "not representable in this build" rather than silently colliding an index.
    """

    position = bisect.bisect_left(vocab, int(object_id))
    if position < len(vocab) and vocab[position] == int(object_id):
        return position
    return -1


__all__ = [
    "BUFF_COUNT",
    "CARD_VOCAB",
    "ENEMY_VOCAB",
    "EXCLUDED_AOE_CARD_IDS",
    "POTION_VOCAB",
    "RELIC_VOCAB",
    "SINGLE_TARGET_CARD_IDS",
    "dense_index",
    "is_single_target",
]
'''


def _format_set(ids: list[int]) -> str:
    if not ids:
        return "frozenset()"
    lines = []
    row = "       " + " ".join(f"{value}," for value in ids)
    # wrap into readable lines of <= 96 columns
    current = "       "
    for value in ids:
        token = f"{value},"
        if len(current) + len(token) + 1 > 96:
            lines.append(current.rstrip())
            current = "       "
        current += token + " "
    lines.append(current.rstrip())
    return "(\n" + "\n".join(lines) + "\n    )"


def _format_tuple(ids: list[int]) -> str:
    # _format_set emits "(\n  1, 2, 3,\n)" which is a valid tuple literal too.
    return _format_set(ids) if ids else "()"


def render_module(
    single: list[int],
    aoe: list[int],
    vocabularies: dict[str, list[int]],
    buff_count: int,
) -> str:
    return TEMPLATE.format(
        single_ids=_format_set(single),
        aoe_ids=_format_set(sorted(set(aoe))),
        card_vocab=_format_tuple(vocabularies["CARDS"]),
        relic_vocab=_format_tuple(vocabularies["RELICS"]),
        potion_vocab=_format_tuple(vocabularies["POTIONS"]),
        enemy_vocab=_format_tuple(vocabularies["ENEMIES"]),
        buff_count=repr(int(buff_count)),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--emulator-root",
        type=Path,
        default=Path(__file__).resolve().parents[2]
        / "third_party/slay-the-spire-2-emulator-main",
    )
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)

    single, aoe = compute_tables(args.emulator_root)
    vocabularies = compute_vocabularies(args.emulator_root)
    rendered = render_module(single, aoe, vocabularies)
    target = Path(__file__).resolve().parents[1] / "advisor_core/card_targeting_v2.py"
    if args.check:
        current = target.read_text(encoding="utf-8") if target.is_file() else ""
        if current != rendered:
            print(f"{target} is stale for emulator at {args.emulator_root}", file=sys.stderr)
            return 1
        return 0
    target.write_text(rendered, encoding="utf-8", newline="\n")
    print(
        "wrote {target}: {n_single} single-target, {n_aoe} excluded AoE; "
        "vocab cards={cards} relics={relics} potions={potions} enemies={enemies}".format(
            target=target,
            n_single=len(single),
            n_aoe=len(aoe),
            cards=len(vocabularies["CARDS"]),
            relics=len(vocabularies["RELICS"]),
            potions=len(vocabularies["POTIONS"]),
            enemies=len(vocabularies["ENEMIES"]),
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
