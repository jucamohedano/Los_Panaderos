> **Status note (2026-09-27).** The live HappyRobot workflows described below
> (Central dispatch, 'Post-mortem Los Panaderos', prompt patching via the MCP
> tools) are no longer reachable: the integration was removed upstream and the
> team has no platform access. The loop now runs against the local deterministic
> policy (`simulator/policy.py`). Every call that addressed the platform goes
> through `simulator/llm.py::PlatformStub`, which records what would have been
> sent and returns nothing; the post-mortem diagnosis can optionally use a model
> through `LLM_API_KEY`/`OPENROUTER_API_KEY` (`reflection.reflect_local`, default model
> `stealth/space-bunny-alpha` on OpenRouter) and is a no-op otherwise.
> **Jev is currently a stub** (`JEV_BACKEND=stub`, the default): the real model
> `typesafe/jev-1.13` is paid and the OpenRouter account has no credits, so every
> shadow verdict is synthetic, confidence 0.0, routed to Central and marked
> `stub: true` in the black box. See §5 for how to switch the real backend on.
> The modules are landed and tested but not yet wired into `SimulatorSession`.

# The self-healing loop in one page

Base: `feature/self-healing-blackbox-oracle-reflection` (black box → oracle → reflection → healing).
This branch adds the forward half (possible worlds, divergence) and decision-time experience
(retrieval, episode brief, optional curator workflow). Nothing existing was removed or republished.

## 1. Two loops, one store

```mermaid
flowchart LR
  subgraph decision["Decision time (seconds, before acting)"]
    B[Belief state<br/>observations only] --> F[Possible worlds<br/>worlds.forecast]
    B --> R[Retrieve similar graded cases<br/>experience.retrieve]
    F --> E[episode_brief<br/>bounded, evidence not orders]
    R --> E
    E -->|optional| C[Obtener experiencia<br/>curates precedents]
    E --> H[HappyRobot<br/>Los Panaderos]
    C --> H
    H --> V[Simulator validates<br/>and applies]
  end
  subgraph postmortem["Post-mortem (minutes, after acting)"]
    V --> T[Telemetry + frozen world]
    T --> O[Hindsight oracle<br/>regret, gap type]
    O --> P[Post-mortem workflow<br/>reflection, root cause, rule]
    P --> L[Lesson gate<br/>confidence, recurrence, credit]
  end
  S[(SQLite black box<br/>.runtime/blackbox.sqlite)]
  T --> S
  O --> S
  P --> S
  L --> S
  S -.next decision.-> R
  V --> A[Forecast for the plan in force]
  A -->|observed drift beyond dispersion<br/>or wind turned ≥45°| D[forecast_divergence → replan]
  D --> B
```

Decision time reads from memory and forecasts; the post-mortem writes to memory. The two never
reason over the same question: the post-mortem asks *why did this decision cost regret*, the
decision asks *what applies now*. Audit: `docs/proposal-audit.md`.

## 2. Who owns what

```mermaid
flowchart TB
  subgraph sim["Simulator (Python)"]
    s1[Physics, partial observation, hidden truth]
    s2[Forecast ensemble + world distance]
    s3[Signature, labels, retrieval, brief]
    s4[Validation, apply, telemetry]
    s5[Oracle, lesson gating, credit]
    s6[(SQLite)]
  end
  subgraph hr["HappyRobot (hackspainteam9 / HackSpain)"]
    h1[Los Panaderos · live · untouched]
    h2[Post-mortem Los Panaderos · live · untouched]
    h3[Obtener experiencia · new · callable · unpublished]
  end
  subgraph jev["Jev (OpenRouter · currently stub) · shadow only"]
    j1[Typed choice / score / escalate<br/>graded beside Central, never applied]
  end
  s3 -->|payload fields| h1
  s3 -->|episode_brief| h3
  h3 -->|mission fields + provenance| h1
  s4 -->|decision record| h2
  h2 -->|diagnosis| s5
  s3 -.compact belief.-> j1
  j1 -.verdict + agreement.-> s6
```

Why: exact state, reproducibility and measurable distance live where the physics is. Judgement,
communication and explanation live in the agents. Memory is SQLite because one process owns the
loop and every row must be replayable offline; Twin DB is out of scope.

## 3. What the agent receives, and what it is not

```mermaid
flowchart LR
  subgraph brief["world_state.episode_brief (≤1200 chars text)"]
    b1[situation: tick, labels, unwarned]
    b2[priority_districts<br/>people × max(forecast threat, downwind, proximity)]
    b3[downwind_front]
    b4[≤3 cases: did / regret / oracle preferred / root cause]
    b5[≤5 lessons with relevance]
    b6[forecast: dispersion, threatened]
    b7[divergence only if it fired]
  end
  brief --> X{Closing line:<br/>"Cases and lessons are evidence, not orders."}
  X --> Y[Current observations, rules,<br/>constraints prevail]
```

Compatibility fields stay (`similar_cases`, `lessons_learned`, `possible_worlds`,
`forecast_divergence`). `mission`, `priority_districts`, `downwind_front`,
`tactical_constraints` are filled from the brief only when the dispatcher left them empty.
Hidden truth never enters any of these.

## 4. Callable workflow, not a modified one

```mermaid
sequenceDiagram
  participant Sim as Simulator
  participant Ex as Obtener experiencia
  participant LP as Los Panaderos (live)
  Sim->>Sim: forecast + retrieve + brief
  opt LP_EXPERIENCE_WORKFLOW=1
    Sim->>Ex: trigger_run(event, world_state, empty mission fields)
    Ex->>Ex: read brief → judge precedents (AI) → validate
    Ex-->>Sim: mission fields + source{caller|agent|deterministic} + confidence
  end
  Sim->>Sim: fill still-empty fields from the deterministic brief
  Sim->>LP: trigger_run(payload)
  LP-->>Sim: orders + explanation
```

Inside the workflow: caller fields always win; agent output is used only with confidence ≥ 0.5,
valid district ids and a non-empty mission; otherwise the deterministic brief is returned. Any
failure on the simulator side logs "Experience workflow skipped" and proceeds. Contract:
`docs/happyrobot-experience-contract.md`; client: `simulator/curator.py`.

## 5. Jev reflex: a shadow beside Central

```mermaid
sequenceDiagram
  participant Sim as Simulator
  participant Jev as Jev (typesafe/jev-1.13)
  participant LP as Los Panaderos (live)
  participant PM as Post-mortem
  Sim->>Sim: compact belief state + one typed choice per vehicle<br/>(candidates from oracle.vehicle_options)
  par shadow
    Sim->>Jev: decisions API (3 s timeout)
    Jev-->>Sim: choice / noul(escalate) / score(threat)
    Sim->>Sim: assemble → validate on a copy → route(reflex | central)
  and system 2
    Sim->>LP: trigger_run(payload)
    LP-->>Sim: orders + explanation
  end
  Sim->>Sim: apply LP orders only; save verdict + agreement (reflexes table)
  PM->>PM: oracle grades LP; same seeds grade the shadow → regret, vs_central
```

`simulator/reflex.py`. **Currently a stub.** `JEV_BACKEND` selects the backend behind `decide()`;
the public functions (`describe`, `questions`, `assemble`, `route`, `summarize`, `decide`) are the
same for both:

| `JEV_BACKEND` | class | what happens |
|---|---|---|
| `stub` (default, also for unset/unknown values) | `StubBackend` | No network. Every vehicle gets its passive default candidate at confidence 0.0, `escalate` noul is 1.0, threat is unknown. `assemble`/`route` run normally and the verdict is always `route: central` with the first reason `stub backend: synthetic answer, no model was called`. The record carries `backend: "stub"`, `stub: true`, `model: "stub/jev-unavailable"`; `BlackBox.reflex_summary()` counts them under `stub`. |
| `openrouter` | `OpenRouterBackend` | The real call: `POST https://openrouter.ai/api/alpha/decisions` with `typesafe/jev-1.13`, bearer `OPENROUTER_API_KEY`, 3 s timeout. **This model is not free; the account currently has no credits, so it fails until credits are added.** Without a key the mode is `off` and `decide()` raises `reflex disabled: no key`. |

To switch Jev on for real, once the OpenRouter account has credits:

```sh
export JEV_BACKEND=openrouter
export OPENROUTER_API_KEY=<your key>  # or keep it in the gitignored .env; reflex.load_env_key() exports it
# optional: REFLEX_MODE=shadow (default when a key is present) | off
```

A stub verdict must never be read as Jev's opinion: check `stub`/`backend` before using `route`,
`confidence` or `agreement` as evidence. `REFLEX_MODE=off` disables the shadow entirely, stub or
not. Route is *central* whenever confidence < 0.7 on the chosen kind of order,
`escalate` ≥ 0.5, any order is high-stakes (`evacuate_*`, `attack_sector`, `contain`), a choice
is unknown, or the assembled decision fails validation. Timeouts and schema errors log "Jev shadow
skipped" and change nothing. A `gated` mode that applies low-stakes reflex orders is deliberately
not implemented: it needs the shadow evidence below first, and explicit approval.

## 6. High stakes stay with people

```mermaid
flowchart LR
  I[Incoming decision] --> Q{Evacuation or<br/>suppression order?}
  Q -->|yes| Central[HappyRobot Central + operator]
  Q -->|no, and a reflex is confident| Fast[Reflex may apply<br/>validated candidate · future gated mode]
  Q -->|no, unsure| Central
  Central --> Op[Operator can pause, revoke lessons,<br/>approve prompt patches]
```

Lessons enter payloads only above the confidence gate; prompt patches are queued for approval;
nothing is auto-published. The same rule bounds the Jev reflex.

## 7. Measured vs. not yet shown

| Claim | Status | Evidence |
|---|---|---|
| Forecast excludes hidden truth; divergence names what changed | measured | `tests/test_worlds.py`, UI recording |
| Retrieval returns close graded cases with matching labels | measured | `tests/test_experience.py`, `adaptation_eval` retrieval coverage |
| Brief is bounded and keeps the evidence boundary | measured | `tests/test_episode_brief.py` |
| Deterministic brief priorities are at least as good as the warning heuristic on held-out seeds | measured in the simulator | `docs/adaptation-evaluation.md` |
| Replay of oracle plans beats hold, never worse, does not beat warning | measured in the simulator | `docs/learning-evaluation.md` |
| HappyRobot decisions improve because of cases/brief | **not shown** | needs live runs with the payload; browser tests used scripted agents |
| Obtener experiencia improves fleet mission fields | **not shown** | node tests pass; unpublished, no live run yet |
| Jev shadow is typed, validated, fail-closed and never applied | measured | `tests/test_reflex.py`; live smoke (Sept 2026, before credits ran out): ~250 ms, routed to Central |
| Jev stub is offline, routed to Central and marked as stub in the black box | measured | `tests/test_reflex.py::JevBackendTests`; no real verdicts are produced while `JEV_BACKEND=stub` |
| Jev reflex could safely replace Central on low-stakes orders | **not shown** | needs shadow agreement / regret evidence over many decisions (post-mortem `reflex` column, `/api/learning.reflex`) |

In-context learning here means better *retrieved evidence*, not weight updates; the model is
frozen. What improves between incidents is the memory and the gates around it.
