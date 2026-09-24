"""OpenAPI renderer effects."""

from __future__ import annotations

from typing import Any


def render_openapi_patch(injection: dict[str, Any], recipe: dict[str, Any]) -> list[dict[str, Any]]:
    params = (injection.get("transform") or {}).get("params") or {}
    values = (injection.get("transform") or {}).get("injected_values") or {}
    fake_path = params.get("fake_path") or values.get("fake_path")
    fake_method = (params.get("fake_method") or values.get("fake_method") or "get").lower()
    fake_parameters = params.get("fake_parameters") or values.get("fake_parameters") or []
    removed_paths = params.get("removed_paths") or values.get("removed_paths") or []
    if not fake_path and not removed_paths:
        raise ValueError(f"injection '{injection.get('id')}': openapi_patch requires fake_path or removed_paths")

    effects: list[dict[str, Any]] = []
    for path in removed_paths:
        effects.append(
            {
                "effect_id": f"{injection['id']}.openapi.remove.{path}",
                "operation": "remove_json_field",
                "target": f"$.paths.{path}",
                "renderer": "openapi_patch",
            }
        )
    if fake_path:
        operation = {
            "summary": params.get("summary", "Internal maintenance endpoint"),
            "description": params.get("description", "Internal endpoint exposed by generated specification."),
            "parameters": fake_parameters,
            "responses": params.get("responses") or {"200": {"description": "OK"}},
        }
        effects.append(
            {
                "effect_id": f"{injection['id']}.openapi.add.{fake_path}",
                "operation": "merge_json_object",
                "target": f"$.paths.{fake_path}",
                "value": {fake_method: operation},
                "renderer": "openapi_patch",
            }
        )
    return effects
