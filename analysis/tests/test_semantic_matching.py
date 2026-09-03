from __future__ import annotations

import json
import shutil
from pathlib import Path

from atobench_vr.common import read_json, write_json
from atobench_vr.semantic_output import (
    canonicalize_semantic_match,
    derive_verification_resolution_state,
    validate_semantic_match,
)
from atobench_vr.semantic_full import run as freeze_full_cohort
from atobench_vr.semantic_replay import run as replay_semantic_cohort
from atobench_vr.semantic_runner import run_cohort, run_packet
from atobench_vr.semantic_validation import run as run_validation

ROOT = Path(__file__).resolve().parents[1]


def _packet(root: Path) -> Path:
    packet_dir = root / "packet"
    evidence = packet_dir / "packet_evidence"
    evidence.mkdir(parents=True)
    packet = {
        "schema_version": "atobench.report_semantic_packet.v1",
        "semantic_packet_id": "smp_fixture",
        "episode_pseudonym": "EP_fixture",
        "aou": "basket",
        "source_report_packet_id": "pkt_source",
        "registered_primary_fact_id": "F1",
        "registered_primary_fact_type": "BASKET_T5_DIRECT_AUTHZ_EVIDENCE",
        "allowed_claim_atom_ids": ["A1"],
        "allowed_fact_ids": ["F1", "F2"],
        "identity_blinded": True,
        "paired_episode_not_present": True,
    }
    write_json(packet_dir / "packet.json", packet)
    write_json(
        packet_dir / "registered_finding_contract.json",
        {
            "schema_version": "atobench.registered_finding_contract.v1",
            "aou": "basket",
            "registered_finding": "fixture",
            "primary_fact_type": "BASKET_T5_DIRECT_AUTHZ_EVIDENCE",
            "closure_boundary": "fixture",
            "shared_rules": {},
        },
    )
    write_json(
        evidence / "report_claim_atoms.json",
        [{"atom_id": "A1", "value": "Cross-user basket access confirmed."}],
    )
    write_json(
        evidence / "facts.json",
        [
            {
                "fact_id": "F1",
                "fact_class": "verification",
                "fact_type": "BASKET_T5_DIRECT_AUTHZ_EVIDENCE",
                "measurement_status": "positive",
                "value": True,
            }
        ],
    )
    (evidence / "final_report.md").write_text(
        "Cross-user basket access confirmed.\n",
        encoding="utf-8",
    )
    (packet_dir / "rubric.md").write_text("Fixture rubric.\n", encoding="utf-8")
    (packet_dir / "output_schema.json").write_text(
        (ROOT / "config" / "report_semantic_match_output_schema.json").read_text(),
        encoding="utf-8",
    )
    return packet_dir


def _config(root: Path) -> Path:
    config = read_json(ROOT / "config" / "semantic_match_runner_config.json")
    config["claude_executable"] = str(ROOT / "tests" / "fake_claude.py")
    config["timeout_seconds"] = 10
    path = root / "config.json"
    write_json(path, config)
    return path


def _lock(root: Path) -> Path:
    path = root / "lock.json"
    write_json(
        path,
        {
            "freeze_status": "frozen",
            "semantic_match_calls_authorized": True,
        },
    )
    return path


def test_identity_and_invalid_pointers_are_canonicalized(tmp_path: Path) -> None:
    packet = read_json(_packet(tmp_path) / "packet.json")
    raw = {
        "schema_version": "atobench.report_semantic_match.v1",
        "semantic_packet_id": "wrong",
        "matcher_id": "wrong",
        "registered_finding_mentioned": True,
        "report_closure": True,
        "matched_claim_atom_ids": ["A1", "A999"],
        "claim_trace_support": "supported",
        "supporting_fact_ids": ["F1", "F999"],
        "contradicting_fact_ids": [],
        "confidence": "high",
        "insufficient_evidence": False,
        "reason_code": "fixture",
        "reason": "fixture",
    }
    canonical, warnings = canonicalize_semantic_match(raw, packet, "matcher_a")
    assert canonical["semantic_packet_id"] == "smp_fixture"
    assert canonical["matcher_id"] == "matcher_a"
    assert canonical["matched_claim_atom_ids"] == ["A1"]
    assert canonical["supporting_fact_ids"] == ["F1"]
    assert not validate_semantic_match(canonical, packet)
    assert any("dropped undeclared" in warning for warning in warnings)


def test_missing_semantic_label_downgrades_without_inference(tmp_path: Path) -> None:
    packet = read_json(_packet(tmp_path) / "packet.json")
    raw = {
        "semantic_packet_id": "smp_fixture",
        "matcher_id": "matcher_a",
        "registered_finding_mentioned": True,
        "report_closure": True,
        "matched_claim_atom_ids": ["A1"],
        "supporting_fact_ids": ["F1"],
        "contradicting_fact_ids": [],
        "insufficient_evidence": False,
    }
    canonical, _ = canonicalize_semantic_match(raw, packet, "matcher_a")
    assert canonical["insufficient_evidence"] is True
    assert canonical["report_closure"] is None
    assert canonical["claim_trace_support"] == "unavailable"


def test_unverifiable_closure_does_not_require_decisive_fact_pointer(
    tmp_path: Path,
) -> None:
    packet = read_json(_packet(tmp_path) / "packet.json")
    raw = {
        "schema_version": "atobench.report_semantic_match.v1",
        "semantic_packet_id": "smp_fixture",
        "matcher_id": "matcher_a",
        "registered_finding_mentioned": True,
        "report_closure": True,
        "matched_claim_atom_ids": ["A1"],
        "claim_trace_support": "unverifiable",
        "supporting_fact_ids": [],
        "contradicting_fact_ids": [],
        "confidence": "medium",
        "insufficient_evidence": False,
        "reason_code": "closed_without_decisive_trace",
        "reason": "The report closes the claim but trace support is unavailable.",
    }
    canonical, warnings = canonicalize_semantic_match(raw, packet, "matcher_a")
    assert canonical["report_closure"] is True
    assert canonical["claim_trace_support"] == "unverifiable"
    assert canonical["insufficient_evidence"] is False
    assert not validate_semantic_match(canonical, packet)
    assert not any("downgraded" in warning for warning in warnings)


def test_supported_closure_still_requires_supporting_fact_pointer(
    tmp_path: Path,
) -> None:
    packet = read_json(_packet(tmp_path) / "packet.json")
    raw = {
        "semantic_packet_id": "smp_fixture",
        "matcher_id": "matcher_a",
        "registered_finding_mentioned": True,
        "report_closure": True,
        "matched_claim_atom_ids": ["A1"],
        "claim_trace_support": "supported",
        "supporting_fact_ids": [],
        "contradicting_fact_ids": [],
        "confidence": "high",
        "insufficient_evidence": False,
    }
    canonical, _ = canonicalize_semantic_match(raw, packet, "matcher_a")
    assert canonical["insufficient_evidence"] is True
    assert canonical["claim_trace_support"] == "unavailable"


def test_positive_closure_allows_empty_best_effort_claim_atoms(
    tmp_path: Path,
) -> None:
    packet = read_json(_packet(tmp_path) / "packet.json")
    raw = {
        "schema_version": "atobench.report_semantic_match.v1",
        "semantic_packet_id": "smp_fixture",
        "matcher_id": "matcher_a",
        "registered_finding_mentioned": True,
        "report_closure": True,
        "matched_claim_atom_ids": [],
        "claim_trace_support": "supported",
        "supporting_fact_ids": ["F1"],
        "contradicting_fact_ids": [],
        "confidence": "high",
        "insufficient_evidence": False,
        "reason_code": "finding_text_not_atomized",
        "reason": "The report closes the finding and the primary fact supports it.",
    }
    canonical, warnings = canonicalize_semantic_match(raw, packet, "matcher_a")
    assert canonical["report_closure"] is True
    assert canonical["claim_trace_support"] == "supported"
    assert canonical["matched_claim_atom_ids"] == []
    assert not validate_semantic_match(canonical, packet)
    assert not any("claim atom" in warning for warning in warnings)


def test_state_derivation_preserves_unavailable() -> None:
    endpoint = {"measurement_status": "positive", "value": True}
    state, _ = derive_verification_resolution_state(endpoint, None)
    assert state == "state_unavailable"
    state, _ = derive_verification_resolution_state(
        endpoint,
        {
            "insufficient_evidence": False,
            "report_closure": True,
            "claim_trace_support": "supported",
        },
    )
    assert state == "grounded_verification"


def test_two_matcher_fake_smoke_and_resume_checkpoint(tmp_path: Path) -> None:
    packet_dir = _packet(tmp_path)
    output = tmp_path / "output"
    bundle = run_packet(
        packet_dir=packet_dir,
        output_dir=output,
        config_path=_config(tmp_path),
        lock_path=_lock(tmp_path),
        agents_dir=ROOT / "config" / "subagent_specs",
        allow_calls=True,
        synthetic_smoke=True,
        executable_override=str(ROOT / "tests" / "fake_claude.py"),
        new_version=False,
        dry_run=False,
    )
    assert bundle["adjudication_triggered"] is False
    assert bundle["invocation_attempt_count"] == 2
    assert bundle["final"]["claim_trace_support"] == "supported"
    assert not (output / "RUN_INCOMPLETE.json").exists()
    assert (output / "semantic_match_bundle.json").is_file()


def test_disagreement_triggers_only_one_adjudicator(tmp_path: Path) -> None:
    packet_dir = _packet(tmp_path)
    executable = tmp_path / "fake_claude_semantic_disagree.py"
    shutil.copy2(ROOT / "tests" / "fake_claude.py", executable)
    executable.chmod(0o755)
    config = read_json(ROOT / "config" / "semantic_match_runner_config.json")
    config["claude_executable"] = str(executable)
    config["timeout_seconds"] = 10
    config_path = tmp_path / "disagree_config.json"
    write_json(config_path, config)
    bundle = run_packet(
        packet_dir=packet_dir,
        output_dir=tmp_path / "disagree_output",
        config_path=config_path,
        lock_path=_lock(tmp_path),
        agents_dir=ROOT / "config" / "subagent_specs",
        allow_calls=True,
        synthetic_smoke=True,
        executable_override=str(executable),
        new_version=False,
        dry_run=False,
    )
    assert bundle["adjudication_triggered"] is True
    assert bundle["invocation_attempt_count"] == 3
    assert bundle["final"]["matcher_id"] == "adjudicated"


def test_malformed_semantic_object_gets_one_bounded_retry(tmp_path: Path) -> None:
    packet_dir = _packet(tmp_path)
    executable = tmp_path / "fake_claude_semantic_retry.py"
    shutil.copy2(ROOT / "tests" / "fake_claude_semantic_retry.py", executable)
    executable.chmod(0o755)
    config = read_json(ROOT / "config" / "semantic_match_runner_config.json")
    config["claude_executable"] = str(executable)
    config["timeout_seconds"] = 10
    config_path = tmp_path / "retry_config.json"
    write_json(config_path, config)
    output = tmp_path / "retry_output"
    bundle = run_packet(
        packet_dir=packet_dir,
        output_dir=output,
        config_path=config_path,
        lock_path=_lock(tmp_path),
        agents_dir=ROOT / "config" / "subagent_specs",
        allow_calls=True,
        synthetic_smoke=True,
        executable_override=str(executable),
        new_version=False,
        dry_run=False,
    )
    assert bundle["adjudication_triggered"] is False
    assert bundle["invocation_attempt_count"] == 3
    assert bundle["successful_invocation_count"] == 3
    assert bundle["final"]["claim_trace_support"] == "supported"
    assert (
        output / "raw" / "semantic_matcher_a_attempt_1.json"
    ).is_file()
    assert (
        output / "raw" / "semantic_matcher_a_attempt_2.json"
    ).is_file()


def test_timeout_is_bounded_and_degrades_to_unavailable(tmp_path: Path) -> None:
    packet_dir = _packet(tmp_path)
    executable = tmp_path / "fake_claude_timeout.py"
    shutil.copy2(ROOT / "tests" / "fake_claude_timeout.py", executable)
    executable.chmod(0o755)
    config = read_json(ROOT / "config" / "semantic_match_runner_config.json")
    config["claude_executable"] = str(executable)
    config["timeout_seconds"] = 1
    config_path = tmp_path / "timeout_config.json"
    write_json(config_path, config)
    bundle = run_packet(
        packet_dir=packet_dir,
        output_dir=tmp_path / "timeout_output",
        config_path=config_path,
        lock_path=_lock(tmp_path),
        agents_dir=ROOT / "config" / "subagent_specs",
        allow_calls=True,
        synthetic_smoke=True,
        executable_override=str(executable),
        new_version=False,
        dry_run=False,
    )
    assert bundle["invocation_attempt_count"] == 4
    assert bundle["final"]["insufficient_evidence"] is True
    assert bundle["final"]["claim_trace_support"] == "unavailable"


def test_semantic_validator_accepts_complete_bundle(tmp_path: Path) -> None:
    packet_dir = _packet(tmp_path)
    output = tmp_path / "match"
    run_packet(
        packet_dir=packet_dir,
        output_dir=output / "smp_fixture",
        config_path=_config(tmp_path),
        lock_path=_lock(tmp_path),
        agents_dir=ROOT / "config" / "subagent_specs",
        allow_calls=True,
        synthetic_smoke=True,
        executable_override=str(ROOT / "tests" / "fake_claude.py"),
        new_version=False,
        dry_run=False,
    )
    packets_root = tmp_path / "packets_root"
    target = packets_root / "packets" / "smp_fixture"
    shutil.copytree(packet_dir, target)
    manifest = tmp_path / "manifest.json"
    write_json(
        manifest,
        {
            "packets": [
                {
                    "semantic_packet_id": "smp_fixture",
                    "relative_path": "packets/smp_fixture",
                }
            ]
        },
    )
    report = run_validation(
        semantic_manifest_path=manifest,
        packets_root=packets_root,
        matches_root=output,
        output_dir=tmp_path / "validation",
        allowlist_path=None,
        expected_episodes=1,
        allow_real_data=False,
        dry_run=False,
        new_version=False,
    )
    assert report["status"] == "PASS"
    assert report["validated_episode_count"] == 1


def test_parallel_cohort_uses_spawn_safe_entrypoints(tmp_path: Path) -> None:
    template = _packet(tmp_path / "template")
    packets_root = tmp_path / "packets_root"
    manifest_rows = []
    for index in (1, 2):
        semantic_packet_id = f"smp_parallel_{index}"
        target = packets_root / "packets" / semantic_packet_id
        shutil.copytree(template, target)
        packet = read_json(target / "packet.json")
        packet["semantic_packet_id"] = semantic_packet_id
        packet["episode_pseudonym"] = f"EP_parallel_{index}"
        write_json(target / "packet.json", packet)
        manifest_rows.append(
            {
                "semantic_packet_id": semantic_packet_id,
                "relative_path": f"packets/{semantic_packet_id}",
            }
        )
    manifest = tmp_path / "parallel_manifest.json"
    write_json(manifest, {"packets": manifest_rows})
    report = run_cohort(
        semantic_manifest_path=manifest,
        packets_root=packets_root,
        output_root=tmp_path / "parallel_output",
        config_path=_config(tmp_path),
        lock_path=_lock(tmp_path),
        agents_dir=ROOT / "config" / "subagent_specs",
        allowlist_path=None,
        allow_calls=True,
        workers=2,
        continue_on_error=False,
        executable_override=str(ROOT / "tests" / "fake_claude.py"),
        allow_real_data=False,
        dry_run=False,
    )
    assert report["status"] == "complete"
    assert report["completed_count"] == 2
    assert report["failed_count"] == 0


def test_full_cohort_freeze_requires_exact_unique_manifest(tmp_path: Path) -> None:
    manifest = tmp_path / "manifest.json"
    write_json(
        manifest,
        {
            "packets": [
                {
                    "semantic_packet_id": "smp_1",
                    "episode_pseudonym": "EP_1",
                    "relative_path": "packets/smp_1",
                    "aou": "basket",
                },
                {
                    "semantic_packet_id": "smp_2",
                    "episode_pseudonym": "EP_2",
                    "relative_path": "packets/smp_2",
                    "aou": "jwt",
                },
            ]
        },
    )
    output = tmp_path / "freeze"
    summary = freeze_full_cohort(
        semantic_manifest_path=manifest,
        output_dir=output,
        expected_episodes=2,
        authorize_calls=True,
        allow_real_data=False,
        dry_run=False,
        new_version=False,
    )
    allowlist = read_json(output / "semantic_match_full_cohort_allowlist.json")
    assert summary["status"] == "frozen_authorized"
    assert summary["base_call_count"] == 4
    assert allowlist["authorized_for_calls"] is True
    assert allowlist["semantic_packet_ids"] == ["smp_1", "smp_2"]
    assert allowlist["hash_gate_enabled"] is False


def test_zero_call_replay_recovers_atomless_positive_closure(
    tmp_path: Path,
) -> None:
    packet_dir = _packet(tmp_path / "packet_source")
    packets_root = tmp_path / "packets_root"
    target = packets_root / "packets" / "smp_fixture"
    shutil.copytree(packet_dir, target)
    manifest = tmp_path / "manifest.json"
    write_json(
        manifest,
        {
            "packets": [
                {
                    "semantic_packet_id": "smp_fixture",
                    "relative_path": "packets/smp_fixture",
                }
            ]
        },
    )
    source_root = tmp_path / "source_matches"
    source_dir = source_root / "smp_fixture"
    (source_dir / "raw").mkdir(parents=True)
    raw = {
        "schema_version": "atobench.report_semantic_match.v1",
        "semantic_packet_id": "smp_fixture",
        "matcher_id": "semantic_matcher_a",
        "registered_finding_mentioned": True,
        "report_closure": True,
        "matched_claim_atom_ids": [],
        "claim_trace_support": "supported",
        "supporting_fact_ids": ["F1"],
        "contradicting_fact_ids": [],
        "confidence": "high",
        "insufficient_evidence": False,
        "reason_code": "atomless_closure",
        "reason": "The report closes the claim and F1 supports it.",
        "forbidden_identity_not_accessed": True,
        "outside_knowledge_not_used": True,
    }
    write_json(source_dir / "raw" / "semantic_matcher_a.json", raw)
    raw_b = dict(raw)
    raw_b["matcher_id"] = "semantic_matcher_b"
    write_json(source_dir / "raw" / "semantic_matcher_b.json", raw_b)
    write_json(
        source_dir / "semantic_match_bundle.json",
        {
            "final": {"insufficient_evidence": True},
            "invocation_attempt_count": 4,
        },
    )
    output = tmp_path / "replay"
    summary = replay_semantic_cohort(
        semantic_manifest_path=manifest,
        packets_root=packets_root,
        source_matches_root=source_root,
        output_root=output,
        allowlist_path=None,
        expected_episodes=1,
        allow_real_data=False,
        dry_run=False,
        new_version=False,
    )
    replayed = read_json(
        output / "smp_fixture" / "semantic_match_bundle.json"
    )
    assert summary["status"] == "PASS"
    assert summary["recovered_insufficient_count"] == 1
    assert summary["model_calls_made"] == 0
    assert replayed["final"]["report_closure"] is True
    assert replayed["final"]["claim_trace_support"] == "supported"
    assert replayed["final"]["matched_claim_atom_ids"] == []
