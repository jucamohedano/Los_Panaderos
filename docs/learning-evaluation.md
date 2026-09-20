# Experience replay evaluation

## Verdict

Persistent memory and retrieval work, but this evaluation **does not establish an
advantage over a simple warning policy or any improvement in HappyRobot itself**.
The prior same-scenario `100 → 0 → 0` example was too weak as evidence.

On 48 held-out decision snapshots, oracle-plan replay improves over hold in 15,
ties in 33 and is never worse than hold. However, the warning heuristic matches
or outperforms replay on every snapshot, and replay fails to help in the changed
ignition and reduced-fleet scenarios. The unchanged-location subset alone would
hide that failure.

## Reproduce

```sh
python3 -m simulator.evaluation --output .runtime/learning-evaluation-final
```

Use a **new** output directory: the command refuses to overwrite earlier evidence.
It creates `memory.sqlite` and `results.json`, leaving the live black box untouched.
No credentials or HappyRobot calls are required. The JSON contains every policy
plan, sample cost/outcome, retrieval distance and source decision ID, candidate
count, truncation flag, timing and training evaluation.

The preliminary run used two equivalent subsets:

```sh
python3 -m simulator.evaluation --output .runtime/learning-evaluation \
  --scenarios north east south calm north_to_east
python3 -m simulator.evaluation --output .runtime/learning-evaluation-stress \
  --scenarios town_west strong_north single_drone
```

The final run includes all eight scenarios together and additionally records
coordination adjustments, origins and fleet counts.

## Method

- **Training:** four north/east snapshots from initial seeds 9 and 17, at tick 2.
  Store the hold decision and its hindsight evaluation in SQLite.
- **Frozen memory:** training finishes before evaluation; no test outcome or
  oracle answer enters retrieval. Training/test seed overlap is rejected, and
  the decision count must remain four throughout evaluation.
- **Test set:** initial seeds 23, 47 and 81 × eight scenarios × ticks 2 and 10 =
  **48 matched snapshots**. Tick 10 follows eight passive ticks; it is not a
  continuation of any tested policy's tick-2 action.
- **Common future seeds:** 101, 211 and 307, with suppression seeded at seed+1.
  Each plan runs for 16 ticks. Four policies × 48 snapshots × three seeds =
  **576 scored policy rollouts**, plus the oracle's candidate search.
- **Comparators:** hold; reproducible random valid plan; belief-only warning
  heuristic (downwind unwarned districts, population then distance); existing
  mock `experience_agent`, which copies a valid historical oracle plan within
  distance 0.15 and otherwise holds.
- **Information boundary:** comparators receive copies of `belief_world(sim)`.
  The replay mock additionally reads the stored *past* oracle plan; no current
  hidden truth or test grade is passed to the policies.
- **Reference:** existing greedy hindsight candidate search, plus all evaluated
  plans, scored with common future seeds. Regret is mean cost minus the best of
  this bounded set. It is **not** distance to a globally optimal policy.
- **Search quality:** 120-second budget; abort on any truncated search. Zero
  truncated searches and zero plan-application exceptions in the measured set.
  Acceptance by `Simulation.apply` can still include duplicate-assignment
  adjustments, so it is not identical to flawless coordination.

All scenarios use one scout and one extinguisher, with no truck, except the
single-drone scenario. North/east/south winds have magnitude one; calm is zero.
`north_to_east` changes direction immediately before the tick-10 decision.
`strong_north` has vector `(0, -3)`. `town_west` moves the fire to vegetation
nearest 15 cells east of the north town district and applies wind `(-3, 0)`.
The latter exposes multiple populated districts. The single-drone scenario
removes the scout while retaining north wind.

## Results: mean regret

Each row averages six snapshots (three initial seeds × two decision ticks).

| Scenario | Hold | Random | Warning heuristic | Oracle-plan replay |
|---|---:|---:|---:|---:|
| North | 100.00 | 100.00 | 0.00 | 0.00 |
| East | 0.00 | 0.00 | 0.00 | 0.00 |
| South | 0.00 | 0.00 | 0.00 | 0.00 |
| Calm | 0.00 | 0.00 | 0.00 | 0.00 |
| North → east | 50.00 | 50.00 | 0.00 | 0.00 |
| Strong north | 100.00 | 100.00 | 0.00 | 0.00 |
| Town, west wind | 14,640.00 | 8,634.33 | 0.00 | 14,640.00 |
| Single drone | 100.00 | 100.00 | 0.00 | 100.00 |
| All 48 snapshots | **1,873.75** | **1,123.04** | **0.00** | **1,842.50** |

The pooled score is dominated by the populated town scenario; use the per-scenario
rows rather than presenting a single aggregate as a general performance claim.
The heuristic targets the same downwind-warning behavior rewarded by the cost,
so this is evidence against replay superiority, not proof the heuristic is an
optimal emergency-response policy.

### Physical and people outcomes

Across snapshots and seeds, the mean newly burned cells is **6.67 for every
policy**. All rollouts have **zero newly burnt people** within the short horizon.
Replay and hold have zero newly safe people; the warning comparator averages
492.75 and random averages 844.67 newly safe people. Random nevertheless has
higher regret because the score weights which populations remain downwind and
unwarned, rather than simply counting all completed evacuations.

Replay's north-wind improvement is a **warning-en-route discount** in the cost.
People at the distant farm are still unwarned at the horizon. We must not
translate `regret 0` into “people saved,” “fire contained” or “mission complete.”

### Retrieval and failure analysis

- The shifted town fire returns **no cases**: rejection of a different situation
  works. The mock then holds, with regret 14,640. A capable current-state policy
  must remain effective when memory is empty.
- The reduced fleet still retrieves three cases, but their historical plan
  includes an absent scout. The mock rejects it and holds, leaving regret 100.
  A real agent should reinterpret evidence using the current fleet, not copy
  historical orders blindly.
- All remaining scenarios retrieve three cases. The mock can choose a northern
  farm-warning case under east/south/calm wind, because it skips zero-regret
  cases and accepts a looser set than the situation warrants. This costs nothing
  under this horizon's score but is not good evidence of contextual reasoning.
- Historical best plans can reserve the farm twice; the engine keeps the scout
  warning and holds the extinguisher. A zero-regret replay can therefore still
  carry a coordination adjustment. This occurs in **33 of 48 replay plans** and
  **7 of 48 random plans**, versus zero for hold and the warning heuristic.
- Wind-shift snapshots show the new condition is available to retrieval and
  scoring; this harness does not test cancellation/communication of a previously
  executed assignment. Automatic forecast-divergence replanning was checked
  separately in the browser.

## UI and API verification

The four-stage strip is above both map headers at 1366×768 and 1024×768 and uses
a readable 2×2 layout at 390×844. Evidence opens in a native dialog without page
scrolling. Browser tests cover ES/EN, focus/keyboard behavior, asynchronous
response ordering, error recovery, replay and reset.

Two bugs were found and fixed:

1. `/api/learning` used a nonexistent response method/global controller. A real
   HTTP regression now exercises the route.
2. A completed replan relabeled the previous divergent check as passed. The strip
   now shows “New plan not checked” until a later check passes, and returns to
   that state when another forecast replaces the checked one.

Browser testing used real forecasts and isolated SQLite with **scripted
HappyRobot responses** and synthetic learning grades. Those display fixtures
are not benchmark results or evidence of live learning.

## Remaining validation before claiming adaptive learning

1. Connect dispatch/fleet inputs and structured case acceptance/rejection outputs;
   run actual HappyRobot with identical prompts and snapshots, with/without
   memory, counterbalanced order and repeated runs.
2. Evaluate longer, closed-loop trajectories with executed assignments, resource
   shortages, changed observations, blocked routes, communication outcomes and
   genuine casualty/containment differences.
3. Test lesson recurrence and retirement with matched contexts or paired replay;
   current exposed/unexposed averages are not causal credit assignment.
4. Evaluate forecast calibration/coverage on held-out scenarios before adapting
   physics parameters from surprise. A threshold check is not a calibration study.
5. Strengthen memory relevance and fallback behavior without tuning only to this
   small benchmark. Retain a separate final test set.

The [proposal audit](proposal-audit.md) retains the original input-triage,
communication, hypothesis and safe-publication commitments alongside these gaps.
