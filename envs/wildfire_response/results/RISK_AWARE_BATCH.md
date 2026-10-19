# Risk-aware director batch — 19 October 2026

## Frozen setup

The free `nvidia/nemotron-3.5-super-vl:free` model uses the existing named-choice
interface, with a frozen `risk-aware` prompt. The prompt balances precautionary
warnings against verification, asks for parallel investigation and consideration
of competing reports, and explicitly discourages repeated notices. It does not
change reward weights, simulation events, legal action choices or model weights.

Planned cases, all training-only, with different locations from the first smoke
batch: `train-009` conflicting reports, `train-010` wind shifts, `train-007` false
alarm, `train-011` multiple fires, `train-006` smoke and `train-008` duplicates.
Each episode permits 32 decisions / 256 ticks. A shared safety cap permits at most
24 API requests across the entire batch. Partial episodes are retained, not scored
as completed episodes. Completed model trajectories replay and receive matched
deterministic comparisons at the same seeds and horizons.

The batch stops on the first provider limit or other transport error, with no
retry, model/provider switch or paid fallback. Provider HTTP 402/429, access/service
errors and the local request cap are distinct stop reasons. The local cap is not
evidence of an external quota rejection. No validation or test cases are used.

## CPU calibration of the planned cases

Twelve scripted full-budget rollouts compared passive and deterministic policies:

- Passive mean reward: **-8.523169044**; mean exposed population fraction
  **0.084059502**; **1/6** natural completions.
- Deterministic mean reward: **-0.036686721**; zero population exposure;
  **4/6** natural completions.
- In the conflicting-report case, passive behavior exposes **24.777748%** of the
  simulated population, while the deterministic baseline protects everyone.
- Multiple-fire and duplicate cases remain budget-truncated even with the
  deterministic policy, showing that successful command parsing is not resolution.

These cases offer greater outcome consequences than most of the original six
smoke configurations, but are still synthetic examples on one approximate map.
Some include a scripted later ignition at an occupied district: an explicit
scenario stressor, not a physics-derived forecast or real historical observation.

## First completed model episode

`train-009` finishes naturally after eight valid decisions with no invalid actions.
However, population exposure is **0.247777484** and reward is **-24.787759152**,
versus **-0.000009821** for the matched deterministic baseline. There are five
redundant messages. The model repeatedly informs `town` but sends no evacuation
order; completion therefore does not mean a successful response. Runtime:
**196.097 seconds**. Eight returned completions report zero cost.

The recorded trajectory replays exactly. This is an exploratory observation,
not evidence that the new prompt is better than the standard prompt, which has
not been run on this same scenario in a paired model comparison. The default
director profile and production runtime remain unchanged.

## Artifacts and checks

- `.runtime/risk-aware-calibration-2026-10-19/scores.json`: scripted calibration.
- `.runtime/director-risk-aware-batch-2026-10-19/`: batch status, completed episode
  outcomes, public trajectories, named-choice proposals and request usage metadata.
- **43 local tests pass**, including simulated first-limit stop, no retry,
  shared-cap accounting, access-versus-quota distinction and test-split exclusion.

The fake HTTP 429/403 printed by unit tests are simulated checks, not live quota
events. Actual provider status is recorded separately in the batch summary.
