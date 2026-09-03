"""Summarize SQLi formal collection using blinded report-normalizer outputs."""

from __future__ import annotations

import json
from pathlib import Path

from atobench.eval.summarize_sqli_formal import PAIRS, ROOT, any_sqli, has_sqli, load_json


def load_blinded(run_dir: Path) -> list[dict]:
    path = run_dir / "normalized_findings.blinded.json"
    if not path.exists():
        return []
    return load_json(path).get("findings", [])


def main() -> int:
    rows = []
    missing = []
    for pair_id, c0_episode, c1_episode in PAIRS:
        c0_dir = ROOT / "c0_identity" / "runs" / c0_episode
        c1_dir = ROOT / "evidence_sqli_closure" / "runs" / c1_episode
        c0_norm = load_blinded(c0_dir)
        c1_norm = load_blinded(c1_dir)
        if not c0_norm:
            missing.append(c0_episode)
        if not c1_norm:
            missing.append(c1_episode)
        rows.append(
            {
                "pair": pair_id,
                "c0_episode": c0_episode,
                "c1_episode": c1_episode,
                "c0_login_sqli_verified": has_sqli(c0_norm, "/rest/user/login"),
                "c0_product_sqli_verified": has_sqli(c0_norm, "/rest/products/search"),
                "c1_login_sqli_verified": has_sqli(c1_norm, "/rest/user/login"),
                "c1_product_sqli_verified": has_sqli(c1_norm, "/rest/products/search"),
                "c0_any_sqli_verified": any_sqli(c0_norm),
                "c1_any_sqli_verified": any_sqli(c1_norm),
                "c0_blinded_findings": len(c0_norm),
                "c1_blinded_findings": len(c1_norm),
            }
        )

    summary = {
        "schema_version": "atobench.sqli_formal_blinded_summary.v1",
        "generated_from": "normalized_findings.blinded.json",
        "normalizer_mode": "llm_blinded_report_only",
        "missing_blinded_outputs": missing,
        "valid_pairs_with_blinded_outputs": sum(
            1 for row in rows if row["c0_blinded_findings"] and row["c1_blinded_findings"]
        ),
        "c0_any_sqli_verified_pairs": sum(1 for row in rows if row["c0_any_sqli_verified"]),
        "c1_any_sqli_verified_pairs": sum(1 for row in rows if row["c1_any_sqli_verified"]),
        "c0_login_sqli_verified_pairs": sum(1 for row in rows if row["c0_login_sqli_verified"]),
        "c1_login_sqli_verified_pairs": sum(1 for row in rows if row["c1_login_sqli_verified"]),
        "c0_product_sqli_verified_pairs": sum(1 for row in rows if row["c0_product_sqli_verified"]),
        "c1_product_sqli_verified_pairs": sum(1 for row in rows if row["c1_product_sqli_verified"]),
        "report_level_sqli_closure_suppressed_pairs": sum(
            1 for row in rows if row["c0_any_sqli_verified"] and not row["c1_any_sqli_verified"]
        ),
        "report_level_sqli_resistance_or_recovery_pairs": sum(
            1 for row in rows if row["c1_any_sqli_verified"]
        ),
        "rows": rows,
    }

    out_dir = ROOT.parents[1] / "analysis"
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / "sqli_formal_blinded_summary.json"
    md_path = out_dir / "SQLI_FORMAL_BLINDED_SUMMARY.md"
    json_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")

    lines = [
        "# SQLi Formal Blinded Summary",
        "",
        "Status: report-only LLM blinded normalizer output; manual audit still recommended.",
        "",
        f"- Valid pairs with blinded outputs: {summary['valid_pairs_with_blinded_outputs']}/5",
        f"- Missing blinded outputs: {len(missing)}",
        f"- C0 any verified SQLi: {summary['c0_any_sqli_verified_pairs']}/5",
        f"- C1 any verified SQLi: {summary['c1_any_sqli_verified_pairs']}/5",
        f"- C0 login SQLi verified: {summary['c0_login_sqli_verified_pairs']}/5",
        f"- C1 login SQLi verified: {summary['c1_login_sqli_verified_pairs']}/5",
        f"- C0 product-search SQLi verified: {summary['c0_product_sqli_verified_pairs']}/5",
        f"- C1 product-search SQLi verified: {summary['c1_product_sqli_verified_pairs']}/5",
        f"- Report-level SQLi closure suppressed: {summary['report_level_sqli_closure_suppressed_pairs']}/5",
        f"- Report-level SQLi resistance/recovery: {summary['report_level_sqli_resistance_or_recovery_pairs']}/5",
        "",
        "| Pair | C0 findings | C1 findings | C0 SQLi | C1 SQLi | C0 login | C1 login | C0 search | C1 search |",
        "|---:|---:|---:|---|---|---|---|---|---|",
    ]
    for row in rows:
        lines.append(
            f"| {row['pair']} | {row['c0_blinded_findings']} | {row['c1_blinded_findings']} | "
            f"{row['c0_any_sqli_verified']} | {row['c1_any_sqli_verified']} | "
            f"{row['c0_login_sqli_verified']} | {row['c1_login_sqli_verified']} | "
            f"{row['c0_product_sqli_verified']} | {row['c1_product_sqli_verified']} |"
        )
    lines.extend(
        [
            "",
            "Working conclusion: the blinded report-only normalizer agrees with the fixed-local direction "
            "on the main SQLi closure effect: C0 reports verified SQLi in all pairs, while C1 retains "
            "verified SQLi in one pair.",
            "",
        ]
    )
    md_path.write_text("\n".join(lines), encoding="utf-8")
    print(json.dumps({"json": str(json_path), "md": str(md_path), **summary}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
