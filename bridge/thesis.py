"""The run "thesis": the model's persisted memory across a full run.

LOCKED = append-only durable facts (character, win condition, key relics/
blessings, hard constraints). PLAN = volatile current strategy read.

The model re-emits both between markers each turn; we parse, merge (LOCKED is
append-only, PLAN is replaced), persist to JSON, and feed it back next turn.
"""
from __future__ import annotations

import json
import re

from . import util

_LOCKED_RE = re.compile(r"<<<LOCKED\s*(.*?)\s*>>>", re.DOTALL)
_PLAN_RE = re.compile(r"<<<PLAN\s*(.*?)\s*>>>", re.DOTALL)


def parse_response(text: str) -> tuple[str, list[str], str]:
    """Split a model response into (advice, locked_lines, plan).

    advice = everything before the first marker (what the overlay shows).
    """
    locked_lines: list[str] = []
    m = _LOCKED_RE.search(text)
    if m:
        for line in m.group(1).splitlines():
            line = line.strip().lstrip("-").strip()
            if line:
                locked_lines.append(line)
    plan = ""
    mp = _PLAN_RE.search(text)
    if mp:
        plan = mp.group(1).strip()

    # advice is the text before whichever marker comes first
    cut = len(text)
    for marker in ("<<<LOCKED", "<<<PLAN"):
        idx = text.find(marker)
        if idx != -1:
            cut = min(cut, idx)
    advice = text[:cut].strip()
    # Safety net: if the model rambled before PICK:, show only from PICK: onward
    # so the overlay stays terse even when the contract is bent.
    pick_idx = advice.find("PICK:")
    if pick_idx > 0:
        advice = advice[pick_idx:].strip()
    advice = _strip_internal_coords(advice)
    return advice, locked_lines, plan


def _strip_internal_coords(text: str) -> str:
    """Remove internal map coordinates the model may leak — the player only sees
    left/middle/right forks, not column/row numbers. Covers "col 2", "(col 2)",
    "row 6", and the shorthand "r6"/"c4"/"r6c4", plus a preceding "at"/"@"."""
    # a coordinate token: "col 2" / "row 6" / "r6" / "c4" / "r6c4"
    tok = r"(?:(?:col(?:umn)?|row)\s*\.?\s*\d+|(?:[rc]\d+){1,3})"
    # optional leading "at "/"@", optional bracket, the token, optional close bracket
    pattern = rf"\s*(?:at\s+|@\s*)?[\(\[,]?\s*{tok}\s*[\)\]]?"
    text = re.sub(pattern, " ", text, flags=re.IGNORECASE)
    text = re.sub(r"[ \t]{2,}", " ", text)         # collapse doubled spaces
    text = re.sub(r"\s+([,.;:])", r"\1", text)      # no space before punctuation
    text = re.sub(r"[(\[]\s*[)\]]", "", text)       # drop empty brackets
    text = re.sub(r"(?:->|→)", "→", text)            # unify arrows
    text = re.sub(r"(?:\s*→\s*)+", " → ", text)      # collapse runs, single-spaced
    return text.strip()


class ThesisStore:
    def __init__(self, path: str):
        self.path = util.resolve(path)
        self.locked: list[str] = []
        self.plan: str = ""
        self.run_key: str | None = None
        self._load()

    def _load(self) -> None:
        if self.path.exists():
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
                self.locked = data.get("locked", [])
                self.plan = data.get("plan", "")
                self.run_key = data.get("run_key")
            except (ValueError, OSError):
                pass

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps({
            "run_key": self.run_key,
            "locked": self.locked,
            "plan": self.plan,
        }, indent=2), encoding="utf-8")

    def reset(self, run_key: str | None = None) -> None:
        self.locked = []
        self.plan = ""
        self.run_key = run_key
        self.save()

    def update(self, new_locked: list[str], new_plan: str) -> None:
        """Append-only merge for LOCKED (dedup, preserve order); replace PLAN."""
        seen = {l.lower() for l in self.locked}
        for line in new_locked:
            if line.lower() not in seen:
                self.locked.append(line)
                seen.add(line.lower())
        if new_plan:
            self.plan = new_plan

    def format_for_prompt(self) -> str:
        if not self.locked and not self.plan:
            return "(empty — this is the first decision of the run)"
        locked = "\n".join(f"- {l}" for l in self.locked) or "(none yet)"
        plan = self.plan or "(none yet)"
        return f"LOCKED:\n{locked}\n\nPLAN:\n{plan}"
