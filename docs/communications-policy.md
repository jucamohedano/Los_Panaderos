# Communications policy (`simulator/comms.py`)

Status 2026-09-27: after every accepted fleet decision, `SimulatorSession` now
asks a **communications policy** who Central should warn and feeds the result to
`Simulation.apply_communications`. Before this the session recorded
`avisos_lanzados=[]`, `destinatarios=[]` — nothing was ever communicated.

**Delivery is SIMULATED.** No Telegram message is sent, no phone is dialled, no
HappyRobot workflow is called. The engine only logs the message and applies the
in-simulation effect (a district starts evacuating, a person is "called").

## The switch

```
COMMS_POLICY=deterministic   # default: rule-based Spanish templates
COMMS_POLICY=llm             # ask the model configured in simulator/llm.py
```

`comms.policy()` reads the variable once per session and returns
`DeterministicCommsPolicy` or `LLMCommsPolicy`. Any other value (or none) is
deterministic; it never raises. The active choice is visible as
`comms_policy` in the session's public state.

`COMMS_POLICY=llm` needs the same credentials as the rest of the LLM path:
`LLM_API_KEY` or `OPENROUTER_API_KEY` (env or gitignored `.env`), optional
`LLM_BASE_URL` / `LLM_MODEL` (defaults: OpenRouter, `stealth/space-bunny-alpha`).
Without a key the LLM policy silently produces deterministic output marked as
fallback (see provenance).

Both policies expose `decide(sim, dispatch, event_type) -> list[dict]`.

## Deterministic policy

Observation-bounded: it reuses `DeterministicFleetPolicy._urgent_districts`
(wind strength > 1.8, district `unwarned`, in the downwind sector of an
*observed* fire or of the farmer's report) and never reads hidden fire cells.

Per decision it emits, in this order:

1. `zone_alert` / `action=evacuate` / `criticality=alta` to every urgent
   `unwarned` district. Text names the district, its population and the refuge
   from `contacts.REFUGES` (name + route).
2. `call` to every person in `contacts.people()` whose `district_id` is being
   evacuated — once per incident (deduped against `sim.communications`).
3. Once a fire is confirmed (`sim.observation` non-empty): `zone_alert` /
   `action=inform` / `criticality=media` to every other `unwarned` district,
   once per incident. `inform` never changes a district's status.

Identical simulation state gives byte-identical output; the policy mutates
nothing. Recipients are only keys of `sim.groups` and names from
`contacts.people()`; it never invents ids, names, phones or chat ids.

Note: `_urgent_districts` can list several districts at once (a west wind puts
all four town districts downwind of the report). The fleet policy warns one
district per vehicle; the comms policy alerts all of them in the same decision,
so those districts flip to `evacuating` immediately and any in-flight
`evacuate_*` drone mission to them becomes redundant.

## LLM policy

`LLMCommsPolicy` builds an observation-bounded JSON state and calls
`simulator.llm.complete_json(SYSTEM_PROMPT, state)`:

```
tick, event, wind, confirmed_fire_cells (sim.observation),
observed_fire_cells (memory cells seen burning), mission (fleet decision text),
districts[{id, name, kind, population, status, downwind, urgent, refuge}],
vehicles[{id, role, mode, status}], people[{name, role, district_id}]
```

No hidden cell data is included. The model is asked for
`{"messages": [ ...message dicts... ]}` in the schema below. `validate()` then
checks **every** item; the whole reply is rejected on the first problem:

- `kind` not in `zone_alert | call | personal_message`
- `zone_alert` with a `district_id` not in `sim.groups`
- `zone_alert` without an explicit `action` in `evacuate | inform`
- `call` / `personal_message` with a `contact_name` not in `contacts.people()`
- empty `information`

Rejection, a `None` reply (no key, timeout, HTTP error, unparsable JSON) or an
exception all fall back to `DeterministicCommsPolicy` for that decision. The
reason is kept in `policy.last_error`. Tests never touch the network.

## Message schema (what the policies emit)

```json
{"kind": "zone_alert", "district_id": "town_north", "criticality": "alta",
 "action": "evacuate", "information": "AVISO DE EVACUACIÓN para Prado Alto (2815 personas): ...",
 "status": "sent"}
{"kind": "call", "contact_name": "Carmen Ortega", "district_id": "town_north",
 "criticality": "alta", "information": "...", "status": "sent"}
```

The engine rebuilds each record from a fixed whitelist (`tick, kind, channel,
district_id, contact_name, criticality, status, information, run_id` plus
`action, action_source, effect` for zone alerts) and discards other keys.

### Safety rule: `action` is always explicit

`Simulation.alert_action` reads an unlabelled zone alert as `inform` on purpose
(a calm message must never put a district on the road). Both policies set
`action` explicitly on every `zone_alert`; the LLM validator rejects alerts
without it, so `action_source` is always `explicit` and the default is never
relied on.

### Status: `sent` vs `simulated`

`comms.SIMULATED_STATUS` (module constant, one-line switch) is `'sent'`.
`sent` is in the engine's `DELIVERED_STATUSES`, so a simulated evacuation order
still flips an `unwarned` district to `evacuating` and raises
`evacuation_warning_delivered` — the demo keeps its core effect. Honesty comes
from the provenance field below, not from the status. Setting the constant to
`'simulated'` makes every message a pure log entry with `effect=no_change`.

## Provenance

Because the engine strips unknown keys, `policy_source` cannot be passed
through `apply_communications`. The session attaches it **after** the call:

```python
applied = self.sim.apply_communications(messages)   # same dict objects it stores
comms.attach_provenance(applied, comms.source_of(self.comms))
```

`apply_communications` appends the very dicts it returns to
`sim.communications`, so the stamp lands in the stored history, in
`state()['communications']`, in checkpoints and in the archive
(`communications_sent`). Verified by `tests/test_comms.py`. Alignment is exact
because the session pre-filters its own list to the three valid kinds, so the
engine skips nothing.

Values:

| `policy_source` | meaning |
|---|---|
| `deterministic` | rule-based policy produced the message |
| `llm` | the model produced it and it passed validation |
| `llm_fallback` | LLM mode was selected but the deterministic policy answered (no key, failure, or invalid reply) |

Where it shows: each record in `sim.communications`; each entry of
`dispatch.avisos_lanzados` (`zone_alert:evacuate→town_north [deterministic]`);
`decision_log[-1]['communications']`; `public_state()['comms_policy']`.
The frontend under `simulator/static/` was not changed, so the archive shows the
message text but not yet the `policy_source` label — that is a follow-up.

## Limitations

- Delivery is simulated only; nothing leaves the process.
- The LLM path has only been exercised with mocked replies (no key on the
  development machine).
- The comms policy runs after the fleet decision and cannot influence it.
- A comms-policy exception is logged (`system` source) and yields no messages;
  the fleet decision stands.
