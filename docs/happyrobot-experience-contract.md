# HappyRobot side of the learning loop

What the simulator now sends, what the agent should do with it, and the one
optional workflow worth building. Nothing here is published; every change to a
workflow is a forked version until a human promotes it.

## Who owns what, and why

| Simulator (repo) | HappyRobot (workspace) |
|---|---|
| Physics, hidden truth, belief world | Judgement: what matters first, who to tell |
| Possible-world ensemble (`possible_worlds`) | Reading the futures, naming the risk it acts on |
| Situation signature, k-nearest graded cases (`similar_cases`) | Weighing cases against current evidence, saying which it used or rejected |
| Hindsight oracle, regret, lesson credit, retirement | Reflection (post-mortem workflow), proposed rules and prompt patches |
| Episode harness, learning curve, dashboard | Communication with responders/residents/coordinator, tool telemetry (the WHY) |

The simulator owns everything that needs ground truth, reproducibility or a
metric. HappyRobot owns everything that needs judgement, language or a phone.
No new workflow is needed to *simulate* the possible worlds: branches carry the
plan in force forward under the physics; nobody decides inside a branch.

## New payload fields (Los Panaderos / Despacho Central trigger)

Both are inside `world_state` (JSON string) and optional; old versions ignore them.

```jsonc
"possible_worlds": { "dispersion": 0.08, "expected_burning_cells": 14,
  "districts": { "farm": { "p_fire_within_8": 0.88, "p_blocked_or_burnt": 0.4, "status_now": "unwarned" }, "town_north": { "p_fire_within_8": 0.12, "p_blocked_or_burnt": 0.0, "status_now": "unwarned" } } },
"forecast_divergence": { "tick": 12, "distance": 0.21, "threshold": 0.10, "what_changed": ["wind", "front town_north"] },   // only when reality broke the forecast
"similar_cases": [ {
  "decision_id": 41, "similarity_distance": 0.09,
  "why_similar": { "phase": 0.0, "wind": 0.1, "fire": 0.0, "districts": 0.12, "fleet": 0.0 },
  "tick": 2, "event_type": "farmer_call", "wind": [0, -1],
  "situation": { "farm": { "status": "unwarned", "distance": 20.2, "downwind": 0.99 }, "town_north": { "status": "unwarned", "distance": 40, "downwind": 0.0 } },
  "did": ["drone-1: hold", "scout-1: hold", "engine-1: continue"], "mission": "wait for confirmation",
  "outcome": { "people_changes": { "farm": ["unwarned", "burnt"] }, "newly_burned_cells": 9, "people_burnt": 100 },
  "regret": 100.0, "gap_type": "judgement",
  "oracle_preferred": ["drone-1: evacuate_farm farm"], "root_cause": "waited for thermal confirmation with residents downwind",
  "lesson": "With a credible smoke report and unwarned residents downwind, warn before confirming."
} ],
"lessons_learned": ["With a credible smoke report and unwarned residents downwind, warn before confirming."]
```

Guarantees: nothing from the hidden truth is in any of these; cases never come
from the current incident; at most 3 cases at distance <= 0.25; `did` and
`oracle_preferred` are short strings, never raw orders.

## Prompt section to add (forked version of Los Panaderos, Central agent)

> **Experience.** `similar_cases` are past decisions in situations close to this
> one, graded with hindsight. They are evidence, not orders: current
> observations, the forecast and your telemetry come first. For each case decide
> whether it applies now; if a case with high regret matches and nothing in the
> present contradicts it, do not repeat what it did. If you set aside a case,
> say why in one clause. `lessons_learned` are rules drawn from such cases;
> apply them when their situation matches, ignore them when it does not.
>
> **Futures.** `possible_worlds` gives, per district, the frequency with which
> the fire came within reach across reseeded rollouts of what you currently
> know. Act on the district with the highest `p_fire_within_8` or `p_blocked_or_burnt` and unwarned people
> before containing. If `forecast_divergence` is present, the previous plan
> assumed something that is now false (`what_changed`): revise the affected
> assignments first and name the invalidated assumption.

Structured output additions to the result node (all optional, keep the schema
backward compatible):

```jsonc
"experience_used": { "applied_case_ids": [41], "rejected_case_ids": [], "rejection_reasons": {}, "lessons_applied": [1] },
"forecast_basis": { "district": "farm", "p_fire_within_8": 0.88 },
"invalidated_assumption": "wind stayed northerly"   // when forecast_divergence was present
```

The simulator already stores the whole decision; these fields let the
post-mortem and the Aprendizaje panel show *which* experience changed the
decision, which is the demo claim ("it learns").

## Optional workflow: candidate plans, evaluated by the simulator

Only build this if there is time after one live run with the fields above.

```
Central agent proposes 2–3 concrete plans  ──►  tool evaluate_plans(plans)  ──►  simulator runs worlds.forecast(sim, plans=...)
                                                                                 returns per plan: dispersion, p_fire_within_8 and p_blocked_or_burnt per district, expected burning cells, likely outcomes
Central picks one, explains the choice against the others  ──►  existing Drone/Scout/Truck agents execute and communicate
```

Contract for `evaluate_plans` (HTTP tool node, POST):

```jsonc
// request
{ "incident_id": "...", "plans": { "warn_farm": {"extinguisher_orders": [...], "scout_orders": [...], "truck_orders": [...]},
                                    "contain_east": {...}, "scout_north": {...} } }
// response (one entry per plan plus "current_orders")
{ "warn_farm": { "dispersion": 0.07, "expected_burning_cells": 15, "districts": { "farm": { "p_fire_within_8": 0.9, "p_blocked_or_burnt": 0.0 } } }, ... }
```

Plans must be orders the simulator can execute (same schema as the result
node), never prose hypotheses like "wind shifts east": the physics can evaluate
a plan, it cannot evaluate a sentence. Each plan set costs about one second.

## Shared experience store

The black box (SQLite, `.runtime/blackbox.sqlite`) is the replay buffer and is
per machine. A HappyRobot Twin table (`cases`: signature JSON, did,
oracle_preferred, regret, gap_type, lesson) plus a DB tool node would let agents
in the workspace query experience directly; on hackspainteam9 the Twin database
currently answers `404 Twin database not available`, so this stays a follow-up
until it is enabled. The retrieval logic (`experience.retrieve`) is
store-agnostic: it only needs rows with a signature and an evaluation.
