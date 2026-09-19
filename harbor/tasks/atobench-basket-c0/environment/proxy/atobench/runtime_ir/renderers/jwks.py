"""JWKS/JWT renderer effects."""

from __future__ import annotations

from typing import Any


def render_jwks_weak_key(injection: dict[str, Any], recipe: dict[str, Any]) -> list[dict[str, Any]]:
    params = (injection.get("transform") or {}).get("params") or {}
    key = {
        "kid": params.get("kid"),
        "kty": params.get("kty", "RSA"),
        "use": params.get("use", "sig"),
        "alg": params.get("alg", "RS256"),
    }
    material = params.get("public_key_material") or {}
    if not key["kid"]:
        raise ValueError(f"injection '{injection.get('id')}': jwks_weak_key requires kid")
    if isinstance(material, dict):
        key.update(material)
    return [
        {
            "effect_id": f"{injection['id']}.jwks.weak_key",
            "operation": "merge_json_object",
            "target": "$",
            "value": {"keys": [key]},
            "renderer": "jwks_weak_key",
        }
    ]
