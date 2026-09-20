# Proposal audit: what is implemented and what remains

Scope: fork PR [#1](https://github.com/jucamohedano/Los_Panaderos/pull/1), targeting
`feature/self-healing-blackbox-oracle-reflection`. This is a reconciliation of the
earlier proposals, not a claim that every suggestion has been delivered.

## Decisions retained

- **SQLite is sufficient and is the only database in scope.** Keep the persistent
  black box for snapshots, forecasts, telemetry, reflections, cases and lessons.
  A shared database is unnecessary for this local demonstration.
- The simulator owns reproducible physics, belief-state futures and hindsight
  evaluation. HappyRobot owns interpretation, prioritisation, coordination,
  communication and explanations.
- No new workflow is needed for physics rollouts. Candidate-plan generation is
  optional; no agent executes independently inside every simulation branch.
- Keep work on the fork. Upstream merging/rebasing is outside the current request.
- Prioritise the earliest decisions and limited resources; do not equate a
  notification being sent with acknowledgment or completed action.

## Coverage of every proposal

| Earlier suggestion | Verified state | What still defines completion |
|---|---|---|
| Bursts of 30–50 reports, including duplicates, stale and contradictory messages | **Pending.** The fork has a farmer report, human messages and sensor events; no report inbox/burst/triage pipeline. | Persist report IDs, timestamps and sources; show accepted/deferred/rejected IDs and decision latency under bursts. |
| Cloudflare report inbox and simulator polling | **Proposed only.** Previous workflow inspection showed shared state/contact lookup. Worker source has not been verified. | Inspect the actual Worker source/API before proposing routes or writes. Preserve this as an integration dependency. |
| HappyRobot normalisation, grouping, prioritisation and selective notifications | **Pending integration.** Communication workflows exist; this fork calls the fleet workflow. | Route selected evidence through dispatch, preserve report provenance, prioritise recipients and avoid redundant notifications. |
| Two-way responder/resident feedback | **Pending.** No structured acknowledgment → new observation → replanning loop is connected here. | Model sent/acknowledged/blocked/completed separately; changed response must trigger the appropriate reassessment. |
| Wind, new observations and scarce resource allocation | **Implemented in the simulator.** Wind affects physics; changing it requests reassessment; order validation handles fleet IDs and conflicting district assignments. | Demonstrate with live dispatch/communication. A scheduled weather/responder-event scenario is still useful. |
| Belief-only possible-world ensemble before decisions | **Implemented.** `worlds.py`, payload `possible_worlds`, SQLite forecasts. | Continue checking held-out calibration. Eight branches are model frequencies, not calibrated operational probabilities. |
| One world distance for multiple purposes | **Partial.** State distance measures ensemble dispersion and observed surprise. | Information-gap distance and world-distance regret are not implemented; oracle regret remains a cost difference. Case retrieval uses a separate signature distance. |
| Runtime surprise → named invalidated assumption → replanning | **Implemented locally.** Four-tick checks can raise `forecast_divergence`; hidden unobserved fire alone cannot trigger it. | Validate live agent reasoning, revised assignments and downstream communication. The UI must distinguish requested replan, completed plan and subsequently checked forecast. |
| Physics under alternative concrete plans | **Library hook implemented.** `worlds.forecast(..., plans=...)`. | No externally callable `evaluate_plans` tool or hypothesis workflow is wired. Add only after the core live integration works. |
| HappyRobot-generated hypotheses | **Deferred intentionally.** | Limit to 2–3 executable plans, validate them, simulate them and let HappyRobot select/explain. Do not treat prose scenarios as calibrated predictions. |
| Black box, telemetry, oracle, persisted reflections | **Implemented in the foundation.** `blackbox.py`, `telemetry.py`, `analysis.py`, `reflection.py`. | Local tests mock calls; a fresh live run must establish current integration compatibility. |
| Explain WHY each action happened | **Partial.** Payload, orders, telemetry and reflection are retained. | Structured report IDs, applied/rejected case IDs, forecast basis and invalidated-assumption output still need parsing, storage and display. Raw reasoning alone does not establish evidence attribution. |
| Cross-incident case retrieval | **Implemented.** Up to three graded cases, belief-only signature, distance cutoff, current-incident exclusion. | Add strong distribution-shift tests, bounded text lengths and quality filters for incomplete/truncated evaluations. Do not copy historical plans without checking current evidence. |
| Contextual lessons, confidence and recurrence gates | **Partial.** Confidence ≥0.5 and execution/judgement gap gate; contextual ranking; deduplication. | Tier 1 has **no recurrence gate**. Ranking has no relevance cutoff, so unrelated/context-free lessons may still be sent. Tier 2 recurrence is a different mechanism. |
| Track whether experience is used | **Partial.** Cases/lessons sent are recorded; lesson exposures are counted. | `experience_used` is only a documented output contract. The dashboard deliberately says “sent / use unconfirmed.” |
| Lesson credit, retirement and operator controls | **Partial.** Exposed/unexposed mean regret, ≥3-use retirement heuristic, retire/restore UI. | Comparable-context controls or paired replay A/B are needed for causal attribution. Current groups are observational and can differ in difficulty. Pinning is not implemented. |
| Surprise-derived world-model learning | **Pending.** Surprise history is persisted; spread/horizon are not learned. | Build a calibration dataset and evaluate coverage before changing model parameters. Earlier advice to automatically “nudge” physics was premature; keep calibration proposals reviewable. |
| Safe prompt-patch proposals, A/B and human promotion | **Partial; important discrepancy.** Tier 3 forks, patches, **automatically publishes to staging** with `force=True`, then replays. It does not publish to development/production. | An explicit opt-in/approval gate before any workflow mutation/publication remains necessary to satisfy the earlier “nothing published automatically” promise. No live healing was invoked in this work. |
| Multi-seed oracle, boundary calibration | **Evaluation harness uses three future seeds.** Live `oracle.evaluate` still defaults to `(9,)`; judgement uses `regret > 100`. | A regret of exactly 100 is still classified `none`. Review threshold semantics and evaluate latency before changing live defaults. Never treat a truncated search as strong zero-regret evidence. |
| Replayed episodes and visible learning curve | **Implemented as a mock-policy demonstration.** | Same-seed oracle-plan copying is not proof that HappyRobot learns. Use frozen-memory held-out comparisons and stronger baselines; then test actual HappyRobot consumption. |
| Clean, no-page-scroll learning/adaptation view | **Implemented in this update.** Four stages above maps; evidence opens in dialogs; responsive layout, replay/reset handling. | Preserve honest states and readable first-glance labels. A completed replan does not itself mean a forecast check passed. |

## Current HappyRobot integration finding

Read-only inspection confirmed **Los Panaderos v29**, version
`01a0bbdf-1d51-7667-8f4e-4b4a3f05703a`, still live. The Drone Agent prompt says:

> “You do NOT decide strategy. The dispatch core decides who is warned, which
> districts have priority and where the effort goes…”

It expects `mission`, `priority_districts`, `downwind_front` and
`tactical_constraints`. The inspected prompt receives `world_state` but has no
explicit `possible_worlds`, `similar_cases` or `forecast_divergence` instructions.
The fork calls Los Panaderos directly; it does not yet implement the upstream
dispatch round-trip expected by this prompt.

Therefore the earlier suggestion to add all strategic reasoning to the fleet
prompt needs refinement: **dispatch should select priorities using forecasts and
experience; the fleet agents should translate them into safe executable orders.**
Inspect the current dispatch version and its result schema before editing a draft.
No workflow was changed or published during this audit.

## Priority order after the current UI/evaluation slice

1. **Safe live integration:** gate automatic workflow mutation/publication; align
   dispatch → fleet → outcome contracts; record a live run showing forecast and
   experience acceptance/rejection. This closes a gap between local behavior and
   the actual agents.
2. **Input load and communication:** deterministic report bursts, triage and
   recipient-aware messages with acknowledgments. These original challenge goals
   remain central and must not disappear behind forecasting features.
3. **Credible learning:** relevance cutoff, recurrence, matched lesson comparisons,
   provenance/quality filters and held-out multi-step evaluation. Retain both
   positive and negative results.
4. **Optional candidate-plan workflow and calibration:** only after the preceding
   loop is demonstrated. No need for a new database or agents inside each rollout.

## Claims we can and cannot make

We can demonstrate persistent SQLite memory, retrieval, reproducible model futures,
observation-triggered replanning, and an auditable interface.

We cannot yet claim improved live HappyRobot decisions across incidents, causal
benefit from a lesson, calibrated wildfire forecasting, completed high-volume
triage, or closed-loop communication. The held-out benchmark and browser evidence
are recorded separately so those limitations remain visible.
