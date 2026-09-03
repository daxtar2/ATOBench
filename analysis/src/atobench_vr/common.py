from __future__ import annotations

import csv
import hashlib
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

SCHEMA_VERSION = "atobench.verification_resilience.stage_manifest.v1"
CODE_VERSION = "1.6.0"


class GateError(RuntimeError):
    """Raised when a blocking analysis gate fails."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_json(value: Any) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(payload).hexdigest()


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def read_json_or_yaml(path: Path) -> Any:
    text = path.read_text(encoding="utf-8")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        data: dict[str, Any] = {}
        for raw in text.splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or ":" not in line:
                continue
            key, value = line.split(":", 1)
            value = value.strip()
            if value in {"true", "false"}:
                data[key.strip()] = value == "true"
            elif value in {"{}", "[]"}:
                data[key.strip()] = json.loads(value)
            else:
                data[key.strip()] = value
        return data


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def write_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, sort_keys=True) + "\n")
            count += 1
    return count


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as handle:
        for line_number, raw in enumerate(handle, 1):
            if not raw.strip():
                continue
            try:
                value = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise GateError(f"invalid JSONL at {path}:{line_number}: {exc}") from exc
            if not isinstance(value, dict):
                raise GateError(f"JSONL row is not an object at {path}:{line_number}")
            rows.append(value)
    return rows


def write_csv(path: Path, rows: list[dict[str, Any]], fields: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def ensure_output_available(output_dir: Path, new_version: bool) -> None:
    completed = output_dir / "stage_manifest.json"
    if completed.exists() and not new_version:
        raise GateError(
            f"refusing to overwrite completed stage {output_dir}; pass --new-version"
        )
    output_dir.mkdir(parents=True, exist_ok=True)


def stage_manifest(
    stage: str,
    inputs: list[Path],
    outputs: list[Path],
    status: str,
    *,
    dry_run: bool,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "code_version": CODE_VERSION,
        "stage": stage,
        "status": status,
        "dry_run": dry_run,
        "created_at": utc_now(),
        "inputs": [
            {
                "path": str(path.resolve()),
                "sha256": sha256_file(path) if path.is_file() else None,
            }
            for path in inputs
        ],
        "outputs": [
            {
                "path": str(path.resolve()),
                "sha256": sha256_file(path) if path.is_file() else None,
            }
            for path in outputs
        ],
        "details": details or {},
        "network_accessed": False,
        "paper_read_for_expected_results": False,
    }


def is_probably_real_path(path: Path) -> bool:
    """Treat every non-temporary input as real unless explicitly authorized.

    The original prototype recognized real data through developer-specific
    absolute paths. That silently disabled the safety gate after relocation.
    Standalone releases instead consider only operating-system temporary
    directories synthetic; project, campaign, and session paths are real.
    """

    resolved = path.resolve()
    temp_root = Path(tempfile.gettempdir()).resolve()
    try:
        resolved.relative_to(temp_root)
        return False
    except ValueError:
        return True


def require_real_data_authorization(paths: Iterable[Path], allowed: bool) -> None:
    if any(is_probably_real_path(path) for path in paths) and not allowed:
        raise GateError("real-data access requires --allow-real-data")
