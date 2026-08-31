"""Reference grounding + prompt assembly.

Loads the local StS2 card/relic dataset (spire-codex schema) and joins it to the
options on the current screen by a normalized id key, so we can inject the EXACT
base + upgraded text into the prompt as authoritative reference.
"""
from __future__ import annotations

import json
import re
import string

from . import screens, util

_STRIP_PREFIXES = ("card_", "relic_", "power_", "potion_")
_PUNCT = str.maketrans("", "", string.punctuation)


def normalize_id(raw: str | None) -> str:
    """Canonicalize an id/name for joining: lowercase, strip prefixes, drop the
    upgrade suffix and all separators/punctuation.

    Examples: 'STRIKE_R' -> 'striker'; 'card_strike_r' -> 'striker';
              'Bash+' -> 'bash'; 'FIELD_NOTES_CARD' -> 'fieldnotescard'.
    """
    if not raw:
        return ""
    s = str(raw).strip().lower()
    # strip a "mod:" namespace if present
    if ":" in s:
        s = s.split(":", 1)[1]
    for pre in _STRIP_PREFIXES:
        if s.startswith(pre):
            s = s[len(pre):]
    # strip trailing upgrade markers: +, +1, plus
    s = re.sub(r"(\+\d*|plus)$", "", s)
    s = s.translate(_PUNCT)            # remove punctuation incl. '+' '-'
    s = re.sub(r"[\s_]+", "", s)        # remove spaces/underscores
    return s


def _strip_bbcode(text: str) -> str:
    """Remove spire-codex color tags like [gold]...[/gold] for plain display."""
    if not text:
        return ""
    return re.sub(r"\[/?[a-zA-Z]+\]", "", text)


class Dataset:
    """Indexed card + relic reference data, keyed by normalized id."""

    def __init__(self) -> None:
        self.cards: dict[str, dict] = {}
        self.relics: dict[str, dict] = {}
        self.version: str = "unknown"
        self.fetched: str = "unknown"

    @classmethod
    def load(cls, data_dir: str) -> "Dataset":
        ds = cls()
        base = util.resolve(data_dir)
        ds.cards = ds._index(base / "cards.json")
        ds.relics = ds._index(base / "relics.json")
        meta = base / "version.json"
        if meta.exists():
            try:
                m = json.loads(meta.read_text(encoding="utf-8"))
                ds.version = m.get("game_version", "unknown")
                ds.fetched = (m.get("fetched_utc") or "unknown")[:10]
            except (ValueError, OSError):
                pass
        return ds

    @staticmethod
    def _index(path) -> dict[str, dict]:
        if not path.exists():
            return {}
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        # Accept either a list of entries or a {id: entry} mapping.
        entries = data.values() if isinstance(data, dict) else data
        index: dict[str, dict] = {}
        # Index by id first (authoritative), then add name keys as a fallback so
        # display-name lookups (e.g. "Strike" -> STRIKE_R) also resolve. id keys
        # win; the first card for an ambiguous name (Strike/Defend are identical
        # across characters) fills the name key.
        entries = list(entries)
        for e in entries:
            k = normalize_id(e.get("id"))
            if k:
                index[k] = e
        for e in entries:
            k = normalize_id(e.get("name"))
            if k:
                index.setdefault(k, e)
        return index

    def lookup_card(self, opt: dict) -> dict | None:
        return self._lookup(self.cards, opt)

    def lookup_relic(self, opt: dict) -> dict | None:
        return self._lookup(self.relics, opt)

    @staticmethod
    def _lookup(index: dict, opt: dict) -> dict | None:
        # If an id is present, trust it ONLY: a present-but-unmatched id means a
        # new/unknown entity, so we must NOT fall back to name (that would resolve a
        # brand-new card to a same-named old one and mislead the model). Fall back to
        # name only when there's no id at all (e.g. cards read from combat piles).
        if opt.get("id"):
            return index.get(normalize_id(opt.get("id")))
        return index.get(normalize_id(opt.get("name")))


def _reference_line(opt: dict, ds: Dataset) -> str:
    """One authoritative reference entry for a single option."""
    otype = opt.get("otype")
    name = opt.get("label") or opt.get("id") or "?"

    if otype in ("relic",):
        entry = ds.lookup_relic(opt)
        if entry:
            desc = _strip_bbcode(entry.get("description", ""))
            rarity = entry.get("rarity", "")
            return f"- RELIC {name} [{rarity}]: {desc}".rstrip()
        instate = opt.get("description", "")
        return (f"- RELIC {name}: {instate} "
                f"(not in local dataset — reason from this text; do NOT assume weak)")

    if otype == "card" or otype in ("Attack", "Skill", "Power", "card"):
        entry = ds.lookup_card(opt)
        if entry:
            base = _strip_bbcode(entry.get("description", ""))
            up = _strip_bbcode(entry.get("upgrade_description", ""))
            cost = entry.get("cost", opt.get("cost", "?"))
            ctype = entry.get("type", opt.get("ctype", ""))
            rarity = entry.get("rarity", opt.get("rarity", ""))
            tags = entry.get("tags") or []
            tagstr = f", tags: {', '.join(tags)}" if tags else ""
            colorless = str(entry.get("color", "")).lower() == "colorless"
            clstr = ", COLORLESS" if colorless else ""
            head = f"- CARD {name} [{ctype} {rarity}, cost {cost}{clstr}{tagstr}]"
            if opt.get("is_upgraded") and up:
                return f"{head} (UPGRADED): {up}"
            line = f"{head}: {base}"
            if up:
                line += f"  | upgraded: {up}"
            return line
        instate = opt.get("description", "")
        return (f"- CARD {name}: {instate} "
                f"(not in local dataset — reason from this text; do NOT assume weak)")

    # event/node/rest options: pass through the in-state text, plus the rich
    # fields the API provides (locked state + any relic the option grants).
    label = opt.get("label", name)
    desc = opt.get("description", "")
    lock = "  [LOCKED — cannot be chosen; do NOT recommend]" if opt.get("is_locked") else ""
    line = f"- OPTION {label}: {desc}{lock}".rstrip()
    relic_name = opt.get("relic_name")
    if relic_name:
        relic_entry = ds.lookup_relic({"id": relic_name, "name": relic_name})
        rtext = (_strip_bbcode(relic_entry.get("description", "")) if relic_entry
                 else opt.get("relic_description", ""))
        line += f"\n    grants relic {relic_name}: {rtext}".rstrip()
    return line


def build_reference_text(state: dict, kind: str, ds: Dataset) -> str:
    opts = screens.extract_options(state, kind)
    if not opts:
        return "(no options parsed from this screen)"
    lines = [_reference_line(o, ds) for o in opts]
    return "\n".join(lines)


def _run_summary(state: dict, ds: Dataset) -> str:
    # Live API: character/hp/gold/relics/potions live under `player`;
    # `run` carries only act/floor/ascension.
    run = state.get("run") or {}
    player = state.get("player") or {}
    character = (player.get("character") or run.get("character") or "?").replace("The ", "")
    hp = player.get("hp", run.get("hp", "?"))
    max_hp = player.get("max_hp", run.get("max_hp", "?"))
    gold = player.get("gold", run.get("gold", "?"))
    block = player.get("block")
    status = ", ".join(_status_label(s) for s in player.get("status", [])) or "none"
    potions = ", ".join(p.get("name") for p in player.get("potions", [])
                        if p.get("name") and p.get("name") != "Empty") or "none"
    head = (
        f"Character: {character} | Act {run.get('act', '?')} "
        f"Floor {run.get('floor', '?')} | Ascension {run.get('ascension', 0)}\n"
        f"HP: {hp}/{max_hp}"
        + (f" | Block: {block}" if block else "")
        + f" | Gold: {gold}\n"
        f"Status: {status}\n"
        f"Potions: {potions}\n"
        f"Owned relics (authoritative text):\n{_owned_relics_text(state, ds)}"
    )
    return head


def _status_label(s) -> str:
    if isinstance(s, dict):
        amt = s.get("amount", s.get("counter"))
        name = s.get("name") or s.get("id") or "?"
        return f"{name} {amt}" if amt not in (None, "") else str(name)
    return str(s)


def _owned_relics_text(state: dict, ds: Dataset) -> str:
    relics = screens.player_relics(state)
    if not relics:
        return "  (none)"
    lines = []
    for r in relics:
        entry = ds.lookup_relic(r)
        text = _strip_bbcode(entry.get("description", "")) if entry else r.get("description", "")
        lines.append(f"  - {r.get('label')}: {text}".rstrip())
    return "\n".join(lines)


def _keyword_glossary(state: dict, kind: str) -> str:
    kws = screens.known_keywords(state, kind)
    kws = [k for k in kws if k.get("description")]
    if not kws:
        return ""
    lines = "\n".join(f"- {k['name']}: {_strip_bbcode(k['description'])}" for k in kws)
    return f"\n\n=== KEYWORD GLOSSARY (authoritative) ===\n{lines}"


def _options_block(state: dict, kind: str) -> str:
    opts = screens.extract_options(state, kind)
    if not opts:
        return "(none)"
    lines = []
    for o in opts:
        price = f" — {o['price']} gold" if o.get("price") is not None else ""
        up = " (upgraded)" if o.get("is_upgraded") else ""
        lock = "  [LOCKED]" if o.get("is_locked") else ""
        grant = f"  -> grants {o['relic_name']}" if o.get("relic_name") else ""
        afford = "  [CAN'T AFFORD]" if o.get("can_afford") is False else ""
        sale = "  [ON SALE]" if o.get("on_sale") else ""
        lines.append(f"[{o.get('index')}] {o.get('label')}{up}{price}{sale}{afford}{lock}{grant}")
    extra = ""
    if kind == "event":
        ev = state.get("event") or {}
        if screens.is_ancient_event(state):
            extra += ("\n** ANCIENT ENCOUNTER — this is a RUN-DEFINING Blessing that "
                      "lasts the whole run. Choose the one that best fits your committed "
                      "plan and ADD it to LOCKED. **\n")
        body = ev.get("body", "")
        if body:
            extra += f"\nEVENT TEXT: {body}\n"
    if kind == "card_reward" and (state.get("card_reward") or {}).get("can_skip"):
        lines.append("[skip] Skip the reward")
    return extra + "\n".join(lines)


def _deck_text(deck_cards: list[dict], deck_source: str, ds: Dataset) -> str:
    if not deck_cards:
        return "(deck unknown — no combat observed yet this run; advice is deck-blind)"
    total = sum(c.get("count", 1) for c in deck_cards)
    lines = [f"source: {deck_source} | {total} cards"]
    for c in deck_cards:
        name = c.get("name", "?")
        up = c.get("is_upgraded")
        entry = ds.lookup_card({"name": name, "is_upgraded": up})  # pile cards: name only
        if entry:
            txt = _strip_bbcode(entry.get(
                "upgrade_description" if up and entry.get("upgrade_description") else "description", ""))
        else:
            txt = "(not in dataset)"
        lines.append(f"  {c.get('count', 1)}x {name}{'+' if up else ''}: {txt}")
    return "\n".join(lines)


def _map_text(state: dict) -> str:
    """Render the FULL act map graph so the model can plan a route, not just the
    next step. STS2MCP exposes every node (map.nodes) with children edges."""
    m = state.get("map") or {}
    if not m:
        return ""
    pos = m.get("current_position") or {}
    boss = m.get("boss") or {}
    boss_name = boss.get("name") or boss.get("id") or "?"
    lines = [f"You are at col {pos.get('col')}, row {pos.get('row')} "
             f"({pos.get('type')}). Boss: {boss_name} at row {boss.get('row', '?')}.",
             "Rows increase toward the boss; col is the horizontal lane. Edges show "
             "which cols you can move to next. Plan a full route, then name the "
             "immediate node to take.",
             "Full graph (row: col=Type -> reachable child cols):"]
    by_row: dict = {}
    for n in m.get("nodes") or []:
        by_row.setdefault(n.get("row"), []).append(n)
    for row in sorted(by_row, key=lambda r: (r is None, r)):
        parts = []
        for n in sorted(by_row[row], key=lambda x: (x.get("col") is None, x.get("col"))):
            kids = ",".join(str(c[0]) for c in (n.get("children") or []) if c)
            parts.append(f"c{n.get('col')}={n.get('type')}" + (f"->[{kids}]" if kids else ""))
        lines.append(f"  r{row}: " + "; ".join(parts))
    # Per-fork BEST SINGLE PATH, computed in code. Reachability (below) counts every
    # node reachable downstream, but lanes merge so that OVERSTATES what one route can
    # collect. The best path is the actual sequence you'd take from each fork.
    paths = _best_paths(state)
    if paths:
        lines.append("")
        lines.append("IMMEDIATE FORKS (player sees these left-to-right). For each, the "
                     "BEST SINGLE ROUTE you'd actually take to the boss — choose the "
                     "fork whose route hits your targets, and name it by position:")
        for p in paths:
            seq = " -> ".join(p["path"]) if p["path"] else "(boss)"
            tally = []
            if p["elites"]:
                tally.append(f"{p['elites']} Elite")
            if p["rests"]:
                tally.append(f"{p['rests']} Rest")
            extra = f"  [{', '.join(tally)} on this route]" if tally else ""
            lines.append(f"  - {p['pos']} path: {seq}{extra}")
    reach = _fork_reachability(state)
    if reach:
        lines.append("Anywhere downstream (NOT all reachable on one route — use the "
                     "routes above to decide):")
        for r in reach:
            summ = ", ".join(f"{cnt} {t}" for t, cnt in r["reach"]) or "boss only"
            lines.append(f"  - {r['pos']}: {summ}")
    return "\n".join(lines)


# Order/words used when summarizing what a fork can reach.
_REACH_ORDER = ["Elite", "Treasure", "Shop", "Merchant", "RestSite", "Unknown", "Monster"]
_REACH_LABEL = {"RestSite": "Rest", "Unknown": "?", "Merchant": "Shop"}

# Routing value of each node type for best-path selection (tunable here).
_NODE_WEIGHTS = {
    "Elite": 5.0, "Treasure": 3.0, "Shop": 3.0, "Merchant": 3.0,
    "RestSite": 2.0, "Unknown": 1.5, "Monster": 0.5, "Ancient": 0.0,
}


def _node_value(node_type) -> float:
    return _NODE_WEIGHTS.get(node_type, 0.5)


def _best_paths(state: dict) -> list[dict]:
    """For each immediate fork, find the highest-value single path to the boss
    (DP over the DAG). Returns [{pos, type, path:[type labels...], elites, rests}]."""
    m = state.get("map") or {}
    index = {(n.get("col"), n.get("row")): n for n in (m.get("nodes") or [])}
    best_val: dict = {}
    best_next: dict = {}

    def solve(key):
        if key in best_val:
            return best_val[key]
        node = index.get(key)
        if node is None:
            return 0.0
        kids = [(c[0], c[1]) for c in (node.get("children") or []) if c]
        best_val[key] = 0.0   # mark visited (guards cycles)
        best_next[key] = None
        best = None
        for k in kids:
            cn = index.get(k)
            cval = _node_value(cn.get("type")) if cn else 0.0
            tot = cval + solve(k)
            if best is None or tot > best:
                best, best_next[key] = tot, k
        best_val[key] = best or 0.0
        return best_val[key]

    out = []
    for o in screens.extract_options(state, "map"):
        start = (o.get("col"), o.get("row"))
        solve(start)
        path_types, elites, rests = [], 0, 0
        cur, guard = start, 0
        while cur is not None and guard < 100:
            node = index.get(cur)
            if node is None:
                break
            t = node.get("type")
            path_types.append(_REACH_LABEL.get(t, t))
            elites += t == "Elite"
            rests += t == "RestSite"
            cur = best_next.get(cur)
            guard += 1
        path_types.append("Boss")
        out.append({"pos": o.get("pos_word"), "type": o.get("name"),
                    "path": path_types, "elites": elites, "rests": rests})
    return out


def _fork_reachability(state: dict) -> list[dict]:
    """For each immediate fork, BFS the child-edge graph and count the node types
    reachable downstream. Returns [{pos, type, reach:[(label,count)...]}]."""
    m = state.get("map") or {}
    index = {(n.get("col"), n.get("row")): n for n in (m.get("nodes") or [])}
    out = []
    for o in screens.extract_options(state, "map"):
        start = (o.get("col"), o.get("row"))
        seen: set = set()
        stack = [start]
        counts: dict = {}
        first = True
        while stack:
            key = stack.pop()
            if key in seen:
                continue
            seen.add(key)
            node = index.get(key)
            if node is None:
                continue
            if not first:  # don't count the fork's own node — that's its immediate type
                t = node.get("type")
                label = _REACH_LABEL.get(t, t)
                counts[label] = counts.get(label, 0) + 1
            first = False
            for c in node.get("children") or []:
                if c:
                    stack.append((c[0], c[1]))
        ordered = sorted(counts.items(),
                         key=lambda kv: (_rank(kv[0]), kv[0]))
        out.append({"pos": o.get("pos_word"), "type": o.get("name"), "reach": ordered})
    return out


def _rank(label: str) -> int:
    canon = {"Rest": "RestSite", "?": "Unknown"}.get(label, label)
    return _REACH_ORDER.index(canon) if canon in _REACH_ORDER else len(_REACH_ORDER)


def build_payload(state: dict, kind: str, ds: Dataset, thesis_text: str,
                  deck_cards: list[dict] | None = None, deck_source: str = "") -> str:
    """Assemble the full user message sent to the model for one decision."""
    deck_section = (f"\n\n=== DECK (current; seeded from starter, verified each combat) "
                    f"===\n{_deck_text(deck_cards or [], deck_source, ds)}")
    map_section = ""
    if kind == "map":
        map_section = f"\n\n=== FULL ACT MAP ===\n{_map_text(state)}"
    return (
        f"SCREEN KIND: {kind}\n\n"
        f"=== RUN STATE ===\n{_run_summary(state, ds)}\n\n"
        f"=== OPTIONS ON SCREEN ===\n{_options_block(state, kind)}\n\n"
        f"=== REFERENCE TEXT (authoritative; numbers are base values; "
        f"dataset fetched {ds.fetched}) ===\n"
        f"{build_reference_text(state, kind, ds)}"
        f"{_keyword_glossary(state, kind)}"
        f"{map_section}"
        f"{deck_section}\n\n"
        f"=== RUNNING THESIS (re-emit + update) ===\n{thesis_text}\n\n"
        f"Give your recommendation for this {kind} screen in the exact output "
        f"format. Re-emit LOCKED (append-only) and PLAN."
    )
