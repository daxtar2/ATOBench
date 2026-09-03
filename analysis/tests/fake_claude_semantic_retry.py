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
if agent != "atobench-report-semantic-matcher":
    raise SystemExit(f"unexpected agent: {agent}")

request = json.loads(Path("match_request.json").read_text())
packet = json.loads(Path("packet.json").read_text())
matcher_id = request["matcher_id"]
state_path = Path(sys.argv[0]).with_name(f".{matcher_id}.attempts")
attempt = int(state_path.read_text() if state_path.is_file() else "0") + 1
state_path.write_text(str(attempt))

if matcher_id == "semantic_matcher_a" and attempt == 1:
    output = {"file_path": "/output_schema.json"}
else:
    output = {
        "schema_version": "atobench.report_semantic_match.v1",
        "semantic_packet_id": packet["semantic_packet_id"],
        "matcher_id": matcher_id,
        "registered_finding_mentioned": True,
        "report_closure": True,
        "matched_claim_atom_ids": [packet["allowed_claim_atom_ids"][0]],
        "claim_trace_support": "supported",
        "supporting_fact_ids": [packet["registered_primary_fact_id"]],
        "contradicting_fact_ids": [],
        "confidence": "high",
        "insufficient_evidence": False,
        "reason_code": "synthetic_retry_success",
        "reason": "The bounded retry returned a usable semantic object.",
        "forbidden_identity_not_accessed": True,
        "outside_knowledge_not_used": True,
    }

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
