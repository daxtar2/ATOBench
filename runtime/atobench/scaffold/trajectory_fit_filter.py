"""trajectory_fit_filter — reject proposals targeting endpoints no prior agent visited.

Reads prior B0/B3 sweeps' turns.jsonl files, computes the distribution of
endpoints visited by agents on this target, rejects proposals whose path_regex
matches no visited endpoint. Optionally retargets: if archetype fits a visited
endpoint, redirect path_regex to the visited one.

Empirical justification (memory: atobench_t3_v3_strongened_primitives_results):
  v2's cross_turn_jwt_escalation targeted /rest/admin/* (never visited by agent)
  → didn't fire. v3 retargeted to /rest/user/whoami (agent's natural post-login
  check) → fired. This filter automates the retargeting.

Memory: atobench_multi_llm_generator_benchmark_design — Filter 2 / Phase D.
"""
from __future__ import annotations

import json
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from atobench.scaffold.primitive_template import PrimitiveTemplate


@dataclass
class TrajectoryFitReport:
    """Summary of trajectory fit filter output."""
    n_input: int = 0
    n_kept: int = 0
    n_retargeted: int = 0
    n_dropped: int = 0
    visited_endpoint_count: int = 0
    total_visits: int = 0
    top_endpoints: list[tuple[str, int]] = field(default_factory=list)
    kept: list[PrimitiveTemplate] = field(default_factory=list)
    dropped: list[tuple[PrimitiveTemplate, str]] = field(default_factory=list)  # (proposal, reason)


def compute_visited_distribution(
    sweep_dirs: list[Path],
    target_url_substr: str | None = None,
) -> dict[str, int]:
    """Compute {path: visit_count} from prior sweeps' turns.jsonl files.

    Args:
        sweep_dirs: list of paths to sweep directories (each containing turns.jsonl).
            Caller should pre-filter by target (use find_prior_sweeps).
        target_url_substr: optional substring to filter by URL (e.g. 'juice-shop' or
            '8888'). Note: most turns.jsonl record URL as just a path (e.g. '/rest/user/login')
            without host, so this filter often matches nothing. Prefer pre-filtering via
            find_prior_sweeps(target_name=...).

    Returns:
        dict mapping normalized URL path → visit count
    """
    counts: Counter = Counter()
    for sweep_dir in sweep_dirs:
        turns_path = sweep_dir / "turns.jsonl"
        if not turns_path.exists():
            continue
        try:
            with open(turns_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        turn = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    # Optional target filter (rarely useful since URLs are usually paths)
                    if target_url_substr:
                        url = (turn.get("tool_args") or {}).get("url", "")
                        if target_url_substr not in url.lower():
                            continue
                    # Extract path from request
                    req = turn.get("request") or {}
                    path = req.get("path") or ""
                    if not path:
                        # Fall back to tool_args.url
                        url = (turn.get("tool_args") or {}).get("url", "")
                        if url and url.startswith("/"):
                            path = url
                    if not path:
                        continue
                    # Strip query string
                    path = path.split("?", 1)[0]
                    counts[path] += 1
        except (OSError, IOError):
            continue
    return dict(counts)


def _path_matches_regex(path: str, path_regex: str) -> bool:
    """Check if path matches path_regex (re.search)."""
    try:
        return bool(re.search(path_regex, path))
    except re.error:
        return False


def _find_similar_visited(path_regex: str, visited: dict[str, int]) -> str | None:
    """Find a visited endpoint that's similar to the proposal's path_regex.

    Heuristic: if the path_regex contains a structural prefix (e.g. '/rest/user/'),
    find a visited endpoint that starts with the same prefix.

    Returns the most-visited similar endpoint, or None.
    """
    # Extract structural prefix from path_regex (strip regex metachars)
    # e.g. '^/rest/admin/.*$' → '/rest/admin/'
    #      '^/api/v1/products/search$' → '/api/v1/products/search'
    cleaned = path_regex.replace("^", "").replace("$", "").strip()
    # Find the longest literal prefix (no regex metachars)
    metachars = r"\[](){}|.*+?"
    prefix = ""
    for c in cleaned:
        if c in metachars:
            break
        prefix += c

    if not prefix:
        return None

    # Find visited endpoints that start with this prefix
    candidates = [(p, c) for p, c in visited.items() if p.startswith(prefix)]
    if not candidates:
        # Try shorter prefix (parent dir)
        # e.g. '/rest/admin/' → '/rest/'
        if "/" in prefix:
            parent = prefix.rsplit("/", 1)[0] + "/"
            candidates = [(p, c) for p, c in visited.items() if p.startswith(parent)]

    if not candidates:
        return None

    # Return the most-visited candidate
    candidates.sort(key=lambda x: -x[1])
    return candidates[0][0]


def filter_by_trajectory_fit(
    proposals: list[PrimitiveTemplate],
    visited: dict[str, int],
    retarget: bool = True,
) -> TrajectoryFitReport:
    """Filter proposals by trajectory fit.

    Args:
        proposals: list of PrimitiveTemplate to filter
        visited: {path: visit_count} from prior sweeps
        retarget: if True, retarget proposals whose path_regex doesn't match any
                  visited endpoint to a similar visited endpoint (if one exists)

    Returns:
        TrajectoryFitReport with kept proposals (possibly retargeted)
    """
    report = TrajectoryFitReport()
    report.n_input = len(proposals)
    report.visited_endpoint_count = len(visited)
    report.total_visits = sum(visited.values())
    report.top_endpoints = sorted(visited.items(), key=lambda x: -x[1])[:20]

    for p in proposals:
        path_regex = p.match.get("path_regex", "")
        if not path_regex:
            report.dropped.append((p, "empty path_regex"))
            report.n_dropped += 1
            continue

        # Check if any visited endpoint matches
        matched_paths = [path for path in visited if _path_matches_regex(path, path_regex)]

        if matched_paths:
            # Proposal's regex matches a visited endpoint → keep as-is
            report.kept.append(p)
            continue

        # No match — try retarget
        if retarget:
            similar = _find_similar_visited(path_regex, visited)
            if similar:
                # Retarget: replace path_regex with a regex matching the similar endpoint
                new_regex = re.escape(similar)
                # Update the proposal (in-place)
                p.match["path_regex"] = new_regex
                p.rationale = f"[retargeted from {path_regex} → {similar}] {p.rationale}"
                report.kept.append(p)
                report.n_retargeted += 1
                continue

        # No retarget possible → drop
        report.dropped.append((p, f"path_regex '{path_regex}' matches no visited endpoint"))
        report.n_dropped += 1

    report.n_kept = len(report.kept)
    return report


def find_prior_sweeps(target_name: str, logs_dir: Path = Path("logs")) -> list[Path]:
    """Find prior sweep directories for a target.

    Target matching:
      - 'juice-shop': exclude crapi sweeps, include t1_/t2_/t3_ sweeps not containing 'crapi'
      - 'crapi': include only sweeps containing 'crapi' or with port 8888 in name

    Args:
        target_name: e.g. 'juice-shop' or 'crapi'
        logs_dir: path to logs/ directory

    Returns:
        list of sweep dir paths that contain turns.jsonl
    """
    if not logs_dir.exists():
        return []

    target_lower = target_name.lower()
    target_substr = target_lower.replace("-", "").replace("_", "")

    # Determine which targets to EXCLUDE
    exclude_targets = []
    if "juice" in target_lower:
        exclude_targets = ["crapi"]
    elif "crapi" in target_lower:
        # crAPI sweeps are explicitly named; only include those
        explicit_only = True

    candidates = []

    for d in logs_dir.iterdir():
        if not d.is_dir():
            continue
        name = d.name.lower()
        if "smoke" in name:
            continue

        # Exclude other targets
        if any(ex in name for ex in exclude_targets):
            continue

        name_normalized = name.replace("-", "").replace("_", "")
        # Match by target substring or task prefix
        if target_substr in name_normalized:
            candidates.append(d)
        elif name.startswith(("t1_", "t2_", "t3_", "t4_")):
            # Generic task sweeps — include if not excluded above
            candidates.append(d)

    # Filter to those with turns.jsonl
    return [d for d in candidates if (d / "turns.jsonl").exists()]
