"""Stable per-screen signature + debounce so the model fires once per decision.

The signature is derived from the decision-relevant content (screen kind, act/
floor, and the set of options on screen). It is stable across animation frames
but changes when the actual choice changes. The Debouncer requires the signature
to hold steady for `settle_s` before it's considered settled, and tracks the
last fired signature so we advise exactly once per distinct decision.
"""
from __future__ import annotations

import hashlib
import time

from . import screens


def build(state: dict, kind: str) -> str:
    run = state.get("run") or {}
    parts = [
        kind,
        str(run.get("act")),
        str(run.get("floor")),
    ]
    for opt in screens.extract_options(state, kind):
        # id/name + index + upgrade flag captures the choice identity.
        parts.append(f"{opt.get('otype')}:{opt.get('id')}:{opt.get('index')}:"
                     f"{int(bool(opt.get('is_upgraded')))}:{opt.get('price', '')}")
    digest = hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()
    return f"{kind}-{run.get('floor')}-{digest[:12]}"


class Debouncer:
    def __init__(self, settle_s: float = 1.5):
        self.settle_s = settle_s
        self._current_sig: str | None = None
        self._first_seen: float = 0.0
        self._last_fired: str | None = None

    def observe(self, sig: str, now: float | None = None) -> None:
        """Record the current signature; resets the settle timer on change."""
        now = time.monotonic() if now is None else now
        if sig != self._current_sig:
            self._current_sig = sig
            self._first_seen = now

    def should_fire(self, sig: str, now: float | None = None) -> bool:
        """True once `sig` has been stable for settle_s and hasn't fired yet."""
        now = time.monotonic() if now is None else now
        if sig != self._current_sig:
            self.observe(sig, now)
            return False
        if sig == self._last_fired:
            return False
        return (now - self._first_seen) >= self.settle_s

    def mark_fired(self, sig: str) -> None:
        self._last_fired = sig

    def reset(self) -> None:
        self._current_sig = None
        self._first_seen = 0.0
        self._last_fired = None
