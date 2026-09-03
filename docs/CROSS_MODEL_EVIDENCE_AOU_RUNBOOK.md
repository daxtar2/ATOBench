# Cross-Model Evidence-AOU Runbook

Status: implementation-ready wrapper for the three frozen Juice Shop evidence AOUs.

## Scope

This runner compares Claude-Code-carried pentest agents routed to different model backends over the same frozen evidence AOU programs:

| AOU key | Unit ID | Protocol config |
|---|---|---|
| `sqli` | `M-C1-SQLI-EVIDENCE-CLOSURE` | `examples/experiments/protocol_v3/juice-shop-protocol-v3-sqli.yaml` |
| `basket` | `M-AUTHZ-BASKET-SCOPE-CLOSURE-PERSISTENT-K2` | `examples/experiments/protocol_v3/juice-shop-protocol-v3_1-basket.yaml` |
| `jwt` | `M-EXPOSURE-JWT-HASH-SUPPRESSION` | `examples/experiments/protocol_v3/juice-shop-protocol-v3_1-jwt.yaml` |

SQLi uses Protocol-v3 because that is the completed SQLi schedule. Basket and JWT use Protocol-v3.1 because v3.1 contains the result-independent assignment-slot commit repair.

## Command

The wrapper is fail-closed when called with no arguments. This prevents an
accidental multiline shell command without trailing `\` from silently starting
the default three-model campaign. Always keep the backslash at the end of each
continued line, or write the command on one line.

Dry-run one paired block for all three models and all three AOUs:

```bash
atobench/scripts/atobench-cross-model \
  --dry-run \
  --campaign-id cross_model_smoke_dryrun \
  --rounds 1 \
  --models glm-5.2 deepseek-v4-pro qwen3.7-max \
  --aous sqli basket jwt
```

Run the formal 15-pair campaign:

```bash
atobench/scripts/atobench-cross-model \
  --campaign-id cross_model_evidence_aou_v1 \
  --rounds 15 \
  --models glm-5.2 deepseek-v4-pro qwen3.7-max \
  --aous sqli basket jwt \
  --claude-effort high \
  --start-target \
  --parallel-workers 3
```

This schedules:

```text
3 models x 3 AOUs x 15 paired blocks x 2 conditions = 270 agent episodes
```

Run a smaller smoke first:

```bash
atobench/scripts/atobench-cross-model \
  --campaign-id cross_model_smoke_v1 \
  --rounds 1 \
  --models glm-5.2 deepseek-v4-pro qwen3.7-max \
  --aous sqli basket jwt \
  --claude-effort high \
  --start-target
```

Run the same smoke with one isolated worker per model:

```bash
atobench/scripts/atobench-cross-model \
  --campaign-id cross_model_smoke_parallel_v1 \
  --rounds 1 \
  --models glm-5.2 deepseek-v4-pro qwen3.7-max \
  --aous sqli basket jwt \
  --claude-effort high \
  --start-target \
  --parallel-workers 3
```

With `--parallel-workers > 1`, the wrapper uses model-level workers. Each
worker receives its own Juice Shop compose target, target-state contract,
target port, clean/deception proxy ports, and process logs. Within a worker,
the Protocol-v3 paired block order is still serial and frozen:

```text
model worker N:
  pair 1: C0 -> C1 or C1 -> C0 according to the frozen schedule
  pair 2: ...
```

The default generated worker ports are:

```text
worker 0: target 3300, proxies 8200/8201
worker 1: target 3301, proxies 8210/8211
worker 2: target 3302, proxies 8220/8221
```

When `--start-target` is used in parallel mode, each worker now starts its
generated compose target through:

```text
docker compose down --remove-orphans --volumes
docker compose up -d
wait for target health readiness
```

This cleanup is scoped to the generated worker compose file for the current
campaign. It prevents stale worker containers from earlier interrupted attempts
from holding ports such as `3300`, `3301`, or `3302`. It does not stop unrelated
Docker containers. If a port conflict remains after this pre-start cleanup, the
port is held by something outside the campaign worker target; choose unused
ports before starting a new campaign.

The readiness wait matters: Docker's `Started` state only means the container
process exists. Juice Shop can still need additional time before the HTTP health
URL returns the expected status. A worker target is considered started only
after the health URL passes, not immediately after `docker compose up -d`.

By default, the cross-model wrapper also stops generated worker targets at the
end of a non-dry-run campaign with the same scoped compose-down command. Use
`--keep-target-running` only when you intentionally want to inspect a worker
target after a run.

Override them only before a campaign starts:

```bash
--target-port-base 3400 --proxy-port-base 8300 --proxy-port-stride 10
```

Do not change worker count or port bases mid-campaign. That creates a new
execution environment and should be recorded as a new campaign.

## Agent Execution Controls

The cross-model runner freezes Claude Code effort as an explicit experimental
parameter. The paper-facing default is:

```text
--claude-effort high
```

Use the same effort for every model and every AOU in a campaign. `high` is the
recommended setting for the formal long-horizon pentest collection: it preserves
agent capability without the runtime/cost expansion of `xhigh` or `max`.

Claude Code currently exposes `--effort`, but does not expose temperature,
top-p, seed, or equivalent sampling controls through the CLI used by this
harness. The generated model provenance records these as
`not_exposed_by_claude_code_cli`; do not claim they were fixed.

The AOU configs also freeze `agent.max_tool_calls`, but this is a prompt-level
soft budget inside the pentest harness, not a hard proxy cutoff. The proxy logs
the actual HTTP request count as `turns.jsonl`; post-collection analysis should
report those measured turns as the budget/noise/cost signal. Do not interpret
`max_tool_calls` as the exact number of HTTP requests made by the agent.

Current formal settings are intentionally AOU-specific:

```text
SQLi:   max_tool_calls=40, timeout_s=1600
JWT:    max_tool_calls=40, timeout_s=1600
Basket: max_tool_calls=70, timeout_s=2000
```

Keep the same budget for all models and both C0/C1 conditions within an AOU.
Different AOUs may use different budgets because their workflows have different
expected lengths; compare conditions within AOU first, then macro-average
per-AOU results.

If a routed model is being collected in a separate campaign because the base
timeout repeatedly censors otherwise active episodes, freeze the changed timeout
policy in the command rather than editing generated YAML by hand. The wrapper
supports:

```text
--agent-timeout-multiplier 1.5
--agent-timeout-override sqli=2400
```

Overrides take precedence over the multiplier. Timeout changes are
execution-control changes: report them as such and do not silently pool them
with campaigns that used the base timeout policy. For Kimi-only diagnostic or
follow-up collection after a SQLi C1 timeout at 1600s, the recommended first
repair is `--agent-timeout-multiplier 1.5`, yielding SQLi/JWT 2400s and Basket
3000s while preserving the same prompt, AOU, dose, schedule, and effort.

Episode failures are not campaign-level stopping conditions by default. The
cross-model wrapper records failed episodes, marks the containing pair failed,
and continues with later pairs. This is required for long collection campaigns:
timeouts, provider errors, or fail-closed preflight errors are observations to
audit, not reasons to discard the remaining schedule. Use `--halt-on-error`
only for debugging.

Failed confirmatory slots are not committed by the underlying Protocol-v3
runner. They should be treated as invalid/missing for the primary effect
estimate and separately reported as timeout/infrastructure/cost outcomes. Do not
silently replace a failed slot inside the same campaign unless the protocol
explicitly defines a retry policy.

## Model Routing

By default, the wrapper uses the same string for both:

- `agent.model`: expected backend name checked against Claude Code route
  attestation (`stream-json` assistant model events, with `modelUsage`
  retained as a compatibility fallback);
- `agent.model_selector`: value passed to `claude --model`.

If cc-switch uses a different selector name from the attested provider model
name, pass explicit mappings:

```bash
atobench/scripts/atobench-cross-model \
  --campaign-id cross_model_evidence_aou_v1 \
  --rounds 5 \
  --models glm-5.2 deepseek-v4-pro qwen3.7-max \
  --model-selector glm-5.2=glm \
  --model-selector deepseek-v4-pro=deepseek \
  --model-selector qwen3.7-max=qwen \
  --aous sqli basket jwt \
  --start-target
```

If the attested provider model does not match `agent.model`, or if no route
evidence is emitted, the episode is treated as invalid rather than being
silently pooled into the wrong model.

## Outputs

For campaign `cross_model_evidence_aou_v1`, outputs are written under:

```text
targets/juice-shop/experiments/cross_model_evidence_aou_v1/
```

Important files:

```text
campaign_manifest.json
events.jsonl
configs/<model_slug>/<aou>.yaml
logs/<model_slug>/<aou>/
process_logs/<model_slug>/<aou>/
worker_targets/worker_<N>/docker-compose.yml
worker_targets/worker_<N>/target_state_contract.yaml
protocol_locks/<aou>_<model_slug>_lock.generated.yaml
```

Each generated config gets its own `experiment_id`, log directory, protocol lock, and deception workspace. This prevents model conditions from sharing manifests or assignment ledgers.

In parallel mode, each generated model protocol spec also freezes the worker
compose file and target-state contract used by that model. This is required
because Protocol-v3 reset/reseed/fingerprint reads the target URL and compose
file from the target-state contract, not only from the top-level experiment
config.

## Post-Collection

Run the artifact integrity gate first. This checks that the campaign is
complete and structurally usable, but it does not compute BRS outcomes or
adjudicate reports:

```bash
python3 -m atobench.experiment.cross_model_artifact_audit \
  runtime/atobench/targets/juice-shop/experiments/<campaign_id> \
  --json-output runtime/atobench/targets/juice-shop/experiments/<campaign_id>/artifact_audit.json \
  --md-output runtime/atobench/targets/juice-shop/experiments/<campaign_id>/ARTIFACT_AUDIT.md
```

Run this from the repository root (so the
`runtime/` directory is on `PYTHONPATH` and the `atobench` package is importable.

After the artifact audit passes, build the trace-derived BRS aggregate and
report-only blind adjudication packets:

```bash
python3 -m atobench.experiment.cross_model_post_collection \
  runtime/atobench/targets/juice-shop/experiments/<campaign_id> \
  --overwrite
```

This writes:

```text
artifact_audit.json
ARTIFACT_AUDIT.md
analysis/cross_model_brs_aggregate.json
analysis/CROSS_MODEL_BRS_AGGREGATE.md
analysis/blind_adjudication_packet/{sqli,basket,jwt}/public/
analysis/blind_adjudication_packet/{sqli,basket,jwt}/private_manifest.json
```

The BRS aggregate is trace-derived only. Report closure remains pending until
the public adjudication sheets are filled without reading private manifests,
runtime traces, normalized findings, model names, or conditions.

## Resume Semantics

The wrapper defaults to:

```text
--skip-committed
```

If a run is interrupted, rerun the same command with the same `--campaign-id`. Slots already committed by the underlying Protocol-v3 runner are skipped. Failed or interrupted slots are not committed by the underlying runner and can be retried unchanged.

Do not change these fields mid-campaign:

- `--models`
- `--aous`
- `--rounds`
- model selector mapping
- AOU configs
- RuntimePrograms
- prompts
- evaluators

Changing them creates a new campaign.

## Boundaries

This wrapper does not:

- change AOU mechanisms;
- retune prompts or RuntimePrograms;
- adjudicate reports;
- compute final cross-model statistics;
- run crAPI exploratory branches.

It only runs the already frozen Juice Shop evidence AOUs across model backends through the existing Claude Code pentest harness.
