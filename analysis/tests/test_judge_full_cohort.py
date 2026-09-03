from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from atobench_vr.judge_full_cohort import run


class JudgeFullCohortTests(unittest.TestCase):
    def fixture_packets(self, root: Path) -> tuple[Path, Path]:
        manifest_rows: list[dict] = []
        for index, packet_id in enumerate(("synthetic-packet-01", "synthetic-packet-02")):
            packet = root / "packets" / f"pkt_{index}"
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
                        "packet_id": packet_id,
                        "packet_schema_version": "fixture.v1",
                        "episode_pseudonym": f"EP-{index}",
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
            manifest_rows.append(
                {
                    "packet_id": packet_id,
                    "dimension": "verification_control",
                    "relative_path": str(packet.relative_to(root / "packets")),
                }
            )
        manifest = {
            "schema_version": "atobench.trajectory_packet_manifest.v1",
            "packet_count": len(manifest_rows),
            "dimension_counts": {"verification_control": len(manifest_rows)},
            "packets": manifest_rows,
        }
        manifest_path = root / "packet_manifest.json"
        manifest_path.write_text(json.dumps(manifest))
        return root / "packets", manifest_path

    def fake_executable(self) -> str:
        path = ROOT / "tests" / "fake_claude.py"
        if not path.is_file():
            self.fail("fake Claude executable is missing")
        return str(path)

    def test_serial_run_processes_all_packets(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            packets_root, manifest_path = self.fixture_packets(root)
            result = run(
                packet_manifest_path=manifest_path,
                packets_root=packets_root,
                output_root=root / "output",
                config_path=ROOT / "config" / "judge_runner_config.json",
                lock_path=ROOT / "config" / "analysis_spec.full_cohort.lock.yaml",
                agents_dir=ROOT / "config" / "subagent_specs",
                allow_judge_calls=True,
                start_index=1,
                limit=None,
                continue_on_error=True,
                workers=1,
                executable_override=self.fake_executable(),
            )
            self.assertEqual(result["stats"]["fresh_runs"], 2)
            self.assertEqual(result["stats"]["completed_packets"], 2)
            self.assertEqual(result["stats"]["failed_packets"], 0)

    def test_parallel_run_processes_all_packets(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            packets_root, manifest_path = self.fixture_packets(root)
            result = run(
                packet_manifest_path=manifest_path,
                packets_root=packets_root,
                output_root=root / "output",
                config_path=ROOT / "config" / "judge_runner_config.json",
                lock_path=ROOT / "config" / "analysis_spec.full_cohort.lock.yaml",
                agents_dir=ROOT / "config" / "subagent_specs",
                allow_judge_calls=True,
                start_index=1,
                limit=None,
                continue_on_error=True,
                workers=2,
                executable_override=self.fake_executable(),
            )
            self.assertEqual(result["stats"]["fresh_runs"], 2)
            self.assertEqual(result["stats"]["completed_packets"], 2)
            self.assertEqual(result["stats"]["failed_packets"], 0)
            self.assertEqual(result["workers"], 2)

    def test_parallel_run_with_progress(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            packets_root, manifest_path = self.fixture_packets(root)
            result = run(
                packet_manifest_path=manifest_path,
                packets_root=packets_root,
                output_root=root / "output",
                config_path=ROOT / "config" / "judge_runner_config.json",
                lock_path=ROOT / "config" / "analysis_spec.full_cohort.lock.yaml",
                agents_dir=ROOT / "config" / "subagent_specs",
                allow_judge_calls=True,
                start_index=1,
                limit=None,
                continue_on_error=True,
                workers=2,
                executable_override=self.fake_executable(),
                progress=True,
            )
            self.assertEqual(result["stats"]["fresh_runs"], 2)
            self.assertEqual(result["stats"]["completed_packets"], 2)
            self.assertEqual(result["stats"]["failed_packets"], 0)


if __name__ == "__main__":
    unittest.main()
