# Adaptive loop: live integration audit

**20 September 2026. Verdict: the core components work, but the latest workflows
are not yet a fully connected, production-ready learning loop.**

Existing credentials cover Jev, HappyRobot inference and Cloudflare state access.
Publishing the experience workflow, aligning the dispatcher client and repairing
the fleet tool acknowledgement remain necessary. Retrieved experience has not
demonstrated a reliable improvement in live decisions.

## What actually ran

```text
Simulator-generated incident → SQLite cases → bounded briefing
                                               ├→ Jev shadow → validated, never applied
                                               ├→ experience model [isolated node tests]
                                               └→ live HappyRobot → local validation
                                                    ↓
                                      telemetry + hindsight oracle
                                                    ↓
                                      live Post-mortem → SQLite lesson
                                                    ↓
                                      next-incident retrieval: verified
```

The audit used an isolated SQLite copy. Incident observations came from the actual
simulator, not invented values for individual required fields. HappyRobot model
responses, OpenRouter responses and Cloudflare HTTP operations were real.
This is integration evidence with synthetic incidents, not emergency-response
validation or a causal learning experiment.

## Versions and credentials

| Component | Version exercised | Evidence / boundary |
|---|---|---|
| Los Panaderos | v29, staging; `01a0bbdf-1d51-7667-8f4e-4b4a3f05703a` | Three completed runs, structured orders parse and validate; repeated scout tool calls remain |
| Central — Sandbox Cloudflare | v23, development; `01a0be43-e8e1-749e-a652-f191f5facad5` | Three initial scenarios, corrected briefing, then wind change; state read/write succeeds and communication is skipped |
| Despacho Central | v26 draft | Inspected, not executed or published |
| Obtener experiencia | v1 draft | Original test-all skipped four nodes; copied model configuration tested in an isolated unpublished workflow |
| Post-mortem Los Panaderos | v1, development | Actual fleet telemetry and oracle result produced a structured diagnosis and reusable lesson |
| Jev | `typesafe/jev-1.13-20260917` | Existing local OpenRouter key works; typed choices, threat, confidence, escalation and simulator validation |

Cloudflare's `STATE_API_TOKEN` is already configured on Central and its sandbox
for development, staging and production. The sandbox successfully persists state
through the configured Worker, returning HTTP 200. Direct access to the owner's
Cloudflare account is unnecessary for this workflow path. This does not supply
missing incident observations: those still originate in the simulator or sensors.

HappyRobot's inference works without adding a user model key. No HappyRobot
OpenRouter integration was found; Jev uses the existing local key. No credential
values were copied into this report. Telephone and messaging credentials were
not exercised; tests used empty contacts and sandbox test mode.

## Findings and fixes

### Fire inferred from smoke was incorrectly presented as observed

The belief forecaster seeds possible fire around an unconfirmed smoke report.
The experience signature then incorrectly treated those simulated cells as
sensor confirmation. The experience model repeated the resulting claim.

Fixed in the repository: confirmation, observed count and observed centroid now
come from sensor memory. Forecast assumptions retain a separate
`believed_fire_cells` count. Jev receives `confirmed=false` for smoke-only input,
and the briefing explicitly identifies inferred fire.

The corrected experience model now describes the fire as unconfirmed; its
confidence was 0.96 and its district IDs passed validation. It nevertheless added
two lower-priority districts beyond the deterministic top district. Valid IDs
alone do not establish sound prioritisation.

Historical SQLite signatures are not silently migrated. The audit regenerated
signatures from snapshots in its isolated copy; existing application memories
need equivalent reindexing before old fire-confirmation labels can be trusted.

### Fleet v29 can lose a warning inside its tool loop

In the no-memory run, Scout made seven `report_scout_plan` calls: the telemetry
detector counted six repetitions. Empty tool results were treated as evidence
that a warning had already been issued. The final orders omitted that warning.
Post-mortem identified the same failure from the recorded steps.

The Central sandbox completes in a single decision pass and avoids that repeated
agent-tool topology. Its outputs still need a dedicated client/telemetry adapter:
the current client pins Los Panaderos' result node, and the telemetry harvester
looks for nodes named `... Agent`, not the sandbox's AI Extract decision node.

The fleet client now accepts `HAPPYROBOT_ENVIRONMENT` with validated values
`development`, `staging`, or `production`; its default stays `development`.
This fixes explicit environment selection. It does not switch workflow IDs,
publish anything, or fix the v29 acknowledgement problem.

### Experience model and its confidence fallback work in isolation

| Input | Model confidence | Deterministic merge |
|---|---:|---|
| Matching cases, corrected fire provenance | 0.96 | Accept model fields |
| Empty brief | 0.05 | Use fallback |
| Different fleet | 0.20 | Use fallback; model identified missing resources |
| Wind-turn event | 0.42 | Use fallback; retain explicit broken-wind premise |

The unchanged reader and merge code were exercised locally; the unchanged prompt,
model and typed extraction schema ran in HappyRobot nodes with static inputs.
Caller precedence was kept intact. This does **not** verify a published
trigger-to-result run of Obtener experiencia, which remains unpublished.

The isolated test workflow is
[Prueba de experiencia](https://platform.eu.happyrobot.ai/hackspainteam9/workflows/6i4vrtikbutj/editor/2277hnhkn0no).
Only this new, unpublished fixture was edited. Existing workflows were neither
edited nor published by the audit.

### Jev remains conservative and cannot apply orders

All five scenario calls returned valid assembled decisions with unchanged
simulator state and routed to Central. After the provenance fix, the matching-case
call had confidence 0.77 at 375 ms; the wind-turn call had confidence 0.39 at
347 ms. High confidence does not override the high-stakes escalation rule.
Jev's suggested orders were stored and graded separately from HappyRobot's.

### Adaptation and retrospective learning close different loops

At tick 10, changing wind from `[0,-1]` to `[1,0]` immediately broke the forecast
premise. The new briefing named that assumption. The sandbox accepted the replan,
returned valid updated orders and persisted state. Its final explanation did
not explicitly name the old wind assumption, so that attribution remains weaker
than the simulator's recorded evidence.

Post-mortem consumed actual fleet telemetry and oracle regret 100, diagnosed an
execution problem at confidence 0.97, and proposed:

> Do not mark an evacuation as issued from an empty result; include it in the
> final Central order unless execution has been explicitly confirmed.

The existing Tier 1 confidence/gap gate accepted the lesson in the isolated SQLite
database. Both the reflected case and the lesson were retrieved for a new incident.
No Northstars, prompt patches or healing publications were applied.

The base implementation can automatically publish a Tier 3 patch to staging.
That path was deliberately not invoked here; an operator approval gate is still
needed before calling the whole system human-controlled.

## Does experience improve decisions?

Not established. One initial v29 pair produced regret 0 with memory versus 100
without it. The same sandbox pair produced 100 in both conditions. These are
single stochastic runs, the first brief had the provenance bug described above,
and the offline oracle used one rollout seed and a 16-tick horizon.
The corrected-brief and wind-turn sandbox retests each returned valid orders with
regret 0 under that same limited grader.

These results support payload compatibility and expose failure modes. They do not
support a claim of learning, nor a production choice between the models.
Use repeated, paired runs on held-out incidents after the integration fixes,
holding all non-memory inputs constant.

## Evidence identifiers

| Check | HappyRobot run ID |
|---|---|
| Fleet v29, memory | `026ce4c1-0875-4fbc-bf93-fe8d83feaeaa` |
| Fleet v29, no memory | `e5529da7-adf1-4566-9eb7-27db469c0c06` |
| Fleet v29, changed fleet | `1044c450-184d-4f1e-9483-96c98cda44b1` |
| Sandbox v23, memory | `9724ffd0-ecf9-4ed5-b4de-3b03183b22d2` |
| Sandbox v23, no memory | `74cdf014-0523-4bd3-bd5e-37c63016f477` |
| Sandbox v23, changed fleet | `6c69a6bc-705b-4b04-8f65-f53585d7b335` |
| Sandbox v23, corrected brief | `5eaee3f8-8cc9-46c3-adae-f649351d4354` |
| Sandbox v23, wind turn | `1424dd2d-2418-485b-8193-2819d44372db` |
| Post-mortem | `e4fd367a-bb50-42c6-9069-94a8114cae82` |

## Repository checks

- Python: 224 tests, one opt-in live test skipped.
- Dashboard: 38 tests passed.
- Focused Ruff fatal-error checks and whitespace checks passed.
- New regressions cover smoke-only input, actual sensor confirmation, satellite
  inference, staging selection and invalid environment rejection.

The local stdio OAuth transport was not exercised; workflow calls used Devin's
authenticated HappyRobot MCP. No UI testing was needed for this audit.
