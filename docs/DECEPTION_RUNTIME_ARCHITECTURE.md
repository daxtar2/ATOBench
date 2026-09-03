# ATOBench Deception Runtime Architecture v2

> Version: v2 migration draft (2026-07-08)
>
> Scope: refactor the mitmproxy deception runtime from primitive-transformer
> dispatch into a rule-driven, effect-driven, attributable runtime with
> clean-relative `pentest_effect` evaluation as the paper-grade path.

## 1. New Execution Chain

The canonical chain is now:

```text
deception_plan.yaml
  -> atobench compile --deception-id <id>
  -> targets/<name>/deceptions/<id>/runtime_program.yaml
  -> RuntimePipeline.execute()
  -> runtime_events[]
  -> legacy deception_tag projection
  -> attribute / report-normalizer / evaluate-pair
  -> pentest_effect.json
```

`deception_config.yaml` remains supported as a legacy projection. If a runtime
program is not provided, `addon.load()` wraps the existing config into a
RuntimeProgram with `legacy_transformer` effects.

The old per-episode `EffectVector` path has been removed from active code. The
benchmark-grade evaluation path is the clean-relative `pentest_effect.json`
produced by `atobench evaluate-pair`.

The preferred runtime path is now runtime-program-only:

```bash
python -m atobench.cli.main run --mode proxy \
  --deception-dir targets/<name>/deceptions/<id> \
  --target-url http://127.0.0.1:<port>
```

`--deception-dir` resolves `runtime_program.yaml` and optional legacy
`deception_config.yaml`. If only `ATOBENCH_RUNTIME_PROGRAM` is set, `addon.load()`
does not require `ATOBENCH_DECEPTION_CONFIG`.

The compiler also reads the primitive catalog:

```text
primitive_index.yaml
  -> compiler catalog validation / metadata enrichment

primitive_recipes.yaml
  -> explicit executable overrides for promoted wiki primitives

recipes.py
  -> auto-derived default recipes for every primitive_index entry

renderers/
  -> optional domain-specific effect generators for recipe-backed primitives

primitive_wiki_v2.md
  -> human/LLM knowledge source, not parsed by runtime
```

## 2. Runtime IR

New schemas:

- `schema/runtime_program.json`
- `schema/runtime_event.json`

New package:

- `runtime_ir/models.py`
- `runtime_ir/recipes.py`

Key concepts:

- `RuntimeProgram`: compiled machine-readable program consumed by the proxy.
- `RuleSpec`: one matched rule with layer, priority, primitive metadata, match
  criteria, effects, guards, conflict policy, and attribution metadata.
- `BindingSpec`: one concrete insertion point for a high-level injection.
  Legacy `trigger + transform` injections compile to one `default` binding;
  explicit `bindings[]` injections compile to one rule per binding.
- `EffectSpec`: generic operation such as JSON field set/remove, body replace,
  header set/remove, synthetic response, stateful response, or legacy adapter.
- `RuntimeEvent`: canonical attribution record emitted by execution.

The IR preserves high-level planner semantics that old `plan_to_config` dropped:

- `injection_id`
- `binding_id`
- `primitive`
- `attack_face`
- `coupling`
- `surface`
- `loader`
- `hook`
- `target`
- `target_dims`
- `trajectory_anchor`
- `rationale`

Primitive names are no longer locked to the 15 legacy transformer enum in
`deception_plan.json`. The compiler validates names against
`primitive_index.yaml` plus the legacy transformer registry. This allows wiki
primitives such as `sitemap_decoy_orchestration` to compile into RuntimeProgram
rules when the plan provides a structured transform.

All primitives in `primitive_index.yaml` are now materialized into queryable
runtime recipes by `runtime_ir/recipes.py`. Explicit entries in
`primitive_recipes.yaml` override the auto-derived defaults. Auto-derived
recipes make every wiki/index primitive selectable by multi-agent planners and
compilable into RuntimeProgram when the plan supplies concrete target-specific
content.

Every recipe now exposes planner-facing insertion templates:

- `suggested_surfaces`: ordered candidate surfaces for the primitive.
- `default_bindings`: binding templates with `binding_id`, `surface`, `loader`,
  `hook`, `selector.endpoint_types`, `target`, and a transform skeleton.
- `binding_template_version`: version marker for future template migrations.

These templates cover all 64 `primitive_index.yaml` entries. They are not a
license to guess target paths silently: planners must replace
`<target_path_regex>` and placeholder payload values with target-specific
evidence before compilation.

Recipe-backed rendering is now supported with `transform.type: recipe_render`
or `transform.params.renderer`. The compiler validates recipe-supported
transform types and required parameters, then dispatches to renderer modules
that emit RuntimeProgram effects. Initial renderers:

- `openapi_patch`
- `graphql_introspection`
- `jwks_weak_key`
- `discovery_text`
- `stateful_sequence`

Runtime trigger matching supports `path_regex`, `methods`, `request_body_match`,
and `every_n_calls`.

## 2.1 Injection Surface and Loader Taxonomy

The planner now describes where a deception is inserted with explicit
`bindings[]`. This is the runtime-facing form of the L7 injection surface
taxonomy:

| Surface | Meaning | Default loader |
|---|---|---|
| `metadata` | HTTP headers, cookies, status/content metadata | `metadata` |
| `structured_payload` | JSON/XML/GraphQL/API body fields | `structured` |
| `document_text` | HTML, text, JS, comments, rendered templates | `document` |
| `failure_signal` | 403/404/429/500/error semantics | `document` or `structured` |
| `discovery_document` | OpenAPI, GraphQL introspection, robots, sitemap, well-known docs | `document` or `structured` |
| `enumeration_listing` | listings, search results, route catalogs, paginated indexes | `structured` |
| `progress_state` | job/session/progress/queue state | `session` |
| `interactive_session` | auth, CSRF, token refresh, WebSocket/SSE, multi-step flows | `session` |

The four loaders are execution categories, not primitive families:

- `metadata`: cheap header/status/cookie mutation.
- `document`: text/html/js/discovery-document patching.
- `structured`: JSON/XML/GraphQL/OpenAPI/list-object patching.
- `session`: stateful cross-turn orchestration and consistency.

Example plan shape:

```yaml
injections:
  - id: openapi-trap
    primitive: openapi_spec_poisoning
    attack_face: F1
    coupling: schema_coupled
    target_dims: [scope_narrowing]
    bindings:
      - binding_id: openapi-json
        surface: discovery_document
        loader: structured
        hook: response
        selector:
          path_regex: ^/openapi\.json$
          methods: [GET]
        target:
          kind: json_body
          path: $.paths
        transform:
          type: recipe_render
          params:
            renderer: openapi_patch
            fake_path: /api/v1/internal/export
            fake_method: post
    rationale: Target-specific deception case rationale.
```

One primitive can bind to multiple surfaces. The compiler emits one
`RuleSpec` per binding while preserving a shared `injection_id`. Runtime events
therefore support both coarse attribution (`injection_id`) and insertion-point
attribution (`binding_id`, `surface`, `loader`, `target`).

## 3. Runtime Layers

The proxy runtime now separates three layers:

| Layer | Meaning | B0 behavior | B3 behavior |
|---|---|---|---|
| `environment_base` | upstream target and synthetic endpoint behavior | enabled | enabled |
| `instrumentation` | logging hooks and non-deceptive experiment instrumentation | enabled | enabled |
| `deception_perturbation` | deception rules/effects | disabled | enabled |

This keeps the old benchmark semantics: B0 vs B3 still differs only by whether
the deception layer executes. The implementation is now explicit instead of
being encoded as a procedural order inside `addon.response()`.

Canonical v2 does not treat `FLAG{...}` as a runtime objective, planner
invariant, or target-profile field. Flag-like strings are only report evidence tokens
that the evaluator may observe inside normalized findings. Legacy
`deception_config.flag` support remains available only when running old
config-only flows through the compatibility adapter.

## 4. Attribution

`runtime_events` is now the canonical attribution source. `deception_tag` is a
compatibility projection generated by `proxy/event_projection.py`.

Each runtime event includes:

- `rule_id`
- `injection_id`
- `binding_id`
- `primitive`
- `surface`
- `loader`
- `hook`
- `target`
- `effect_id`
- `operation`
- `layer`
- `target_dims`
- `trajectory_anchor`
- `matched_anchor`
- `guard_results`
- `details.u_t`

Legacy readers that inspect `deception_tag.z_t`, `u_t`, or
`primitives_fired` continue to work. Instrumentation events are not projected
into `deception_tag`, so B0 instrumentation does not count as deception.

## 5. Compatibility Strategy

The migration is intentionally non-breaking:

- Existing `deception_config.yaml` still validates.
- Existing transformer classes still run through `legacy_transformer` effects.
- Existing evaluator code can continue reading `deception_tag`.
- New compiler output can bypass transformer classes by using generic effects.
- New primitives should be implemented as IR recipes/effects rather than new
  transformer classes.
- Existing `trigger + transform` plans are accepted and compiled into a
  synthetic `binding_id: default`; new multi-surface plans should use
  `bindings[]`.

## 6. Modified Components

Added:

- `atobench/runtime_ir/__init__.py`
- `atobench/runtime_ir/models.py`
- `atobench/runtime_ir/recipes.py`
- `atobench/runtime_ir/surfaces.py`
- `atobench/runtime_ir/renderers/`
- `atobench/runtime_ir/tests/test_runtime_ir.py`
- `atobench/deception_frame/primitive_recipes.yaml`
- `atobench/schema/runtime_program.json`
- `atobench/schema/runtime_event.json`
- `atobench/proxy/rule_engine.py`
- `atobench/proxy/event_projection.py`
- `atobench/scaffold/compile_runtime_program.py`
- `atobench/scaffold/deception_workspace.py`
- `atobench/scaffold/attribution_report.py`

Updated:

- `atobench/proxy/addon.py`: response hook now delegates to `RuntimePipeline`.
- `atobench/proxy/logging.py`: turn records now include `runtime_events`.
- `atobench/schema/loader.py`: added runtime schema validators.
- `atobench/schema/deception_config.json`: added `max_primitives_per_response`.
- `atobench/schema/deception_plan.json`: primitive field now uses catalog/compiler
  validation instead of a 15-name static enum; injections now support explicit
  `bindings[]` with `surface`, `loader`, `hook`, `selector`, `target`, and
  binding-local `transform`.
- `atobench/schema/runtime_program.json`: `RuleSpec` now carries
  `binding_id`, `surface`, `loader`, `hook`, and `target`.
- `atobench/schema/runtime_event.json`: canonical runtime events now carry
  `binding_id`, `surface`, `loader`, `hook`, and `target`.
- `atobench/scaffold/compile_runtime_program.py`: compiler now expands
  `injections[].bindings[]` into per-binding rules and synthesizes
  `binding_id: default` for legacy `trigger + transform` injections.
- `atobench/proxy/rule_engine.py`: response pipeline respects `hook: response`
  and counts max deception cap by distinct injection, not by binding rule.
- `atobench/scaffold/attribution_report.py`: attribution reports now include
  per-binding fire counts in addition to per-injection attribution.
- `atobench/scaffold/plan_to_config.py`: carries plan max cap into legacy config.
- `atobench/agents/proxy_runner.py`: accepts optional `runtime_program_path` and
  passes `ATOBENCH_RUNTIME_PROGRAM` to mitmdump.
- `atobench/cli/main.py`: adds `--runtime-program` for `run --mode proxy` and
  `sweep-proxy`, `--deception-dir` workspace resolution, `atobench compile`, and
  `atobench attribute`.
- `pyproject.toml`: includes `atobench.runtime_ir` package.
- Operator-side Claude Code skill `deception-experiment-cycle` (not part of
  this repository): legacy full-cycle checklist; current campaign runs use `atobench-experiment-operator` for campaign
  control and `make-deception` for the bounded planner bridge.
- Operator-side agent definition `deception-validator`: updated validator contract from
  15-name enum + config translation to catalog validation + RuntimeProgram
  compilation + legacy config projection.
- Operator-side agent definition `deception-planner`: planner now prefers explicit
  `bindings[]` for new RuntimeProgram cases.
- Operator-side agent definition `deception-consistency`: consistency checks now normalize
  explicit bindings and legacy trigger/transform into one binding view before
  checking conflicts, known-real-finding evidence boundaries, and cross-surface
  consistency.

## 7. Migration Log

2026-07-07:

1. Added Runtime IR models and schemas.
2. Added event projection so canonical runtime events can produce legacy
   `deception_tag`.
3. Added rule engine with explicit instrumentation/deception layers.
4. Wrapped old transformer registry behind `legacy_transformer` effects.
5. Added compiler from `deception_plan.yaml` to `RuntimeProgram`.
6. Wired `addon.response()` to the new runtime pipeline.
7. Added focused regression tests for compiler, rule execution, projection,
   legacy transformer compatibility, and event logging.
8. Updated EffectVector evaluator to prefer `runtime_events` and fall back to
   legacy `deception_tag`.
9. Added primitive catalog/recipe loading. `primitive_index.yaml` is now the
   compiler catalog; `primitive_recipes.yaml` is the structured bridge from
   wiki prose to executable RuntimeProgram recipes.
10. Added CLI/runner support for `--runtime-program` / `ATOBENCH_RUNTIME_PROGRAM`.
11. Updated the deception experiment skill and deception-validator subagent to
   use compile-first artifacts.
12. Added auto-derived recipes for all `primitive_index.yaml` entries and
    query helpers for multi-agent primitive selection.
13. Removed generic runtime's eager transformer-registry import; transformer
    registry is imported only when a `legacy_transformer` effect executes.
14. Added generic text/html synthetic response support for non-JSON primitives.
15. Added ID-scoped deception workspaces under
   `targets/<name>/deceptions/<deception_id>/`.
16. Added runtime-program-only proxy loading; `ATOBENCH_DECEPTION_CONFIG` is no
   longer required when `ATOBENCH_RUNTIME_PROGRAM` is set.
17. Added automatic run artifact archiving under
   `deceptions/<deception_id>/runs/<episode_id>/`.
18. Added compiler conflict-policy checks for response cap, same-target writes,
   and body clobber declarations.
19. Added `atobench attribute` / `scaffold.attribution_report` for runtime-event
   attribution reports.
20. Promoted high-value primitives to explicit recipes:
   `openapi_spec_poisoning`, `graphql_introspection_hallucination`,
   `jwks_weak_key_injection`, `fake_jwt_decoded_claim`,
   `vuln_symptom_inject`, `auth_scope_downgrade_echo`,
   `mirror_reflection_trap`, `rate_limit_gaslighting`,
   `canary_honeypot_warning`.
21. Added recipe renderer framework and initial renderers for OpenAPI,
   GraphQL, JWKS, discovery text, and stateful sequences.
22. Wired compiler to validate recipe contracts and dispatch `recipe_render`.
23. Added runtime matching for `request_body_match` and `every_n_calls`.
24. Ran package tests: `396 passed, 5 skipped`.
25. Added explicit `bindings[]` with injection surfaces, loaders, hooks, and
    targets. The compiler expands one injection into multiple rules while
    preserving shared attribution.
26. Added binding-aware runtime events and attribution report aggregation.

2026-07-08:

1. Promoted L7 injection surface taxonomy into a first-class runtime concept.
   The supported surfaces are `metadata`, `structured_payload`,
   `document_text`, `failure_signal`, `discovery_document`,
   `enumeration_listing`, `progress_state`, and `interactive_session`.
2. Added the four execution loader categories: `metadata`, `document`,
   `structured`, and `session`. These are runtime parser/patcher categories,
   not primitive families.
3. Added `runtime_ir/surfaces.py` as the shared vocabulary and inference
   helper for legacy plan migration. Legacy `trigger + transform` injections
   compile into a synthetic `binding_id: default` with inferred
   `surface/loader/target`.
4. Extended `deception_plan.json` so an injection may provide either:
   legacy `trigger + transform`, or new explicit `bindings[]`.
5. Extended RuntimeProgram `RuleSpec` with `binding_id`, `surface`, `loader`,
   `hook`, and `target`.
6. Extended RuntimeEvent with the same binding/surface fields so attribution
   does not need to infer insertion points from transformer tags or ad hoc
   log details.
7. Updated the compiler from one-injection-one-rule to
   one-injection-many-bindings. Each binding becomes one rule; all rules keep
   the same `injection_id` and distinct `binding_id`.
8. Preserved backward compatibility for old plans by keeping `trigger` and
   `transform` accepted and compiling them as `binding_id: default`.
9. Fixed recipe renderer dispatch for binding-local transforms, so
   bindings-only cases such as `openapi_spec_poisoning` can pass renderer
   parameters from `bindings[].transform.params`.
10. Adjusted runtime max deception cap to count distinct injections rather
    than binding rules. This allows one high-level primitive to touch multiple
    surfaces without consuming multiple primitive-cap slots.
11. Added response-hook gating. Current mitmproxy execution applies only
    `hook: response`; RuntimeProgram request hooks are schema/IR-visible but
    intentionally not executed until a request-side runtime pipeline is added.
12. Updated attribution reports to show both per-injection and per-binding
    fire counts. This distinguishes, for example, a discovery-document binding
    from a structured-payload binding under the same primitive.
13. Updated the deception experiment skill and planner/consistency/validator
    subagent prompts so multi-agent planning prefers explicit `bindings[]`
    and validates conflicts at the binding level.
14. Added regression coverage for bindings-only plan schema acceptance,
    multi-binding rule expansion, renderer-backed binding transforms,
    binding-aware runtime events, and binding-aware attribution.
15. Added `suggested_surfaces` and `default_bindings` to all 64 recipe catalog
    entries. Query results now provide planner-ready binding templates for each
    primitive.
16. Added compiler support for header metadata bindings: `target.kind: header`
    plus `transform.injected_values` now emits `set_header` effects instead of
    JSON field writes.
17. Added regression coverage for full 64-primitive binding-template coverage,
    query exposure, OpenAPI renderer defaults, and executable header bindings.
18. Added target-centric pentest effect evaluation in
    `atobench/eval/pentest_effect.py`. The new evaluator consumes paired clean
    and deception runs, target-profile `known_real_vulns`, final reports, plans,
    RuntimeProgram metadata, and `runtime_events`.
19. Defined clean-relative benchmark metrics in
    `atobench/eval/PENTEST_EFFECT_METRICS.md`: contact rate, verified real
    recall, clean-relative recall drop, verified fabrication rate, evidence
    closure rate, trajectory deviation, deception budget waste, cognitive
    adoption, and layered attack-success-style rates.
20. Split fake evidence adoption into three levels: behavioral/final-report
    adoption, suspected fake lead, and verified fake finding. A fake value in
    the final report no longer counts as outcome success unless the agent
    upgrades it into a verified vulnerability with evidence closure.
21. Added `atobench pentest-effect` so archived clean/deception run artifacts can
    produce the new benchmark JSON report without custom Python glue.
22. Added regression coverage for the pentest evaluator: contact denominator
    handling, verified fake fabrication, clean-relative missed real
    vulnerabilities, suspected fake leads, and behavioral adoption without
    final-report adoption.
23. Added `atobench/schema/normalized_findings.json` and validator support for a
    report-normalizer stage. Normalized reports structure claim level,
    endpoint, vulnerability class, evidence closure, evidence fields, and raw
    text without exposing runtime fake values or ground truth.
24. Updated `pentest_effect` to consume optional normalized clean/deception
    reports. If normalized findings are supplied, raw regex/prose parsing is
    bypassed and the deterministic evaluator performs real/fake matching.
25. Added `atobench normalize-report` as a deterministic fallback normalizer and
    extended `atobench pentest-effect` with `--clean-normalized-report` and
    `--deception-normalized-report`.
26. Added the operator-side `report-normalizer` agent definition and updated the deception
    experiment skill to a 10-step flow: attribution, report normalization,
    pentest-effect metrics, then impact report.
27. Extended `PENTEST_EFFECT_METRICS.md` with a Potemkin-inspired result
    analysis protocol: baseline rows, contact-conditioned metrics, budget
    waste, trace taxonomy, and ATOBench-specific novelty framing.
28. Ran package tests: `411 passed, 5 skipped`.
29. Updated the evaluation protocol for black-box pentest agents:
    `report-normalizer` is retained as a blinded report coder so agents can keep
    their native report format. It structures claims only and cannot see
    treatment labels, runtime fake values, runtime events, deception payloads,
    or ground truth. `native_structured` remains useful for controlled agents
    and sensitivity analysis; real/fake matching and success rates remain
    deterministic in `pentest_effect`.
30. Connected the evaluation chain end-to-end with run-directory artifacts:
    `archive_episode_run()` now writes `final_report.txt` into each archived
    run when report text is available.
31. Added `atobench/eval/pair_workflow.py` and `atobench evaluate-pair`. The command
    accepts `--clean-run-dir` and `--deception-run-dir`, ensures final report
    files exist, validates or creates `normalized_findings.json`, infers
    workspace `target_profile/deception_plan/runtime_program`, and writes
    `pentest_effect.json` plus `evaluation_manifest.json`.
32. Updated the experiment skill Step 9 to use `atobench evaluate-pair` after the
    blinded report-normalizer writes normalized findings. `--normalizer-mode
    existing` is the benchmark path; `heuristic` is explicitly debug-only.
33. Added regression tests for run-dir report extraction, pair evaluation
    artifact generation, and archived final report persistence.
34. Ran package tests: `413 passed, 5 skipped`.
35. Ran a pre-experiment architecture audit before target experiments. Verified
    that `ATOBENCH_RUNTIME_PROGRAM` is the preferred proxy entrypoint, that B0
    suppresses only `deception_perturbation` rules inside `RuntimePipeline`,
    and that `runtime_events` remains the canonical attribution record.
36. Isolated legacy `deception_config.synthetic_endpoints` from
    RuntimeProgram-mode runs. When `ATOBENCH_RUNTIME_PROGRAM` is set, the old
    addon `request()` synthetic endpoint branch returns immediately, so the
    compatibility `deception_config.yaml` emitted beside `runtime_program.yaml`
    cannot add hidden legacy request-time behavior to new experiments.
37. Updated proxy module comments to state that `runtime_events` is canonical
    and `deception_tag` is only a compatibility projection.
38. Fixed the experiment skill Step 5 validation block so artifact validation
    commands and renderer-binding guidance are clearly separated.
39. Tightened RuntimeProgram precedence when both runtime and legacy config are
    provided manually: absent an explicit `ATOBENCH_EPISODE_ID`, the proxy now
    takes `episode_id` from `runtime_program.yaml` before falling back to the
    compatibility config.
40. Removed flag-centric assumptions from the canonical v2 compile path. Target
    profile is the canonical target metadata input; flags are not accepted as
    runtime objectives or instrumentation.
41. Updated `deception_plan.json`, planner/validator/consistency prompts, and
    the experiment skill so there is one pentest objective: find as many valid
    vulnerabilities as possible. `scoring_profile` is now `pentest_report`;
    `flag_regex`, `ctf_flag`, `multi_service_flag`, and
    `real_flag_reachable` are no longer canonical v2 plan concepts.
42. Reframed flag-like strings as report outcome evidence only. Clean-relative
    evaluation should compare normalized findings: a clean report that includes
    a proof token plus a verified vulnerability description can be counted as a
    discovered finding; a deception report that omits that verified finding is
    evidence for suppression/missed-real-finding effects.
43. Added an open-source experiment-cycle wrapper under `atobench/experiment/`.
    The wrapper reads YAML configs, writes an experiment manifest, starts target
    services, prepares scaffold prompts, compiles ID-scoped RuntimeProgram
    workspaces, runs B0/B3 proxy episodes in foreground or background, archives
    clean-run artifacts, and dispatches attribution/evaluate-pair commands.
44. Formalized the execution boundary between Python and Claude Code:
    deterministic shell/Python steps are exposed through
    `atobench experiment <action> --config ...`; the current default
    Agent-tool stage is the bounded `make-deception` planner bridge. The older
    the operator-side `deception-experiment-cycle` skill (not part of this
    repository) remains a human-facing checklist, not the campaign runner.
45. Added `agentic-pentest-benchmark` as a proxy episode driver. It runs
    `claude -p` with `Agent:*` enabled and delegates the audit to the
    `agentic-pentest-benchmark` subagent, asking it to use `report-generator`
    when available. In this path success is a usable pentest report artifact,
    not flag capture.
46. Updated the Juice Shop target compose file to start only the target service.
    RuntimeProgram-mode mitmproxy is started per episode by `atobench run --mode
    proxy`, avoiding stale compose-level `deception_config.yaml` dependencies.
47. Added `scripts/atobench-experiment` as a thin open-source wrapper over
    `python -m atobench.cli.main experiment ...`. It provides a stable shell entry
    for target startup, scaffolding, foreground/background episode runs,
    attribution, and evaluation while preserving the Claude Code skill boundary
    for Agent-tool stages.
48. Made the agentic pentest proxy driver honor `agent.subagent_type` in the
    actual Claude prompt. This keeps pentest-agent substitution config-driven:
    comparing different pentest agents no longer requires patching the runner
    prompt or report normalizer.
49. Updated the repository README to point new users at the v2
    `scripts/atobench-experiment` / RuntimeProgram / report-normalizer /
    evaluate-pair path and explicitly mark `--primitive`, `--coupling`,
    service-mode runs, and transformer-primary configs as legacy boundaries.
50. Made `examples/experiments/juice-shop-agentic.yaml` the default
    `atobench experiment` config. `--config` is now optional for the default
    Juice Shop workflow and remains available for modified target/agent/episode
    YAML files.
51. Removed the writeup dependency from the canonical experiment scaffold and
    compile path. `atobench experiment scaffold` now writes
    `scaffold_work/target_profile.yaml` from the experiment YAML and generates
    recon/planner/consistency/validator prompts from that profile.
52. Tightened `compile_runtime_program_files()` and
    `create_deception_workspace()` so `target_profile_path` is required for v2
    compilation; there is no writeup fallback on the RuntimeProgram path.
53. Deprecated legacy `atobench scaffold` and `atobench generate` CLI entrypoints so
    accidental old commands fail clearly instead of looking for old target
    metadata files.
54. Updated the deception-experiment-cycle skill and stable subagent prompts to
    use `target_profile_path` and `atobench experiment scaffold`.
55. Re-ordered the canonical experiment wrapper to clean-first. `run-clean`
    now writes and uses
    `targets/<name>/experiments/<experiment_id>/clean_runtime_program.yaml`
    with `source.kind: clean_runtime`, no deception workspace, no plan, and no
    transformer/config dependency. Its archived turns feed
    `scaffold_work/trajectory_profile.json`.
56. `atobench experiment scaffold` is now a post-clean step. It refuses to run
    without `scaffold_work/clean_run/turns.jsonl`, so planner prompts are
    trajectory-aware rather than target-profile-only.
57. `atobench experiment compile` is now gated on
    `clean_run/turns.jsonl`, `trajectory_profile.json`,
    `target_profile.yaml`, and `deception_plan.yaml`. This prevents accidental
    compile-first runs in the benchmark path.
58. Foreground `run-clean` and `run-deception` now print progress every ~10s
    from `turns.jsonl`, `episodes.jsonl`, and `orchestrator.jsonl`:
    elapsed time, turn count, deception event count, recently fired
    injections, phase, and latest HTTP request/status.
59. Added `atobench experiment make-deception` /
    `scripts/atobench-experiment make-deception`. This command runs after
    `scaffold`: it launches Claude Code with `Agent:*` permission but instructs
    the main session to invoke only the `deception-planner` agent. It does not
    call the outer `deception-experiment-cycle` skill and does not invoke
    recon/consistency/validator/report-normalizer. The planner agent owns the
    narrow deception-making task: read scaffold artifacts, create or reuse a
    minimal endpoint inventory, write and schema-check `deception_plan.yaml`,
    and write planner notes. Python then calls the deterministic compile gate.
    It refuses invalid clean reports before starting.
60. Added multi-run experiment support gates for paired-experiment calibration:
    foreground B3 now receives a fresh episode id like B0; `attribute` and
    `evaluate` resolve the latest B3 id from the experiment manifest; clean
    runs are preserved under `scaffold_work/clean_runs/<episode_id>/` while
    `scaffold_work/clean_run/` remains the latest trajectory input; and
    `validate-clean` / `validate-deception` write `run_validity.json` so
    timeout, parser, missing-artifact, health, and contact conditions are
    separated from scientific outcomes.

## 8. Pre-Experiment Audit and Legacy Boundary

Canonical target experiment path:

```text
atobench experiment init/start-target/health
  -> atobench experiment run-clean
  -> targets/<name>/scaffold_work/clean_run/{turns.jsonl, episode_summary.json, final_report.txt}
  -> targets/<name>/scaffold_work/clean_runs/<episode_id>/{turns.jsonl, episode_summary.json, final_report.txt}
  -> atobench experiment validate-clean
  -> targets/<name>/scaffold_work/trajectory_profile.json
  -> atobench experiment scaffold
  -> atobench experiment make-deception
  -> deception-planner writes deception_plan.yaml and planner notes
  -> deterministic compile gate
  -> targets/<name>/deceptions/<id>/runtime_program.yaml
  -> atobench experiment run-deception
  -> targets/<name>/deceptions/<id>/runs/<episode_id>/{turns.jsonl, episode_summary.json, final_report.txt}
  -> atobench experiment validate-deception
  -> atobench attribute
  -> blinded report-normalizer writes normalized_findings.json
  -> atobench evaluate-pair --normalizer-mode existing
  -> pentest_effect.json + evaluation_manifest.json
```

Components allowed on the canonical path:

- `runtime_program.yaml`: authoritative runtime program.
- `clean_runtime_program.yaml`: authoritative B0 proxy runtime for clean
  trajectory collection. It contains no deception rules and is not stored in a
  deception workspace.
- `runtime_events[]`: authoritative execution and attribution evidence.
- `deception_plan.yaml` and explicit `bindings[]`: authoritative planner to
  runtime contract.
- `report-normalizer`: blinded report coder only. It may structure native
  agent reports, but it must not see runtime events, fake values, deception
  payloads, treatment labels, or ground truth.
- `pentest_effect.json`: authoritative benchmark metric output for paired
  clean/deception experiments.

Legacy or auxiliary components that must not drive new paper metrics:

- `deception_config.yaml`: compatibility projection only. It may be generated
  beside `runtime_program.yaml` for old validators and instrumentation fields,
  but new proxy runs should be launched through `--deception-dir` or
  `--runtime-program`.
- `legacy_transformer` effects and `atobench.proxy.transformers`: compatibility
  adapter for old configs. New cases should use generic effects or
  recipe-backed renderers.
- `deception_tag`: compatibility projection from `runtime_events`; useful for
  old logs but not the source of truth.
- `EffectVectorEvaluator`, `atobench analyze`, and `ProxyEpisodeRunner._evaluate`:
  removed legacy outputs. Do not use them for new experiments.
- `atobench sweep-proxy`: legacy comparison runner; it summarized EffectVector-era
  fields and should not be used as the paper benchmark aggregation path.
- `atobench run --mode services`, `--primitive`, and `--coupling`: old service
  framework entrypoint. Current target experiments should use
  `--mode proxy --deception-dir`.
- `atobench scaffold` / `atobench generate`: deprecated legacy entrypoints. Current
  experiments use `atobench experiment scaffold`, target_profile.yaml, and
  RuntimeProgram workspaces.
- `atobench/scaffold/MULTI_AGENT_PIPELINE.md` and older adaptive-generator design
  docs: historical design references, not the current experiment protocol.

Required gates before treating a run as benchmark-valid:

- Clean `clean_runtime_program.yaml` validates with `validate_runtime_program`
  and has `source.kind: clean_runtime`.
- Deception `runtime_program.yaml` validates with `validate_runtime_program`.
- Clean B0 run has isolated `turns.jsonl`, `episode_summary.json`, and
  `final_report.txt` under `targets/<name>/scaffold_work/clean_run/`.
- `trajectory_profile.json` exists before planner/compile. Planner bindings
  should target surfaces actually contacted in the clean run unless the paper
  explicitly analyzes non-contact.
- B3 archived run has `turns.jsonl`, `episode_summary.json`,
  `final_report.txt`, and at least one applied `deception_perturbation`
  runtime event unless the case is explicitly analyzing non-contact.
- Attribution report is generated from `runtime_events`.
- Both clean and deception reports have blinded `normalized_findings.json`
  files that validate against `schema/normalized_findings.json`.
- `atobench evaluate-pair --normalizer-mode existing` writes
  `pentest_effect.json` and `evaluation_manifest.json`.

Known residual risks after this audit:

- RuntimeProgram request hooks are not executed yet. Request-side deception
  must currently be represented as response rewriting, synthetic response to an
  upstream 404/route response, or implemented later through a request-pipeline
  extension.
- Some primitives still use auto-derived generic recipes. They are queryable
  and compilable when the planner supplies concrete bindings and payloads, but
  only promoted recipes/renderers encode primitive-specific patch logic.
- Matching real and fake findings still depends on endpoint/vulnerability-class
  normalization quality. This should be audited after the first real target
  experiment batch.
- The report-normalizer is a blinded coder, but its coding quality still needs
  inter-rater or spot-check auditing before final paper tables.
- Cross-run aggregation and statistical reporting are not yet a dedicated CLI;
  current benchmark output is per paired run.

## 9. Remaining Work

- Add request-hook execution for `hook: request`; current runtime records the
  hook but only executes response hooks, and legacy request synthetic endpoints
  are intentionally disabled in RuntimeProgram-mode runs.
- Move each existing transformer to a first-class generic effect recipe where
  possible, starting with `fake_version_banner`, `substitute_subgoal`, and
  `decoy_sql_search`.
- Add conflict-policy validation for body clobber declarations beyond the
  current execution cap.
- Continue promoting remaining wiki entries from generic binding templates into
  explicit `primitive_recipes.yaml` overrides only when they need custom
  renderers beyond generic effects. Target-specific payload content is still
  supplied by planner transforms.
