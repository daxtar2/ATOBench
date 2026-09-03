"""Run-directory workflow for clean/deception pentest effect evaluation."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from atobench.eval.pentest_effect import (
    evaluate_pentest_effect_from_paths,
    normalize_report_to_schema,
)
from atobench.schema.loader import validate_normalized_findings


def evaluate_run_pair(
    *,
    clean_run_dir: str | Path,
    deception_run_dir: str | Path,
    target_profile_path: str | Path | None = None,
    plan_path: str | Path | None = None,
    runtime_program_path: str | Path | None = None,
    output_path: str | Path | None = None,
    target: str | None = None,
    normalizer_mode: str = "heuristic",
    overwrite_normalized: bool = False,
    clean_normalized_report_path: str | Path | None = None,
    deception_normalized_report_path: str | Path | None = None,
) -> dict[str, Any]:
    """Evaluate paired run directories and write artifacts into deception run.

    `normalizer_mode` currently supports:
      - `existing`: require normalized_findings.json to already exist.
      - `heuristic`: create missing normalized_findings.json deterministically.

    The LLM report-normalizer subagent is intentionally not invoked from this
    Python helper; the experiment skill invokes it explicitly for black-box
    benchmark runs. This helper validates and consumes its output.
    """

    clean_dir = Path(clean_run_dir)
    deception_dir = Path(deception_run_dir)
    if normalizer_mode not in {"existing", "heuristic"}:
        raise ValueError("normalizer_mode must be 'existing' or 'heuristic'")

    clean_turns = _require_file(clean_dir / "turns.jsonl")
    deception_turns = _require_file(deception_dir / "turns.jsonl")
    clean_report = ensure_final_report_file(clean_dir)
    deception_report = ensure_final_report_file(deception_dir)

    clean_norm = _resolve_normalized_findings(
        explicit_path=clean_normalized_report_path,
        run_dir=clean_dir,
        report_path=clean_report,
        mode=normalizer_mode,
        overwrite=overwrite_normalized,
    )
    deception_norm = _resolve_normalized_findings(
        explicit_path=deception_normalized_report_path,
        run_dir=deception_dir,
        report_path=deception_report,
        mode=normalizer_mode,
        overwrite=overwrite_normalized,
    )

    out_path = Path(output_path) if output_path else deception_dir / "pentest_effect.json"
    result = evaluate_pentest_effect_from_paths(
        clean_turns_path=clean_turns,
        deception_turns_path=deception_turns,
        clean_report_path=clean_report,
        deception_report_path=deception_report,
        target_profile_path=target_profile_path,
        plan_path=plan_path,
        runtime_program_path=runtime_program_path,
        clean_normalized_report_path=clean_norm,
        deception_normalized_report_path=deception_norm,
        target=target,
    )
    result.setdefault("artifacts", {})
    result["artifacts"].update(
        {
            "clean_run_dir": str(clean_dir),
            "deception_run_dir": str(deception_dir),
            "clean_final_report": str(clean_report),
            "deception_final_report": str(deception_report),
            "clean_normalized_findings": str(clean_norm),
            "deception_normalized_findings": str(deception_norm),
            "pentest_effect": str(out_path),
            "normalizer_mode": normalizer_mode,
        }
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    _update_pair_manifest(deception_dir, result)
    return result


def ensure_final_report_file(run_dir: str | Path) -> Path:
    """Ensure `<run_dir>/final_report.txt` exists, deriving it from summaries."""

    run = Path(run_dir)
    report = run / "final_report.txt"
    if report.exists():
        return report

    final_text = _final_report_from_summary(run / "episode_summary.json")
    if not final_text:
        final_text = _final_report_from_episodes_jsonl(run / "episodes.jsonl")
    report.write_text(final_text or "", encoding="utf-8")
    return report


def _resolve_normalized_findings(
    *,
    explicit_path: str | Path | None,
    run_dir: Path,
    report_path: Path,
    mode: str,
    overwrite: bool,
) -> Path:
    if explicit_path:
        path = _require_file(Path(explicit_path))
        validate_normalized_findings(_load_json(path))
        return path
    return ensure_normalized_findings(
        run_dir=run_dir,
        report_path=report_path,
        mode=mode,
        overwrite=overwrite,
    )


def ensure_normalized_findings(
    *,
    run_dir: str | Path,
    report_path: str | Path | None = None,
    mode: str = "heuristic",
    overwrite: bool = False,
) -> Path:
    """Ensure `<run_dir>/normalized_findings.json` exists and validates."""

    run = Path(run_dir)
    output = run / "normalized_findings.json"
    if output.exists() and not overwrite:
        validate_normalized_findings(_load_json(output))
        return output
    if mode == "existing":
        raise FileNotFoundError(f"normalized findings not found: {output}")
    if mode != "heuristic":
        raise ValueError("mode must be 'existing' or 'heuristic'")

    report = Path(report_path) if report_path else ensure_final_report_file(run)
    normalized = normalize_report_to_schema(
        report.read_text(encoding="utf-8"),
        source_report_id=run.name,
    )
    output.write_text(json.dumps(normalized, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    validate_normalized_findings(normalized)
    return output


def infer_workspace_paths(deception_run_dir: str | Path) -> dict[str, Path]:
    """Infer plan/runtime/target-profile paths from a run under deceptions/<id>/runs."""

    run = Path(deception_run_dir)
    dec_dir = run.parent.parent if run.parent.name == "runs" else None
    if not dec_dir:
        return {}
    paths = {
        "deception_dir": dec_dir,
        "target_profile_path": dec_dir / "target_profile.yaml",
        "plan_path": dec_dir / "deception_plan.yaml",
        "runtime_program_path": dec_dir / "runtime_program.yaml",
    }
    return {key: value for key, value in paths.items() if value.exists() or key == "deception_dir"}


def _final_report_from_summary(path: Path) -> str:
    if not path.exists():
        return ""
    summary = _load_json(path)
    if "final_report_text" in summary:
        return str(summary.get("final_report_text") or "")
    return ""


def _final_report_from_episodes_jsonl(path: Path) -> str:
    if not path.exists():
        return ""
    final = ""
    with open(path, "r", encoding="utf-8") as handle:
        for line in handle:
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if row.get("final_report_text"):
                final = str(row.get("final_report_text") or "")
    return final


def _update_pair_manifest(deception_run_dir: Path, result: dict[str, Any]) -> None:
    manifest_path = deception_run_dir / "evaluation_manifest.json"
    manifest = {
        "schema_version": "0.1.0",
        "deception_run_dir": str(deception_run_dir),
        "pentest_effect": result.get("artifacts", {}).get("pentest_effect"),
        "contact_rate": result.get("opportunity", {}).get("contact_rate"),
        "verified_fabrication_rate": result.get("outcome", {}).get("verified_fabrication_rate"),
        "clean_relative_recall_drop": result.get("outcome", {}).get("clean_relative_recall_drop"),
        "report_sources": {
            "clean": result.get("inputs", {}).get("clean_report_source"),
            "deception": result.get("inputs", {}).get("deception_report_source"),
        },
        "artifacts": result.get("artifacts", {}),
    }
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _require_file(path: Path) -> Path:
    if not path.exists():
        raise FileNotFoundError(f"required file not found: {path}")
    return path


def _load_json(path: Path) -> dict[str, Any]:
    with open(path, "r", encoding="utf-8") as handle:
        value = json.load(handle)
    return value if isinstance(value, dict) else {}
