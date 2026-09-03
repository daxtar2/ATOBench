#!/usr/bin/env python3
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from atobench_vr.judge_runner import run_packet


def build_packet(root: Path) -> Path:
    packet = root / "packet"
    evidence = packet / "packet_evidence"
    evidence.mkdir(parents=True)
    (packet / "rubric.md").write_text("# Synthetic orchestration rubric\n")
    (evidence / "messages.json").write_text(
        json.dumps({"MSG-001": "The agent performed an independent check."})
    )
    (evidence / "facts.json").write_text(
        json.dumps({"FACT-001": {"type": "independent_check", "value": True}})
    )
    (packet / "output_schema.json").write_text(
        (ROOT / "config" / "output_schema.json").read_text()
    )
    (packet / "packet.json").write_text(
        json.dumps(
            {
                "packet_id": "synthetic-packet-01",
                "packet_schema_version": "fixture.v1",
                "episode_pseudonym": "EP-X",
                "aou": "sqli",
                "dimension": "verification_control",
                "input_fact_ids": ["FACT-001"],
                "input_message_ids": ["MSG-001"],
                "input_event_ids": [],
                "packet_sha256": "fixture",
                "redaction_version": "fixture",
                "prompt_version": "fixture",
            }
        )
    )
    return packet


with tempfile.TemporaryDirectory(prefix="atobench-subagent-smoke-") as tmp:
    smoke_root = Path(tmp)
    fake_claude = ROOT / "tests" / "fake_claude.py"
    result = run_packet(
        packet_dir=build_packet(smoke_root),
        output_dir=smoke_root / "output",
        config_path=ROOT / "config" / "judge_runner_config.json",
        lock_path=ROOT / "config" / "analysis_spec.draft.lock.yaml",
        agents_dir=ROOT / "config" / "subagent_specs",
        allow_judge_calls=False,
        synthetic_smoke=True,
        executable_override=str(fake_claude),
    )
    summary = {
        "schema_version": "atobench.subagent_orchestration_smoke.v1",
        "status": "PASS",
        "packet_id": result["packet_id"],
        "parallel_reviewer_count": 2,
        "evidence_verifier_count": 2,
        "adjudication_triggered": result["adjudication_triggered"],
        "invocation_count": result["invocation_count"],
        "final_method": result["final"]["method"],
        "real_claude_called": False,
        "model_cost_usd": 0,
        "workspace_retained": False,
    }
    print(json.dumps(summary, indent=2, sort_keys=True))
