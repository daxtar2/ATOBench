"""coupling_enforcer — hard filter rejecting loose-only proposals.

Empirical justification (memory: atobench_coupling_sweep_results):
  - loose coupling: 0% derail (decorative field, agent ignores)
  - schema_coupled: 100% derail (required field + _links rewrite)
  - precondition: 100% behavioral compliance (B2-like, no flag break)
  - signal_removal: variable (use for FN targets)

Memory: atobench_multi_llm_generator_benchmark_design — Filter 3 / Phase E.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from atobench.scaffold.primitive_template import PrimitiveTemplate


@dataclass
class CouplingEnforcerReport:
    """Summary of coupling enforcement output."""
    n_input: int = 0
    n_kept: int = 0
    n_dropped: int = 0
    kept: list[PrimitiveTemplate] = field(default_factory=list)
    dropped: list[tuple[PrimitiveTemplate, str]] = field(default_factory=list)


def _has_schema_coupling(proposal: PrimitiveTemplate) -> bool:
    """Check if proposal has schema_coupling indicators.

    A proposal is schema_coupled if:
      - coupling.strength == 'schema_coupled', OR
      - coupling.required_field_rewrites is non-empty, OR
      - coupling._links_rewrite is True for any rewrite
    """
    if proposal.coupling.get("strength") == "schema_coupled":
        return True
    rewrites = proposal.coupling.get("required_field_rewrites", [])
    if any(r.get("_links_rewrite") or r.get("required") for r in rewrites):
        return True
    return False


def _has_signal_removal(proposal: PrimitiveTemplate) -> bool:
    """Check if proposal has signal_removal indicators.

    A proposal is signal_removal if:
      - coupling.strength == 'signal_removal', OR
      - coupling.signal_removal_targets is non-empty, OR
      - transform.fields_to_modify contains a 'remove' action
    """
    if proposal.coupling.get("strength") == "signal_removal":
        return True
    if proposal.coupling.get("signal_removal_targets"):
        return True
    fields_to_modify = proposal.transform.get("fields_to_modify", [])
    if any(f.get("action") == "remove" for f in fields_to_modify):
        return True
    return False


def _has_precondition(proposal: PrimitiveTemplate) -> bool:
    """Check if proposal has precondition indicators."""
    return proposal.coupling.get("strength") == "precondition"


def enforce_coupling(proposals: list[PrimitiveTemplate]) -> CouplingEnforcerReport:
    """Filter proposals by coupling strength.

    Rejects proposals that are loose-only (no schema_coupling OR signal_removal OR precondition).

    Args:
        proposals: list of PrimitiveTemplate to filter

    Returns:
        CouplingEnforcerReport with kept proposals
    """
    report = CouplingEnforcerReport()
    report.n_input = len(proposals)

    for p in proposals:
        if _has_schema_coupling(p) or _has_signal_removal(p) or _has_precondition(p):
            report.kept.append(p)
        else:
            reason = (
                f"loose-only coupling rejected (strength={p.coupling.get('strength')}, "
                f"no required_field_rewrites, no signal_removal_targets) — "
                f"empirically 0% effect (memory: atobench_coupling_sweep_results)"
            )
            report.dropped.append((p, reason))
            report.n_dropped += 1

    report.n_kept = len(report.kept)
    return report
