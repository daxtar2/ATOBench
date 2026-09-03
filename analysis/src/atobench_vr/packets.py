from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import re
import shutil
from collections import Counter
from pathlib import Path
from typing import Any

from .common import (
    GateError,
    ensure_output_available,
    load_jsonl,
    read_json,
    sha256_file,
    sha256_json,
    stage_manifest,
    write_json,
    write_jsonl,
)
from .redaction import StableRedactor

DIMENSIONS = ("verification_control", "stop_decision", "report_grounding")
RUBRIC_FILES = {
    "verification_control": "verification_control.md",
    "stop_decision": "stop_decision.md",
    "report_grounding": "report_grounding.md",
}
SECRET_PATTERNS = {
    "bearer": re.compile(r"(?i)Authorization\s*:\s*Bearer\s+(?![<\\\"'])[A-Za-z0-9._=-]+"),
    "jwt": re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{4,}\b"),
    "email": re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"),
    "password_field": re.compile(
        r"(?i)\bpassword\s*[:=][ \t]*(?:\\?[\"'])?(?![<\\\"'])[A-Za-z0-9!@#$%^&*._:+=-]{4,}"
    ),
}


def _opaque(seed: str, prefix: str, value: str, length: int = 20) -> str:
    digest = hmac.new(seed.encode(), value.encode(), hashlib.sha256).hexdigest()
    return f"{prefix}_{digest[:length]}"


def _group(rows: list[dict[str, Any]], key: str) -> dict[str, list[dict[str, Any]]]:
    result: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        result.setdefault(str(row.get(key) or ""), []).append(row)
    return result


def _walk_keys(value: Any, forbidden: set[str], path: str = "$") -> list[dict[str, str]]:
    hits: list[dict[str, str]] = []
    if isinstance(value, dict):
        for key, child in value.items():
            if key.lower() in forbidden:
                hits.append({"path": f"{path}.{key}", "rule": "forbidden_key"})
            hits.extend(_walk_keys(child, forbidden, f"{path}.{key}"))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            hits.extend(_walk_keys(child, forbidden, f"{path}[{index}]"))
    return hits


def _scan_text(text: str, forbidden_values: list[str]) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    leakage: list[dict[str, str]] = []
    secrets: list[dict[str, str]] = []
    for value in forbidden_values:
        if len(value) >= 3 and value in text:
            leakage.append({"rule": "private_identity_value", "value_sha256": sha256_json(value)})
    for name, pattern in SECRET_PATTERNS.items():
        for match in pattern.finditer(text):
            if name == "password_field":
                probe_context = text[match.start() : match.start() + 160].lower()
                if any(
                    marker in probe_context
                    for marker in (
                        " or 1=1",
                        " or '1'='1",
                        " or \\'1\\'=\\'1",
                        "--",
                    )
                ):
                    continue
            secrets.append({"rule": name})
            break
    return leakage, secrets


def _mapped_pointer(pointer: str, message_map: dict[str, str], event_map: dict[str, str]) -> str:
    if pointer.startswith("message:"):
        return "message:" + message_map.get(pointer.split(":", 1)[1], "UNAVAILABLE")
    if pointer.startswith("event:"):
        return "event:" + event_map.get(pointer.split(":", 1)[1], "UNAVAILABLE")
    if pointer.startswith("report:"):
        return pointer
    return "opaque:" + hashlib.sha256(pointer.encode()).hexdigest()[:12]


def _blind_private_text(text: str, forbidden_values: list[str]) -> str:
    result = text
    for value in sorted(
        {value for value in forbidden_values if len(value) >= 3},
        key=len,
        reverse=True,
    ):
        result = result.replace(value, "<PRIVATE_IDENTITY>")
    return result


def _blind_private_value(value: Any, forbidden_values: list[str]) -> Any:
    if isinstance(value, str):
        return _blind_private_text(value, forbidden_values)
    if isinstance(value, dict):
        return {
            key: _blind_private_value(item, forbidden_values)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_blind_private_value(item, forbidden_values) for item in value]
    return value


def _drop_forbidden_keys(value: Any, forbidden: set[str]) -> Any:
    if isinstance(value, dict):
        return {
            key: _drop_forbidden_keys(child, forbidden)
            for key, child in value.items()
            if key.lower() not in forbidden
        }
    if isinstance(value, list):
        return [_drop_forbidden_keys(child, forbidden) for child in value]
    return value


def _report_lines(
    path: Path, redactor: StableRedactor, forbidden_values: list[str]
) -> str:
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    return "\n".join(
        f"L{line_number:04d}: "
        f"{_blind_private_text(redactor.redact(line), forbidden_values)}"
        for line_number, line in enumerate(lines, 1)
    ) + "\n"


def build_packets(
    *,
    episode_rows: list[dict[str, Any]],
    facts: list[dict[str, Any]],
    messages: list[dict[str, Any]],
    action_cycles: list[dict[str, Any]],
    alignments: list[dict[str, Any]],
    stop_contexts: list[dict[str, Any]],
    atoms: list[dict[str, Any]],
    output_dir: Path,
    config: dict[str, Any],
    rubrics_dir: Path,
    output_schema_path: Path,
) -> dict[str, Any]:
    seed = str(config["packet_id_seed"])
    forbidden_keys = {
        str(value).lower() for value in config["forbidden_public_keys"]
    }
    facts_by_episode = _group(facts, "episode_id")
    messages_by_episode = _group(messages, "episode_id")
    cycles_by_episode = _group(action_cycles, "episode_id")
    alignments_by_episode = _group(alignments, "episode_id")
    stops_by_episode = {
        str(row["episode_id"]): row for row in stop_contexts
    }
    atoms_by_episode = _group(atoms, "episode_id")
    lineage_rows: list[dict[str, Any]] = []
    packet_manifest_rows: list[dict[str, Any]] = []
    leakage_hits: list[dict[str, Any]] = []
    secret_hits: list[dict[str, Any]] = []
    for episode in episode_rows:
        episode_id = str(episode.get("episode_id") or "")
        aou = str(episode.get("aou") or "")
        report_path = Path(str(episode.get("report_path") or ""))
        if not episode_id or aou not in {"sqli", "basket", "jwt"}:
            raise GateError(f"invalid packet episode row: {episode}")
        episode_facts = facts_by_episode.get(episode_id, [])
        episode_messages = messages_by_episode.get(episode_id, [])
        episode_cycles = cycles_by_episode.get(episode_id, [])
        episode_alignments = alignments_by_episode.get(episode_id, [])
        episode_atoms = atoms_by_episode.get(episode_id, [])
        public_values = [
            str(episode.get(key))
            for key in ("episode_id", "pair_id", "campaign_id", "condition", "model")
            if episode.get(key) is not None
        ]
        message_ids = sorted(
            {
                str(row.get("message_uuid"))
                for row in episode_messages
                if row.get("message_uuid")
            }
        )
        event_ids = sorted(
            {
                pointer.split(":", 1)[1]
                for fact in episode_facts
                for pointer in fact.get("source_pointers", [])
                if pointer.startswith("event:")
            }
        )
        fact_ids = sorted(str(fact["fact_id"]) for fact in episode_facts)
        message_map = {value: f"M{index:04d}" for index, value in enumerate(message_ids, 1)}
        event_map = {value: f"E{index:04d}" for index, value in enumerate(event_ids, 1)}
        fact_map = {value: f"F{index:04d}" for index, value in enumerate(fact_ids, 1)}
        atom_map = {
            str(atom["atom_id"]): f"A{index:04d}"
            for index, atom in enumerate(episode_atoms, 1)
        }
        cycle_ids = sorted(
            str(cycle["action_cycle_id"])
            for cycle in episode_cycles
            if cycle.get("action_cycle_id")
        )
        cycle_map = {
            value: f"C{index:04d}" for index, value in enumerate(cycle_ids, 1)
        }
        episode_pseudonym = _opaque(seed, "EP", episode_id, 16)
        redactor = StableRedactor(f"{seed}:{episode_pseudonym}")
        public_messages = [
            {
                "message_id": message_map[str(row["message_uuid"])],
                "sequence_index": row.get("sequence_index"),
                "content_type": row.get("content_type"),
                "recorded_rationale": _blind_private_text(
                    redactor.redact(row["recorded_rationale_text"]), public_values
                )
                if isinstance(row.get("recorded_rationale_text"), str)
                else None,
                "visible_text": _blind_private_text(
                    redactor.redact(row["visible_text"]), public_values
                )
                if isinstance(row.get("visible_text"), str)
                else None,
                "tool_name": row.get("tool_name"),
                "tool_input": _blind_private_value(
                    redactor.redact_value(row["tool_input_redacted"]),
                    public_values,
                )
                if row.get("content_type") == "tool_use"
                and row.get("tool_input_redacted") is not None
                else None,
            }
            for row in episode_messages
            if str(row.get("message_uuid")) in message_map
        ]
        for row in public_messages:
            if row.get("tool_input") is not None:
                row["tool_input"] = _drop_forbidden_keys(row["tool_input"], forbidden_keys)
        public_facts = [
            {
                "fact_id": fact_map[str(fact["fact_id"])],
                "fact_class": fact["fact_class"],
                "fact_type": fact["fact_type"],
                "value": _blind_private_text(fact["value"], public_values)
                if isinstance(fact.get("value"), str)
                else fact.get("value"),
                "measurement_status": fact["measurement_status"],
                "importance": fact["importance"],
                "source_kind": fact["source_kind"],
                "source_pointers": [
                    _mapped_pointer(pointer, message_map, event_map)
                    for pointer in fact.get("source_pointers", [])
                ],
                "confidence": fact["confidence"],
            }
            for fact in episode_facts
        ]
        public_atoms = [
            {
                "atom_id": atom_map[str(atom["atom_id"])],
                "atom_type": atom["atom_type"],
                "value": _blind_private_text(
                    redactor.redact(atom["value"]), public_values
                )
                if isinstance(atom.get("value"), str)
                else atom.get("value"),
                "importance": atom["importance"],
                "report_line_numbers": atom["report_line_numbers"],
                "trace_support_label": atom["trace_support_label"],
                "trace_fact_ids": [
                    fact_map[value]
                    for value in atom.get("trace_fact_ids", [])
                    if value in fact_map
                ],
                "semantic_matching_status": atom["semantic_matching_status"],
            }
            for atom in episode_atoms
        ]
        alignments_by_cycle: dict[str, list[dict[str, Any]]] = {}
        for alignment in episode_alignments:
            cycle_id = str(alignment.get("action_cycle_id") or "")
            if cycle_id:
                alignments_by_cycle.setdefault(cycle_id, []).append(alignment)
        public_actions = []
        for cycle in episode_cycles:
            source_cycle_id = str(cycle.get("action_cycle_id") or "")
            if source_cycle_id not in cycle_map:
                continue
            action_alignments = alignments_by_cycle.get(source_cycle_id, [])
            public_actions.append(
                {
                    "action_cycle_id": cycle_map[source_cycle_id],
                    "sequence_index": cycle.get("sequence_index"),
                    "tool_name": cycle.get("tool_name"),
                    "preceding_message_ids": [
                        message_map[value]
                        for value in cycle.get("preceding_recorded_message_ids", [])
                        if value in message_map
                    ],
                    "tool_result_message_ids": [
                        message_map[value]
                        for value in cycle.get("tool_result_message_ids", [])
                        if value in message_map
                    ],
                    "http_requests": [
                        {
                            "method": request.get("method"),
                            "canonical_route": request.get("canonical_route"),
                            "request_body_sha256": request.get("request_body_sha256"),
                            "has_authorization_header": request.get(
                                "has_authorization_header"
                            ),
                        }
                        for request in cycle.get("parsed_http_requests", [])
                    ],
                    "alignments": [
                        {
                            "request_index": alignment.get("request_index"),
                            "alignment_status": alignment.get("alignment_status"),
                            "accepted_event_ids": [
                                event_map[value]
                                for value in alignment.get("accepted_event_ids", [])
                                if value in event_map
                            ],
                            "accepted_for_evidence": alignment.get(
                                "accepted_for_evidence"
                            ),
                        }
                        for alignment in action_alignments
                    ],
                }
            )
        for dimension in DIMENSIONS:
            packet_id = _opaque(
                seed, "pkt", f"{episode_id}:{dimension}", 20
            )
            dimension_dir = output_dir / dimension / packet_id
            evidence_dir = dimension_dir / "packet_evidence"
            evidence_dir.mkdir(parents=True)
            evidence_files: list[Path] = []
            if dimension in {"verification_control", "stop_decision"}:
                message_path = evidence_dir / "messages.json"
                write_json(message_path, public_messages)
                evidence_files.append(message_path)
                allowed_fact_classes = {"intervention", "verification", "process"}
                dimension_facts = [
                    fact for fact in public_facts if fact["fact_class"] in allowed_fact_classes
                ]
                fact_path = evidence_dir / "facts.json"
                write_json(fact_path, dimension_facts)
                action_path = evidence_dir / "actions.json"
                write_json(action_path, public_actions)
                evidence_files.extend([fact_path, action_path])
            if dimension == "stop_decision":
                stop = dict(stops_by_episode.get(episode_id) or {})
                stop.pop("episode_id", None)
                stop_path = evidence_dir / "stop_context.json"
                write_json(stop_path, stop)
                evidence_files.append(stop_path)
            if dimension == "report_grounding":
                if not report_path.is_file():
                    raise GateError(f"report missing for report-grounding packet: {episode_id}")
                report_output = evidence_dir / "final_report.md"
                report_output.write_text(
                    _report_lines(report_path, redactor, public_values),
                    encoding="utf-8",
                )
                atom_path = evidence_dir / "report_claim_atoms.json"
                write_json(atom_path, public_atoms)
                fact_path = evidence_dir / "facts.json"
                write_json(
                    fact_path,
                    [
                        fact
                        for fact in public_facts
                        if fact["fact_class"] in {"intervention", "verification", "report"}
                    ],
                )
                evidence_files.extend([report_output, atom_path, fact_path])
            core = {
                "packet_id": packet_id,
                "packet_schema_version": config["packet_schema_version"],
                "episode_pseudonym": episode_pseudonym,
                "aou": aou,
                "dimension": dimension,
                "input_fact_ids": [
                    fact["fact_id"]
                    for fact in public_facts
                    if not (
                        dimension == "report_grounding"
                        and fact["fact_class"] == "process"
                    )
                ],
                "input_message_ids": (
                    [row["message_id"] for row in public_messages]
                    if dimension != "report_grounding"
                    else []
                ),
                "input_event_ids": list(event_map.values()),
                "redaction_version": config["redaction_version"],
                "prompt_version": config["prompt_version"],
            }
            evidence_hashes = {
                path.name: sha256_file(path) for path in sorted(evidence_files)
            }
            core["packet_sha256"] = sha256_json(
                {"core": core, "evidence_hashes": evidence_hashes}
            )
            packet_path = dimension_dir / "packet.json"
            write_json(packet_path, core)
            shutil.copy2(
                rubrics_dir / RUBRIC_FILES[dimension], dimension_dir / "rubric.md"
            )
            shutil.copy2(output_schema_path, dimension_dir / "output_schema.json")
            public_files = [
                packet_path,
                dimension_dir / "rubric.md",
                dimension_dir / "output_schema.json",
                *evidence_files,
            ]
            for public_file in public_files:
                text = public_file.read_text(encoding="utf-8", errors="replace")
                leak, secrets = _scan_text(text, public_values)
                leakage_hits.extend(
                    {
                        "packet_id": packet_id,
                        "file": public_file.name,
                        **hit,
                    }
                    for hit in leak
                )
                secret_hits.extend(
                    {
                        "packet_id": packet_id,
                        "file": public_file.name,
                        **hit,
                    }
                    for hit in secrets
                )
                if public_file.suffix == ".json":
                    try:
                        json_value = json.loads(text)
                    except json.JSONDecodeError:
                        continue
                    leakage_hits.extend(
                        {
                            "packet_id": packet_id,
                            "file": public_file.name,
                            **hit,
                        }
                        for hit in _walk_keys(json_value, forbidden_keys)
                    )
            lineage_rows.append(
                {
                    "packet_id": packet_id,
                    "packet_file_sha256": sha256_file(packet_path),
                    "episode_id": episode_id,
                    "pair_id": episode.get("pair_id"),
                    "campaign_id": episode.get("campaign_id"),
                    "condition": episode.get("condition"),
                    "model": episode.get("model"),
                    "dimension": dimension,
                    "message_id_map": message_map,
                    "event_id_map": event_map,
                    "fact_id_map": fact_map,
                    "atom_id_map": atom_map,
                    "action_cycle_id_map": cycle_map,
                    "must_never_be_sent_to_judge": True,
                }
            )
            packet_manifest_rows.append(
                {
                    "packet_id": packet_id,
                    "dimension": dimension,
                    "aou": aou,
                    "relative_path": str(dimension_dir.relative_to(output_dir)),
                    "packet_file_sha256": sha256_file(packet_path),
                    "evidence_hashes": evidence_hashes,
                }
            )
    return {
        "packets": packet_manifest_rows,
        "lineage": lineage_rows,
        "leakage_hits": leakage_hits,
        "secret_hits": secret_hits,
    }


def run(
    *,
    episode_manifest_path: Path,
    facts_path: Path,
    messages_path: Path,
    action_cycles_path: Path,
    alignment_path: Path,
    stop_contexts_path: Path,
    claim_atoms_path: Path,
    output_dir: Path,
    config_path: Path,
    rubrics_dir: Path,
    output_schema_path: Path,
    dry_run: bool,
    new_version: bool,
) -> dict[str, Any]:
    episodes = load_jsonl(episode_manifest_path)
    facts = load_jsonl(facts_path)
    messages = load_jsonl(messages_path)
    action_cycles = load_jsonl(action_cycles_path)
    alignments = load_jsonl(alignment_path)
    stops = load_jsonl(stop_contexts_path)
    atoms = load_jsonl(claim_atoms_path)
    config = read_json(config_path)
    if dry_run:
        return {
            "stage": "07_build_judge_packets",
            "status": "dry_run",
            "episode_count": len(episodes),
            "planned_packet_count": len(episodes) * 3,
        }
    ensure_output_available(output_dir, new_version)
    result = build_packets(
        episode_rows=episodes,
        facts=facts,
        messages=messages,
        action_cycles=action_cycles,
        alignments=alignments,
        stop_contexts=stops,
        atoms=atoms,
        output_dir=output_dir,
        config=config,
        rubrics_dir=rubrics_dir,
        output_schema_path=output_schema_path,
    )
    private_dir = output_dir / "private"
    private_dir.mkdir(parents=True)
    write_jsonl(private_dir / "packet_lineage.jsonl", result["lineage"])
    write_json(output_dir / "leakage_scan.json", {"hits": result["leakage_hits"]})
    write_json(output_dir / "secret_scan.json", {"hits": result["secret_hits"]})
    packet_manifest = {
        "schema_version": "atobench.trajectory_packet_manifest.v1",
        "packet_count": len(result["packets"]),
        "dimension_counts": dict(
            Counter(row["dimension"] for row in result["packets"])
        ),
        "packets": result["packets"],
        "leakage_hit_count": len(result["leakage_hits"]),
        "secret_hit_count": len(result["secret_hits"]),
        "private_lineage_is_judge_forbidden": True,
    }
    write_json(output_dir / "packet_manifest.json", packet_manifest)
    blocked = bool(result["leakage_hits"] or result["secret_hits"])
    manifest = stage_manifest(
        "07_build_judge_packets",
        [
            episode_manifest_path,
            facts_path,
            messages_path,
            action_cycles_path,
            alignment_path,
            stop_contexts_path,
            claim_atoms_path,
            config_path,
            output_schema_path,
            *[rubrics_dir / name for name in RUBRIC_FILES.values()],
        ],
        [
            output_dir / "packet_manifest.json",
            output_dir / "leakage_scan.json",
            output_dir / "secret_scan.json",
            private_dir / "packet_lineage.jsonl",
        ],
        "blocked" if blocked else "complete",
        dry_run=False,
        details={
            "packet_count": len(result["packets"]),
            "leakage_hit_count": len(result["leakage_hits"]),
            "secret_hit_count": len(result["secret_hits"]),
        },
    )
    write_json(output_dir / "stage_manifest.json", manifest)
    if blocked:
        raise GateError(
            f"packet safety gate failed: leakage={len(result['leakage_hits'])}, "
            f"secrets={len(result['secret_hits'])}"
        )
    return manifest


def cli(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--episode-manifest", type=Path, required=True)
    parser.add_argument("--facts", type=Path, required=True)
    parser.add_argument("--messages", type=Path, required=True)
    parser.add_argument("--action-cycles", type=Path, required=True)
    parser.add_argument("--alignment", type=Path, required=True)
    parser.add_argument("--stop-contexts", type=Path, required=True)
    parser.add_argument("--claim-atoms", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--rubrics-dir", type=Path, required=True)
    parser.add_argument("--output-schema", type=Path, required=True)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--new-version", action="store_true")
    args = parser.parse_args(argv)
    try:
        result = run(
            episode_manifest_path=args.episode_manifest,
            facts_path=args.facts,
            messages_path=args.messages,
            action_cycles_path=args.action_cycles,
            alignment_path=args.alignment,
            stop_contexts_path=args.stop_contexts,
            claim_atoms_path=args.claim_atoms,
            output_dir=args.output,
            config_path=args.config,
            rubrics_dir=args.rubrics_dir,
            output_schema_path=args.output_schema,
            dry_run=args.dry_run,
            new_version=args.new_version,
        )
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    except GateError as exc:
        parser.exit(2, f"BLOCKED: {exc}\n")
    return 2
