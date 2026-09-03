from __future__ import annotations

import csv
import json
import tempfile
import unittest
from pathlib import Path

from atobench_vr.common import write_json, write_jsonl
from atobench_vr.final_qa import run
from atobench_vr.result_facts import run as run_result_facts
from tests.test_result_facts import _fixture as result_fact_fixture


def _upstream(tmp_path: Path) -> dict[str, Path]:
    paths = result_fact_fixture(tmp_path)
    facts = tmp_path / "facts"
    run_result_facts(
        statistics_root=paths["root"],
        audit_path=paths["audit"],
        output_dir=facts,
        allow_real_data=False,
        dry_run=False,
        new_version=False,
    )
    membership = tmp_path / "membership.jsonl"
    write_jsonl(
        membership,
        [
            {"pair_id": "pair-1"},
            {"pair_id": "pair-2"},
        ],
    )
    packet_leakage = tmp_path / "packet_leakage.json"
    packet_secret = tmp_path / "packet_secret.json"
    census = tmp_path / "census.json"
    alignment = tmp_path / "alignment.json"
    fact_validation = tmp_path / "fact_validation.json"
    judge_validation = tmp_path / "judge_validation.json"
    semantic_validation = tmp_path / "semantic_validation.json"
    write_json(packet_leakage, {"hits": []})
    write_json(packet_secret, {"hits": []})
    write_json(
        census,
        {"status": "complete", "details": {"blocking_conflict_count": 0}},
    )
    write_json(
        alignment,
        {"pending_alignment_count": 0, "accepted_alignment_row_count": 1},
    )
    write_json(
        fact_validation,
        {"status": "passed", "details": {"violation_count": 0}},
    )
    write_json(
        judge_validation,
        {
            "status": "PASS",
            "violation_count": 0,
            "validated_bundle_count": 1290,
        },
    )
    write_json(
        semantic_validation,
        {
            "status": "PASS",
            "violation_count": 0,
            "validated_episode_count": 430,
        },
    )
    validation_root = tmp_path / "validation"
    validation_root.mkdir()
    return {
        "facts": facts,
        "statistics": paths["root"],
        "audit": paths["audit"],
        "membership": membership,
        "packet_leakage": packet_leakage,
        "packet_secret": packet_secret,
        "census": census,
        "alignment": alignment,
        "fact_validation": fact_validation,
        "judge_validation": judge_validation,
        "semantic_validation": semantic_validation,
        "validation_root": validation_root,
        "output": tmp_path / "qa",
    }


def _run(paths: dict[str, Path], *, dry_run: bool = False) -> dict:
    return run(
        facts_root=paths["facts"],
        statistics_root=paths["statistics"],
        statistics_audit_path=paths["audit"],
        population_membership_path=paths["membership"],
        packet_leakage_path=paths["packet_leakage"],
        packet_secret_path=paths["packet_secret"],
        census_manifest_path=paths["census"],
        alignment_summary_path=paths["alignment"],
        fact_registry_validation_path=paths["fact_validation"],
        judge_validation_path=paths["judge_validation"],
        semantic_validation_path=paths["semantic_validation"],
        validation_root=paths["validation_root"],
        output_dir=paths["output"],
        author_approval_path=None,
        allow_real_data=False,
        dry_run=dry_run,
        new_version=False,
    )


class FinalQATests(unittest.TestCase):
    def test_internal_qa_passes_but_missing_holdout_and_approval_block_release(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = _upstream(Path(tmp))
            summary = _run(paths)
            self.assertTrue(summary["internal_qa_pass"])
            self.assertEqual(
                summary["status"], "PASS_INTERNAL_QA_FINAL_GATE_BLOCKED"
            )
            self.assertEqual(
                summary["blocking_gates"],
                ["agent_only_validation", "author_approval"],
            )
            final = json.loads(
                (paths["output"] / "final_gate_status.json").read_text()
            )
            self.assertFalse(final["paper_integration_authorized"])
            schema = json.loads(
                (paths["output"] / "schema_validation.json").read_text()
            )
            self.assertEqual(schema["violation_count"], 0)

    def test_tampered_fact_fails_internal_qa(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = _upstream(Path(tmp))
            fact_path = paths["facts"] / "results_facts.json"
            payload = json.loads(fact_path.read_text())
            fact = next(
                value
                for value in payload["facts"]
                if value["family"] == "capability_conditioned_retention"
                and value["scope"] == "aou"
            )
            fact["estimate"]["value"] = 0.123
            write_json(fact_path, payload)
            summary = _run(paths)
            self.assertEqual(summary["status"], "FAIL_INTERNAL_QA")
            schema = json.loads(
                (paths["output"] / "schema_validation.json").read_text()
            )
            self.assertEqual(schema["status"], "FAIL")
            self.assertGreater(schema["violation_count"], 0)

    def test_dry_run_creates_no_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            paths = _upstream(Path(tmp))
            plan = _run(paths, dry_run=True)
            self.assertEqual(plan["status"], "READY")
            self.assertFalse(paths["output"].exists())


if __name__ == "__main__":
    unittest.main()
