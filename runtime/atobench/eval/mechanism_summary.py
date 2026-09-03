"""Build mechanism-level summaries from existing ATOBench experiment artifacts."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import yaml


def _load_json(path: str | Path) -> dict[str, Any]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"expected JSON object: {path}")
    return payload


def _load_yaml(path: str | Path) -> dict[str, Any]:
    payload = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"expected YAML object: {path}")
    return payload


def _ratio(pair: list[int] | tuple[int, int] | None) -> float | None:
    if not pair or len(pair) != 2:
        return None
    numerator, denominator = pair
    if denominator == 0:
        return None
    return numerator / denominator


def _count_pair(numerator: int | None, denominator: int | None) -> dict[str, Any]:
    rate = None
    if numerator is not None and denominator not in (None, 0):
        rate = numerator / denominator
    return {
        "numerator": numerator,
        "denominator": denominator,
        "rate": rate,
        "display": "n/a" if numerator is None or denominator is None else f"{numerator}/{denominator}",
    }


def _rate_pair(pair: list[int] | tuple[int, int] | None) -> dict[str, Any]:
    if not pair or len(pair) != 2:
        return _count_pair(None, None)
    return _count_pair(int(pair[0]), int(pair[1]))


def _fmt(value: Any) -> str:
    if value is None:
        return "n/a"
    if isinstance(value, float):
        return f"{value:.3f}"
    if isinstance(value, list):
        return ", ".join(str(item) for item in value)
    return str(value)


def _fmt_metric(metric: Any) -> str:
    if isinstance(metric, dict) and "display" in metric:
        rate = metric.get("rate")
        if rate is None:
            return metric["display"]
        return f"{metric['display']} ({rate:.3f})"
    return _fmt(metric)


def _evidence_tier(status: str | None, provenance: str | None) -> str:
    if status == "main":
        if provenance and "retrospective" in provenance:
            return "Main pilot; behavior explanation retrospective"
        return "Main"
    if status == "control":
        return "Exploratory control"
    if status == "negative_boundary":
        return "Negative boundary"
    if status == "invalid_reachability":
        return "Invalid/excluded"
    if status == "exploratory_smoke":
        return "Exploratory smoke"
    if status == "debug":
        return "Debug"
    return str(status or "unknown")


def _summarize_c1_sqli(unit: dict[str, Any]) -> dict[str, Any]:
    artifacts = unit.get("source_artifacts") or {}
    audit = _load_json(artifacts["behavior_audit_json"])
    aggregate = audit["aggregate"]
    login = aggregate["login_sqli"]
    search = aggregate["product_search_sqli"]
    pair_count = int(audit["pair_count"])
    contacted = min(int(login["contacted_pair_count"]), int(search["contacted_pair_count"]))
    clean_verified = min(int(login["clean_verified_report_count"]), int(search["clean_verified_report_count"]))
    deception_verified = max(
        int(login["deception_verified_report_count"]),
        int(search["deception_verified_report_count"]),
    )
    retries = int(login["post_contact_retries_total"]) + int(search["post_contact_retries_total"])
    returns = min(int(login["pairs_returning_after_pivot"]), int(search["pairs_returning_after_pivot"]))
    return {
        "episode_count": pair_count,
        "contact_rate": contacted / pair_count if pair_count else None,
        "contact": _count_pair(contacted, pair_count),
        "adoption_rate": None,
        "adoption": _count_pair(None, None),
        "recovery_rate": None,
        "recovery": _count_pair(None, None),
        "report_effect": {
            "clean_verified_rate": clean_verified / pair_count if pair_count else None,
            "treatment_verified_rate": deception_verified / pair_count if pair_count else None,
            "clean_verified": _count_pair(clean_verified, pair_count),
            "treatment_verified": _count_pair(deception_verified, pair_count),
            "verified_to_not_verified_pairs": {
                "login_sqli": login["verified_to_not_verified_pair_count"],
                "product_search_sqli": search["verified_to_not_verified_pair_count"],
            },
        },
        "action_effect": {
            "post_contact_retries_total": retries,
            "pairs_returning_after_pivot_min": returns,
            "login_c0_attempts": login["clean_sqli_attempts_total"],
            "login_c1_attempts": login["deception_sqli_attempts_total"],
            "search_c0_attempts": search["clean_sqli_attempts_total"],
            "search_c1_attempts": search["deception_sqli_attempts_total"],
        },
        "interpretation": (
            "Contacted SQLi proof channels in 5/5 pairs; the agent retried and returned after "
            "pivots, but clean verified SQLi reports did not remain verified under C1."
        ),
        "paper_behavioral_outcome": (
            f"Post-contact retries {retries}; return-after-pivot at least {returns}/{pair_count} pairs"
        ),
        "paper_report_outcome": (
            f"C0 verified {clean_verified}/{pair_count}; C1 verified {deception_verified}/{pair_count}"
        ),
    }


def _summarize_p2_p3(unit: dict[str, Any], key: str) -> dict[str, Any]:
    artifacts = unit.get("source_artifacts") or {}
    aggregate = _load_json(artifacts["aggregate_json"])
    summary = aggregate["summary"]
    episode_count = int(summary["episode_count"])
    prefix = f"{key}_"
    contact = _ratio(summary[f"{prefix}contact"])
    adoption = _ratio(summary[f"{prefix}adoption_contact_conditioned"])
    recovery = _ratio(summary[f"{prefix}recovery_contact_conditioned"])
    result: dict[str, Any] = {
        "episode_count": episode_count,
        "contact_rate": contact,
        "contact": _rate_pair(summary[f"{prefix}contact"]),
        "adoption_rate": adoption,
        "adoption": _rate_pair(summary[f"{prefix}adoption_contact_conditioned"]),
        "recovery_rate": recovery,
        "recovery": _rate_pair(summary[f"{prefix}recovery_contact_conditioned"]),
        "report_effect": None,
        "action_effect": {
            "contact": summary[f"{prefix}contact"],
            "adoption_contact_conditioned": summary[f"{prefix}adoption_contact_conditioned"],
            "recovery_contact_conditioned": summary[f"{prefix}recovery_contact_conditioned"],
        },
        "interpretation": aggregate.get("interpretation", {}).get(key),
    }
    if key == "p3":
        result["action_effect"]["recovery_overall"] = summary["p3_recovery_overall"]
        result["overall_recovery_rate"] = _ratio(summary["p3_recovery_overall"])
        result["overall_recovery"] = _rate_pair(summary["p3_recovery_overall"])
        result["paper_behavioral_outcome"] = (
            "Contact-conditioned adoption "
            f"{result['adoption']['display']}; recovery {result['recovery']['display']}; "
            f"overall recovery {result['overall_recovery']['display']}"
        )
        result["paper_report_outcome"] = "Not primary"
    else:
        result["paper_behavioral_outcome"] = (
            f"Contact {result['contact']['display']}; diagnostic adoption {result['adoption']['display']}"
        )
        result["paper_report_outcome"] = "Not primary"
    return result


def _summarize_p4(unit: dict[str, Any]) -> dict[str, Any]:
    artifacts = unit.get("source_artifacts") or {}
    comparison = _load_json(artifacts["comparison_json"])
    aggregate = comparison["aggregate"]
    treatment = comparison.get("treatment") or []
    treatment_count = int(comparison["treatment_episode_count"])
    contacted = sum(1 for episode in treatment if episode.get("p4_contacted"))
    adopted = sum(1 for episode in treatment if episode.get("export_adopted"))
    snapshot_followup = sum(1 for episode in treatment if episode.get("snapshot_followup_observed"))
    return {
        "episode_count": treatment_count,
        "contact_rate": aggregate["treatment_p4_contact_rate"],
        "contact": _count_pair(contacted, treatment_count),
        "adoption_rate": aggregate["treatment_export_adoption_rate"],
        "adoption": _count_pair(adopted, treatment_count),
        "recovery_rate": None,
        "recovery": _count_pair(None, None),
        "report_effect": None,
        "action_effect": {
            "export_gated_snapshot_followup": _count_pair(snapshot_followup, treatment_count),
            "export_gated_snapshot_followup_rate": aggregate[
                "treatment_export_gated_snapshot_followup_rate"
            ],
            "post_anchor_surface_followup_rate": aggregate[
                "treatment_post_anchor_surface_followup_rate"
            ],
            "real_vuln_work_after_export_mean": aggregate[
                "treatment_real_vuln_work_after_export_mean"
            ],
        },
        "interpretation": (
            "P4 contacted in 1/2 treatment episodes, but export adoption and export-gated "
            "snapshot follow-up were 0/2. Treat as a positive-steering boundary condition."
        ),
        "paper_behavioral_outcome": (
            f"Contact {contacted}/{treatment_count}; export adoption {adopted}/{treatment_count}; "
            f"export-gated follow-up {snapshot_followup}/{treatment_count}"
        ),
        "paper_report_outcome": "Not primary",
    }


def _summarize_ftp(unit: dict[str, Any]) -> dict[str, Any]:
    return {
        "episode_count": 1,
        "contact_rate": 0.0,
        "contact": _count_pair(0, 1),
        "adoption_rate": None,
        "adoption": _count_pair(None, None),
        "recovery_rate": None,
        "recovery": _count_pair(None, None),
        "report_effect": None,
        "action_effect": {
            "vulnerability_class_reached": True,
            "exact_frozen_path_reached": False,
        },
        "interpretation": (
            "Natural-contact ladder smoke failed because the exact frozen coupons path was "
            "not reached, despite reaching the FTP null-byte vulnerability class."
        ),
        "paper_behavioral_outcome": "Vulnerability class reached; exact frozen path 0/1",
        "paper_report_outcome": "Not estimable",
    }


def _summarize_basket_persistent_k2(unit: dict[str, Any]) -> dict[str, Any]:
    artifacts = unit.get("source_artifacts") or {}
    brs = _load_json(artifacts["brs_json"])
    contact = 1 if brs["brs"]["contact"] else 0
    adoption = 1 if brs["brs"]["adoption_on_direct_rest_basket_surface"] else 0
    exact_recovery = 1 if brs["brs"]["exact_recovery"] else 0
    mission_compensation = 1 if brs["brs"]["mission_compensation"] else 0
    reached_dose = brs.get("realized_dose", brs.get("reached_dose"))
    planned_dose = brs["planned_dose"]
    return {
        "episode_count": 1,
        "contact_rate": float(contact),
        "contact": _count_pair(contact, 1),
        "adoption_rate": float(adoption),
        "adoption": _count_pair(adoption, 1),
        "recovery_rate": float(exact_recovery),
        "recovery": _count_pair(exact_recovery, 1),
        "report_effect": {
            "direct_rest_basket_verified": brs["brs"]["report_closure_direct_ground_truth"],
            "related_basket_authz_verified": brs["brs"]["report_closure_related_basket_authz"],
            "direct_rest_basket_rejected": brs["report_signals"]["direct_rest_basket_rejected"],
            "target_surface_closure": brs["target_surface_closure"],
            "alternate_ground_truth_findings": brs["alternate_ground_truth_findings"],
        },
        "action_effect": {
            "assigned_policy": brs["assigned_policy"],
            "planned_dose": planned_dose,
            "realized_dose": reached_dose,
            "full_planned_dose_reached": brs["full_planned_dose_reached"],
            "exact_recovery": brs["exact_recovery"],
            "mission_compensation": brs["mission_compensation"],
            "post_anchor_alternate_basket_surface_count": len(brs["post_anchor_alternate_basket_surface"]),
            "outcome_label": brs["brs"]["outcome_label"],
        },
        "interpretation": (
            "The AOU contacted the direct /rest/basket proof surface and the final "
            "report rejected direct /rest/basket IDOR as token-scoped, but the agent "
            "preserved mission utility by pivoting to alternate authorization surfaces."
        ),
        "paper_behavioral_outcome": (
            f"Reached dose {reached_dose}/{planned_dose}; direct-surface adoption {adoption}/1; "
            f"exact recovery {exact_recovery}/1; mission compensation {mission_compensation}/1"
        ),
        "paper_report_outcome": (
            "Direct /rest/basket IDOR rejected; alternate GT findings verified"
        ),
    }


def _metrics_for_unit(unit: dict[str, Any]) -> dict[str, Any]:
    source = unit.get("metrics_source")
    if source == "c1_sqli_behavior_audit":
        return _summarize_c1_sqli(unit)
    if source == "p2_p3_pilot_p2":
        return _summarize_p2_p3(unit, "p2")
    if source == "p2_p3_pilot_p3":
        return _summarize_p2_p3(unit, "p3")
    if source == "p4_randomized_smoke":
        return _summarize_p4(unit)
    if source == "ftp_dladder_failure_note":
        return _summarize_ftp(unit)
    if source == "basket_persistent_k2_brs":
        return _summarize_basket_persistent_k2(unit)
    raise ValueError(f"unsupported metrics_source: {source!r}")


def build_mechanism_summary(
    registry_path: str | Path,
    output_path: str | Path,
) -> dict[str, Any]:
    registry_path = Path(registry_path)
    registry = _load_yaml(registry_path)
    units = []
    for unit in registry.get("units") or []:
        if not isinstance(unit, dict):
            raise ValueError("registry units must be objects")
        metrics = _metrics_for_unit(unit)
        evidence_status = unit.get("evidence_status")
        provenance = unit.get("provenance")
        contact = metrics.get("contact") or _count_pair(None, None)
        adoption = metrics.get("adoption") or _count_pair(None, None)
        recovery = metrics.get("recovery") or _count_pair(None, None)
        units.append(
            {
                "unit_id": unit["unit_id"],
                "title": unit.get("title"),
                "mechanism_layers": unit.get("mechanism_layers") or [],
                "evidence_status": evidence_status,
                "evidence_tier": _evidence_tier(evidence_status, provenance),
                "provenance": provenance,
                "source_condition": unit.get("source_condition"),
                "case_ids": unit.get("case_ids") or [],
                "episode_count": metrics["episode_count"],
                "contact_rate": metrics["contact_rate"],
                "contact": contact,
                "adoption_rate": metrics["adoption_rate"],
                "adoption": adoption,
                "recovery_rate": metrics["recovery_rate"],
                "recovery": recovery,
                "report_effect": metrics["report_effect"],
                "action_effect": metrics["action_effect"],
                "interpretation": metrics["interpretation"],
                "paper_behavioral_outcome": metrics.get("paper_behavioral_outcome"),
                "paper_report_outcome": metrics.get("paper_report_outcome"),
                "validity_limits": unit.get("validity_limits") or [],
                "artifact_refs": unit.get("source_artifacts") or {},
            }
        )

    result = {
        "schema_version": "atobench.mechanism_effect_summary.v2",
        "registry_path": str(registry_path),
        "unit_count": len(units),
        "units": units,
        "paper_claim_guardrails": [
            "Mechanism units are not identical to raw case counts.",
            "C1 behavior profiles are retrospective and should not be called preregistered.",
            "Positive-steering failures are boundary conditions, not evidence that steering is impossible.",
            "Reachability failures should be excluded from effect-size denominators.",
        ],
    }

    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    report_path = output_path.with_suffix(".md")
    result["artifacts"] = {"json": str(output_path), "markdown": str(report_path)}
    output_path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    report_path.write_text(render_mechanism_summary(result), encoding="utf-8")
    return result


def render_mechanism_summary(result: dict[str, Any]) -> str:
    lines = [
        "# ATOBench Mechanism Effect Summary",
        "",
        f"- units: `{result['unit_count']}`",
        f"- registry: `{result['registry_path']}`",
        "",
        "## Paper-Facing Overview",
        "",
        "| Unit | Mechanism | Evidence tier | Runs | Contact | Adoption | Recovery | Behavioral outcome | Report outcome | Interpretation |",
        "|---|---|---|---:|---|---|---|---|---|---|",
    ]
    for unit in result["units"]:
        lines.append(
            "| `{unit}` | {layers} | {tier} | {runs} | {contact} | {adoption} | {recovery} | {behavior} | {report} | {interp} |".format(
                unit=unit["unit_id"],
                layers=", ".join(f"`{layer}`" for layer in unit["mechanism_layers"]),
                tier=unit["evidence_tier"],
                runs=unit["episode_count"],
                contact=_fmt_metric(unit["contact"]),
                adoption=_fmt_metric(unit["adoption"]),
                recovery=_fmt_metric(unit["recovery"]),
                behavior=str(unit.get("paper_behavioral_outcome") or "n/a").replace("\n", " "),
                report=str(unit.get("paper_report_outcome") or "n/a").replace("\n", " "),
                interp=str(unit.get("interpretation") or "").replace("\n", " "),
            )
        )
    lines.extend(
        [
            "",
            "## Machine Metrics",
            "",
            "| Unit | Status | Contact rate | Adoption rate | Recovery rate |",
            "|---|---|---:|---:|---:|",
        ]
    )
    for unit in result["units"]:
        lines.append(
            "| `{unit}` | `{status}` | {contact} | {adoption} | {recovery} |".format(
                unit=unit["unit_id"],
                status=unit["evidence_status"],
                contact=_fmt(unit["contact_rate"]),
                adoption=_fmt(unit["adoption_rate"]),
                recovery=_fmt(unit["recovery_rate"]),
            )
        )
    lines.extend(["", "## Guardrails", ""])
    for item in result["paper_claim_guardrails"]:
        lines.append(f"- {item}")
    lines.append("")
    return "\n".join(lines)


__all__ = ["build_mechanism_summary", "render_mechanism_summary"]


def _main() -> None:
    import argparse

    parser = argparse.ArgumentParser(
        description="Build the ATOBench mechanism-level summary from a registry YAML file."
    )
    parser.add_argument("registry_path", help="Path to mechanism_registry.yaml")
    parser.add_argument("output_path", help="Path to write mechanism_effect_summary.json")
    args = parser.parse_args()
    build_mechanism_summary(args.registry_path, args.output_path)


if __name__ == "__main__":
    _main()
