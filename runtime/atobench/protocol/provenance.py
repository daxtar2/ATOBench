"""Create non-secret model attestations and deterministic protocol snapshots."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import tarfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from atobench.protocol.v3 import ATOBENCH_ROOT


PROVENANCE_SCHEMA = "atobench.model_provenance_attestation.v1"
SNAPSHOT_SCHEMA = "atobench.protocol_source_snapshot.v1"
_SOURCE_DIRS = ("agents", "cli", "experiment", "protocol", "proxy", "schema", "eval")
_EXCLUDED_PARTS = {"__pycache__", ".pytest_cache", "experiments", "state", "scaffold_work", "logs", "tests"}


def capture_model_provenance(
    output_path: str | Path,
    *,
    model_identifier: str,
    provider: str,
    revision_status: str = "provider_build_not_exposed",
    settings_path: str | Path | None = None,
) -> dict[str, Any]:
    """Record only non-secret provider configuration fingerprints.

    API tokens are deliberately never read or serialized. The route is stored as
    a hash because a per-account gateway URL is operational provenance, not a
    paper-facing credential.
    """

    settings_env = _safe_settings_env(settings_path)
    route = _setting_value("ANTHROPIC_BASE_URL", settings_env)
    configured_model = _setting_value("ANTHROPIC_MODEL", settings_env)
    model_labels = {
        key: _setting_value(key, settings_env)
        for key in sorted(set(os.environ) | set(settings_env))
        if key.startswith("ANTHROPIC_DEFAULT_") and key.endswith(("_MODEL", "_MODEL_NAME"))
    }
    route_hash = _sha256_text(route) if route else None
    configuration = {
        "route_sha256": route_hash,
        "configured_model": configured_model or None,
        "model_labels": model_labels,
        "auth_env_present": any(
            key in os.environ or key in settings_env for key in ("ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY")
        ),
    }
    payload = {
        "schema_version": PROVENANCE_SCHEMA,
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "provider": provider,
        "transport": "anthropic_compatible_gateway",
        "model_identifier": model_identifier,
        "configured_model_matches_identifier": configured_model.casefold() == model_identifier.casefold(),
        "revision": {
            "status": revision_status,
            "value": None,
            "reason": (
                "The configured Anthropic-compatible gateway exposes the versioned model label "
                "but no provider build or immutable deployment revision."
            ),
        },
        "carrier": {"name": "Claude Code", "version": _command_version("claude", ["--version"])},
        "configuration": configuration,
        "configuration_sha256": _sha256_json(configuration),
        "credential_policy": "No token, API key, or raw gateway route is serialized.",
    }
    output = Path(output_path).expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(yaml.safe_dump(payload, sort_keys=False, allow_unicode=False), encoding="utf-8")
    return payload


def build_source_snapshot(
    protocol_manifest: str | Path,
    output_archive: str | Path,
    output_manifest: str | Path,
    *,
    root: str | Path | None = None,
) -> dict[str, Any]:
    """Archive the code and frozen inputs required for protocol-v3 collection.

    The archive intentionally excludes historical runs and the protocol manifest
    itself. The latter contains the archive hash, so including it would create a
    circular dependency. A separate manifest binds archive members to SHA-256.
    """

    repo = Path(root).expanduser().resolve() if root else ATOBENCH_ROOT
    spec = yaml.safe_load(Path(protocol_manifest).expanduser().read_text(encoding="utf-8")) or {}
    members = _snapshot_members(repo, spec, output_archive=output_archive, output_manifest=output_manifest)
    file_hashes = {path.relative_to(repo).as_posix(): _sha256_file(path) for path in members}
    snapshot = {
        "schema_version": SNAPSHOT_SCHEMA,
        "protocol_id": spec.get("protocol_id"),
        "contents_policy": "collection code plus frozen input artifacts; excludes historical runs and protocol manifest",
        "files": file_hashes,
        "files_sha256": _sha256_json(file_hashes),
    }
    manifest_bytes = json.dumps(snapshot, sort_keys=True, indent=2).encode("utf-8") + b"\n"

    archive = Path(output_archive).expanduser().resolve()
    archive.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive, mode="w:xz", format=tarfile.PAX_FORMAT) as handle:
        for path in members:
            _add_deterministic_file(handle, path, path.relative_to(repo).as_posix())
        _add_deterministic_bytes(handle, manifest_bytes, "SOURCE_SNAPSHOT_MANIFEST.json")

    snapshot["source_archive_sha256"] = _sha256_file(archive)
    snapshot["source_archive_path"] = str(archive.relative_to(repo))
    manifest = Path(output_manifest).expanduser().resolve()
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(json.dumps(snapshot, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    return snapshot


def _snapshot_members(repo: Path, spec: dict[str, Any], *, output_archive: str | Path, output_manifest: str | Path) -> list[Path]:
    paths: set[Path] = {repo / "__init__.py"}
    for directory in _SOURCE_DIRS:
        base = repo / directory
        if not base.is_dir():
            continue
        paths.update(path for path in base.rglob("*.py") if not _excluded(path, repo))
    for item in spec.get("frozen_artifacts") or []:
        if isinstance(item, dict) and item.get("path"):
            paths.add((repo / str(item["path"])).resolve())
    for path in (Path(output_archive).resolve(), Path(output_manifest).resolve()):
        paths.discard(path)
    missing = sorted(str(path.relative_to(repo)) for path in paths if not path.is_file())
    if missing:
        raise FileNotFoundError(f"source snapshot inputs missing: {', '.join(missing)}")
    return sorted(paths, key=lambda path: path.relative_to(repo).as_posix())


def _excluded(path: Path, repo: Path) -> bool:
    return any(part in _EXCLUDED_PARTS for part in path.relative_to(repo).parts)


def _add_deterministic_file(handle: tarfile.TarFile, path: Path, name: str) -> None:
    _add_deterministic_bytes(handle, path.read_bytes(), name)


def _add_deterministic_bytes(handle: tarfile.TarFile, payload: bytes, name: str) -> None:
    info = tarfile.TarInfo(name=name)
    info.size = len(payload)
    info.mode = 0o644
    info.mtime = 0
    info.uid = 0
    info.gid = 0
    info.uname = ""
    info.gname = ""
    handle.addfile(info, io.BytesIO(payload))


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _sha256_json(value: Any) -> str:
    return _sha256_text(json.dumps(value, sort_keys=True, separators=(",", ":")))


def _safe_settings_env(settings_path: str | Path | None) -> dict[str, str]:
    if not settings_path:
        return {}
    raw = json.loads(Path(settings_path).expanduser().read_text(encoding="utf-8"))
    env = raw.get("env") if isinstance(raw, dict) else None
    if not isinstance(env, dict):
        return {}
    # Do not copy or inspect credential values. Only whitelisted model-routing
    # settings are allowed into this provenance capture.
    safe: dict[str, str] = {}
    for key in env:
        name = str(key)
        if name in {"ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_API_KEY"}:
            safe[name] = "<present>"
        elif name == "ANTHROPIC_BASE_URL" or name == "ANTHROPIC_MODEL" or name.startswith("ANTHROPIC_DEFAULT_"):
            safe[name] = str(env[key])
    return safe


def _setting_value(key: str, settings_env: dict[str, str]) -> str:
    return str(os.environ.get(key) or settings_env.get(key) or "").strip()


def _command_version(command: str, args: list[str]) -> str | None:
    from shutil import which
    from subprocess import run

    executable = which(command)
    if not executable:
        return None
    try:
        return run([executable, *args], capture_output=True, text=True, check=True, timeout=10).stdout.strip()
    except Exception:
        return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subcommands = parser.add_subparsers(dest="command", required=True)
    capture = subcommands.add_parser("capture-model")
    capture.add_argument("--output", required=True)
    capture.add_argument("--model", required=True)
    capture.add_argument("--provider", required=True)
    capture.add_argument("--settings", help="Optional Claude settings JSON; only non-secret model routing fields are read.")
    snapshot = subcommands.add_parser("build-source-snapshot")
    snapshot.add_argument("--protocol-manifest", required=True)
    snapshot.add_argument("--output-archive", required=True)
    snapshot.add_argument("--output-manifest", required=True)
    args = parser.parse_args()
    if args.command == "capture-model":
        result = capture_model_provenance(
            args.output,
            model_identifier=args.model,
            provider=args.provider,
            settings_path=args.settings,
        )
    else:
        result = build_source_snapshot(args.protocol_manifest, args.output_archive, args.output_manifest)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
