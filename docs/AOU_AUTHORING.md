# Authoring AOUs and Onboarding Your Own Target

This guide covers the two extension workflows of the runtime:

1. **Bring your own target** — run ATOBench against a target other than the
   bundled OWASP Juice Shop reference.
2. **Author a new AOU** — define a new registered response transformation
   (Adversarial Observation Unit) and make it schedulable by the paired
   campaign runner.

Design-level requirements (what makes a *valid* AOU: opportunity/effect
separation, prompt-specificity levels, mandatory unaffected paths) are in
[AOU_OPPORTUNITY_CONTRACT_STANDARD.md](AOU_OPPORTUNITY_CONTRACT_STANDARD.md);
this page is the engineering walkthrough. Concepts are in
[CONCEPTS.md](CONCEPTS.md).

## Part 1 — Bring your own target

Everything the runner needs from a target lives in one directory under
`runtime/atobench/targets/<name>/`. Juice Shop is the reference
implementation; copy its shape:

```text
targets/<name>/
├── docker-compose.yml                  # pinned image digest, one service
├── protocol_v3/
│   └── target_state_contract.yaml      # reset / readiness / seed contract
└── benchmark_suites/
    └── <suite-id>/                     # created per AOU work, see Part 2
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

### Pointing an experiment at your target

An experiment config (next section) references the target by path, so no
registry edit is needed for the runtime itself:

```yaml
target:
  name: my-target
  target_dir: ../../../targets/my-target
  target_url: http://127.0.0.1:3000
  health_url: http://127.0.0.1:3000/
  health_expected_status: 200
  compose_file: ../../../targets/my-target/docker-compose.yml
  discovery_paths: [/]        # shown to the agent as the known surface
```

Keep the target intentionally vulnerable and **isolated** — the agent will
attack it with real tool calls. Only point ATOBench at systems you own or are
explicitly authorized to test.

## Part 2 — Author a new AOU

An AOU is a pair of RuntimePrograms (C0 passthrough and C1 transformation)
plus the glue that makes the runner schedule it. The confirmatory suite is
the worked example: `targets/juice-shop/benchmark_suites/juice-shop-confirmatory-v1/`.

### Step 1 — Design the unit

Fix the three components before touching code (full standard in the contract
document):

- **Selector** — which requests are eligible (path, method, request-shape
  predicate). Selectors must match on the *request*, never on model state.
- **Transform** — how the response is rewritten. The change must preserve
  HTTP/schema plausibility where the contract requires it and must not touch
  the target's actual state.
- **Application rule** — when the transform fires (first match only, every
  match, all-but-one…). Rules that leave a recovery path are what make the
  AOU diagnostic.
- **Unaffected path** — at least one native traffic path that can confirm or
  contradict the changed observation. Without it the agent cannot verify,
  and the episode measures nothing.
- **Ground truth** — the registered security facts and the trajectory
  events/report claims used to score the agent's response.

### Step 2 — Write the RuntimeProgram pair

One program directory per case under your suite:

```text
benchmark_suites/<suite-id>/programs/
├── c0_identity/runtime_program.yaml        # native passthrough baseline
└── <case-name>/runtime_program.yaml        # the C1 transformation
```

A program binds rules to selectors and effects; the schema is
`runtime/atobench/schema/runtime_program.json`. A C1 rule looks like this
(abridged from `programs/evidence_sqli_closure/runtime_program.yaml`):

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
    request_body_match: "(?i)('|%27|\\bor\\b|--)"    # request-shape selector
  effects:
  - operation: synthetic_response
    status: 401
    # ... response body template
```

Transform primitives live in `runtime/atobench/proxy/transformers/` (each
exports into a registry consumed by the rule engine,
`runtime/atobench/proxy/rule_engine.py`). Compose your effect from existing
primitives first; add a new transformer module only when no primitive covers
the change.

Note: `program_id` values are derived hashes (`rp_` + sha256 over
`suite_id:key`), not free text — when you rename a suite or key, regenerate
the ids and update both the manifest and the program files together.

### Step 3 — Register the cases in the suite manifest

`benchmark_suites/<suite-id>/suite_manifest.yaml` maps each condition to a
program:

```yaml
schema_version: atobench.benchmark_suite.v1
suite_id: <suite-id>
target_name: <target-name>
target_url: http://127.0.0.1:3000
standard_conditions:
  C0: programs/c0_identity/runtime_program.yaml
  YOUR_CASE: programs/<case-name>/runtime_program.yaml
case_count: 2
runtime_rule_count: <total rules>
```

### Step 4 — Freeze the suite (hash-locked, fail-closed)

The suite directory is pinned by `freeze_manifest.json` (sha256 over every
file). The runner refuses to materialize a campaign if anything is stale:

```python
from atobench.experiment.suite import validate_frozen_suite
print(validate_frozen_suite("runtime/atobench/targets/juice-shop/benchmark_suites/<suite-id>"))
# {'status': 'valid', 'mismatches': [], 'missing': [], 'runtime_errors': []}
```

After editing any file in the suite (including the renames you just made),
rebuild the manifest — hash every file except `freeze_manifest.json` itself,
and recompute `program_id`s from the new suite id. Suite creation/freezing
helpers live in `runtime/atobench/experiment/suite.py`
(`freeze_suite_from_workspace`, `validate_frozen_suite`).

### Step 5 — Create the experiment config

One YAML per AOU under
`runtime/atobench/examples/experiments/protocol_v3/`. The SQLi example is the
annotated template:

```yaml
experiment_id: juice_shop_protocol_v3_sqli
target:      {…}                  # your target, see Part 1
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

Scoring uses ground-truth cards keyed by target name. The convention is
`runtime/atobench/experiment/ground_truth/<target>_ground_truth_cards.yaml`
(see `juice_shop_ground_truth_cards.yaml`); the suite tooling copies the
target's cards into the suite at creation
(`suite.py:_default_ground_truth_path`). Register the observable facts,
trajectory events, and report claims your AOU is scored against.

### Step 8 — (Optional) Analysis-layer registration

To analyze the new AOU through the platform scaffold, add a bundle at
`analysis/config/platform/aous/<id>.json` — with sha256 `frozen_artifacts`
pins on the experiment YAML and the C1 runtime program — and register it in
`analysis/config/platform/registry.json`. Pins are re-validated on every
platform command; recompute them whenever the pinned files change. The
built-in judge/statistics tooling is calibrated on the three shipped AOUs
(their keys appear in analysis configs and eval scripts); treat a new AOU as
runtime-complete first, then extend analysis configs deliberately.

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
also satisfy the contract-standard gates (replay validation, native controls,
recovery/contradiction control) from
[AOU_OPPORTUNITY_CONTRACT_STANDARD.md](AOU_OPPORTUNITY_CONTRACT_STANDARD.md).

## Touchpoint summary

| Touchpoint | File |
|---|---|
| C0/C1 programs | `targets/<name>/benchmark_suites/<suite>/programs/*/runtime_program.yaml` |
| Suite manifest + freeze lock | `…/<suite>/suite_manifest.yaml`, `freeze_manifest.json` |
| Experiment config | `runtime/atobench/examples/experiments/protocol_v3/<name>.yaml` |
| AOU registry | `runtime/atobench/experiment/cross_model_protocol.py` (`AOUS`) |
| Ground truth | `runtime/atobench/experiment/ground_truth/<target>_ground_truth_cards.yaml` |
| Target contract | `targets/<name>/protocol_v3/target_state_contract.yaml` |
| Analysis registration (optional) | `analysis/config/platform/{aous,registry.json}` |
| Transform primitives | `runtime/atobench/proxy/transformers/` |
