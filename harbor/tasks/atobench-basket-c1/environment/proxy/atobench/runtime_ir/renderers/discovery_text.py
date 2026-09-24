"""Discovery text renderer effects for robots/sitemap/security.txt-style endpoints."""

from __future__ import annotations

from typing import Any


def render_discovery_text(injection: dict[str, Any], recipe: dict[str, Any]) -> list[dict[str, Any]]:
    params = (injection.get("transform") or {}).get("params") or {}
    body = params.get("body") or (injection.get("transform") or {}).get("injected_values", {}).get("body")
    if not isinstance(body, str):
        removed = params.get("fake_removed_paths") or []
        narrative = params.get("narrative", "deprecated")
        body = "\n".join(f"Disallow: {p}" for p in removed) + f"\n# {narrative}"
    return [
        {
            "effect_id": f"{injection['id']}.discovery_text",
            "operation": "synthetic_response",
            "status": params.get("status", 200),
            "content_type": params.get("content_type", "text/plain"),
            "value": {"body": body},
            "renderer": "discovery_text",
        }
    ]
