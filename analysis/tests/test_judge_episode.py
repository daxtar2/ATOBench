from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from atobench_vr.common import GateError
from atobench_vr.judge_episode import DIMENSIONS, _select_episode_rows


class JudgeEpisodeTests(unittest.TestCase):
    def test_selects_exactly_one_packet_for_each_dimension(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "plan.jsonl"
            rows = [
                {
                    "episode_pseudonym": "EP-X",
                    "dimension": dimension,
                    "packet_id": f"pkt-{index}",
                    "relative_path": f"{dimension}/pkt-{index}",
                    "minimum_calls": 4,
                    "maximum_calls": 5,
                }
                for index, dimension in enumerate(DIMENSIONS, 1)
            ]
            path.write_text(
                "".join(json.dumps(row) + "\n" for row in rows),
                encoding="utf-8",
            )
            selected = _select_episode_rows(path, "EP-X")
            self.assertEqual(
                [row["dimension"] for row in selected],
                list(DIMENSIONS),
            )

    def test_rejects_incomplete_dimension_set(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "plan.jsonl"
            path.write_text(
                json.dumps(
                    {
                        "episode_pseudonym": "EP-X",
                        "dimension": "verification_control",
                        "packet_id": "pkt-1",
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            with self.assertRaises(GateError):
                _select_episode_rows(path, "EP-X")

    def test_packet_selection_needs_no_duplicate_yaml_allowlist(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "plan.jsonl"
            rows = [
                {
                    "episode_pseudonym": "EP-Y",
                    "dimension": dimension,
                    "packet_id": f"packet-{dimension}",
                    "relative_path": f"{dimension}/packet-{dimension}",
                    "minimum_calls": 4,
                    "maximum_calls": 5,
                }
                for dimension in DIMENSIONS
            ]
            path.write_text(
                "".join(json.dumps(row) + "\n" for row in rows),
                encoding="utf-8",
            )
            selected = _select_episode_rows(path, "EP-Y")
            self.assertEqual(len(selected), 3)


if __name__ == "__main__":
    unittest.main()
