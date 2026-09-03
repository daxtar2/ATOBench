"""primitive_template — open-ended LLM proposal format + validator + translator.

LLMs fill the primitive_template JSON format (schema: atobench/schema/primitive_template.json).
This module provides:
  - PrimitiveTemplate dataclass (mirrors the schema)
  - validate_primitive_template() — validates against the JSON Schema
  - PrimitiveTranslator — maps (archetype, transform.type, state_machine.type,
    coupling.strength, response_fields) to one of the 15 registered primitive
    names in deception_config.json's enum.

Proposals that don't map to any existing transformer are flagged as 'novel form'
(logged for paper §7, dropped from running config — no transformer to run them).

Memory: atobench_multi_llm_generator_benchmark_design — Phase A.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

SCHEMA_PATH = Path(__file__).parent.parent / "schema" / "primitive_template.json"

# 15 registered primitive names (must match atobench/proxy/transformers/__init__.py REGISTRY keys)
REGISTERED_PRIMITIVES = [
    "fake_version_banner",
    "vuln_symptom_inject",
    "no_vuln_gaslighting",
    "corrupt_belief",
    "poison_dependency",
    "false_completion",
    "exhaustion_trap",
    "induce_loop",
    "substitute_subgoal",
    "preemptive_ftp_gaslight",
    "cross_turn_jwt_escalation",
    "hardcoded_cred_comment",
    "decoy_sql_search",
    "cross_turn_jwt_escalation_v2",
    "decoy_sql_search_v2",
]

# Mapping rules: (archetype, transform.type, state_machine.type, response_field_patterns)
# → registered primitive name.
# response_field_patterns: list of substrings; if any matches a response_field, the rule fires.
# Empty response_field_hints = match any (wildcard).
# Empty response_fields in proposal + non-empty hints = NO match (fall through to next rule).
# Order matters — first match wins.
MAPPING_RULES: list[dict[str, Any]] = [
    # belief_injection family (response_fields required for disambiguation)
    {
        "archetype": "belief_injection",
        "transform_type": "replace",
        "state_machine_type": "none",
        "response_field_hints": ["server", "version", "banner", "X-Powered-By", "x-powered-by"],
        "mapped": "fake_version_banner",
    },
    {
        "archetype": "belief_injection",
        "transform_type": "augment",
        "state_machine_type": "none",
        "response_field_hints": ["password_hash", "secret_token", "api_key", "error_rate", "recent_failures", "leaked_data"],
        "mapped": "vuln_symptom_inject",
    },
    {
        "archetype": "belief_injection",
        "transform_type": "replace",
        "state_machine_type": "none",
        "response_field_hints": ["security_posture", "vulnerabilities", "audit_result", "hardening_score"],
        "mapped": "no_vuln_gaslighting",
    },
    # stateful primitives — map by state_machine.type alone (archetype is loose signal;
    # LLMs use state_corruption / stateful_escalation interchangeably for stateful_replace)
    {
        "archetype": None,  # any archetype
        "transform_type": "stateful_replace",
        "state_machine_type": "decrementing_queue",
        "response_field_hints": [],
        "mapped": "corrupt_belief",
    },
    {
        "archetype": None,
        "transform_type": "stateful_replace",
        "state_machine_type": "perpetual_pending",
        "response_field_hints": [],
        "mapped": "corrupt_belief",
    },
    {
        "archetype": None,
        "transform_type": "stateful_replace",
        "state_machine_type": "eta_cycle",
        "response_field_hints": [],
        "mapped": "exhaustion_trap",
    },
    {
        "archetype": None,
        "transform_type": "stateful_replace",
        "state_machine_type": "token_ttl",
        "response_field_hints": [],
        "mapped": "induce_loop",
    },
    {
        "archetype": None,
        "transform_type": "stateful_replace",
        "state_machine_type": "dedup",
        "response_field_hints": [],
        "mapped": "cross_turn_jwt_escalation",
    },
    # link_injection — archetype is the strong signal; response_fields optional
    {
        "archetype": "link_injection",
        "transform_type": "augment",
        "state_machine_type": "none",
        "response_field_hints": [],  # empty hints = match any response_fields (including empty)
        "mapped": "substitute_subgoal",
    },
    # termination — archetype is the strong signal
    {
        "archetype": "termination",
        "transform_type": "replace",
        "state_machine_type": "none",
        "response_field_hints": [],
        "mapped": "false_completion",
    },
    # request_validation — archetype is the strong signal
    {
        "archetype": "request_validation",
        "transform_type": "augment",
        "state_machine_type": "none",
        "response_field_hints": [],
        "mapped": "poison_dependency",
    },
    # credential_bait — archetype is the strong signal
    {
        "archetype": "credential_bait",
        "transform_type": "augment",
        "state_machine_type": "none",
        "response_field_hints": [],
        "mapped": "hardcoded_cred_comment",
    },
    # signal_suppression — archetype is the strong signal
    {
        "archetype": "signal_suppression",
        "transform_type": "replace",
        "state_machine_type": "none",
        "response_field_hints": [],
        "mapped": "preemptive_ftp_gaslight",
    },
    # decoy_endpoint — archetype + synthetic_response
    {
        "archetype": "decoy_endpoint",
        "transform_type": "synthetic_response",
        "state_machine_type": "none",
        "response_field_hints": [],
        "mapped": "decoy_sql_search",
    },
]


@dataclass
class PrimitiveTemplate:
    """One LLM-generated proposal in primitive_template format."""
    name: str
    archetype: str
    match: dict[str, Any]  # {path_regex, methods?, response_fields?, request_fields?}
    transform: dict[str, Any]  # {type, fields_to_modify?, state_machine?}
    coupling: dict[str, Any]  # {strength, required_field_rewrites?, signal_removal_targets?}
    rationale: str
    self_confidence: float
    fake_values: dict[str, Any] = field(default_factory=dict)
    avoid_pattern: str | None = None
    # Filled by translator after LLM output:
    mapped_primitive_name: str | None = None
    is_novel: bool = False  # True if no mapping rule matched

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "PrimitiveTemplate":
        return cls(
            name=d["name"],
            archetype=d["archetype"],
            match=d["match"],
            transform=d["transform"],
            coupling=d["coupling"],
            rationale=d["rationale"],
            self_confidence=d["self_confidence"],
            fake_values=d.get("fake_values", {}),
            avoid_pattern=d.get("avoid_pattern"),
        )

    def to_dict(self) -> dict[str, Any]:
        d = {
            "name": self.name,
            "archetype": self.archetype,
            "match": self.match,
            "transform": self.transform,
            "coupling": self.coupling,
            "rationale": self.rationale,
            "self_confidence": self.self_confidence,
            "fake_values": self.fake_values,
        }
        if self.avoid_pattern:
            d["avoid_pattern"] = self.avoid_pattern
        if self.mapped_primitive_name:
            d["mapped_primitive_name"] = self.mapped_primitive_name
        if self.is_novel:
            d["is_novel"] = True
        return d


def validate_primitive_template(d: dict[str, Any]) -> tuple[bool, list[str]]:
    """Validate a dict against primitive_template.json schema.

    Returns (is_valid, list_of_errors).
    """
    errors: list[str] = []

    # Required top-level fields
    required = ["name", "archetype", "match", "transform", "coupling", "rationale", "self_confidence"]
    for k in required:
        if k not in d:
            errors.append(f"missing required field: {k}")

    # Enum checks (use `or {}` to handle None values from LLM nulls)
    valid_archetypes = {
        "belief_injection", "state_corruption", "link_injection", "signal_suppression",
        "decoy_endpoint", "credential_bait", "stateful_escalation", "termination",
        "request_validation",
    }
    if d.get("archetype") and d["archetype"] not in valid_archetypes:
        errors.append(f"invalid archetype: {d['archetype']}")

    valid_transform_types = {"replace", "augment", "stateful_replace", "synthetic_response"}
    transform = d.get("transform") or {}
    transform_type = transform.get("type")
    if transform_type and transform_type not in valid_transform_types:
        errors.append(f"invalid transform.type: {transform_type}")

    valid_coupling = {"loose", "schema_coupled", "precondition", "signal_removal"}
    coupling = d.get("coupling") or {}
    coupling_strength = coupling.get("strength")
    if coupling_strength and coupling_strength not in valid_coupling:
        errors.append(f"invalid coupling.strength: {coupling_strength}")

    valid_sm_types = {"none", "decrementing_queue", "perpetual_pending", "eta_cycle", "token_ttl", "dedup"}
    sm = transform.get("state_machine") or {}
    sm_type = sm.get("type")
    if sm_type and sm_type not in valid_sm_types:
        errors.append(f"invalid state_machine.type: {sm_type}")

    # name pattern
    name = d.get("name", "")
    if name and not re.fullmatch(r"^[a-z][a-z0-9_]*$", name):
        errors.append(f"invalid name (must be snake_case): {name}")

    # path_regex must compile (handle None match)
    match = d.get("match") or {}
    path_regex = match.get("path_regex", "")
    if path_regex:
        try:
            re.compile(path_regex)
        except re.error as e:
            errors.append(f"invalid path_regex '{path_regex}': {e}")

    # self_confidence range
    sc = d.get("self_confidence")
    if sc is not None and not (0.0 <= sc <= 1.0):
        errors.append(f"self_confidence out of range [0,1]: {sc}")

    # Cross-field constraints
    if transform_type == "stateful_replace" and sm_type in (None, "none"):
        errors.append("stateful_replace requires state_machine.type != 'none'")

    if transform_type == "synthetic_response" and not path_regex:
        errors.append("synthetic_response requires non-empty path_regex")

    if coupling_strength == "loose":
        # loose is structurally valid but will be rejected by coupling_enforcer
        # (memory: atobench_coupling_sweep_results — 0% effect)
        pass

    return (len(errors) == 0, errors)


def _safe_get(d: dict | None, key: str, default: Any = None) -> Any:
    """Helper: get from a possibly-None dict."""
    if d is None:
        return default
    return d.get(key, default)


class PrimitiveTranslator:
    """Maps PrimitiveTemplate → registered primitive name.

    Uses MAPPING_RULES (first match wins). If no rule matches, marks the proposal
    as 'novel form' — logged for paper §7 but dropped from running config.
    """

    def translate(self, template: PrimitiveTemplate) -> PrimitiveTemplate:
        """Fill in mapped_primitive_name (or mark is_novel=True if no mapping)."""
        # Handle None fields gracefully (LLM may return null for transform/match/coupling)
        transform = template.transform or {}
        match = template.match or {}
        sm = transform.get("state_machine") or {}

        transform_type = transform.get("type", "")
        sm_type = sm.get("type", "none")
        response_fields = [f.lower() for f in match.get("response_fields", [])]

        for rule in MAPPING_RULES:
            # archetype: None = wildcard (match any archetype)
            rule_archetype = rule["archetype"]
            if rule_archetype is not None and template.archetype != rule_archetype:
                continue
            if transform_type != rule["transform_type"]:
                continue
            if sm_type != rule["state_machine_type"]:
                continue
            hints = rule["response_field_hints"]
            # Empty hints = match any (wildcard)
            # Non-empty hints = at least one hint must appear in response_fields
            if hints:
                if not response_fields:
                    continue  # hints required but proposal has no response_fields
                if not any(any(hint in rf for rf in response_fields) for hint in hints):
                    continue
            template.mapped_primitive_name = rule["mapped"]
            return template

        # No rule matched — novel form
        template.is_novel = True
        return template

    def translate_all(self, templates: list[PrimitiveTemplate]) -> tuple[list[PrimitiveTemplate], list[PrimitiveTemplate]]:
        """Translate all; return (mappable, novel) tuples."""
        mappable: list[PrimitiveTemplate] = []
        novel: list[PrimitiveTemplate] = []
        for t in templates:
            t = self.translate(t)
            if t.is_novel:
                novel.append(t)
            else:
                mappable.append(t)
        return mappable, novel


def to_deception_config_entry(template: PrimitiveTemplate) -> dict[str, Any]:
    """Convert a translated PrimitiveTemplate to a deception_config.yaml primitive entry.

    The entry must conform to deception_config.json's primitives[] items schema.
    """
    if not template.mapped_primitive_name:
        raise ValueError(f"cannot convert untranslated template: {template.name} (is_novel={template.is_novel})")

    # Handle None fields gracefully
    match = template.match or {}
    transform = template.transform or {}
    coupling = template.coupling or {}
    sm = transform.get("state_machine") or {}

    entry: dict[str, Any] = {
        "name": template.mapped_primitive_name,
        "coupling": coupling.get("strength", "schema_coupled"),
        "match": {"path_regex": match.get("path_regex", "")},
        "fake_values": template.fake_values or {},
        "stateless": sm.get("type", "none") == "none",
    }

    # Optional fields
    if match.get("methods"):
        entry["match"]["methods"] = match["methods"]
    if match.get("response_fields"):
        entry["match"]["response_fields"] = match["response_fields"]
    if match.get("request_fields"):
        entry["match"]["request_fields"] = match["request_fields"]

    # State machine
    if sm and sm.get("type") != "none":
        sm_entry = {"type": sm["type"]}
        if "initial_state" in sm:
            sm_entry["initial_state"] = sm["initial_state"]
        if "terminal_state" in sm:
            sm_entry["terminal_state"] = sm["terminal_state"]
        if "transitions" in sm:
            sm_entry["transitions"] = sm["transitions"]
        if "max_uses" in sm:
            sm_entry["max_uses"] = sm["max_uses"]
        if "min_diff_fields" in sm:
            sm_entry["min_diff_fields"] = sm["min_diff_fields"]
        entry["state_machine"] = sm_entry
        entry["stateless"] = False

    # Coupling-specific params
    if coupling.get("strength") == "signal_removal":
        targets = coupling.get("signal_removal_targets", [])
        if targets:
            entry["params"] = {"signal_removal_targets": targets}
    elif coupling.get("strength") == "schema_coupled":
        rewrites = coupling.get("required_field_rewrites", [])
        if rewrites:
            entry["params"] = {"required_field_rewrites": rewrites}

    return entry


def load_schema() -> dict[str, Any]:
    """Load the primitive_template.json JSON Schema."""
    with open(SCHEMA_PATH, "r", encoding="utf-8") as f:
        return json.load(f)
