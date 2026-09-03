#!/usr/bin/env python3
"""Build primitive_index.yaml from primitive_wiki_v2.md.

Parses each `### <num> <name> <mark>` entry and extracts:
- name, family (Mx), attack_face (Fy), state (stateless/stateful + sm_name)
- effect_dims, fits_endpoint_types (inferred from path_regex), applicability_tier
- has_realism_constraints, evidence_tier, wiki_line_range

Output: atobench/deception_frame/primitive_index.yaml
"""
from __future__ import annotations
import re
import sys
from pathlib import Path

WIKI = Path(__file__).parent / "primitive_wiki_v2.md"
OUT = Path(__file__).parent / "primitive_index.yaml"

# Family → endpoint_type hints (from wiki §1 fits_endpoint_types vocabulary)
FAMILY_ENDPOINT_HINTS = {
    "M1": ["robots", "sitemap", "well_known", "info", "changelog"],
    "M2": ["swagger", "well_known", "internal_api"],
    "M3": ["root", "version", "banner", "info", "jwks", "audit", "health", "users"],
    "M4": ["polling", "queue", "async_job", "eta", "progress", "websocket"],
    "M5": ["oauth", "token", "attestation", "login", "consent", "submit"],
    "M6": ["admin", "users", "debug", "sensitive", "flag", "ftp", "actuator", "login"],
    "M7": ["users", "me", "whoami", "introspect", "features", "oauth"],
    "M8": ["debug", "users", "search", "fetch", "upload"],
    "M9": ["admin", "flag", "secure", "users", "search", "internal_api"],
    "M10": ["admin", "ftp", "robots", "well_known", "users"],
}

# Section header pattern: ### <num>.<num> <name> [marks]
HEADER_RE = re.compile(r"^### (\d+\.\d+) (\S+)(.*)$")
FAMILY_RE = re.compile(r"\*\*Family\*\*:\s*([^|]+?)\s*\|\s*\*\*Face\*\*:\s*(F\d+)")
STATE_RE = re.compile(r"\*\*State\*\*:\s*(stateless|stateful)\s*(?:\(([^)]+)\))?")
EFFECT_RE = re.compile(r"\*\*Effect dims\*\*:\s*\[([^\]]+)\]")
APPLIC_RE = re.compile(r"\*\*(?:Target applicability|Applicability)\*\*:\s*(\S+)")
EVIDENCE_RE = re.compile(r"\*\*Empirical\*\*:\s*(📊|🔬|💡)")


def parse_wiki() -> list[dict]:
    lines = WIKI.read_text(encoding="utf-8").splitlines()
    entries = []

    # Find all section headers
    header_indices = []
    for i, line in enumerate(lines):
        m = HEADER_RE.match(line)
        if m:
            header_indices.append((i, m))

    # Sections that are not primitives (combination rules, dropped, etc.)
    SKIP_SECTIONS = {"15.1", "15.2", "15.3", "16.1", "16.2", "16.3", "16.4", "16.5"}

    for idx, (line_idx, m) in enumerate(header_indices):
        num, name, marks = m.group(1), m.group(2), m.group(3)
        if num in SKIP_SECTIONS:
            continue
        # Compute line range: from this header to next header (or end of section)
        end_idx = header_indices[idx + 1][0] - 1 if idx + 1 < len(header_indices) else min(line_idx + 100, len(lines))

        # Read first ~60 lines of section to extract metadata (Empirical block often ~line 35-45)
        section_lines = lines[line_idx : line_idx + 60]

        # Defaults
        family = ""
        cross_family = ""
        attack_face = ""
        state = "stateless"
        sm_name = None
        effect_dims = []
        applicability = "target-agnostic"  # default
        has_realism = "⚑" in marks
        evidence_tier = "hypothetical"

        for sl in section_lines:
            if not family:
                fm = FAMILY_RE.search(sl)
                if fm:
                    raw_fam = fm.group(1)
                    if raw_fam.startswith("独立"):
                        family = "standalone"
                        # Capture cross-family annotation e.g. "独立 (M6 + M8 复合)" or "独立 (F5)"
                        cm = re.search(r"\(([^)]*)\)", raw_fam)
                        if cm:
                            cross_family = cm.group(1).strip()
                    else:
                        # Take first Mx token
                        m_match = re.match(r"(M\d+)", raw_fam)
                        family = m_match.group(1) if m_match else raw_fam.split()[0]
                    attack_face = fm.group(2) or ""

            sm = STATE_RE.search(sl)
            if sm and not sm_name:
                state = sm.group(1)
                sm_name = sm.group(2)

            em = EFFECT_RE.search(sl)
            if em and not effect_dims:
                effect_dims = [d.strip() for d in em.group(1).split(",")]

            am = APPLIC_RE.search(sl)
            if am and applicability == "target-agnostic":
                applicability = am.group(1)

            ev = EVIDENCE_RE.search(sl)
            if ev:
                symbol = ev.group(1)
                evidence_tier = {"📊": "data", "🔬": "inferred", "💡": "hypothetical"}[symbol]

        # Infer fits_endpoint_types from family
        fits = FAMILY_ENDPOINT_HINTS.get(family, [])

        entry = {
            "name": name,
            "section": num,
            "family": family,
            "attack_face": attack_face,
            "state": state,
            "state_machine": sm_name,
            "effect_dims": effect_dims,
            "fits_endpoint_types": fits,
            "applicability_tier": applicability,
            "has_realism_constraints": has_realism,
            "evidence_tier": evidence_tier,
            "marks": marks.strip(),
            "wiki_line_range": [line_idx + 1, end_idx + 1],  # 1-indexed, inclusive
        }
        if cross_family:
            entry["cross_family"] = cross_family
        entries.append(entry)

    return entries


def to_yaml(entries: list[dict]) -> str:
    out = [
        "# ATOBench Primitive Index — structured catalog of all wiki entries",
        "#",
        "# Generated from primitive_wiki_v2.md by build_primitive_index.py.",
        "# Subagent (deception-planner) reads this first to filter candidates by",
        "# (family, attack_face, fits_endpoint_types, applicability_tier, evidence_tier),",
        "# then lazy-loads the specific wiki section via wiki_line_range.",
        "#",
        "# To regenerate: python3 atobench/deception_frame/build_primitive_index.py",
        "",
        f"schema_version: \"0.1.0\"",
        f"source_wiki: primitive_wiki_v2.md",
        f"n_primitives: {len(entries)}",
        "",
        "primitives:",
    ]
    for e in entries:
        out.append(f"  - name: {e['name']}")
        out.append(f"    section: \"{e['section']}\"")
        out.append(f"    family: {e['family']}")
        out.append(f"    attack_face: {e['attack_face']}")
        out.append(f"    state: {e['state']}")
        if e["state_machine"]:
            out.append(f"    state_machine: {e['state_machine']}")
        if e["effect_dims"]:
            out.append(f"    effect_dims: [{', '.join(e['effect_dims'])}]")
        if e["fits_endpoint_types"]:
            out.append(f"    fits_endpoint_types: [{', '.join(e['fits_endpoint_types'])}]")
        out.append(f"    applicability_tier: {e['applicability_tier']}")
        out.append(f"    has_realism_constraints: {str(e['has_realism_constraints']).lower()}")
        out.append(f"    evidence_tier: {e['evidence_tier']}")
        if e.get("cross_family"):
            out.append(f"    cross_family: \"{e['cross_family']}\"")
        out.append(f"    marks: \"{e['marks']}\"")
        out.append(f"    wiki_line_range: [{e['wiki_line_range'][0]}, {e['wiki_line_range'][1]}]")
    return "\n".join(out) + "\n"


def main():
    entries = parse_wiki()
    if not entries:
        print("ERROR: no entries parsed")
        sys.exit(1)
    print(f"Generated {len(entries)} entries")
    OUT.write_text(to_yaml(entries), encoding="utf-8")
    print(f"Written to {OUT}")
    # Quick sanity stats
    from collections import Counter
    fams = Counter(e["family"] for e in entries)
    states = Counter(e["state"] for e in entries)
    evs = Counter(e["evidence_tier"] for e in entries)
    print(f"Families: {dict(fams)}")
    print(f"States: {dict(states)}")
    print(f"Evidence tiers: {dict(evs)}")


if __name__ == "__main__":
    main()
