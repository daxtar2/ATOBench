from __future__ import annotations

import json
from pathlib import Path

import pytest

from atobench_vr.common import GateError
from atobench_vr.platform import (
    execute_campaign,
    freeze_campaign,
    init_campaign,
    list_platform,
    plan_campaign,
    prepare_execution,
    validate_campaign,
)


ROOT = Path(__file__).resolve().parents[1]
REGISTRY = ROOT / "config" / "platform" / "registry.json"
CAMPAIGN = "juice-shop-vr-stage20-smoke"
LOCK = ROOT / "tests" / "fixtures" / "platform" / "juice-shop-vr-stage20-smoke.freeze.json"


def _synthetic_platform(root: Path) -> Path:
    (root / "config" / "platform" / "targets").mkdir(parents=True)
    (root / "config" / "platform" / "aous").mkdir()
    (root / "config" / "platform" / "profiles").mkdir()
    (root / "config" / "platform" / "campaigns").mkdir()
    (root / "pyproject.toml").write_text("[project]\nname = 'synthetic'\nversion = '0'\n")
    (root / "runner.sh").write_text("#!/bin/sh\nexit 0\n")
    registry = root / "config" / "platform" / "registry.json"
    registry.write_text(
        json.dumps(
            {
                "schema_version": "atobench.platform_registry.v1",
                "targets": {"target-v1": "config/platform/targets/target.json"},
                "aous": {"aou-v1": "config/platform/aous/aou.json"},
                "profiles": {"profile-v1": "config/platform/profiles/profile.json"},
                "campaigns": {},
            }
        )
    )
    (root / "config" / "platform" / "targets" / "target.json").write_text(
        json.dumps({"id": "target-v1", "freeze_status": "frozen", "frozen_artifacts": []})
    )
    (root / "config" / "platform" / "aous" / "aou.json").write_text(
        json.dumps(
            {
                "id": "aou-v1",
                "target_id": "target-v1",
                "freeze_status": "frozen",
                "runtime_adapter": {"aou_key": "synthetic"},
                "frozen_artifacts": [],
            }
        )
    )
    (root / "config" / "platform" / "profiles" / "profile.json").write_text(
        json.dumps({"id": "profile-v1", "freeze_status": "frozen", "frozen_artifacts": []})
    )
    return registry


def test_platform_lists_frozen_reference_components() -> None:
    listing = list_platform(REGISTRY)
    assert listing["status"] == "PASS"
    assert listing["targets"] == ["juice-shop-protocol-v3"]
    assert len(listing["aous"]) == 3
    assert CAMPAIGN in listing["campaigns"]


def test_platform_fails_closed_without_external_artifact_authorization() -> None:
    with pytest.raises(GateError, match="allow-external-artifacts"):
        validate_campaign(
            registry_path=REGISTRY,
            campaign_id=CAMPAIGN,
            allow_external_artifacts=False,
        )


def test_platform_validates_frozen_legacy_bridge_and_plans_dry_run() -> None:
    validation = validate_campaign(
        registry_path=REGISTRY,
        campaign_id=CAMPAIGN,
        allow_external_artifacts=True,
    )
    assert validation["status"] == "PASS_FROZEN_BINDING"
    assert validation["aou_ids"] == [
        "sqli-evidence-closure-v3",
        "basket-scope-closure-v3_1",
        "jwt-hash-suppression-v3_1",
    ]
    plan = plan_campaign(
        registry_path=REGISTRY,
        campaign_id=CAMPAIGN,
        allow_external_artifacts=True,
    )
    assert plan["status"] == "DRY_RUN_PLAN_ONLY"
    assert plan["command"][0].endswith("atobench-cross-model")
    aou_offset = plan["command"].index("--aous") + 1
    assert plan["command"][aou_offset : aou_offset + 3] == ["sqli", "basket", "jwt"]
    assert [binding["legacy_aou_key"] for binding in plan["aou_bindings"]] == [
        "sqli",
        "basket",
        "jwt",
    ]


def test_platform_freeze_writes_replayable_plan_lock(tmp_path: Path) -> None:
    output = tmp_path / "platform.freeze.json"
    lock = freeze_campaign(
        registry_path=REGISTRY,
        campaign_id=CAMPAIGN,
        output_path=output,
        allow_external_artifacts=True,
        new_version=False,
    )
    assert lock["status"] == "FROZEN_PLAN_ONLY"
    assert lock["execution_performed"] is False
    assert json.loads(output.read_text())["lock_id"] == lock["lock_id"]
    with pytest.raises(GateError, match="refusing to overwrite"):
        freeze_campaign(
            registry_path=REGISTRY,
            campaign_id=CAMPAIGN,
            output_path=output,
            allow_external_artifacts=True,
            new_version=False,
        )


def test_execution_prepare_revalidates_lock_and_stays_metadata_only(
    tmp_path: Path,
) -> None:
    output = tmp_path / "handoff"
    handoff = prepare_execution(
        registry_path=REGISTRY,
        campaign_id=CAMPAIGN,
        lock_path=LOCK,
        output_dir=output,
        allow_external_artifacts=True,
        new_version=False,
    )
    assert handoff["status"] == "READY_FOR_EXPLICIT_EXECUTION"
    assert handoff["authorization"]["execution_performed"] is False
    assert handoff["safety"]["stdout_or_agent_content_captured"] is False
    assert (output / "execution_handoff.json").is_file()
    with pytest.raises(GateError, match="refusing to reuse"):
        prepare_execution(
            registry_path=REGISTRY,
            campaign_id=CAMPAIGN,
            lock_path=LOCK,
            output_dir=output,
            allow_external_artifacts=True,
            new_version=False,
        )


def test_execution_refuses_launch_without_explicit_authorization(tmp_path: Path) -> None:
    output = tmp_path / "handoff"
    prepare_execution(
        registry_path=REGISTRY,
        campaign_id=CAMPAIGN,
        lock_path=LOCK,
        output_dir=output,
        allow_external_artifacts=True,
        new_version=False,
    )
    with pytest.raises(GateError, match="allow-real-execution"):
        execute_campaign(
            registry_path=REGISTRY,
            campaign_id=CAMPAIGN,
            lock_path=LOCK,
            handoff_path=output / "execution_handoff.json",
            allow_external_artifacts=True,
            allow_real_execution=False,
            new_version=False,
        )


def test_execution_prepare_rejects_tampered_freeze_lock(tmp_path: Path) -> None:
    lock = json.loads(LOCK.read_text())
    lock["command"].append("--unexpected")
    tampered_lock = tmp_path / "tampered.freeze.json"
    tampered_lock.write_text(json.dumps(lock), encoding="utf-8")
    with pytest.raises(GateError, match="freeze lock mismatch"):
        prepare_execution(
            registry_path=REGISTRY,
            campaign_id=CAMPAIGN,
            lock_path=tampered_lock,
            output_dir=tmp_path / "handoff",
            allow_external_artifacts=True,
            new_version=False,
        )


def test_init_campaign_creates_unregistered_manifest_without_mutating_registry(
    tmp_path: Path,
) -> None:
    output = tmp_path / "candidate.json"
    original_registry = REGISTRY.read_bytes()
    result = init_campaign(
        registry_path=REGISTRY,
        campaign_id="juice-shop-init-test",
        target_id="juice-shop-protocol-v3",
        aou_ids=["sqli-evidence-closure-v3"],
        profile_id="verification-resilience-stage20",
        models=["gpt-5.5"],
        rounds=1,
        parallel_workers=1,
        start_target=True,
        continue_on_error=True,
        agent_timeout_multiplier=1.5,
        max_tool_calls=None,
        runner_adapter="atobench-cross-model-v1",
        runner_entrypoint="../runtime/atobench/scripts/atobench-cross-model",
        runner_module="atobench.experiment.cross_model_protocol",
        output_path=output,
        register=False,
        allow_external_artifacts=True,
        dry_run=False,
        new_version=False,
    )
    assert result["status"] == "CREATED_UNREGISTERED"
    assert result["registration"]["performed"] is False
    manifest = json.loads(output.read_text())
    assert manifest["id"] == "juice-shop-init-test"
    assert [item["artifact_id"] for item in manifest["frozen_artifacts"]] == [
        "legacy_runner",
        "legacy_runner_protocol",
    ]
    assert REGISTRY.read_bytes() == original_registry


def test_init_campaign_dry_run_has_no_filesystem_effect(tmp_path: Path) -> None:
    output = ROOT / "config" / "platform" / "campaigns" / "juice_shop_init_dry_run.json"
    assert not output.exists()
    result = init_campaign(
        registry_path=REGISTRY,
        campaign_id="juice-shop-init-dry-run",
        target_id="juice-shop-protocol-v3",
        aou_ids=["sqli-evidence-closure-v3"],
        profile_id="verification-resilience-stage20",
        models=["gpt-5.5"],
        rounds=1,
        parallel_workers=1,
        start_target=False,
        continue_on_error=True,
        agent_timeout_multiplier=None,
        max_tool_calls=60,
        runner_adapter="atobench-cross-model-v1",
        runner_entrypoint="../runtime/atobench/scripts/atobench-cross-model",
        runner_module="atobench.experiment.cross_model_protocol",
        output_path=output,
        register=True,
        allow_external_artifacts=True,
        dry_run=True,
        new_version=False,
    )
    assert result["status"] == "DRY_RUN_READY"
    assert result["registration"]["performed"] is False
    assert not output.exists()


def test_init_campaign_registers_a_synthetic_manifest_and_validates(tmp_path: Path) -> None:
    registry = _synthetic_platform(tmp_path / "platform")
    output = registry.parent / "campaigns" / "synthetic-run.json"
    result = init_campaign(
        registry_path=registry,
        campaign_id="synthetic-run",
        target_id="target-v1",
        aou_ids=["aou-v1"],
        profile_id="profile-v1",
        models=["synthetic-model"],
        rounds=2,
        parallel_workers=1,
        start_target=False,
        continue_on_error=True,
        agent_timeout_multiplier=None,
        max_tool_calls=60,
        runner_adapter="atobench-cross-model-v1",
        runner_entrypoint="runner.sh",
        runner_module=None,
        output_path=output,
        register=True,
        allow_external_artifacts=False,
        dry_run=False,
        new_version=False,
    )
    assert result["status"] == "CREATED_AND_REGISTERED"
    registry_value = json.loads(registry.read_text())
    assert registry_value["campaigns"] == {
        "synthetic-run": "config/platform/campaigns/synthetic-run.json"
    }
    validation = validate_campaign(
        registry_path=registry,
        campaign_id="synthetic-run",
        allow_external_artifacts=False,
    )
    assert validation["status"] == "PASS_FROZEN_BINDING"
    assert plan_campaign(
        registry_path=registry,
        campaign_id="synthetic-run",
        allow_external_artifacts=False,
    )["command"][-1] == "60"
