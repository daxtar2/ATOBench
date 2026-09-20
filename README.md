# ATOBench

**Measuring — and training — how autonomous penetration-testing agents verify vulnerabilities when target evidence lies.**

[paper](https://arxiv.org/abs/2608.12996) · [concepts](docs/CONCEPTS.md) · [evaluation design](harbor/docs/EVALUATION_DESIGN.md) · Apache-2.0

Autonomous pentest agents trust target responses: responses steer the attack
and decide what the final report claims. A deceptive response can therefore
redirect both — and a final report alone reveals nothing about how the agent
weighed conflicting evidence, changed course, or decided to stop. ATOBench
makes this verification process observable: a proxy injects *registered*
response transformations between target and agent, each transformed episode
is paired with a native episode under identical conditions, and the pair is
aligned at the first affected response. The result is a causal, stage-level
account of how a changed observation propagates through actions, evidence
recovery, stopping, and reporting.

Across 450 episodes and five model routes, the framework shows that increased
activity can mask a broken verification chain — e.g. SQLi grounded
verification collapses from 44.0% to 0% under deception while agents keep
probing — and that successful recovery depends on finding usable evidence and
preserving it through reporting.

![ATOBench overview](docs/figures/overview.png)

## Why ATOBench

- **Process, not just outcomes.** Every episode resolves into a stage chain —
  contact → detect → adapt → recover → close → support — reported as
  stage-conditional rates, so you see *where* verification breaks, not only
  *that* it broke. The binary grounded-verification endpoint
  `G = evidence ∧ report closure ∧ trace support` is retained for headline
  comparability.
- **Verifiable by construction.** The proxy sees every byte both ways, so
  evidence, recovery paths, and report support are checked deterministically
  against the wire log. No LLM judge participates in any scored path — the
  same rewards serve benchmark evaluation and RL training (RLVR-style).
- **Causal by design.** Matched Native/ATO pairs share model, task, budget,
  and harness; comparison starts at the intervention anchor. Downstream
  differences are attributable to the changed observation alone.
- **Anti-hack evidence scoping.** Only evidence on the contract's registered
  surface counts; real-but-out-of-scope findings (e.g. a different vulnerable
  endpoint) are flagged separately instead of masquerading as recovery.
- **A difficulty ladder, not a fixed test.** Deception dose, coupling, and
  selector tightness are parameters, so evaluation becomes a dose-response
  curve — and the same ladder is a curriculum for post-training.
- **Eval and RL in one artifact.** Tasks run on
  [Harbor](https://github.com/harbor-framework/harbor): one task definition
  yields benchmark trials, ATIF trajectories, and trainer-ready rollout
  batches with deterministic rewards.

## Quickstart

Requires Docker and an agent provider key (e.g. `ANTHROPIC_API_KEY`).

```bash
pip install harbor

# one episode of the SQLi contract under deception (C1), native control is -c0
harbor run -p harbor/tasks/atobench-sqli-c1 -a claude-code -m claude-sonnet-5

# stage-chain report over any set of recorded trials
python3 analysis/scripts/atobench-vr stage-chain jobs/<job...> --out out/ --allow-real-data

# trainer-ready rollout batch (ATIF trajectories + rewards)
python3 harbor/scripts/export_rollout_batch.py jobs/<job> --out rollouts.jsonl
```

Each trial records the wire-level trajectory (`turns.jsonl`), the agent-side
ATIF trajectory, the report, and a deterministic `reward.json` — all
regradable without re-running the agent (`harbor job regrade`).

## How it works

```text
┌────────────┐   only route    ┌──────────────────────┐        ┌──────────────┐
│ agent      │ ───────────────►│ proxy sidecar        │───────►│ target       │
│ (main      │◄─────────────── │ mitmproxy + frozen   │        │ (Juice Shop) │
│ container) │  transformed or │ RuntimeProgram       │        │              │
└────────────┘  native response└──────────┬───────────┘        └──────────────┘
                                           │ turns.jsonl (wire ground truth)
                                           ▼
                              separate grading environment
                              deterministic reward + stage signals
```

- **Observation-only perturbation** — the transform rewrites a response after
  the target executes and before the agent observes; requests, target code and
  state, the underlying vulnerability, prompts, and tools are untouched.
- **Frozen contracts with a preserved recovery path** — every observation
  contract (AOU) is replay-tested, native-controlled, and ships with a
  deterministic recovery/contradiction control, so a well-verifying agent
  always has a path to the truth.
- **Fail-closed reproducibility** — frozen suites pin SHA-256 of their
  dependencies; runners refuse to proceed on mismatch.

## Documentation

| | |
|---|---|
| Concepts & vocabulary (ATO, AOU, anchors, the three shipped contracts) | [docs/CONCEPTS.md](docs/CONCEPTS.md) |
| Evaluation design: stage chain, difficulty ladder, judge boundary | [harbor/docs/EVALUATION_DESIGN.md](harbor/docs/EVALUATION_DESIGN.md) |
| Reward contract (per-episode metrics, shaped reward) | [harbor/docs/REWARD_SPEC.md](harbor/docs/REWARD_SPEC.md) |
| RL rollout contract & stability validation | [harbor/docs/RL_ROLLOUT_CONTRACT.md](harbor/docs/RL_ROLLOUT_CONTRACT.md) |
| Real-agent runbook (providers, flakes, artifact persistence) | [harbor/docs/REAL_AGENT_RUNBOOK.md](harbor/docs/REAL_AGENT_RUNBOOK.md) |
| Authoring new observation contracts | [docs/AOU_AUTHORING.md](docs/AOU_AUTHORING.md) |
| Adapting a different agent | [docs/AGENT_ADAPTATION.md](docs/AGENT_ADAPTATION.md) |
| Legacy host-side runner (paper reproduction) | [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) |

## Repository layout

```text
├── harbor/        current execution layer: Harbor tasks, reward contract, docs, scripts
├── runtime/       package atobench — legacy host-side runner (paper reproduction path)
├── analysis/      package atobench_vr — stage-chain reports, pair statistics, judges (offline)
└── docs/          concept guides, contract standards, runbooks, figures
```

## Citation

```bibtex
@misc{chen2026atobench,
  title         = {ATOBench: Tracing How Autonomous Penetration-Testing Agents
                   Verify Vulnerabilities When Target Evidence Lies},
  author        = {Chen, Qiyang and Li, Yixi and Zhang, Fengwei and Liu, Junlin},
  year          = {2026},
  eprint        = {2608.12996},
  archivePrefix = {arXiv},
  primaryClass  = {cs.CR},
  doi           = {10.48550/arXiv.2608.12996},
  url           = {https://arxiv.org/abs/2608.12996}
}
```

## License

Apache-2.0. See [LICENSE](LICENSE) and [NOTICE](NOTICE).
