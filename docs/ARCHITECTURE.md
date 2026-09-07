# ATOBench Architecture

This map connects the concepts in [CONCEPTS.md](CONCEPTS.md) to the code in
this repository. Paths are relative to the repository root.

```text
agent (claude -p) ──► mitmproxy addon ──► Juice Shop (docker)
                          │                     │
                          ▼                     ▼
                 RuntimeProgram rules     episode JSONL logs
                          │
                          ▼
              paired campaign output (per-episode dirs)
                          │
                          ▼
                    analysis layer  ──►  statistics / exports
```

## Two installable packages

- **`runtime/`** — package `atobench` (+ legacy `proxy` JSONL logger).
  Executes episodes: agent carrier, mitmproxy response transformation,
  Protocol-v3 paired campaign scheduling, frozen target suites.
- **`analysis/`** — package `atobench_vr`. Reconstructs evidence from episode
  outputs, runs blinded judges, computes resilience statistics, and exports
  learning data.

## Runtime: from agent prompt to paired campaign

| Concern | Module | Notes |
|---|---|---|
| Campaign scheduler (primary CLI) | `runtime/atobench/experiment/cross_model_protocol.py` | `atobench-cross-model` entry point. Builds the per-model Protocol-v3 config, assignment schedule, and provenance record; plans C0/C1 pairs; `--dry-run` materializes everything without Docker or model calls |
| Single-episode lifecycle | `runtime/atobench/experiment/cycle.py` | `ExperimentCycle`: start target → run agent → attribute events → evaluate |
| Episode CLI | `runtime/atobench/cli/main.py` | `atobench-experiment` entry point (`run-deception`, `run-clean`, ...) |
| Frozen suites | `runtime/atobench/experiment/suite.py` | Loads `benchmark_suites/*`; `validate_frozen_suite` fails closed on any hash mismatch in `freeze_manifest.json` |
| Deception design loop | `runtime/atobench/experiment/cycle.py` + `runtime/atobench/scaffold/` | `experiment` actions `scaffold` → `make-deception` (a Claude Code planning agent authors `deception_plan.yaml`) → `compile` (deterministic gate) → `freeze-suite`; the agent selects from the methodology corpus in `runtime/atobench/deception_frame/`; offline variant in `experiment/offline_construction.py` |
| Agent carrier | `runtime/atobench/agents/proxy_runner.py` | Spawns `claude -p`, parses the JSON stream, captures route attestation; `agents/claude_code.py` is the `BaseAgent` reference adapter |
| mitmproxy addon | `runtime/atobench/proxy/addon.py` | Reverse proxy in front of the target; applies the active RuntimeProgram |
| Rule engine | `runtime/atobench/proxy/rule_engine.py` | `RuntimePipeline` executes a compiled RuntimeProgram against one flow: selectors → transformers → application rules |
| Transformers | `runtime/atobench/proxy/transformers/` | The response-transform primitives an AOU program is built from |
| Program IR | `runtime/atobench/runtime_ir/` | RuntimeProgram data model and rendering; JSON schema in `runtime/atobench/schema/` |
| Target contract | `runtime/atobench/targets/juice-shop/` | `docker-compose.yml`, `protocol_v3/target_state_contract.yaml` (reset/health semantics), confirmatory suite under `benchmark_suites/juice-shop-confirmatory-v1/` with per-program `programs/*/runtime_program.yaml` |
| Experiment configs | `runtime/atobench/examples/experiments/protocol_v3/` | One YAML per AOU (`juice-shop-protocol-v3*-sqli/basket/jwt.yaml`) binding task, budget, subagent, and execution spec |
| Protocol spec | `runtime/atobench/experiment/protocol_v3/` | `PROTOCOL_V3_EXECUTION_SPEC.yaml` (schedule, prohibitions, provenance attestation) and historical `frozen_inputs/` collection manifests |

## Analysis: from episode output to resilience numbers

The CLI is `analysis/scripts/atobench-vr` (module `atobench_vr.cli`), with a
registry of stage commands in `atobench_vr/cli.py`. Typical order:

1. **Evidence reconstruction** — `atobench_vr/packets.py`,
   `atobench_vr/alignment.py`, `atobench_vr/facts.py` build source-linked,
   identity-blinded packets and deterministic evidence labels from the raw
   episode output. `atobench_vr/redaction.py` enforces the blinding boundary.
2. **Judging** — `atobench_vr/judge_runner.py` (plus preflight/resume/replay
   helpers) runs the blinded Agent-as-Judge and trajectory-judge tasks;
   `atobench_vr/semantic_runner.py` and `atobench_vr/semantic_validation.py`
   handle report-semantic matching.
3. **Episode and pair states** — `atobench_vr/episode_states.py` and
   `atobench_vr/pair_profiles.py` assemble per-episode verification outcomes
   and matched Native/ATO pair profiles.
4. **Statistics** — `atobench_vr/resilience_statistics.py` computes the
   paired estimates (with `statistics_freeze.py` freezing populations);
   `atobench_vr/final_qa.py` gates the release of results.
5. **Exports** — `atobench_vr/learning_data.py`,
   `learning_transitions.py`, `counterfactual_trajectories.py`,
   `transition_audit.py` produce training-style exports with provenance
   stripped; `atobench_vr/transition_audit.py` builds human-audit packets.

Data-dependent commands are **fail-closed**: they treat every non-temporary
input as real data and refuse to run without explicit
`--allow-real-data` authorization (`atobench_vr/common.py`).

## Guarded platform scaffold

`atobench_vr/platform.py` is a dry-run-first scaffold around the runtime: it
validates frozen component manifests (`analysis/config/platform/`), plans the
exact runner command, and only freezes plans — it never starts a target or
calls a model by itself. Real execution requires an explicit multi-flag
authorization chain. The freeze lock consumed by the tests lives at
`analysis/tests/fixtures/platform/juice-shop-vr-stage20-smoke.freeze.json`.

## Hash pinning (why hashes appear everywhere)

Three layers pin the tree, each with a different scope:

- `runtime/atobench/targets/juice-shop/benchmark_suites/*/freeze_manifest.json`
  — hashes every suite file; validated by `validate_frozen_suite`.
- `runtime/atobench/experiment/protocol_v3/frozen_inputs/*_collection_source.manifest.json`
  — historical provenance of the original frozen collection; recorded hashes
  describe that collection, not the live tree.
- `analysis/config/platform/**/*.json` → `frozen_artifacts` — sha256 pins on
  the runner wrapper, protocol module, example configs, runtime programs, and
  target contract; re-validated on every platform command.

If you edit any pinned file, recompute the corresponding pins — the code
fails closed otherwise by design.

## Quick verification paths

```bash
# materialize a full paired plan without Docker or a model call
atobench-cross-model --campaign-id demo --rounds 1 --models qwen3.7-plus \
  --model-selector qwen3.7-plus=opus --aous sqli --parallel-workers 1 --dry-run

# self-check of the analysis layer (reference dataset optional)
python3 analysis/scripts/atobench-vr doctor

# secret/private-path audit of the whole tree
python3 runtime/scripts/release_check.py .
```
