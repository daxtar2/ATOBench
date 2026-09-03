"""Compile deception_plan.yaml into the RuntimeProgram IR.

This is the explicit Plan -> Compile boundary for the v2 runtime. The compiler
preserves planner semantics that were previously dropped by plan_to_config:
attack_face, target_dims, trajectory_anchor, attribution, and guard policy.
"""

from __future__ import annotations

import json
import re
import uuid
from pathlib import Path
from typing import Any

import yaml

from atobench.scaffold.plan_to_config import translate
from atobench.schema.loader import (
    validate_deception_config,
    validate_deception_plan,
    validate_runtime_program,
)
from atobench.runtime_ir.recipes import (
    LEGACY_TRANSFORMER_PRIMITIVES,
    load_primitive_index,
    load_primitive_recipes,
)
from atobench.runtime_ir.renderers import render_effects
from atobench.runtime_ir.surfaces import infer_loader, infer_surface, infer_target


def compile_runtime_program(
    plan: dict[str, Any],
    target_profile: dict[str, Any],
    baseline: str = "B3",
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """Compile plan + target profile into RuntimeProgram, legacy config, report."""
    validate_deception_plan(plan)

    primitive_index = load_primitive_index()
    primitive_recipes = load_primitive_recipes()
    _validate_plan_primitives(plan, primitive_index)
    legacy_config = translate(_legacy_projectable_plan(plan), target_profile, baseline=baseline)
    p = plan["plan"]
    program_id = f"rp_{uuid.uuid4().hex[:12]}"
    rules: list[dict[str, Any]] = []
    compile_report = {
        "program_id": program_id,
        "plan_id": p.get("plan_id"),
        "baseline": baseline,
        "rules": [],
        "legacy_projection": True,
        "errors": [],
    }

    for inj_idx, inj in enumerate(p.get("injections", [])):
        primitive_meta = primitive_index.get(inj["primitive"], {})
        primitive_recipe = primitive_recipes.get(inj["primitive"], {})
        bindings = _bindings_for_injection(inj, primitive_recipe)
        for binding_idx, binding in enumerate(bindings):
            rule = _compile_binding(
                inj,
                binding,
                priority=1000 + inj_idx * 100 + binding_idx,
                primitive_meta=primitive_meta,
                primitive_recipe=primitive_recipe,
            )
            rules.append(rule)
            compile_report["rules"].append(
                {
                    "injection_id": inj["id"],
                    "binding_id": rule.get("binding_id"),
                    "primitive": inj["primitive"],
                    "surface": rule.get("surface"),
                    "loader": rule.get("loader"),
                    "hook": rule.get("hook"),
                    "target": rule.get("target"),
                    "effect_operations": [e["operation"] for e in rule["effects"]],
                    "legacy_adapter": False,
                    "recipe_status": rule.get("recipe_status"),
                    "target_dims": inj.get("target_dims", []),
                    "trajectory_anchor": rule.get("attribution", {}).get("trajectory_anchor"),
                }
            )

    # Keep instrumentation in the program so the new runtime has explicit layers.
    _validate_conflict_policy(rules, p.get("meta", {}).get("max_primitives_per_response", 2))
    rules = _instrumentation_rules_from_legacy_config(legacy_config) + rules
    program = {
        "schema_version": "0.2.0",
        "program_id": program_id,
        "episode_id": legacy_config["episode_id"],
        "task_id": p["task_mode"],
        "baseline": baseline,
        "target": legacy_config.get("target", {}),
        "source": {"kind": "deception_plan", "plan_id": p.get("plan_id")},
        "max_primitives_per_response": p.get("meta", {}).get("max_primitives_per_response", 2),
        "rules": rules,
        "legacy_config": legacy_config,
    }
    validate_runtime_program(program)
    validate_deception_config(legacy_config)
    return program, legacy_config, compile_report


def compile_runtime_program_files(
    plan_path: str | Path,
    runtime_program_path: str | Path,
    legacy_config_path: str | Path | None = None,
    compile_report_path: str | Path | None = None,
    baseline: str = "B3",
    target_profile_path: str | Path | None = None,
) -> dict[str, Any]:
    """File-based compiler entrypoint used by scaffold/validator stages."""
    with open(plan_path, "r", encoding="utf-8") as f:
        plan = yaml.safe_load(f)
    if target_profile_path is not None:
        with open(target_profile_path, "r", encoding="utf-8") as f:
            target_profile = yaml.safe_load(f)
    else:
        raise ValueError("target_profile_path is required for v2 compilation")

    program, legacy_config, report = compile_runtime_program(plan, target_profile, baseline=baseline)

    _write_yaml(runtime_program_path, program)
    if legacy_config_path is not None:
        _write_yaml(legacy_config_path, legacy_config)
    if compile_report_path is not None:
        with open(compile_report_path, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2)
    return program


def _bindings_for_injection(
    inj: dict[str, Any],
    primitive_recipe: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    if inj.get("bindings"):
        return [dict(binding) for binding in inj.get("bindings") or []]

    transform = inj.get("transform") or {}
    surface = infer_surface(transform, primitive_recipe)
    return [
        {
            "binding_id": "default",
            "surface": surface,
            "loader": infer_loader(surface, transform),
            "hook": "response",
            "selector": inj.get("trigger") or {},
            "target": infer_target(transform, surface),
            "transform": transform,
            "trajectory_anchor": inj.get("trajectory_anchor"),
        }
    ]


def _compile_binding(
    inj: dict[str, Any],
    binding: dict[str, Any],
    priority: int,
    primitive_meta: dict[str, Any] | None = None,
    primitive_recipe: dict[str, Any] | None = None,
) -> dict[str, Any]:
    primitive_meta = primitive_meta or {}
    primitive_recipe = primitive_recipe or {}
    transform = binding.get("transform") or inj.get("transform") or {}
    injected_values = transform.get("injected_values") or {}
    binding_id = binding.get("binding_id") or "default"
    _validate_recipe_contract(
        inj,
        primitive_recipe,
        transform=transform,
        state_machine=binding.get("state_machine") or inj.get("state_machine"),
        binding_id=binding_id,
    )
    _reject_c9_links(inj["id"], injected_values)
    effect_inj = dict(inj)
    effect_inj["transform"] = transform
    if binding_id != "default":
        effect_inj["id"] = f"{inj['id']}.{binding_id}"
    if binding.get("state_machine"):
        effect_inj["state_machine"] = binding["state_machine"]
    effects = _effects_from_transform(transform, effect_inj, primitive_recipe, target=binding.get("target"))
    if not effects:
        raise ValueError(f"injection '{inj['id']}' binding '{binding_id}' compiles to no effects")

    selector = binding.get("selector") or inj.get("trigger") or {}
    if not selector.get("path_regex"):
        raise ValueError(f"injection '{inj['id']}' binding '{binding_id}' requires selector.path_regex")

    rule = {
        "rule_id": _rule_id(inj["id"], binding_id),
        "layer": "deception_perturbation",
        "priority": priority,
        "injection_id": inj["id"],
        "binding_id": binding_id,
        "primitive": inj["primitive"],
        "attack_face": inj.get("attack_face") or primitive_meta.get("attack_face"),
        "family": primitive_meta.get("family"),
        "wiki_line_range": primitive_meta.get("wiki_line_range"),
        "coupling": inj.get("coupling", "schema_coupled"),
        "surface": binding.get("surface"),
        "loader": binding.get("loader"),
        "hook": binding.get("hook", "response"),
        "target": binding.get("target"),
        "source": "deception_plan",
        "match": {
            "path_regex": selector["path_regex"],
            "methods": selector.get("methods") or ["GET"],
        },
        "guards": _guards_for_injection(primitive_recipe),
        "effects": effects,
        "recipe_status": primitive_recipe.get("execution_status", "plan_inline_transform"),
        "attribution": {
            "target_dims": inj.get("target_dims", []),
            "side_dims": inj.get("side_dims", []),
            "trajectory_anchor": binding.get("trajectory_anchor") or inj.get("trajectory_anchor"),
            "rationale": inj.get("rationale"),
            "binding_rationale": binding.get("rationale"),
        },
        "conflict_policy": {"on_same_target": "priority_order"},
    }
    if selector.get("request_body_match"):
        rule["match"]["request_body_match"] = selector["request_body_match"]
    if selector.get("every_n_calls"):
        rule["match"]["every_n_calls"] = selector["every_n_calls"]
    params = transform.get("params") or {}
    if params.get("allow_conflict"):
        rule["conflict_policy"]["allow_conflict"] = True
    if params.get("allow_clobber"):
        rule["conflict_policy"]["allow_clobber"] = True
    return rule


def _effects_from_transform(
    transform: dict[str, Any],
    inj: dict[str, Any],
    recipe: dict[str, Any] | None = None,
    target: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    transform_type = transform.get("type")
    values = transform.get("injected_values") or {}
    effects: list[dict[str, Any]] = []
    params = transform.get("params", {}) or {}

    renderer_name = params.get("renderer") or (recipe or {}).get("renderer")
    if transform_type == "recipe_render" or renderer_name:
        if not renderer_name:
            raise ValueError(f"injection '{inj['id']}': recipe_render requires transform.params.renderer or recipe.renderer")
        return render_effects(str(renderer_name), inj, recipe or {})

    if target and target.get("kind") == "header" and transform_type in {"augment", "replace"}:
        header_name = str(target.get("header") or "")
        for key, value in values.items():
            effect_target = header_name if header_name and header_name != "*" else str(key)
            effects.append(
                {
                    "effect_id": f"{inj['id']}.set_header.{effect_target}",
                    "operation": "set_header",
                    "target": effect_target,
                    "value": value,
                }
            )
    elif transform_type == "replace":
        effects.append(
            {
                "effect_id": f"{inj['id']}.replace_body",
                "operation": "replace_json_body",
                "value": values,
                "clobbers": transform.get("response_fields", []),
            }
        )
    elif transform_type == "augment":
        for key, value in values.items():
            op = "merge_json_object" if isinstance(value, dict) else "set_json_field"
            effects.append(
                {
                    "effect_id": f"{inj['id']}.augment.{key}",
                    "operation": op,
                    "target": _json_target(key),
                    "value": value,
                }
            )
    elif transform_type == "stateful_replace":
        sm = inj.get("state_machine") or {}
        params = sm.get("params") or {}
        effects.append(
            {
                "effect_id": f"{inj['id']}.stateful_replace",
                "operation": "stateful_response",
                "state_key": sm.get("name", inj["id"]),
                "states": params.get("states") or params.get("cycle") or [],
                "value": values,
            }
        )
    elif transform_type == "synthetic_response":
        effects.append(
            {
                "effect_id": f"{inj['id']}.synthetic_response",
                "operation": "synthetic_response",
                "status": params.get("status", 200),
                "content_type": params.get("content_type"),
                "value": values,
            }
        )
    else:
        raise ValueError(f"injection '{inj['id']}': unsupported transform.type '{transform_type}'")

    for field in transform.get("removed_fields") or []:
        field = str(field)
        if field.lower().startswith("header:"):
            effects.append(
                {
                    "effect_id": f"{inj['id']}.remove_header.{field[7:]}",
                    "operation": "remove_header",
                    "target": field[7:],
                }
            )
        else:
            effects.append(
                {
                    "effect_id": f"{inj['id']}.remove_field.{field}",
                    "operation": "remove_json_field",
                    "target": _json_target(field),
                }
            )
    return effects


def _instrumentation_rules_from_legacy_config(cfg: dict[str, Any]) -> list[dict[str, Any]]:
    from atobench.runtime_ir.models import build_legacy_runtime_program

    legacy_program = build_legacy_runtime_program(cfg, active_primitives=[])
    return legacy_program["rules"]


def _validate_plan_primitives(plan: dict[str, Any], primitive_index: dict[str, dict[str, Any]]) -> None:
    known = set(primitive_index) | set(LEGACY_TRANSFORMER_PRIMITIVES)
    for inj in plan["plan"].get("injections", []):
        name = inj.get("primitive")
        if name not in known:
            raise ValueError(
                f"injection '{inj.get('id')}': unknown primitive '{name}'. "
                "Expected a name from primitive_index.yaml or legacy transformer registry."
            )


def _legacy_projectable_plan(plan: dict[str, Any]) -> dict[str, Any]:
    """Return a plan copy containing only legacy-config-projectable primitives."""
    import copy

    out = copy.deepcopy(plan)
    injections = out["plan"].get("injections", [])
    out["plan"]["injections"] = [
        inj for inj in injections
        if inj.get("primitive") in LEGACY_TRANSFORMER_PRIMITIVES
        and inj.get("trigger")
        and inj.get("transform")
    ]
    if len(out["plan"]["injections"]) != len(injections):
        skipped = [
            inj.get("primitive") for inj in injections
            if inj.get("primitive") not in LEGACY_TRANSFORMER_PRIMITIVES
        ]
        out["plan"].setdefault("meta", {})["unimplemented_wiki_primitives"] = skipped
    return out


def _guards_for_injection(recipe: dict[str, Any]) -> list[dict[str, Any]]:
    guards = recipe.get("default_guards")
    if not guards:
        guards = ["no_cve_links"]
    return [{"guard": str(g)} for g in guards]


def _validate_recipe_contract(
    inj: dict[str, Any],
    recipe: dict[str, Any],
    transform: dict[str, Any] | None = None,
    state_machine: dict[str, Any] | None = None,
    binding_id: str | None = None,
) -> None:
    if not recipe:
        return
    transform = transform or inj.get("transform") or {}
    transform_type = transform.get("type")
    supported = recipe.get("supported_transform_types") or []
    if supported and transform_type not in supported:
        scope = f" binding '{binding_id}'" if binding_id else ""
        raise ValueError(
            f"injection '{inj.get('id')}'{scope}: transform.type '{transform_type}' is not "
            f"supported by recipe {inj.get('primitive')} (supported={supported})"
        )

    available = set()
    available.update((transform.get("params") or {}).keys())
    available.update((transform.get("injected_values") or {}).keys())
    available.update((state_machine or inj.get("state_machine") or {}).get("params", {}).keys())
    for required in recipe.get("required_parameters") or []:
        if required not in available:
            scope = f" binding '{binding_id}'" if binding_id else ""
            raise ValueError(
                f"injection '{inj.get('id')}'{scope}: missing recipe parameter '{required}' "
                f"for primitive {inj.get('primitive')}"
            )


def _rule_id(injection_id: str, binding_id: str) -> str:
    if binding_id == "default":
        return f"rule_{injection_id}"
    raw = f"rule_{injection_id}_{binding_id}"
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", raw)


def _validate_conflict_policy(rules: list[dict[str, Any]], max_per_response: int) -> None:
    by_match: dict[tuple[str, tuple[str, ...]], list[dict[str, Any]]] = {}
    for rule in rules:
        match = rule.get("match") or {}
        key = (
            match.get("path_regex", ""),
            tuple(sorted(str(m).upper() for m in (match.get("methods") or ["GET"]))),
        )
        by_match.setdefault(key, []).append(rule)

    for key, group in by_match.items():
        unique_injections = {r.get("injection_id") for r in group}
        if len(unique_injections) > max_per_response:
            ids = sorted(str(i) for i in unique_injections)
            raise ValueError(
                f"conflict policy violation: {len(unique_injections)} injections match {key}, "
                f"max_primitives_per_response={max_per_response}: {ids}"
            )
        target_writers: dict[str, str] = {}
        clobber_rules = []
        for rule in group:
            allow_conflict = (rule.get("conflict_policy") or {}).get("allow_conflict")
            allow_clobber = (rule.get("conflict_policy") or {}).get("allow_clobber")
            for effect in rule.get("effects") or []:
                op = effect.get("operation")
                if op in {"replace_json_body", "synthetic_response"}:
                    clobber_rules.append(rule)
                target = _effect_target_key(effect)
                if not target or allow_conflict:
                    continue
                previous = target_writers.get(target)
                if previous and previous != rule.get("injection_id"):
                    raise ValueError(
                        f"conflict policy violation: injections {previous!r} and "
                        f"{rule.get('injection_id')!r} both write {target} on {key}; "
                        "set transform.params.allow_conflict=true only if intentional"
                    )
                target_writers[target] = rule.get("injection_id", "")
            if clobber_rules and len(group) > 1 and not allow_clobber:
                ids = [r.get("injection_id") for r in group]
                raise ValueError(
                    f"conflict policy violation: replace/synthetic response clobber on {key} "
                    f"with multiple rules {ids}; set transform.params.allow_clobber=true "
                    "on the clobbering injection only if intentional"
                )


def _effect_target_key(effect: dict[str, Any]) -> str | None:
    op = effect.get("operation")
    if op in {"set_json_field", "remove_json_field", "merge_json_object"}:
        return f"json:{effect.get('target', '$')}"
    if op in {"set_header", "remove_header"}:
        return f"header:{str(effect.get('target', '')).lower()}"
    if op in {"replace_json_body", "synthetic_response", "stateful_response"}:
        return "body:*"
    return None


def _json_target(field: str) -> str:
    return field if field.startswith("$") else f"$.{field}"


def _reject_c9_links(injection_id: str, values: dict[str, Any]) -> None:
    links = values.get("_links")
    if not isinstance(links, dict):
        return
    forbidden = [key for key in links if str(key).lower() in {"cve", "advisory"}]
    if forbidden:
        raise ValueError(
            f"injection '{injection_id}' violates C9 realism: forbidden _links keys {forbidden}"
        )


def _write_yaml(path: str | Path, obj: dict[str, Any]) -> None:
    with open(path, "w", encoding="utf-8") as f:
        yaml.safe_dump(obj, f, sort_keys=False, allow_unicode=True)
