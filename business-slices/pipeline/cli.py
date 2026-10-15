#!/usr/bin/env python3
"""Validate and materialize ScenarioBuildSpec → generated/<scenario_id>/ (local only)."""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = Path(__file__).resolve().parent / "schemas" / "scenario_build_spec.v0.json"
GENERATED = ROOT / "generated"
TEMPLATES = ROOT / "templates" / "slice_skeleton"


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def validate_spec(spec: dict) -> list[str]:
    """Lightweight validation without jsonschema dependency (optional enhance later)."""
    errors: list[str] = []
    schema = load_json(SCHEMA_PATH)
    required = schema.get("required", [])
    for key in required:
        if key not in spec:
            errors.append(f"missing required field: {key}")
    if spec.get("schema_version") != "build_spec.v0":
        errors.append("schema_version must be build_spec.v0")
    paths = spec.get("paths") or []
    if len(paths) < 2:
        errors.append("paths must contain at least 2 registered paths (P0 + bypass)")
    hooks = spec.get("hooks") or []
    if not hooks:
        errors.append("hooks must be non-empty for C1 dose support")
    path_ids = {p.get("path_id") for p in paths}
    for h in hooks:
        if h.get("path_id") not in path_ids:
            errors.append(f"hook {h.get('hook_id')} references unknown path_id {h.get('path_id')}")
    src = spec.get("source") or {}
    if src.get("kind") == "private_trace_ref" and not src.get("private_ref"):
        errors.append("source.private_ref required when kind=private_trace_ref")
    return errors


def materialize(spec: dict, out_dir: Path, force: bool = False) -> Path:
    scenario_id = spec["scenario_id"]
    dest = out_dir / scenario_id
    if dest.exists():
        if not force:
            raise FileExistsError(f"{dest} exists (use --force to overwrite)")
        shutil.rmtree(dest)
    dest.mkdir(parents=True)

    # Copy public skeleton if present; otherwise write minimal stubs.
    if TEMPLATES.is_dir():
        shutil.copytree(TEMPLATES, dest, dirs_exist_ok=True)
    else:
        (dest / "README.md").write_text(
            f"# {spec['display_name']}\n\nGenerated locally — do not commit.\n",
            encoding="utf-8",
        )

    (dest / "build_spec.json").write_text(
        json.dumps(spec, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    manifest = {
        "scenario_id": scenario_id,
        "display_name": spec["display_name"],
        "business_goal": spec["business_goal"],
        "path_ids": [p["path_id"] for p in spec["paths"]],
        "hook_ids": [h["hook_id"] for h in spec["hooks"]],
        "outcome": spec["outcome"],
        "metrics_ref": "metrics.v1",
        "note": "Artifact under generated/ — gitignored; not for GitHub",
    }
    (dest / "manifest.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    # Path / hook registries for future graders
    (dest / "path_signatures.json").write_text(
        json.dumps(spec["paths"], indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    (dest / "hooks.json").write_text(
        json.dumps(spec["hooks"], indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    return dest


def cmd_validate(args: argparse.Namespace) -> int:
    spec = load_json(Path(args.spec))
    errors = validate_spec(spec)
    if errors:
        print("INVALID", args.spec)
        for e in errors:
            print(f"  - {e}")
        return 1
    print("OK", args.spec)
    return 0


def cmd_build_one(args: argparse.Namespace) -> int:
    spec_path = Path(args.spec)
    spec = load_json(spec_path)
    errors = validate_spec(spec)
    if errors:
        for e in errors:
            print(f"  - {e}", file=sys.stderr)
        return 1
    # Refuse to read private payloads into the repo tree copy of the spec publish path
    src = spec.get("source") or {}
    if src.get("kind") == "private_trace_ref":
        private = ROOT / "_private" / src["private_ref"]
        if not private.is_file():
            print(
                f"private ref not found locally: {private} "
                "(create under business-slices/_private/ — never commit)",
                file=sys.stderr,
            )
            return 2
        print(f"note: using private ref {private} (not packaged into git)")
    out_root = Path(args.out) if args.out else GENERATED
    dest = materialize(spec, out_root, force=args.force)
    print(f"wrote {dest} (gitignored if under business-slices/generated/)")
    return 0


def cmd_build_batch(args: argparse.Namespace) -> int:
    specs_dir = Path(args.specs_dir)
    paths = sorted(specs_dir.glob("*.json"))
    if not paths:
        print(f"no specs in {specs_dir}", file=sys.stderr)
        return 1
    rc = 0
    for path in paths:
        ns = argparse.Namespace(spec=str(path), out=args.out, force=args.force)
        print(f"== {path.name}")
        one_rc = cmd_build_one(ns)
        if one_rc != 0:
            rc = one_rc
    return rc


def main() -> int:
    parser = argparse.ArgumentParser(description="Business-slice env build pipeline (local artifacts)")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_v = sub.add_parser("validate", help="Validate a BuildSpec JSON")
    p_v.add_argument("spec", help="Path to BuildSpec JSON")
    p_v.set_defaults(func=cmd_validate)

    p_b = sub.add_parser("build-one", help="Materialize one scenario under generated/")
    p_b.add_argument("spec", help="Path to BuildSpec JSON")
    p_b.add_argument("--out", default="", help="Output root (default: business-slices/generated)")
    p_b.add_argument("--force", action="store_true")
    p_b.set_defaults(func=cmd_build_one)

    p_batch = sub.add_parser("build-batch", help="Build all *.json specs in a directory")
    p_batch.add_argument("specs_dir", help="Directory of BuildSpec JSON files")
    p_batch.add_argument("--out", default="", help="Output root")
    p_batch.add_argument("--force", action="store_true")
    p_batch.set_defaults(func=cmd_build_batch)

    args = parser.parse_args()
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
