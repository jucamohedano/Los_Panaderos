# Adaptation and experience: how to read the system

**Adaptation changes the next plan when new observations disagree with the
forecast. Experience gives HappyRobot relevant historical examples before it
decides.** Neither mechanism trains the model's weights.

This guide describes the current fork implementation. The
[proposal audit](proposal-audit.md) tracks unfinished integrations; the
[evaluation report](learning-evaluation.md) records measured results and limits.

## Start with the Adaptación strip

The strip sits above the maps. Each card answers one question:

| Question | What to look for | What opening it shows |
|---|---|---|
| **¿Qué puede pasar?** / What could happen? | Number of simulated futures and the most threatened district | Forecast assumptions, district threat frequencies and observation checks |
| **¿Hay que cambiar el plan?** / Does the plan need to change? | Not checked, within forecast, replanning requested, or new plan not checked | The changed observations and the forecast they contradicted |
| **¿Qué experiencia tiene?** / What experience is available? | Cases and lessons **sent**, not confirmed use | Similar cases, their matching labels, relevant differences, actions and saved rules |
| **¿Qué resultados obtiene?** / What results does it get? | First-to-latest average cost gap | Result metrics, a labelled chart, incident history and the meaning of regret |

The experience and results cards open different views in the same dialog.
Technical distances, original orders, full incident history and rule controls
are behind disclosures. This keeps a first visit understandable without removing
the evidence needed to investigate a decision.

For example, a case may match on northward wind, an initial report and unwarned
people, but have **two scouts when the current incident has one**. The card shows
that difference separately. Similarity is a reason to inspect a case, not
permission to repeat its plan.

“New plan not checked” matters: issuing a replacement forecast does not prove
that the replacement is accurate. A later observation check must pass first.
In recording playback, live evidence is hidden. Reset clears current incident
context, but historical cases and result history remain in SQLite.

## The loop and ownership

```text
Current observations + wind + people + fleet
  → belief-state forecast + retrieval of graded historical cases
  → HappyRobot decision, explanation and tool telemetry
  → simulator validates and applies executable orders
  → forecast the new plan; compare it with later observations
      → disagreement: request another decision
  → hindsight evaluation + reflection
      → persist case and eligible lesson for future decisions
```

The **simulator** owns reproducible physics, partial observations, forecast
distance, validation, SQLite and retrospective evaluation. These operations need
exact state and repeatable comparisons.

**HappyRobot** owns prioritisation, interpretation, communication and its
explanation for the chosen plan. Cases and forecasts are input evidence. The
simulator does not silently substitute its hindsight evaluator's plan for a live
HappyRobot decision.

No extra workflow is needed to advance the physics inside each possible world.
An optional future workflow could propose two or three executable plans and use
the existing `worlds.forecast(..., plans=...)` library hook to compare them. That
external tool is not wired yet.

The fork sends the new fields, but live workflow consumption and structured
accepted/rejected-case attribution are still pending. Browser demonstrations
use scripted responses; they verify the interface, not actual HappyRobot
learning. See the [HappyRobot contract](happyrobot-experience-contract.md).

## Forecasting and adaptation

`simulator/worlds.py` constructs a belief world from sensor observations,
remembered sightings, delayed satellite information and the smoke report.
Unobserved hidden fire is excluded. Population status is treated as reported
telemetry in this educational model.

By default, eight copies run for sixteen ticks with independently reseeded
spread and suppression randomness under the orders in force. The output
contains district threat frequencies, possible population outcomes, burn-cell
frequencies and disagreement among branches. These are frequencies within this
model, not calibrated operational wildfire probabilities.

The world-state distance combines fire fronts (25%), burned cells (15%),
population states (40%), district threat (15%) and fleet position (5%). Fire
fronts allow a two-cell tolerance, avoiding an alarm for every minor stochastic
shift. A representative branch—the medoid, closest to the others—is used for
comparison with observations.

After applying a decision, the controller produces a forecast for that plan.
Every four ticks within its horizon it compares new observations with the
matching forecast checkpoint. Hidden, unobserved fire alone cannot produce a
surprise. Divergence requires distance greater than:

```text
min(0.30, max(0.05, 2 × checkpoint ensemble dispersion))
```

The controller records the distance, threshold and named changes, then requests
`forecast_divergence` once for that forecast. In automatic mode this leads to a
new HappyRobot decision. Human pause/control still applies. Forecasts do not
automatically adjust the fire model's spread rate.

A visible wind change is a second, checkpoint-free path: a turn of 45° or more,
or a strength change of at least 1, breaks the premise every branch shared. An
operator wind change invalidates the standing forecast on the spot and the
`forecast_update` decision that follows carries `forecast_divergence` naming
"every branch assumed the old wind"; a wind change first seen at a checkpoint
raises `forecast_divergence` itself.

Why this design: reproducible simulation makes expectations testable; partial
observations prevent hindsight leakage; an ensemble and tolerance avoid
treating ordinary random variation as a reason to change every order.

## How historical retrieval works

`simulator/experience.py` creates a **situation signature** before each decision.
This is separate from the world-state distance above: it is a smaller,
interpretable summary for finding relevant precedents.

| Component | Weight | Numeric comparison today |
|---|---:|---|
| Response phase | 10% | Tick difference, capped at tick 30 |
| Wind | 15% | Direction and strength |
| Known fire | 15% | Confirmation and number of believed fire cells |
| Districts | 45% | Per-district population status, downwind alignment and distance to believed fire/report |
| Fleet | 15% | Idle scouts, idle extinguisher drones and mobile trucks |

District distances are capped at 40 cells. Each district contributes equally;
its population count is stored but does not weight the current distance.
Likewise, fire centroid, event type and total fleet counts are recorded but
are not direct terms in the numeric score. These are important limitations,
especially when ignition location or fleet composition changes.

For each decision, retrieval:

1. Reads applied, oracle-graded decisions with stored signatures from SQLite.
2. Excludes the current incident.
3. Excludes explicitly truncated evaluations and missing, non-numeric or
   non-finite regret. Legacy evaluations without a truncation flag remain
   eligible; a missing flag is not proof of a complete search.
4. Computes the weighted numeric distance; keeps distances at most **0.25**.
5. Returns the nearest **three**, with more recent decision IDs breaking ties.

Each returned case includes what was ordered, the retrospective outcome,
regret, the evaluator's preferred alternative when applicable, and any
reflection. The payload contains summaries, not frozen snapshots or current
hidden fire. Historical outcomes are hindsight evidence about that past
incident; they are not observations of the present.

### Fine-grained labels instead of a large ontology

The labels now provide a small, versioned vocabulary:

| Label family | Examples and rule |
|---|---|
| Phase | `phase:early` before tick 30; `phase:later` afterwards |
| Event | `event:farmer_call`, `event:forecast_divergence`, or the recorded trigger |
| Evidence | `fire:observed`, `fire:unconfirmed` |
| Wind | Eight compass directions **toward which** wind moves; `wind:calm`, `wind:light` below strength 2, `wind:strong` at or above 2 |
| People | `people:downwind` if an unwarned district has alignment ≥0.5; otherwise `people:no_downwind`; `people:nearby` if an unwarned district is within 8 cells of believed fire/report |
| Resources | `fleet:scouts:1`, `fleet:extinguishers:1`, `fleet:trucks:1` |

These labels describe the signature, not a new urgency policy. For example,
the label's downwind threshold is distinct from the oracle's cost heuristic.
`people:no_downwind` means no such exposure in the stored belief signature,
not a guarantee of safety.

New signatures store `label_version: 2` and `labels` (version 2 measures downwind
exposure for every district; version 1 zeroed it beyond 40 cells, which hid whole
towns under a west wind). Retrieval derives labels from older signatures too, without
a schema migration or invented observations.
Returned cases include `matching_labels` and `differing_labels` with
`current_only` and `case_only`. The UI translates them; raw labels and numeric
`why_similar` terms remain inspectable. An older API response with no labels
gets an explicit unavailable-label message.

**Labels currently explain retrieval; they do not change its numerical
ranking or serve as hard compatibility filters.** This preserves the measured
baseline while making weak matches visible. A large ontology or an embedding
database would add maintenance and obscure why two situations matched before
we have shown that those additions improve decisions.

### What should improve next?

Use the labels to evaluate concrete changes rather than assume a richer
taxonomy is sufficient:

1. ~~Resource compatibility~~ — done: the fleet term now weighs composition,
   and replay re-grounds intents on the current fleet
   ([benchmark](adaptation-evaluation.md), fixes 3–4).
2. Evaluate evidence freshness and event relevance in the score. Relative fire
   location is covered (fix 1: downwind exposure beyond 40 cells); the
   remaining measured gap is a wind memory never saw (west).
3. Consider diversity across incidents so one incident cannot occupy all three
   result slots. Currently all three can come from one earlier incident.
4. Add report provenance/freshness labels once a real report inbox exists;
   do not synthesize unknown timestamps or credibility.
5. Measure useful retrieval and downstream decisions on frozen held-out
   memory, then compare actual HappyRobot runs with/without cases.

The last step needs structured case acceptance/rejection from HappyRobot.
Better-looking matches alone do not establish better emergency decisions.

## “Replay” has two meanings here

**Recording playback** shows stored simulation frames. It makes no new AI calls
and does not change the incident.

**Experience replay** refers to using stored decisions as examples. In the
live path, HappyRobot receives cases as context and chooses its own action.
There are no gradients, model fine-tuning or automatic copying.

The offline `simulator.episodes.experience_agent` is a deliberately simple
test policy: it takes the first retrieved graded case within distance **0.15**
(a regret-0 case counts: its best plan is what was done) and applies the
hindsight-best plan of that case. If the current fleet cannot execute the
literal plan (different vehicle ids or counts), the plan's *intents* —
(command, district) pairs — are re-grounded on the vehicles available now via
the oracle's validated options (`episodes.reground`). Otherwise it holds.
Validity does not establish tactical suitability.

Keeping this replay policy provides a reproducible comparator. In the
[scenario-family benchmark](adaptation-evaluation.md) it matches the warning
heuristic on every family that memory has seen and falls back to hold under a
wind memory never saw (west), because the nearest case is beyond the adoption
distance. It never scored worse than hold.

## Evaluation, reflections and lessons

The hindsight **oracle** replays a frozen pre-decision state, including hidden
truth, and searches a bounded set of alternatives. Its demo cost weights
exposed people, unwarned downwind people, blocked groups, burned cells and
suppression. **Regret = actual-plan cost − best cost found.**

Lower is better under that cost model. Zero means the bounded search found no
better plan; it does not mean no harm, global optimality or mission complete.
Regret is in simulation cost points, not a percentage or number of lives saved.
Live evaluation still defaults to one future seed; the offline benchmark uses
three. Truncation and the strict `regret > 100` judgement threshold remain
important when interpreting grades.

A HappyRobot reflection can use telemetry, the frozen decision, evaluation and
execution signals to propose a root cause, rule or prompt patch. SQLite retains
these alongside the decision. Proposed explanations are evidence to inspect,
not automatically verified causal accounts.

Tier 1 stores nonempty rules for execution/judgement gaps with confidence at
least **0.5**. Active rules are ranked by context similarity; at most five are
sent. There is currently no Tier 1 recurrence gate or relevance cutoff.

Lesson exposure records which decisions received a rule. The retirement
heuristic compares mean regret with versus without exposure: after at least
three graded uses, a rule can be retired if its exposed mean is more than ten
points higher. Operators can retire/restore rules in **Saved rules**. This
comparison is observational: incident difficulty and other changes can explain
the difference. Restoration is reversible, not a permanent pin.

Inherited Tier 2 can create a Northstar after recurring diagnoses. Inherited
Tier 3 can fork/patch a workflow, **automatically publish to staging**, replay it
and record a report. Development/production promotion remains human. An
explicit opt-in before staging changes is still pending. This work does not
invoke or publish live healing.

The results view averages finite graded regrets per incident, oldest to
latest. Historical charts can include truncated evaluations even though
retrieval now excludes them. They are descriptive history, not a controlled
experiment or proof that any lesson caused improvement.

## Storage and implementation map

SQLite at `.runtime/blackbox.sqlite` is sufficient for this local demo:
transactions, inspectable records and persistence across resets without another
service. There is no Twin DB dependency. A shared multi-process deployment would
need its own access, retention and concurrency design.

| Code | Responsibility |
|---|---|
| `simulator/worlds.py` | Belief world, ensemble, state distance, surprise |
| `simulator/experience.py` | Signatures, labels, case retrieval, lesson ranking/credit, result aggregates |
| `simulator/blackbox.py` | Decisions, snapshots, telemetry, outcomes, evaluations, reflections, forecasts, surprises, cases, lesson exposures |
| `simulator/server.py` | Decision loop; `/api/state`, `/api/learning`, `/api/postmortem` |
| `simulator/oracle.py`, `analysis.py`, `reflection.py`, `healing.py` | Retrospective evaluation and proposed corrections |
| `simulator/episodes.py`, `evaluation.py` | Offline mock replay and held-out comparators |
| `simulator/static/` | Adaptation strip and evidence dialogs |

Use `python3 -m unittest discover -s tests -p 'test_*.py'` and
`node --test tests/*.cjs` for automated checks. The
[evaluation report](learning-evaluation.md) documents the separate frozen-memory
benchmark and its exact scenarios. UI tests with synthetic grades are separate
from both the benchmark and a future live HappyRobot experiment.
