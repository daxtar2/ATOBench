"""Generate runtime-event attribution reports for deception workspaces."""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

import yaml


def generate_attribution_report(
    deception_dir: str | Path,
    episode_id: str | None = None,
    output_path: str | Path | None = None,
) -> dict[str, Any]:
    """Generate a markdown attribution report from workspace run artifacts."""
    dec_dir = Path(deception_dir)
    run_dir = _resolve_run_dir(dec_dir, episode_id)
    turns = _load_jsonl(run_dir / "turns.jsonl")
    plan = _load_yaml(dec_dir / "deception_plan.yaml")
    planned = plan.get("plan", {}).get("injections", [])

    event_counts: Counter[str] = Counter()
    binding_counts: Counter[tuple[str, str]] = Counter()
    primitive_counts: Counter[str] = Counter()
    fake_values: dict[str, set[str]] = defaultdict(set)
    event_turns: dict[str, list[int]] = defaultdict(list)
    binding_turns: dict[tuple[str, str], list[int]] = defaultdict(list)

    for turn in turns:
        for event in _deception_events(turn):
            inj_id = event.get("injection_id") or event.get("rule_id") or "unknown"
            primitive = event.get("primitive") or "unknown"
            binding_id = event.get("binding_id") or "default"
            event_counts[inj_id] += 1
            binding_counts[(inj_id, binding_id)] += 1
            primitive_counts[primitive] += 1
            event_turns[inj_id].append(turn.get("turn_idx", 0))
            binding_turns[(inj_id, binding_id)].append(turn.get("turn_idx", 0))
            details = event.get("details") or {}
            for value in (details.get("u_t") or {}).values():
                if isinstance(value, str) and len(value) >= 6:
                    fake_values[inj_id].add(value)

    rows = []
    for inj in planned:
        inj_id = inj.get("id", "")
        primitive = inj.get("primitive", "")
        count = event_counts[inj_id] or primitive_counts[primitive]
        planned_bindings = [
            b.get("binding_id", "default")
            for b in (inj.get("bindings") or [{"binding_id": "default"}])
        ]
        rows.append(
            {
                "injection_id": inj_id,
                "primitive": primitive,
                "target_dims": inj.get("target_dims", []),
                "fired": count > 0,
                "fires": count,
                "turns": event_turns.get(inj_id, []),
                "fake_values": sorted(fake_values.get(inj_id, [])),
                "bindings": [
                    {
                        "binding_id": binding_id,
                        "fires": binding_counts[(inj_id, binding_id)],
                        "turns": binding_turns.get((inj_id, binding_id), []),
                    }
                    for binding_id in planned_bindings
                ],
            }
        )

    report_path = Path(output_path) if output_path else run_dir / "attribution_report.md"
    report_path.write_text(_render_markdown(dec_dir, run_dir, rows), encoding="utf-8")
    return {
        "deception_dir": str(dec_dir),
        "run_dir": str(run_dir),
        "episode_id": run_dir.name,
        "report_path": str(report_path),
        "n_turns": len(turns),
        "n_planned": len(planned),
        "n_fired": sum(1 for r in rows if r["fired"]),
        "uncontacted": [r["injection_id"] for r in rows if not r["fired"]],
        "rows": rows,
    }


def _resolve_run_dir(dec_dir: Path, episode_id: str | None) -> Path:
    runs_dir = dec_dir / "runs"
    if episode_id:
        run_dir = runs_dir / episode_id
        if not run_dir.exists():
            raise FileNotFoundError(f"run dir not found: {run_dir}")
        return run_dir
    candidates = sorted([p for p in runs_dir.iterdir() if p.is_dir()]) if runs_dir.exists() else []
    if not candidates:
        raise FileNotFoundError(f"no runs found under {runs_dir}")
    return candidates[-1]


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows


def _load_yaml(path: Path) -> dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _deception_events(turn: dict[str, Any]) -> list[dict[str, Any]]:
    events = turn.get("runtime_events") or []
    out = [
        e for e in events
        if isinstance(e, dict)
        and e.get("layer") == "deception_perturbation"
        and e.get("status") == "applied"
    ]
    if out:
        return out
    # Legacy fallback.
    tag = turn.get("deception_tag") or {}
    return [
        {
            "injection_id": entry.get("injection_id") or entry.get("z_t"),
            "primitive": entry.get("z_t"),
            "details": {"u_t": entry.get("u_t") or {}},
            "layer": "deception_perturbation",
            "status": "applied",
        }
        for entry in (tag.get("primitives_fired") or [])
    ]


def _render_markdown(dec_dir: Path, run_dir: Path, rows: list[dict[str, Any]]) -> str:
    lines = [
        f"# Attribution Report: {run_dir.name}",
        "",
        f"- deception_dir: `{dec_dir}`",
        f"- run_dir: `{run_dir}`",
        f"- planned_injections: {len(rows)}",
        f"- fired_injections: {sum(1 for r in rows if r['fired'])}",
        "",
        "## Per-Injection Attribution",
        "",
        "| injection | primitive | fired | fires | turns | target_dims | fake_values |",
        "|---|---|---:|---:|---|---|---|",
    ]
    for row in rows:
        lines.append(
            "| {injection_id} | {primitive} | {fired} | {fires} | {turns} | {dims} | {values} |".format(
                injection_id=row["injection_id"],
                primitive=row["primitive"],
                fired="yes" if row["fired"] else "no",
                fires=row["fires"],
                turns=",".join(str(t) for t in row["turns"]),
                dims=",".join(row["target_dims"]),
                values="<br>".join(row["fake_values"]),
            )
        )
    lines.extend(["", "## Per-Binding Attribution", ""])
    lines.extend(
        [
            "| injection | binding | fires | turns |",
            "|---|---|---:|---|",
        ]
    )
    for row in rows:
        for binding in row.get("bindings") or []:
            lines.append(
                "| {injection_id} | {binding_id} | {fires} | {turns} |".format(
                    injection_id=row["injection_id"],
                    binding_id=binding["binding_id"],
                    fires=binding["fires"],
                    turns=",".join(str(t) for t in binding["turns"]),
                )
            )
    uncontacted = [r["injection_id"] for r in rows if not r["fired"]]
    lines.extend(["", "## Counterfactual Notes", ""])
    if uncontacted:
        lines.append(f"- Uncontacted deception: {', '.join(uncontacted)}")
        lines.append("- Exclude uncontacted injections from strong causal claims for this episode.")
    else:
        lines.append("- All planned injections fired at least once in this episode.")
    lines.append("")
    return "\n".join(lines)
