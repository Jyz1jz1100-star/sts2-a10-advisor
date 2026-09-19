"""Which events get a mask that their own step does not honour.

``probe_shop_refusals.py`` attributes the emulator's mask-vs-step refusals; the event half of
that story is a single ``default:`` branch. ``WriteEventActionMask`` (RunEngine.cs:3459) opens
with ``SetMask(EventSkipAction)`` and then switches on ``State.EventId`` over the events it
knows about, falling through to

    default:
        for (int i = 0; i <= RunConstants.EventSkipAction; i++) SetMask(mask, i);

i.e. an unknown event gets *every* option advertised, unconditionally -- while ``StepEvent``
(RunEngine.cs:2042) switches on the same id and returns -1 for an option whose precondition
fails (Ranwid the Elder's option 0 consumes a held potion and refuses when none is held,
RunEngine.cs:2668-2675).

So the exposure is enumerable from source: the set of ids ``StepEvent`` handles but
``WriteEventActionMask`` does not, plus the ids neither mentions. This script parses both
switches out of the checked-in decompiled source and reports the difference. Static, so it
re-runs in milliseconds and cannot drift from an episode sample.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ENGINE = (ROOT.parent / "third_party" / "slay-the-spire-2-emulator-main"
                  / "src" / "Sts2Emulator" / "Core" / "Run" / "RunEngine.cs")
DEFAULT_CONSTANTS = (ROOT.parent / "third_party" / "slay-the-spire-2-emulator-main"
                     / "src" / "Sts2Emulator" / "Core" / "Run" / "RunConstants.cs")


def switch_on_case_names(source: str, signature: str) -> set[str]:
    """Case labels of the ``switch (State.EventId)`` inside one method.

    Brace-matched from the method's opening brace to its own closing brace, then from the
    switch header to that switch's closing brace, so a nested switch inside some event's
    branch cannot be mistaken for the outer one.
    """
    body_start = source.index("{", source.index(signature))
    depth = 0
    body = None
    for index in range(body_start, len(source)):
        depth += source[index] == "{"
        if source[index] == "}":
            depth -= 1
            if depth == 0:
                body = source[body_start:index]
                break
    if body is None:
        raise ValueError(f"unbalanced braces after {signature!r}")
    switch_start = body.index("{", body.index("switch (State.EventId)"))
    depth = 0
    for index in range(switch_start, len(body)):
        depth += body[index] == "{"
        if body[index] == "}":
            depth -= 1
            if depth == 0:
                return set(re.findall(r"case RunConstants\.(Event\w+):",
                                       body[switch_start:index]))
    raise ValueError(f"unbalanced switch in {signature!r}")


def step_case_bodies(source: str) -> dict[str, str]:
    """Text of each ``case RunConstants.Event*`` arm of ``StepEvent``.

    Only used to count ``return -1`` per event, which separates an event whose options are
    unconditional (over-advertising is harmless) from one with a precondition (the default
    mask arm is then a refusal waiting for a policy that picks that option).
    """
    start = source.index("private int StepEvent")
    body = source[start:start + source[start:].index("\n    private ")]
    arms = re.split(r"case RunConstants\.(Event\w+):", body)
    return {name: text.split("case RunConstants.")[0]
            for name, text in zip(arms[1::2], arms[2::2])}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--engine", type=Path, default=DEFAULT_ENGINE)
    parser.add_argument("--constants", type=Path, default=DEFAULT_CONSTANTS)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()

    engine = args.engine.read_text(encoding="utf-8")
    constants = args.constants.read_text(encoding="utf-8")
    ids = {name: int(value) for name, value in
           re.findall(r"const int (Event\w+) = (-?\d+);", constants)}
    mask_cases = switch_on_case_names(engine, "private void WriteEventActionMask")
    step_cases = switch_on_case_names(engine, "private int StepEvent")

    # EventSkipAction and EventResultPending are state markers sharing the id space, not
    # events an author can place; counting them would inflate every number below.
    sentinels = {"EventSkipAction", "EventResultPending"}
    events = set(ids) - sentinels
    over_advertising = sorted((step_cases - mask_cases) & events,
                              key=lambda n: (ids[n], n))
    neither = sorted(events - step_cases - mask_cases, key=lambda n: (ids[n], n))
    bodies = step_case_bodies(engine)
    refusables = {name: bodies[name].count("return -1") for name in over_advertising
                  if name in bodies}
    # Which option indices an arm actually handles. An observed refusal on a base outside
    # this set is the structural class -- the default mask arm advertises 0..EventSkipAction
    # and the event has fewer options than that -- not a precondition failure.
    handled = {name: sorted({int(v) for v in re.findall(r"action == (\d+)",
                                                         bodies.get(name, ""))})
               for name in over_advertising}
    payload = {
        "_comment": [
            "Events whose StepEvent branch can refuse an option that WriteEventActionMask's",
            "default arm advertises for them. 'neither' lists ids with no branch in either",
            "switch -- for those the step falls through to its own default, so the advertised",
            "options are refused outright, not merely un-preconditioned.",
            "'refusable_option_counts' is a textual count of return -1 in that event's step arm:",
            "0 means the event's options carry no precondition, so advertising all of them is",
            "harmless; a nonzero count is the exposure. This is a static bound, not a measured",
            "refusal rate -- probe_shop_refusals.py supplies the observed one.",
        ],
        "declared_event_count": len(events),
        "events_with_a_refusable_option": sum(1 for v in refusables.values() if v),
        "excluded_state_markers": sorted(sentinels & set(ids)),
        "handled_options_by_step_arm": handled,
        "refusable_option_counts": refusables,
        "source_sha256": {
            "RunConstants.cs": hashlib.sha256(args.constants.read_bytes()).hexdigest(),
            "RunEngine.cs": hashlib.sha256(args.engine.read_bytes()).hexdigest(),
        },
        "events_in_mask_switch": len(mask_cases - sentinels),
        "events_in_step_switch": len(step_cases - sentinels),
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "over_advertising_events": [
            {"name": name, "id": ids.get(name),
             "handled_options": handled.get(name),
             "return_minus_1_in_its_step_arm": refusables.get(name)}
            for name in over_advertising],
        "unhandled_by_either": [{"name": name, "id": ids.get(name)} for name in neither],
    }
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    if args.out:
        args.out.write_text(text + "\n", encoding="utf-8")
    print(f"{len(events)} events declared, {len(sentinels & set(ids))} state markers excluded; "
          f"mask switch handles {len(mask_cases - sentinels)}, "
          f"step switch handles {len(step_cases - sentinels)}")
    print(f"exposed by the default arm: {len(over_advertising)} events, of which "
          f"{payload['events_with_a_refusable_option']} have at least one 'return -1' option")
    print("handled by neither switch:",
          [(e["id"], e["name"]) for e in payload["unhandled_by_either"]])
    return 0


if __name__ == "__main__":
    sys.exit(main())
