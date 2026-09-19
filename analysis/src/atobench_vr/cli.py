from __future__ import annotations

import importlib
import importlib.util
import json
import shutil
import sys
from pathlib import Path
from typing import Callable

from . import __version__
from .pipeline import smoke


COMMANDS = {
    "pipeline": "atobench_vr.pipeline",
    "align": "atobench_vr.alignment",
    "align-deterministic": "atobench_vr.alignment_deterministic",
    "align-verify": "atobench_vr.alignment_verifier",
    "rebuild-predicates": "atobench_vr.predicate_rebuild",
    "build-facts": "atobench_vr.facts",
    "validate-facts": "atobench_vr.fact_validation",
    "extract-claims": "atobench_vr.claims",
    "build-packets": "atobench_vr.packets",
    "run-judges": "atobench_vr.judge_runner",
    "run-judge-cohort": "atobench_vr.judge_full_cohort",
    "validate-judges": "atobench_vr.judge_validation",
    "build-episode-states": "atobench_vr.episode_states",
    "build-semantic-packets": "atobench_vr.semantic_packets",
    "run-semantic-matchers": "atobench_vr.semantic_runner",
    "validate-semantic-matches": "atobench_vr.semantic_validation",
    "build-pairs": "atobench_vr.pair_profiles",
    "freeze-statistics": "atobench_vr.statistics_freeze",
    "analyze": "atobench_vr.resilience_statistics",
    "export-results": "atobench_vr.result_facts",
    "export-learning-data": "atobench_vr.learning_data",
    "export-learning-transitions": "atobench_vr.learning_transitions",
    "export-counterfactual-trajectories": "atobench_vr.counterfactual_trajectories",
    "build-transition-audit": "atobench_vr.transition_audit",
    "platform": "atobench_vr.platform",
    "qa": "atobench_vr.final_qa",
    "import-harbor-job": "atobench_vr.harbor_trials",
}


def _project_root(start: Path) -> Path:
    current = start.resolve()
    for candidate in (current, *current.parents):
        if (candidate / "pyproject.toml").is_file() and (
            candidate / "config"
        ).is_dir():
            return candidate
    return current


def _doctor(root: Path) -> int:
    required = [
        root / "config" / "pipeline_contract.json",
        root / "config" / "report_semantic_match_contracts.json",
        root / "config" / "rubrics" / "verification_control.md",
        root / "config" / "schemas" / "learning_record.schema.json",
        root / "config" / "schemas" / "learning_transition.schema.json",
        root / "config" / "schemas" / "counterfactual_trajectory_object.schema.json",
        root / "config" / "platform" / "registry.json",
    ]
    reference_dataset = [
        root / "reference" / "latest" / "analysis_spec.lock.json",
        root
        / "reference"
        / "latest"
        / "cohort"
        / "pair_resilience_profiles.jsonl",
        root
        / "reference"
        / "latest"
        / "learning_data_v1"
        / "dataset_manifest.json",
        root
        / "reference"
        / "latest"
        / "learning_transitions_v1"
        / "dataset_manifest.json",
        root
        / "reference"
        / "latest"
        / "counterfactual_trajectory_v1"
        / "dataset_manifest.json",
        root
        / "reference"
        / "latest"
        / "transition_audit_packets_v1"
        / "selection_manifest.json",
        root
        / "reference"
        / "latest"
        / "platform"
        / "juice-shop-vr-stage20-smoke.freeze.json",
    ]
    required_checks = {str(path.relative_to(root)): path.is_file() for path in required}
    reference_checks = {
        str(path.relative_to(root)): path.is_file() for path in reference_dataset
    }
    optional = {
        "pytest": importlib.util.find_spec("pytest") is not None,
        "matplotlib": importlib.util.find_spec("matplotlib") is not None,
        "numpy": importlib.util.find_spec("numpy") is not None,
        "claude_cli": shutil.which("claude") is not None,
    }
    reference_present = all(reference_checks.values())
    note = (
        "Optional tools are needed only for their matching workflow: "
        "pytest for tests, matplotlib/numpy for figures, and claude for "
        "authorized Judge or semantic-matcher calls."
    )
    if not reference_present:
        note += (
            " The frozen reference dataset is not shipped with the release; "
            "data-dependent workflows stay fail-closed until it is regenerated "
            "from a frozen campaign."
        )
    result = {
        "schema_version": "atobench.verification_resilience.doctor.v1",
        "version": __version__,
        "project_root": str(root),
        "required": required_checks,
        "reference_dataset": reference_checks,
        "optional": optional,
        "status": "PASS" if all(required_checks.values()) else "FAIL",
        "note": note,
    }
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["status"] == "PASS" else 2


def _usage() -> str:
    commands = "\n".join(f"  {name}" for name in sorted(COMMANDS))
    return f"""ATOBench Verification Resilience {__version__}

Usage:
  atobench-vr doctor [PROJECT_ROOT]
  atobench-vr smoke
  atobench-vr <command> [command arguments]

Commands:
{commands}

Use `atobench-vr <command> --help` for command-specific arguments.
"""


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] in {"help", "-h", "--help"}:
        print(_usage())
        return 0
    command, rest = args[0], args[1:]
    if command == "doctor":
        root = _project_root(Path(rest[0]) if rest else Path.cwd())
        return _doctor(root)
    if command == "smoke":
        print(json.dumps(smoke(), indent=2, sort_keys=True))
        return 0
    module_name = COMMANDS.get(command)
    if module_name is None:
        print(f"unknown command: {command}\n\n{_usage()}", file=sys.stderr)
        return 2
    module = importlib.import_module(module_name)
    entry: Callable[[list[str] | None], int] = module.cli
    return int(entry(rest))


if __name__ == "__main__":
    raise SystemExit(main())
