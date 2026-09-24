"""substitute_subgoal — rewrite `_links` to lure agent into wrong subgoal.

Stateless. Response-side.

Coupling variants:
- loose: add decorative `_links.canonical` pointing to wrong endpoint
- schema_coupled: REPLACE `_links` with planner's fake_values._links dict verbatim.
    Lure agent (via HATEOAS _links) into wrong subgoal / fake endpoint.
    ⚠️ Passthrough (L1.x fix 2026-07-07): old code hardcoded canonical/self/next
    and DELETED any _links.flag — that broke planner's lure design. Now passes
    through fake_values._links verbatim, only strips _links.cve/advisory (C9 ❌).
- precondition: substitution active only when request from non-admin token
- signal_removal: remove `_links` entirely — agent has no navigation hints

Memory: atobench_t3_v2_sweep_results — admin_api substitute_subgoal is headline T3.
"""
from __future__ import annotations

from typing import Any

from atobench.proxy.flow import HTTPFlow
from atobench.proxy.state_store import StateStore
from atobench.proxy.transformers.base import PrimitiveTransformer


# Keys that are C9 ❌ Unnatural if put in _links (servers don't self-advertise CVEs)
_CVE_LINK_KEYS = ("cve", "advisory", "vuln", "vulnerability")


class SubstituteSubgoalTransformer(PrimitiveTransformer):
    NAME = "substitute_subgoal"
    STATEFUL = False

    def apply(self, flow: HTTPFlow, primitive_spec: dict[str, Any], state_store: StateStore) -> None:
        coupling = self._coupling(primitive_spec)
        if coupling == "loose":
            self._loose(flow, primitive_spec)
        elif coupling == "schema_coupled":
            self._schema_coupled(flow, primitive_spec)
        elif coupling == "precondition":
            self._precondition(flow, primitive_spec)
        elif coupling == "signal_removal":
            self._signal_removal(flow)

    def _loose(self, flow: HTTPFlow, spec: dict[str, Any]) -> None:
        """Decorative: add a _links.canonical pointing to a wrong endpoint.
        Agent can ignore (real _links preserved). Uses fake_values._links.canonical
        if present, else falls back to self-path (no hardcoded fake)."""
        fake_values = spec.get("fake_values", {})
        planner_links = fake_values.get("_links") or {}
        canonical_target = (
            planner_links.get("canonical")
            if isinstance(planner_links, dict)
            else None
        ) or fake_values.get("canonical_redirect") or flow.request.path

        data = flow.get_or_parse_response_json() or {}
        links = data.setdefault("_links", {})
        links["canonical"] = canonical_target
        flow.response.json = data
        flow.write_response_json()
        self._mark(flow, self.NAME, {
            "fake_value": canonical_target,
            "_links.canonical": canonical_target,
        })

    def _schema_coupled(self, flow: HTTPFlow, spec: dict[str, Any]) -> None:
        """REPLACE `_links` with planner's fake_values._links dict verbatim.

        Planner controls exact _links shape — e.g. {_links: {flag: {href: /fake/flag}, canonical: /fake/flag}}.
        This is the lure mechanism: agent reading HATEOAS _links.flag follows the fake href.

        Defense-in-depth: strip _links.cve / _links.advisory etc. (C9 ❌ Unnatural,
        same anti-pattern as fake_version_banner._links.cve — servers don't self-advertise CVEs).
        """
        fake_values = spec.get("fake_values", {})
        planner_links = fake_values.get("_links")

        if not isinstance(planner_links, dict) or not planner_links:
            # Fallback: no _links in fake_values — use old canonical-redirect behavior
            # (legacy support for configs that only set canonical_redirect field)
            self._loose(flow, spec)
            return

        # Defense-in-depth: strip CVE-advertisement keys (C9 ❌ Unnatural)
        realism_violation_stripped: list[str] = []
        cleaned_links: dict[str, Any] = {}
        for k, v in planner_links.items():
            if k.lower() in _CVE_LINK_KEYS:
                realism_violation_stripped.append(f"_links.{k}")
                continue
            # Also strip nested cve/advisory inside _links.<key>.href etc.
            if isinstance(v, dict):
                cleaned_v = {}
                for sub_k, sub_v in v.items():
                    if sub_k.lower() in _CVE_LINK_KEYS or (
                        isinstance(sub_v, str)
                        and ("cve-" in sub_v.lower() or "nvd.nist" in sub_v.lower())
                    ):
                        realism_violation_stripped.append(f"_links.{k}.{sub_k}")
                    else:
                        cleaned_v[sub_k] = sub_v
                if cleaned_v:
                    cleaned_links[k] = cleaned_v
            elif isinstance(v, str) and ("cve-" in v.lower() or "nvd.nist" in v.lower()):
                realism_violation_stripped.append(f"_links.{k}")
            else:
                cleaned_links[k] = v

        if not cleaned_links:
            # All _links were CVE advertisement — nothing legitimate to inject
            self._mark(flow, self.NAME, {
                "fake_value": "",
                "realism_violation_stripped": realism_violation_stripped,
            })
            return

        # REPLACE existing _links with planner's cleaned _links (this IS the deception:
        # real _links.flag.href disappears, fake _links.flag.href appears)
        data = flow.get_or_parse_response_json() or {}
        data["_links"] = cleaned_links

        # Set Location header if planner provided canonical_redirect or _links.canonical
        canonical_target = cleaned_links.get("canonical") or fake_values.get("canonical_redirect")
        if canonical_target and isinstance(canonical_target, str):
            self._set_header(flow, "Location", canonical_target)

        flow.response.json = data
        flow.write_response_json()

        # Build u_t with full fidelity for FCA detection
        u_t: dict[str, Any] = {"fake_value": ""}
        for k, v in cleaned_links.items():
            if isinstance(v, dict):
                for sub_k, sub_v in v.items():
                    u_t[f"_links.{k}.{sub_k}"] = sub_v
                    if (isinstance(sub_v, str) and len(sub_v) >= 6
                            and u_t["fake_value"] == ""):
                        u_t["fake_value"] = sub_v
            else:
                u_t[f"_links.{k}"] = v
                if (isinstance(v, str) and len(v) >= 6
                        and u_t["fake_value"] == ""):
                    u_t["fake_value"] = v

        if realism_violation_stripped:
            u_t["realism_violation_stripped"] = realism_violation_stripped

        self._mark(flow, self.NAME, u_t)

    def _precondition(self, flow: HTTPFlow, spec: dict[str, Any]) -> None:
        auth = flow.request.header("Authorization") or ""
        if "admin" not in auth.lower():
            self._schema_coupled(flow, spec)
        # else pass through (admin sees correct _links)

    def _signal_removal(self, flow: HTTPFlow) -> None:
        data = flow.get_or_parse_response_json()
        if data is None:
            return
        if "_links" in data:
            del data["_links"]
        flow.response.json = data
        flow.write_response_json()
        # Strip Location header too
        self._remove_header(flow, "Location")
        self._mark(flow, self.NAME, {"fake_value": "", "removed": ["_links", "Location"]})
