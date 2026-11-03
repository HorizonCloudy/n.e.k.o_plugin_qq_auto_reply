from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path


class DeepChatStore:
    """SQLite persistence for mood and relationship state.

    Standard library only. A short-lived connection per operation keeps the
    module usable from async handlers and from plain ``unittest`` without
    thread-affinity surprises. The schema is created on first use.
    """

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _init_schema(self) -> None:
        with self.connection() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS deepchat_mood (
                    user_key TEXT PRIMARY KEY,
                    valence REAL NOT NULL DEFAULT 0.0,
                    arousal REAL NOT NULL DEFAULT 0.5,
                    current TEXT NOT NULL DEFAULT 'neutral',
                    updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS deepchat_relationship (
                    user_id TEXT PRIMARY KEY,
                    affinity REAL NOT NULL DEFAULT 0.0,
                    trust REAL NOT NULL DEFAULT 0.5,
                    interactions INTEGER NOT NULL DEFAULT 0,
                    milestones TEXT NOT NULL DEFAULT '[]',
                    updated_at REAL NOT NULL
                );
                """
            )
