# Real-agent validation runbook

Prerequisites: `ANTHROPIC_API_KEY` (and `ANTHROPIC_BASE_URL` if using a
gateway provider) available in the environment. On Cursor cloud agents, add
them under **Cloud Agents > Secrets** — they are injected into newly started
VMs only.

On a fresh cloud-agent VM, bootstrap Docker + harbor first:

```bash
sudo bash harbor/scripts/dev_vm_bootstrap.sh
export PATH="$HOME/.local/bin:$PATH"
```

## Paired run (SQLi first — the strongest-effect contract in the paper)

```bash
harbor run -p harbor/tasks/atobench-sqli-c1 -a claude-code -m anthropic/claude-sonnet-5
harbor run -p harbor/tasks/atobench-sqli-c0 -a claude-code -m anthropic/claude-sonnet-5
```

Then the other two AOUs:

```bash
harbor run -p harbor/tasks/atobench-jwt-c1    -a claude-code -m anthropic/claude-sonnet-5
harbor run -p harbor/tasks/atobench-jwt-c0    -a claude-code -m anthropic/claude-sonnet-5
harbor run -p harbor/tasks/atobench-basket-c1 -a claude-code -m anthropic/claude-sonnet-5
harbor run -p harbor/tasks/atobench-basket-c0 -a claude-code -m anthropic/claude-sonnet-5
```

## What to check per trial

1. `verifier/reward.json` — `reward`, `outcome` (3 = grounded), and the
   process signals (`contact`, `adaptive_verification`, `evidence_recovery`,
   `evidence_via_registered_path`).
2. `agent/trajectory.json` — ATIF v1.7 agent-side trajectory (confirmed
   landing with claude-code 2.1.278).
3. `artifacts/logs/proxy/turns.jsonl` — wire-level ground truth.
4. Compare against the oracle baseline for the same task (see
   `harbor/README.md` validation matrix): a real agent may legitimately score
   0 — that is the measurement working, not a pipeline failure. Pipeline
   failures look like: missing artifacts, verifier exceptions, or
   `contact=0` combined with very few turns.

## If the run errors

- `401 authentication_failed` in `agent/claude-code.txt`: credentials not
  injected / wrong base URL for the provider.
- Agent never contacts the proxy URL: check the trial's `turns.jsonl` is
  empty and the instruction reached the agent (`agent/` logs).
- Verifier scores 0 with `contact=0` and a normal-looking trajectory: the
  agent never probed the AOU surface — a real behavioral result worth
  recording, not an infra bug.
