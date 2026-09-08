from __future__ import annotations

import json
import os
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path

import jsonschema

from atobench.agents.command_agent import (
    CommandAgent,
    build_command_agent_env,
    expand_placeholders,
    load_command_agent_config,
)
from atobench.agents.proxy_runner import ProxyEpisodeRunner
from atobench.agents.session_events import (
    MAX_EVENT_TEXT_CHARS,
    SessionEventWriter,
    claude_stream_to_session_events,
    validate_session_file,
)
from atobench.experiment.cycle import _proxy_driver
from atobench.schema.loader import validate_agent_session_event


def _minimal_config(tmpdir: Path, *, command: list[str], report_path: str, **extra: object) -> Path:
    cfg = {
        "schema_version": "atobench.command_agent_config.v1",
        "name": "test-agent",
        "command": command,
        "report_path": report_path,
    }
    cfg.update(extra)
    path = tmpdir / "command_agent.yaml"
    path.write_text(json.dumps(cfg), encoding="utf-8")
    return path


class SessionEventWriterTests(unittest.TestCase):
    def test_writer_emits_schema_valid_sequence(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "agent_session.jsonl"
            writer = SessionEventWriter(path, "ep_test", attestation_mode="stream_verified")
            writer.append("session_start", {"driver": "claude-code"})
            writer.append(
                "tool_call",
                {"tool_name": "Bash", "arguments_summary": "curl http://proxy", "truncated": False},
            )
            writer.append_text_event("final_report", "FINAL_FINDINGS: done")
            writer.append_session_end(0, 12.5)
            lines = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
            self.assertEqual([event["seq"] for event in lines], [0, 1, 2, 3])
            self.assertEqual([event["event_type"] for event in lines], [
                "session_start", "tool_call", "final_report", "session_end",
            ])
            for event in lines:
                validate_agent_session_event(event)
            self.assertEqual(lines[0]["attestation"], {"mode": "stream_verified"})
            self.assertEqual(lines[2]["payload"]["truncated"], False)

    def test_writer_rejects_unknown_event_type(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            writer = SessionEventWriter(Path(tmpdir) / "s.jsonl", "ep_test")
            with self.assertRaises(jsonschema.ValidationError):
                writer.append("chain_of_thought", {"text": "hidden"})

    def test_writer_rejects_unknown_payload_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            writer = SessionEventWriter(Path(tmpdir) / "s.jsonl", "ep_test")
            with self.assertRaises(jsonschema.ValidationError):
                writer.append("session_start", {"driver": "x", "raw_body": "http response dump"})

    def test_writer_truncates_oversized_text(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "s.jsonl"
            writer = SessionEventWriter(path, "ep_test")
            event = writer.append_text_event("assistant_message", "x" * (MAX_EVENT_TEXT_CHARS * 2))
            self.assertTrue(event["payload"]["truncated"])
            validate_agent_session_event(event)


class ClaudeStreamTranslationTests(unittest.TestCase):
    def test_translation_drops_thinking_and_resolves_tool_names(self) -> None:
        stream = "\n".join((
            json.dumps({"type": "system", "subtype": "init"}),
            json.dumps({"type": "assistant", "message": {"model": "glm-5.2", "content": [
                {"type": "thinking", "thinking": "secret reasoning"},
                {"type": "text", "text": "probing the login endpoint"},
                {"type": "tool_use", "id": "toolu_1", "name": "Bash", "input": {"command": "curl -s http://p/"}},
            ]}}),
            json.dumps({"type": "user", "message": {"content": [
                {"type": "tool_result", "tool_use_id": "toolu_1", "content": [{"type": "text", "text": "200 OK body"}]},
            ]}}),
            json.dumps({"type": "result", "subtype": "success", "is_error": False, "result": "report text"}),
        ))
        events = claude_stream_to_session_events(stream)
        types = [event["event_type"] for event in events]
        self.assertEqual(types, ["session_start", "assistant_message", "tool_call", "tool_result", "final_report"])
        self.assertNotIn("secret reasoning", json.dumps(events))
        self.assertEqual(events[2]["payload"]["tool_name"], "Bash")
        self.assertEqual(events[2]["payload"]["tool_use_id"], "toolu_1")
        self.assertEqual(events[3]["payload"]["tool_name"], "Bash")
        self.assertEqual(events[4]["payload"]["text"], "report text")

    def test_stream_error_result_yields_error_event(self) -> None:
        stream = json.dumps({"type": "result", "subtype": "error_during_execution", "is_error": True})
        events = claude_stream_to_session_events(stream)
        self.assertEqual([event["event_type"] for event in events], ["error"])
        self.assertTrue(events[0]["payload"]["fatal"])

    def test_validate_session_file_counts_invalid_lines(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "s.jsonl"
            writer = SessionEventWriter(path, "ep_test")
            writer.append("session_start", {"driver": "x"})
            with path.open("a", encoding="utf-8") as handle:
                handle.write("not json at all\n")
            summary = validate_session_file(path)
            self.assertEqual(summary["event_count"], 1)
            self.assertEqual(summary["invalid_line_count"], 1)
            self.assertEqual(summary["episode_ids"], ["ep_test"])


class CommandAgentConfigTests(unittest.TestCase):
    def test_load_applies_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = _minimal_config(Path(tmpdir), command=["my-agent"], report_path="{output_dir}/report.md")
            cfg = load_command_agent_config(path)
            self.assertEqual(cfg.name, "test-agent")
            self.assertEqual(cfg.timeout_s, 900)
            self.assertEqual(cfg.startup_timeout_s, 120)
            self.assertIsNone(cfg.env_allowlist)
            self.assertEqual(cfg.report_path, "{output_dir}/report.md")

    def test_load_rejects_unknown_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = _minimal_config(Path(tmpdir), command=["a"], report_path="r", api_key="SHOULD_NOT_PASS")
            with self.assertRaises(jsonschema.ValidationError):
                load_command_agent_config(path)

    def test_load_rejects_missing_command(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = _minimal_config(Path(tmpdir), command=["a"], report_path="r")
            data = json.loads(path.read_text(encoding="utf-8"))
            del data["command"]
            path.write_text(json.dumps(data), encoding="utf-8")
            with self.assertRaises(jsonschema.ValidationError):
                load_command_agent_config(path)

    def test_expand_placeholders_rejects_unknown_tokens(self) -> None:
        with self.assertRaisesRegex(ValueError, "unknown placeholder"):
            expand_placeholders("run --key {api_key}", {})
        self.assertEqual(
            expand_placeholders("run {episode_id} at {proxy_url}", {"episode_id": "ep_1", "proxy_url": "http://p"}),
            "run ep_1 at http://p",
        )

    def test_env_is_deny_by_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = _minimal_config(Path(tmpdir), command=["a"], report_path="r")
            cfg = load_command_agent_config(path)
            host = {
                "PATH": "/usr/bin",
                "ANTHROPIC_API_KEY": "test-credential-value-not-a-real-key",
                "ATOBENCH_LOG_DIR": "/tmp/logs",
            }
            env = build_command_agent_env(cfg, "ep_1", host_env=host)
            self.assertEqual(env["PATH"], "/usr/bin")
            self.assertNotIn("ANTHROPIC_API_KEY", env)
            self.assertNotIn("ATOBENCH_LOG_DIR", env)
            self.assertEqual(env["ATOBENCH_EPISODE_ID"], "ep_1")

    def test_env_honors_custom_allowlist_and_overrides(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            path = _minimal_config(
                Path(tmpdir),
                command=["a"],
                report_path="r",
                env_allowlist=["MY_AGENT_TOKEN"],
                env={"MY_AGENT_MODE": "pentest"},
            )
            cfg = load_command_agent_config(path)
            env = build_command_agent_env(cfg, "ep_1", host_env={"MY_AGENT_TOKEN": "t", "PATH": "/usr/bin"})
            self.assertEqual(env["MY_AGENT_TOKEN"], "t")
            self.assertNotIn("PATH", env)
            self.assertEqual(env["MY_AGENT_MODE"], "pentest")


class CommandAgentRunTests(unittest.TestCase):
    def _fake_agent_script(self, tmpdir: Path) -> Path:
        script = tmpdir / "fake_agent.py"
        script.write_text(
            textwrap.dedent(
                """
                import json, os, sys
                from pathlib import Path
                output_dir = Path(sys.argv[sys.argv.index("--output-dir") + 1])
                probe = {
                    "episode_id": os.environ.get("ATOBENCH_EPISODE_ID"),
                    "secret_seen": os.environ.get("ATOBENCH_TEST_SECRET"),
                    "task_file_first_line": Path(sys.argv[sys.argv.index("--task-file") + 1]).read_text().splitlines()[0],
                }
                (output_dir / "probe.json").write_text(json.dumps(probe), encoding="utf-8")
                session = {
                    "schema_version": "atobench.agent_session_event.v1",
                    "episode_id": probe["episode_id"],
                    "seq": 0,
                    "ts": "2026-09-07T00:00:00.000+00:00",
                    "event_type": "assistant_message",
                    "attestation": {"mode": "adapter_declared"},
                    "payload": {"text": "agent worked", "truncated": False},
                }
                (output_dir / "trajectory.jsonl").write_text(json.dumps(session) + "\\n", encoding="utf-8")
                report = output_dir / "report.md"
                report.write_text("## Findings\\n\\nAttack surface verified.\\n", encoding="utf-8")
                """
            ).strip(),
            encoding="utf-8",
        )
        return script

    def test_command_agent_runs_to_report_with_env_isolation(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir = Path(tmpdir)
            script = self._fake_agent_script(tmpdir)
            config_path = _minimal_config(
                tmpdir,
                command=[sys.executable, str(script), "--output-dir", "{output_dir}", "--task-file", "{task_file}"],
                report_path="{output_dir}/report.md",
                session_path="{output_dir}/trajectory.jsonl",
            )
            workspace = tmpdir / "workspace"
            episode_id = "ep_command_test_0001"
            os.environ["ATOBENCH_TEST_SECRET"] = "test-secret-value-should-not-leak"
            self.addCleanup(os.environ.pop, "ATOBENCH_TEST_SECRET", None)
            agent = CommandAgent(
                episode_spec={"episode_id": episode_id, "target_url": "http://127.0.0.1:3000"},
                proxy_urls={"target": "http://127.0.0.1:8100"},
                command_config=config_path,
                workspace=workspace,
            )
            agent.reset({})
            report = agent.run()
            self.assertTrue(report.final_report_text.startswith("## Findings"))
            self.assertEqual(report.raw_metadata["returncode"], 0)
            self.assertEqual(report.raw_metadata["agent_session"]["event_count"], 1)
            self.assertEqual(report.raw_metadata["agent_session"]["invalid_line_count"], 0)

            probe = json.loads((workspace / "probe.json").read_text(encoding="utf-8"))
            self.assertEqual(probe["episode_id"], episode_id)
            self.assertIsNone(probe["secret_seen"])
            self.assertEqual(probe["task_file_first_line"], "ATOBench pentest audit engagement.")

            session_lines = [json.loads(line) for line in (workspace / "agent_session.jsonl").read_text(encoding="utf-8").splitlines()]
            self.assertEqual([event["event_type"] for event in session_lines], [
                "session_start", "task_brief", "final_report", "session_end",
            ])
            self.assertTrue(all(event["episode_id"] == episode_id for event in session_lines))
            self.assertEqual(session_lines[0]["attestation"], {"mode": "adapter_declared"})

            task_prompt = (workspace / "task_prompt.txt").read_text(encoding="utf-8")
            self.assertIn(episode_id, task_prompt)
            self.assertIn("http://127.0.0.1:8100", task_prompt)
            self.assertIn(str(workspace / "report.md"), task_prompt)

    def test_command_agent_missing_report_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir = Path(tmpdir)
            config_path = _minimal_config(
                tmpdir,
                command=[sys.executable, "-c", "import time; time.sleep(0.1)"],
                report_path="{output_dir}/never_written.md",
            )
            workspace = tmpdir / "workspace"
            agent = CommandAgent(
                episode_spec={"episode_id": "ep_missing_report"},
                proxy_urls={"target": "http://127.0.0.1:8100"},
                command_config=config_path,
                workspace=workspace,
            )
            agent.reset({})
            report = agent.run()
            self.assertTrue(report.final_report_text.startswith("PARSE_FAIL"))
            session_lines = [json.loads(line) for line in (workspace / "agent_session.jsonl").read_text(encoding="utf-8").splitlines()]
            types = [event["event_type"] for event in session_lines]
            self.assertIn("error", types)
            self.assertEqual(types[-1], "session_end")

    def test_command_agent_timeout(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir = Path(tmpdir)
            config_path = _minimal_config(
                tmpdir,
                command=[sys.executable, "-c", "import time; time.sleep(30)"],
                report_path="{output_dir}/report.md",
                timeout_s=2,
            )
            workspace = tmpdir / "workspace"
            agent = CommandAgent(
                episode_spec={"episode_id": "ep_timeout"},
                proxy_urls={"target": "http://127.0.0.1:8100"},
                command_config=config_path,
                workspace=workspace,
                timeout_s=2,
            )
            agent.reset({})
            started = time.monotonic()
            report = agent.run()
            self.assertLess(time.monotonic() - started, 20)
            self.assertTrue(report.final_report_text.startswith("SUBAGENT_TIMEOUT"))
            self.assertTrue((workspace / "task_prompt.txt").exists())

    def test_command_agent_requires_config(self) -> None:
        with self.assertRaisesRegex(ValueError, "command_config"):
            CommandAgent(
                episode_spec={"episode_id": "ep_x"},
                proxy_urls={"target": "http://127.0.0.1:8100"},
            )


class DriverPlumbingTests(unittest.TestCase):
    def test_proxy_driver_maps_command_aliases(self) -> None:
        self.assertEqual(_proxy_driver("command"), "command")
        self.assertEqual(_proxy_driver("command-agent"), "command")
        self.assertEqual(_proxy_driver("command_agent"), "command")
        self.assertEqual(_proxy_driver("agentic-pentest-benchmark"), "agentic-pentest-benchmark")
        self.assertEqual(_proxy_driver("claude-code"), "subagent")

    def test_runner_command_driver_requires_config(self) -> None:
        runner = ProxyEpisodeRunner(
            deception_config_path=None,
            runtime_program_path=None,
            target_url="http://127.0.0.1:3000",
            proxy_port=8100,
            task_id="T3",
            baseline="B0",
            episode_id="ep_nocommand",
            driver="command",
        )
        result = runner._spawn_command_agent()
        self.assertTrue(result["final_report_text"].startswith("PARSE_FAIL"))
        self.assertEqual(result["raw"]["error"], "missing_command_config")

    def test_session_artifact_skipped_without_isolated_workspace(self) -> None:
        runner = ProxyEpisodeRunner(
            deception_config_path=None,
            runtime_program_path=None,
            target_url="http://127.0.0.1:3000",
            proxy_port=8100,
            task_id="T3",
            baseline="B0",
            episode_id="ep_noiso",
        )
        self.assertIsNone(runner._write_agent_session_from_stream("", returncode=0, duration_s=1.0))
        self.assertIsNone(runner._write_agent_session_json_result({}, returncode=0, duration_s=1.0))


if __name__ == "__main__":
    unittest.main()
