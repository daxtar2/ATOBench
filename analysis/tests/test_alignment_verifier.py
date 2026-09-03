from __future__ import annotations

import shutil
import stat
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from atobench_vr.alignment import run as run_alignment
from atobench_vr.alignment_reclassify import _reclassified_status
from atobench_vr.alignment_verifier import (
    _authorized_task_ids,
    run as run_verifiers,
)
from atobench_vr.common import (
    GateError,
    load_jsonl,
    write_json,
    write_jsonl,
)


class AlignmentVerifierTests(unittest.TestCase):
    def test_reclassification_separates_agreed_exclude_from_disagreement(self) -> None:
        agreed = {
            "accepted_for_evidence": False,
            "decisions": [
                {
                    "selection_status": "ambiguous",
                    "selected_candidate_id": None,
                },
                {
                    "selection_status": "ambiguous",
                    "selected_candidate_id": None,
                },
            ],
        }
        disagreed = {
            "accepted_for_evidence": False,
            "decisions": [
                {
                    "selection_status": "selected",
                    "selected_candidate_id": "H0001",
                },
                {
                    "selection_status": "ambiguous",
                    "selected_candidate_id": None,
                },
            ],
        }
        self.assertEqual(_reclassified_status(agreed), "agreed_exclude")
        self.assertEqual(_reclassified_status(disagreed), "disagreed_excluded")

    def test_authorized_allowlist_still_obeys_hard_call_cap(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "allowlist.json"
            task = {"alignment_task_id": "task-1", "candidates": []}
            write_json(
                path,
                {
                    "status": "frozen_authorized",
                    "authorized_for_calls": True,
                    "task_ids": ["task-1"],
                },
            )
            with self.assertRaisesRegex(GateError, "exceeding --max-calls=1"):
                _authorized_task_ids(
                    path,
                    tasks=[task],
                    max_calls=1,
                )

    def fixture(self, root: Path) -> tuple[Path, Path, Path]:
        cycles = root / "cycles.jsonl"
        events = root / "events.jsonl"
        write_jsonl(
            cycles,
            [
                {
                    "episode_id": "private-episode",
                    "action_cycle_id": "private-cycle",
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
                    "event_id": "private-event",
                    "episode_id": "private-episode",
                    "method": "GET",
                    "canonical_route": "/api/items/{id}",
                    "timestamp": "2026-01-01T00:00:01Z",
                    "status_code": 200,
                }
            ],
        )
        alignment_dir = root / "alignment"
        run_alignment(
            action_cycles_path=cycles,
            http_events_paths=[events],
            output_dir=alignment_dir,
            tolerance_seconds=3.0,
            allow_real_data=False,
            dry_run=False,
            new_version=False,
        )
        return cycles, events, alignment_dir / "claude_http_alignment.jsonl"

    def fake_claude(self) -> str:
        path = ROOT / "tests" / "fake_claude.py"
        if not path.is_file():
            self.fail("fake Claude executable is missing")
        return str(path)

    def test_two_verifiers_upgrade_timestamp_match(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cycles, events, alignment = self.fixture(root)
            before = load_jsonl(alignment)
            pending = [
                row
                for row in before
                if row["dual_verifier_status"] == "pending_dual_verification"
            ]
            self.assertEqual(len(pending), 1)
            self.assertFalse(pending[0]["accepted_for_evidence"])
            manifest = run_verifiers(
                alignment_path=alignment,
                action_cycles_path=cycles,
                http_events_paths=[events],
                output_dir=root / "validated",
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
                executable_override=self.fake_claude(),
                task_allowlist_path=None,
                max_calls=None,
                preflight_only=False,
                dry_run=False,
                new_version=False,
            )
            self.assertEqual(manifest["details"]["invocation_count"], 2)
            self.assertEqual(manifest["details"]["accepted_count"], 1)
            after = load_jsonl(
                root / "validated" / "claude_http_alignment.validated.jsonl"
            )
            accepted = [
                row
                for row in after
                if row.get("dual_verifier_status") == "agreed_accepted"
            ]
            self.assertEqual(len(accepted), 1)
            self.assertTrue(accepted[0]["accepted_for_evidence"])
            self.assertEqual(accepted[0]["accepted_event_ids"], ["private-event"])

    def test_real_verifier_calls_are_blocked_by_draft_lock(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cycles, events, alignment = self.fixture(root)
            with self.assertRaises(GateError):
                run_verifiers(
                    alignment_path=alignment,
                    action_cycles_path=cycles,
                    http_events_paths=[events],
                    output_dir=root / "validated",
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
                    allow_calls=True,
                    synthetic_smoke=False,
                    executable_override=self.fake_claude(),
                    task_allowlist_path=None,
                    max_calls=None,
                    preflight_only=False,
                    dry_run=False,
                    new_version=False,
                )

    def test_real_run_without_task_allowlist_is_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cycles, events, alignment = self.fixture(root)
            lock = root / "lock.json"
            write_json(
                lock,
                {
                    "freeze_status": "frozen",
                    "alignment_verifier_calls_authorized": True,
                    "hashes": {},
                },
            )
            with self.assertRaisesRegex(GateError, "task-allowlist"):
                run_verifiers(
                    alignment_path=alignment,
                    action_cycles_path=cycles,
                    http_events_paths=[events],
                    output_dir=root / "validated",
                    config_path=ROOT / "config" / "judge_runner_config.json",
                    lock_path=lock,
                    agent_spec_path=ROOT
                    / "config"
                    / "subagent_specs"
                    / "atobench-alignment-verifier.md",
                    output_schema_path=ROOT
                    / "config"
                    / "alignment_verifier_output_schema.json",
                    tolerance_seconds=3.0,
                    allow_real_data=False,
                    allow_calls=True,
                    synthetic_smoke=False,
                    executable_override=self.fake_claude(),
                    task_allowlist_path=None,
                    max_calls=None,
                    preflight_only=False,
                    dry_run=False,
                    new_version=False,
                )

    def test_verifier_disagreement_remains_excluded(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            cycles, events, alignment = self.fixture(root)
            disagree = root / "fake_claude_disagree.py"
            shutil.copy2(ROOT / "tests" / "fake_claude.py", disagree)
            disagree.chmod(disagree.stat().st_mode | stat.S_IXUSR)
            manifest = run_verifiers(
                alignment_path=alignment,
                action_cycles_path=cycles,
                http_events_paths=[events],
                output_dir=root / "validated",
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
                executable_override=str(disagree),
                task_allowlist_path=None,
                max_calls=None,
                preflight_only=False,
                dry_run=False,
                new_version=False,
            )
            self.assertEqual(manifest["details"]["accepted_count"], 0)
            self.assertEqual(manifest["details"]["disagreement_count"], 1)
            after = load_jsonl(
                root / "validated" / "claude_http_alignment.validated.jsonl"
            )
            excluded = [
                row
                for row in after
                if row.get("dual_verifier_status") == "disagreed_excluded"
            ]
            self.assertEqual(len(excluded), 1)
            self.assertFalse(excluded[0]["accepted_for_evidence"])


if __name__ == "__main__":
    unittest.main()
