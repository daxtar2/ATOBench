"""Summarize Basket protocol-v2 blinded report adjudication."""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _parse_bool(value: str) -> bool | None:
    normalized = value.strip().lower()
    if normalized in {"yes", "true", "y"}:
        return True
    if normalized in {"no", "false", "n"}:
        return False
    return None


def _parse_sheet(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    lines = path.read_text(encoding="utf-8").splitlines()
    for line in lines:
        if not line.startswith("| basket_report_"):
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if len(cells) < 10:
            raise ValueError(f"unexpected adjudication row shape: {line}")
        rows.append(
            {
                "blind_id": cells[0],
                "report_file": cells[1].strip("`"),
                "direct_basket_idor_verified": _parse_bool(cells[2]),
                "direct_basket_rejected_or_downgraded": _parse_bool(cells[3]),
                "direct_quote": cells[4],
                "alternate_basket_domain_authz": _parse_bool(cells[5]),
                "alternate_surface": cells[6],
                "any_authz_verified": _parse_bool(cells[7]),
                "transient_or_artifact_mention": _parse_bool(cells[8]),
                "notes": cells[9],
            }
        )
    return rows


def _by_episode_from_aggregate(aggregate: dict[str, Any]) -> dict[str, dict[str, Any]]:
    by_episode: dict[str, dict[str, Any]] = {}
    for row in aggregate["c0_summary"]["episodes"]:
        by_episode[row["episode_id"]] = {
            "condition": "C0",
            "clean_denominator": row.get("clean_denominator"),
            "report_closure": row.get("report_closure"),
        }
    for row in aggregate["c1_summary"]["episodes"]:
        by_episode[row["episode_id"]] = {
            "condition": "C1",
            "planned_dose": row.get("planned_dose"),
            "realized_dose": row.get("realized_dose"),
            "full_planned_dose_reached": row.get("full_planned_dose_reached"),
            "exact_recovery": row.get("exact_recovery"),
            "target_surface_closure": row.get("target_surface_closure"),
            "mission_compensation": row.get("mission_compensation"),
            "brs": row.get("brs"),
        }
    return by_episode


def _count(rows: list[dict[str, Any]], key: str, value: bool = True) -> int:
    return sum(1 for row in rows if row.get(key) is value)


def _condition_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "episodes": len(rows),
        "direct_basket_idor_verified": _count(rows, "direct_basket_idor_verified"),
        "direct_basket_rejected_or_downgraded": _count(rows, "direct_basket_rejected_or_downgraded"),
        "alternate_basket_domain_authz": _count(rows, "alternate_basket_domain_authz"),
        "any_authz_verified": _count(rows, "any_authz_verified"),
        "transient_or_artifact_mention": _count(rows, "transient_or_artifact_mention"),
    }


def summarize(
    *,
    adjudication_sheet: Path,
    private_manifest: Path,
    aggregate_json: Path,
    output_json: Path,
    output_md: Path,
) -> dict[str, Any]:
    sheet_rows = _parse_sheet(adjudication_sheet)
    private = json.loads(private_manifest.read_text(encoding="utf-8"))
    aggregate = json.loads(aggregate_json.read_text(encoding="utf-8"))
    private_by_blind = {row["blind_id"]: row for row in private["records"]}
    aggregate_by_episode = _by_episode_from_aggregate(aggregate)

    joined: list[dict[str, Any]] = []
    for row in sheet_rows:
        private_row = private_by_blind[row["blind_id"]]
        episode_id = private_row["episode_id"]
        joined.append(
            {
                **row,
                "pair": private_row["pair"],
                "condition": private_row["condition"],
                "episode_id": episode_id,
                "run_dir": private_row["run_dir"],
                "aggregate": aggregate_by_episode.get(episode_id, {}),
            }
        )

    c0_rows = [row for row in joined if row["condition"] == "C0"]
    c1_rows = [row for row in joined if row["condition"] == "C1"]
    result = {
        "schema_version": "atobench.basket_protocol_v2_adjudication_summary.v1",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "inputs": {
            "adjudication_sheet": str(adjudication_sheet),
            "private_manifest": str(private_manifest),
            "aggregate_json": str(aggregate_json),
        },
        "blind_summary": _condition_summary(joined),
        "condition_summary": {
            "C0": _condition_summary(c0_rows),
            "C1": _condition_summary(c1_rows),
        },
        "brs_alignment": {
            "c1_adjudicated_direct_verified": _count(c1_rows, "direct_basket_idor_verified"),
            "c1_brs_direct_closure": sum(
                1 for row in c1_rows if ((row["aggregate"].get("brs") or {}).get("report_closure_direct_ground_truth") is True)
            ),
            "c1_adjudicated_direct_rejected": _count(c1_rows, "direct_basket_rejected_or_downgraded"),
            "c1_brs_target_surface_adoption": sum(
                1 for row in c1_rows if ((row["aggregate"].get("brs") or {}).get("adoption_on_direct_rest_basket_surface") is True)
            ),
            "c1_brs_exact_recovery": sum(1 for row in c1_rows if row["aggregate"].get("exact_recovery") is True),
        },
        "records": joined,
        "interpretation": {
            "report_level_result": (
                "Report-only adjudication supports the aggregate direction: C0 direct Basket closure is 3/3; "
                "C1 direct Basket closure is 2/3; C1 direct Basket rejected/downgraded is 1/3."
            ),
            "paper_claim_boundary": (
                "The Basket persistent K=2 AOU produces a mixed BRS distribution, not stable suppression: "
                "one treatment report adopts/rejects the direct Basket hypothesis, while two recover and close it."
            ),
        },
    }
    output_json.parent.mkdir(parents=True, exist_ok=True)
    output_json.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    _write_markdown(result, output_md)
    return result


def _write_markdown(result: dict[str, Any], path: Path) -> None:
    blind = result["blind_summary"]
    c0 = result["condition_summary"]["C0"]
    c1 = result["condition_summary"]["C1"]
    align = result["brs_alignment"]
    lines = [
        "# Basket Protocol-v2 Adjudication Summary",
        "",
        f"Created: `{result['created_at']}`",
        "",
        "## Report-Only Blind Counts",
        "",
        f"- Reports adjudicated: `{blind['episodes']}`",
        f"- Direct Basket IDOR verified: `{blind['direct_basket_idor_verified']}/{blind['episodes']}`",
        f"- Direct Basket rejected/downgraded: `{blind['direct_basket_rejected_or_downgraded']}/{blind['episodes']}`",
        f"- Any authorization finding verified: `{blind['any_authz_verified']}/{blind['episodes']}`",
        f"- Qualifying alternate basket-domain authz: `{blind['alternate_basket_domain_authz']}/{blind['episodes']}`",
        f"- Transient/artifact basket mention: `{blind['transient_or_artifact_mention']}/{blind['episodes']}`",
        "",
        "## Unblinded Condition Counts",
        "",
        "| Condition | Reports | Direct Basket verified | Direct Basket rejected/downgraded | Any authz verified | Alternate basket-domain authz | Transient/artifact mention |",
        "|---|---:|---:|---:|---:|---:|---:|",
        f"| C0 | {c0['episodes']} | {c0['direct_basket_idor_verified']} | {c0['direct_basket_rejected_or_downgraded']} | {c0['any_authz_verified']} | {c0['alternate_basket_domain_authz']} | {c0['transient_or_artifact_mention']} |",
        f"| C1 | {c1['episodes']} | {c1['direct_basket_idor_verified']} | {c1['direct_basket_rejected_or_downgraded']} | {c1['any_authz_verified']} | {c1['alternate_basket_domain_authz']} | {c1['transient_or_artifact_mention']} |",
        "",
        "## BRS Alignment",
        "",
        f"- C1 adjudicated direct Basket verified: `{align['c1_adjudicated_direct_verified']}/3`",
        f"- C1 BRS direct closure: `{align['c1_brs_direct_closure']}/3`",
        f"- C1 adjudicated direct rejected/downgraded: `{align['c1_adjudicated_direct_rejected']}/3`",
        f"- C1 BRS target-surface adoption: `{align['c1_brs_target_surface_adoption']}/3`",
        f"- C1 BRS exact recovery: `{align['c1_brs_exact_recovery']}/3`",
        "",
        "## Per-Report Mapping",
        "",
        "| Blind ID | Condition | Pair | Direct verified | Direct rejected | Any authz | Transient/artifact | BRS outcome |",
        "|---|---|---:|---:|---:|---:|---:|---|",
    ]
    for row in result["records"]:
        brs = row["aggregate"].get("brs") or {}
        lines.append(
            "| `{blind}` | `{condition}` | {pair} | `{verified}` | `{rejected}` | `{authz}` | `{transient}` | `{outcome}` |".format(
                blind=row["blind_id"],
                condition=row["condition"],
                pair=row["pair"],
                verified=row["direct_basket_idor_verified"],
                rejected=row["direct_basket_rejected_or_downgraded"],
                authz=row["any_authz_verified"],
                transient=row["transient_or_artifact_mention"],
                outcome=brs.get("outcome_label", "-"),
            )
        )
    lines.extend(
        [
            "",
            "## Interpretation",
            "",
            result["interpretation"]["report_level_result"],
            "",
            result["interpretation"]["paper_claim_boundary"],
            "",
        ]
    )
    path.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    base = Path("targets/juice-shop/experiments/juice_shop_basket_formal/analysis")
    packet = base / "basket_protocol_v2_adjudication_packet"
    parser.add_argument("--adjudication-sheet", type=Path, default=packet / "public/adjudication_sheet.md")
    parser.add_argument("--private-manifest", type=Path, default=packet / "private_manifest.json")
    parser.add_argument("--aggregate-json", type=Path, default=base / "basket_protocol_v2_n3_aggregate_20260715.json")
    parser.add_argument("--output-json", type=Path, default=base / "basket_protocol_v2_adjudication_summary_20260715.json")
    parser.add_argument("--output-md", type=Path, default=base / "BASKET_PROTOCOL_V2_ADJUDICATION_SUMMARY_20260715.md")
    args = parser.parse_args()
    result = summarize(
        adjudication_sheet=args.adjudication_sheet,
        private_manifest=args.private_manifest,
        aggregate_json=args.aggregate_json,
        output_json=args.output_json,
        output_md=args.output_md,
    )
    print(
        json.dumps(
            {
                "blind_reports": result["blind_summary"]["episodes"],
                "c0_direct_verified": result["condition_summary"]["C0"]["direct_basket_idor_verified"],
                "c1_direct_verified": result["condition_summary"]["C1"]["direct_basket_idor_verified"],
                "c1_direct_rejected": result["condition_summary"]["C1"]["direct_basket_rejected_or_downgraded"],
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
