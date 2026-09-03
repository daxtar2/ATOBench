# ATOBench Cross-Model Runtime

Executable runtime for ATOBench's paired Native/ATO cross-model episodes.
The package keeps the original execution path:

```text
atobench-cross-model
  -> Protocol-v3 paired C0/C1 campaign scheduler
  -> atobench-experiment CLI and ExperimentCycle
  -> agent carrier and route attestation
  -> mitmproxy addon, rule engine, and RuntimeProgram
  -> schema validation, target-state reset, attribution, and evaluation
```

The included public reference bundle is the frozen Juice Shop confirmatory
suite (SQLi, Basket, and JWT evidence AOUs). It retains the protocol configs,
runtime programs, target-state contract, and public target compose definition.

## Deliberate exclusions

This package does **not** contain provider configuration, keys/tokens,
cookies, run logs, frozen experiment outputs, blind condition mappings, or
editor artifacts. `scripts/release_check.py` audits the tree for forbidden
paths and secrets.

## Install

Python 3.10+ plus Docker and a configured Claude Code-compatible carrier are
required for a real run.

```bash
python3 -m pip install ./runtime
```

## Validate without a model call

The command below materializes the exact per-model Protocol-v3 configs,
assignment schedule, provenance record, and source snapshot, without starting
Docker or calling a model:

```bash
atobench-cross-model \
  --campaign-id qwen37plus-sqli-dryrun \
  --rounds 1 \
  --models qwen3.7-plus \
  --model-selector qwen3.7-plus=opus \
  --aous sqli \
  --parallel-workers 1 \
  --dry-run
```

## Run an authorized smoke test

Only run this against the included intentionally vulnerable benchmark or a
system you are explicitly authorized to test:

```bash
atobench-cross-model \
  --campaign-id qwen37plus-sqli-smoke \
  --rounds 1 \
  --models qwen3.7-plus \
  --model-selector qwen3.7-plus=opus \
  --aous sqli \
  --claude-effort high \
  --agent-timeout-multiplier 1.5 \
  --start-target \
  --parallel-workers 1
```

Treat a pair as invalid unless route attestation is present and both episodes
show agent-originated work; short proxy-health-only executions are not valid
smokes. The runner fails closed on route-attestation or provider errors.

## Layout

```text
atobench/                            executable runtime package
proxy/                               legacy episode JSONL logging dependency
atobench/targets/juice-shop/         public reference target contract/suite
atobench/examples/                   Protocol-v3 configs
scripts/release_check.py             path, generated-artifact, and secret audit
tests/                               release projection smoke tests
```

## License

Apache-2.0. See [LICENSE](LICENSE).
