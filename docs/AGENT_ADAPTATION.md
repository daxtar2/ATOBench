# Adapting Your Own Penetration-Testing Agent

ATOBench measures what an agent *does* over HTTP, not how it is built. Any
agent that can (a) send HTTP requests through a proxy and (b) write a final
report can be evaluated. This guide covers the three integration surfaces:

1. **Driver** — how the harness launches your agent.
2. **Task brief** — what your agent is told to do.
3. **Trajectory contract** — the driver-neutral session file the harness
   emits for every episode.

The design rule: an adapter is a *plug converter, not a behavior corrector*.
It absorbs interface differences (CLI flags, output formats, session files)
and never changes what the agent is asked to do or sees. Behavioral
differences are exactly what the benchmark measures.

## The measurement principle

The primary evidence in every episode is the **proxy-side trajectory**
(`turns.jsonl`, written by mitmproxy). It records the HTTP requests the
target actually received and the responses it actually returned — regardless
of which agent driver ran. Deception contact, evidence recovery, and stopping
are reconstructed from that record; agent self-reports are supporting
material, never the source of truth.

This is why a new agent does not need deep integration: if its traffic goes
through the proxy, measurement works.

## Built-in Claude Code drivers

The shipped harness runs Claude Code through two drivers selected via
`agent.driver` in the experiment config (or `--driver` on `atobench-experiment
run --mode proxy`):

| Driver | Mechanism | Session attestation |
|---|---|---|
| `claude-code` (→ `subagent`) | spawns `claude -p --output-format json` | `adapter_declared` (final result only) |
| `agentic-pentest-benchmark` | spawns `claude -p --output-format stream-json` with a vendored agent definition | `stream_verified` (full event stream) |

For every episode run in an isolated workspace, the harness writes
`agent_session.jsonl` next to the raw `claude_stream.jsonl`.

## Running a third-party agent: the `command` driver

The `command` driver wraps an arbitrary agent CLI without writing code.
Describe the launch in a config validated against
`atobench/schema/command_agent_config.json`:

```yaml
# my_agent.command.yaml  (schema: atobench.command_agent_config.v1)
schema_version: atobench.command_agent_config.v1
name: my-agent
command:
  - my-agent
  - --task-file
  - "{task_file}"
  - --proxy
  - "{proxy_url}"
  - --output
  - "{output_dir}"
report_path: "{output_dir}/final_report.txt"
session_path: "{output_dir}/trajectory.jsonl"   # optional, if your agent emits the canonical schema
env_allowlist:                                   # deny-by-default; forward only what your agent needs
  - PATH
  - HOME
  - LANG
  - TZ
env:
  MY_AGENT_MODE: pentest
timeout_s: 900
startup_timeout_s: 120
```

### Placeholders

`command`, `report_path`, `session_path`, and `cwd` may contain
`{proxy_url}`, `{target_url}`, `{episode_id}`, `{output_dir}`, `{task_file}`,
and `{report_path}`. Any other brace token is rejected before launch.

### What the harness provides

- `{output_dir}` — an isolated, empty per-episode workspace. Your agent owns
  this directory during the episode.
- `{task_file}` — `task_prompt.txt`, the written task brief. The brief is
  the same pentest-audit contract used by the built-in drivers: scope, the
  proxy URL as the only authorized origin, stage budget, stop conditions, and
  the path where the final report must be written.

### What the harness expects

- **A report artifact** at `report_path`. If the file is missing or empty,
  the episode fails closed (`PARSE_FAIL`), even if the process exited 0.
- **All target traffic through the proxy**. The task brief says so; the
  proxy-side record is what gets measured. Traffic that bypasses the proxy is
  invisible to the benchmark.

### Environment isolation

The agent process receives a **deny-by-default** environment: only variables
listed in `env_allowlist`, plus the config's `env` mapping, plus
`ATOBENCH_EPISODE_ID`. Harness credentials (for example `ANTHROPIC_API_KEY`
or gateway routes) are not forwarded unless you explicitly list them. If your
agent needs its own API key, put it in `env` (or your own secret file outside
the episode tree) rather than broadening the allowlist.

### Launching an episode

From an experiment config:

```yaml
agent:
  driver: command
  command_config: ./my_agent.command.yaml
```

or directly:

```bash
atobench-experiment run --task T3 --baseline B0 --mode proxy \
  --runtime-program <runtime_program.yaml> \
  --target-url http://127.0.0.1:3000 \
  --driver command \
  --agent-command-config my_agent.command.yaml \
  --output-dir logs
```

Paired Native/ATO campaigns select the driver the same way for both
conditions; everything else in Protocol-v3 (assignment, isolation,
fingerprinting) is unchanged.

## Trajectory contract: `agent_session.jsonl`

Every driver emits one session file into the episode workspace, one JSON
object per line, validated against `atobench/schema/agent_session_event.json`.
The event vocabulary is closed:

| Event type | Meaning |
|---|---|
| `session_start` | driver identity |
| `task_brief` | pointer to the task file |
| `assistant_message` | agent-visible text output |
| `tool_call` | one agent action (name + summarized arguments) |
| `tool_result` | summarized outcome of that action |
| `error` | failure marker (fatal or not) |
| `final_report` | the report text (or a pointer to it) |
| `session_end` | exit code and duration |

Every event carries `schema_version`, `episode_id`, `seq` (strictly
increasing), `ts`, `attestation`, and `payload`.

**Redaction rules.** Text payloads are capped; chain-of-thought is not a
representable event type; raw HTTP bodies are never copied into the session
file (they live in the proxy-side record). The session file is safe to
collect and share under the same policy as the rest of an episode.

**Attestation modes.** An agent that emits its own structured event stream
gets `stream_verified` events — the ordering and content are attested by the
agent process. Otherwise the harness writes `adapter_declared` events from
process-level observation (spawn, task brief, report artifact, exit code) and
the file contains no intermediate trajectory. Adapter authors who can emit
`agent_session.jsonl` themselves (configured via `session_path`) should do
so; the harness validates it and records the summary. Claims that cannot be
cross-checked stay the agent's word — the proxy record remains the referee,
and pair validity gates (route attestation, agent-originated work) are
unchanged.

## Isolation layers

| Layer | Mechanism | Applies to |
|---|---|---|
| Environment | deny-by-default `env_allowlist` | all drivers (built-in drivers currently inherit the harness environment; list only what you need for third-party agents) |
| Measurement | mitmproxy is the only measured path; `turns.jsonl` is harness-written | all drivers |
| Failure/state | empty-workspace check per episode, startup watchdog (kills a hung agent before it consumes the budget), hard timeout, fail-closed report artifact | all drivers |
| Network (recommended for untrusted agents) | run the agent inside a container attached to the same Docker network as the target and proxy, with no other egress; the documented compose pattern in `docs/ARCHITECTURE.md` extends naturally | command driver |

For trusted reference agents (Claude Code) the first three layers are active
in the shipped runtime. The fourth is a deployment recommendation when you
do not control the code of the agent under test.

## Adapting the analysis layer

The shipped analysis pipeline (`analysis/`) consumes Claude Code session
transcripts. Its parsers are deliberately coupled to that format because the
frozen cohort was collected with it. The stable integration point for other
agents is `agent_session.jsonl`: a converter from the canonical schema to the
analysis input format is a bounded, mechanical task — the canonical file
carries the same informational content (actions, summaries, report) with a
closed vocabulary, so the converter never needs to understand the agent.

Until a converter exists for your agent, all proxy-side metrics
(`atobench-experiment extract-action-trace`, `audit-behavior`,
`pentest-effect`, `evaluate-pair`) work unchanged, because they never read
agent-internal data.

## Adapter author checklist

- [ ] Config validates: `python -c "from atobench.schema.loader import validate_command_agent_config; import yaml, sys; validate_command_agent_config(yaml.safe_load(open(sys.argv[1])))" my_agent.command.yaml`
- [ ] Agent reaches the target only through `{proxy_url}`.
- [ ] Report artifact appears at `report_path` on success, partial success, and give-up.
- [ ] `env_allowlist` is minimal; no harness credentials leak into the agent.
- [ ] Timeouts set to your agent's realistic budget.
- [ ] Optional: agent emits `agent_session.jsonl` (validate with
      `atobench.agents.session_events.validate_session_file`).
