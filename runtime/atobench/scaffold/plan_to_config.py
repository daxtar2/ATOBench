"""Translate deception_plan.yaml to proxy schema 0.1.0 deception_config.yaml.

This is a compatibility projection for older proxy config consumers. Canonical
v2 execution uses RuntimeProgram and target_profile.yaml.
"""

from __future__ import annotations

import os
from typing import Any

import yaml

from atobench.schema.loader import validate_deception_config, validate_deception_plan


def translate(plan: dict[str, Any], target_profile: dict[str, Any] | None = None, baseline: str = "B3") -> dict[str, Any]:
    """Translate a v2 deception plan plus target profile to legacy config shape."""

    validate_deception_plan(plan)
    p = plan["plan"]
    profile_target = (target_profile or {}).get("target", {}) if isinstance(target_profile, dict) else {}

    ep_id = f"ep_{os.urandom(6).hex()}"
    primitives: list[dict[str, Any]] = []
    stateful_primitives = {"corrupt_belief", "exhaustion_trap", "induce_loop"}

    for inj in p.get("injections", []):
        prim_name = inj["primitive"]
        if prim_name in stateful_primitives and "state_machine" not in inj:
            raise ValueError(
                f"injection '{inj['id']}': primitive '{prim_name}' is stateful "
                "but has no state_machine field"
            )

        trigger = inj.get("trigger") or {}
        transform = inj.get("transform") or {}
        match: dict[str, Any] = {"path_regex": trigger["path_regex"]}
        if trigger.get("methods"):
            match["methods"] = trigger["methods"]
        if transform.get("response_fields"):
            match["response_fields"] = transform["response_fields"]

        prim: dict[str, Any] = {
            "name": prim_name,
            "coupling": inj["coupling"],
            "match": match,
            "fake_values": dict(transform.get("injected_values", {})),
            "stateless": "state_machine" not in inj,
        }
        if "state_machine" in inj:
            sm = inj["state_machine"]
            sm_out: dict[str, Any] = {"type": sm["name"]}
            for key, value in sm.get("params", {}).items():
                if value is not None:
                    sm_out[key] = value
            prim["state_machine"] = sm_out
        if "params" in transform:
            prim["params"] = dict(transform["params"])
        primitives.append(prim)

    target_block: dict[str, Any] = {"base_url": p["target"]["entry_url"]}
    swagger_path = profile_target.get("swagger") or profile_target.get("openapi_path")
    if swagger_path:
        target_block["swagger_path"] = swagger_path

    config: dict[str, Any] = {
        "schema_version": "0.1.0",
        "episode_id": ep_id,
        "task_id": p["task_mode"],
        "baseline": baseline,
        "target": target_block,
        "primitives": primitives,
        "logging": {
            "turns_jsonl": "/logs/turns.jsonl",
            "state_path": f"/logs/state_{ep_id}.json",
        },
    }
    if p.get("meta", {}).get("max_primitives_per_response") is not None:
        config["max_primitives_per_response"] = p["meta"]["max_primitives_per_response"]

    validate_deception_config(config)
    return config


def translate_files(plan_path: str, target_profile_path: str, output_path: str, baseline: str = "B3") -> dict[str, Any]:
    """Read plan + target_profile YAML, write compatibility config YAML."""

    with open(plan_path, "r", encoding="utf-8") as handle:
        plan = yaml.safe_load(handle)
    with open(target_profile_path, "r", encoding="utf-8") as handle:
        target_profile = yaml.safe_load(handle)

    config = translate(plan, target_profile, baseline=baseline)
    with open(output_path, "w", encoding="utf-8") as handle:
        yaml.safe_dump(config, handle, sort_keys=False, allow_unicode=True)
    return config
