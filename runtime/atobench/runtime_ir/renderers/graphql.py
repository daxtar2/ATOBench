"""GraphQL introspection renderer effects."""

from __future__ import annotations

from typing import Any


def render_graphql_introspection(injection: dict[str, Any], recipe: dict[str, Any]) -> list[dict[str, Any]]:
    params = (injection.get("transform") or {}).get("params") or {}
    fake_types = params.get("fake_types") or []
    fake_fields = params.get("fake_fields") or []
    fake_mutations = params.get("fake_mutations") or []
    if not (fake_types or fake_fields or fake_mutations):
        raise ValueError(f"injection '{injection.get('id')}': graphql_introspection requires fake_types/fake_fields/fake_mutations")
    value = {}
    if fake_types:
        value.setdefault("__schema", {})["types"] = fake_types
    if fake_fields:
        value.setdefault("__schema", {}).setdefault("queryType", {})["fields"] = fake_fields
    if fake_mutations:
        value.setdefault("__schema", {}).setdefault("mutationType", {})["fields"] = fake_mutations
    return [
        {
            "effect_id": f"{injection['id']}.graphql.introspection",
            "operation": "merge_json_object",
            "target": "$.data",
            "value": value,
            "renderer": "graphql_introspection",
        }
    ]
