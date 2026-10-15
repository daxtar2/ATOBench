"""SQLite persistence for the data-change-approval slice."""
from __future__ import annotations

import os
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path

DATA_DIR = Path(os.environ.get("DATA_DIR", "/tmp/change-approval-data"))
DB_PATH = DATA_DIR / "slice.db"
_lock = threading.RLock()


def _connect() -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


@contextmanager
def db():
    with _lock:
        conn = _connect()
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()


SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
  id INTEGER PRIMARY KEY,
  login TEXT UNIQUE NOT NULL,
  display_name TEXT NOT NULL,
  role TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS target_orders (
  record_id TEXT PRIMARY KEY,
  status TEXT NOT NULL,
  customer TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS change_orders (
  order_id INTEGER PRIMARY KEY AUTOINCREMENT,
  workflow_instance_id INTEGER UNIQUE NOT NULL,
  comment TEXT NOT NULL,
  classify TEXT NOT NULL,
  target_db TEXT NOT NULL,
  target_table TEXT NOT NULL,
  target_record_id TEXT NOT NULL,
  exec_sql TEXT NOT NULL,
  desired_status TEXT NOT NULL,
  estimate_affect_rows INTEGER NOT NULL,
  exec_mode TEXT NOT NULL,
  committer_id INTEGER NOT NULL,
  status_code TEXT NOT NULL,
  status_desc TEXT NOT NULL,
  plugin_type TEXT NOT NULL DEFAULT 'DC_COMMON',
  list_status_code TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  FOREIGN KEY (committer_id) REFERENCES users(id)
);

CREATE TABLE IF NOT EXISTS approvals (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  order_id INTEGER NOT NULL,
  workflow_instance_id INTEGER NOT NULL,
  approval_type TEXT NOT NULL,
  comment TEXT,
  approver_id INTEGER NOT NULL,
  created_at TEXT NOT NULL,
  FOREIGN KEY (order_id) REFERENCES change_orders(order_id),
  FOREIGN KEY (approver_id) REFERENCES users(id)
);

CREATE TABLE IF NOT EXISTS executions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  order_id INTEGER UNIQUE NOT NULL,
  executor_id INTEGER NOT NULL,
  result TEXT NOT NULL,
  progress TEXT NOT NULL,
  result_artifact TEXT,
  detail TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  FOREIGN KEY (order_id) REFERENCES change_orders(order_id),
  FOREIGN KEY (executor_id) REFERENCES users(id)
);

CREATE TABLE IF NOT EXISTS feature_flags (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS todo_inbox (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id INTEGER NOT NULL,
  order_id INTEGER NOT NULL,
  kind TEXT NOT NULL,
  active INTEGER NOT NULL DEFAULT 1,
  FOREIGN KEY (user_id) REFERENCES users(id),
  FOREIGN KEY (order_id) REFERENCES change_orders(order_id)
);
"""


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
