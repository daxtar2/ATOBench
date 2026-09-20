"""Pattern matcher — decides whether a flow matches a primitive's match spec.

A match spec (from deception_config.yaml) has:
    path_regex: str            # matched against request.path (path-only, no query)
    methods: [str]             # optional, GET by default
    response_fields: [str]     # fields expected in the response JSON
    request_fields: [str]     # fields expected in the request JSON (optional)

Returns a MatchResult with the matched primitive + coupling name, plus the
parsed response/request JSON for the transformer to use.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from atobench.proxy.flow import HTTPFlow


@dataclass
class MatchResult:
    matched: bool
    primitive_name: str | None = None
    coupling: str | None = None
    reason: str = ""


def _path_only(path: str) -> str:
    """Strip query string from path for regex matching."""
    return path.split("?", 1)[0]


def matches(flow: HTTPFlow, primitive_spec: dict[str, Any]) -> MatchResult:
    """Check if `flow` matches `primitive_spec`'s match criteria.

    `primitive_spec` is a deception_config primitive entry:
        {name, coupling, match: {path_regex, methods, response_fields, request_fields}, ...}

    Returns MatchResult. Only the path/method checks are strict; response_fields
    are reported in `reason` but do not block matching (transformer may inject
    the field if missing — that's the whole point of schema_coupled).
    """
    match_spec = primitive_spec.get("match", {})
    path_regex = match_spec.get("path_regex", "")
    methods = match_spec.get("methods") or ["GET"]

    # Method check
    if flow.request.method.upper() not in [m.upper() for m in methods]:
        return MatchResult(matched=False, reason=f"method {flow.request.method} not in {methods}")

    # Path regex check
    path = _path_only(flow.request.path)
    try:
        if not re.search(path_regex, path):
            return MatchResult(matched=False, reason=f"path {path} does not match {path_regex}")
    except re.error as e:
        return MatchResult(matched=False, reason=f"invalid regex {path_regex}: {e}")

    return MatchResult(
        matched=True,
        primitive_name=primitive_spec.get("name"),
        coupling=primitive_spec.get("coupling"),
        reason=f"path {path} matches {path_regex}; method {flow.request.method}",
    )


def find_match(flow: HTTPFlow, primitives: list[dict[str, Any]]) -> MatchResult:
    """Find the first primitive in `primitives` that matches the flow.

    ⚠️ DEPRECATED 2026-07-07: returns only the first match — multi-primitive-per-path
    plans (governed by plan schema's max_primitives_per_response cap) only fire the
    first primitive with this function. Use `find_all_matches` instead.

    Kept as backward-compat wrapper. New code should use find_all_matches.
    """
    for p in primitives:
        r = matches(flow, p)
        if r.matched:
            return r
    return MatchResult(matched=False, reason="no primitive matched")


def find_all_matches(
    flow: HTTPFlow,
    primitives: list[dict[str, Any]],
    max_primitives_per_response: int = 2,
) -> list[MatchResult]:
    """Find ALL primitives in `primitives` that match the flow, up to cap.

    Iterates in config order, returns list of MatchResult for every matching
    primitive. Respects max_primitives_per_response cap (from plan schema's
    meta.max_primitives_per_response, default 2) — if more primitives match
    than the cap, only the first `cap` are returned (config-order priority).

    This is the correct function to use when a plan has multiple primitives
    targeting the same (path, method) pair — e.g., fake_version_banner
    (body-clobber) + substitute_subgoal (_links lure) on the same endpoint.
    find_match (deprecated) would only fire the first one.
    """
    matches_list: list[MatchResult] = []
    for p in primitives:
        if len(matches_list) >= max_primitives_per_response:
            break
        r = matches(flow, p)
        if r.matched:
            matches_list.append(r)
    return matches_list
