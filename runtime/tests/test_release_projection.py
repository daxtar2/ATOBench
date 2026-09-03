from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

from atobench.agents.proxy_runner import (
    _parse_claude_stream_json,
    _load_completion_artifact,
    _summarize_claude_stream,
    _vendored_agent_definition,
)
from atobench.experiment.cross_model_protocol import AOUS, build_parser, main
from proxy.jsonl_logger import start_episode


class ReleaseProjectionTests(unittest.TestCase):
    def test_executable_runtime_components_are_present(self) -> None:
        root = Path(__file__).resolve().parents[1] / "atobench"
        required = (
            root / "cli" / "main.py",
            root / "experiment" / "cycle.py",
            root / "experiment" / "cross_model_protocol.py",
            root / "agents" / "proxy_runner.py",
            root / "proxy" / "rule_engine.py",
            root / "schema" / "runtime_program.json",
            root / "targets" / "juice-shop" / "benchmark_suites" / "juice-shop-confirmatory-v1" / "suite_manifest.yaml",
        )
        self.assertTrue(all(path.is_file() for path in required))
        self.assertEqual(set(AOUS), {"sqli", "basket", "jwt"})
        self.assertTrue(callable(start_episode))
        args = build_parser().parse_args(["--models", "qwen3.7-plus", "--aous", "sqli", "--dry-run"])
        self.assertTrue(args.dry_run)

    def test_stream_route_attestation_parser_reads_assistant_model_event(self) -> None:
        stream = "\n".join((
            json.dumps({"type": "assistant", "message": {"model": "qwen3.7-plus"}}),
            json.dumps({"type": "result", "is_error": False, "result": "complete"}),
        ))
        result, events = _parse_claude_stream_json(stream)
        self.assertEqual(result["type"], "result")
        self.assertEqual(events[0]["message"]["model"], "qwen3.7-plus")

    def test_incomplete_stream_is_diagnosable_but_not_accepted_as_a_result(self) -> None:
        stream = "\n".join((
            json.dumps({"type": "system", "subtype": "init"}),
            json.dumps({"type": "assistant", "message": {"model": "glm-5.2"}}),
        ))
        with self.assertRaisesRegex(ValueError, "no JSON stream result event"):
            _parse_claude_stream_json(stream)
        summary = _summarize_claude_stream(stream)
        self.assertEqual(summary["event_count"], 2)
        self.assertEqual(summary["last_event_types"], ["system:init", "assistant:<none>"])

    def test_completion_artifact_is_a_strict_fallback_for_missing_result_events(self) -> None:
        report = {
            "posture_assessment": "secure",
            "run_validity": "complete",
            "budget_used": {"max_tool_calls": 1, "estimated_http_calls": 1, "stopped_reason": "coverage_complete"},
            "coverage_summary": {"tested_surface": ["/"], "untested_surface": [], "auth_attempted": False},
            "findings": [],
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            artifact = Path(tmpdir) / "agent_final_report.txt"
            artifact.write_text("FINAL_FINDINGS: " + json.dumps(report), encoding="utf-8")
            self.assertEqual(_load_completion_artifact(artifact), artifact.read_text(encoding="utf-8"))
            artifact.write_text("FINAL_FINDINGS: " + json.dumps(report) + " trailing", encoding="utf-8")
            self.assertIsNotNone(_load_completion_artifact(artifact))

    def test_harness_agent_is_vendored_for_direct_cli_execution(self) -> None:
        definition = _vendored_agent_definition("atobench-harnessed-pentest")
        self.assertIsNotNone(definition)
        assert definition is not None
        self.assertIn("Single-agent ATOBench pentest benchmark agent", definition["description"])
        self.assertIn("Do not call, invoke, spawn, or simulate any other subagent", definition["prompt"])

    def test_original_protocol_runner_materializes_a_pair_in_dry_run(self) -> None:
        root = Path(__file__).resolve().parents[1] / "atobench"
        campaign_id = "release_projection_test_pair"
        campaign_dir = root / "targets" / "juice-shop" / "experiments" / campaign_id
        try:
            with redirect_stdout(StringIO()):
                returncode = main([
                    "--campaign-id", campaign_id,
                    "--rounds", "1",
                    "--models", "qwen3.7-plus",
                    "--model-selector", "qwen3.7-plus=opus",
                    "--aous", "sqli",
                    "--parallel-workers", "1",
                    "--dry-run",
                ])
            self.assertEqual(returncode, 0)
            manifest = json.loads((campaign_dir / "campaign_manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(len(manifest["planned_episodes"]), 2)
            self.assertEqual([item["condition"] for item in manifest["planned_episodes"]], ["C1", "C0"])
        finally:
            shutil.rmtree(campaign_dir, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
