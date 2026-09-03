# Learning-data design: literature grounding

This note records which external research decisions materially shaped
`LearningRecord v1`. It is not a broad survey.

## Execution and training must be decoupled

[Agent Lightning](https://arxiv.org/abs/2508.03680) formulates agent execution
as an MDP, introduces a unified data interface, and decomposes agent trajectories
into training transitions while keeping the training system separate from the
agent runtime. ATOBench follows the same systems boundary: the exporter is a
read-only consumer of frozen analysis artifacts and does not import or control
the pentest agent.

## Preserve action–observation interaction boundaries

[InterCode](https://arxiv.org/abs/2306.14898) models interactive code generation
as an RL environment with code as actions and execution feedback as observations.
This supports a future ATOBench transition view at tool-call boundaries. The
current lightweight Stage 20 cohort does not include those raw action and
observation bodies, so v1 explicitly marks SFT and offline-transition use as not
ready instead of fabricating them.

## Cyber tasks need verifiable environments and intermediate structure

[Cybench](https://arxiv.org/abs/2408.08926) packages reproducible CTF
environments, validates task solvability, and adds subtasks for detailed
capability evaluation. [AutoPenBench](https://arxiv.org/abs/2410.03225) uses
generic and task-specific milestones to compare pentest agents and expose their
limitations. ATOBench maps this requirement to registered findings,
AOU-specific predicates, target-side evidence facts, stop descriptors and
report-closure states rather than a single terminal success label.

## Long horizons need credit below the episode level

[TRACE](https://arxiv.org/abs/2607.13988) identifies sparse outcome rewards as
misleading for long tool-use trajectories because failed rollouts can contain
valuable intermediate actions. It assigns credit at turn/tool-call boundaries.
ATOBench therefore exports typed process labels separately from episode
outcomes. It does not assign generic numeric rewards: the meaning of a positive
or negative cyber fact depends on the AOU and objective.

## Verifiable labels must remain distinct from learned judgments

Work on
[verifiable process rewards](https://openreview.net/pdf?id=DS6NHNcCRK)
emphasizes objective intermediate feedback for long-horizon agentic reasoning.
ATOBench consequently separates exact deterministic HTTP predicates from
explicit agent text, model Judge scores and semantic-matcher decisions. Only
exact deterministic positive/negative facts are marked as verifiable
process-label candidates in v1.

## Attacker and defender are both learning agents

[CyberBattleSim](https://www.microsoft.com/en-us/research/project/cyberbattlesim/)
frames attacker and defender behavior in a cyber environment through an
OpenAI-Gym-style RL interface. ATOBench keeps this dual perspective but works at
the observation/evidence layer: the same frozen Native/ATO pair can support
pentest-agent learning and defender intervention-policy analysis, under
separate objectives and release authorization.

## Resulting v1 decisions

1. One schema, three views: episode summary, process label, counterfactual pair.
2. No hidden chain-of-thought.
3. No source absolute paths or raw secrets.
4. No generic scalar reward.
5. Model-derived labels never masquerade as environment truth.
6. Same target/AOU/unit/block stays in one data split.
7. Current export is a candidate substrate, not an RL-ready dataset.
8. Direct SFT/offline RL waits for a separately audited action-observation join.
