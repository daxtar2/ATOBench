# ATOBench

**ATOBench: Tracing How Autonomous Penetration-Testing Agents Verify Vulnerabilities When Target Evidence Lies**

ATOBench is an evaluation framework that makes the verification process of
autonomous penetration-testing agents observable under *Adversarial Target
Observation* (ATO). It injects registered response transformations at runtime
and pairs each transformed episode with a native episode under the same
environment. Each pair is aligned at the first affected response, and a
source-linked reconstruction then follows later actions, evidence recovery,
stopping, and report support.

Three frozen observation contracts (Adversarial Observation Units, AOUs)
cover different evidence structures:

| AOU | Registered intervention | Preserved recovery path |
|---|---|---|
| **SQLi proof** | Login/search requests carrying registered SQLi markers return 401 / empty results | Same-endpoint raw-JSON Unicode-escape request bypasses the raw selector |
| **Basket ownership** | Cross-identity basket reads keep HTTP 200 and schema but show the wrong owner with an empty product list | Own-basket, reverse-direction, and unauthenticated traffic stay native; the third eligible read returns the native basket |
| **JWT artifact** | A successful login returns a re-signed RS256 token with the registered claim removed | The token keeps working; a later login reacquires the native artifact |

The framework executes matched Native/ATO episodes, normalizes and aligns
their trajectories at intervention contact, and reconstructs how changed
observations propagate through subsequent actions, evidence recovery,
stopping, and reporting.

![ATOBench overview](docs/figures/atobench_overview.png)

## Repository layout

```text
runtime/    executable runtime: mitmproxy response-transformation engine,
            Protocol-v3 paired cross-model campaign runner, agent adapters,
            frozen Juice Shop AOU suites, evaluation components
analysis/   evidence reconstruction + judging layer: trajectory compiler,
            semantic matching, resilience statistics, learning-data exports
docs/       design documents (deception runtime architecture, AOU contract
            standard, experiment design, runbook, evaluation workflow)
```

## Installation

Python 3.10+ is required. Real runs additionally need Docker and a configured
Claude Code-compatible agent carrier.

```bash
python3 -m pip install ./runtime
python3 -m pip install -e './analysis'
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

Only run this against the included intentionally vulnerable benchmark target
(OWASP Juice Shop) or a system you are explicitly authorized to test:

```bash
atobench-cross-model \
  --campaign-id qwen37plus-sqli-smoke \
  --rounds 1 \
  --models qwen3.7-plus \
  --model-selector qwen3.7-plus=opus \
  --aous sqli \
  --claude-effort high \
  --start-target \
  --parallel-workers 1
```

Treat a pair as invalid unless route attestation is present and both episodes
show agent-originated work. The runner fails closed on route-attestation or
provider errors.

## What is intentionally not in this repository

- **Run logs and episode outputs.** All campaign run data (turns, mitmproxy
  dumps, agent workspaces, frozen run outputs) is excluded.
- **Frozen reference datasets.** The analysis layer's frozen cohort exports
  (450-episode Stage-20 reference) are not shipped. They can be regenerated
  from a frozen campaign with the `analysis/scripts/atobench-vr` export
  commands.
- **Provider configuration.** No API keys, tokens, or gateway routes. LLM
  endpoints are configured via `OPENAI_API_KEY` / `OPENAI_BASE_URL`.
- Historical provenance manifests (`*_collection_source.manifest.json`,
  freeze manifests of construction artifacts) reference the original frozen
  collection; recorded hashes describe the released tree.

## Safety and authorized use

This project exists to *evaluate* agentic penetration testing under
defender-controlled observations. The bundled target is the intentionally
vulnerable OWASP Juice Shop running in an isolated container. Do not point
the harness at systems you do not own or are not explicitly authorized to
test.

## Citation

See [CITATION.cff](CITATION.cff).

## License

Apache-2.0. See [LICENSE](LICENSE) and [NOTICE](NOTICE).
