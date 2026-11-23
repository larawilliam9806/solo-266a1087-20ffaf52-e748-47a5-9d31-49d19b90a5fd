"""SQLite 连接与 schema 初始化。"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from typing import Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS versions (
    version    TEXT PRIMARY KEY,
    created_at TEXT NOT NULL
);

-- 标本登记册：同号只计一次
CREATE TABLE IF NOT EXISTS specimens (
    specimen_no    TEXT PRIMARY KEY,
    intake_version TEXT NOT NULL,
    species_no     TEXT NOT NULL,
    created_at     TEXT NOT NULL
);

-- 每个版本下每件标本的鉴定状态：
--   resolved -> species_no 已归入该种
--   pending  -> candidates(JSON 数组) 待重鉴
CREATE TABLE IF NOT EXISTS specimen_states (
    version     TEXT NOT NULL,
    specimen_no TEXT NOT NULL,
    status      TEXT NOT NULL CHECK (status IN ('resolved', 'pending')),
    species_no  TEXT,
    candidates  TEXT,
    PRIMARY KEY (version, specimen_no)
);

-- 每个版本预先算好的按种统计名单
CREATE TABLE IF NOT EXISTS species_counts (
    version    TEXT NOT NULL,
    species_no TEXT NOT NULL,
    count      INTEGER NOT NULL CHECK (count >= 0),
    PRIMARY KEY (version, species_no)
);

CREATE INDEX IF NOT EXISTS idx_states_version_status
    ON specimen_states (version, status);
"""


def connect(db_path: str) -> sqlite3.Connection:
    """打开一个自动提交模式的连接，事务由代码显式控制。"""
    conn = sqlite3.connect(db_path, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def init_db(db_path: str, initial_version: str, now: str) -> None:
    conn = connect(db_path)
    try:
        conn.executescript(SCHEMA)
        row = conn.execute(
            "SELECT value FROM meta WHERE key = 'current_version'"
        ).fetchone()
        if row is None:
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute(
                    "INSERT INTO versions (version, created_at) VALUES (?, ?)",
                    (initial_version, now),
                )
                conn.execute(
                    "INSERT INTO meta (key, value) VALUES ('current_version', ?)",
                    (initial_version,),
                )
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
    finally:
        conn.close()


@contextmanager
def write_tx(db_path: str) -> Iterator[sqlite3.Connection]:
    """写事务：BEGIN IMMEDIATE，异常时整体回滚（换版失败保留原版）。"""
    conn = connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        yield conn
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


@contextmanager
def read_tx(db_path: str) -> Iterator[sqlite3.Connection]:
    """读事务：给查询一个一致性快照。"""
    conn = connect(db_path)
    try:
        conn.execute("BEGIN")
        yield conn
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()
