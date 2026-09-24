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

> **Gateway model names.** When `ANTHROPIC_BASE_URL` points at a
> gateway/relay, Harbor passes the `-m` string through verbatim, and many
> gateways only serve bare model IDs. If a trial dies within minutes with
> `503 No available channel for model ...`, retry with the bare id
> (`-m claude-sonnet-5` instead of `-m anthropic/claude-sonnet-5`). Check
> what the gateway serves via `curl $ANTHROPIC_BASE_URL/v1/models`.
>
> **Transient first-turn failures.** Two observed flake classes, both
> recovered by simply re-running: provider-side `API Error: Content block
> not found`, and a nondeterministic safety-classifier refusal on the pentest
> instruction (~1 in 6 trials in the validation batch). For campaigns, retry
> the trial rather than treating these as measurements.

```bash
harbor run -p harbor/tasks/atobench-sqli-c1 -a claude-code -m claude-sonnet-5
harbor run -p harbor/tasks/atobench-sqli-c0 -a claude-code -m claude-sonnet-5
```

Then the other two AOUs:

```bash
harbor run -p harbor/tasks/atobench-jwt-c1    -a claude-code -m claude-sonnet-5
harbor run -p harbor/tasks/atobench-jwt-c0    -a claude-code -m claude-sonnet-5
harbor run -p harbor/tasks/atobench-basket-c1 -a claude-code -m claude-sonnet-5
harbor run -p harbor/tasks/atobench-basket-c0 -a claude-code -m claude-sonnet-5
```

## Preserve artifacts immediately (cloud VMs are ephemeral)

Trial directories live only on the VM that produced them; when a cloud agent
VM is reclaimed, unexported results are lost. Right after a batch, export and
persist:

```bash
# trainer-ready JSONL (small) — safe to commit to a results branch
python3 harbor/scripts/export_rollout_batch.py jobs/<job> --out results/<job>.jsonl

# or upload the whole job to Harbor Hub for sharing/archival
harbor upload jobs/<job>
```

Do this before ending the run; `jobs/` is gitignored by design, so results
branches or the Hub are the durable store.

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
