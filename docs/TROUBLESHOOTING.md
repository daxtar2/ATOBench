# Troubleshooting and FAQ

Symptom-first fixes for the most common failures. Campaign operation details
live in the [cross-model runbook](CROSS_MODEL_EVIDENCE_AOU_RUNBOOK.md); the
evaluation data flow is in [EVALUATION_WORKFLOW.md](EVALUATION_WORKFLOW.md).

## Campaign execution

**The wrapper refuses to run and prints a fail-closed message.**
`atobench-cross-model` fails closed when called with no arguments. This
guards against a multiline shell command whose trailing `\` was lost, which
would otherwise silently start the default three-model campaign. Keep the
backslash on every continued line, or write the command on one line.

**A worker target fails to start with a port conflict.** With
`--parallel-workers > 1`, workers use generated ports (base: target `3300`,
proxies `8200/8201`, one stride per worker). Before each start the runner
cleans up only its own campaign's compose project, so a remaining conflict is
held by something outside the campaign — pick unused
`--target-port-base` / `--proxy-port-base` / `--proxy-port-stride` values
before the campaign starts. Never change worker count or port bases
mid-campaign; that is a new execution environment.

**The container is "Up" but episodes fail to reach the target.** Docker's
`Started` state only means the container process exists. Juice Shop needs
additional time before its HTTP health URL passes, and a worker target is
considered started only after that health check — not after
`docker compose up -d` returns.

**Episodes are marked invalid with a route-attestation error.** The attested
provider model (from Claude Code `stream-json` assistant events) must match
the configured `agent.model`. If your cc-switch selector name differs from
the provider model name, pass explicit
`--model-selector <model>=<selector>` mappings. Mismatched or missing
attestation marks the episode invalid instead of pooling it into the wrong
model.

**Long episodes die with `SUBAGENT_TIMEOUT`.** Failed episodes are recorded,
their pair is marked failed, and the campaign continues — timeouts are
observations to audit, not stopping conditions. If a routed model is
repeatedly censored by the base timeout, raise it at the command level with
`--agent-timeout-multiplier` or `--agent-timeout-override <aou>=<seconds>`,
and report the changed timeout policy; do not silently pool campaigns with
different timeout policies.

**An interrupted run — do I restart from scratch?** No. Rerun the same
command with the same `--campaign-id`: committed slots are skipped by
default (`--skip-committed`), and failed or interrupted slots can be retried
unchanged. Do not change models, AOUs, rounds, selector mappings, AOU
configs, RuntimePrograms, prompts, or evaluators mid-campaign.

## Environment and configuration

**`claude` is not found, or judge/analysis stages fail immediately.** The
agent carrier and the analysis-layer judges both invoke the Claude Code CLI.
Install it, authenticate it, and make sure it is on `PATH`. Model routing
and credentials come from your own Claude Code configuration — nothing
provider-specific is stored in this repository.

**A frozen-suite, protocol, or platform command fails with a hash
mismatch.** That is the fail-closed design: suites, execution specs, and
platform configs pin SHA-256 hashes of their artifacts. If you intentionally
edited a pinned file, recompute the corresponding pins (see "Hash pinning"
in [ARCHITECTURE.md](ARCHITECTURE.md)); if you did not, restore the file.

**Does `max_tool_calls` cap my episode?** No. It is a prompt-level soft
budget inside the pentest harness, not a hard proxy cutoff. The proxy-logged
HTTP request count in `turns.jsonl` is the measured cost signal — report
those turns, not the configured budget.

## Analysis layer

**A data-dependent command refuses to run.** By design: the analysis layer
treats every non-temporary input as real data and requires explicit
`--allow-real-data`. Verify your inputs first, then pass the flag.

**Some analysis tests are skipped.** The four test files that consume the
frozen reference cohort skip themselves when the reference data is absent —
it is intentionally not shipped with the repository.

## FAQ

**Where are my results?**
Under `runtime/atobench/targets/juice-shop/experiments/<campaign_id>/`
(configs, logs, protocol locks, worker targets). What to run next is in
[EVALUATION_WORKFLOW.md](EVALUATION_WORKFLOW.md).

**Can I point ATOBench at another target?**
Yes — that is the intended extension path. See
[AOU_AUTHORING.md](AOU_AUTHORING.md) for target onboarding and AOU creation,
and [AOU_OPPORTUNITY_CONTRACT_STANDARD.md](AOU_OPPORTUNITY_CONTRACT_STANDARD.md)
for the validity requirements an AOU must satisfy before it is frozen.

**Why do Native (C0) runs go through the same proxy as ATO (C1) runs?**
So the only designed difference between the conditions is the registered
transformation itself. The proxy is part of the measurement apparatus, not
part of the perturbation.
