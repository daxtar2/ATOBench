# Generic platform scaffold

## Purpose

The platform scaffold connects the existing execution and analysis chains
without rewriting either one. It treats the legacy runner as an adapter and
treats Verification Resilience as the first frozen analysis profile.

```text
TargetAdapter + AOUBundle + AnalysisProfile + Campaign
                 │
                 ▼
        frozen binding validation
                 │
                 ▼
          execution plan only
                 │
                 ▼
legacy runner / future adapter → frozen campaign → profile analysis → data plane
```

The current adapter emits an exact plan for `atobench-cross-model` by default.
It only launches the legacy runner after a separate handoff revalidates the
freeze lock and the caller supplies both explicit execution flags. Execution
authorization stays separate from configuration loading.

`init-campaign` turns already registered frozen components into a new campaign
manifest. It calculates the legacy-runner digest at creation time, validates
the component artifacts, and refuses duplicate IDs. By default it creates an
**unregistered** manifest; only `--register` updates the local registry.

## Contracts

All v1 platform contracts are JSON so the standalone package has no YAML
dependency.

| Contract | Responsibility |
|---|---|
| `atobench.target_adapter.v1` | target lifecycle capabilities and snapshot-bound artifacts |
| `atobench.aou_bundle.v1` | target-compatible frozen AOU, conditions, runtime adapter and artifacts |
| `atobench.analysis_profile.v1` | analysis/data-plane profile and entrypoint |
| `atobench.campaign.v1` | exact target + AOUs + profile + execution adapter binding |
| `atobench.platform_registry.v1` | local index of all component manifests |
| `atobench.platform_freeze.v1` | replayable, plan-only lock generated from a validated campaign |

Every external file is declared with a SHA-256. `validate`, `plan`, `run
--dry-run`, and `freeze` require `--allow-external-artifacts` before reading an
artifact outside the standalone project root.

The registry is an index, not a frozen source artifact: adding another campaign
does not invalidate a lock for an unchanged campaign. Locks still bind the
selected IDs, manifest digests, component artifacts, runner digest and exact
legacy command. For `atobench-cross-model-v1`, the wrapper, dispatcher and
Protocol runner modules are all frozen because all three are loaded at runtime.

## Current bridge

`juice-shop-vr-stage20-smoke` binds:

- `juice-shop-protocol-v3` target adapter;
- frozen SQLi, Basket and JWT AOU bundles;
- `verification-resilience-stage20` profile;
- the existing `../atobench/scripts/atobench-cross-model` runner.

The platform IDs are intentionally stable and descriptive. The bridge maps
them to the legacy runner keys `sqli`, `basket`, and `jwt` only at the adapter
boundary. Future runners must not require their internal keys to leak into new
campaign manifests.

## Commands

From the standalone project root:

```bash
python3 scripts/atobench list

python3 scripts/atobench validate \
  --campaign juice-shop-vr-stage20-smoke \
  --allow-external-artifacts

python3 scripts/atobench run \
  --campaign juice-shop-vr-stage20-smoke \
  --allow-external-artifacts \
  --dry-run

python3 scripts/atobench freeze \
  --campaign juice-shop-vr-stage20-smoke \
  --allow-external-artifacts \
  --output workspace/platform-locks/juice-shop-vr-stage20-smoke.freeze.json
```

## Creating a new campaign

Do not reuse `juice-shop-vr-stage20-smoke` for data collection. Create one
uniquely named campaign first. This command writes a frozen configuration and
registers it locally; it does not start a target or call a model:

```bash
python3 scripts/atobench init-campaign \
  --id juice-shop-gpt55-sqlijwt-basket-t1 \
  --target juice-shop-protocol-v3 \
  --aous sqli-evidence-closure-v3 basket-scope-closure-v3_1 jwt-hash-suppression-v3_1 \
  --profile verification-resilience-stage20 \
  --models gpt-5.5 \
  --rounds 1 \
  --parallel-workers 1 \
  --start-target \
  --agent-timeout-multiplier 1.5 \
  --max-tool-calls 60 \
  --output config/platform/campaigns/juice_shop_gpt55_sqlijwt_basket_t1.json \
  --register \
  --allow-external-artifacts
```

For inspection only, add `--dry-run`: it emits the derived manifest and digest
without writing the manifest or registry. Omit `--register` to write an
unregistered manifest outside the registry for review. `--max-tool-calls` is
optional; omitting it preserves the runner's unlimited/default behavior.

Then validate and freeze the exact newly registered ID:

```bash
python3 scripts/atobench validate \
  --campaign juice-shop-gpt55-sqlijwt-basket-t1 \
  --allow-external-artifacts

python3 scripts/atobench freeze \
  --campaign juice-shop-gpt55-sqlijwt-basket-t1 \
  --allow-external-artifacts \
  --output workspace/platform-locks/juice-shop-gpt55-sqlijwt-basket-t1.freeze.json
```

The next step after a reviewed dry-run is a per-run handoff:

```bash
python3 scripts/atobench prepare \
  --campaign juice-shop-vr-stage20-smoke \
  --lock reference/latest/platform/juice-shop-vr-stage20-smoke.freeze.json \
  --output workspace/guarded-runs/juice-shop-vr-stage20-smoke \
  --allow-external-artifacts
```

`prepare` revalidates every declared artifact and requires the target adapter
to expose `health`; campaigns that start a target must additionally expose
`start` and `reset`. It writes only `execution_handoff.json` metadata.

The following command is intentionally the first command that can start a
target or call a model. Use it only for a newly created, uniquely named
campaign manifest and after reviewing its handoff:

```bash
python3 scripts/atobench execute \
  --campaign juice-shop-vr-stage20-smoke \
  --lock reference/latest/platform/juice-shop-vr-stage20-smoke.freeze.json \
  --handoff workspace/guarded-runs/juice-shop-vr-stage20-smoke/execution_handoff.json \
  --allow-external-artifacts \
  --allow-real-execution
```

`execute` validates the plan, handoff and lock again immediately before it
launches the legacy runner. Its receipt stores exit metadata and hashes only;
it does not capture stdout, agent content, raw credentials, or target response
bodies.

## Adding a new frozen AOU

1. Create a target adapter manifest if its target is new; declare only
   lifecycle capabilities and hash-bound source artifacts.
2. Create an AOU bundle with a stable ID, `target_id`, C0/C1 conditions,
   runtime adapter metadata and hashes for its opportunity contract/runtime.
3. Create or reuse an analysis profile. A new profile must state what it reads
   and which data-plane exports it can produce.
4. Register the target/AOU/profile manifests in `config/platform/registry.json`.
5. Use `init-campaign` to build the compatible campaign and, after review,
   explicitly register it.
6. Run `validate`, then generate a plan-only freeze lock before any execution.

Do not add secrets, response bodies, host identity, raw credentials or mutable
latest paths to a platform manifest. Store only source locations and digests.
