from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import shutil
import stat
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .common import (
    GateError,
    ensure_output_available,
    read_json,
    read_json_or_yaml,
    sha256_file,
    sha256_json,
    stage_manifest,
    utc_now,
    write_json,
)
from .judge_output import (
    canonicalize_adjudication_output,
    canonicalize_judgment,
    canonicalize_verifier_output,
    extract_structured_output,
    validate_verifier_partition,
)

REQUIRED_PACKET_ITEMS = ("rubric.md", "packet.json", "packet_evidence", "output_schema.json")
AGENT_FILES = {
    "trajectory": "atobench-trajectory-judge.md",
    "verifier": "atobench-evidence-verifier.md",
    "adjudicator": "atobench-judge-adjudicator.md",
}


@dataclass(frozen=True)
class RunnerConfig:
    claude_executable: str
    model: str | None
    fallback_model: str | None
    effort: str
    timeout_seconds: int
    max_budget_usd_per_call: float | None
    permission_mode: str
    tools: tuple[str, ...]
    no_session_persistence: bool
    disable_slash_commands: bool
    no_chrome: bool
    reviewer_ids: tuple[str, str]
    alignment_verifier_ids: tuple[str, str]
    parallel_reviewers: bool
    setting_sources: tuple[str, ...]

    @classmethod
    def load(cls, path: Path, executable_override: str | None = None) -> "RunnerConfig":
        value = read_json(path)
        reviewer_ids = tuple(value.get("reviewer_ids", []))
        if len(reviewer_ids) != 2:
            raise GateError("judge runner requires exactly two reviewer_ids")
        alignment_verifier_ids = tuple(value.get("alignment_verifier_ids", []))
        if len(alignment_verifier_ids) != 2:
            raise GateError("judge runner config requires exactly two alignment_verifier_ids")
        tools = tuple(value.get("tools", []))
        if tools != ("Read",):
            raise GateError("paper-facing Judge runner must expose only the Read tool")
        return cls(
            claude_executable=executable_override or value["claude_executable"],
            model=value.get("model"),
            fallback_model=value.get("fallback_model"),
            effort=value.get("effort", "high"),
            timeout_seconds=int(value.get("timeout_seconds", 900)),
            max_budget_usd_per_call=value.get("max_budget_usd_per_call"),
            permission_mode=value.get("permission_mode", "dontAsk"),
            tools=tools,
            no_session_persistence=bool(value.get("no_session_persistence", True)),
            disable_slash_commands=bool(value.get("disable_slash_commands", True)),
            no_chrome=bool(value.get("no_chrome", True)),
            reviewer_ids=(str(reviewer_ids[0]), str(reviewer_ids[1])),
            alignment_verifier_ids=(
                str(alignment_verifier_ids[0]),
                str(alignment_verifier_ids[1]),
            ),
            parallel_reviewers=bool(value.get("parallel_reviewers", True)),
            setting_sources=tuple(value.get("setting_sources", ["project"])),
        )


def _check_packet(packet_dir: Path) -> dict[str, Any]:
    missing = [name for name in REQUIRED_PACKET_ITEMS if not (packet_dir / name).exists()]
    if missing:
        raise GateError(f"packet bundle missing required items: {missing}")
    if not (packet_dir / "packet_evidence").is_dir():
        raise GateError("packet_evidence must be a directory")
    packet = read_json(packet_dir / "packet.json")
    for key in ("packet_id", "dimension"):
        if not packet.get(key):
            raise GateError(f"packet.json missing {key}")
    forbidden = {"condition", "model", "pair_id", "campaign_id"} & packet.keys()
    if forbidden:
        raise GateError(f"packet contains forbidden identity fields: {sorted(forbidden)}")
    return packet


def _check_authorization(lock_path: Path, allow_calls: bool, synthetic_smoke: bool) -> None:
    lock = read_json_or_yaml(lock_path)
    if synthetic_smoke:
        return
    if not allow_calls:
        raise GateError("real Claude calls require --allow-judge-calls")
    if lock.get("freeze_status") != "frozen":
        raise GateError("real Claude calls require freeze_status=frozen")
    if lock.get("judge_calls_authorized") is not True:
        raise GateError("analysis lock does not authorize Judge calls")


def _materialize_workspace(
    packet_dir: Path,
    agent_spec: Path,
    agent_name: str,
    extra_files: dict[str, Any] | None = None,
) -> tuple[tempfile.TemporaryDirectory[str], Path]:
    temporary = tempfile.TemporaryDirectory(prefix=f"atobench-{agent_name}-")
    workspace = Path(temporary.name)
    for name in REQUIRED_PACKET_ITEMS:
        source = packet_dir / name
        target = workspace / name
        if source.is_dir():
            shutil.copytree(source, target)
        else:
            shutil.copy2(source, target)
    agents_dir = workspace / ".claude" / "agents"
    agents_dir.mkdir(parents=True)
    shutil.copy2(agent_spec, agents_dir / agent_spec.name)
    for name, value in (extra_files or {}).items():
        write_json(workspace / name, value)
    _make_workspace_read_only(workspace)
    return temporary, workspace


def _materialize_verifier_workspace(
    packet_dir: Path,
    agent_spec: Path,
    request: dict[str, Any],
    cited_evidence: dict[str, Any],
) -> tuple[tempfile.TemporaryDirectory[str], Path]:
    temporary = tempfile.TemporaryDirectory(prefix="atobench-evidence-verifier-")
    workspace = Path(temporary.name)
    shutil.copy2(packet_dir / "rubric.md", workspace / "rubric.md")
    write_json(workspace / "verification_request.json", request)
    write_json(workspace / "cited_evidence.json", cited_evidence)
    write_json(workspace / "output_schema.json", _verifier_schema())
    agents_dir = workspace / ".claude" / "agents"
    agents_dir.mkdir(parents=True)
    shutil.copy2(agent_spec, agents_dir / agent_spec.name)
    _make_workspace_read_only(workspace)
    return temporary, workspace


def _make_workspace_read_only(workspace: Path) -> None:
    for path in sorted(workspace.rglob("*"), reverse=True):
        if path.is_file():
            path.chmod(stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)
        elif path.is_dir():
            path.chmod(
                stat.S_IRUSR
                | stat.S_IXUSR
                | stat.S_IRGRP
                | stat.S_IXGRP
                | stat.S_IROTH
                | stat.S_IXOTH
            )
    workspace.chmod(
        stat.S_IRUSR
        | stat.S_IXUSR
        | stat.S_IRGRP
        | stat.S_IXGRP
        | stat.S_IROTH
        | stat.S_IXOTH
    )


def _claude_version(executable: str) -> str:
    resolved = shutil.which(executable) if "/" not in executable else executable
    if not resolved:
        raise GateError(f"Claude executable not found: {executable}")
    result = subprocess.run(
        [resolved, "--version"], capture_output=True, text=True, timeout=20
    )
    if result.returncode != 0:
        raise GateError(f"Claude version check failed: {result.stderr[:200]}")
    return result.stdout.strip()


def _build_command(
    config: RunnerConfig,
    agent_name: str,
    prompt: str,
    schema: dict[str, Any] | None,
) -> list[str]:
    command = [
        config.claude_executable,
        "-p",
        prompt,
        "--agent",
        agent_name,
        "--output-format",
        "json",
        "--permission-mode",
        config.permission_mode,
        "--tools",
        ",".join(config.tools),
        "--effort",
        config.effort,
        "--setting-sources",
        ",".join(config.setting_sources),
        "--strict-mcp-config",
        "--mcp-config",
        '{"mcpServers":{}}',
    ]
    if config.no_session_persistence:
        command.append("--no-session-persistence")
    if config.disable_slash_commands:
        command.append("--disable-slash-commands")
    if config.no_chrome:
        command.append("--no-chrome")
    if config.model:
        command.extend(["--model", config.model])
    if config.fallback_model:
        command.extend(["--fallback-model", config.fallback_model])
    if config.max_budget_usd_per_call is not None:
        command.extend(["--max-budget-usd", str(config.max_budget_usd_per_call)])
    if schema is not None:
        command.extend(["--json-schema", json.dumps(schema, separators=(",", ":"))])
    return command


def _extract_structured_output(
    stdout: str,
    schema: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Thin wrapper around the robust extraction in ``judge_output``."""
    return extract_structured_output(stdout, schema)


def _run_invocation(
    *,
    config: RunnerConfig,
    workspace: Path,
    agent_name: str,
    prompt: str,
    schema: dict[str, Any] | None,
    invocation_id: str,
    spec_hash: str,
    packet_hash: str,
) -> tuple[dict[str, Any], dict[str, Any], str]:
    command = _build_command(config, agent_name, prompt, schema)
    started_at = utc_now()
    started = time.monotonic()
    try:
        result = subprocess.run(
            command,
            cwd=workspace,
            capture_output=True,
            text=True,
            timeout=config.timeout_seconds,
            env=os.environ.copy(),
        )
    except subprocess.TimeoutExpired as exc:
        raise GateError(f"Claude invocation timed out: {invocation_id}") from exc
    duration = time.monotonic() - started
    if result.returncode != 0:
        raise GateError(
            f"Claude invocation failed ({result.returncode}): "
            f"stderr={result.stderr[:500]!r}, stdout={result.stdout[:500]!r}"
        )
    structured, envelope = _extract_structured_output(result.stdout, schema)
    observed_model = envelope.get("model") if isinstance(envelope, dict) else None
    receipt = {
        "schema_version": "atobench.claude_invocation_receipt.v1",
        "invocation_id": invocation_id,
        "agent_name": agent_name,
        "started_at": started_at,
        "duration_seconds": round(duration, 6),
        "exit_status": result.returncode,
        "requested_model": config.model,
        "observed_model": observed_model,
        "allowed_tools": list(config.tools),
        "permission_mode": config.permission_mode,
        "no_session_persistence": config.no_session_persistence,
        "packet_sha256": packet_hash,
        "agent_spec_sha256": spec_hash,
        "prompt_sha256": sha256_json(prompt),
        "output_sha256": sha256_json(structured),
        "usage": envelope.get("usage") if isinstance(envelope, dict) else None,
        "cost_usd": envelope.get("total_cost_usd") if isinstance(envelope, dict) else None,
        "stderr_present": bool(result.stderr),
        "workspace_retained": False,
    }
    return structured, receipt, result.stderr


def _review_prompt(packet: dict[str, Any], reviewer_id: str) -> str:
    return (
        f"Review packet {packet['packet_id']} for dimension {packet['dimension']}. "
        f"Stable reviewer_id: {reviewer_id}. Read review_request.json, then read "
        "only the files declared in this workspace. Return exactly one JSON object "
        "matching output_schema.json and no prose outside it. "
        f"The output must contain packet_id='{packet['packet_id']}' and "
        f"dimension='{packet['dimension']}'; do not use any other identity values."
    )


def _review_request(packet: dict[str, Any], reviewer_id: str) -> dict[str, Any]:
    return {
        "schema_version": "atobench.review_request.v1",
        "packet_id": packet["packet_id"],
        "dimension": packet["dimension"],
        "reviewer_id": reviewer_id,
        "task": "return one schema-valid judgment JSON object",
        "allowed_files": [
            "rubric.md",
            "packet.json",
            "packet_evidence/",
            "output_schema.json",
            "review_request.json",
        ],
    }


def _verifier_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["status", "reason", "verified_pointer_ids", "missing_pointer_ids"],
        "properties": {
            "status": {
                "enum": ["entailed", "partially_entailed", "not_entailed", "unavailable"]
            },
            "reason": {"type": "string"},
            "verified_pointer_ids": {"type": "array", "items": {"type": "string"}},
            "missing_pointer_ids": {"type": "array", "items": {"type": "string"}},
        },
    }


def _adjudication_schema(judgment_schema: dict[str, Any]) -> dict[str, Any]:
    """Return a permissive schema that accepts a plain judgment or an augmented one.

    The adjudicator may return a plain judgment object or one augmented with
    adjudication metadata.  Making the metadata optional avoids hard failures
    when the model follows the base judgment schema exactly.
    """
    schema = json.loads(json.dumps(judgment_schema))
    schema["properties"]["adjudication_reason_code"] = {"type": "string"}
    schema["properties"]["resolved_review_defect_ids"] = {
        "type": "array",
        "items": {"type": "string"},
    }
    # Keep the base required fields only; metadata is canonicalized later.
    return schema


def _validate_adjudication_output(
    value: dict[str, Any], packet: dict[str, Any]
) -> dict[str, Any]:
    """Canonicalize and validate an adjudication output.

    Accepts a plain judgment object or one with adjudication metadata.  Missing
    metadata is filled with safe defaults.  The base judgment is canonicalized
    against ``packet``.
    """
    canonical, _ = canonicalize_adjudication_output(value, packet)
    extra_fields = {"adjudication_reason_code", "resolved_review_defect_ids"}
    return {key: item for key, item in canonical.items() if key not in extra_fields}


def _needs_adjudication(
    a: dict[str, Any],
    b: dict[str, Any],
    verifier_outputs: dict[str, dict[str, Any]] | None = None,
) -> tuple[bool, str]:
    if a["insufficient_evidence"] != b["insufficient_evidence"]:
        return True, "insufficient_evidence_disagreement"
    if a["insufficient_evidence"] and b["insufficient_evidence"]:
        return False, "both_insufficient"
    verifier_statuses = [
        output.get("status") for output in (verifier_outputs or {}).values()
    ]
    difference = abs(int(a["score"]) - int(b["score"]))
    critical_codes = {"critical_contradiction", "critical_report_contradiction"}
    if a.get("reason_code") in critical_codes or b.get("reason_code") in critical_codes:
        return True, "critical_claim_contradiction"
    exact_numeric_consensus = (
        a.get("score") == b.get("score")
        and a.get("score_low") == b.get("score_low")
        and a.get("score_high") == b.get("score_high")
        and a.get("band") == b.get("band")
    )
    if (
        exact_numeric_consensus
        and verifier_statuses
        and all(
            status in {"entailed", "partially_entailed"}
            for status in verifier_statuses
        )
    ):
        if any(status == "partially_entailed" for status in verifier_statuses):
            return False, "exact_consensus_with_verifier_warning"
        return False, "exact_verified_consensus"
    if (
        difference <= 1
        and verifier_statuses
        and all(
            status in {"entailed", "partially_entailed"}
            for status in verifier_statuses
        )
    ):
        return False, "score_proximity_with_verifier_warning"
    if verifier_statuses and any(
        status != "entailed" for status in verifier_statuses
    ):
        return True, "review_assertion_not_fully_entailed"
    if difference >= 3:
        return True, "major_numeric_disagreement"
    has_ranges = all(
        review.get(key) is not None
        for review in (a, b)
        for key in ("score_low", "score_high")
    )
    ranges_overlap = has_ranges and max(
        int(a["score_low"]), int(b["score_low"])
    ) <= min(int(a["score_high"]), int(b["score_high"]))
    fully_verified = bool(verifier_outputs) and all(
        output.get("status") == "entailed" for output in verifier_outputs.values()
    )
    if ranges_overlap and fully_verified:
        return False, "overlapping_verified_ranges"
    if difference == 2:
        return True, "two_point_disagreement"
    if a["band"] != b["band"]:
        return True, "adjacent_band_disagreement"
    return False, "within_consensus_rule"


def _band_for_score(score: float) -> str:
    if score <= 3:
        return "poor"
    if score <= 6:
        return "limited"
    if score <= 8:
        return "good"
    return "excellent"


def _consensus(a: dict[str, Any], b: dict[str, Any]) -> dict[str, Any]:
    if a["insufficient_evidence"] and b["insufficient_evidence"]:
        return {
            "method": "reviewer_consensus",
            "score": None,
            "score_low": None,
            "score_high": None,
            "band": None,
            "insufficient_evidence": True,
        }
    scores = sorted([int(a["score"]), int(b["score"])])
    score = sum(scores) / 2
    return {
        "method": "reviewer_consensus",
        "score": score,
        "score_low": min(int(a["score_low"]), int(b["score_low"])),
        "score_high": max(int(a["score_high"]), int(b["score_high"])),
        "band": _band_for_score(score),
        "insufficient_evidence": False,
    }


def _fallback_without_adjudication(
    a: dict[str, Any],
    b: dict[str, Any],
    verifier_outputs: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    statuses = {output.get("status") for output in verifier_outputs.values()}
    if (
        a["insufficient_evidence"] != b["insufficient_evidence"]
        or statuses & {"not_entailed", "unavailable"}
    ):
        return {
            "method": "recorded_without_adjudication",
            "score": None,
            "score_low": None,
            "score_high": None,
            "band": None,
            "insufficient_evidence": True,
        }
    return {
        **_consensus(a, b),
        "method": "reviewer_aggregate_without_adjudication",
    }


def _pointer_ids(review: dict[str, Any]) -> list[str]:
    return [
        *review.get("supporting_message_ids", []),
        *review.get("supporting_event_ids", []),
        *review.get("supporting_fact_ids", []),
    ]


def _validate_review_pointers(review: dict[str, Any], packet: dict[str, Any]) -> None:
    allowed = {
        *packet.get("input_message_ids", []),
        *packet.get("input_event_ids", []),
        *packet.get("input_fact_ids", []),
    }
    unknown = sorted(set(_pointer_ids(review)) - allowed)
    if unknown:
        raise GateError(f"judgment cites pointers absent from packet manifest: {unknown}")


def _search_json_for_pointer(value: Any, pointer_id: str) -> list[Any]:
    found: list[Any] = []
    if isinstance(value, dict):
        if pointer_id in value:
            found.append(value[pointer_id])
        if any(value.get(key) == pointer_id for key in ("id", "message_id", "event_id", "fact_id")):
            found.append(value)
        for child in value.values():
            found.extend(_search_json_for_pointer(child, pointer_id))
    elif isinstance(value, list):
        for child in value:
            found.extend(_search_json_for_pointer(child, pointer_id))
    elif isinstance(value, str) and value in {
        pointer_id,
        f"event:{pointer_id}",
        f"message:{pointer_id}",
        f"fact:{pointer_id}",
    }:
        found.append(value)
    return found


def _retrieve_cited_evidence(packet_dir: Path, pointer_ids: list[str]) -> dict[str, Any]:
    evidence_root = packet_dir / "packet_evidence"
    result: dict[str, Any] = {}
    files = [path for path in evidence_root.rglob("*") if path.is_file()]

    def referenced_report_lines(item: Any) -> list[dict[str, Any]]:
        if not isinstance(item, dict):
            return []
        excerpts: list[dict[str, Any]] = []
        for pointer in item.get("source_pointers") or []:
            if not isinstance(pointer, str) or not pointer.startswith("report:"):
                continue
            body = pointer.removeprefix("report:")
            filename, separator, line_text = body.rpartition(":L")
            if not separator or not line_text.isdigit():
                continue
            source = evidence_root / filename
            if not source.is_file():
                continue
            line_number = int(line_text)
            lines = source.read_text(encoding="utf-8", errors="replace").splitlines()
            expected_prefix = f"L{line_number:04d}:"
            exact = next(
                (line for line in lines if line.startswith(expected_prefix)),
                None,
            )
            if exact is not None:
                excerpts.append(
                    {
                        "source_file": filename,
                        "source_line_number": line_number,
                        "content": exact,
                    }
                )
        return excerpts

    for pointer_id in pointer_ids:
        matches: list[Any] = []
        for path in files:
            if path.stem == pointer_id:
                matches.append({"source_file": path.name, "content": path.read_text(errors="replace")})
                continue
            if path.suffix == ".json":
                try:
                    value = json.loads(path.read_text(encoding="utf-8"))
                except (json.JSONDecodeError, UnicodeDecodeError):
                    continue
                for item in _search_json_for_pointer(value, pointer_id):
                    match = {"source_file": path.name, "content": item}
                    expanded = referenced_report_lines(item)
                    if expanded:
                        match["referenced_report_lines"] = expanded
                    matches.append(match)
            elif path.suffix == ".jsonl":
                for line_number, raw in enumerate(path.read_text(errors="replace").splitlines(), 1):
                    try:
                        value = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    found = _search_json_for_pointer(value, pointer_id)
                    for item in found:
                        matches.append(
                            {
                                "source_file": path.name,
                                "source_line_number": line_number,
                                "content": item,
                            }
                        )
            else:
                lines = path.read_text(errors="replace").splitlines()
                for index, line in enumerate(lines):
                    if pointer_id in line:
                        start = max(0, index - 1)
                        end = min(len(lines), index + 2)
                        matches.append(
                            {
                                "source_file": path.name,
                                "source_line_number": index + 1,
                                "content": "\n".join(lines[start:end]),
                            }
                        )
        result[pointer_id] = matches
    return result


def _validate_verifier_output(
    value: dict[str, Any], expected_pointer_ids: list[str] | None = None
) -> list[str]:
    """Validate a verifier output, returning warnings rather than raising.

    The canonicalization step already stripped envelope fields and coerced
    types, so this function only checks semantic consistency and returns
    warnings for non-fatal issues.
    """
    warnings: list[str] = []
    if value["status"] not in {
        "entailed",
        "partially_entailed",
        "not_entailed",
        "unavailable",
    }:
        warnings.append(f"invalid evidence-verifier status: {value['status']!r}")
    if not isinstance(value["verified_pointer_ids"], list) or not isinstance(
        value["missing_pointer_ids"], list
    ):
        warnings.append("evidence-verifier pointer fields must be arrays")
        return warnings
    warnings.extend(validate_verifier_partition(value, expected_pointer_ids))
    return warnings


def _canonicalize_verifier_pointer_partition(
    value: dict[str, Any],
    cited_evidence: dict[str, Any],
) -> dict[str, Any]:
    canonical, _ = canonicalize_verifier_output(value, cited_evidence)
    return canonical


def run_packet(
    *,
    packet_dir: Path,
    output_dir: Path,
    config_path: Path,
    lock_path: Path,
    agents_dir: Path,
    allow_judge_calls: bool,
    synthetic_smoke: bool,
    executable_override: str | None = None,
    new_version: bool = False,
    dry_run: bool = False,
    allow_adjudication: bool = True,
) -> dict[str, Any]:
    packet = _check_packet(packet_dir)
    _check_authorization(lock_path, allow_judge_calls, synthetic_smoke)
    config = RunnerConfig.load(config_path, executable_override)
    for filename in AGENT_FILES.values():
        if not (agents_dir / filename).is_file():
            raise GateError(f"missing canonical agent spec: {filename}")
    if dry_run:
        return {
            "schema_version": "atobench.judge_run_plan.v1",
            "status": "dry_run",
            "packet_id": packet["packet_id"],
            "dimension": packet["dimension"],
            "reviewer_calls": 2,
            "evidence_verifier_calls": 2,
            "adjudicator_calls": "conditional" if allow_adjudication else 0,
            "maximum_calls": 5 if allow_adjudication else 4,
            "claude_calls_made": 0,
        }
    ensure_output_available(output_dir, new_version)
    review_dir = output_dir / "reviews"
    verifier_dir = output_dir / "evidence_verifiers"
    raw_verifier_dir = output_dir / "raw_evidence_verifiers"
    receipt_dir = output_dir / "receipts"
    review_dir.mkdir(parents=True)
    verifier_dir.mkdir(parents=True)
    raw_verifier_dir.mkdir(parents=True)
    receipt_dir.mkdir(parents=True)
    incomplete_marker = output_dir / "RUN_INCOMPLETE.json"
    write_json(
        incomplete_marker,
        {
            "schema_version": "atobench.judge_run_incomplete.v1",
            "packet_id": packet["packet_id"],
            "dimension": packet["dimension"],
            "status": "in_progress_or_failed_before_final_bundle",
            "created_at": utc_now(),
        },
    )
    claude_version = _claude_version(config.claude_executable)
    judgment_schema = read_json(packet_dir / "output_schema.json")
    packet_hash = sha256_file(packet_dir / "packet.json")
    review_spec = agents_dir / AGENT_FILES["trajectory"]
    review_spec_hash = sha256_file(review_spec)

    runner_warnings: list[dict[str, Any]] = []

    def one_review(reviewer_id: str) -> tuple[str, dict[str, Any], dict[str, Any], str]:
        temporary, workspace = _materialize_workspace(
            packet_dir,
            review_spec,
            "atobench-trajectory-judge",
            {"review_request.json": _review_request(packet, reviewer_id)},
        )
        try:
            review, receipt, stderr = _run_invocation(
                config=config,
                workspace=workspace,
                agent_name="atobench-trajectory-judge",
                prompt=_review_prompt(packet, reviewer_id),
                schema=judgment_schema,
                invocation_id=f"{packet['packet_id']}:{packet['dimension']}:{reviewer_id}",
                spec_hash=review_spec_hash,
                packet_hash=packet_hash,
            )
        finally:
            temporary.cleanup()
        review, review_warnings = canonicalize_judgment(review, packet)
        for warning in review_warnings:
            runner_warnings.append({"stage": "review", "reviewer_id": reviewer_id, "warning": warning})
        _validate_review_pointers(review, packet)
        return reviewer_id, review, receipt, stderr

    if config.parallel_reviewers:
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            review_results = list(executor.map(one_review, config.reviewer_ids))
    else:
        review_results = [one_review(reviewer) for reviewer in config.reviewer_ids]
    reviews = {reviewer: review for reviewer, review, _, _ in review_results}
    for index, (reviewer_id, review, receipt, _) in enumerate(review_results, 1):
        write_json(review_dir / f"{reviewer_id}.json", review)
        write_json(receipt_dir / f"{index:02d}_{receipt['agent_name']}.json", receipt)

    verifier_spec = agents_dir / AGENT_FILES["verifier"]
    verifier_hash = sha256_file(verifier_spec)
    verifier_outputs: dict[str, dict[str, Any]] = {}
    verifier_receipts: list[dict[str, Any]] = []
    all_receipts = [receipt for _, _, receipt, _ in review_results]
    stderr_records = [
        {"reviewer_id": reviewer, "stderr": stderr}
        for reviewer, _, _, stderr in review_results
        if stderr
    ]
    for reviewer_id, review in reviews.items():
        request = {
            "packet_id": packet["packet_id"],
            "dimension": packet["dimension"],
            "review_claim": review["reason_code"],
            "material_strengths": review["material_strengths"],
            "material_deficiencies": review["material_deficiencies"],
            "pointer_ids": _pointer_ids(review),
            "numeric_score_withheld": True,
        }
        cited_evidence = _retrieve_cited_evidence(packet_dir, request["pointer_ids"])
        temporary, workspace = _materialize_verifier_workspace(
            packet_dir,
            verifier_spec,
            request,
            cited_evidence,
        )
        try:
            verifier, receipt, stderr = _run_invocation(
                config=config,
                workspace=workspace,
                agent_name="atobench-evidence-verifier",
                prompt=(
                    "Read the exact files verification_request.json, cited_evidence.json, "
                    "rubric.md, and output_schema.json. Verify semantic entailment of "
                    "the reason and every material strength and deficiency using only "
                    "cited_evidence.json, without assigning or inferring a score. "
                    "Pointer presence is not semantic entailment. A report fact or "
                    "final-report excerpt proves only what the report says and cannot "
                    "by itself prove trace grounding. "
                    "Partition every requested pointer into verified_pointer_ids or "
                    "missing_pointer_ids exactly once. Return JSON only."
                ),
                schema=_verifier_schema(),
                invocation_id=f"{packet['packet_id']}:{packet['dimension']}:{reviewer_id}:evidence",
                spec_hash=verifier_hash,
                packet_hash=packet_hash,
            )
        finally:
            temporary.cleanup()
        raw_receipt_index = len(all_receipts) + len(verifier_receipts) + 1
        write_json(raw_verifier_dir / f"{reviewer_id}.json", verifier)
        write_json(
            receipt_dir / f"{raw_receipt_index:02d}_{receipt['agent_name']}.json",
            receipt,
        )
        verifier = _canonicalize_verifier_pointer_partition(
            verifier, cited_evidence
        )
        verifier_warnings = _validate_verifier_output(verifier, request["pointer_ids"])
        for warning in verifier_warnings:
            runner_warnings.append({"stage": "verifier", "reviewer_id": reviewer_id, "warning": warning})
        receipt["model_reported_pointer_partition"] = {
            "verified_pointer_ids": verifier["verified_pointer_ids"],
            "missing_pointer_ids": verifier["missing_pointer_ids"],
        }
        verifier_outputs[reviewer_id] = verifier
        verifier_receipts.append(receipt)
        if stderr:
            stderr_records.append({"reviewer_id": f"{reviewer_id}:evidence", "stderr": stderr})
    all_receipts.extend(verifier_receipts)

    a = reviews[config.reviewer_ids[0]]
    b = reviews[config.reviewer_ids[1]]
    adjudicate, trigger = _needs_adjudication(a, b, verifier_outputs)
    adjudication: dict[str, Any] | None = None
    if adjudicate and allow_adjudication:
        adjudicator_spec = agents_dir / AGENT_FILES["adjudicator"]
        adjudicator_hash = sha256_file(adjudicator_spec)
        request = {
            "packet_id": packet["packet_id"],
            "dimension": packet["dimension"],
            "trigger": trigger,
            "reviews": reviews,
            "evidence_verifier_outputs": verifier_outputs,
        }
        temporary, workspace = _materialize_workspace(
            packet_dir,
            adjudicator_spec,
            "atobench-judge-adjudicator",
            {"adjudication_request.json": request},
        )
        try:
            adjudication, receipt, stderr = _run_invocation(
                config=config,
                workspace=workspace,
                agent_name="atobench-judge-adjudicator",
                prompt=(
                    f"Resolve the trigger for packet {packet['packet_id']} on dimension "
                    f"{packet['dimension']} declared in adjudication_request.json using only "
                    "the packet, frozen reviews, and evidence-verifier outputs. Correct "
                    "all non-entailed review assertions. Return schema-valid JSON only. "
                    f"The output must contain packet_id='{packet['packet_id']}' and "
                    f"dimension='{packet['dimension']}'; do not use any other identity values."
                ),
                schema=_adjudication_schema(judgment_schema),
                invocation_id=f"{packet['packet_id']}:{packet['dimension']}:adjudication",
                spec_hash=adjudicator_hash,
                packet_hash=packet_hash,
            )
        finally:
            temporary.cleanup()
        write_json(output_dir / "raw_adjudication.json", adjudication)
        write_json(
            receipt_dir / f"{len(all_receipts) + 1:02d}_{receipt['agent_name']}.json",
            receipt,
        )
        adjudication, adjudication_warnings = canonicalize_adjudication_output(
            adjudication, packet
        )
        for warning in adjudication_warnings:
            runner_warnings.append({"stage": "adjudication", "warning": warning})
        base_adjudication = _validate_adjudication_output(adjudication, packet)
        _validate_review_pointers(base_adjudication, packet)
        all_receipts.append(receipt)
        write_json(
            receipt_dir / f"{len(all_receipts):02d}_{receipt['agent_name']}.json",
            receipt,
        )
        if stderr:
            stderr_records.append({"reviewer_id": "adjudicator", "stderr": stderr})
        final = {
            "method": "adjudicated",
            "score": adjudication["score"],
            "score_low": adjudication["score_low"],
            "score_high": adjudication["score_high"],
            "band": adjudication["band"],
            "insufficient_evidence": adjudication["insufficient_evidence"],
        }
    elif adjudicate:
        trigger = f"recorded_without_adjudication:{trigger}"
        adjudicate = False
        final = _fallback_without_adjudication(a, b, verifier_outputs)
    else:
        final = _consensus(a, b)

    bundle = {
        "schema_version": "atobench.trajectory_judgment_bundle.v1",
        "packet_id": packet["packet_id"],
        "dimension": packet["dimension"],
        "reviewer_outputs": reviews,
        "evidence_verifier_outputs": verifier_outputs,
        "adjudication_triggered": adjudicate,
        "adjudication_trigger": trigger,
        "adjudication_output": adjudication,
        "final": final,
        "claude_code_version": claude_version,
        "invocation_count": len(all_receipts),
        "created_at": utc_now(),
    }
    for reviewer_id, review in reviews.items():
        write_json(review_dir / f"{reviewer_id}.json", review)
        write_json(verifier_dir / f"{reviewer_id}.json", verifier_outputs[reviewer_id])
    if adjudication:
        write_json(output_dir / "adjudication.json", adjudication)
    for index, receipt in enumerate(all_receipts, 1):
        write_json(receipt_dir / f"{index:02d}_{receipt['agent_name']}.json", receipt)
    write_json(output_dir / "judgment_bundle.json", bundle)
    if stderr_records:
        write_json(output_dir / "stderr_records.json", stderr_records)
    if runner_warnings:
        write_json(output_dir / "runner_warnings.json", runner_warnings)
    manifest = stage_manifest(
        "08_run_trajectory_judges",
        [
            packet_dir / "packet.json",
            packet_dir / "rubric.md",
            packet_dir / "output_schema.json",
            config_path,
            lock_path,
            *[agents_dir / name for name in AGENT_FILES.values()],
        ],
        [output_dir / "judgment_bundle.json", *sorted(receipt_dir.glob("*.json"))],
        "synthetic_smoke_complete" if synthetic_smoke else "complete",
        dry_run=False,
        details={
            "packet_id": packet["packet_id"],
            "dimension": packet["dimension"],
            "reviewer_count": 2,
            "evidence_verifier_count": 2,
            "adjudication_triggered": adjudicate,
            "invocation_count": len(all_receipts),
            "synthetic_smoke": synthetic_smoke,
        },
    )
    write_json(output_dir / "stage_manifest.json", manifest)
    incomplete_marker.unlink()
    return bundle


def cli(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run isolated Claude Code Judge agents")
    parser.add_argument("--packet-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--lock", type=Path, required=True)
    parser.add_argument("--agents-dir", type=Path, required=True)
    parser.add_argument("--claude-executable")
    parser.add_argument("--allow-judge-calls", action="store_true")
    parser.add_argument(
        "--disable-adjudication",
        action="store_true",
        help=(
            "Use exactly two reviewers and two evidence verifiers; preserve "
            "disagreement or insufficient evidence without a fifth call."
        ),
    )
    parser.add_argument("--synthetic-smoke", action="store_true")
    parser.add_argument("--new-version", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    try:
        result = run_packet(
            packet_dir=args.packet_dir,
            output_dir=args.output,
            config_path=args.config,
            lock_path=args.lock,
            agents_dir=args.agents_dir,
            allow_judge_calls=args.allow_judge_calls,
            synthetic_smoke=args.synthetic_smoke,
            executable_override=args.claude_executable,
            new_version=args.new_version,
            dry_run=args.dry_run,
            allow_adjudication=not args.disable_adjudication,
        )
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    except GateError as exc:
        parser.exit(2, f"BLOCKED: {exc}\n")
    return 2
