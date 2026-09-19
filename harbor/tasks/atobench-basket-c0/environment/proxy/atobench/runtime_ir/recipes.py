"""Primitive catalog and recipe loading for RuntimeProgram compilation."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from atobench.runtime_ir.surfaces import LOADERS, SURFACES, SURFACE_DEFAULT_LOADER, TARGET_KINDS

REPO_ROOT = Path(__file__).resolve().parents[2]
DECEPTION_FRAME_DIR = REPO_ROOT / "atobench" / "deception_frame"
PRIMITIVE_INDEX_PATH = DECEPTION_FRAME_DIR / "primitive_index.yaml"
PRIMITIVE_RECIPES_PATH = DECEPTION_FRAME_DIR / "primitive_recipes.yaml"

LEGACY_TRANSFORMER_PRIMITIVES = {
    "fake_version_banner",
    "vuln_symptom_inject",
    "no_vuln_gaslighting",
    "corrupt_belief",
    "poison_dependency",
    "false_completion",
    "exhaustion_trap",
    "induce_loop",
    "substitute_subgoal",
    "preemptive_ftp_gaslight",
    "cross_turn_jwt_escalation",
    "hardcoded_cred_comment",
    "decoy_sql_search",
    "cross_turn_jwt_escalation_v2",
    "decoy_sql_search_v2",
}


def load_primitive_index(path: str | Path = PRIMITIVE_INDEX_PATH) -> dict[str, dict[str, Any]]:
    """Load primitive_index.yaml as name -> metadata."""
    p = Path(path)
    if not p.exists():
        return {}
    with open(p, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return {
        item["name"]: dict(item)
        for item in data.get("primitives", [])
        if isinstance(item, dict) and item.get("name")
    }


def load_primitive_recipes(
    path: str | Path = PRIMITIVE_RECIPES_PATH,
    include_index_defaults: bool = True,
) -> dict[str, dict[str, Any]]:
    """Load executable recipes as name -> recipe.

    Explicit recipes override auto-derived index defaults. Auto-derived recipes
    make every primitive in primitive_index.yaml queryable and compilable when
    the planner supplies a structured transform.
    """
    recipes: dict[str, dict[str, Any]] = {}
    if include_index_defaults:
        for name, meta in load_primitive_index().items():
            recipes[name] = derive_recipe_from_index(meta)

    p = Path(path)
    if p.exists():
        with open(p, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        for item in data.get("recipes", []):
            if isinstance(item, dict) and item.get("name"):
                base = recipes.get(item["name"], {})
                merged = {**base, **dict(item)}
                merged.setdefault("derived_from_index", bool(base))
                recipes[item["name"]] = merged
    for name, recipe in list(recipes.items()):
        recipes[name] = with_surface_binding_defaults(recipe)
    return recipes


def derive_recipe_from_index(meta: dict[str, Any]) -> dict[str, Any]:
    """Derive a default executable recipe from one primitive_index entry."""
    state = meta.get("state", "stateless")
    state_machine = meta.get("state_machine")
    has_realism = bool(meta.get("has_realism_constraints"))
    default_transform = "augment"
    supported = ["augment", "replace", "synthetic_response"]
    operations = ["set_json_field", "merge_json_object", "replace_json_body", "synthetic_response"]
    if state == "stateful":
        default_transform = "stateful_replace" if state_machine else "synthetic_response"
        supported = ["stateful_replace", "synthetic_response", "augment"]
        operations = ["stateful_response", "synthetic_response", "set_json_field", "merge_json_object"]

    return {
        "name": meta["name"],
        "family": meta.get("family"),
        "attack_face": meta.get("attack_face"),
        "state": state,
        "state_machine": state_machine,
        "effect_dims": list(meta.get("effect_dims") or []),
        "fits_endpoint_types": list(meta.get("fits_endpoint_types") or []),
        "applicability_tier": meta.get("applicability_tier"),
        "evidence_tier": meta.get("evidence_tier"),
        "wiki_line_range": list(meta.get("wiki_line_range") or []),
        "execution_status": "index_default_recipe",
        "derived_from_index": True,
        "default_transform_type": default_transform,
        "supported_transform_types": supported,
        "supported_operations": operations,
        "default_guards": ["no_cve_links"] if has_realism else [],
        "runtime_requirements": [
            "trigger.path_regex",
            "trigger.methods",
            "transform.type",
            "transform.injected_values or transform.removed_fields",
            "trajectory_anchor",
        ],
        "content_parameters": [
            "transform.injected_values",
            "transform.params.content_type",
            "transform.params.status",
            "state_machine.params.states",
        ],
    }


def with_surface_binding_defaults(recipe: dict[str, Any]) -> dict[str, Any]:
    """Attach planner-facing surface and binding templates to a recipe."""
    out = dict(recipe)
    template = derive_surface_binding_template(out)
    out.setdefault("suggested_surfaces", template["suggested_surfaces"])
    out.setdefault("default_bindings", template["default_bindings"])
    out.setdefault("binding_template_version", "0.1.0")
    return out


def derive_surface_binding_template(recipe: dict[str, Any]) -> dict[str, Any]:
    """Derive generic binding templates from primitive metadata.

    These templates are intentionally not silently executable. They provide the
    multi-agent planner with stable surface/loader/target defaults while still
    requiring target-specific path_regex and payload values in the final plan.
    """
    profiles = _surface_profiles_for_recipe(recipe)
    bindings = []
    for idx, profile in enumerate(profiles):
        bindings.append(_binding_template(recipe, profile, idx))
    return {
        "suggested_surfaces": [p["surface"] for p in profiles],
        "default_bindings": bindings,
    }


def _surface_profiles_for_recipe(recipe: dict[str, Any]) -> list[dict[str, str]]:
    name = str(recipe.get("name", ""))
    family = str(recipe.get("family", ""))
    endpoints = set(str(e) for e in (recipe.get("fits_endpoint_types") or []))
    state = recipe.get("state")
    state_machine = recipe.get("state_machine")

    overrides = _NAME_SURFACE_OVERRIDES.get(name)
    if overrides:
        return [_profile(surface, target_kind=target_kind, target_path=target_path) for surface, target_kind, target_path in overrides]

    if family == "M1":
        return [_profile("discovery_document", "text_body", "$")]
    if family == "M2":
        if "graphql" in name:
            return [_profile("discovery_document", "json_body", "$.__schema")]
        if "error" in name or "validation_error" in name:
            return [_profile("failure_signal", "json_body", "$")]
        return [_profile("discovery_document", "json_body", "$.paths")]
    if family == "M3":
        if "jwks" in name:
            return [_profile("structured_payload", "json_body", "$.keys")]
        if "version" in name or "banner" in name:
            return [
                _profile("metadata", "header", "Server"),
                _profile("structured_payload", "json_body", "$"),
            ]
        if state == "stateful" or state_machine:
            return [_profile("progress_state", "json_body", "$")]
        return [_profile("structured_payload", "json_body", "$")]
    if family == "M4":
        if "websocket" in endpoints or "websocket" in name:
            return [_profile("interactive_session", "synthetic_endpoint", "$")]
        return [_profile("progress_state", "json_body", "$")]
    if family == "M5":
        return [_profile("interactive_session", "json_body", "$")]
    if family == "M6":
        if any(token in name for token in ["cors", "cookie", "header"]):
            return [_profile("metadata", "header", "*")]
        if any(token in name for token in ["comment", "ftp", "artifact"]):
            return [_profile("document_text", "text_body", "$")]
        if state == "stateful" or state_machine:
            return [_profile("interactive_session", "json_body", "$")]
        return [_profile("failure_signal", "json_body", "$")]
    if family == "M7":
        if state == "stateful" or state_machine:
            return [_profile("interactive_session", "json_body", "$")]
        return [_profile("structured_payload", "json_body", "$")]
    if family == "M8":
        if "upload" in endpoints or "upload" in name:
            return [_profile("interactive_session", "json_body", "$")]
        if "debug" in endpoints:
            return [_profile("document_text", "text_body", "$")]
        return [_profile("structured_payload", "json_body", "$")]
    if family == "M9":
        if any(token in name for token in ["content_type", "etag", "rate_limit"]):
            return [_profile("metadata", "header", "*")]
        if state == "stateful" or state_machine:
            return [_profile("progress_state", "json_body", "$")]
        return [_profile("structured_payload", "json_body", "$")]
    if family == "M10":
        if "robots" in endpoints or "well_known" in endpoints:
            return [_profile("discovery_document", "text_body", "$")]
        return [_profile("document_text", "text_body", "$")]

    if state == "stateful" or state_machine:
        return [_profile("interactive_session", "json_body", "$")]
    return [_profile("structured_payload", "json_body", "$")]


def _profile(surface: str, target_kind: str, target_path: str) -> dict[str, str]:
    loader = SURFACE_DEFAULT_LOADER.get(surface, "structured")
    if target_kind == "json_body" and surface in {"discovery_document", "failure_signal", "enumeration_listing"}:
        loader = "structured"
    if target_kind in {"text_body", "html_body"} and surface in {"failure_signal", "enumeration_listing"}:
        loader = "document"
    return {
        "surface": surface if surface in SURFACES else "structured_payload",
        "loader": loader if loader in LOADERS else "structured",
        "target_kind": target_kind if target_kind in TARGET_KINDS else "json_body",
        "target_path": target_path,
    }


def _binding_template(recipe: dict[str, Any], profile: dict[str, str], idx: int) -> dict[str, Any]:
    binding_id = _binding_id(profile["surface"], idx)
    target = {"kind": profile["target_kind"]}
    if profile["target_kind"] == "header":
        target["header"] = profile["target_path"]
    else:
        target["path"] = profile["target_path"]
    content_type = _content_type_for_target(profile["target_kind"])
    if content_type:
        target["content_type"] = content_type

    transform = _transform_template(recipe, profile)
    binding: dict[str, Any] = {
        "binding_id": binding_id,
        "surface": profile["surface"],
        "loader": profile["loader"],
        "hook": "response",
        "selector": {
            "endpoint_types": list(recipe.get("fits_endpoint_types") or []),
            "path_regex": "<target_path_regex>",
            "methods": _methods_for_recipe(recipe),
        },
        "target": target,
        "transform": transform,
        "notes": "Template for planner use; replace placeholders with target-specific selector and payload values.",
    }
    if recipe.get("state") == "stateful" or recipe.get("state_machine"):
        binding["state_machine"] = {
            "name": recipe.get("state_machine") or "<state_machine_name>",
            "params": {"states": ["<state_1>", "<state_2>"]},
        }
    return binding


def _binding_id(surface: str, idx: int) -> str:
    base = {
        "metadata": "metadata",
        "structured_payload": "structured-body",
        "document_text": "document-text",
        "failure_signal": "failure-signal",
        "discovery_document": "discovery-doc",
        "enumeration_listing": "listing",
        "progress_state": "progress-state",
        "interactive_session": "session",
    }.get(surface, "binding")
    return base if idx == 0 else f"{base}-{idx + 1}"


def _content_type_for_target(target_kind: str) -> str | None:
    if target_kind == "json_body":
        return "application/json"
    if target_kind == "text_body":
        return "text/plain"
    if target_kind == "html_body":
        return "text/html"
    return None


def _methods_for_recipe(recipe: dict[str, Any]) -> list[str]:
    endpoints = set(str(e) for e in (recipe.get("fits_endpoint_types") or []))
    name = str(recipe.get("name", ""))
    if endpoints & {"login", "token", "oauth", "submit", "upload", "search", "fetch"}:
        return ["GET", "POST"]
    if any(token in name for token in ["method_override", "upload", "sql_search", "ssrf"]):
        return ["GET", "POST"]
    return ["GET"]


def _transform_template(recipe: dict[str, Any], profile: dict[str, str]) -> dict[str, Any]:
    transform_type = recipe.get("default_transform_type")
    if not transform_type:
        supported = recipe.get("supported_transform_types") or ["augment"]
        transform_type = supported[0]
    out: dict[str, Any] = {"type": transform_type}
    params: dict[str, Any] = {}
    if recipe.get("renderer"):
        params["renderer"] = recipe["renderer"]
    for required in recipe.get("required_parameters") or []:
        params[str(required)] = f"<{required}>"
    if params:
        out["params"] = params

    if transform_type in {"augment", "replace"}:
        if profile["target_kind"] == "header":
            out["removed_fields"] = []
            header = profile["target_path"] if profile["target_path"] != "*" else "<header_name>"
            out["injected_values"] = {header: "<target_specific_value>"}
        else:
            out["response_fields"] = ["<field>"]
            out["injected_values"] = {"<field>": "<target_specific_value>"}
    elif transform_type == "synthetic_response":
        out["injected_values"] = {"body": "<target_specific_body>"}
        out.setdefault("params", {}).setdefault("content_type", _content_type_for_target(profile["target_kind"]))
    elif transform_type == "stateful_replace":
        out["injected_values"] = {"state": "<target_specific_state_value>"}
    elif transform_type == "recipe_render":
        out.setdefault("params", {})
    return out


_NAME_SURFACE_OVERRIDES: dict[str, list[tuple[str, str, str]]] = {
    "fake_version_banner": [
        ("metadata", "header", "Server"),
        ("structured_payload", "json_body", "$"),
    ],
    "metrics_endpoint_fake_vulns": [("structured_payload", "json_body", "$")],
    "jwks_weak_key_injection": [("structured_payload", "json_body", "$.keys")],
    "fake_jwt_decoded_claim": [("structured_payload", "json_body", "$.claims")],
    "no_vuln_gaslighting": [("failure_signal", "json_body", "$")],
    "hardcoded_cred_comment": [("document_text", "text_body", "$")],
    "fake_cors_misconfiguration": [("metadata", "header", "Access-Control-Allow-Origin")],
    "cookie_attribute_phantom": [("metadata", "header", "Set-Cookie")],
    "security_header_stripping": [("metadata", "header", "*")],
    "preemptive_ftp_gaslight": [("enumeration_listing", "text_body", "$")],
    "auth_scope_downgrade_echo": [("structured_payload", "json_body", "$")],
    "cross_turn_jwt_escalation": [("interactive_session", "json_body", "$.claims")],
    "decoy_sql_search": [("structured_payload", "json_body", "$")],
    "method_override_decoy": [("metadata", "header", "X-HTTP-Method-Override")],
    "ssrf_egress_echo": [("structured_payload", "json_body", "$")],
    "rate_limit_gaslighting": [("metadata", "header", "Retry-After")],
    "conditional_request_etag_loop": [("metadata", "header", "ETag")],
    "partial_success_5xx": [("failure_signal", "json_body", "$")],
    "content_type_lie": [("metadata", "header", "Content-Type")],
    "content_negotiation_misdirect": [("metadata", "header", "Content-Type")],
    "canary_honeypot_warning": [("document_text", "text_body", "$")],
    "legalese_robots_deterrent": [("discovery_document", "text_body", "$")],
    "pii_gaslight_deterrent": [("document_text", "text_body", "$")],
    "exploit_breadcrumb_bait": [("document_text", "text_body", "$")],
    "honeypot_admin_login": [("interactive_session", "json_body", "$")],
    "false_completion": [("progress_state", "json_body", "$")],
    "substitute_subgoal": [("structured_payload", "json_body", "$._links")],
    "flag_response_redaction": [("failure_signal", "json_body", "$")],
}


def query_primitive_recipes(
    *,
    family: str | None = None,
    attack_face: str | None = None,
    effect_dim: str | None = None,
    endpoint_type: str | None = None,
    state: str | None = None,
) -> list[dict[str, Any]]:
    """Return recipes matching planner/subagent query constraints."""
    recipes = load_primitive_recipes()
    out = []
    for recipe in recipes.values():
        if family and recipe.get("family") != family:
            continue
        if attack_face and recipe.get("attack_face") != attack_face:
            continue
        if state and recipe.get("state") != state:
            continue
        if effect_dim and effect_dim not in (recipe.get("effect_dims") or []):
            continue
        if endpoint_type and endpoint_type not in (recipe.get("fits_endpoint_types") or []):
            continue
        out.append(recipe)
    return sorted(out, key=lambda r: r.get("name", ""))


def known_primitive_names() -> set[str]:
    """Union of legacy transformer names and primitive_index names."""
    return set(LEGACY_TRANSFORMER_PRIMITIVES) | set(load_primitive_index())
