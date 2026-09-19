"""Stateful renderer effects."""

from __future__ import annotations

from typing import Any


def render_stateful_sequence(injection: dict[str, Any], recipe: dict[str, Any]) -> list[dict[str, Any]]:
    sm = injection.get("state_machine") or {}
    params = sm.get("params") or {}
    states = params.get("states") or params.get("cycle") or []
    if not states:
        states = [(injection.get("transform") or {}).get("injected_values") or {}]
    return [
        {
            "effect_id": f"{injection['id']}.stateful_sequence",
            "operation": "stateful_response",
            "state_key": sm.get("name", injection["id"]),
            "states": states,
            "value": (injection.get("transform") or {}).get("injected_values") or {},
            "renderer": "stateful_sequence",
        }
    ]
