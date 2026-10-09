"""Database connection and lifecycle management for SQLite in WAL mode."""

from __future__ import annotations

import sqlite3
from typing import Dict, Optional

DEFAULT_WORKOUTS: Dict[str, dict] = {
    "workout_123": {"owner": "user_123", "intensity": 100},
    "workout_456": {"owner": "user_999", "intensity": 100},
    "workout_789": {"owner": "user_123", "intensity": 15},
}

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS workouts (
    id TEXT PRIMARY KEY,
    owner TEXT NOT NULL,
    intensity INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS idempotency_ledger (
    user_id TEXT NOT NULL,
    request_id TEXT NOT NULL,
    status TEXT NOT NULL,
    response_json TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    lease_expires_at REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (user_id, request_id)
);

CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    request_id TEXT NOT NULL,
    actor TEXT NOT NULL,
    workout_id TEXT NOT NULL,
    new_intensity INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS workout_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type TEXT NOT NULL,
    workout_id TEXT NOT NULL,
    actor TEXT NOT NULL,
    request_id TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    ts REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_ledger_stale 
ON idempotency_ledger(status, lease_expires_at);

CREATE INDEX IF NOT EXISTS idx_workout_events 
ON workout_events(workout_id, ts);
"""


def init_database(conn: sqlite3.Connection, seed_workouts: Optional[Dict[str, dict]] = None) -> None:
    """Initialize tables, apply migrations, and seed default data if database is empty."""
    with conn:
        conn.executescript(SCHEMA_SQL)
        
        cursor = conn.execute("PRAGMA table_info(idempotency_ledger);")
        columns = [row["name"] for row in cursor.fetchall()]
        if "lease_expires_at" not in columns:
            conn.execute("ALTER TABLE idempotency_ledger ADD COLUMN lease_expires_at REAL NOT NULL DEFAULT 0;")

        cursor = conn.execute("SELECT COUNT(*) FROM workouts;")
        count = cursor.fetchone()[0]
        if count == 0:
            initial = seed_workouts or DEFAULT_WORKOUTS
            conn.executemany(
                "INSERT INTO workouts (id, owner, intensity) VALUES (?, ?, ?);",
                [
                    (w_id, spec["owner"], spec["intensity"])
                    for w_id, spec in initial.items()
                ],
            )


def configure_connection(conn: sqlite3.Connection, is_memory: bool = False) -> sqlite3.Connection:
    conn.row_factory = sqlite3.Row
    if not is_memory:
        conn.execute("PRAGMA journal_mode = WAL;")
        conn.execute("PRAGMA synchronous = NORMAL;")
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.execute("PRAGMA busy_timeout = 5000;")
    return conn