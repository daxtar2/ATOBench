"""Shared vocabulary for runtime injection surfaces and loaders."""

from __future__ import annotations

from typing import Any


SURFACES = {
    "metadata",
    "structured_payload",
    "document_text",
    "failure_signal",
    "discovery_document",
    "enumeration_listing",
    "progress_state",
    "interactive_session",
}

LOADERS = {
    "metadata",
    "document",
    "structured",
    "session",
}

HOOKS = {
    "request",
    "response",
}

TARGET_KINDS = {
    "json_body",
    "text_body",
    "html_body",
    "header",
    "status_code",
    "cookie",
    "synthetic_endpoint",
    "request_body",
    "request_header",
}

SURFACE_DEFAULT_LOADER = {
    "metadata": "metadata",
    "structured_payload": "structured",
    "document_text": "document",
    "failure_signal": "document",
    "discovery_document": "document",
    "enumeration_listing": "structured",
    "progress_state": "session",
    "interactive_session": "session",
}


def infer_surface(transform: dict[str, Any], recipe: dict[str, Any] | None = None) -> str:
    """Infer a conservative surface for legacy trigger/transform injections."""
    params = transform.get("params") or {}
    renderer = str(params.get("renderer") or (recipe or {}).get("renderer") or "")
    content_type = str(params.get("content_type") or "").lower()
    transform_type = transform.get("type")

    if renderer in {"openapi_patch", "graphql_introspection_patch"}:
        return "discovery_document"
    if renderer in {"jwks_patch"}:
        return "structured_payload"
    if renderer in {"discovery_text"} or "text" in content_type or "html" in content_type:
        return "document_text"
    if transform_type == "stateful_replace":
        return "progress_state"
    if transform.get("removed_fields"):
        removed = [str(f).lower() for f in transform.get("removed_fields") or []]
        if any(f.startswith("header:") for f in removed):
            return "metadata"
    return "structured_payload"


def infer_loader(surface: str, transform: dict[str, Any] | None = None) -> str:
    transform = transform or {}
    params = transform.get("params") or {}
    if surface == "failure_signal" and str(params.get("content_type", "")).lower().startswith("application/json"):
        return "structured"
    return SURFACE_DEFAULT_LOADER.get(surface, "structured")


def infer_target(transform: dict[str, Any], surface: str) -> dict[str, Any]:
    """Infer a target descriptor for legacy trigger/transform injections."""
    params = transform.get("params") or {}
    if surface == "metadata":
        removed = [str(f) for f in transform.get("removed_fields") or []]
        headers = [f[7:] for f in removed if f.lower().startswith("header:")]
        return {"kind": "header", "header": headers[0] if headers else "*"}
    if transform.get("type") == "synthetic_response":
        content_type = params.get("content_type")
        if content_type and "html" in str(content_type).lower():
            return {"kind": "html_body", "path": "$", "content_type": content_type}
        if content_type and "text" in str(content_type).lower():
            return {"kind": "text_body", "path": "$", "content_type": content_type}
        return {"kind": "synthetic_endpoint", "path": "$", "content_type": content_type}
    if surface in {"document_text", "discovery_document"}:
        return {"kind": "text_body", "path": "$", "content_type": params.get("content_type")}
    return {"kind": "json_body", "path": "$"}
