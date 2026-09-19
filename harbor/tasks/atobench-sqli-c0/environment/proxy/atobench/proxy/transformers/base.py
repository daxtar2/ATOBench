"""PrimitiveTransformer ABC.

Every transformer implements:
    applies_to(flow, primitive_spec) -> bool
        -- called by addon; if True, addon calls apply()
    apply(flow, primitive_spec, state_store) -> None
        -- mutates flow.response and sets flow.deception_tag

`primitive_spec` is the primitive entry from deception_config.yaml:
    {name, coupling, match, fake_values, state_machine, stateless, params}

The base class provides helpers for common operations:
- set_response_field(field, value)
- remove_response_field(field)
- set_response_header(name, value)
- mark_deception(z_t, u_t, D_t_snapshot)
"""
from __future__ import annotations

import abc
from typing import Any

from atobench.proxy.flow import HTTPFlow
from atobench.proxy.state_store import StateStore


class PrimitiveTransformer(abc.ABC):
    """Base class for all deception transformers.

    Subclasses must set:
        NAME: str            -- primitive name (matches deception_config enum)
        STATEFUL: bool       -- whether cross-turn state is required

    Subclasses must implement:
        apply(flow, primitive_spec, state_store) -> None

    The default `applies_to` checks the primitive_spec's `name` field matches
    self.NAME. Override for more sophisticated gating (e.g. coupling-variant
    specific behavior).
    """

    NAME: str = ""
    STATEFUL: bool = False

    def applies_to(self, flow: HTTPFlow, primitive_spec: dict[str, Any]) -> bool:
        return primitive_spec.get("name") == self.NAME

    @abc.abstractmethod
    def apply(self, flow: HTTPFlow, primitive_spec: dict[str, Any], state_store: StateStore) -> None:
        """Mutate flow.response and set flow.deception_tag.

        Implementations should:
        1. Read coupling variant from primitive_spec["coupling"]
        2. Apply the variant-specific transformation to flow.response
        3. Set flow.deception_tag = {z_t, u_t, D_t_snapshot}
        """
        raise NotImplementedError

    # --- shared helpers ---

    @staticmethod
    def _ensure_response_json(flow: HTTPFlow) -> dict | None:
        """Parse response body as JSON if not already; return dict or None."""
        return flow.get_or_parse_response_json()

    @staticmethod
    def _write_response_json(flow: HTTPFlow) -> None:
        """Serialize response.json back to response.body."""
        flow.write_response_json()

    @staticmethod
    def _set_field(flow: HTTPFlow, field: str, value: Any) -> None:
        """Set a field in the response JSON body."""
        data = flow.get_or_parse_response_json()
        if data is None:
            data = {}
        data[field] = value
        flow.response.json = data
        flow.write_response_json()

    @staticmethod
    def _remove_field(flow: HTTPFlow, field: str) -> None:
        data = flow.get_or_parse_response_json()
        if data is None or field not in data:
            return
        del data[field]
        flow.write_response_json()

    @staticmethod
    def _set_header(flow: HTTPFlow, name: str, value: str) -> None:
        flow.response.set_header(name, value)

    @staticmethod
    def _remove_header(flow: HTTPFlow, name: str) -> None:
        for k in list(flow.response.headers.keys()):
            if k.lower() == name.lower():
                del flow.response.headers[k]

    def _mark(self, flow: HTTPFlow, z_t: str, u_t: dict[str, Any], D_t_snapshot: dict[str, Any] | None = None) -> None:
        """Append this primitive's tag to the flow's deception_tag.

        L1.5 fix 2026-07-07: previously _mark OVERWROTE flow.deception_tag, so
        when multiple primitives fired on the same response (e.g. fake_version_banner
        + substitute_subgoal on same path), only the LAST primitive's z_t/u_t
        survived. Now _mark appends to `primitives_fired` list and merges u_t.

        z_t: primitive name (e.g. "fake_version_banner")
        u_t: dict of fake values injected — keys with values >=6 chars are
             candidates for FCA detection (final_report_text substring match)
        D_t_snapshot: stateful snapshot for stateful primitives (None for stateless)

        Resulting deception_tag shape:
            {
              "primitives_fired": [  # canonical — list of all fired primitives
                {"z_t": "fake_version_banner", "u_t": {...}, "D_t_snapshot": {}},
                {"z_t": "substitute_subgoal", "u_t": {...}, "D_t_snapshot": {}},
              ],
              "z_t": "fake_version_banner",  # backward-compat: FIRST primitive name
              "u_t": {merged dict — all primitives' u_t keys, later overwrites earlier},
              "D_t_snapshot": {merged},
            }
        """
        new_entry = {
            "z_t": z_t,
            "u_t": dict(u_t),
            "D_t_snapshot": dict(D_t_snapshot) if D_t_snapshot else {},
        }

        existing = flow.deception_tag or {}
        # Backward compat: read old shape (z_t string, u_t dict) and migrate to new
        existing_primitives = existing.get("primitives_fired", [])
        if not existing_primitives and existing.get("z_t"):
            # Old shape — migrate the existing single entry
            existing_primitives = [{
                "z_t": existing["z_t"],
                "u_t": existing.get("u_t", {}),
                "D_t_snapshot": existing.get("D_t_snapshot", {}),
            }]

        existing_primitives.append(new_entry)

        # Merged u_t (later primitives' keys overwrite earlier — usually distinct keys)
        merged_u_t: dict[str, Any] = {}
        for entry in existing_primitives:
            for k, v in (entry.get("u_t") or {}).items():
                merged_u_t[k] = v

        # Merged D_t_snapshot
        merged_D_t: dict[str, Any] = {}
        for entry in existing_primitives:
            for k, v in (entry.get("D_t_snapshot") or {}).items():
                merged_D_t[k] = v

        # First primitive's z_t (backward compat — consumers reading flow.deception_tag.z_t
        # get the first primitive, not the last)
        first_z_t = existing_primitives[0]["z_t"] if existing_primitives else z_t

        flow.deception_tag = {
            "primitives_fired": existing_primitives,
            "z_t": first_z_t,
            "u_t": merged_u_t,
            "D_t_snapshot": merged_D_t,
        }

    def _coupling(self, primitive_spec: dict[str, Any]) -> str:
        return primitive_spec.get("coupling", "loose")
