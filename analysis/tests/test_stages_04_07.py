from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from atobench_vr.alignment import run as run_alignment
from atobench_vr.claims import run as run_claims
from atobench_vr.common import load_jsonl, write_jsonl
from atobench_vr.facts import run as run_facts
from atobench_vr.packets import run as run_packets
from atobench_vr.sessions import parse_http_commands


class Stage0407Tests(unittest.TestCase):
    def test_curl_parser_is_conservative_and_hashes_body(self) -> None:
        body = '{"q":"fixture"}'
        requests = parse_http_commands(
            "Bash",
            {
                "command": (
                    "curl -X POST -H 'Authorization: Bearer <BEARER_X>' "
                    f"--data '{body}' http://fixture.local/api/check/42"
                )
            },
        )
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0]["method"], "POST")
        self.assertEqual(requests[0]["canonical_route"], "/api/check/{id}")
        self.assertEqual(
            requests[0]["request_body_sha256"],
            hashlib.sha256(body.encode()).hexdigest(),
        )

    def test_alignment_fact_claim_and_packet_pipeline(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            body = '{"q":"fixture"}'
            body_hash = hashlib.sha256(body.encode()).hexdigest()
            cycles_path = root / "action_cycles.jsonl"
            write_jsonl(
                cycles_path,
                [
                    {
                        "schema_version": "atobench.action_cycle.v1",
                        "episode_id": "private_episode_001",
                        "action_cycle_id": "private-session:t1",
                        "session_tree_id": "private-session",
                        "session_id": "private-session",
                        "agent_id": None,
                        "tool_name": "Bash",
                        "tool_use_id": "t1",
                        "timestamp": "2026-01-01T00:00:00Z",
                        "sequence_index": 1,
                        "preceding_recorded_message_ids": ["msg-1"],
                        "tool_result_message_ids": ["msg-2"],
                        "tool_input_redacted": {},
                        "tool_result_redacted": [],
                        "parsed_http_requests": [
                            {
                                "request_index": 0,
                                "method": "POST",
                                "url_redacted": "http://fixture.local/api/check/42",
                                "canonical_route": "/api/check/{id}",
                                "request_body_sha256": body_hash,
                                "has_authorization_header": True,
                                "parse_status": "parsed_curl",
                            }
                        ],
                    }
                ],
            )
            events_path = root / "http_events.jsonl"
            write_jsonl(
                events_path,
                [
                    {
                        "event_id": "private_episode_001:http:1",
                        "episode_id": "private_episode_001",
                        "method": "POST",
                        "canonical_route": "/api/check/{resource_id}",
                        "request_body_sha256": body_hash,
                        "timestamp": "2026-01-01T00:00:01Z",
                        "status_code": 200,
                    }
                ],
            )
            alignment_dir = root / "03_alignment"
            alignment_manifest = run_alignment(
                action_cycles_path=cycles_path,
                http_events_paths=[events_path],
                output_dir=alignment_dir,
                tolerance_seconds=3.0,
                allow_real_data=False,
                dry_run=False,
                new_version=False,
            )
            self.assertEqual(
                alignment_manifest["details"]["accepted_for_evidence_count"], 1
            )
            messages_path = root / "redacted_messages.jsonl"
            write_jsonl(
                messages_path,
                [
                    {
                        "episode_id": "private_episode_001",
                        "message_uuid": "msg-1",
                        "sequence_index": 0,
                        "content_type": "thinking",
                        "recorded_rationale_text": "I will check independently, then stop with bounded evidence.",
                        "visible_text": None,
                        "tool_name": None,
                    },
                    {
                        "episode_id": "private_episode_001",
                        "message_uuid": "msg-tool",
                        "sequence_index": 1,
                        "content_type": "tool_use",
                        "recorded_rationale_text": None,
                        "visible_text": None,
                        "tool_name": "Bash",
                        "tool_input_redacted": {
                            "command": (
                                "curl -H 'Authorization: Bearer raw.secret.tok' "
                                "http://private_campaign/api/check/42"
                            ),
                            "description": "probe private_pair_001",
                        },
                    },
                    {
                        "episode_id": "private_episode_001",
                        "message_uuid": "msg-2",
                        "sequence_index": 2,
                        "content_type": "tool_result",
                        "recorded_rationale_text": None,
                        "visible_text": "HTTP 200 for <EMAIL_ABCDEF12>",
                        "tool_name": None,
                    },
                ],
            )
            predicates_path = root / "predicate_decisions.jsonl"
            write_jsonl(
                predicates_path,
                [
                    {
                        "episode_id": "private_episode_001",
                        "predicate_id": "SQLI_TP_PRIMARY_EVIDENCE",
                        "value": True,
                        "measurement_status": "positive",
                        "importance": "critical",
                        "evidence_event_ids": ["private_episode_001:http:1"],
                        "predicate_version": "fixture.freeze.v1",
                    }
                ],
            )
            facts_dir = root / "04_facts"
            run_facts(
                predicate_decisions_path=predicates_path,
                alignment_path=alignment_dir / "claude_http_alignment.jsonl",
                redacted_messages_path=messages_path,
                output_dir=facts_dir,
                dry_run=False,
                new_version=False,
            )
            facts = load_jsonl(facts_dir / "episode_fact_registry.jsonl")
            endpoint = next(
                fact
                for fact in facts
                if fact["fact_type"] == "SQLI_TP_PRIMARY_EVIDENCE"
            )
            self.assertEqual(endpoint["measurement_status"], "positive")
            self.assertEqual(endpoint["confidence"], "exact")
            report = root / "final_report.md"
            report.write_text(
                "Verified POST /api/check/42 returned HTTP 200.\n"
                "Contact: analyst@example.test; password=fixture-secret\n"
            )
            report_manifest = root / "reports.jsonl"
            write_jsonl(
                report_manifest,
                [
                    {
                        "episode_id": "private_episode_001",
                        "report_path": str(report),
                    }
                ],
            )
            claims_dir = root / "report_atoms"
            run_claims(
                report_manifest_path=report_manifest,
                output_dir=claims_dir,
                dry_run=False,
                new_version=False,
            )
            atoms = load_jsonl(claims_dir / "report_claim_atoms.jsonl")
            self.assertGreaterEqual(len(atoms), 3)
            episode_manifest = root / "packet_episodes.jsonl"
            write_jsonl(
                episode_manifest,
                [
                    {
                        "episode_id": "private_episode_001",
                        "pair_id": "private_pair_001",
                        "campaign_id": "private_campaign",
                        "condition": "C1",
                        "model": "private-model",
                        "aou": "sqli",
                        "report_path": str(report),
                    }
                ],
            )
            packet_dir = root / "05_judge_packets"
            packet_manifest = run_packets(
                episode_manifest_path=episode_manifest,
                facts_path=facts_dir / "episode_fact_registry.jsonl",
                messages_path=messages_path,
                action_cycles_path=cycles_path,
                alignment_path=alignment_dir / "claude_http_alignment.jsonl",
                stop_contexts_path=facts_dir / "stop_contexts.jsonl",
                claim_atoms_path=claims_dir / "report_claim_atoms.jsonl",
                output_dir=packet_dir,
                config_path=ROOT / "config" / "packet_builder_config.json",
                rubrics_dir=ROOT / "config" / "rubrics",
                output_schema_path=ROOT / "config" / "output_schema.json",
                dry_run=False,
                new_version=False,
            )
            self.assertEqual(packet_manifest["details"]["packet_count"], 3)
            self.assertEqual(packet_manifest["details"]["leakage_hit_count"], 0)
            self.assertEqual(packet_manifest["details"]["secret_hit_count"], 0)
            stop_packet = next(
                row
                for row in json.loads(
                    (packet_dir / "packet_manifest.json").read_text()
                )["packets"]
                if row["dimension"] == "stop_decision"
            )
            stop_messages = json.loads(
                (
                    packet_dir
                    / stop_packet["relative_path"]
                    / "packet_evidence"
                    / "messages.json"
                ).read_text()
            )
            public_tool_use = next(
                row for row in stop_messages if row["content_type"] == "tool_use"
            )
            self.assertIn("curl", public_tool_use["tool_input"]["command"])
            self.assertIn("<BEARER_", public_tool_use["tool_input"]["command"])
            self.assertIn(
                "<PRIVATE_IDENTITY>",
                public_tool_use["tool_input"]["description"],
            )
            public_text = "\n".join(
                path.read_text(errors="replace")
                for path in packet_dir.rglob("*")
                if path.is_file() and "private" not in path.parts
            )
            for private_value in (
                "private_episode_001",
                "private_pair_001",
                "private_campaign",
                "private-model",
                "analyst@example.test",
                "fixture-secret",
            ):
                self.assertNotIn(private_value, public_text)
            report_packet = next(
                row
                for row in json.loads(
                    (packet_dir / "packet_manifest.json").read_text()
                )["packets"]
                if row["dimension"] == "report_grounding"
            )
            report_root = packet_dir / report_packet["relative_path"]
            self.assertFalse((report_root / "packet_evidence" / "messages.json").exists())
            self.assertTrue((report_root / "packet_evidence" / "final_report.md").is_file())


if __name__ == "__main__":
    unittest.main()
