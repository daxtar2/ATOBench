"""Seed / reset for the data-change-approval slice."""
from __future__ import annotations

from datetime import datetime, timezone

from db import db, init_schema

USERS = [
    (1, "alice", "Alice Chen", "developer"),
    (2, "bob", "Bob Wang", "approver"),
    (3, "carol", "Carol Li", "free_ops_developer"),
]

DEFAULT_FLAGS = {
    "approval_path_enabled": "true",
    "free_ops_enabled": "true",
    "list_status_lag": "false",
    "todo_lag": "false",
}


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def reset_database() -> None:
    with db() as conn:
        init_schema(conn)
        for table in (
            "todo_inbox",
            "executions",
            "approvals",
            "change_orders",
            "target_orders",
            "feature_flags",
            "users",
        ):
            conn.execute(f"DELETE FROM {table}")
        # Reset AUTOINCREMENT counters so oracles get stable ids after reset.
        conn.execute("DELETE FROM sqlite_sequence")
        conn.executemany(
            "INSERT INTO users (id, login, display_name, role) VALUES (?, ?, ?, ?)",
            USERS,
        )
        conn.execute(
            "INSERT INTO target_orders (record_id, status, customer) VALUES (?, ?, ?)",
            ("ORD-1001", "PENDING", "Acme Retail"),
        )
        for key, value in DEFAULT_FLAGS.items():
            conn.execute(
                "INSERT INTO feature_flags (key, value) VALUES (?, ?)",
                (key, value),
            )


def get_flag(conn, key: str) -> bool:
    row = conn.execute(
        "SELECT value FROM feature_flags WHERE key = ?", (key,)
    ).fetchone()
    return bool(row) and row["value"].lower() in {"1", "true", "yes", "on"}


def set_flag(conn, key: str, value: bool) -> None:
    conn.execute(
        "INSERT INTO feature_flags (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, "true" if value else "false"),
    )


def user_by_login(conn, login: str):
    return conn.execute(
        "SELECT * FROM users WHERE login = ?", (login,)
    ).fetchone()


def user_by_id(conn, user_id: int):
    return conn.execute(
        "SELECT * FROM users WHERE id = ?", (user_id,)
    ).fetchone()
