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
        # 写回在回复主链路上：拿不到锁时最多等 0.5s 就放弃，别让整条回复
        # 卡在 SQLite 的默认 5s 上（另一个进程/线程持锁时尤其明显）。
        conn = sqlite3.connect(self.db_path, timeout=0.5)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    @contextmanager
    def session(self) -> Iterator[sqlite3.Connection]:
        """一个逻辑读作用域共用一个连接（enrich 这类"读两张表再拼片段"的场景）。

        与 :meth:`connection` 的差别只在**语义**：``connection()`` 是「一次操作一个
        连接」，``session()`` 是「一个作用域一个连接」。少开连接 = 少一次拿锁的机会，
        也少一次 0.5s 超时的风险。
        """
        with self.connection() as conn:
            yield conn

    #: DDL 不能参数化，所以迁移只允许执行这张白名单里的 (表, 列, 定义)。
    _MIGRATIONS: dict[str, dict[str, str]] = {
        "deepchat_relationship": {"frozen": "INTEGER NOT NULL DEFAULT 0"},
    }

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
                    updated_at REAL NOT NULL,
                    frozen INTEGER NOT NULL DEFAULT 0
                );
                -- 宿主记忆的**社交坐标索引**：只存坐标（谁 × 哪个域），不存内容。
                -- 作用：遗忘时能沿"用户轴 / 群轴"枚举出该清哪些 subject。
                -- ⚠️ 本表含社交关系坐标，属于隐私数据：**禁止明文备份/导出**。
                CREATE TABLE IF NOT EXISTS deepchat_subject_index (
                    subject_kind TEXT NOT NULL,
                    group_id TEXT NOT NULL DEFAULT '',
                    user_id TEXT NOT NULL,
                    last_written_at REAL NOT NULL,
                    PRIMARY KEY (subject_kind, group_id, user_id)
                );
                CREATE INDEX IF NOT EXISTS ix_subject_index_user
                    ON deepchat_subject_index(user_id);
                CREATE INDEX IF NOT EXISTS ix_subject_index_group
                    ON deepchat_subject_index(group_id);
                """
            )
            # 旧库没有 frozen 列：加法式迁移，保留既有行。
            self._ensure_column(conn, "deepchat_relationship", "frozen", "INTEGER NOT NULL DEFAULT 0")

    @staticmethod
    def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
        return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}

    def _ensure_column(self, conn: sqlite3.Connection, table: str, column: str, ddl: str) -> None:
        """Add one whitelisted column if missing; tolerant of concurrent starts."""
        if self._MIGRATIONS.get(table, {}).get(column) != ddl:
            raise ValueError(f"未登记的迁移列：{table}.{column}")
        if column in self._columns(conn, table):
            return
        try:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
        except sqlite3.OperationalError:
            # 另一个进程可能刚好补上了同一列；列确实存在才算无害。
            if column not in self._columns(conn, table):
                raise
