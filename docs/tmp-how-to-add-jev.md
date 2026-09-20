# TMP: How to add Jev (without Guillermo's store)

Temporary notes. Not a spec. Goal: drop a System-One reflex in front of HappyRobot so low-stakes orders apply in ~300 ms, and Central only runs when Jev is unsure or the action is high-stakes.

Jev is TypeSafe's decision model (`typesafe/jev-1.13` on OpenRouter). State in, typed `choice` / `score` / `noul` out. It does not generate text. We already have an OpenRouter key.

## Why not Guillermo's Worker

Despacho Central and Gestor info incendios persist reports and dispatch state on Guillermo's Cloudflare Worker (`los-panaderos-state.guillermovillarsanchez.workers.dev`). We do not have that dataset, token, or source.

Do not wait on it. The simulator already owns the loop, the black box, and the candidate action space. Jev can run on that. Citizen-call fusion is a later swap: same JSON shape, different URL.

## Where Jev lives

**Not a new HappyRobot workflow.** Call it from the Python backend (`simulator/reflex.py` + a few lines in `Controller._decide`), the same place that today calls Los Panaderos via MCP.

HappyRobot stays the System-2 path. Putting Jev *inside* the workflow would add MCP poll latency and kill the point of a 70–500 ms model.

Optional later: a Jev triage node in **Post-mortem Los Panaderos** (not latency-sensitive). That is independent of the reflex.

## What to build

### 1. Client

`POST https://openrouter.ai/api/alpha/decisions` with `Authorization: Bearer $OPENROUTER_API_KEY`.

```json
{
  "model": "typesafe/jev-1.13",
  "state": { "...filtered world..." },
  "questions": {
    "scout-1": { "type": "choice", "instructions": "...", "criteria": { "hold": "...", "patrol": "..." } },
    "escalate": { "type": "noul", "instructions": "This situation needs Central." },
    "threat": { "type": "score", "instructions": "...", "criteria": ["calm", "watch", "urgent", "critical"] }
  }
}
```

Pin `typesafe/jev-1.13`. Timeout 3 s. Missing key → reflex off, current behaviour unchanged.

### 2. State (small, complete, no memory on Jev's side)

Do not dump the full payload. Jev's accuracy drops with unused context.

Include: event type, wind, known fire (fresh/stale), each vehicle's position/status, districts (status, downwind, population), last 3 mission records, active lessons, one similar past case if we have graded history.

Leave out: hidden ground truth, full grid, satellite internals.

JevPilot pattern worth copying: compact tables, instructions only for situations that apply, resolve single-option questions in code without calling the API.

### 3. Action space = `oracle.vehicle_options(sim)`

Per vehicle, a short validated list (hold / scout cells / contain cells / evacuate district; truck continue / attack leading cells; scout continue / patrol / evacuate). One `choice` per vehicle. Code assembles and `sim.apply` validates. Jev never invents coordinates.

High-stakes (`evacuate_*`, contain/attack with no known fire) always escalate to Central, even if Jev is confident.

### 4. Controller cascade

On every existing decision point (event + 16-tick cadence):

1. If `REFLEX_MODE=off` or no key → Central as today.
2. Call Jev. If escalate.noul high, confidence below threshold, invalid assembly, timeout, or high-stakes → Central.
3. Else apply Jev's orders, record `source='jev'` in the black box, schedule the post-mortem pipeline (oracle still grades; telemetry harvest is skipped because there is no HappyRobot run).
4. `command_rejected` retries always go to Central.

Modes: `gated` (default with a key), `shadow` (Jev decides, Central still applies, oracle grades both), `off`.

### 5. Learning without Guillermo

The black box is our store. Lessons already re-enter the next payload. For Jev:

- Put the five active lessons + one similar regretful case in Jev's state (pick the similar case with a `noul` per recent graded decision).
- Persist per-action confidence thresholds in SQLite (`hold`, `continue`, `scout`, `contain`, `patrol`, `attack_sector`). After the oracle grades a Jev decision: raise the bar on regret, lower it on success. Bounded, e.g. start 0.7, −0.02 / +0.10, floor 0.55, cap 0.95.

No Twin DB, no Worker.

## Later: fused evidence (when we have a store)

If we want citizen calls and Despacho Central in Jev's state without Guillermo's KV:

1. Add `/api/reports` + `/api/dispatch-state` on this same Python server (or a Worker under our account).
2. Seed synthetic Marina-shaped report cards for the demo.
3. Fork Gestor / Despacho Central and point `poll_url` / `reset_url` / `STATE_API_TOKEN` at us.
4. Jev then also answers: per-report credibility vs sensors, duplicate?, conflict with last dispatch?

That is a separate layer. The reflex works without it.

## Safety / demo notes

- Jev never bypasses `sim.apply`. Never runs in replay. Never evacuates on its own.
- Fail closed: any HTTP/schema error → Central.
- Dashboard: add a Who column (Central / Jev) on the post-mortem table; show escalation reason and current thresholds.
- Staging hazard unrelated but live: `healing.AB_ENVIRONMENT = 'staging'` would force-publish over the team's Los Panaderos v29. Do not turn that on while adding Jev. Fix healing to fork+patch without publishing.

## Files to add (when we implement)

- `simulator/reflex.py` — OpenRouter client, state/questions, assemble, gate
- `simulator/server.py` — cascade in `_decide`
- `simulator/blackbox.py` — `source`, `reflex_json`, thresholds table
- `tests/test_reflex.py` — mocked HTTP
- dashboard Who column
- env: `OPENROUTER_API_KEY`, `REFLEX_MODE=gated|shadow|off`

HappyRobot workflows to leave alone for v1: Los Panaderos v26 (development) / v29 (staging), Post-mortem v1.
