# Designing AOUs: the Agent-Driven Loop and the Manual Path

ATOBench ships two ways to produce an AOU, and they have different audiences:

- **Path A — agent-driven design loop (the primary, shipped workflow).**
  A *planning agent* authors the AOU. You provide the target and the
  experiment intent; the framework scaffolds a workspace and hands a
  Claude Code session a bounded task: consult the deception-methodology
  corpus, select primitives, and write `deception_plan.yaml`. Python owns
  every deterministic gate around that step. This is how the bundled AOU
  suites were produced.
- **Path B — manual authoring.** An engineer writes the RuntimePrograms and
  suite files directly. Useful for small edits, tooling, or runs without an
  agent carrier.

Both paths converge on the same artifacts: a frozen benchmark suite whose
C1 programs the campaign scheduler executes.

Design-level validity requirements (what makes an AOU *scientifically*
usable — opportunity/effect separation, prompt-specificity levels, mandatory
unaffected recovery paths) are in
[AOU_OPPORTUNITY_CONTRACT_STANDARD.md](AOU_OPPORTUNITY_CONTRACT_STANDARD.md).
Concepts are in [CONCEPTS.md](CONCEPTS.md).

## Onboarding your own target (both paths)

Everything the runner needs from a target lives in one directory under
`runtime/atobench/targets/<name>/`. Juice Shop is the reference
implementation; copy its shape:

```text
targets/<name>/
├── docker-compose.yml                  # pinned image digest, one service
├── protocol_v3/
│   └── target_state_contract.yaml      # reset / readiness / seed contract
└── benchmark_suites/
    └── <suite-id>/                     # produced by either path
```

### Target state contract

`protocol_v3/target_state_contract.yaml` is what makes episodes reproducible
and pairs comparable. It declares (see
`targets/juice-shop/protocol_v3/target_state_contract.yaml` for a full
example):

| Section | Purpose |
|---|---|
| `target` | name, version, URL, compose file, container name, **image digest** (pin it — runs are only comparable against the same binary target) |
| `reset` | the compose down/up arguments used to restore pristine state between episodes |
| `readiness` | health timeout, poll interval, and the probe paths the runner polls before an episode starts |
| `seed` | deterministic identities created before collection, a canary record, and a pollution record — pairs assume the same seeded state |
| `expected_probes` | expected status codes for own/foreign/unauthenticated probes, used to verify the vulnerable behavior is present |

An experiment config references the target by path, so no registry edit is
needed for the runtime itself:

```yaml
target:
  name: my-target
  target_dir: ../../../targets/my-target
  target_url: http://127.0.0.1:3000
  health_url: http://127.0.0.1:3000/
  health_expected_status: 200
  compose_file: ../../../targets/my-target/docker-compose.yml
  discovery_paths: [/]        # the surface shown to agents and planners
```

Keep the target intentionally vulnerable and **isolated** — the agent will
attack it with real tool calls. Only point ATOBench at systems you own or are
explicitly authorized to test.

## The deception-methodology corpus

The planning agent does not improvise. It selects from a shipped, curated
corpus under `runtime/atobench/deception_frame/` and
`runtime/atobench/primitives/`:

| File | Role |
|---|---|
| `deception_frame/primitive_index.yaml` + `primitive_recipes.yaml` | The machine-readable catalog the planner **must** select from (the 64-entry recipe catalog); maps each primitive to surfaces, loaders, hooks, selectors, transforms |
| `deception_frame/primitive_wiki_v2.md` | Per-primitive cognitive-mechanism wiki: the agent prior each primitive exploits and how |
| `deception_frame/deception_tricks_catalog_final.md` | Trick catalog with prerequisites and coupling notes |
| `deception_frame/deception_framework_v3.md` | The overall deception framework |
| `deception_frame/realism_constraints_v2.yaml` | Realism constraints a plan must respect |
| `primitives/library.yaml` | Primitive library |
| `scaffold/ADAPTIVE_GENERATOR_DESIGN*.md`, `scaffold/MULTI_AGENT_PIPELINE*.md`, `scaffold/MULTI_LLM_GENERATOR_BENCHMARK_DESIGN.md` | Design docs for the generator pipelines that surround the planner |
| `deception_frame/build_primitive_index.py` | Rebuilds the primitive index |

## Path A — the agent-driven design loop

The loop is a sequence of `experiment` actions driven by one experiment
config (create it as in [the manual path](#path-b--manual-authoring) Step 5 —
the planner needs `target`, `runtime`, `agent`, and `subagents` sections):

```text
run-clean ──► scaffold ──► make-deception ──► compile ──► run-deception / paired campaign
 (baseline)    (workspace)  (agent plans)     (gate)         freeze-suite ──► validate-suite
```

```bash
cd runtime
atobench-experiment experiment run-clean      --config examples/experiments/protocol_v3/my-config.yaml
atobench-experiment experiment make-deception --config examples/experiments/protocol_v3/my-config.yaml
atobench-experiment experiment compile        --config examples/experiments/protocol_v3/my-config.yaml
atobench-experiment experiment freeze-suite   --config examples/experiments/protocol_v3/my-config.yaml --suite-id my-target-v1
atobench-experiment experiment validate-suite --config examples/experiments/protocol_v3/my-config.yaml --suite-id my-target-v1
```

(`make-deception` runs `scaffold` itself; the explicit `scaffold` action
re-builds the workspace after a new clean run.)

### What each stage does

- **`run-clean`** — records a native baseline episode. Its trajectory is the
  evidence the planner conditions on.
- **`scaffold`** — materializes the planner workspace under the target's
  `scaffold_work/`: `target_profile.yaml`, `endpoint_inventory.jsonl`,
  a `trajectory_profile.json`, and prompt files including
  `planner_prompt.md` — the full task contract the planner executes
  (`experiment/scaffold.py`).
- **`make-deception`** — writes `make_deception_prompt.md` and launches
  Claude Code (`claude -p` with `Agent:*` plus a read/write/python
  allow-list, `experiment/cycle.py`). The outer session must start exactly
  one subagent, **`deception-planner`**, which reads the profile, inventory,
  and planner prompt, selects primitives **from
  `primitive_index.yaml`/`primitive_recipes.yaml`**, writes
  `deception_plan.yaml`, schema-validates it
  (`atobench.schema.loader.validate_deception_plan`), and leaves planner
  notes. The wrapper then compiles the plan.
- **`compile`** — the deterministic gate: compiles `deception_plan.yaml`
  into a RuntimeProgram workspace, refusing invalid plans. Nothing reaches
  the proxy without passing it.
- **`freeze-suite`** / **`validate-suite`** — freeze the compiled workspace
  into a hash-locked benchmark suite (`experiment/suite.py:
  freeze_suite_from_workspace`) and fail-closed validate it.

### Planning modes (what the planner is allowed to know)

Set `subagents.planning_mode` in the experiment config; the mode changes the
planner's inputs and is enforced after planning (`cycle.py` rejects plans
that leak forbidden anchors):

| Mode | Planner sees | Use |
|---|---|---|
| `trajectory_aware` | Clean-run trajectory profile; injections must carry trajectory anchors | Adaptive/trajectory-conditioned AOU design |
| `static_inventory` | Endpoint inventory only; trajectory anchors forbidden | Inventory-grounded design without clean-trajectory conditioning |
| `non_contact` | Inventory only; bindings restricted to predeclared unvisited control paths | Control conditions |

### Notes on the planner agent

- The loop expects a Claude Code subagent named `deception-planner`. The
  persona definition is operator-side (it lived in the maintainer's editor
  configuration, which is not part of this release); the scaffold-written
  `planner_prompt.md` carries the complete task contract, so a thin agent
  definition that follows that prompt is sufficient. Any capable coding
  agent that honors the same prompt works as a drop-in planner.
- Hard rules baked into the prompt: select primitives from the index/recipes
  only (never legacy transformer classes), prefer explicit `bindings[]`,
  do not run episodes or normalize reports, validate the plan schema before
  finishing.

### Offline construction (design-time, no episodes)

For building candidate suites without recording a clean run first:

```bash
atobench-experiment experiment construct-offline --config ... \
  --surface-inventory surfaces.jsonl --ground-truth cards.yaml \
  --primitive-index ... --primitive-recipes ... --suite-id my-offline-v1
atobench-experiment experiment validate-offline --config ... --suite-id my-offline-v1
atobench-experiment experiment review-offline   --config ... --suite-id my-offline-v1
```

`construct-offline` combines a surface inventory, the primitive index and
recipes, and ground-truth cards into a candidate suite;
`validate-offline`/`review-offline` gate it; `materialize-offline-suite`,
`replay-offline-suite`, and `freeze-offline-suite` carry it to a frozen
suite (`experiment/offline_construction.py`).

## Path B — manual authoring

Skip the agent and write the artifacts directly. This is the reference for
what Path A produces.

### Step 1 — Design the unit

Fix the contract before touching code (full standard in the contract
document):

- **Selector** — which requests are eligible (path, method, request-shape
  predicate). Selectors must match on the *request*, never on model state.
- **Transform** — how the response is rewritten, without touching the
  target's actual state.
- **Application rule** — when the transform fires. Rules that leave a
  recovery path are what make the AOU diagnostic.
- **Unaffected path** — at least one native traffic path that can confirm or
  contradict the changed observation.
- **Ground truth** — the registered facts, trajectory events, and report
  claims used to score the response.

### Step 2 — Write the RuntimeProgram pair

```text
benchmark_suites/<suite-id>/programs/
├── c0_identity/runtime_program.yaml        # native passthrough baseline
└── <case-name>/runtime_program.yaml        # the C1 transformation
```

The schema is `runtime/atobench/schema/runtime_program.json`. A C1 rule
(abridged from the shipped SQLi case):

```yaml
schema_version: 0.2.0
program_id: rp_<12 hex>            # derived from suite_id + key, see note
episode_id: ep_<case>
task_id: T3
target: {base_url: http://127.0.0.1:3000}
rules:
- rule_id: rule_<descriptive-name>
  layer: deception_perturbation
  primitive: validation_error_schema_hallucination   # transformer name
  match:
    path_regex: ^/rest/user/login/?$
    methods: [POST]
    request_body_match: "(?i)('|%27|\\bor\\b|--)"
  effects:
  - operation: synthetic_response
    status: 401
```

Transform primitives live in `runtime/atobench/proxy/transformers/`
(registered in a registry consumed by
`runtime/atobench/proxy/rule_engine.py`). Prefer existing primitives; add a
transformer module only when nothing covers the change.

Note: `program_id` values are derived hashes (`rp_` + sha256 over
`suite_id:key`), not free text — when you rename a suite or key, regenerate
the ids and update the manifest and program files together.

### Step 3 — Register the cases in the suite manifest

`benchmark_suites/<suite-id>/suite_manifest.yaml` maps each condition to a
program (`standard_conditions:` — see the shipped suite for the shape) and
records `case_count` / `runtime_rule_count`.

### Step 4 — Freeze the suite (hash-locked, fail-closed)

The suite is pinned by `freeze_manifest.json` (sha256 over every file); the
runner refuses a stale suite:

```python
from atobench.experiment.suite import validate_frozen_suite
print(validate_frozen_suite("runtime/atobench/targets/juice-shop/benchmark_suites/<suite-id>"))
# {'status': 'valid', 'mismatches': [], 'missing': [], 'runtime_errors': []}
```

Rebuild the manifest after any suite edit (hash every file except
`freeze_manifest.json` itself); helpers live in
`runtime/atobench/experiment/suite.py`.

### Step 5 — Create the experiment config

One YAML per AOU under
`runtime/atobench/examples/experiments/protocol_v3/`. The SQLi example is
the annotated template:

```yaml
experiment_id: juice_shop_protocol_v3_sqli
target:      {…}                  # your target, see above
runtime:     {deception_id, task, episode ids, proxy ports, log_dir}
agent:       {driver, subagent_type, timeout_s, max_tool_calls, model}
subagents:   {executor: prompt_files, planning_mode: static_inventory, …}
benchmark_suite:
  suite_id: <suite-id>
  suite_dir: <path>
  c0_runtime_program: <path>      # relative paths are resolved
  c1_runtime_program: <path>
  active_program: <case-name>
  enforce_frozen: true
protocol_v3:
  unit_id: M-C1-<YOUR-UNIT-NAME>
  execution_spec: ../../../experiment/protocol_v3/PROTOCOL_V3_EXECUTION_SPEC.yaml
  target_state_contract: <path>
```

### Step 6 — Register the AOU key with the campaign runner

The paired campaign scheduler keeps a registry in
`runtime/atobench/experiment/cross_model_protocol.py`:

```python
AOUS: dict[str, AouConfig] = {
    "sqli": AouConfig(key="sqli", unit_id=…, block_prefix="S",
                      base_config=REPO_ROOT / "examples" / "experiments" / …,
                      preferred_protocol="protocol_v3"),
    # add yours:
    "myaou": AouConfig(key="myaou", unit_id="M-C1-MYAOU",
                       block_prefix="M",
                       base_config=REPO_ROOT / "examples" / "experiments" / "protocol_v3" / "my-target-protocol-v3-myaou.yaml",
                       preferred_protocol="protocol_v3"),
}
```

Once registered, the key flows everywhere automatically: `--aous myaou`
validates (choices are `sorted(AOUS)`), scheduling uses the `block_prefix`,
and `--agent-timeout-override myaou=2400` works.

### Step 7 — Ground-truth cards

Scoring uses ground-truth cards keyed by target name, at
`runtime/atobench/experiment/ground_truth/<target>_ground_truth_cards.yaml`
(see `juice_shop_ground_truth_cards.yaml`); the suite tooling copies the
target's cards into the suite at creation
(`suite.py:_default_ground_truth_path`).

### Step 8 — (Optional) Analysis-layer registration

To analyze the new AOU through the platform scaffold, add a bundle at
`analysis/config/platform/aous/<id>.json` — with sha256 `frozen_artifacts`
pins on the experiment YAML and the C1 runtime program — and register it in
`analysis/config/platform/registry.json`. Pins are re-validated on every
platform command. The built-in judge/statistics tooling is calibrated on the
three shipped AOUs (their keys appear in analysis configs and eval scripts);
treat a new AOU as runtime-complete first, then extend analysis configs
deliberately.

### Step 9 — Validate without a model call

```bash
atobench-cross-model \
  --campaign-id myaou-dryrun \
  --rounds 1 \
  --models your-model \
  --model-selector your-model=opus \
  --aous myaou \
  --parallel-workers 1 \
  --dry-run
```

`--dry-run` materializes the per-model configs, assignment schedule, and
provenance record — no Docker, no model calls. Before counted collection,
also satisfy the contract-standard gates (replay validation, native
controls, recovery/contradiction control) from
[AOU_OPPORTUNITY_CONTRACT_STANDARD.md](AOU_OPPORTUNITY_CONTRACT_STANDARD.md).

## Touchpoint summary

| Touchpoint | File |
|---|---|
| Design loop actions | `runtime/atobench/experiment/cycle.py` (`scaffold`, `make-deception`, `compile`, `freeze-suite`, …) |
| Planner workspace + prompts | `runtime/atobench/experiment/scaffold.py` → target `scaffold_work/` |
| Methodology corpus | `runtime/atobench/deception_frame/`, `runtime/atobench/primitives/library.yaml` |
| Offline construction | `runtime/atobench/experiment/offline_construction.py` |
| C0/C1 programs | `targets/<name>/benchmark_suites/<suite>/programs/*/runtime_program.yaml` |
| Suite manifest + freeze lock | `…/<suite>/suite_manifest.yaml`, `freeze_manifest.json` |
| Experiment config | `runtime/atobench/examples/experiments/protocol_v3/<name>.yaml` |
| AOU registry | `runtime/atobench/experiment/cross_model_protocol.py` (`AOUS`) |
| Ground truth | `runtime/atobench/experiment/ground_truth/<target>_ground_truth_cards.yaml` |
| Target contract | `targets/<name>/protocol_v3/target_state_contract.yaml` |
| Analysis registration (optional) | `analysis/config/platform/{aous,registry.json}` |
| Transform primitives | `runtime/atobench/proxy/transformers/` |
