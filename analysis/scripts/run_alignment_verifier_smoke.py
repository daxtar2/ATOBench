#!/usr/bin/env python3
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from atobench_vr.alignment import run as run_alignment
from atobench_vr.alignment_verifier import run as run_verifiers
from atobench_vr.common import load_jsonl, write_jsonl


with tempfile.TemporaryDirectory(prefix="atobench-alignment-smoke-") as tmp:
    smoke_root = Path(tmp)
    cycles = smoke_root / "cycles.jsonl"
    events = smoke_root / "events.jsonl"
    write_jsonl(
        cycles,
        [
            {
                "episode_id": "synthetic-private-episode",
                "action_cycle_id": "synthetic-private-cycle",
                "timestamp": "2026-01-01T00:00:00Z",
                "parsed_http_requests": [
                    {
                        "request_index": 0,
                        "method": "GET",
                        "canonical_route": "/api/items/{id}",
                        "request_body_sha256": None,
                    }
                ],
            }
        ],
    )
    write_jsonl(
        events,
        [
            {
                "event_id": "synthetic-private-event",
                "episode_id": "synthetic-private-episode",
                "method": "GET",
                "canonical_route": "/api/items/{id}",
                "timestamp": "2026-01-01T00:00:01Z",
            }
        ],
    )
    initial = smoke_root / "initial"
    run_alignment(
        action_cycles_path=cycles,
        http_events_paths=[events],
        output_dir=initial,
        tolerance_seconds=3.0,
        allow_real_data=False,
        dry_run=False,
        new_version=False,
    )
    validated = smoke_root / "validated"
    manifest = run_verifiers(
        alignment_path=initial / "claude_http_alignment.jsonl",
        action_cycles_path=cycles,
        http_events_paths=[events],
        output_dir=validated,
        config_path=ROOT / "config" / "judge_runner_config.json",
        lock_path=ROOT / "config" / "analysis_spec.draft.lock.yaml",
        agent_spec_path=ROOT
        / "config"
        / "subagent_specs"
        / "atobench-alignment-verifier.md",
        output_schema_path=ROOT
        / "config"
        / "alignment_verifier_output_schema.json",
        tolerance_seconds=3.0,
        allow_real_data=False,
        allow_calls=False,
        synthetic_smoke=True,
        executable_override=str(ROOT / "tests" / "fake_claude.py"),
        task_allowlist_path=None,
        max_calls=None,
        preflight_only=False,
        dry_run=False,
        new_version=False,
    )
    rows = load_jsonl(validated / "claude_http_alignment.validated.jsonl")
    accepted = [
        row
        for row in rows
        if row.get("dual_verifier_status") == "agreed_accepted"
    ]
    print(
        json.dumps(
            {
                "schema_version": "atobench.alignment_verifier_smoke.v1",
                "status": "PASS" if len(accepted) == 1 else "FAIL",
                "pending_alignment_count": 1,
                "parallel_verifier_count": 2,
                "accepted_after_agreement": len(accepted),
                "invocation_count": manifest["details"]["invocation_count"],
                "real_claude_called": False,
                "model_cost_usd": 0,
                "workspace_retained": False,
            },
            indent=2,
            sort_keys=True,
        )
    )
