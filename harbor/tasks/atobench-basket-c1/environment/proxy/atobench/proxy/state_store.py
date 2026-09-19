"""Per-episode state store for stateful primitives.

Stateful primitives (corrupt_belief, exhaustion_trap, induce_loop) maintain
D_t state across turns. The state_store provides:

- get(episode_id, primitive, key) -> any
- set(episode_id, primitive, key, value) -> None
- inc(episode_id, primitive, key, by=1) -> int  # returns new value
- snapshot(episode_id, primitive) -> dict       # for D_t_snapshot in tag
- flush(episode_id) -> None                     # persist to disk
- restore(episode_id) -> None                   # load from disk on startup

Persistence: state is JSON-serialized to /logs/state_<episode_id>.json.
Flushed every N turns (default 5) and on episode end. On proxy restart,
state is restored from disk if the file exists.

In-memory store is a nested dict: store[episode_id][primitive][key] = value
"""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any


class StateStore:
    def __init__(self, log_dir: str | Path = "logs", flush_every: int = 5) -> None:
        self.log_dir = Path(log_dir)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self._store: dict[str, dict[str, dict[str, Any]]] = {}
        self._turn_counts: dict[str, int] = {}  # episode_id -> turns since last flush
        self._lock = threading.Lock()
        self._flush_every = flush_every

    def _state_path(self, episode_id: str) -> Path:
        return self.log_dir / f"state_{episode_id}.json"

    def get(self, episode_id: str, primitive: str, key: str, default: Any = None) -> Any:
        with self._lock:
            ep = self._store.get(episode_id, {})
            prim = ep.get(primitive, {})
            return prim.get(key, default)

    def set(self, episode_id: str, primitive: str, key: str, value: Any) -> None:
        with self._lock:
            self._store.setdefault(episode_id, {}).setdefault(primitive, {})[key] = value

    def inc(self, episode_id: str, primitive: str, key: str, by: int = 1, default: int = 0) -> int:
        with self._lock:
            ep = self._store.setdefault(episode_id, {})
            prim = ep.setdefault(primitive, {})
            new_val = prim.get(key, default) + by
            prim[key] = new_val
            return new_val

    def snapshot(self, episode_id: str, primitive: str) -> dict[str, Any]:
        with self._lock:
            ep = self._store.get(episode_id, {})
            prim = ep.get(primitive, {})
            return dict(prim)  # shallow copy — values are JSON-serializable primitives

    def tick(self, episode_id: str) -> bool:
        """Increment the turn counter, flush if it's time. Returns True if flushed."""
        with self._lock:
            self._turn_counts[episode_id] = self._turn_counts.get(episode_id, 0) + 1
            should_flush = self._turn_counts[episode_id] >= self._flush_every
        if should_flush:
            self.flush(episode_id)
        return should_flush

    def flush(self, episode_id: str) -> None:
        """Persist state for an episode to disk."""
        with self._lock:
            ep_state = self._store.get(episode_id, {})
            data = json.dumps(ep_state, ensure_ascii=False, indent=2)
            path = self._state_path(episode_id)
            tmp = path.with_suffix(".tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(data)
            os.replace(tmp, path)
            self._turn_counts[episode_id] = 0

    def restore(self, episode_id: str) -> bool:
        """Load state from disk. Returns True if restored, False if no file."""
        path = self._state_path(episode_id)
        if not path.exists():
            return False
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError):
            return False
        with self._lock:
            self._store[episode_id] = data
        return True

    def clear(self, episode_id: str) -> None:
        """Remove state for an episode (in-memory and on-disk)."""
        with self._lock:
            self._store.pop(episode_id, None)
            self._turn_counts.pop(episode_id, None)
        path = self._state_path(episode_id)
        if path.exists():
            try:
                path.unlink()
            except OSError:
                pass
