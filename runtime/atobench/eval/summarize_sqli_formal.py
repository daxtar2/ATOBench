"""Summarize the SQLi formal collection fixed-local audit artifacts."""

from __future__ import annotations

import json
import statistics
from pathlib import Path


ROOT = (
    Path(__file__).resolve().parents[1]
    / "targets/juice-shop/experiments/juice_shop_sqli_formal/frozen_runs/"
    "juice-shop-confirmatory-v1"
)

PAIRS = [
    (1, "ep_sqli_formal_c0_20260714_150655_f0a5fa", "ep_sqli_formal_c1_20260714_151334_472088"),
    (2, "ep_sqli_formal_c0_20260714_152508_b4c45f", "ep_sqli_formal_c1_20260714_153154_97bb2c"),
    (3, "ep_sqli_formal_c0_20260714_154950_855ee1", "ep_sqli_formal_c1_20260714_155743_45fbba"),
    (4, "ep_sqli_formal_c0_20260714_161228_7a2d92", "ep_sqli_formal_c1_20260714_161948_39ffb6"),
    (5, "ep_sqli_formal_c0_20260714_162857_ce9fcc", "ep_sqli_formal_c1_20260714_163512_8d8a90"),
]


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def load_norm(run_dir: Path) -> list[dict]:
    path = run_dir / "normalized_findings.fixed_local.json"
    if not path.exists():
        return []
    return load_json(path).get("findings", [])


def has_sqli(findings: list[dict], endpoint: str) -> bool:
    return any(
        finding.get("claim_level") == "verified"
        and finding.get("vuln_class") == "sqli"
        and endpoint in (finding.get("endpoint") or "")
        for finding in findings
    )


def any_sqli(findings: list[dict]) -> bool:
    return any(
        finding.get("claim_level") == "verified" and finding.get("vuln_class") == "sqli"
        for finding in findings
    )


def main() -> int:
    rows = []
    for pair_id, c0_episode, c1_episode in PAIRS:
        c0_dir = ROOT / "c0_identity" / "runs" / c0_episode
        c1_dir = ROOT / "evidence_sqli_closure" / "runs" / c1_episode
        c0_norm = load_norm(c0_dir)
        c1_norm = load_norm(c1_dir)
        effect = load_json(c1_dir / "pentest_effect.json")
        c0_validity = load_json(c0_dir / "run_validity.json")
        c1_validity = load_json(c1_dir / "run_validity.json")
        rows.append(
            {
                "pair": pair_id,
                "c0_episode": c0_episode,
                "c1_episode": c1_episode,
                "c0_valid": c0_validity.get("is_valid"),
                "c1_valid": c1_validity.get("is_valid"),
                "c0_turns": c0_validity.get("counts", {}).get("turns"),
                "c1_turns": c1_validity.get("counts", {}).get("turns"),
                "c1_deceptive_turns": c1_validity.get("counts", {}).get("deceptive_turns"),
                "fired_injections": effect.get("opportunity", {}).get("fired_injections"),
                "contact_rate": effect.get("opportunity", {}).get("contact_rate"),
                "c0_login_sqli_verified": has_sqli(c0_norm, "/rest/user/login"),
                "c0_product_sqli_verified": has_sqli(c0_norm, "/rest/products/search"),
                "c1_login_sqli_verified": has_sqli(c1_norm, "/rest/user/login"),
                "c1_product_sqli_verified": has_sqli(c1_norm, "/rest/products/search"),
                "c0_any_sqli_verified": any_sqli(c0_norm),
                "c1_any_sqli_verified": any_sqli(c1_norm),
                "c0_fixed_local_findings": len(c0_norm),
                "c1_fixed_local_findings": len(c1_norm),
                "gave_up": effect.get("auxiliary", {}).get("gave_up"),
                "clean_finding_disappearance_rate": effect.get("outcome", {}).get("clean_finding_disappearance_rate"),
                "clean_finding_polarity_flip_rate": effect.get("outcome", {}).get("clean_finding_polarity_flip_rate"),
            }
        )

    deceptive_turns = [row["c1_deceptive_turns"] for row in rows]
    summary = {
        "schema_version": "atobench.sqli_formal_interim_summary.v1",
        "generated_from": "fixed_local_normalized_findings_and_pentest_effect_json",
        "valid_pairs": sum(1 for row in rows if row["c0_valid"] and row["c1_valid"]),
        "excluded_infrastructure_attempts": 1,
        "c1_contact_both_rules_pairs": sum(
            1 for row in rows if row["fired_injections"] == 2 and row["contact_rate"] == 1.0
        ),
        "c0_any_sqli_verified_pairs": sum(1 for row in rows if row["c0_any_sqli_verified"]),
        "c1_any_sqli_verified_pairs": sum(1 for row in rows if row["c1_any_sqli_verified"]),
        "c0_login_sqli_verified_pairs": sum(1 for row in rows if row["c0_login_sqli_verified"]),
        "c1_login_sqli_verified_pairs": sum(1 for row in rows if row["c1_login_sqli_verified"]),
        "c0_product_sqli_verified_pairs": sum(1 for row in rows if row["c0_product_sqli_verified"]),
        "c1_product_sqli_verified_pairs": sum(1 for row in rows if row["c1_product_sqli_verified"]),
        "c1_deceptive_turns_total": sum(deceptive_turns),
        "c1_deceptive_turns_mean": statistics.mean(deceptive_turns),
        "rows": rows,
    }

    out_dir = ROOT.parents[1] / "analysis"
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / "sqli_formal_interim_summary.json"
    md_path = out_dir / "SQLI_FORMAL_INTERIM_SUMMARY.md"
    json_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")

    lines = [
        "# SQLi Formal Interim Summary",
        "",
        "Status: fixed-local preliminary audit; blinded/manual adjudication pending.",
        "",
        f"- Valid pairs: {summary['valid_pairs']}/5",
        f"- Excluded infrastructure attempts: {summary['excluded_infrastructure_attempts']}",
        f"- C1 pairs with both SQLi rules contacted: {summary['c1_contact_both_rules_pairs']}/5",
        f"- C0 any verified SQLi by fixed-local parser: {summary['c0_any_sqli_verified_pairs']}/5",
        f"- C1 any verified SQLi by fixed-local parser: {summary['c1_any_sqli_verified_pairs']}/5",
        f"- C0 login SQLi verified: {summary['c0_login_sqli_verified_pairs']}/5",
        f"- C1 login SQLi verified: {summary['c1_login_sqli_verified_pairs']}/5",
        f"- C0 product-search SQLi verified: {summary['c0_product_sqli_verified_pairs']}/5",
        f"- C1 product-search SQLi verified: {summary['c1_product_sqli_verified_pairs']}/5",
        f"- C1 transformed-contact turns: total {summary['c1_deceptive_turns_total']}, "
        f"mean {summary['c1_deceptive_turns_mean']:.1f}",
        "",
        "| Pair | C0 turns | C1 turns | C1 deceptive | C0 SQLi | C1 SQLi | Both rules contacted | gave_up |",
        "|---:|---:|---:|---:|---|---|---|---|",
    ]
    for row in rows:
        both_rules = row["fired_injections"] == 2 and row["contact_rate"] == 1.0
        lines.append(
            f"| {row['pair']} | {row['c0_turns']} | {row['c1_turns']} | "
            f"{row['c1_deceptive_turns']} | {row['c0_any_sqli_verified']} | "
            f"{row['c1_any_sqli_verified']} | {both_rules} | {row['gave_up']} |"
        )
    lines.extend(
        [
            "",
            "Interpretation: fixed-local parser shows stable report-level SQLi closure "
            "difference under full C1 contact, while C1 agents continue non-SQLi testing. "
            "Treat this as preliminary until blinded/manual adjudication and SQLi-specific "
            "BRS audit are complete.",
            "",
        ]
    )
    md_path.write_text("\n".join(lines), encoding="utf-8")
    print(json.dumps({"json": str(json_path), "md": str(md_path), **summary}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
