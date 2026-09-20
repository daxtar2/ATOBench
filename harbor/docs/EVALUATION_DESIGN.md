# ATOBench evaluation design (Harbor era)

Status: implemented (reward v3 + stage-chain analyzer)
Supersedes: the implicit "reward = G" reading of the spike design

## Position

`G = E ∧ C ∧ S` is retained as the **primary endpoint** — the headline,
cross-benchmark-comparable quantity — but it is not the evaluation. A single
bit per episode conflates three different failures (never reached the surface,
reached but never recovered, recovered but never reported). The evaluation
object of ATOBench is the **stage chain**, and the evaluation report is the
vector of stage-conditional probabilities, measured as a function of
**deception difficulty**.

Everything scored is computed from the proxy-side wire log and the report
file. No model participates in any scored path. Judges exist only in the
offline diagnostic layer and never feed a score.

## The stage chain

Penetration verification has a causal structure — evidence flows from
observation through reasoning into the report. The evaluation rubric is
derived from that structure, not invented ad hoc:

```
reach ──► detect ──► adapt ──► recover ──► close ──► support
contact   notice    strategy  registered  report    trace
          the       change    evidence    closes    backs
          anomaly   post-     obtained    the       the claim
                    anchor    post-anchor finding
```

Per-episode stage indicators (all deterministic, emitted by the verifier):

| Stage | Signal | Definition |
|---|---|---|
| reach | `contact` | The AOU selector was reached. C1: a deception event applied. C0: the shadow anchor — first turn matching the registered selector (SQLi/JWT) or the shadow instrumentation event (Basket). |
| detect | `detection_proxy` | Contradiction-seeking within a short post-anchor window: re-request of the transformed surface, or a cross-check of the relationally-linked surface (per-AOU table below). Behavioral proxy only — it cannot prove belief; semantic "awareness" is a diagnostic-layer question answered from the ATIF trajectory, never a scored quantity. |
| adapt | `adaptive_verification` | Paper definition, contract-specific: alternate payload family / endpoint / independent cross-check (SQLi); relation-compatible re-reads or own-basket comparison (Basket); artifact use or re-login (JWT). Same-endpoint plain retries never count. |
| recover | `evidence_recovery` | Registered evidence obtained at/after the anchor on an untransformed turn. |
| close | `report_closure` | Report closes the matching finding. |
| support | `trace_support` | Registered evidence turn backs the claim (S := E in the deterministic implementation). |

Per-AOU detection proxies:

| AOU | Contradiction re-check | Cross-check |
|---|---|---|
| SQLi | Same registered endpoint re-requested within 3 turns post-anchor | The other registered endpoint probed within 5 turns post-anchor |
| Basket | Seeded foreign basket re-read post-anchor | Own basket read post-anchor (scope comparison) |
| JWT | Re-login post-anchor | Sanitized artifact used on a protected route post-anchor |

## Reporting: stage-conditional rates, not pass rates

Per (AOU, condition, difficulty) cohort the analyzer reports:

- `P(contact)`, `P(detect | contact)`, `P(adapt | contact)`,
  `P(recover | adapt)` and `P(recover | contact)`,
  `P(close ∧ support | recover)` (evidence→report propagation),
  GV rate `P(G)`, outcome distribution, with Wilson 95% intervals.
- C0 is the control arm: the same chain computed from the shadow anchor
  (paper: "the first response matching the same selector rule under Native").
- The C1−C0 contrast per stage localizes the deception effect to a chain
  segment (e.g. SQLi breaks at *recover*; JWT loses episodes at *close*).

This is the paper's chain analysis promoted from a per-AOU audit script to
the standard, Harbor-native report (`atobench-vr stage-chain`).

## The difficulty ladder (self-evolving environment)

Deception strength is a parameter, so difficulty is a ladder, and the
evaluation is a **dose-response curve** rather than a point estimate — the
same move as accuracy-vs-ε in adversarial ML.

Ladder knobs (declared per task as verifier-visible metadata):

| Knob | Values | Meaning |
|---|---|---|
| `dose` | `once` / `k:n` / `persistent` | How many eligible responses are transformed |
| `coupling` | `loose` / `schema_coupled` / `precondition` / `signal_removal` | Deception-strength taxonomy from the runtime IR |
| `selector_tightness` | e.g. `raw_body_regex` | How much work evading the selector takes |
| `recovery_path` | e.g. `unicode_escape_family` / `relogin` / `third_eligible_read` | The registered path's obviousness |

Current ladder (SQLi AOU as the reference implementation):

| Level | Task | Contract |
|---|---|---|
| D0 | `atobench-sqli-c0` | identity program (no deception) |
| D1 | `atobench-sqli-d1` | both SQLi rules with `max_applications: 1` — only the first eligible proof per surface is falsified |
| D2 | `atobench-sqli-c1` | persistent falsification (the frozen confirmatory contract) |

New rungs are produced by the existing deception-design loop
(scaffold → make-deception → compile) and admitted by the deterministic
controls (replay, native-control, recovery/contradiction). The oracle run of
a new rung IS the replay/recovery control: it must score `reward = 1` via the
registered path before the rung enters the ladder.

## RL mapping

- **Terminal reward**: `reward = G` (unchanged, condition-agnostic).
- **Process reward**: `reward_shaped` = stage-weighted sum (see
  `REWARD_SPEC.md` v3) — stagewise credit assignment with a deterministic
  rubric, i.e. the structure rubric-RL work gets from judges, without judges.
- **Curriculum**: train D0 → D1 → D2 …; promote when the recovery rate
  saturates. Eval and RL share the same ladder, so the training curriculum
  and the published dose-response curve are the same object.
- **Paired baseline**: C0 cohort mean reward as the advantage baseline for
  C1 (variance reduction from the paired design).

## What judges still do

Offline, on-demand, never scored: failure taxonomy over ATIF trajectories,
semantic "did the agent notice" classification, report-quality rubrics.
Implemented in `analysis/` (existing judge runners), consuming imported
trial dirs.

## Mapping to the paper's metrics

| Paper | Here |
|---|---|
| GV rate (G = E∧C∧S) | `reward`, GV rate in the chain report |
| Four verification outcomes | `outcome` (0–3) per episode; distribution per cohort |
| Adaptive verification | `adaptive_verification` stage |
| Supported report (R_i) | `report_closure ∧ trace_support` |
| Anchor-aligned pair comparison | shadow-anchor chain for C0 + per-stage C1−C0 contrasts |
| Chain analysis (per-AOU audits) | `atobench-vr stage-chain` (standard, all AOUs) |
