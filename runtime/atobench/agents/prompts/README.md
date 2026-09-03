# ATOBench Agent Prompts

Externalized task prompts for the Claude Code subagent driver. Loaded by
`atobench/agents/claude_code.py` and formatted with `proxy_urls` and
`defense_posture`/`engagement_id` per episode.

## Files

- `T1.md` — single-service flag-capture (acme only)
- `T2.md` — multi-service OIDC chain (acme_idp → auth_service → admin_api)
- `T3.md` — pentest audit (3 services, agentic-pentest-benchmark subagent)

## Format

Python `str.format()` placeholders. Variables supplied by the adapter:

| Placeholder | T1 | T2 | T3 |
|---|---|---|---|
| `{proxy_url}` | ✓ (acme) | — | — |
| `{idp}` | — | ✓ (acme_idp) | — |
| `{auth}` | — | ✓ (auth_service) | ✓ |
| `{admin}` | — | ✓ (admin_api) | ✓ |
| `{acme_idp}` | — | — | ✓ |
| `{defense_posture}` | — | — | ✓ |
| `{engagement_id}` | — | — | ✓ |

## Versioning

Prompts are versioned with the package (atobench `__version__`). A change to
any prompt file should be accompanied by a `atobench/schema/CHANGELOG.md` entry
under the schema version that introduced the change, since prompt content
affects episode reproducibility.
