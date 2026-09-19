#!/usr/bin/env bash
# Vendored-source sync for the ATOBench Harbor tasks.
#
# Each Harbor task must be self-contained (registry distribution uploads only
# the task directory), so the proxy sidecar build context carries a vendored
# snapshot of the atobench proxy stack plus the frozen RuntimeProgram (or
# program template, for the stateful basket AOU) for the task's condition.
# This script refreshes those snapshots from the single source of truth under
# runtime/. Re-run it after editing any pinned input, then review the diff
# before committing (fail-closed philosophy preserved: the vendored copy is
# what actually ships and runs).
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SRC="$REPO_ROOT/runtime/atobench"
CONFIRMATORY="$SRC/targets/juice-shop/benchmark_suites/juice-shop-confirmatory-v1/programs"
EXPANSION="$SRC/targets/juice-shop/aou_expansion_v0/programs"
TASKS="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

vendor_stack() {
    local dest="$1"
    rm -rf "$dest/atobench"
    mkdir -p "$dest/atobench"
    cp "$SRC/__init__.py" "$dest/atobench/__init__.py"
    for pkg in proxy runtime_ir schema; do
        cp -r "$SRC/$pkg" "$dest/atobench/$pkg"
    done
}

# The basket seed sidecar additionally needs the protocol state manager
# (reseed + template materialization).
vendor_protocol() {
    local dest="$1"
    mkdir -p "$dest/atobench/protocol"
    cp "$SRC/protocol/__init__.py" "$dest/atobench/protocol/__init__.py"
    cp "$SRC/protocol/juice_shop_state.py" "$dest/atobench/protocol/juice_shop_state.py"
}

for task in atobench-sqli-c0 atobench-sqli-c1 atobench-jwt-c0 atobench-jwt-c1; do
    vendor_stack "$TASKS/$task/environment/proxy"
done
for task in atobench-basket-c0 atobench-basket-c1; do
    vendor_stack "$TASKS/$task/environment/proxy"
    vendor_protocol "$TASKS/$task/environment/proxy"
done
find "$TASKS" -name "__pycache__" -type d -prune -exec rm -rf {} +

# Shared reward core (atobench.reward.v2) into every task's tests/.
for task in atobench-sqli-c0 atobench-sqli-c1 atobench-jwt-c0 atobench-jwt-c1 atobench-basket-c0 atobench-basket-c1; do
    cp "$TASKS/_shared/reward_core.py" "$TASKS/$task/tests/reward_core.py"
done

# Frozen RuntimePrograms (static conditions).
cp "$CONFIRMATORY/c0_identity/runtime_program.yaml" "$TASKS/atobench-sqli-c0/environment/proxy/runtime_program.yaml"
cp "$CONFIRMATORY/evidence_sqli_closure/runtime_program.yaml" "$TASKS/atobench-sqli-c1/environment/proxy/runtime_program.yaml"
cp "$EXPANSION/c0_identity/runtime_program.yaml" "$TASKS/atobench-jwt-c0/environment/proxy/runtime_program.yaml"
cp "$EXPANSION/jwt_hash_suppression/runtime_program.yaml" "$TASKS/atobench-jwt-c1/environment/proxy/runtime_program.yaml"

# Basket program templates (materialized per-trial by the seed sidecar).
for task in atobench-basket-c0 atobench-basket-c1; do
    cp "$EXPANSION/basket_scope_closure_persistent_k2/c0_shadow_runtime_program.yaml" \
       "$TASKS/$task/environment/proxy/c0_shadow_template.yaml"
    cp "$EXPANSION/basket_scope_closure_persistent_k2/c1_treatment_runtime_program.yaml" \
       "$TASKS/$task/environment/proxy/c1_treatment_template.yaml"
done

echo "synced: proxy stack + programs/templates -> harbor/tasks/*/environment/proxy/"
