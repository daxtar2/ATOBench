"""runtime_implementability — verify plan would actually fire on inventory paths.

L3 fix 2026-07-07: detects "configured but never fires" bugs BEFORE running an
episode. Catches bugs like:
  - 2 primitives on same path_regex but pattern_matcher only returns first
    (L1 bug — caught here because 2nd primitive would have 0 fire paths)
  - path_regex doesn't match any inventory path (planner typo or wrong path)
  - primitive's methods list excludes the method agent actually uses

The skill's step 5 gate calls this between planner output and B3 run.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import yaml

from atobench.proxy.pattern_matcher import find_all_matches
from atobench.proxy.flow import HTTPFlow, Request, Response


def _load_inventory(inventory_path: Path | str) -> list[dict[str, Any]]:
    """Load endpoint_inventory.jsonl — one endpoint per line."""
    out: list[dict[str, Any]] = []
    path = Path(inventory_path)
    if not path.exists():
        return out
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


def _inventory_to_flows(inventory: list[dict[str, Any]]) -> list[HTTPFlow]:
    """Convert inventory entries to synthetic HTTPFlow objects for pattern_matcher."""
    flows: list[HTTPFlow] = []
    for entry in inventory:
        path = entry.get("path", "/")
        method = entry.get("method", "GET").upper()
        flow = HTTPFlow(
            request=Request(method=method, path=path, headers={}, body=None, json=None),
            response=Response(status_code=200, headers={}, body=None, json={}),
        )
        flow.episode_id = "implementability_check"
        flows.append(flow)
    return flows


def _load_plan_primitives(plan_path: Path | str) -> list[dict[str, Any]]:
    """Load plan injections and translate to proxy primitive spec shape
    (plan_to_config.translate does the full job; here we do minimal translation
    just enough for pattern_matcher)."""
    with open(plan_path, "r", encoding="utf-8") as f:
        plan = yaml.safe_load(f)
    injections = plan.get("plan", {}).get("injections", [])

    primitives: list[dict[str, Any]] = []
    for inj in injections:
        trigger = inj.get("trigger", {})
        transform = inj.get("transform", {})
        prim = {
            "name": inj.get("primitive"),
            "coupling": inj.get("coupling"),
            "match": {
                "path_regex": trigger.get("path_regex", ""),
                "methods": trigger.get("methods") or ["GET"],
                "response_fields": transform.get("response_fields", []),
            },
            "fake_values": transform.get("injected_values", {}),
        }
        primitives.append(prim)
    return primitives


def check_implementability(
    plan_path: Path | str,
    inventory_path: Path | str,
    max_primitives_per_response: int = 2,
) -> dict[str, Any]:
    """Verify every primitive in plan would fire at least once on inventory paths.

    Args:
        plan_path: path to deception_plan.yaml
        inventory_path: path to endpoint_inventory.jsonl (recon output)
        max_primitives_per_response: cap from plan meta (default 2)

    Returns:
        {
          "all_implementable": bool,
          "total_primitives": int,
          "total_inventory_paths": int,
          "primitive_fire_counts": {prim_name: n_inventory_paths_it_fires_on},
          "unimplementable_primitives": [prim_names with 0 fire paths],
          "multi_primitive_paths": [inventory paths where >=2 primitives fire],
          "warnings": [str],
        }
    """
    primitives = _load_plan_primitives(plan_path)
    inventory = _load_inventory(inventory_path)
    flows = _inventory_to_flows(inventory)

    fire_counts: dict[str, int] = {p["name"]: 0 for p in primitives}
    multi_paths: list[dict[str, Any]] = []
    warnings: list[str] = []

    for flow, inv_entry in zip(flows, inventory):
        matches = find_all_matches(flow, primitives, max_primitives_per_response)
        if len(matches) >= 2:
            multi_paths.append({
                "path": flow.request.path,
                "method": flow.request.method,
                "primitives": [m.primitive_name for m in matches],
            })
        for m in matches:
            if m.primitive_name:
                fire_counts[m.primitive_name] = fire_counts.get(m.primitive_name, 0) + 1

    unimplementable = [name for name, count in fire_counts.items() if count == 0]

    # Warnings for primitives with very few fire paths (fragile)
    for name, count in fire_counts.items():
        if count == 0:
            warnings.append(f"primitive '{name}' would NEVER fire on any inventory path — plan is unimplementable as configured")
        elif count == 1 and len(flows) > 5:
            warnings.append(f"primitive '{name}' fires on only 1 inventory path — fragile (agent may not visit that exact path)")

    # Warning for multi-primitive paths (L1.5 territory — make sure _mark merge works)
    if multi_paths:
        warnings.append(
            f"{len(multi_paths)} inventory paths have multi-primitive-per-response "
            f"(>=2 primitives fire on same path) — verify L1.5 fix is in place "
            f"(deception_tag.primitives_fired must record ALL fired primitives)"
        )

    return {
        "all_implementable": len(unimplementable) == 0,
        "total_primitives": len(primitives),
        "total_inventory_paths": len(inventory),
        "primitive_fire_counts": fire_counts,
        "unimplementable_primitives": unimplementable,
        "multi_primitive_paths": multi_paths,
        "warnings": warnings,
    }


def check_implementability_files(
    plan_path: str,
    inventory_path: str,
    output_path: str | None = None,
) -> dict[str, Any]:
    """Convenience: load files, run check, optionally write JSON report."""
    result = check_implementability(plan_path, inventory_path)
    if output_path:
        Path(output_path).write_text(
            json.dumps(result, indent=2, ensure_ascii=False, default=str),
            encoding="utf-8",
        )
    return result
