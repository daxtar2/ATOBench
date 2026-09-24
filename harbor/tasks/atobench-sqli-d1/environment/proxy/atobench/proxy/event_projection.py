"""Projection between canonical RuntimeEvents and legacy deception_tag."""

from __future__ import annotations

from typing import Any

from atobench.runtime_ir.models import event_to_legacy_primitive_entry


def project_events_to_deception_tag(
    events: list[dict[str, Any]],
    existing_tag: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the legacy deception_tag shape from runtime events.

    Only deception-layer applied events are projected as primitive fires.
    Instrumentation events remain visible in `runtime_events` but do not make
    a B0 turn look deceptive to old evaluators.
    """
    existing_tag = existing_tag or {}
    existing_primitives = list(existing_tag.get("primitives_fired") or [])
    if not existing_primitives and existing_tag.get("z_t"):
        existing_primitives.append(
            {
                "z_t": existing_tag.get("z_t"),
                "u_t": existing_tag.get("u_t") or {},
                "D_t_snapshot": existing_tag.get("D_t_snapshot") or {},
            }
        )

    projected = []
    for event in events:
        if event.get("layer") != "deception_perturbation":
            continue
        if event.get("status") != "applied":
            continue
        primitive = event.get("primitive")
        if not primitive:
            continue
        projected.append(event_to_legacy_primitive_entry(event))

    primitives_fired = existing_primitives + projected
    if not primitives_fired:
        return {}

    merged_u_t: dict[str, Any] = {}
    merged_d_t: dict[str, Any] = {}
    for entry in primitives_fired:
        for key, value in (entry.get("u_t") or {}).items():
            merged_u_t[key] = value
        for key, value in (entry.get("D_t_snapshot") or {}).items():
            merged_d_t[key] = value

    return {
        "primitives_fired": primitives_fired,
        "z_t": primitives_fired[0].get("z_t"),
        "u_t": merged_u_t,
        "D_t_snapshot": merged_d_t,
    }
