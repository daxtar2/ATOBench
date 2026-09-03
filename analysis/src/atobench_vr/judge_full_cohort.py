from __future__ import annotations

import argparse
import concurrent.futures
import json
import multiprocessing
import shutil
import sys
import threading
from pathlib import Path
from typing import Any

from .common import GateError, read_json, stage_manifest, utc_now, write_json
from .judge_resume import run as run_resume
from .judge_runner import run_packet


def _looks_like_adjudication_resume_candidate(output_dir: Path) -> bool:
    return (
        (output_dir / "RUN_INCOMPLETE.json").is_file()
        and (output_dir / "reviews").is_dir()
        and (output_dir / "raw_evidence_verifiers").is_dir()
        and (output_dir / "receipts").is_dir()
        and len(list((output_dir / "reviews").glob("*.json"))) == 2
        and len(list((output_dir / "raw_evidence_verifiers").glob("*.json"))) == 2
        and len(list((output_dir / "receipts").glob("*.json"))) == 4
    )


def _has_partial_state(output_dir: Path) -> bool:
    return output_dir.exists() and any(output_dir.iterdir())


def _archive_partial(output_dir: Path, archive_root: Path, packet_id: str) -> Path:
    stamp = utc_now().replace(":", "").replace("+00:00", "Z")
    archive_dir = archive_root / f"{output_dir.name}__{packet_id}__{stamp}"
    archive_dir.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(output_dir), str(archive_dir))
    return archive_dir


class _ProgressMonitor:
    """Live progress display for the full-cohort runner.

    Workers emit ``start`` heartbeats through a managed queue; the main process
    increments completion counts when futures finalize. The display updates in
    place on TTYs and prints one line per update otherwise.
    """

    def __init__(self, total: int, enabled: bool) -> None:
        self.total = total
        self.enabled = enabled
        self.completed = 0
        self.skipped = 0
        self.failed = 0
        self.running: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()
        self._queue: Any | None = None
        self._manager: Any | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._tty = sys.stdout.isatty()

    def start(self) -> Any | None:
        if not self.enabled:
            return None
        self._manager = multiprocessing.Manager()
        self._queue = self._manager.Queue()
        self._thread = threading.Thread(target=self._consume, daemon=True)
        self._thread.start()
        return self._queue

    def _consume(self) -> None:
        import queue as queue_module

        while not self._stop.is_set() or not self._queue.empty():
            try:
                msg = self._queue.get(timeout=0.2)
            except queue_module.Empty:
                continue
            if msg.get("type") == "start":
                worker = str(msg.get("worker", "unknown"))
                with self._lock:
                    self.running[worker] = msg
                self._render()

    def update(self, event: dict[str, Any]) -> None:
        if not self.enabled:
            return
        packet_id = event.get("packet_id")
        with self._lock:
            for worker, info in list(self.running.items()):
                if info.get("packet_id") == packet_id:
                    self.running.pop(worker, None)
                    break
            self.completed += 1
            if event.get("mode") == "skip_completed":
                self.skipped += 1
            if event.get("status") == "failed":
                self.failed += 1
        self._render()

    def _render(self) -> None:
        if not self.enabled:
            return
        with self._lock:
            line = self._format_line()
        if self._tty:
            sys.stdout.write("\r" + line)
            sys.stdout.flush()
        else:
            sys.stdout.write(line + "\n")
            sys.stdout.flush()

    def _format_line(self) -> str:
        running_count = len(self.running)
        pct = (self.completed / self.total * 100) if self.total else 0.0
        parts = [
            f"[{self.completed}/{self.total}] {pct:.1f}%",
            f"running:{running_count}",
            f"completed:{self.completed}",
            f"skipped:{self.skipped}",
            f"failed:{self.failed}",
        ]
        if self.running:
            labels = []
            for name, info in sorted(self.running.items()):
                short = (
                    name.replace("ForkProcess-", "W")
                    .replace("SpawnProcess-", "W")
                    .replace("MainProcess", "Wmain")
                )
                labels.append(f"{short}:{info.get('packet_id', '?')}")
            parts.append("| " + " ".join(labels))
        return " | ".join(parts)

    def stop(self) -> None:
        if not self.enabled:
            return
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)
        if self._tty:
            sys.stdout.write("\n")
            sys.stdout.flush()
        else:
            self._render()


def _run_single_packet(
    row: dict[str, Any],
    *,
    absolute_index: int,
    selected_index: int,
    selected_total: int,
    packets_root: Path,
    output_root: Path,
    archive_root: Path,
    config_path: Path,
    lock_path: Path,
    agents_dir: Path,
    allow_judge_calls: bool,
    executable_override: str | None = None,
    heartbeat_queue: Any | None = None,
) -> dict[str, Any]:
    """Process one packet and return an event record.

    This is a top-level function so it can be dispatched by a process pool.
    """
    relative_path = str(row["relative_path"])
    packet_dir = packets_root / relative_path
    output_dir = output_root / relative_path
    packet_id = str(row["packet_id"])
    dimension = str(row["dimension"])

    event: dict[str, Any] = {
        "index": absolute_index,
        "selected_index": selected_index,
        "selected_total": selected_total,
        "packet_id": packet_id,
        "dimension": dimension,
        "relative_path": relative_path,
        "started_at": utc_now(),
    }

    try:
        if (output_dir / "judgment_bundle.json").is_file():
            event["mode"] = "skip_completed"
        elif _looks_like_adjudication_resume_candidate(output_dir):
            event["mode"] = "resume_adjudication"
        else:
            if _has_partial_state(output_dir):
                archived_to = _archive_partial(output_dir, archive_root, packet_id)
                event["archived_partial_dir"] = str(archived_to.resolve())
            event["mode"] = "fresh_run"

        if heartbeat_queue is not None:
            try:
                heartbeat_queue.put(
                    {
                        "type": "start",
                        "absolute_index": absolute_index,
                        "packet_id": packet_id,
                        "dimension": dimension,
                        "relative_path": relative_path,
                        "mode": event.get("mode"),
                        "worker": multiprocessing.current_process().name,
                    },
                    timeout=1,
                )
            except Exception:
                pass

        if event["mode"] == "resume_adjudication":
            run_resume(
                packet_dir=packet_dir,
                incomplete_dir=output_dir,
                output_dir=output_dir,
                config_path=config_path,
                lock_path=lock_path,
                agents_dir=agents_dir,
                allow_judge_calls=allow_judge_calls,
                new_version=False,
            )
        elif event["mode"] == "fresh_run":
            run_packet(
                packet_dir=packet_dir,
                output_dir=output_dir,
                config_path=config_path,
                lock_path=lock_path,
                agents_dir=agents_dir,
                allow_judge_calls=allow_judge_calls,
                synthetic_smoke=False,
                executable_override=executable_override,
                new_version=False,
                dry_run=False,
                allow_adjudication=True,
            )
        event["status"] = "complete"
    except Exception as exc:
        event["status"] = "failed"
        event["error"] = str(exc)
    finally:
        event["finished_at"] = utc_now()
    return event


def run(
    *,
    packet_manifest_path: Path,
    packets_root: Path,
    output_root: Path,
    config_path: Path,
    lock_path: Path,
    agents_dir: Path,
    allow_judge_calls: bool,
    start_index: int,
    limit: int | None,
    continue_on_error: bool,
    workers: int = 1,
    executable_override: str | None = None,
    progress: bool = False,
) -> dict[str, Any]:
    packet_manifest = read_json(packet_manifest_path)
    rows = list(packet_manifest.get("packets") or [])
    if not rows:
        raise GateError("packet manifest contains no packets")
    if start_index < 1:
        raise GateError("--start-index must be >= 1")
    if limit is not None and limit < 1:
        raise GateError("--limit must be >= 1 when provided")
    if workers < 1:
        raise GateError("--workers must be >= 1")

    selected = rows[start_index - 1 :]
    if limit is not None:
        selected = selected[:limit]
    if not selected:
        raise GateError("no packets selected by start-index/limit")

    output_root.mkdir(parents=True, exist_ok=True)
    archive_root = output_root / "_archived_partials"
    progress_path = output_root / "full_cohort_progress.json"
    summary_path = output_root / "full_cohort_summary.json"
    event_log_path = output_root / "full_cohort_event_log.jsonl"

    monitor = _ProgressMonitor(total=len(selected), enabled=progress)
    heartbeat_queue = monitor.start()

    stats = {
        "selected_packets": len(selected),
        "completed_packets": 0,
        "skipped_completed": 0,
        "resumed_adjudication": 0,
        "fresh_runs": 0,
        "archived_partials": 0,
        "failed_packets": 0,
    }
    failures: list[dict[str, Any]] = []

    def finalize_event(event: dict[str, Any]) -> None:
        mode = event.get("mode")
        status = event.get("status")
        if status == "failed":
            stats["failed_packets"] += 1
            failures.append(
                {
                    "index": event["index"],
                    "packet_id": event["packet_id"],
                    "dimension": event["dimension"],
                    "relative_path": event["relative_path"],
                    "error": event.get("error"),
                }
            )
        elif mode == "skip_completed":
            stats["skipped_completed"] += 1
        elif mode == "resume_adjudication":
            stats["resumed_adjudication"] += 1
            stats["completed_packets"] += 1
        elif mode == "fresh_run":
            if "archived_partial_dir" in event:
                stats["archived_partials"] += 1
            stats["fresh_runs"] += 1
            stats["completed_packets"] += 1

        event["finished_at"] = utc_now()
        monitor.update(event)
        with event_log_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, ensure_ascii=False, sort_keys=True))
            handle.write("\n")
        write_json(
            progress_path,
            {
                "schema_version": "atobench.full_cohort_progress.v1",
                "updated_at": utc_now(),
                "packet_manifest_path": str(packet_manifest_path.resolve()),
                "packets_root": str(packets_root.resolve()),
                "output_root": str(output_root.resolve()),
                "start_index": start_index,
                "limit": limit,
                "workers": workers,
                "stats": stats,
                "last_event": event,
                "failures": failures[-20:],
            },
        )

    work_items = [
        (
            row,
            start_index + local_index - 1,
            local_index,
            len(selected),
        )
        for local_index, row in enumerate(selected, 1)
    ]

    try:
        if workers == 1:
            for row, absolute_index, selected_index, selected_total in work_items:
                event = _run_single_packet(
                    row,
                    absolute_index=absolute_index,
                    selected_index=selected_index,
                    selected_total=selected_total,
                    packets_root=packets_root,
                    output_root=output_root,
                    archive_root=archive_root,
                    config_path=config_path,
                    lock_path=lock_path,
                    agents_dir=agents_dir,
                    allow_judge_calls=allow_judge_calls,
                    executable_override=executable_override,
                    heartbeat_queue=heartbeat_queue,
                )
                finalize_event(event)
                if event["status"] == "failed" and not continue_on_error:
                    break
        else:
            with concurrent.futures.ProcessPoolExecutor(max_workers=workers) as executor:
                futures = {
                    executor.submit(
                        _run_single_packet,
                        row,
                        absolute_index=absolute_index,
                        selected_index=selected_index,
                        selected_total=selected_total,
                        packets_root=packets_root,
                        output_root=output_root,
                        archive_root=archive_root,
                        config_path=config_path,
                        lock_path=lock_path,
                        agents_dir=agents_dir,
                        allow_judge_calls=allow_judge_calls,
                        executable_override=executable_override,
                        heartbeat_queue=heartbeat_queue,
                    ): (row, absolute_index, selected_index, selected_total)
                    for row, absolute_index, selected_index, selected_total in work_items
                }
                for future in concurrent.futures.as_completed(futures):
                    try:
                        event = future.result()
                    except Exception as exc:
                        row, absolute_index, selected_index, selected_total = futures[future]
                        event = {
                            "index": absolute_index,
                            "selected_index": selected_index,
                            "selected_total": selected_total,
                            "packet_id": str(row["packet_id"]),
                            "dimension": str(row["dimension"]),
                            "relative_path": str(row["relative_path"]),
                            "started_at": utc_now(),
                            "status": "failed",
                            "error": str(exc),
                        }
                    finalize_event(event)
                    if event["status"] == "failed" and not continue_on_error:
                        executor.shutdown(wait=False, cancel_futures=True)
                        break
    finally:
        monitor.stop()

    status = "complete" if not failures else "failed"
    summary = {
        "schema_version": "atobench.full_cohort_judge_summary.v1",
        "created_at": utc_now(),
        "packet_manifest_path": str(packet_manifest_path.resolve()),
        "packets_root": str(packets_root.resolve()),
        "output_root": str(output_root.resolve()),
        "start_index": start_index,
        "limit": limit,
        "workers": workers,
        "status": status,
        "stats": stats,
        "failures": failures,
    }
    write_json(summary_path, summary)
    manifest = stage_manifest(
        "08m_run_full_cohort_judges",
        [
            packet_manifest_path,
            config_path,
            lock_path,
            *sorted(agents_dir.glob("*.md")),
        ],
        [progress_path, summary_path, event_log_path],
        "complete" if status == "complete" else "blocked",
        dry_run=False,
        details={
            "selected_packets": len(selected),
            "completed_packets": stats["completed_packets"],
            "skipped_completed": stats["skipped_completed"],
            "resumed_adjudication": stats["resumed_adjudication"],
            "fresh_runs": stats["fresh_runs"],
            "archived_partials": stats["archived_partials"],
            "failed_packets": stats["failed_packets"],
            "workers": workers,
        },
    )
    write_json(output_root / "stage_manifest.json", manifest)
    return summary


def cli(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--packet-manifest", type=Path, required=True)
    parser.add_argument("--packets-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--lock", type=Path, required=True)
    parser.add_argument("--agents-dir", type=Path, required=True)
    parser.add_argument("--allow-judge-calls", action="store_true")
    parser.add_argument("--start-index", type=int, default=1)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--continue-on-error", action="store_true")
    parser.add_argument(
        "--workers",
        type=int,
        default=1,
        help="Number of packets to process concurrently. Each packet uses an independent Claude Code process.",
    )
    parser.add_argument("--claude-executable")
    parser.add_argument(
        "--progress",
        action="store_true",
        help="Show a live progress bar with per-worker status on stdout.",
    )
    args = parser.parse_args(argv)
    try:
        result = run(
            packet_manifest_path=args.packet_manifest,
            packets_root=args.packets_root,
            output_root=args.output,
            config_path=args.config,
            lock_path=args.lock,
            agents_dir=args.agents_dir,
            allow_judge_calls=args.allow_judge_calls,
            start_index=args.start_index,
            limit=args.limit,
            continue_on_error=args.continue_on_error,
            workers=args.workers,
            executable_override=args.claude_executable,
            progress=args.progress,
        )
    except GateError as exc:
        parser.exit(2, f"BLOCKED: {exc}\n")
    print(result)
    return 0
