from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from atobench_vr.common import GateError
from atobench_vr.pipeline import run_flatten, run_framework_stage, smoke
from atobench_vr.redaction import (
    StableRedactor,
    residual_sensitive_kinds,
    summarize_large_value,
)
from atobench_vr.schemas import validate_judgment
from atobench_vr.sessions import build_action_cycles, session_inventory_row


class FrameworkTests(unittest.TestCase):
    def test_smoke_is_offline_and_non_scientific(self) -> None:
        result = smoke()
        self.assertEqual(result["status"], "PASS")
        self.assertFalse(result["real_campaigns_read"])
        self.assertFalse(result["judge_calls_made"])
        self.assertFalse(result["scientific_analysis_performed"])

    def test_zero_output_sidecars_is_inline_only_not_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "session.jsonl"
            path.write_text(json.dumps({"sessionId": "session", "message": {"role": "assistant", "content": "small inline output"}}) + "\n")
            row = session_inventory_row(path)
        self.assertEqual(row["optional_sidecar_output_count"], 0)
        self.assertEqual(row["output_coverage_status"], "canonical_jsonl_inline_only")
        self.assertFalse(row["missing_output_inferred"])

    def test_redaction_is_stable_within_scope(self) -> None:
        redactor = StableRedactor("fixture")
        one = redactor.redact("email user@example.test")
        two = redactor.redact("again user@example.test")
        self.assertEqual(one.split()[-1], two.split()[-1])
        self.assertNotIn("user@example.test", one + two)

    def test_redaction_v2_is_idempotent_and_covers_secret_types(self) -> None:
        raw = (
            "Authorization: Bearer eyJa.bcde.fghi\n"
            "Cookie: sid=abc123\npassword=secret\ntotpSecret=ABCDEF\n"
            "resetAnswer=blue\napi_key=abcdefgh1234\nuserId=42\n"
            "sessionId=abc-def\nemail=user@example.test"
        )
        redactor = StableRedactor("pair")
        redacted = redactor.redact(raw)
        self.assertEqual(redactor.redact(redacted), redacted)
        quoted = "securityAnswer: '<RESET_ANSWER_1234ABCD>'"
        self.assertEqual(redactor.redact(quoted), quoted)
        self.assertFalse(residual_sensitive_kinds(redacted))
        for value in (
            "eyJabcdefgh",
            "abc123",
            "secret",
            "ABCDEF",
            "blue",
            "abcdefgh1234",
            "user@example.test",
        ):
            self.assertNotIn(value, redacted)

    def test_large_body_is_summarized_after_redaction(self) -> None:
        value = "password=secret\n" + ("x" * 40_000)
        summarized = summarize_large_value(value, StableRedactor("pair"))
        self.assertTrue(summarized["redacted_large_body"])
        self.assertNotIn("password=secret", summarized["redacted_prefix"])
        self.assertEqual(summarized["original_char_count"], len(value))

    def test_structured_redaction_preserves_json_shape(self) -> None:
        value = {
            "command": "curl --data '{\"password\":\"s,e}c\\\"ret\"}' /api",
            "nested": ["user@example.test", 42, None],
        }
        redacted = summarize_large_value(value, StableRedactor("pair"))
        self.assertEqual(set(redacted), {"command", "nested"})
        self.assertIsInstance(redacted["nested"], list)
        self.assertEqual(redacted["nested"][1:], [42, None])
        self.assertNotIn("user@example.test", json.dumps(redacted))

    def test_judge_runner_is_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(GateError):
                run_framework_stage("08", Path(tmp) / "judge", dry_run=False, new_version=False)

    def test_null_is_required_for_insufficient_evidence(self) -> None:
        value = {
            "schema_version": "atobench.trajectory_judgment.v1",
            "packet_id": "p",
            "dimension": "stop_decision",
            "score": 1,
            "score_low": 1,
            "score_high": 1,
            "band": "poor",
            "confidence": "low",
            "insufficient_evidence": True,
            "reason_code": "missing",
            "material_strengths": [],
            "material_deficiencies": [],
            "supporting_message_ids": [],
            "supporting_event_ids": [],
            "supporting_fact_ids": [],
            "forbidden_identity_not_accessed": True,
            "outside_knowledge_not_used": True,
        }
        self.assertTrue(validate_judgment(value))

    def test_reused_tool_id_links_only_until_next_use(self) -> None:
        rows = [
            {
                "episode_id": "ep",
                "global_episode_id": "global-ep",
                "global_pair_id": "pair",
                "session_id": "session",
                "session_tree_id": "tree",
                "message_uuid": f"m{index}",
                "message_content_id": f"c{index}",
                "source_file_sha256": "a" * 64,
                "source_line_number": index,
                "content_item_index": 0,
                "sequence_index": index,
                "content_type": content_type,
                "tool_use_id": "reused" if content_type == "tool_use" else None,
                "tool_result_for_id": (
                    "reused" if content_type == "tool_result" else None
                ),
            }
            for index, content_type in enumerate(
                ["tool_use", "tool_result", "tool_use", "tool_result"]
            )
        ]
        cycles = build_action_cycles(rows)
        self.assertEqual(len(cycles), 2)
        self.assertEqual(cycles[0]["tool_result_content_ids"], ["c1"])
        self.assertEqual(cycles[1]["tool_result_content_ids"], ["c3"])
        self.assertNotEqual(cycles[0]["action_cycle_id"], cycles[1]["action_cycle_id"])

    def test_real_flatten_requires_census_mapping(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(GateError):
                run_flatten(
                    Path("/var/lib/atobench/real-agent-sessions"),
                    Path(tmp) / "out",
                    allow_real_data=True,
                    dry_run=True,
                    new_version=False,
                )


if __name__ == "__main__":
    unittest.main()
