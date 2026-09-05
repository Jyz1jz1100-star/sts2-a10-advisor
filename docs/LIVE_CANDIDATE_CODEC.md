# Live candidate codec

`advisor_core/live_candidate_codec.py` is a read-only adapter for one already
observed STS2MCP JSON state. It produces a bounded, variable-length list of
visible candidates and the exact POST body for each candidate. It does not
poll the game, call the bridge, press UI controls, run autoplay, or choose a
strategy.

## Source contract

The shape is pinned to the local STS2MCP v0.4.0 source used with game build
`public-beta-v0.111.0`:

- `McpMod.StateBuilder.cs`: `BuildMapState`, `BuildCardRewardState`,
  `BuildShopState`, `BuildRestSiteState`, and `BuildEventState`.
- `McpMod.Actions.cs`: the corresponding action names and parameter names.
- Upstream reference: [`docs/raw-full.md`](https://github.com/Gennadiyev/STS2MCP/blob/main/docs/raw-full.md).

The source reports Neow as `state_type: "event"`, with an `event` container
whose visible `event_id` is `"NEOW"`. The codec therefore returns
`family="neow"` and preserves `state_type="event"`. An explicit
`state_type: "neow"` input alias is accepted only when the state supplies an
`event` or `neow` container and `event_id` is `NEOW`.

The input may be either the raw state object or the fixture-style envelope
`{"build": ..., "state": {...}}`. If a build is present it must equal the
locked build. If it is absent, the result's `build` field is the target
contract label, not a runtime version check; version-lock/preflight code owns
that check.

## Candidate families and wire bodies

| Family | Visible source container | Legal candidate | Exact wire body |
|---|---|---|---|
| `map` | `map.next_options` | each source map option | `{"action":"choose_map_node","index": INDEX}` |
| `card_reward` | `card_reward.cards` | each source card | `{"action":"select_card_reward","card_index": INDEX}` |
| `card_reward` | `card_reward.can_skip` | skip when `true` | `{"action":"skip_card_reward"}` |
| `shop` | affordable, stocked `shop.items` | each purchasable item | `{"action":"shop_purchase","index": INDEX}` |
| `shop` | `shop.can_proceed` | leave when `true` | `{"action":"proceed"}` |
| `rest_site` | enabled `rest_site.options` | each enabled option | `{"action":"choose_rest_option","index": INDEX}` |
| `rest_site` | `rest_site.can_proceed` | continue when `true` | `{"action":"proceed"}` |
| `event`/`neow` | unlocked, unchosen `event.options` | each visible option | `{"action":"choose_event_option","index": INDEX}` |

The codec preserves each source `index`; it never renumbers a list. Event
options—including a visible Proceed option—use `choose_event_option`, not
`proceed`, as required by the upstream action handler.

## Stable identities and visible features

Every result has `family`, `identity`, `wire_action`, `text`, `features`, and
`comment_zh`. `comment_zh` is always `None` in extracted states. It is a
placeholder for an explicitly supplied human/teacher annotation and is never
filled with an inferred recommendation.

Map identities use the visible node type and coordinates. Event identities use
the visible event id and source option index. Rest-site identities use the
visible option id. Card rewards and shop items include their source slot in
the identity (`...:slot:INDEX`) so two identical visible card offerings remain
distinct and both resolve to their exact wire index. Duplicate source indices
are always ambiguous and rejected.

`features` is an allowlisted projection of the source object. Card, relic and
potion metadata, option flags, prices, map lookahead, and C# hover tips are
retained only in their documented visible fields. `keywords` is projected to
`name`/`description`; map `leads_to` is projected to `col`/`row`/`type`.
Unknown fields, including nested unknown fields, are not copied.

The mappings exposed by a candidate are fresh copies. Internally the validated
JSON trees are recursively frozen, so mutating `candidate.wire_action`,
`candidate.features`, or `to_dict()` output cannot change a previously checked
legal set.

## Fail-closed rules

The codec raises a `LiveCandidateContractError` subclass for missing or
malformed containers, missing source booleans, unsupported screens, duplicate
source indices, duplicate stable identities, unavailable shop inventory,
locked/disabled-only states, dialogue with no actionable event options, and
other states where no legal visible candidate can be proven.

Empty arrays are not automatically errors. The source can legitimately expose
`rest_site.options: []` with `can_proceed: true`, or `shop.items: []` with
`can_proceed: true`; those states produce the real `proceed` candidate. A
card-reward `cards: []` state is accepted only when `can_skip: true`. A map
with no `next_options`, or any empty state with no exposed transition, fails
closed.

Live exception (2026-09-06, verified in-game against 0.29.1): the mod
ForceOpens the merchant inventory when the shop screen is entered, so the live
shop reports `can_proceed: false` while stocked items are listed. When at
least one stocked item exists, the codec exposes the `shop:proceed` candidate
even with `can_proceed: false` — the wire `proceed` handler closes the
inventory and then clicks the re-enabled proceed button
(`ExecuteProceed` in `McpMod.Actions.cs`). A shop with no stocked item and
`can_proceed: false` still fails closed.

## Integration and acceptance boundary (2026-09-05)

The codec is the contract boundary for the live out-of-combat policy. The
current implementation extracts the candidate set before running the heuristic,
maps the recommendation's internal action to a wire action, and verifies that
the primary and alternative recommendations belong to the current candidate
set. The driver then sends the validated wire action. The policy does not yet
score candidate objects directly; the codec itself remains read-only and does
not poll, post, execute, or claim that the chosen candidate is globally
optimal.

The fixture suite covers map, card reward, shop, rest-site, event/Neow,
duplicate identity, source-slot preservation, affordability and malformed
state rejection. This proves the local contract only. No real-game full run has
yet verified every transition, especially reward re-enumeration after a claim,
event semantics, or the end-to-end handoff between Combat Solver and the
out-of-combat owner.
