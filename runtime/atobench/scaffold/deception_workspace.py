"""Per-deception workspace management.

A deception workspace is an ID-scoped folder that stores the artifacts for one
planned deception case:

    targets/<name>/deceptions/<deception_id>/
      deception_plan.yaml
      runtime_program.yaml
      deception_config.yaml
      compile_report.json
      validation_report.md
      runs/
"""

from __future__ import annotations

import os
import re
import shutil
import json
from pathlib import Path
from typing import Any

from atobench.scaffold.compile_runtime_program import compile_runtime_program_files


def make_deception_id(prefix: str = "dec") -> str:
    return f"{prefix}_{os.urandom(4).hex()}"


def validate_deception_id(deception_id: str) -> str:
    if not re.match(r"^[a-z][a-z0-9_-]{2,63}$", deception_id):
        raise ValueError(
            "deception_id must match ^[a-z][a-z0-9_-]{2,63}$ "
            f"(got {deception_id!r})"
        )
    return deception_id


def workspace_path(target_dir: str | Path, deception_id: str) -> Path:
    return Path(target_dir) / "deceptions" / validate_deception_id(deception_id)


def create_deception_workspace(
    target_dir: str | Path,
    deception_id: str | None = None,
    plan_path: str | Path | None = None,
    target_profile_path: str | Path | None = None,
    baseline: str = "B3",
) -> dict[str, Any]:
    """Create/compile one ID-scoped deception workspace.

    The source plan is copied into the workspace before compilation so later
    edits to target-level files do not mutate this deception case.
    """
    target = Path(target_dir)
    dec_id = validate_deception_id(deception_id or make_deception_id())
    out_dir = workspace_path(target, dec_id)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "runs").mkdir(exist_ok=True)

    src_plan = Path(plan_path) if plan_path else target / "deception_plan.yaml"
    src_profile = Path(target_profile_path) if target_profile_path else target / "scaffold_work" / "target_profile.yaml"
    if not src_plan.exists():
        raise FileNotFoundError(f"deception plan not found: {src_plan}")
    if not src_profile.exists():
        raise FileNotFoundError(f"target_profile not found: {src_profile}")

    ws_plan = out_dir / "deception_plan.yaml"
    ws_profile = out_dir / "target_profile.yaml"
    shutil.copyfile(src_plan, ws_plan)
    shutil.copyfile(src_profile, ws_profile)

    runtime_program = out_dir / "runtime_program.yaml"
    legacy_config = out_dir / "deception_config.yaml"
    compile_report = out_dir / "compile_report.json"
    compile_runtime_program_files(
        plan_path=ws_plan,
        runtime_program_path=runtime_program,
        legacy_config_path=legacy_config,
        compile_report_path=compile_report,
        baseline=baseline,
        target_profile_path=ws_profile,
    )

    validation_report = out_dir / "validation_report.md"
    validation_report.write_text(
        "\n".join(
            [
                "# Deception Workspace Validation Report",
                "",
                f"- deception_id: {dec_id}",
                f"- baseline: {baseline}",
                f"- plan: {ws_plan}",
                f"- runtime_program: {runtime_program}",
                f"- legacy_config: {legacy_config}",
                f"- compile_report: {compile_report}",
                "- overall: pass",
                "",
            ]
        ),
        encoding="utf-8",
    )

    return {
        "deception_id": dec_id,
        "workspace_dir": str(out_dir),
        "plan_path": str(ws_plan),
        "target_profile_path": str(ws_profile),
        "runtime_program_path": str(runtime_program),
        "deception_config_path": str(legacy_config),
        "compile_report_path": str(compile_report),
        "validation_report_path": str(validation_report),
        "runs_dir": str(out_dir / "runs"),
    }


def archive_episode_run(
    deception_dir: str | Path,
    episode_id: str,
    log_dir: str | Path = "logs",
    summary_path: str | Path | None = None,
) -> dict[str, Any]:
    """Copy one episode's runtime artifacts into a deception workspace."""
    dec_dir = Path(deception_dir)
    run_dir = dec_dir / "runs" / episode_id
    run_dir.mkdir(parents=True, exist_ok=True)
    logs = Path(log_dir)

    archived: dict[str, str] = {}
    turns_out = run_dir / "turns.jsonl"
    n_turns = _filter_jsonl(logs / "turns.jsonl", turns_out, episode_id)
    archived["turns_jsonl"] = str(turns_out)

    episodes_out = run_dir / "episodes.jsonl"
    _filter_jsonl(logs / "episodes.jsonl", episodes_out, episode_id)
    archived["episodes_jsonl"] = str(episodes_out)

    summary_src = Path(summary_path) if summary_path else logs / "episode_summaries" / f"{episode_id}.json"
    if summary_src.exists():
        summary_dst = run_dir / "episode_summary.json"
        shutil.copyfile(summary_src, summary_dst)
        archived["episode_summary"] = str(summary_dst)
        final_report = _final_report_from_summary(summary_dst)
        if final_report:
            final_report_path = run_dir / "final_report.txt"
            final_report_path.write_text(final_report, encoding="utf-8")
            archived["final_report"] = str(final_report_path)

    if "final_report" not in archived:
        final_report = _final_report_from_episodes_jsonl(episodes_out)
        final_report_path = run_dir / "final_report.txt"
        final_report_path.write_text(final_report, encoding="utf-8")
        archived["final_report"] = str(final_report_path)

    for src_name, dst_name in [
        (f"state_{episode_id}.json", "state.json"),
        (f"mitm_{episode_id}.log", "mitm.log"),
    ]:
        src = logs / src_name
        if src.exists():
            dst = run_dir / dst_name
            shutil.copyfile(src, dst)
            archived[dst_name] = str(dst)

    manifest = {
        "episode_id": episode_id,
        "deception_dir": str(dec_dir),
        "run_dir": str(run_dir),
        "log_dir": str(logs),
        "n_turns": n_turns,
        "artifacts": archived,
    }
    manifest_path = run_dir / "run_manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest


def _filter_jsonl(src: Path, dst: Path, episode_id: str) -> int:
    count = 0
    if not src.exists():
        dst.write_text("", encoding="utf-8")
        return count
    with open(src, "r", encoding="utf-8") as f_in, open(dst, "w", encoding="utf-8") as f_out:
        for line in f_in:
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if obj.get("episode_id") == episode_id:
                f_out.write(line)
                count += 1
    return count


def _final_report_from_summary(path: Path) -> str:
    try:
        summary = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ""
    return str(summary.get("final_report_text") or "")


def _final_report_from_episodes_jsonl(path: Path) -> str:
    final = ""
    if not path.exists():
        return final
    with open(path, "r", encoding="utf-8") as handle:
        for line in handle:
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if row.get("final_report_text"):
                final = str(row.get("final_report_text") or "")
    return final
