# ATOBench Verification & Resilience Analysis

`atobench_vr` is the analysis layer of ATOBench. It takes frozen campaign
episodes produced by the ATOBench runtime and compiles hard-to-use
long-horizon agent–environment interactions into sourced, evidence-aware data
objects: evidence graphs, predicate/fact registries, blinded judge
diagnostics, Native/ATO pair profiles, resilience statistics, and
learning-data exports.

It is not a trajectory viewer. The goal is to locate *where* a capability
chain forms or breaks: intervention contact, adoption, verification,
recovery, stopping, or reporting. See
[docs/PROJECT_VISION_AND_LEARNING_DATA.md](docs/PROJECT_VISION_AND_LEARNING_DATA.md)
for the full vision and the training-data roadmap.

## Scope

This project consumes frozen campaigns and agent sessions and performs:

1. Census, session flatten/redaction, and Claude↔HTTP alignment;
2. Evidence graph, predicate, and fact registry construction;
3. Three identity-blinded judge dimensions: Verification Control, Stop
   Decision, Report Grounding;
4. Registered-finding semantic closure matching;
5. Episode state and Native/ATO pair profiling;
6. Missingness-aware resilience statistics, result facts, and QA;
7. Trajectory dynamics exploratory analysis and figures.

The AOU runtime and cross-model agent scheduling live in the companion
`runtime/` project ("data production layer"). The interface between the two
is defined in [docs/AOU_RUNTIME_INTERFACE.md](docs/AOU_RUNTIME_INTERFACE.md).

## Data boundary

The frozen reference datasets (the Stage-20 cohort: 450 episodes / 225
Native-ATO pairs / 5 models / 3 AOUs, plus derived learning-data exports) are
**not shipped in this repository**. All export commands below operate on a
frozen campaign directory and its pair profiles; run a campaign with
`runtime/atobench-cross-model` first, or point the commands at your own
frozen cohort.

Raw agent sessions and HTTP corpora remain external by design: exports emit
structured records without request/response bodies, field values, identity or
resource values, raw paths, or free text, and never expose hidden
chain-of-thought.

## Installation and checks

```bash
python3 -m pip install -e '.[test]'
python3 -m pytest -q
```

The self-checks make no network requests and no model calls:

```bash
python3 scripts/atobench-vr doctor
python3 scripts/atobench-vr smoke   # synthetic data only
```

Plotting requires the optional figures extra:

```bash
python3 -m pip install -e '.[test,figures]'
```

## Platform scaffold

The generic platform scaffold connects Target Adapters, frozen AOU bundles,
campaigns, and analysis profiles through hash-bound manifests, and maps
generic AOU IDs onto `atobench-cross-model` runner keys. `init-campaign`
mints a unique campaign with runner hashes; only an explicit `--register`
modifies the registry. By default only validation, planning, and freeze locks
are allowed; real execution requires freeze-lock revalidation, an execution
handoff, and double explicit authorization.

```bash
python3 scripts/atobench list
python3 scripts/atobench validate --campaign juice-shop-vr-stage20-smoke --allow-external-artifacts
python3 scripts/atobench run --campaign juice-shop-vr-stage20-smoke --allow-external-artifacts --dry-run
python3 scripts/atobench init-campaign --help
```

See [docs/PLATFORM_SCAFFOLD.md](docs/PLATFORM_SCAFFOLD.md).

## Exports

Replace `<frozen-campaign-root>` with your frozen campaign / reference root
containing the pair profiles, episode states, and fact registry.

**LearningRecord v1** — episode summaries, process labels, and C0/C1
counterfactual pair records:

```bash
python3 scripts/atobench-vr export-learning-data \
  --pair-profiles <frozen-campaign-root>/cohort/pair_resilience_profiles.jsonl \
  --episode-states <frozen-campaign-root>/cohort/unblinded_episode_states.jsonl \
  --fact-registry <frozen-campaign-root>/cohort/episode_fact_registry.jsonl \
  --output workspace/learning-data-v1 \
  --target-id juice-shop \
  --target-snapshot-id <your-snapshot-id> \
  --allow-real-data
```

**Structured transition v1** — one record per tool-call boundary
(state-before, action class, observation metadata, intervention/evidence
events, state-after). Requires frozen graph nodes, which are intentionally
not copied into the repository:

```bash
python3 scripts/atobench-vr export-learning-transitions \
  --graph-nodes /path/to/frozen/graph_nodes.jsonl \
  --pair-profiles <frozen-campaign-root>/cohort/pair_resilience_profiles.jsonl \
  --episode-states <frozen-campaign-root>/cohort/unblinded_episode_states.jsonl \
  --output workspace/learning-transitions-v1 \
  --target-id juice-shop \
  --target-snapshot-id <your-snapshot-id> \
  --allow-real-data
```

**Transition audit packets** — deterministic stratified closed-set audit
samples:

```bash
python3 scripts/atobench-vr build-transition-audit \
  --transitions workspace/learning-transitions-v1/transition_records.jsonl \
  --output workspace/transition-audit-v1 \
  --sample-per-stratum 8 \
  --allow-real-data
```

**Counterfactual Verification Trajectory Dataset v1** — six canonical object
types with Replay / Diagnostic / Counterfactual views:

```bash
python3 scripts/atobench-vr export-counterfactual-trajectories \
  --transitions workspace/learning-transitions-v1/transition_records.jsonl \
  --episodes workspace/learning-data-v1/episode_records.jsonl \
  --process-labels workspace/learning-data-v1/process_label_records.jsonl \
  --pairs workspace/learning-data-v1/counterfactual_pair_records.jsonl \
  --output workspace/counterfactual-trajectory-v1 \
  --allow-real-data
```

Every export writes a dataset manifest, dataset card, quality summary, and
export audit alongside the JSONL records. Direct SFT and direct offline RL
are deliberately blocked: exports carry no natural-language context and no
frozen AOU-specific reward map. Completed export directories are never
overwritten.

## New experiments

1. Copy `config/project.example.json` for your own configuration;
2. Register the frozen campaign's absolute path in `config/campaign_registry.yaml`;
3. Pass the agent session/subagent JSONL roots;
4. Run census and smoke first, then advance stage by stage;
5. Any judge or semantic-matcher invocation must use the frozen allowlist
   and explicit authorization.

```bash
python3 scripts/atobench-vr --help
```

## Documentation

- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)
- [docs/PROJECT_VISION_AND_LEARNING_DATA.md](docs/PROJECT_VISION_AND_LEARNING_DATA.md)
- [docs/LEARNING_DATA_LITERATURE.md](docs/LEARNING_DATA_LITERATURE.md)
- [docs/DATA_LAYOUT.md](docs/DATA_LAYOUT.md)
- [docs/AOU_RUNTIME_INTERFACE.md](docs/AOU_RUNTIME_INTERFACE.md)
- [docs/PLATFORM_SCAFFOLD.md](docs/PLATFORM_SCAFFOLD.md)
- [docs/COUNTERFACTUAL_TRAJECTORY_DATASET.md](docs/COUNTERFACTUAL_TRAJECTORY_DATASET.md)
- [PROJECT_STATUS.json](PROJECT_STATUS.json) — machine-readable frozen-state status
