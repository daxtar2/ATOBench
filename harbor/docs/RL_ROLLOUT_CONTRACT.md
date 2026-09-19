# RL rollout contract & stability validation

How ATOBench-on-Harbor serves as an Agentic RL environment, and the stability
properties validated without running any training.

## Rollout = trial; batch = job

A rollout is one Harbor trial: fresh containers, the agent acting through the
proxy sidecar, then deterministic grading in a separate environment. A rollout
batch is a Harbor job — N trials of one task (or a dataset), with concurrency
controlled by `-n` and samples per task by `-k`:

```bash
# 8 rollouts of the SQLi ATO task, 4 at a time
harbor run -p harbor/tasks/atobench-sqli-c1 -a claude-code -m claude-sonnet-5 -k 8 -n 4
```

## Per-rollout data contract

Each trial directory carries everything a trainer or analysis loop needs:

| Path | Content | Role |
|---|---|---|
| `agent/trajectory.json` | ATIF v1.7 (messages, tool calls, observations, metrics) | policy trajectory |
| `verifier/reward.json` | `reward` (0/1 G chain), `reward_shaped` (dense), all process signals | reward |
| `artifacts/logs/proxy/turns.jsonl` | wire-level ground truth incl. `runtime_events` | offline analysis / filtering |
| `artifacts/app/report.txt` | agent's final report | report-closure auditing |

`harbor/scripts/export_rollout_batch.py` flattens a job directory into one
JSONL row per trial (`trajectory`, `reward`, `reward_shaped`, `metrics`,
`condition`, `wire_turns`), skipping incomplete trials fail-closed:

```bash
python3 harbor/scripts/export_rollout_batch.py jobs/<job> --out rollouts.jsonl
```

## Validated stability properties

All validations used the oracle agent (zero API cost; identical container
topology to real-agent runs):

1. **Concurrency & isolation.** 4 parallel trials of the heaviest topology
   (basket: main + proxy + juice-shop + seed sidecar per trial) completed in
   54 s vs ~40 s serial, 0 errors, and every trial produced the identical
   seeded fixture (baskets 6/7) — per-trial fresh containers make seeding
   deterministic by construction; no port, volume, or state leakage between
   trials.
2. **Reward determinism.** Two independent `harbor job regrade` passes over
   the same 4 recorded trials produced byte-identical `reward.json`. Rewards
   are pure functions of recorded artifacts — safe to mix into advantage
   estimation.
3. **Failure semantics (fault injection).**
   - Target killed during environment startup → trial errors cleanly
     (`RuntimeError: dependency failed to start`), no score recorded, nothing
     left running. Retry with `-r` treats these as infra flakes.
   - Target killed mid-agent-phase (slow-oracle variant) → trial completes,
     verifier scores `reward=0` / `unresolved_verification` on the partial
     wire log. Fail closed: infra failure can never masquerade as success.
4. **Throughput (this 2-core dev VM, vfs Docker).** ~4.4 oracle trials/min
   for the 4-container basket topology; SQLi/JWT (3 containers) are lighter.
   Real-agent runs are agent-latency-bound, so local Docker sustains far more
   concurrent trials than one API key's rate limit; scale-out paths are the
   cloud sandbox providers (`--env daytona|modal|...`, compose via DinD).

## Retry policy for real-agent campaigns

Two transient first-turn failure classes were observed in the real-agent
batch, both recoverable by retrying the trial: provider-side
`Content block not found`, and a nondeterministic safety-classifier refusal
on the pentest instruction (~1/6 trials). Configure campaigns with
`-r 2 --retry-include <those exception types>` rather than discarding the
samples.

## What is NOT yet load-tested

- Cloud sandbox providers (Daytona/Modal DinD compose path) — local Docker
  and EC2-style hosts are the validated path.
- Hundreds of concurrent trials on one host (only 4-way concurrency tested).
- Multi-task dataset runs (`dataset.toml` packaging is a follow-up).
