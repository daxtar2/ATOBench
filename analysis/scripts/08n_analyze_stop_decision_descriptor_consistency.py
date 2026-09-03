#!/usr/bin/env python3
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from pathlib import Path

from atobench_vr.common import load_jsonl, read_json, write_json, write_jsonl
from atobench_vr.stop_descriptors import derive_stop_descriptors


def _rate(rows: list[dict], key: str) -> float | None:
    values = [row[key] for row in rows if row.get(key) is not None]
    if not values:
        return None
    return sum(bool(value) for value in values) / len(values)


def _mean(values: list[float]) -> float | None:
    if not values:
        return None
    return sum(values) / len(values)


def _summary_block(rows: list[dict]) -> dict:
    score_diffs = [float(row["score_difference"]) for row in rows if row.get("score_difference") is not None]
    return {
        "packet_count": len(rows),
        "exact_score_agreement_rate": _rate(rows, "exact_score_agreement"),
        "within_one_point_rate": _rate(rows, "within_one_point"),
        "band_agreement_rate": _rate(rows, "band_agreement"),
        "mean_score_difference": _mean(score_diffs),
        "readiness_state_counts": dict(sorted(Counter(row["readiness_state"] for row in rows).items())),
        "stop_fit_counts": dict(sorted(Counter(row["stop_fit"] for row in rows).items())),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--episode-manifest", type=Path, required=True)
    parser.add_argument("--packet-root", type=Path, required=True)
    parser.add_argument("--official-summary", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    manifest_rows = load_jsonl(args.episode_manifest)
    manifest_by_episode = {row["episode_id"]: row for row in manifest_rows}
    manifest_by_pseudonym: dict[str, dict] = {}
    for row in manifest_rows:
        episode_id = row["episode_id"]
        # packet messages contain only the pseudonym; derive from packet tree
        # by matching episode_pseudonym inside packet.json.
        for packet_path in sorted((args.packet_root / "stop_decision").glob("*/packet.json")):
            packet = read_json(packet_path)
            if episode_id in packet.get("input_message_ids", []):
                continue
        # actual mapping is recovered by opening each stop_decision packet once below.

    official = read_json(args.official_summary)
    official_by_packet = {
        row["packet_id"]: row
        for row in official["results"]
        if row["dimension"] == "stop_decision"
    }

    # Build pseudonym -> manifest metadata by matching aou/condition/model order through the
    # pilot manifests would be brittle. Instead, recover using the stable packet episode_pseudonym
    # and the 07a manifest ordering within each (aou, condition) stratum.
    by_stratum_manifest: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for row in manifest_rows:
        by_stratum_manifest[(row["aou"], row["condition"])].append(row)
    for rows in by_stratum_manifest.values():
        rows.sort(key=lambda row: (row["campaign_id"], row["model"], row["episode_id"]))

    stop_packets: list[dict] = []
    for packet_dir in sorted((args.packet_root / "stop_decision").iterdir()):
        packet = read_json(packet_dir / "packet.json")
        facts = read_json(packet_dir / "packet_evidence" / "facts.json")
        stop_context = read_json(packet_dir / "packet_evidence" / "stop_context.json")
        descriptor = derive_stop_descriptors(packet, facts, stop_context)
        official_row = official_by_packet[packet["packet_id"]]
        stop_packets.append(
            {
                "packet_id": packet["packet_id"],
                "episode_pseudonym": packet["episode_pseudonym"],
                "aou": packet["aou"],
                "descriptor": descriptor,
                "official": official_row,
            }
        )

    # deterministic join of condition/model metadata:
    # in the pilot there are exactly two stop_decision packets per AOU, one C0 and one C1, duplicated
    # across two models each. Use the official summary's episode order together with manifest stratum counts.
    by_aou_packets: dict[str, list[dict]] = defaultdict(list)
    for row in stop_packets:
        by_aou_packets[row["aou"]].append(row)
    for rows in by_aou_packets.values():
        rows.sort(key=lambda row: row["episode_pseudonym"])

    enriched_rows: list[dict] = []
    used_manifest_ids: set[str] = set()
    for aou, packet_rows in sorted(by_aou_packets.items()):
        # manifest has 4 rows per AOU: two C0/C1 across models
        manifest_group = sorted(
            by_stratum_manifest[(aou, "C0")] + by_stratum_manifest[(aou, "C1")],
            key=lambda row: (row["condition"], row["campaign_id"], row["model"], row["episode_id"]),
        )
        packet_rows = sorted(packet_rows, key=lambda row: row["episode_pseudonym"])
        if len(packet_rows) != len(manifest_group):
            raise SystemExit(f"manifest/packet mismatch for {aou}: {len(packet_rows)} vs {len(manifest_group)}")
        for packet_row, meta in zip(packet_rows, manifest_group, strict=True):
            used_manifest_ids.add(meta["episode_id"])
            official_row = packet_row["official"]
            descriptor = packet_row["descriptor"]
            enriched_rows.append(
                {
                    "packet_id": packet_row["packet_id"],
                    "episode_pseudonym": packet_row["episode_pseudonym"],
                    "aou": aou,
                    "condition": meta["condition"],
                    "campaign_id": meta["campaign_id"],
                    "model": meta["model"],
                    "pair_id": meta["pair_id"],
                    "score_a": official_row["score_a"],
                    "score_b": official_row["score_b"],
                    "score_difference": official_row["score_difference"],
                    "exact_score_agreement": official_row["exact_score_agreement"],
                    "within_one_point": official_row["within_one_point"],
                    "band_agreement": official_row["band_agreement"],
                    "final_score": official_row["final"]["score"],
                    "final_band": official_row["final"]["band"],
                    "readiness_state": descriptor["derived_descriptors"]["readiness_state"],
                    "stop_fit": descriptor["derived_descriptors"]["stop_fit"],
                    "descriptor_confidence": descriptor["derived_descriptors"]["confidence"],
                    "primary_measurement_status": descriptor["primary_evidence"]["measurement_status"],
                    "explicit_conflict_count": descriptor["stop_context"]["explicit_conflict_count"],
                    "unavailable_verification_fact_count": descriptor["stop_context"]["unavailable_verification_fact_count"],
                }
            )

    args.output.mkdir(parents=True, exist_ok=True)
    write_jsonl(args.output / "stop_decision_descriptor_consistency_rows.jsonl", enriched_rows)

    by_condition = {
        condition: _summary_block([row for row in enriched_rows if row["condition"] == condition])
        for condition in sorted({row["condition"] for row in enriched_rows})
    }
    by_aou = {
        aou: _summary_block([row for row in enriched_rows if row["aou"] == aou])
        for aou in sorted({row["aou"] for row in enriched_rows})
    }
    by_readiness = {
        state: _summary_block([row for row in enriched_rows if row["readiness_state"] == state])
        for state in sorted({row["readiness_state"] for row in enriched_rows})
    }
    by_stop_fit = {
        state: _summary_block([row for row in enriched_rows if row["stop_fit"] == state])
        for state in sorted({row["stop_fit"] for row in enriched_rows})
    }

    highest_instability = sorted(
        enriched_rows,
        key=lambda row: (
            -(float(row["score_difference"]) if row["score_difference"] is not None else -1),
            row["episode_pseudonym"],
        ),
    )[:5]

    write_json(
        args.output / "summary.json",
        {
            "schema_version": "atobench.stop_decision_descriptor_consistency.v1",
            "packet_count": len(enriched_rows),
            "overall": _summary_block(enriched_rows),
            "by_condition": by_condition,
            "by_aou": by_aou,
            "by_readiness_state": by_readiness,
            "by_stop_fit": by_stop_fit,
            "highest_instability_examples": highest_instability,
            "note": (
                "Engineering pilot is stratified, not pair-complete; this artifact supports "
                "descriptor-versus-score consistency analysis, not paired C0/C1 transition estimation."
            ),
        },
    )


if __name__ == "__main__":
    main()
