from __future__ import annotations

import argparse
from collections import Counter
from pathlib import Path

from atobench_vr.common import read_json, write_json, write_jsonl
from atobench_vr.stop_descriptors import derive_stop_descriptors


def _packet_rows(packet_root: Path) -> list[dict]:
    rows: list[dict] = []
    for packet_dir in sorted(packet_root.iterdir()):
        if not packet_dir.is_dir():
            continue
        packet = read_json(packet_dir / "packet.json")
        facts = read_json(packet_dir / "packet_evidence" / "facts.json")
        stop_context = read_json(packet_dir / "packet_evidence" / "stop_context.json")
        row = derive_stop_descriptors(packet, facts, stop_context)
        row["packet_dir"] = str(packet_dir.resolve())
        rows.append(row)
    return rows


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--packet-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    rows = _packet_rows(args.packet_root)
    args.output.mkdir(parents=True, exist_ok=True)
    write_jsonl(args.output / "stop_decision_descriptors.jsonl", rows)

    readiness_counts = Counter(row["derived_descriptors"]["readiness_state"] for row in rows)
    stop_fit_counts = Counter(row["derived_descriptors"]["stop_fit"] for row in rows)
    by_aou = Counter(row["aou"] for row in rows)

    write_json(
        args.output / "summary.json",
        {
            "schema_version": "atobench.stop_decision_descriptor_summary.v1",
            "packet_count": len(rows),
            "aou_counts": dict(sorted(by_aou.items())),
            "readiness_state_counts": dict(sorted(readiness_counts.items())),
            "stop_fit_counts": dict(sorted(stop_fit_counts.items())),
        },
    )


if __name__ == "__main__":
    main()
