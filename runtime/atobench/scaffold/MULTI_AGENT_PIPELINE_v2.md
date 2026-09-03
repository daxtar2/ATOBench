# ATOBench Multi-Agent Pipeline v2

> Version: v2 migration draft (2026-07-08)
>
> This document records the scaffold change introduced by Runtime IR. It does
> not replace `MULTI_AGENT_PIPELINE.md`; it narrows the change needed for the
> Plan -> Compile -> Validate -> Execute -> Attribute chain.

## Stage Flow

```text
recon
  -> trajectory profile
  -> planner
  -> consistency
  -> compiler + validator
  -> runtime execution
  -> attribution
```

## Artifact Contract

Planner still writes:

- `targets/<name>/deception_plan.yaml`

Compiler/validator now writes:

- `targets/<name>/deceptions/<deception_id>/deception_plan.yaml`
- `targets/<name>/deceptions/<deception_id>/runtime_program.yaml`
- `targets/<name>/deceptions/<deception_id>/deception_config.yaml`
- `targets/<name>/deceptions/<deception_id>/compile_report.json`
- `targets/<name>/deceptions/<deception_id>/validation_report.md`
- `targets/<name>/deceptions/<deception_id>/runs/`

`runtime_program.yaml` is the primary proxy artifact. `deception_config.yaml`
is retained as a legacy projection for older runners.

The runtime command should pass both artifacts:

```bash
python -m atobench.cli.main compile \
  --target-dir targets/<name> \
  --deception-id dec_<short-name>

python -m atobench.cli.main run --mode proxy \
  --deception-dir targets/<name>/deceptions/dec_<short-name> \
  ...

python -m atobench.cli.main attribute \
  --deception-dir targets/<name>/deceptions/dec_<short-name> \
  --episode-id <episode_id>
```

## Compiler Responsibility

`atobench/scaffold/compile_runtime_program.py` performs:

- plan schema validation
- writeup schema validation; canonical v2 writeups do not contain `flag`,
  `synthetic_endpoints`, or `link_advertisements`
- primitive catalog validation against `deception_frame/primitive_index.yaml`
- recipe lookup through `runtime_ir/recipes.py`
- auto-derived default recipes for every `primitive_index.yaml` entry
- explicit recipe overrides from `deception_frame/primitive_recipes.yaml`
- planner-facing `suggested_surfaces` and `default_bindings` for every recipe
- recipe renderer dispatch for `transform.type: recipe_render`
- explicit `bindings[]` expansion: one high-level injection may compile to
  multiple RuntimeProgram rules with distinct `binding_id`, `surface`, `loader`,
  `hook`, and `target`
- C9 realism hard-block for `_links.cve` and `_links.advisory`
- high-level field preservation into Runtime IR
- generic effect compilation
- legacy config projection
- compile report generation

The legacy `deception_config.yaml` projection is emitted for compatibility only.
It must not receive hidden instrumentation from `writeup.yaml`; RuntimeProgram is
the canonical artifact for new experiments.

## Runtime Responsibility

The proxy consumes `RuntimeProgram` through `RuntimePipeline`.

If `ATOBENCH_RUNTIME_PROGRAM` is set, `addon.load()` reads that program. Otherwise
it wraps `deception_config.yaml` into a legacy RuntimeProgram automatically.

`primitive_wiki_v2.md` is not parsed by runtime. Every `primitive_index.yaml`
entry is materialized into a default recipe for query/compile eligibility.
Wiki entries become fully target-specific executable cases when either:

- planner emits an inline structured `transform`, or
- planner emits explicit `bindings[]` where each binding has a `selector`,
  `target`, and `transform`, or
- the primitive is promoted into `primitive_recipes.yaml` with explicit
  defaults/renderers.

Recipe query results now include `suggested_surfaces` and `default_bindings`.
The recommended planner flow is:

1. Query candidate recipes by effect dim, family, attack face, endpoint type,
   and state.
2. Select a `default_bindings[]` template whose `selector.endpoint_types`
   matches the inventory endpoint.
3. Replace `<target_path_regex>` with an observed target path regex.
4. Replace placeholder transform values with target-specific payloads.
5. Emit the filled binding under `injections[].bindings[]`.

## Injection Surface Contract

Planner output may use either the legacy shape or the explicit surface shape.
The legacy shape remains valid:

```yaml
trigger: {path_regex: ^/version$, methods: [GET]}
transform: {type: augment, injected_values: {version: Express/4.16.0}}
```

The preferred shape for new cases is:

```yaml
bindings:
  - binding_id: version-json
    surface: structured_payload
    loader: structured
    hook: response
    selector:
      path_regex: ^/version$
      methods: [GET]
    target:
      kind: json_body
      path: $.version
    transform:
      type: augment
      injected_values:
        version: Express/4.16.0
```

Supported surfaces are `metadata`, `structured_payload`, `document_text`,
`failure_signal`, `discovery_document`, `enumeration_listing`,
`progress_state`, and `interactive_session`. Supported loaders are `metadata`,
`document`, `structured`, and `session`. Current mitmproxy execution runs
`hook: response`; request hooks are schema/IR-visible but not executed yet.

## Validation Gate

The validator stage should pass only if:

- `deception_plan.yaml` validates.
- `runtime_program.yaml` validates.
- `deception_config.yaml` validates.
- implementability check passes.
- compile report has no errors.
- conflict policy checks pass: response cap, same-target writes, and body
  clobbers are either absent or explicitly allowed.
- trigger/selector conditions compile and validate: `path_regex`, `methods`,
  `request_body_match`, and `every_n_calls`.
- every explicit binding has a valid `surface`, `loader`, `hook`, `target`, and
  recipe-supported `transform.type`.

## Compatibility

Existing subagent prompts may keep producing `deception_plan.yaml`. The only
required validator change is to call `compile_runtime_program_files()` and treat
`runtime_program.yaml` as the canonical runtime artifact.
