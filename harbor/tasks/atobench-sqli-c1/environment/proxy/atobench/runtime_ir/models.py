"""Small helpers for ATOBench Runtime IR dictionaries.

The schemas live in atobench/schema/runtime_program.json and
atobench/schema/runtime_event.json. This module intentionally keeps the runtime
representation as plain dicts because the rest of the scaffold/proxy layer
already passes YAML/JSON-shaped dictionaries.
"""

from __future__ import annotations

import hashlib
import uuid
from typing import Any


LAYER_ORDER = {
    "environment_base": 10,
    "instrumentation": 20,
    "deception_perturbation": 30,
}


def stable_rule_id(prefix: str, payload: dict[str, Any]) -> str:
    raw = repr(sorted(payload.items())).encode("utf-8", errors="replace")
    return f"{prefix}_{hashlib.sha1(raw).hexdigest()[:10]}"


def build_legacy_runtime_program(
    cfg: dict[str, Any],
    active_primitives: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build a RuntimeProgram compatibility wrapper around deception_config.

    Legacy configs have no high-level plan fields, so the wrapper preserves what
    it can and marks the source as `legacy_config`. Deception rules use the
    `legacy_transformer` effect to preserve behavior during migration.
    """
    primitives = active_primitives if active_primitives is not None else cfg.get("primitives", [])
    episode_id = cfg.get("episode_id", "")
    baseline = cfg.get("baseline", "B3")
    rules: list[dict[str, Any]] = []

    flag = cfg.get("flag") or {}
    if flag.get("endpoint") and flag.get("value"):
        payload = {"endpoint": flag["endpoint"], "method": flag.get("method", "GET")}
        rules.append(
            {
                "rule_id": stable_rule_id("inst_flag", payload),
                "layer": "instrumentation",
                "priority": 100,
                "injection_id": "instrumentation.flag",
                "primitive": "instrumentation.flag",
                "source": "legacy_config",
                "match": {
                    "path_regex": f"^{_escape_path(flag['endpoint'])}$",
                    "methods": [flag.get("method", "GET")],
                },
                "effects": [
                    {
                        "effect_id": "inject_real_flag",
                        "operation": "inject_flag",
                        "value": flag["value"],
                    }
                ],
                "attribution": {"target_dims": [], "trajectory_anchor": None},
            }
        )

    for idx, adv in enumerate(cfg.get("link_advertisements") or []):
        endpoint = adv.get("endpoint")
        links = adv.get("links") or {}
        if not endpoint or not links:
            continue
        payload = {"endpoint": endpoint, "idx": idx}
        rules.append(
            {
                "rule_id": stable_rule_id("inst_links", payload),
                "layer": "instrumentation",
                "priority": 110 + idx,
                "injection_id": f"instrumentation.links.{idx}",
                "primitive": "instrumentation.link_advertisement",
                "source": "legacy_config",
                "match": {"path_regex": f"^{_escape_path(endpoint)}$", "methods": ["GET"]},
                "effects": [
                    {
                        "effect_id": "inject_link_advertisement",
                        "operation": "merge_json_object",
                        "target": "$._links",
                        "value": links,
                    }
                ],
                "attribution": {"target_dims": [], "trajectory_anchor": None},
            }
        )

    for idx, prim in enumerate(primitives or []):
        match = dict(prim.get("match") or {})
        rules.append(
            {
                "rule_id": prim.get("id") or stable_rule_id("rule", {"idx": idx, **prim}),
                "layer": "deception_perturbation",
                "priority": 1000 + idx,
                "injection_id": prim.get("injection_id") or prim.get("name") or f"primitive_{idx}",
                "primitive": prim.get("name", ""),
                "coupling": prim.get("coupling", "schema_coupled"),
                "source": "legacy_config",
                "match": match,
                "effects": [
                    {
                        "effect_id": "legacy_transformer",
                        "operation": "legacy_transformer",
                        "primitive_spec": prim,
                    }
                ],
                "attribution": {
                    "target_dims": prim.get("target_dims", []),
                    "trajectory_anchor": prim.get("trajectory_anchor"),
                },
                "conflict_policy": {"max_per_response": cfg.get("max_primitives_per_response", 2)},
            }
        )

    return {
        "schema_version": "0.2.0",
        "program_id": cfg.get("program_id") or f"rp_{uuid.uuid4().hex[:12]}",
        "episode_id": episode_id,
        "task_id": cfg.get("task_id", "T1"),
        "baseline": baseline,
        "target": cfg.get("target", {}),
        "source": {"kind": "legacy_config", "plan_id": cfg.get("plan_id")},
        "max_primitives_per_response": cfg.get("max_primitives_per_response", 2),
        "rules": rules,
        "legacy_config": cfg,
    }


def make_event(
    *,
    episode_id: str,
    turn_idx: int | None,
    rule: dict[str, Any],
    effect: dict[str, Any],
    operation: str,
    status: str = "applied",
    before_ref: Any = None,
    after_ref: Any = None,
    guard_results: list[dict[str, Any]] | None = None,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    attribution = rule.get("attribution") or {}
    return {
        "event_id": f"evt_{uuid.uuid4().hex[:16]}",
        "episode_id": episode_id,
        "turn_idx": turn_idx,
        "rule_id": rule.get("rule_id", ""),
        "injection_id": rule.get("injection_id", ""),
        "binding_id": rule.get("binding_id"),
        "primitive": rule.get("primitive", ""),
        "coupling": rule.get("coupling"),
        "surface": rule.get("surface"),
        "loader": rule.get("loader"),
        "hook": rule.get("hook"),
        "target": rule.get("target"),
        "effect_id": effect.get("effect_id", operation),
        "operation": operation,
        "status": status,
        "layer": rule.get("layer", "deception_perturbation"),
        "target_dims": attribution.get("target_dims") or [],
        "trajectory_anchor": attribution.get("trajectory_anchor"),
        "matched_anchor": _matched_anchor(rule),
        "before_ref": before_ref,
        "after_ref": after_ref,
        "guard_results": guard_results or [],
        "details": details or {},
    }


def event_to_legacy_primitive_entry(event: dict[str, Any]) -> dict[str, Any]:
    """Project a RuntimeEvent into the old primitives_fired entry shape."""
    details = event.get("details") or {}
    u_t = details.get("u_t")
    if not isinstance(u_t, dict):
        u_t = {}
    return {
        "z_t": event.get("primitive", ""),
        "u_t": dict(u_t),
        "D_t_snapshot": dict(details.get("D_t_snapshot") or {}),
        "rule_id": event.get("rule_id", ""),
        "injection_id": event.get("injection_id", ""),
        "binding_id": event.get("binding_id"),
        "effect_id": event.get("effect_id", ""),
    }


def _escape_path(path: str) -> str:
    import re

    return re.escape(path)


def _matched_anchor(rule: dict[str, Any]) -> dict[str, Any]:
    match = rule.get("match") or {}
    return {
        "path_regex": match.get("path_regex"),
        "methods": match.get("methods") or [],
    }
