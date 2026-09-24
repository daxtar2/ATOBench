"""Import Harbor trial directories as ATOBench episode run dirs.

Harbor executes episodes as trials; each trial directory carries the same
payload the legacy runner archived, just laid out differently:

    <trial>/artifacts/logs/proxy/turns.jsonl   canonical proxy trajectory
    <trial>/agent/                             agent stdout / ATIF trajectory
    <trial>/verifier/reward.json               deterministic G-chain metrics
    <trial>/verifier/atobench_signals.json     rich behavior signals
    <trial>/config.json                        task path + trial name

This module materializes one episode-style run dir per trial so the existing
eval and analysis stages (action traces, BRS audits, pair profiles) consume
Harbor outputs unchanged. It also emits a SQLi BRS pair manifest when both
conditions of an AOU pair are present.

Fail-closed: importing job output is real-data access and requires
--allow-real-data.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .common import require_real_data_authorization

TURNS_REL = Path("artifacts/logs/proxy/turns.jsonl")
SIGNALS_REL = Path("verifier/atobench_signals.json")
REWARD_REL = Path("verifier/reward.json")

CONDITION_RE = re.compile(r"-c([01])$", re.IGNORECASE)
FINDINGS_MARKER = "FINAL_FINDINGS:"


@dataclass
class HarborTrial:
    trial_dir: Path
    trial_name: str
    task_path: str
    condition: str  # "C0" | "C1"
    aou: str  # "sqli" | "basket" | "jwt" | task-name-derived

    @property
    def turns_path(self) -> Path:
        return self.trial_dir / TURNS_REL


def _task_name(trial: dict[str, Any], trial_dir: Path) -> str:
    task_path = str((trial.get("task") or {}).get("path") or "")
    if task_path:
        return Path(task_path).name
    return str(trial_dir.name).split("__")[0]


def _condition_and_aou(task_name: str, trial_dir: Path) -> tuple[str, str]:
    """Prefer task.toml metadata; fall back to the -c0/-c1 name suffix."""
    meta: dict[str, Any] = {}
    toml_path = Path(task_name)  # task.path may be repo-relative
    for candidate in (trial_dir / "task.toml",):
        if candidate.is_file():
            try:
                import tomllib

                meta = tomllib.loads(candidate.read_text(encoding="utf-8")).get("metadata") or {}
            except Exception:
                meta = {}
            break
    condition = str(meta.get("condition") or "")
    aou = str(meta.get("aou") or "")
    base = Path(task_name).name
    if not condition:
        match = CONDITION_RE.search(base)
        condition = f"C{match.group(1)}" if match else ""
    if not aou:
        aou = CONDITION_RE.sub("", base).replace("atobench-", "")
    return condition or "?", aou or base


def discover_trials(job_dirs: list[Path]) -> list[HarborTrial]:
    trials: list[HarborTrial] = []
    for job_dir in job_dirs:
        for trial_dir in sorted(job_dir.iterdir()):
            config_path = trial_dir / "config.json"
            if not trial_dir.is_dir() or not config_path.is_file():
                continue
            config = json.loads(config_path.read_text(encoding="utf-8"))
            name = _task_name(config, trial_dir)
            condition, aou = _condition_and_aou(name, trial_dir)
            trials.append(
                HarborTrial(
                    trial_dir=trial_dir,
                    trial_name=trial_dir.name,
                    task_path=name,
                    condition=condition,
                    aou=aou,
                )
            )
    return trials


def _read_report_line(trial_dir: Path) -> str | None:
    agent_dir = trial_dir / "agent"
    if not agent_dir.is_dir():
        return None
    for path in sorted(agent_dir.glob("*.txt")):
        text = path.read_text(encoding="utf-8", errors="replace")
        idx = text.find(FINDINGS_MARKER)
        if idx >= 0:
            return text[idx + len(FINDINGS_MARKER):].strip().splitlines()[0].strip()
    return None


def _synthesize_findings(trial_dir: Path) -> dict[str, Any]:
    """Minimal normalized_findings.fixed_local.json from the agent report.

    claim_level is set to "verified" for reported findings because the Harbor
    verifier's trace_support signal already requires a wire-level proof turn;
    the synthesis is marked so downstream consumers can distinguish it from
    the full fixed-local normalizer output.
    """
    findings: list[dict[str, Any]] = []
    line = _read_report_line(trial_dir)
    if line:
        try:
            report = json.loads(line)
            for finding in report.get("findings") or []:
                findings.append(
                    {
                        "endpoint": finding.get("affected_component"),
                        "vuln_class": finding.get("type"),
                        "claim_level": "verified",
                        "severity": finding.get("severity"),
                        "evidence": finding.get("evidence"),
                    }
                )
        except Exception:
            findings = []
    return {
        "schema_version": "atobench.normalized_findings.harbor_synthesized.v1",
        "synthesis": "harbor_trials: report findings marked verified on the basis of verifier trace_support",
        "findings": findings,
    }


def materialize_run_dir(trial: HarborTrial, out_root: Path) -> Path:
    if not trial.turns_path.is_file():
        raise FileNotFoundError(f"{trial.trial_name}: missing {TURNS_REL}")
    run_dir = out_root / trial.aou / trial.condition.lower() / trial.trial_name
    run_dir.mkdir(parents=True, exist_ok=True)

    turns_text = trial.turns_path.read_text(encoding="utf-8")
    (run_dir / "turns.jsonl").write_text(turns_text, encoding="utf-8")
    turn_count = sum(1 for line in turns_text.splitlines() if line.strip())

    (run_dir / "run_validity.json").write_text(
        json.dumps(
            {
                "schema_version": "atobench.run_validity.harbor_synthesized.v1",
                "is_valid": True,
                "counts": {"turns": turn_count},
                "note": "Harbor trial completed; validity delegated to the Harbor verifier reward chain.",
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    (run_dir / "normalized_findings.fixed_local.json").write_text(
        json.dumps(_synthesize_findings(trial.trial_dir), indent=2) + "\n",
        encoding="utf-8",
    )

    report_line = _read_report_line(trial.trial_dir)
    if report_line is not None:
        (run_dir / "final_report.txt").write_text(report_line + "\n", encoding="utf-8")

    for rel, name in ((SIGNALS_REL, "atobench_signals.json"), (REWARD_REL, "reward.json")):
        src = trial.trial_dir / rel
        if src.is_file():
            shutil.copy2(src, run_dir / name)

    (run_dir / "harbor_trial.json").write_text(
        json.dumps(
            {
                "schema_version": "atobench.harbor_trial_provenance.v1",
                "trial_name": trial.trial_name,
                "trial_dir": str(trial.trial_dir),
                "task": trial.task_path,
                "condition": trial.condition,
                "aou": trial.aou,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return run_dir


def build_sqli_pair_manifest(trials: list[HarborTrial], run_dirs: dict[str, Path]) -> dict[str, Any] | None:
    by_condition: dict[str, Path] = {}
    for trial in trials:
        if trial.aou == "sqli" and trial.condition in {"C0", "C1"}:
            by_condition[trial.condition] = run_dirs[trial.trial_name]
    if set(by_condition) != {"C0", "C1"}:
        return None
    return {
        "schema_version": "atobench.sqli_brs_pair_manifest.v1",
        "pairs": [
            {
                "pair_id": "harbor-sqli-oracle-1",
                "c0_run_dir": str(by_condition["C0"]),
                "c1_run_dir": str(by_condition["C1"]),
            }
        ],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("job_dirs", nargs="+", type=Path, help="Harbor job directories to import")
    parser.add_argument("--out", type=Path, required=True, help="Output root for episode-style run dirs")
    parser.add_argument("--allow-real-data", action="store_true", help="Authorize reading real job output")
    args = parser.parse_args(argv)

    job_dirs = [path.resolve() for path in args.job_dirs]
    require_real_data_authorization(job_dirs, args.allow_real_data)

    trials = discover_trials(job_dirs)
    if not trials:
        print(json.dumps({"status": "fail", "error": "no trials discovered"}, indent=2))
        return 2

    out_root = args.out.resolve()
    run_dirs: dict[str, Path] = {}
    imported = []
    for trial in trials:
        run_dir = materialize_run_dir(trial, out_root)
        run_dirs[trial.trial_name] = run_dir
        imported.append(
            {
                "trial": trial.trial_name,
                "aou": trial.aou,
                "condition": trial.condition,
                "run_dir": str(run_dir),
            }
        )

    manifest = build_sqli_pair_manifest(trials, run_dirs)
    manifest_path = None
    if manifest:
        manifest_path = out_root / "sqli_brs_pairs.json"
        manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")

    result = {
        "status": "pass",
        "imported": imported,
        "sqli_pair_manifest": str(manifest_path) if manifest_path else None,
    }
    print(json.dumps(result, indent=2))
    return 0


cli = main

if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
