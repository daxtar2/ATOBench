from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from atobench_vr.common import GateError
from atobench_vr.judge_runner import _extract_structured_output, run_packet


class JudgeRunnerTests(unittest.TestCase):
    def test_extracts_final_schema_object_after_prefatory_json(self) -> None:
        decision = {
            "schema_version": "atobench.alignment_verifier_decision.v1",
            "alignment_task_id": "task-1",
        }
        envelope = {
            "type": "result",
            "result": (
                'First considered {"candidate_id":"H0001"}. Final:\\n```json\\n'
                + json.dumps(decision)
                + "\\n```"
            ),
        }
        extracted, _ = _extract_structured_output(json.dumps(envelope))
        self.assertEqual(extracted, decision)

    def test_extracts_schema_object_from_nested_result_wrapper(self) -> None:
        schema = {
            "type": "object",
            "required": ["packet_id", "score", "adjudication_reason_code"],
        }
        decision = {
            "packet_id": "packet-1",
            "score": 4,
            "adjudication_reason_code": "resolved",
        }
        envelope = {
            "type": "result",
            "subtype": "success",
            "result": [
                {"type": "text", "text": "preface"},
                {"type": "json", "value": decision},
            ],
            "usage": {"input_tokens": 100},
        }
        extracted, returned_envelope = _extract_structured_output(
            json.dumps(envelope), schema
        )
        self.assertEqual(extracted, decision)
        self.assertEqual(returned_envelope, envelope)

    def fixture_packet(self, root: Path) -> Path:
        packet = root / "packet"
        evidence = packet / "packet_evidence"
        evidence.mkdir(parents=True)
        (packet / "rubric.md").write_text("# Synthetic frozen rubric\n")
        (evidence / "messages.json").write_text(
            json.dumps({"MSG-001": "The agent performed an independent check."})
        )
        (evidence / "facts.json").write_text(
            json.dumps({"FACT-001": {"type": "independent_check", "value": True}})
        )
        schema = json.loads((ROOT / "config" / "output_schema.json").read_text())
        (packet / "output_schema.json").write_text(json.dumps(schema))
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

    def fake_executable(self) -> str:
        path = ROOT / "tests" / "fake_claude.py"
        if not path.is_file():
            self.fail("fake Claude executable is missing")
        return str(path)

    def test_full_synthetic_orchestration_calls_five_isolated_agents(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            packet = self.fixture_packet(root)
            result = run_packet(
                packet_dir=packet,
                output_dir=root / "output",
                config_path=ROOT / "config" / "judge_runner_config.json",
                lock_path=ROOT / "config" / "analysis_spec.draft.lock.yaml",
                agents_dir=ROOT / "config" / "subagent_specs",
                allow_judge_calls=False,
                synthetic_smoke=True,
                executable_override=self.fake_executable(),
            )
            self.assertTrue(result["adjudication_triggered"])
            self.assertEqual(result["adjudication_trigger"], "two_point_disagreement")
            self.assertEqual(result["invocation_count"], 5)
            self.assertEqual(result["final"]["method"], "adjudicated")
            self.assertEqual(result["final"]["score"], 8)
            receipts = list((root / "output" / "receipts").glob("*.json"))
            self.assertEqual(len(receipts), 5)
            for receipt_path in receipts:
                receipt = json.loads(receipt_path.read_text())
                self.assertEqual(receipt["allowed_tools"], ["Read"])
                self.assertTrue(receipt["no_session_persistence"])
                self.assertFalse(receipt["workspace_retained"])

    def test_real_calls_remain_blocked_while_lock_is_draft(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            packet = self.fixture_packet(root)
            with self.assertRaises(GateError):
                run_packet(
                    packet_dir=packet,
                    output_dir=root / "output",
                    config_path=ROOT / "config" / "judge_runner_config.json",
                    lock_path=ROOT / "config" / "analysis_spec.draft.lock.yaml",
                    agents_dir=ROOT / "config" / "subagent_specs",
                    allow_judge_calls=True,
                    synthetic_smoke=False,
                    executable_override=self.fake_executable(),
                )


if __name__ == "__main__":
    unittest.main()
