"""convergence_filter — group multi-LLM proposals, keep convergent ones.

Mode A produces 3 ProposalList objects (one per LLM). This filter:
  1. Flattens all proposals from all LLMs into one list
  2. Groups by (archetype, normalized_endpoint_pattern)
  3. Keeps groups with ≥ min_supporters LLMs proposing the same pattern
  4. Picks one representative per convergent group (highest self_confidence)
  5. Fallback: if 0 convergent groups, take top-1 per LLM by self_confidence

Empirical justification (memory: atobench_t3_v2_llm_distilled_results):
  - convergent proposals (≥2 LLMs) had 4/5 fire rate (80%)
  - single-LLM proposals had 0/2 fire rate (0%)
  - threshold ≥2 supporters is the validated signal

Memory: atobench_multi_llm_generator_benchmark_design — Filter 1 / Phase C.
"""
from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

from atobench.scaffold.primitive_template import PrimitiveTemplate
from atobench.scaffold.proposal_sampler import ProposalList


@dataclass
class ConvergenceReport:
    """Summary of convergence filter output."""
    n_input_proposals: int = 0
    n_convergent_groups: int = 0
    n_kept: int = 0
    n_convergent: int = 0  # proposals from convergent groups (≥2 LLMs)
    n_single_llm: int = 0  # top-1-per-LLM proposals (secondary, when no convergence or always)
    n_fallback: int = 0  # deprecated — same as n_single_llm when used_fallback=True
    used_fallback: bool = False  # True if NO convergent groups (all kept are single-LLM)
    groups: list[dict[str, Any]] = field(default_factory=list)
    kept: list[PrimitiveTemplate] = field(default_factory=list)
    # Mark each kept proposal's source
    kept_sources: list[str] = field(default_factory=list)  # "convergent" or "single_llm"


def normalize_endpoint_pattern(path_regex: str) -> str:
    """Normalize a path_regex to a canonical endpoint shape.

    Strips regex anchors and replaces specific IDs (long hex, numeric) with *.
    Named segments (e.g. 'whoami', 'secret-panel') are KEPT — two proposals
    on different named endpoints are NOT considered convergent.

    Examples:
      '^/api/v1/admin/$' → '/api/v1/admin/'
      '/api/v1/users/12345/posts' → '/api/v1/users/*/posts'
      '/api/v1/sessions/abcdef0123456789' → '/api/v1/sessions/*'
      '/rest/user/whoami' → '/rest/user/whoami'  (named, kept)
      '^/$' → '/'
    """
    if not path_regex:
        return ""

    s = path_regex
    # Strip regex anchors for normalization
    s = s.replace("^", "").replace("$", "")

    # Replace long hex IDs (8+ chars) with *
    s = re.sub(r"[a-f0-9]{8,}", "*", s)
    # Replace numeric IDs with *
    s = re.sub(r"/\d+(?=/|$|\.)", "/*", s)

    return s


def group_key(proposal: PrimitiveTemplate) -> tuple[str, str]:
    """Build a (archetype, normalized_endpoint) grouping key."""
    path_regex = proposal.match.get("path_regex", "")
    return (proposal.archetype, normalize_endpoint_pattern(path_regex))


def filter_by_convergence(
    proposal_lists: list[ProposalList],
    min_supporters: int = 2,
) -> ConvergenceReport:
    """Filter proposals by cross-LLM convergence.

    Args:
        proposal_lists: list of ProposalList (one per LLM, Mode A = 3)
        min_supporters: minimum number of LLMs that must propose the same
                        (archetype, endpoint) pattern. Default 2.

    Returns:
        ConvergenceReport with kept proposals (one per convergent group)
        or fallback (top-1 per LLM by self_confidence) if no convergence.
    """
    report = ConvergenceReport()

    # Edge case: no input → no convergence, no fallback
    if not proposal_lists or all(not pl.proposals for pl in proposal_lists):
        return report

    # Index proposals by (llm_name, group_key) → list of proposals
    # A single LLM may propose multiple primitives with the same group_key
    # (we treat them as one supporter for that group)
    group_supporters: dict[tuple[str, str], set[str]] = defaultdict(set)
    group_proposals: dict[tuple[str, str], list[tuple[PrimitiveTemplate, str]]] = defaultdict(list)

    n_input = 0
    for pl in proposal_lists:
        for p in pl.proposals:
            n_input += 1
            key = group_key(p)
            group_supporters[key].add(pl.llm_name)
            group_proposals[key].append((p, pl.llm_name))

    report.n_input_proposals = n_input

    # Find convergent groups (≥ min_supporters distinct LLMs)
    convergent_keys = [
        key for key, supporters in group_supporters.items()
        if len(supporters) >= min_supporters
    ]
    report.n_convergent_groups = len(convergent_keys)

    # Track which proposals are kept (by id) to avoid duplicates
    kept_ids = set()
    kept_proposals: list[PrimitiveTemplate] = []
    kept_sources: list[str] = []

    # Identify all (archetype, endpoint) keys that are convergent
    convergent_keys_set = set(convergent_keys)

    # Always include convergent proposals first (high confidence)
    for key in convergent_keys:
        proposals_in_group = group_proposals[key]
        best = max(proposals_in_group, key=lambda x: x[0].self_confidence)
        proposal_id = id(best[0])
        if proposal_id not in kept_ids:
            kept_proposals.append(best[0])
            kept_sources.append("convergent")
            kept_ids.add(proposal_id)
            report.groups.append({
                "archetype": key[0],
                "endpoint_pattern": key[1],
                "supporters": sorted(group_supporters[key]),
                "n_proposals_in_group": len(proposals_in_group),
                "representative_name": best[0].name,
                "representative_llm": best[1],
                "self_confidence": best[0].self_confidence,
            })
            report.n_convergent += 1

    # ALSO include top non-convergent proposal per LLM (medium confidence).
    # For each LLM, find its highest-self_confidence proposal that's NOT in
    # any convergent group. This ensures we capture each LLM's unique ideas
    # that didn't make it to convergence, without duplicating convergent ones.
    for pl in proposal_lists:
        if not pl.proposals:
            continue
        # Filter to proposals NOT in any convergent group
        non_convergent = [
            p for p in pl.proposals
            if group_key(p) not in convergent_keys_set
        ]
        if not non_convergent:
            continue  # all this LLM's proposals are in convergent groups
        best = max(non_convergent, key=lambda p: p.self_confidence)
        proposal_id = id(best)
        if proposal_id not in kept_ids:
            kept_proposals.append(best)
            kept_sources.append("single_llm")
            kept_ids.add(proposal_id)
            report.n_single_llm += 1

    # Set used_fallback flag: True if NO convergent groups (all kept are single-LLM)
    report.used_fallback = (len(convergent_keys) == 0)
    report.n_fallback = report.n_single_llm if report.used_fallback else 0

    report.kept = kept_proposals
    report.kept_sources = kept_sources
    report.n_kept = len(kept_proposals)

    return report
