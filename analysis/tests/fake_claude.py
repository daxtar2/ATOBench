#!/usr/bin/env python3
from __future__ import annotations

import json
import sys
from pathlib import Path


def argument(name: str) -> str:
    index = sys.argv.index(name)
    return sys.argv[index + 1]


if "--version" in sys.argv:
    print("2.1.156-fake")
    raise SystemExit(0)

agent = argument("--agent")
prompt = argument("-p")
if agent == "atobench-trajectory-judge":
    is_b = "trajectory_judge_b" in prompt
    score = 9 if is_b else 7
    band = "excellent" if is_b else "good"
    packet_id = prompt.split("Review packet ", 1)[1].split(" ", 1)[0]
    dimension = prompt.split("for dimension ", 1)[1].split(".", 1)[0]
    output = {
        "schema_version": "atobench.trajectory_judgment.v1",
        "packet_id": packet_id,
        "dimension": dimension,
        "score": score,
        "score_low": score,
        "score_high": score,
        "band": band,
        "confidence": "medium",
        "insufficient_evidence": False,
        "reason_code": "synthetic_independent_check",
        "material_strengths": ["Independent check was attempted."],
        "material_deficiencies": [],
        "supporting_message_ids": ["MSG-001"],
        "supporting_event_ids": [],
        "supporting_fact_ids": ["FACT-001"],
        "forbidden_identity_not_accessed": True,
        "outside_knowledge_not_used": True,
    }
elif agent == "atobench-evidence-verifier":
    output = {
        "status": "entailed",
        "reason": "The cited fixture explicitly records the independent check.",
        "verified_pointer_ids": ["MSG-001", "FACT-001"],
        "missing_pointer_ids": [],
    }
elif agent == "atobench-judge-adjudicator":
    request_path = Path("adjudication_request.json")
    if request_path.is_file():
        request = json.loads(request_path.read_text())
        packet_id = request["packet_id"]
        dimension = request["dimension"]
    else:
        packet_id = "synthetic-packet-01"
        dimension = "verification_control"
    output = {
        "schema_version": "atobench.trajectory_judgment.v1",
        "packet_id": packet_id,
        "dimension": dimension,
        "score": 8,
        "score_low": 7,
        "score_high": 9,
        "band": "good",
        "confidence": "medium",
        "insufficient_evidence": False,
        "reason_code": "synthetic_adjudicated",
        "material_strengths": ["Independent check was attempted."],
        "material_deficiencies": [],
        "supporting_message_ids": ["MSG-001"],
        "supporting_event_ids": [],
        "supporting_fact_ids": ["FACT-001"],
        "forbidden_identity_not_accessed": True,
        "outside_knowledge_not_used": True,
        "adjudication_reason_code": "resolved_two_point_gap",
        "resolved_review_defect_ids": ["reviewer_band_placement"],
    }
elif agent == "atobench-alignment-verifier":
    reviewer_id = (
        "alignment_verifier_b"
        if "alignment_verifier_b" in prompt
        else "alignment_verifier_a"
    )
    task_id = prompt.split("alignment task ", 1)[1].split(" ", 1)[0]
    disagree = "disagree" in sys.argv[0] and reviewer_id == "alignment_verifier_b"
    output = {
        "schema_version": "atobench.alignment_verifier_decision.v1",
        "alignment_task_id": task_id,
        "reviewer_id": reviewer_id,
        "selection_status": "ambiguous" if disagree else "selected",
        "selected_candidate_id": None if disagree else "H0001",
        "constraint_results": {
            "method_match": True,
            "route_match": True,
            "payload_nonconflict": True,
            "timestamp_within_tolerance": True,
            "unique_best_candidate": not disagree,
        },
        "reason": (
            "The candidate set is ambiguous."
            if disagree
            else "The only candidate satisfies every frozen constraint."
        ),
        "forbidden_identity_not_accessed": True,
        "outside_knowledge_not_used": True,
    }
elif agent == "atobench-report-semantic-matcher":
    request = json.loads(Path("match_request.json").read_text())
    packet = json.loads(Path("packet.json").read_text())
    semantic_disagree = (
        "semantic_disagree" in sys.argv[0]
        and request["matcher_id"] == "semantic_matcher_b"
    )
    output = {
        "schema_version": "atobench.report_semantic_match.v1",
        "semantic_packet_id": packet["semantic_packet_id"],
        "matcher_id": request["matcher_id"],
        "registered_finding_mentioned": True,
        "report_closure": False if semantic_disagree else True,
        "matched_claim_atom_ids": [packet["allowed_claim_atom_ids"][0]],
        "claim_trace_support": (
            "not_applicable" if semantic_disagree else "supported"
        ),
        "supporting_fact_ids": (
            [] if semantic_disagree else [packet["registered_primary_fact_id"]]
        ),
        "contradicting_fact_ids": [],
        "confidence": "high",
        "insufficient_evidence": False,
        "reason_code": "synthetic_supported_closure",
        "reason": "The synthetic report claim and primary fact agree.",
        "forbidden_identity_not_accessed": True,
        "outside_knowledge_not_used": True,
    }
elif agent == "atobench-report-semantic-adjudicator":
    packet = json.loads(Path("packet.json").read_text())
    request = json.loads(Path("adjudication_request.json").read_text())
    required_files = {
        "packet_evidence/final_report.md",
        "packet_evidence/report_claim_atoms.json",
        "packet_evidence/facts.json",
    }
    if not required_files.issubset(set(request.get("allowed_files") or [])):
        raise SystemExit("semantic adjudication request omitted evidence files")
    output = {
        "schema_version": "atobench.report_semantic_match.v1",
        "semantic_packet_id": packet["semantic_packet_id"],
        "matcher_id": "semantic_adjudicator",
        "registered_finding_mentioned": True,
        "report_closure": True,
        "matched_claim_atom_ids": [packet["allowed_claim_atom_ids"][0]],
        "claim_trace_support": "supported",
        "supporting_fact_ids": [packet["registered_primary_fact_id"]],
        "contradicting_fact_ids": [],
        "confidence": "high",
        "insufficient_evidence": False,
        "reason_code": "synthetic_adjudicated_closure",
        "reason": "The synthetic adjudicator resolved the label.",
        "forbidden_identity_not_accessed": True,
        "outside_knowledge_not_used": True,
    }
else:
    raise SystemExit(f"unexpected agent: {agent}")

print(
    json.dumps(
        {
            "type": "result",
            "subtype": "success",
            "model": "fake-model",
            "usage": {"input_tokens": 100, "output_tokens": 50},
            "total_cost_usd": 0.0,
            "structured_output": output,
        }
    )
)
