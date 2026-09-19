#!/usr/bin/env bash
# Vendored-source sync for the ATOBench Harbor tasks.
#
# Each Harbor task must be self-contained (registry distribution uploads only
# the task directory), so the proxy sidecar build context carries a vendored
# snapshot of the atobench proxy stack plus the frozen RuntimeProgram for the
# task's condition. This script refreshes those snapshots from the single
# source of truth under runtime/. Re-run it after editing any pinned input,
# then review the diff before committing (fail-closed philosophy preserved:
# the vendored copy is what actually ships and runs).
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SRC="$REPO_ROOT/runtime/atobench"
SUITE="$SRC/targets/juice-shop/benchmark_suites/juice-shop-confirmatory-v1/programs"

vendor_stack() {
    local dest="$1"
    rm -rf "$dest/atobench"
    mkdir -p "$dest/atobench"
    cp "$SRC/__init__.py" "$dest/atobench/__init__.py"
    for pkg in proxy runtime_ir schema; do
        cp -r "$SRC/$pkg" "$dest/atobench/$pkg"
        find "$dest/atobench/$pkg" -name "__pycache__" -type d -prune -exec rm -rf {} +
    done
}

for task in atobench-sqli-c0 atobench-sqli-c1; do
    ctx="$(dirname "${BASH_SOURCE[0]}")/$task/environment/proxy"
    vendor_stack "$ctx"
done

cp "$SUITE/c0_identity/runtime_program.yaml" \
   "$(dirname "${BASH_SOURCE[0]}")/atobench-sqli-c0/environment/proxy/runtime_program.yaml"
cp "$SUITE/evidence_sqli_closure/runtime_program.yaml" \
   "$(dirname "${BASH_SOURCE[0]}")/atobench-sqli-c1/environment/proxy/runtime_program.yaml"

echo "synced: proxy stack + runtime programs -> atobench-sqli-c{0,1}/environment/proxy/"
