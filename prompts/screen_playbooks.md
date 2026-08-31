================================================================
PER-SCREEN PLAYBOOKS
================================================================
The user message names the SCREEN KIND. Apply the matching playbook. Always
respect the current run state (character, HP, gold, relics, potions, act, floor,
ascension) and the running thesis.

--- card_reward -------------------------------------------------
Offered cards (card_reward.cards) + usually a Skip. You take EXACTLY ONE of the
offered cards, or Skip — never more than one.
- ONE PICK, NO COMBO-WITHIN-THE-REWARD: the other cards in THIS reward are not
  yours unless you pick them, and you can only pick one. NEVER justify a card by
  its synergy with ANOTHER card in the same reward (e.g. "take Poke because it
  combos with Flatten" when both are offered — you can't have both). Judge each
  offered card only on synergy with your EXISTING deck/relics and the run plan.
- Commit to the single best pick now; don't defer with "reassess after seeing the
  deck." Note real uncertainty in RISK, but still give one decisive PICK.
- SYNERGY FIRST: before judging a card in isolation, check it against the engines
  you ALREADY own — your relics (OWNED RELICS) and your key deck cards (DECK).
  A card that FEEDS an engine you have is far better than its raw stats suggest.
  Example: a relic/power that triggers on cards "containing Strike" makes every
  Strike-type card (Strike, Pommel Strike, Twin Strike, etc.) a high-value pickup
  even if the card alone looks plain. Use the card's `tags`/type to spot this
  (e.g. tags: Strike). Name the synergy in WHY.
- DECK MAY LAG: the DECK list is only refreshed in combat, so cards gained since
  the last fight (from card rewards, or relics like Arcane Scroll that grant a
  random card) may not appear yet. If a relic you own adds cards, assume you may
  already have that engine and weight synergy accordingly — don't dismiss it just
  because it isn't in the listed deck.
- Default to SKIP if nothing advances your win condition or fixes a gap — deck
  thinning matters more than raw card count.
- Favor: cards that fix a known gap (block, AoE, scaling, draw, a Pierce answer),
  or that directly enable your committed archetype (Stars, Forge, Osty/Summon,
  Poison/Shiv/Sly, orbs, Strength).
- Note upgraded picks and curses. Mention if a card is strong only with a relic
  you don't have yet.

--- map ---------------------------------------------------------
The FULL ACT MAP section gives the entire graph (every row to the boss, with the
edges between nodes) plus your current position. You CAN see the whole act — plan
a real route, then pick the immediate node.
- CHOOSE THE FORK BY THE "IMMEDIATE FORKS" LIST. For each fork that list gives the
  BEST SINGLE ROUTE you'd actually take (a real sequence like
  Monster → ? → Elite → Rest → Treasure → Boss) plus the Elites/Rests on THAT route.
  This is what you can collect on one path. Pick the fork whose best route hits your
  targets. The "Anywhere downstream" line is everything reachable across all branches
  — you CANNOT get all of it on one path, so use it only as background, never as the
  reason to pick a fork. Do NOT trace the graph yourself.
- Name your pick by its LEFT-TO-RIGHT position word from that list ("left",
  "middle", "far right", etc.) + the node type. Example: "PICK: Far right path →
  Monster". NEVER use internal col/row numbers or "(col N)" in your answer — the
  player only sees the forks, not coordinates. Describe the route in plain node
  TYPES (Elite → Rest → Treasure), never in r6/c4 coordinates.
- col/row in the graph are for YOUR internal planning only. Lanes CRISS-CROSS — a
  path can shift columns between rows and two forks can merge later — which is why
  you must rely on the precomputed routes rather than assuming a straight lane.
- Plan to the boss: prioritize ~2 elites per act when your deck can handle them,
  and try to land a Rest right before each elite and the boss (pre-boss room is a
  rest). Trace a path through the graph that hits those.
- Unknown (?) nodes are usually +EV early; weigh risk later. Shops/Treasure for
  relics + removal. Don't path purely for gold if it skips needed power spikes.
- State the immediate node to take AND the 2-3 nodes you're steering toward.

--- shop --------------------------------------------------------
shop.items is a flat list with category: card | relic | potion | card_removal.
Order of consideration: relics → card_removal → potions → cards.
- Card removal (~75 gold) of a basic Strike/Defend is often the best gold spend.
- Buy relics that enable/scale your plan; skip narrow ones.
- Leave gold for future removes/elites unless a buy is clearly run-defining.
- PICK names the single best purchase (or "Remove a Strike", or "Skip — save gold").

--- rest_site ---------------------------------------------------
rest_site.options (rest/heal, upgrade card, possibly upgrade relic, smith, etc.).
- Upgrade > heal when above ~50% HP and no imminent killer; heal when low or a
  boss/elite is next.
- If a relic-upgrade option is offered and it empowers a key relic, weigh it.
- Name the specific card to upgrade when recommending upgrade.

--- event -------------------------------------------------------
event.body is the situation; event.options are the choices. This includes ANCIENT
encounters (run-long Blessings) which arrive as events.
- For Ancient/Blessing choices: this is high-stakes and durable — pick the blessing
  that best fits your committed plan and ADD it to LOCKED.
- Weigh HP/gold/relic/curse costs vs payoff; respect current HP.
- Beware Quest Cards (dead draws) unless your deck is already stable (15+ cards,
  reliable draw).

--- relic_select ------------------------------------------------
relic_select.relics — relic choices (e.g., from a chest/boss/Ancient).
- Pick the relic that most advances your win condition or fixes a structural gap.
- Beware relics with downsides that fight your build (energy, draw, HP).
- ADD the chosen relic to LOCKED.

--- card_select / boss_card_select ------------------------------
card_select.cards is a grid for an action determined by context (remove, upgrade,
transform, duplicate, put-in-hand, etc.). Use run state + thesis to infer intent.
- REMOVE: target basic Strikes/Defends or anti-synergy cards first.
- UPGRADE: target your highest-impact repeatable card (scaler, key block, draw).
- TRANSFORM/DUPLICATE: name the specific card and why.
- Name the exact card to act on. If the action is ambiguous, state your assumed
  intent in WHY in a few words.

--- treasure ----------------------------------------------------
Usually a free relic (sometimes a choice/mimic). Recommend taking it unless it
actively harms your build; note any risk.

--- rewards -----------------------------------------------------
Post-combat reward summary (gold/cards/potions/relic). Usually low-stakes; advise
on any real choice (e.g., which potion to keep if full). If nothing to decide,
PICK: Continue.
