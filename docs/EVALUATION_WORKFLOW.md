# Evaluation workflow

What happens to the data after a paired campaign finishes. Three layers,
each strictly downstream of the previous one:

1. **Artifact integrity gate** (runtime) — is the campaign complete and
   structurally usable?
2. **Pair-level evaluation** (runtime `eval` layer, deterministic) — what did
   each agent do, and did verification hold up in this pair?
3. **Campaign-level analysis** (`analysis` package `atobench_vr`) — blinded
   judging, pair profiles, resilience statistics, learning-data exports.

```text
paired campaign output
        │
        ▼
 [1] artifact audit            structural completeness gate (no model calls)
        │
        ▼
 [2] pair-level evaluation     deterministic: action traces, behavior audits,
        │                      pentest-effect metrics      (atobench-experiment)
        ▼
 [3] campaign analysis         reconstruction → blinded judges → pair profiles
        │                      → resilience statistics → exports (atobench-vr)
        ▼
 resilience numbers + learning-data exports
```

## Input contract

Every layer consumes the campaign output directory written by
`atobench-cross-model`
(`runtime/atobench/targets/juice-shop/experiments/<campaign_id>/`; see the
runbook's *Outputs* section for the file layout). A frozen campaign must let
the analysis census resolve, at minimum: the campaign manifest, pair/episode
assignment, episode lifecycle and terminal status, model/AOU/condition/
unit-block fields, the final report and its hash, target-side event files
with source hashes, and locatable episode identity inside the agent sessions.
The full binding between runtime outputs and the analysis layer is defined in
`analysis/docs/AOU_RUNTIME_INTERFACE.md`.

## Layer 1 — artifact integrity gate

Run this first. It checks that the campaign is complete and structurally
usable, but it does not compute BRS outcomes or adjudicate reports:

```bash
python3 -m atobench.experiment.cross_model_artifact_audit \
  runtime/atobench/targets/juice-shop/experiments/<campaign_id> \
  --json-output runtime/atobench/targets/juice-shop/experiments/<campaign_id>/artifact_audit.json \
  --md-output runtime/atobench/targets/juice-shop/experiments/<campaign_id>/ARTIFACT_AUDIT.md
```

## Layer 2 — pair-level evaluation (deterministic)

No model is called anywhere in this layer; every command is reproducible
directly from episode artifacts. Run `atobench-experiment <command> --help`
for exact arguments.

| Command | Input → output | Purpose |
|---|---|---|
| `extract-action-trace` | `<episode>/turns.jsonl` → `agent_action_trace.jsonl` | Redaction-safe HTTP action trace |
| `normalize-report` | raw final report → `normalized_findings` | Deterministic report normalization |
| `audit-behavior` | explicit JSON pair manifest → `behavior_audit.json` | Paired C0/C1 behavior audit over action traces |
| `pentest-effect` | paired turn traces + reports → effect metrics | Clean-relative deception effect |
| `evaluate-pair` | clean run dir + deception run dir → `pentest_effect.json` | End-to-end pair workflow (infers workspace artifacts) |
| `validate-behavior-profiles`, `audit-behavior-profiles` | profile sidecar + paired campaign traces | Profile contact and causal identifiability |

The metrics are defined in
`runtime/atobench/eval/PENTEST_EFFECT_METRICS.md`.

## Layer 3 — campaign analysis (`atobench_vr`)

The analysis layer runs a staged pipeline over the whole campaign. Stages
`01`–`15` are dispatched through `atobench-vr stage <NN>`; the later stages
are exposed as named export commands and standalone scripts under
`analysis/scripts/`. Typical order (one row ≈ one stage):

| Stage | Command / script | Purpose |
|---|---|---|
| 01 | `stage 01` (census) | episode/session/event census |
| 02–03 | `stage 02`, `stage 03` | session flatten, redaction |
| 04 | `stage 04` / `align` (+ `align-verify`, `align-deterministic`) | Claude↔HTTP alignment |
| 05 | `stage 05` / `build-facts` | episode fact registry |
| 06 | `stage 06` / `extract-claims` | report claim atoms |
| 07–08 | `stage 07`, `stage 08` / `build-packets`, `run-judges` | identity-blinded judge packets and judge runs |
| 09–10 | adjudication + `validate-judges` | judgment adjudication and validation |
| 11–12 | `build-episode-states`, `build-pairs` | episode verification outcomes, Native/ATO pair profiles |
| 12c–12j | `build-semantic-packets`, `run-semantic-matchers`, `validate-semantic-matches` | registered-finding semantic closure |
| 13 | `stage 13` / `freeze-statistics` | paired resilience estimates on frozen populations |
| 14–15 | `export-results`, `qa` | result facts + release QA gate |
| 16–17 | `analysis/scripts/16_plot_…`, `17_build_…` | figures, trajectory dynamics |
| 18–20 | `export-learning-data`, `export-learning-transitions`, `build-transition-audit`, `export-counterfactual-trajectories` | learning-data exports |

Two invariants hold across the whole layer:

- **Identity blinding.** Judge and semantic-matcher packets never expose
  model names, conditions, or pair identities; the blinded layers are the
  only components that see report content.
- **Fail-closed real-data handling.** Data-dependent commands treat every
  non-temporary input as real data and refuse to run without explicit
  `--allow-real-data` authorization.

The model-backed stages — `run-judges` (07–08) and the semantic matchers
(12c–12j) — invoke the Claude Code CLI (`claude -p`) with the blinded
packets, so they need a working Claude Code installation; every other stage
is local and deterministic. Each export writes a dataset manifest, dataset
card, quality summary, and export audit alongside its JSONL records.

For what the exports contain (and what they deliberately do not), see
[analysis/README.md](../analysis/README.md) and
[analysis/docs/PROJECT_VISION_AND_LEARNING_DATA.md](../analysis/docs/PROJECT_VISION_AND_LEARNING_DATA.md).
