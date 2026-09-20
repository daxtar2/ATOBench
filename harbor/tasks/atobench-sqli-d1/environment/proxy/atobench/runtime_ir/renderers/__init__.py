"""Renderer registry for recipe-backed RuntimeProgram effects."""

from __future__ import annotations

from typing import Any, Callable

from atobench.runtime_ir.renderers.discovery_text import render_discovery_text
from atobench.runtime_ir.renderers.graphql import render_graphql_introspection
from atobench.runtime_ir.renderers.jwks import render_jwks_weak_key
from atobench.runtime_ir.renderers.openapi import render_openapi_patch
from atobench.runtime_ir.renderers.stateful import render_stateful_sequence

Renderer = Callable[[dict[str, Any], dict[str, Any]], list[dict[str, Any]]]

REGISTRY: dict[str, Renderer] = {
    "discovery_text": render_discovery_text,
    "graphql_introspection": render_graphql_introspection,
    "jwks_weak_key": render_jwks_weak_key,
    "openapi_patch": render_openapi_patch,
    "stateful_sequence": render_stateful_sequence,
}


def render_effects(renderer_name: str, injection: dict[str, Any], recipe: dict[str, Any]) -> list[dict[str, Any]]:
    renderer = REGISTRY.get(renderer_name)
    if renderer is None:
        raise ValueError(f"unknown renderer '{renderer_name}'")
    return renderer(injection, recipe)
