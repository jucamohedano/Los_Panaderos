# OpenRouter director smoke test — 19 October 2026

Model: `nvidia/nemotron-3.5-super-vl:free`, invoked with the user's existing
OpenRouter credential. No credential was copied into the repository or artifacts.
The final run uses strict role-specific function schemas, extended reasoning
disabled, public observations and previous-action validation feedback. Invalid
orders remain invalid; the adapter does not substitute a scripted policy.

## Final four-decision run

- Scenario `train-000`, four decisions / 32 simulation ticks, one CPU episode.
- Four model requests; two valid actions, two invalid actions.
- Rejections: inappropriate scout district ID; unsafe/ineffective containment position.
- Director reward: **-0.10401570870535715**.
- Matched deterministic reward: **-0.000007672991071428572**. Less negative is better.
- Recorded-action replay reproduces the director reward and observations exactly.
- Budget truncation, not natural termination; no claim that the incident was resolved.
- Elapsed director episode: **83.202 seconds**.
- Model usage: **16,249 prompt tokens**, **1,985 completion tokens**.
- Provider-reported cost for these four requests: **0**.
- Provider response times: 46.992, 7.870, 18.154 and 8.793 seconds.

Artifacts are local and ignored by Git:
`.runtime/director-openrouter-2026-10-19-strict/` contains `director.json.gz`,
`deterministic.json.gz`, `requests.json` and `summary.json`. The trajectory records
the source digest; the run began while the strict-schema patch was still being
committed, so its Git HEAD alone does not identify the whole working tree.

## Debugging history

The first attempt used two requests: one completion exhausted all 4,096 tokens
on reasoning without a usable action, then the provider returned HTTP 502. The
runner stopped and saved an incomplete artifact rather than retrying or claiming
a completed evaluation. Disabling extended reasoning avoided that failure mode.

The second attempt made three requests but all actions failed validation,
triggering the environment's three-invalid-action truncation. The model mixed
fleet roles and message recipients and added an unsupported root field. A tighter
schema constrains IDs, role arrays and communication recipient/channel combinations.

There were nine request attempts across these three debugging runs. The adapter
did not switch to another model or a paid provider. Reported costs for returned
completions were zero; the failed request did not supply a usage record.

## Interpretation and next work

This validates the model-to-environment connection and reproducible scoring, not
director competence. The selected model still performs worse than the scripted
baseline in this short scenario. All 31 core/data/director tests pass without
network access; the model itself is nondeterministic and provider availability varies.

Next evaluate command compliance across the six scenario families before spending
requests on full-length or held-out runs. Improve command-specific schema constraints
and candidate selection without silently repairing tactical choices. Then measure
population exposure, burned area, evacuation disruption and redundant notices over
matched seeds and horizons. The 2026 official evidence pack has not yet been converted
into playable historical scenarios. No model weights were trained in this test.
