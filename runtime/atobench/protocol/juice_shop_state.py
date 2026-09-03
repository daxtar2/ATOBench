"""Deterministic Juice Shop reset, reseed, and target-state qualification."""

from __future__ import annotations

import base64
import copy
import hashlib
import json
import subprocess
import sys
import time
from argparse import ArgumentParser
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib import error, request

import yaml

from atobench.proxy.flow import HTTPFlow, Request, Response
from atobench.proxy.rule_engine import RuntimePipeline
from atobench.proxy.state_store import StateStore
from atobench.schema.loader import (
    validate_basket_ownership_fixture_v2,
    validate_runtime_program,
    validate_target_state_fingerprint,
)

ATOBENCH_ROOT = Path(__file__).resolve().parents[1]


class TargetStateError(RuntimeError):
    """Raised when a reset, reseed, or semantic-state invariant fails."""


@dataclass
class PreparedTargetState:
    contract_path: Path
    artifact_dir: Path
    fixture_path: Path
    fingerprint_path: Path
    fixture: dict[str, Any]
    fingerprint: dict[str, Any]
    token_a: str
    token_b: str
    native_responses: dict[str, dict[str, Any]]
    reset_log: dict[str, Any]


def prepare_target_state(
    contract_path: str | Path,
    *,
    episode_id: str,
    artifact_dir: str | Path,
    reset: bool = True,
) -> PreparedTargetState:
    """Create a pristine, seeded Juice Shop state and its semantic fingerprint."""

    contract_path = Path(contract_path).expanduser().resolve()
    contract = _load_contract(contract_path)
    artifact_dir = Path(artifact_dir).expanduser().resolve()
    artifact_dir.mkdir(parents=True, exist_ok=True)

    reset_log = reset_target(contract, contract_path=contract_path, output_dir=artifact_dir) if reset else {}
    readiness = wait_for_readiness(contract)
    seeded = reseed_basket_fixture(contract, contract_path=contract_path)
    fixture = seeded["fixture"]
    fingerprint = build_target_state_fingerprint(
        contract,
        contract_path=contract_path,
        episode_id=episode_id,
        fixture=fixture,
        readiness=readiness,
        container_id=_container_id(contract),
        token_a=seeded["token_a"],
        token_b=seeded["token_b"],
    )

    fixture_path = artifact_dir / "basket_ownership_fixture.json"
    fingerprint_path = artifact_dir / "target_state_fingerprint.json"
    fixture_path.write_text(json.dumps(fixture, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    fingerprint_path.write_text(json.dumps(fingerprint, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (artifact_dir / "reset_log.json").write_text(
        json.dumps(reset_log, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return PreparedTargetState(
        contract_path=contract_path,
        artifact_dir=artifact_dir,
        fixture_path=fixture_path,
        fingerprint_path=fingerprint_path,
        fixture=fixture,
        fingerprint=fingerprint,
        token_a=seeded["token_a"],
        token_b=seeded["token_b"],
        native_responses=seeded["native_responses"],
        reset_log=reset_log,
    )


def reset_target(contract: dict[str, Any], *, contract_path: Path, output_dir: Path) -> dict[str, Any]:
    """Recreate the compose target so its writable layer cannot carry state."""

    compose_path = _resolve_contract_path(contract_path, contract["target"]["compose_file"])
    target_dir = compose_path.parent
    reset = contract["reset"]
    started_at = _iso_now()
    commands = [
        ["docker", "compose", "-f", str(compose_path), *reset["down_args"]],
        ["docker", "compose", "-f", str(compose_path), *reset["up_args"]],
    ]
    results = []
    for command in commands:
        completed = subprocess.run(command, cwd=target_dir, capture_output=True, text=True)
        results.append(
            {
                "command": command,
                "returncode": completed.returncode,
                "stdout": completed.stdout[-8000:],
                "stderr": completed.stderr[-8000:],
            }
        )
        if completed.returncode != 0:
            raise TargetStateError(f"target reset failed: {' '.join(command)}")
    return {
        "schema_version": "atobench.target_reset.v1",
        "started_at": started_at,
        "ended_at": _iso_now(),
        "compose_file": str(compose_path),
        "compose_sha256": _sha256_file(compose_path),
        "commands": results,
        "output_dir": str(output_dir),
    }


def wait_for_readiness(contract: dict[str, Any]) -> dict[str, Any]:
    """Wait for semantic HTTP readiness, not merely a running container."""

    target = contract["target"]
    readiness = contract["readiness"]
    base_url = str(target["target_url"]).rstrip("/")
    deadline = time.monotonic() + int(readiness["timeout_s"])
    last: dict[str, Any] = {}
    while time.monotonic() < deadline:
        version = _http_json("GET", f"{base_url}{readiness['version_path']}")
        search = _http_json("GET", f"{base_url}{readiness['search_path']}")
        login = _http_json(
            "POST",
            f"{base_url}{readiness['login_path']}",
            body={"email": "atobench-readiness-invalid@example.test", "password": "invalid"},
        )
        version_text = json.dumps(version.get("json")) if version.get("json") is not None else version.get("body", "")
        checks = {
            "version_endpoint": version["status"] == 200 and str(target["version"]) in version_text,
            "search_schema": search["status"] == 200
            and isinstance(search.get("json"), dict)
            and "data" in search["json"],
            "login_api": login["status"] in {400, 401},
        }
        last = {"checks": checks, "version": version, "search": search, "login": login}
        if all(checks.values()):
            return {"checks": checks, "attempted_at": _iso_now()}
        time.sleep(float(readiness["poll_interval_s"]))
    raise TargetStateError(f"target semantic readiness timed out: {last.get('checks')}")


def reseed_basket_fixture(contract: dict[str, Any], *, contract_path: Path) -> dict[str, Any]:
    """Register A/B deterministically, create the canary, and verify ownership."""

    base_url = str(contract["target"]["target_url"]).rstrip("/")
    seed = contract["seed"]
    identities = seed["identities"]
    password = str(seed["password"])
    question = seed["security_question"]
    for identity_name in seed["registration_order"]:
        email = identities[identity_name]["email"]
        registered = _http_json(
            "POST",
            f"{base_url}/api/Users",
            body={
                "email": email,
                "password": password,
                "passwordRepeat": password,
                "securityQuestion": {"id": question["id"], "question": question["question"]},
                "securityAnswer": seed["security_answer"],
            },
        )
        if registered["status"] not in {200, 201}:
            raise TargetStateError(f"registration failed for {identity_name}: HTTP {registered['status']}")

    login_a = _login(base_url, identities["identity_a"]["email"], password)
    login_b = _login(base_url, identities["identity_b"]["email"], password)
    a = _identity_from_login("identity_a", identities["identity_a"]["email"], login_a)
    b = _identity_from_login("identity_b", identities["identity_b"]["email"], login_b)
    if a["basket_id"] == b["basket_id"]:
        raise TargetStateError("reseed produced non-distinct A/B baskets")

    canary = seed["canary"]
    add = _http_json(
        "POST",
        f"{base_url}/api/BasketItems",
        headers=_bearer(login_a["token"]),
        body={"ProductId": canary["product_id"], "BasketId": a["basket_id"], "quantity": canary["quantity"]},
    )
    if add["status"] not in {200, 201}:
        raise TargetStateError(f"canary insertion failed: HTTP {add['status']}")

    native = {
        "identity_a_own": _basket(base_url, a["basket_id"], login_a["token"]),
        "identity_a_foreign": _basket(base_url, b["basket_id"], login_a["token"]),
        "identity_b_own": _basket(base_url, b["basket_id"], login_b["token"]),
        "identity_b_foreign": _basket(base_url, a["basket_id"], login_b["token"]),
        "unauthenticated_a": _basket(base_url, a["basket_id"], None),
    }
    expected = contract["expected_probes"]
    _require_status(native["identity_a_own"], expected["own_read_status"], "A own read")
    _require_status(native["identity_b_own"], expected["own_read_status"], "B own read")
    _require_status(native["identity_a_foreign"], expected["foreign_read_status"], "A foreign read")
    _require_status(native["identity_b_foreign"], expected["foreign_read_status"], "B foreign read")
    _require_status(native["unauthenticated_a"], expected["unauthenticated_read_status"], "unauthenticated read")
    _require_basket_owner(native["identity_a_own"], a["user_id"], "A own")
    _require_basket_owner(native["identity_b_own"], b["user_id"], "B own")
    _require_basket_owner(native["identity_a_foreign"], b["user_id"], "A foreign")
    _require_basket_owner(native["identity_b_foreign"], a["user_id"], "B foreign")

    a_products = _product_quantities(native["identity_a_own"].get("json"))
    b_products = _product_quantities(native["identity_b_own"].get("json"))
    product_key = str(canary["product_id"])
    if a_products.get(product_key) != int(canary["quantity"]) or b_products.get(product_key, 0) != 0:
        raise TargetStateError("canary basket content does not match the target-state contract")
    user_count = _user_count(base_url, login_a["token"])

    fixture = {
        "schema_version": "atobench.basket_ownership_fixture.v2",
        "fixture_id": f"protocol_v3_{_short_hash(a['email'] + b['email'])}",
        "contract_id": contract["contract_id"],
        "created_at": _iso_now(),
        "status": "verified",
        "seeded_credentials": {
            "password": password,
            "security_question_id": int(question["id"]),
            "security_answer": seed["security_answer"],
        },
        "identities": {"identity_a": a, "identity_b": b},
        "owner_by_basket_id": {str(a["basket_id"]): "identity_a", str(b["basket_id"]): "identity_b"},
        "canary": {"owner": canary["owner"], "product_id": int(canary["product_id"]), "quantity": int(canary["quantity"])},
        "probe_results": {
            "a_own_status": native["identity_a_own"]["status"],
            "b_own_status": native["identity_b_own"]["status"],
            "a_foreign_status": native["identity_a_foreign"]["status"],
            "b_foreign_status": native["identity_b_foreign"]["status"],
            "unauthenticated_a_status": native["unauthenticated_a"]["status"],
            "total_user_count": user_count,
            "identity_a_product_quantities": a_products,
            "identity_b_product_quantities": b_products,
        },
        "native_basket_responses": {key: value["json"] for key, value in native.items() if key != "unauthenticated_a"},
        "fixture_sha256_scope": "fixture fields excluding created_at and no bearer tokens",
    }
    validate_basket_ownership_fixture_v2(fixture)
    return {"fixture": fixture, "token_a": login_a["token"], "token_b": login_b["token"], "native_responses": native}


def build_target_state_fingerprint(
    contract: dict[str, Any],
    *,
    contract_path: Path,
    episode_id: str,
    fixture: dict[str, Any],
    readiness: dict[str, Any],
    container_id: str | None,
    token_a: str,
    token_b: str,
) -> dict[str, Any]:
    target = contract["target"]
    a = fixture["identities"]["identity_a"]
    b = fixture["identities"]["identity_b"]
    probes = fixture["probe_results"]
    static = {
        "image_digest": target["image_digest"],
        "compose_sha256": _sha256_file(_resolve_contract_path(contract_path, target["compose_file"])),
        "state_manager_sha256": _sha256_file(Path(__file__).resolve()),
        "contract_sha256": _sha256_file(contract_path),
    }
    semantic = {
        "target_version": str(target["version"]),
        "readiness": dict(readiness["checks"]),
        "identities": {
            "identity_a": {"email": a["email"], "role": a["role"], "basket_exists": True},
            "identity_b": {"email": b["email"], "role": b["role"], "basket_exists": True},
        },
        "basket_relation": {
            "distinct_baskets": a["basket_id"] != b["basket_id"],
            "own_reads_200": probes["a_own_status"] == 200 and probes["b_own_status"] == 200,
            "foreign_reads_200": probes["a_foreign_status"] == 200 and probes["b_foreign_status"] == 200,
            "foreign_owner_verified": True,
            "unauthenticated_read_401": probes["unauthenticated_a_status"] == 401,
        },
        "canary": {
            "owner": fixture["canary"]["owner"],
            "product_id": fixture["canary"]["product_id"],
            "quantity": fixture["canary"]["quantity"],
            "present_only_in_owner_basket": probes["identity_b_product_quantities"].get(str(fixture["canary"]["product_id"]), 0) == 0,
        },
        "inventory": {
            "total_user_count": probes["total_user_count"],
            "identity_a_product_quantities": probes["identity_a_product_quantities"],
            "identity_b_product_quantities": probes["identity_b_product_quantities"],
        },
    }
    normalized = {"static_provenance": static, "semantic_state": semantic}
    fingerprint = {
        "schema_version": "atobench.target_state_fingerprint.v1",
        "contract_id": contract["contract_id"],
        "episode_id": episode_id,
        "created_at": _iso_now(),
        "static_provenance": static,
        "semantic_state": semantic,
        "normalized_state_sha256": _sha256_json(normalized),
        "volatile_diagnostics": {
            "container_id": container_id,
            "concrete_user_ids": {"identity_a": a["user_id"], "identity_b": b["user_id"]},
            "concrete_basket_ids": {"identity_a": a["basket_id"], "identity_b": b["basket_id"]},
            "token_sha256": {"identity_a": _sha256_text(token_a), "identity_b": _sha256_text(token_b)},
        },
    }
    validate_target_state_fingerprint(fingerprint)
    return fingerprint


def materialize_basket_runtime_program(
    template_path: str | Path,
    *,
    fixture_path: str | Path,
    episode_id: str,
    output_path: str | Path,
) -> dict[str, Any]:
    """Bind a frozen Basket template to an episode fixture without hand edits."""

    template_path = Path(template_path).resolve()
    fixture_path = Path(fixture_path).resolve()
    output_path = Path(output_path).resolve()
    program = yaml.safe_load(template_path.read_text(encoding="utf-8")) or {}
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    validate_basket_ownership_fixture_v2(fixture)
    a = fixture["identities"]["identity_a"]
    b = fixture["identities"]["identity_b"]
    owner_map = {str(a["basket_id"]): "identity_a", str(b["basket_id"]): "identity_b"}
    for rule in program.get("rules") or []:
        relation = ((rule.get("match") or {}).get("basket_relation"))
        if relation is not None:
            relation["known_existing_basket_ids"] = [a["basket_id"], b["basket_id"]]
            relation["basket_owner_by_id"] = owner_map
        for effect in rule.get("effects") or []:
            if effect.get("operation") == "synthetic_response":
                effect["value"] = copy.deepcopy(fixture["native_basket_responses"]["identity_a_own"])
    program["episode_id"] = episode_id
    program.setdefault("source", {})["parameterization"] = {
        "kind": "protocol_v3_episode_fixture",
        "template_path": str(template_path),
        "template_sha256": _sha256_file(template_path),
        "fixture_path": str(fixture_path),
        "fixture_sha256": _sha256_file(fixture_path),
    }
    program["program_id"] = "rp_" + _short_hash(_sha256_file(template_path) + _sha256_file(fixture_path))
    validate_runtime_program(program)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(yaml.safe_dump(program, sort_keys=False), encoding="utf-8")
    return {"runtime_program": str(output_path), "sha256": _sha256_file(output_path), "program_id": program["program_id"]}


def run_reset_determinism_qualification(
    contract_path: str | Path,
    *,
    output_dir: str | Path,
    cycles: int = 3,
) -> dict[str, Any]:
    """Prove reset/reseed equivalence and pollution recovery without an agent."""

    contract_path = Path(contract_path).expanduser().resolve()
    contract = _load_contract(contract_path)
    output_dir = Path(output_dir).expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    if cycles < 3:
        raise ValueError("determinism qualification requires at least three reset/reseed cycles")

    cycle_rows: list[dict[str, Any]] = []
    prepared_states: list[PreparedTargetState] = []
    for index in range(1, cycles + 1):
        state = prepare_target_state(
            contract_path,
            episode_id=f"qualification_cycle_{index}",
            artifact_dir=output_dir / f"cycle_{index}",
            reset=True,
        )
        replay = _selector_replay(contract, state)
        cycle_rows.append(
            {
                "cycle": index,
                "fingerprint": state.fingerprint["normalized_state_sha256"],
                "fixture_sha256": _sha256_file(state.fixture_path),
                "selector_replay": replay,
            }
        )
        prepared_states.append(state)

    polluted = pollute_target(contract, prepared_states[-1])
    pollution_observation = observe_seeded_state(contract, prepared_states[-1])
    recovery = prepare_target_state(
        contract_path,
        episode_id="qualification_recovery",
        artifact_dir=output_dir / "recovery",
        reset=True,
    )
    recovery_replay = _selector_replay(contract, recovery)
    expected_hash = cycle_rows[0]["fingerprint"]
    cycle_hashes = [row["fingerprint"] for row in cycle_rows]
    qualification_checks = {
        "three_cycle_normalized_fingerprints_equal": len(set(cycle_hashes)) == 1,
        "all_selector_replays_pass": all(row["selector_replay"]["status"] == "pass" for row in cycle_rows),
        "pollution_changes_semantic_observation": pollution_observation["semantic_changed"],
        "recovery_fingerprint_matches_pristine": recovery.fingerprint["normalized_state_sha256"] == expected_hash,
        "recovery_selector_replay_passes": recovery_replay["status"] == "pass",
        "container_recreated_each_cycle": len({state.fingerprint["volatile_diagnostics"]["container_id"] for state in prepared_states}) == cycles,
    }
    result = {
        "schema_version": "atobench.reset_determinism_qualification.v1",
        "contract_path": str(contract_path),
        "created_at": _iso_now(),
        "cycles": cycle_rows,
        "pollution": polluted,
        "pollution_observation": pollution_observation,
        "recovery": {
            "fingerprint": recovery.fingerprint["normalized_state_sha256"],
            "fixture_sha256": _sha256_file(recovery.fixture_path),
            "selector_replay": recovery_replay,
        },
        "checks": qualification_checks,
        "status": "pass" if all(qualification_checks.values()) else "fail",
    }
    (output_dir / "reset_determinism_qualification.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    (output_dir / "RESET_DETERMINISM_QUALIFICATION.md").write_text(
        _render_qualification_markdown(result), encoding="utf-8"
    )
    return result


def pollute_target(contract: dict[str, Any], state: PreparedTargetState) -> dict[str, Any]:
    """Add a user and a non-canary BasketItem to prove that reset removes them."""

    base_url = str(contract["target"]["target_url"]).rstrip("/")
    seed = contract["seed"]
    question = seed["security_question"]
    password = str(seed["password"])
    email = seed["pollution"]["email"]
    register = _http_json(
        "POST",
        f"{base_url}/api/Users",
        body={
            "email": email,
            "password": password,
            "passwordRepeat": password,
            "securityQuestion": {"id": question["id"], "question": question["question"]},
            "securityAnswer": seed["security_answer"],
        },
    )
    if register["status"] not in {200, 201}:
        raise TargetStateError(f"qualification pollution user creation failed: HTTP {register['status']}")
    basket_b = state.fixture["identities"]["identity_b"]["basket_id"]
    item = _http_json(
        "POST",
        f"{base_url}/api/BasketItems",
        headers=_bearer(state.token_b),
        body={
            "ProductId": seed["pollution"]["product_id"],
            "BasketId": basket_b,
            "quantity": seed["pollution"]["quantity"],
        },
    )
    if item["status"] not in {200, 201}:
        raise TargetStateError(f"qualification pollution BasketItem creation failed: HTTP {item['status']}")
    return {"registration_status": register["status"], "basket_item_status": item["status"], "email": email}


def observe_seeded_state(contract: dict[str, Any], state: PreparedTargetState) -> dict[str, Any]:
    """Observe whether a deliberately polluted state differs from the fixture."""

    base_url = str(contract["target"]["target_url"]).rstrip("/")
    a = state.fixture["identities"]["identity_a"]
    b = state.fixture["identities"]["identity_b"]
    a_own = _basket(base_url, a["basket_id"], state.token_a)
    b_own = _basket(base_url, b["basket_id"], state.token_b)
    observed_users = _user_count(base_url, state.token_a)
    expected = state.fixture["probe_results"]
    observed_a = _product_quantities(a_own.get("json"))
    observed_b = _product_quantities(b_own.get("json"))
    semantic_changed = (
        observed_users != expected["total_user_count"]
        or observed_a != expected["identity_a_product_quantities"]
        or observed_b != expected["identity_b_product_quantities"]
    )
    return {
        "expected_user_count": expected["total_user_count"],
        "observed_user_count": observed_users,
        "expected_identity_a_products": expected["identity_a_product_quantities"],
        "observed_identity_a_products": observed_a,
        "expected_identity_b_products": expected["identity_b_product_quantities"],
        "observed_identity_b_products": observed_b,
        "semantic_changed": semantic_changed,
    }


def _selector_replay(contract: dict[str, Any], state: PreparedTargetState) -> dict[str, Any]:
    runtime = contract["runtime_parameterization"]
    c0_path = state.artifact_dir / "runtime_program_c0.yaml"
    c1_path = state.artifact_dir / "runtime_program_c1.yaml"
    materialize_basket_runtime_program(
        _resolve_contract_path(state.contract_path, runtime["basket_c0_template"]),
        fixture_path=state.fixture_path,
        episode_id="qualification_c0",
        output_path=c0_path,
    )
    materialize_basket_runtime_program(
        _resolve_contract_path(state.contract_path, runtime["basket_c1_template"]),
        fixture_path=state.fixture_path,
        episode_id="qualification_c1",
        output_path=c1_path,
    )
    c0 = yaml.safe_load(c0_path.read_text(encoding="utf-8"))
    c1 = yaml.safe_load(c1_path.read_text(encoding="utf-8"))
    a = state.fixture["identities"]["identity_a"]
    b = state.fixture["identities"]["identity_b"]
    foreign = state.native_responses["identity_a_foreign"]
    own = state.native_responses["identity_a_own"]
    unauth = state.native_responses["unauthenticated_a"]

    c0_pipeline = RuntimePipeline(c0, StateStore(state.artifact_dir / "selector_c0_state"), "qualification_c0", "B0")
    _record_token(c0_pipeline, state.token_a, 1)
    c0_flow = _http_flow(f"/rest/basket/{b['basket_id']}", foreign, state.token_a)
    c0_events = c0_pipeline.execute(c0_flow, turn_idx=2)

    c1_pipeline = RuntimePipeline(c1, StateStore(state.artifact_dir / "selector_c1_state"), "qualification_c1", "B3")
    _record_token(c1_pipeline, state.token_a, 1)
    c1_flows = [_http_flow(f"/rest/basket/{b['basket_id']}", foreign, state.token_a) for _ in range(3)]
    c1_events = [c1_pipeline.execute(flow, turn_idx=index) for index, flow in enumerate(c1_flows, start=2)]
    own_flow = _http_flow(f"/rest/basket/{a['basket_id']}", own, state.token_a)
    own_events = c1_pipeline.execute(own_flow, turn_idx=5)
    unauth_flow = _http_flow(f"/rest/basket/{a['basket_id']}", unauth, None)
    unauth_events = c1_pipeline.execute(unauth_flow, turn_idx=6)

    def applied(events: list[dict[str, Any]]) -> list[str]:
        return [str(event.get("injection_id")) for event in events if event.get("status") == "applied"]

    checks = {
        "c0_shadow_records_foreign_opportunity": bool(applied(c0_events)),
        "c0_shadow_does_not_mutate": c0_flow.response.json == foreign["json"],
        "c1_first_contact_transforms": bool(applied(c1_events[0])),
        "c1_second_contact_transforms": bool(applied(c1_events[1])),
        "c1_third_contact_native": not applied(c1_events[2]) and c1_flows[2].response.json == foreign["json"],
        "c1_own_read_untouched": not applied(own_events) and own_flow.response.json == own["json"],
        "c1_unauthenticated_read_untouched": not applied(unauth_events) and unauth_flow.response.json == unauth["json"],
    }
    return {
        "status": "pass" if all(checks.values()) else "fail",
        "checks": checks,
        "materialized": {"c0": str(c0_path), "c1": str(c1_path)},
    }


def _record_token(pipeline: RuntimePipeline, token: str, turn_idx: int) -> None:
    flow = HTTPFlow(
        request=Request(method="POST", path="/rest/user/login", headers={"Content-Type": "application/json"}),
        response=Response(status_code=200, headers={"Content-Type": "application/json"}, body=b"", json={"authentication": {"token": token}}),
    )
    pipeline.execute(flow, turn_idx=turn_idx)


def _http_flow(path: str, observed: dict[str, Any], token: str | None) -> HTTPFlow:
    headers = {"Accept": "application/json"}
    if token:
        headers.update(_bearer(token))
    return HTTPFlow(
        request=Request(method="GET", path=path, headers=headers),
        response=Response(
            status_code=int(observed["status"]),
            headers=dict(observed.get("headers") or {}),
            body=str(observed.get("body") or "").encode("utf-8"),
            json=copy.deepcopy(observed.get("json")),
        ),
    )


def _render_qualification_markdown(result: dict[str, Any]) -> str:
    lines = [
        "# Protocol-v3 Reset Determinism Qualification",
        "",
        f"Status: `{result['status']}`",
        "",
        "| Check | Result |",
        "|---|---|",
    ]
    for key, value in result["checks"].items():
        lines.append(f"| `{key}` | `{str(value).lower()}` |")
    lines.extend(["", "## Pristine Cycles", "", "| Cycle | Fingerprint | Selector replay |", "|---|---|---|"])
    for row in result["cycles"]:
        lines.append(f"| {row['cycle']} | `{row['fingerprint']}` | `{row['selector_replay']['status']}` |")
    lines.append("")
    return "\n".join(lines)


def _http_json(method: str, url: str, *, headers: dict[str, str] | None = None, body: dict[str, Any] | None = None) -> dict[str, Any]:
    encoded = json.dumps(body).encode("utf-8") if body is not None else None
    request_headers = {"Accept": "application/json", **(headers or {})}
    if body is not None:
        request_headers.setdefault("Content-Type", "application/json")
    req = request.Request(url, data=encoded, method=method, headers=request_headers)
    try:
        with request.urlopen(req, timeout=15) as response:
            raw = response.read()
            status = int(response.status)
            response_headers = dict(response.headers.items())
    except error.HTTPError as exc:
        raw = exc.read()
        status = int(exc.code)
        response_headers = dict(exc.headers.items())
    except Exception as exc:
        return {"status": 0, "headers": {}, "body": str(exc), "json": None}
    text = raw.decode("utf-8", errors="replace")
    try:
        parsed = json.loads(text) if text else None
    except json.JSONDecodeError:
        parsed = None
    return {"status": status, "headers": response_headers, "body": text, "json": parsed}


def _login(base_url: str, email: str, password: str) -> dict[str, Any]:
    response = _http_json("POST", f"{base_url}/rest/user/login", body={"email": email, "password": password})
    auth = ((response.get("json") or {}).get("authentication") or {})
    token = auth.get("token")
    if response["status"] != 200 or not isinstance(token, str):
        raise TargetStateError(f"login failed for {email}: HTTP {response['status']}")
    return {"response": response, "token": token, "bid": auth.get("bid")}


def _identity_from_login(name: str, email: str, login: dict[str, Any]) -> dict[str, Any]:
    claims = _jwt_claims(login["token"])
    data = claims.get("data") if isinstance(claims.get("data"), dict) else claims
    user_id = data.get("id")
    role = data.get("role")
    basket_id = login.get("bid")
    if not isinstance(user_id, int) or not isinstance(basket_id, int) or role != "customer":
        raise TargetStateError(f"login claims invalid for {name}: user_id={user_id}, bid={basket_id}, role={role}")
    return {"email": email, "user_id": user_id, "role": role, "basket_id": basket_id, "known_existing": True}


def _basket(base_url: str, basket_id: int, token: str | None) -> dict[str, Any]:
    return _http_json("GET", f"{base_url}/rest/basket/{basket_id}", headers=_bearer(token) if token else None)


def _bearer(token: str | None) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"} if token else {}


def _require_status(response: dict[str, Any], expected: int, label: str) -> None:
    if response["status"] != expected:
        raise TargetStateError(f"{label}: expected HTTP {expected}, got {response['status']}")


def _require_basket_owner(response: dict[str, Any], expected_user_id: int, label: str) -> None:
    data = ((response.get("json") or {}).get("data") or {})
    if data.get("UserId") != expected_user_id:
        raise TargetStateError(f"{label}: expected UserId {expected_user_id}, got {data.get('UserId')}")


def _product_quantities(body: Any) -> dict[str, int]:
    data = body.get("data") if isinstance(body, dict) else None
    products = data.get("Products") if isinstance(data, dict) else None
    values: dict[str, int] = {}
    for product in products if isinstance(products, list) else []:
        if not isinstance(product, dict) or not isinstance(product.get("id"), int):
            continue
        basket_item = product.get("BasketItem") if isinstance(product.get("BasketItem"), dict) else {}
        quantity = basket_item.get("quantity", product.get("quantity", 1))
        if isinstance(quantity, int):
            values[str(product["id"])] = quantity
    return dict(sorted(values.items()))


def _user_count(base_url: str, token: str) -> int:
    response = _http_json("GET", f"{base_url}/api/Users", headers=_bearer(token))
    data = (response.get("json") or {}).get("data")
    if response["status"] != 200 or not isinstance(data, list):
        raise TargetStateError(f"unable to count users through /api/Users: HTTP {response['status']}")
    return len(data)


def _jwt_claims(token: str) -> dict[str, Any]:
    parts = token.split(".")
    if len(parts) < 2:
        raise TargetStateError("login token is not a JWT")
    payload = parts[1] + "=" * (-len(parts[1]) % 4)
    try:
        value = json.loads(base64.urlsafe_b64decode(payload.encode("ascii")))
    except (ValueError, json.JSONDecodeError) as exc:
        raise TargetStateError("login JWT payload cannot be decoded") from exc
    if not isinstance(value, dict):
        raise TargetStateError("login JWT payload is not an object")
    return value


def _load_contract(path: Path) -> dict[str, Any]:
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    required = {"schema_version", "contract_id", "target", "reset", "readiness", "seed", "expected_probes", "runtime_parameterization"}
    if not isinstance(raw, dict) or raw.get("schema_version") != "atobench.target_state_contract.v1" or not required <= set(raw):
        raise TargetStateError(f"invalid target-state contract: {path}")
    return raw


def _resolve_contract_path(contract_path: Path, raw_path: str) -> Path:
    path = Path(raw_path)
    return path.resolve() if path.is_absolute() else (ATOBENCH_ROOT / path).resolve()


def _container_id(contract: dict[str, Any]) -> str | None:
    completed = subprocess.run(
        ["docker", "inspect", "--format", "{{.Id}}", contract["target"]["container_name"]],
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip() if completed.returncode == 0 else None


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _sha256_json(value: dict[str, Any]) -> str:
    return _sha256_text(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")))


def _short_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def main(argv: list[str] | None = None) -> int:
    """Run the target-state gate without starting a pentest agent."""

    parser = ArgumentParser(description="Protocol-v3 Juice Shop target-state manager")
    subparsers = parser.add_subparsers(dest="command", required=True)
    prepare = subparsers.add_parser("prepare", help="reset, reseed, and fingerprint one episode")
    prepare.add_argument("--contract", type=Path, required=True)
    prepare.add_argument("--episode-id", required=True)
    prepare.add_argument("--artifact-dir", type=Path, required=True)
    prepare.add_argument("--no-reset", action="store_true")
    qualify = subparsers.add_parser("qualify", help="run three pristine cycles and pollution recovery")
    qualify.add_argument("--contract", type=Path, required=True)
    qualify.add_argument("--output-dir", type=Path, required=True)
    qualify.add_argument("--cycles", type=int, default=3)
    args = parser.parse_args(argv)

    try:
        if args.command == "prepare":
            state = prepare_target_state(
                args.contract,
                episode_id=args.episode_id,
                artifact_dir=args.artifact_dir,
                reset=not args.no_reset,
            )
            result = {
                "status": "pass",
                "fixture_path": str(state.fixture_path),
                "fingerprint_path": str(state.fingerprint_path),
                "fingerprint": state.fingerprint["normalized_state_sha256"],
            }
        else:
            result = run_reset_determinism_qualification(
                args.contract,
                output_dir=args.output_dir,
                cycles=args.cycles,
            )
    except TargetStateError as exc:
        print(json.dumps({"status": "fail", "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["status"] == "pass" else 2


if __name__ == "__main__":  # pragma: no cover - exercised through the CLI
    raise SystemExit(main())
