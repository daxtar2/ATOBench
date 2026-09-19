# ATOBench Schema Changelog

Versioned schemas for EpisodeSpec, TurnRecord, EpisodeSummary. Bumped on every
breaking change. Old episode logs retain their `schema_version` field for
in-place upgrades; loaders must support one minor version of backward compat.

## 0.1.0 — 2026-07-03

Initial public schema. Replaces the env-var configuration mechanism.

- `episode_spec.json`: schema_version, episode_id, task_id (T1-T4), baseline (B0-B3),
  primitive_config (list of {primitive, coupling_variant, params}), ablated_primitives,
  defense_posture (none/strong/weak), budget (wall_clock_s, max_turns, max_tokens),
  seed, agent (driver, llm_backbone, subagent), task_overrides, run_idx.
- `turn_record.json`: i, ts, svc, req, status, body, is_deceptive, deception_tag,
  reception.
- `episode_summary.json`: schema_version, episode_id, task_id, baseline,
  primitive_config, defense_posture, agent, run_idx, effect_vector (8-dim + flag_rate + fca),
  n_turns, n_deceptive, wall_clock_s, total_cost_usd, total_tokens, flag_captured,
  give_up, competence_passed, reception_per_turn, teardown_ok.

### Backward compatibility shim

Existing scripts that set `ATOBENCH_ABLATE_PRIMITIVE`, `ATOBENCH_COUPLING_VARIANT`,
`ATOBENCH_BASELINE`, `ATOBENCH_TASK`, `ATOBENCH_EPISODE_ID`, `ATOBENCH_AGENT_DRIVER`,
`ATOBENCH_LLM_BACKBONE` env vars continue to work via the
`atobench.schema.env_compat` shim, which materializes an EpisodeSpec from env vars.
New code should use `EpisodeSpec.from_json(path)` or `EpisodeSpec.from_dict(d)`.

### Migration plan

- v0.1.x: env-var shim + EpisodeSpec JSON both supported. New sweeps should emit
  EpisodeSpec JSON alongside the env-var invocation.
- v0.2.0: EpisodeSpec JSON becomes the canonical config; env vars deprecated.
- v0.3.0: env-var shim removed.
