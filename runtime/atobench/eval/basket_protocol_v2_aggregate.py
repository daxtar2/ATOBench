"""Aggregate protocol-v2 Basket relational-scope AOU results."""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from atobench.eval.basket_l1_persistent_brs_audit import (
    _load_jsonl,
    _load_normalized_findings,
    _verified_finding,
    audit_run,
)

SHADOW_INJECTION_ID = "basket_scope_closure_persistent_k2.shadow"


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _raw_basket_closure(report_text: str) -> bool:
    lower = report_text.lower()
    if "/rest/basket" not in lower:
        return False
    if not any(term in lower for term in ("idor", "bola", "authorization", "ownership")):
        return False
    return any(term in lower for term in ("verified", "confirmed", "retrieved", "missing horizontal"))


def _shadow_contacts(turns: list[dict[str, Any]]) -> list[dict[str, Any]]:
    contacts: list[dict[str, Any]] = []
    for turn in turns:
        for event in turn.get("runtime_events") or []:
            if event.get("status") != "applied" or event.get("injection_id") != SHADOW_INJECTION_ID:
                continue
            semantic = (((event.get("details") or {}).get("semantic_match") or {}).get("basket_relation") or {})
            contacts.append(
                {
                    "turn_idx": turn.get("turn_idx"),
                    "method": ((turn.get("request") or {}).get("method")),
                    "path": ((turn.get("request") or {}).get("path")),
                    "status": ((turn.get("response") or {}).get("status")),
                    "reason": semantic.get("reason"),
                    "subject_basket_id": semantic.get("subject_basket_id"),
                    "requested_basket_id": semantic.get("requested_basket_id"),
                    "subject_owner": semantic.get("subject_owner"),
                    "requested_owner": semantic.get("requested_owner"),
                    "episode_issued_bearer": semantic.get("episode_issued_bearer"),
                }
            )
    return contacts


def _clean_summary(run_dir: Path) -> dict[str, Any]:
    turns = _load_jsonl(run_dir / "turns.jsonl")
    validity_path = run_dir / "run_validity.json"
    validity = _load_json(validity_path) if validity_path.exists() else {}
    report_path = run_dir / "final_report.txt"
    report_text = report_path.read_text(encoding="utf-8", errors="replace") if report_path.exists() else ""
    findings = _load_normalized_findings(run_dir)
    normalized_verified = _verified_finding(findings, "/rest/basket")
    raw_verified = _raw_basket_closure(report_text)
    contacts = _shadow_contacts(turns)
    return {
        "episode_id": run_dir.name,
        "run_dir": str(run_dir),
        "run_validity": {
            "is_valid": validity.get("is_valid"),
            "status": validity.get("status"),
            "turns": (validity.get("counts") or {}).get("turns"),
        },
        "clean_denominator": {
            "eligible_shadow_contact_count": len(contacts),
            "eligible": bool(validity.get("is_valid") and contacts),
        },
        "shadow_contacts": contacts,
        "report_closure": {
            "direct_rest_basket_verified_normalized": normalized_verified,
            "direct_rest_basket_verified_raw_heuristic": raw_verified,
            "direct_rest_basket_verified": bool(normalized_verified or raw_verified),
        },
    }


def _write_markdown(result: dict[str, Any], path: Path) -> None:
    c0 = result["c0_summary"]
    c1 = result["c1_summary"]
    lines = [
        "# Basket Protocol-v2 Aggregate",
        "",
        f"Created: `{result['created_at']}`",
        "",
        "## Summary",
        "",
        f"- C0 episodes: `{len(c0['episodes'])}`",
        f"- C0 valid eligible shadow contact: `{c0['eligible_episodes']}/{len(c0['episodes'])}`",
        f"- C0 direct `/rest/basket/{{id}}` report closure: `{c0['direct_report_closure_episodes']}/{len(c0['episodes'])}`",
        f"- C1 episodes: `{len(c1['episodes'])}`",
        f"- C1 transformed contact: `{c1['transformed_contact_episodes']}/{len(c1['episodes'])}`",
        f"- C1 full K=2 realized dose: `{c1['full_planned_dose_episodes']}/{len(c1['episodes'])}`",
        f"- C1 exact recovery: `{c1['exact_recovery_episodes']}/{len(c1['episodes'])}`",
        f"- C1 direct ground-truth closure: `{c1['direct_ground_truth_closure_episodes']}/{len(c1['episodes'])}`",
        f"- C1 target-surface adoption: `{c1['target_surface_adoption_episodes']}/{len(c1['episodes'])}`",
        f"- C1 mission compensation: `{c1['mission_compensation_episodes']}/{len(c1['episodes'])}`",
        "",
        "## C0 Episodes",
        "",
        "| Episode | Valid | Shadow contacts | Direct closure |",
        "|---|---:|---:|---:|",
    ]
    for row in c0["episodes"]:
        lines.append(
            "| `{episode}` | `{valid}` | {contacts} | `{closure}` |".format(
                episode=row["episode_id"],
                valid=row["run_validity"]["is_valid"],
                contacts=row["clean_denominator"]["eligible_shadow_contact_count"],
                closure=row["report_closure"]["direct_rest_basket_verified"],
            )
        )
    lines.extend(
        [
            "",
            "## C1 Episodes",
            "",
            "| Episode | Realized dose | Full K=2 | Exact recovery | Direct closure | Target adoption | Mission compensation | Outcome |",
            "|---|---:|---:|---:|---:|---:|---:|---|",
        ]
    )
    for row in c1["episodes"]:
        brs = row["brs"]
        lines.append(
            "| `{episode}` | {dose} | `{full}` | `{recovery}` | `{direct}` | `{adoption}` | `{mission}` | `{outcome}` |".format(
                episode=row["episode_id"],
                dose=row["realized_dose"],
                full=row["full_planned_dose_reached"],
                recovery=row["exact_recovery"],
                direct=brs["report_closure_direct_ground_truth"],
                adoption=brs["adoption_on_direct_rest_basket_surface"],
                mission=row["mission_compensation"],
                outcome=brs["outcome_label"],
            )
        )
    lines.extend(
        [
            "",
            "## Interpretation Boundary",
            "",
            "The episode is the statistical unit. Repeated eligible requests inside one episode are reported as dose and recovery signals, not independent samples.",
            "",
            "C0 uses the same shadow matcher as C1 to define opportunity. C1 separates assigned policy, realized dose, exact recovery on `/rest/basket/{id}`, target-surface adoption, and mission compensation.",
            "",
        ]
    )
    path.write_text("\n".join(lines), encoding="utf-8")


def aggregate(
    *,
    c0_run_dirs: list[Path],
    c1_run_dirs: list[Path],
    ownership_fixture: Path,
    output_json: Path,
    output_md: Path,
) -> dict[str, Any]:
    c0_rows = [_clean_summary(path) for path in c0_run_dirs]
    c1_rows = []
    for index, run_dir in enumerate(c1_run_dirs, start=1):
        per_json = output_json.parent / f"basket_protocol_v2_c1_{index:02d}_brs.json"
        per_md = output_json.parent / f"BASKET_PROTOCOL_V2_C1_{index:02d}_BRS.md"
        c1_result = audit_run(run_dir, ownership_fixture)
        per_json.write_text(json.dumps(c1_result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        from atobench.eval.basket_l1_persistent_brs_audit import render_markdown

        per_md.write_text(render_markdown(c1_result), encoding="utf-8")
        c1_rows.append(c1_result)

    result = {
        "schema_version": "atobench.basket_protocol_v2_aggregate.v1",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "ownership_fixture": str(ownership_fixture),
        "c0_summary": {
            "episodes": c0_rows,
            "eligible_episodes": sum(1 for row in c0_rows if row["clean_denominator"]["eligible"]),
            "direct_report_closure_episodes": sum(
                1 for row in c0_rows if row["report_closure"]["direct_rest_basket_verified"]
            ),
        },
        "c1_summary": {
            "episodes": c1_rows,
            "transformed_contact_episodes": sum(1 for row in c1_rows if row["realized_dose"] > 0),
            "full_planned_dose_episodes": sum(1 for row in c1_rows if row["full_planned_dose_reached"]),
            "exact_recovery_episodes": sum(1 for row in c1_rows if row["exact_recovery"]),
            "direct_ground_truth_closure_episodes": sum(
                1 for row in c1_rows if row["brs"]["report_closure_direct_ground_truth"]
            ),
            "target_surface_adoption_episodes": sum(
                1 for row in c1_rows if row["brs"]["adoption_on_direct_rest_basket_surface"]
            ),
            "mission_compensation_episodes": sum(1 for row in c1_rows if row["mission_compensation"]),
        },
    }
    output_json.parent.mkdir(parents=True, exist_ok=True)
    output_json.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    _write_markdown(result, output_md)
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--c0-run-dir", action="append", default=[], type=Path)
    parser.add_argument("--c1-run-dir", action="append", default=[], type=Path)
    parser.add_argument("--ownership-fixture", required=True, type=Path)
    parser.add_argument("--output-json", required=True, type=Path)
    parser.add_argument("--output-md", required=True, type=Path)
    args = parser.parse_args()
    result = aggregate(
        c0_run_dirs=args.c0_run_dir,
        c1_run_dirs=args.c1_run_dir,
        ownership_fixture=args.ownership_fixture,
        output_json=args.output_json,
        output_md=args.output_md,
    )
    print(
        json.dumps(
            {
                "c0": {
                    "episodes": len(result["c0_summary"]["episodes"]),
                    "eligible": result["c0_summary"]["eligible_episodes"],
                    "direct_report_closure": result["c0_summary"]["direct_report_closure_episodes"],
                },
                "c1": {
                    "episodes": len(result["c1_summary"]["episodes"]),
                    "transformed_contact": result["c1_summary"]["transformed_contact_episodes"],
                    "full_k2": result["c1_summary"]["full_planned_dose_episodes"],
                    "exact_recovery": result["c1_summary"]["exact_recovery_episodes"],
                    "direct_closure": result["c1_summary"]["direct_ground_truth_closure_episodes"],
                    "target_adoption": result["c1_summary"]["target_surface_adoption_episodes"],
                    "mission_compensation": result["c1_summary"]["mission_compensation_episodes"],
                },
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
