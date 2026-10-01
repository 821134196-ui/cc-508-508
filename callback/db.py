"""SQLite 连接、建表与时间线事件。

时间线（timeline_events）是追加式的：任何状态变化都只插入事件、不抹掉历史。
这保证了“不能直接删除逾期记录”“责任交接可追溯”等要求。
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime
from typing import Any, Iterator

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS agents (
    id           INTEGER PRIMARY KEY,
    name         TEXT NOT NULL UNIQUE,
    role         TEXT NOT NULL CHECK (role IN ('agent','supervisor')),
    status       TEXT NOT NULL DEFAULT 'active'
                 CHECK (status IN ('active','on_leave')),
    created_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS customers (
    id           INTEGER PRIMARY KEY,
    name         TEXT NOT NULL,
    phone        TEXT NOT NULL UNIQUE,
    -- 禁扰时段：每天 [quiet_from, quiet_to)，本地“墙钟时间” HH:MM
    quiet_from   TEXT,
    quiet_to     TEXT,
    note         TEXT DEFAULT '',
    created_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS promises (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    customer_id        INTEGER NOT NULL REFERENCES customers(id),
    -- 问题摘要与原始会话
    summary            TEXT NOT NULL,
    origin_session     TEXT NOT NULL,
    -- 客户可接听时段（自由文本 + 结构化 JSON，便于展示）
    reachable_window   TEXT NOT NULL,
    -- 承诺时刻（本地 ISO 文本，不含时区歧义，系统统一按本地时钟处理）
    promised_at        TEXT NOT NULL,
    -- 有效负责人：CREATE -> 每次转派用 CAS 更新，保证同一时刻只有一个
    owner_agent_id     INTEGER NOT NULL REFERENCES agents(id),
    -- 乐观锁版本号，每次更新 +1，用于并发转派竞争
    version            INTEGER NOT NULL DEFAULT 1,
    -- open(待处理) / fulfilled(已回拨) / rescheduled(已改约,由新承诺承接)
    -- / closed_resolved(其他渠道已解决) / closed_duplicate(重复创建)
    -- / dispute(有争议,待主管) / closed_dispute(主管已裁决关闭)
    status             TEXT NOT NULL DEFAULT 'open',
    -- 逾期标记独立于状态：承诺时刻已过且仍未闭环即为逾期（见 services.is_overdue）
    overdue            INTEGER NOT NULL DEFAULT 0,
    created_by_agent   INTEGER NOT NULL REFERENCES agents(id),
    created_at         TEXT NOT NULL,
    closed_at          TEXT
);

-- 同一客户同时只能有一条“有效”承诺（open 或 dispute）。
-- rescheduled / fulfilled / closed_* 均不再占位，从根上防止重复创建。
CREATE UNIQUE INDEX IF NOT EXISTS ux_active_promise
    ON promises(customer_id) WHERE status IN ('open','dispute');

CREATE TABLE IF NOT EXISTS calls (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    promise_id    INTEGER NOT NULL REFERENCES promises(id),
    agent_id      INTEGER NOT NULL REFERENCES agents(id),
    started_at    TEXT NOT NULL,
    outcome       TEXT NOT NULL CHECK (outcome IN
                  ('connected','wrong_number','customer_refused','no_answer')),
    recording     TEXT,          -- 本地虚构录音文件相对路径
    note          TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS outcomes (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    promise_id    INTEGER NOT NULL UNIQUE REFERENCES promises(id),
    call_id       INTEGER REFERENCES calls(id),
    result        TEXT NOT NULL,  -- 回拨结果
    next_step     TEXT NOT NULL,  -- 下一步
    agent_id      INTEGER NOT NULL REFERENCES agents(id),
    recorded_at   TEXT NOT NULL
);

-- 再次预约：不接通且“有依据”时，由一次未接通呼叫派生一条新承诺
CREATE TABLE IF NOT EXISTS reschedules (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    old_promise_id   INTEGER NOT NULL REFERENCES promises(id),
    new_promise_id   INTEGER NOT NULL REFERENCES promises(id),
    reason           TEXT NOT NULL,  -- 依据：客户短信改约 / 可接听时段 / 值班组安排
    created_by_agent INTEGER NOT NULL REFERENCES agents(id),
    created_at       TEXT NOT NULL
);

-- 转派（含坐席请假 -> 值班组的交接）
CREATE TABLE IF NOT EXISTS transfers (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    promise_id       INTEGER NOT NULL REFERENCES promises(id),
    from_agent_id    INTEGER NOT NULL REFERENCES agents(id),
    to_agent_id      INTEGER NOT NULL REFERENCES agents(id),
    reason           TEXT NOT NULL,
    expected_version INTEGER NOT NULL,  -- 发起转派时读到的版本
    won              INTEGER NOT NULL,  -- 是否赢得并发竞争
    created_at       TEXT NOT NULL
);

-- 主管裁决（关闭争议必须主管确认）
CREATE TABLE IF NOT EXISTS dispute_reviews (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    promise_id    INTEGER NOT NULL REFERENCES promises(id),
    supervisor_id INTEGER NOT NULL REFERENCES agents(id),
    decision      TEXT NOT NULL CHECK (decision IN ('close','reopen')),
    note          TEXT NOT NULL,
    created_at    TEXT NOT NULL
);

-- 追加式时间线
CREATE TABLE IF NOT EXISTS timeline_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    promise_id  INTEGER REFERENCES promises(id),
    customer_id INTEGER REFERENCES customers(id),
    at          TEXT NOT NULL,
    actor_id    INTEGER REFERENCES agents(id),
    kind        TEXT NOT NULL,
    detail      TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS ix_timeline_promise ON timeline_events(promise_id, id);
CREATE INDEX IF NOT EXISTS ix_promises_due ON promises(status, promised_at);
"""


def now() -> str:
    """统一的当前时间入口，测试可通过 services 的时钟钩子替换。"""
    return datetime.now().replace(microsecond=0).isoformat()


def connect(db_path: str | None = None) -> sqlite3.Connection:
    path = str(db_path or config.DB_PATH)
    conn = sqlite3.connect(path, timeout=5, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


def log_event(
    conn: sqlite3.Connection,
    kind: str,
    *,
    promise_id: int | None = None,
    customer_id: int | None = None,
    actor_id: int | None = None,
    detail: str = "",
    at: str | None = None,
) -> None:
    conn.execute(
        """INSERT INTO timeline_events
           (promise_id, customer_id, at, actor_id, kind, detail)
           VALUES (?,?,?,?,?,?)""",
        (promise_id, customer_id, at or now(), actor_id, kind, detail),
    )


def fetchone(conn: sqlite3.Connection, sql: str, params: tuple[Any, ...] = ()):
    return conn.execute(sql, params).fetchone()


def fetchall(conn: sqlite3.Connection, sql: str, params: tuple[Any, ...] = ()):
    return conn.execute(sql, params).fetchall()
