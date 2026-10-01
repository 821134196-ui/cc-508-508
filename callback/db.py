"""SQLite 持久层：保存承诺、时间线事件、拨打记录、转派记录。

所有业务时间均以 UTC ISO-8601 字符串（带 +00:00）存储，界面层负责转换。
时间线表 events 为只追加(append-only)，任何状态变更都写入一条事件，
逾期记录因此无法被坐席"物理删除"——只能经主管关闭争议。
"""
from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Iterator, Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS agents (
    id          INTEGER PRIMARY KEY,
    name        TEXT NOT NULL UNIQUE,
    role        TEXT NOT NULL CHECK (role IN ('agent','supervisor','duty_group')),
    on_leave    INTEGER NOT NULL DEFAULT 0,          -- 坐席是否请假
    active      INTEGER NOT NULL DEFAULT 1,
    created_at  TEXT NOT NULL
);

-- 客户可接听时段 / 禁扰时段（本地墙上时钟，按星期几）
-- kind='ok' 表示可接听窗口；kind='dnc' 表示禁扰窗口。
-- start_minute/end_minute 为距当天 00:00 的分钟数，允许跨午夜(end<=start 表示跨过 24:00)。
CREATE TABLE IF NOT EXISTS availability_windows (
    id           INTEGER PRIMARY KEY,
    customer_id  INTEGER NOT NULL,
    kind         TEXT NOT NULL CHECK (kind IN ('ok','dnc')),
    weekday      INTEGER NOT NULL CHECK (weekday BETWEEN 0 AND 6),
    start_minute INTEGER NOT NULL CHECK (start_minute BETWEEN 0 AND 1439),
    end_minute   INTEGER NOT NULL CHECK (end_minute BETWEEN 0 AND 1440),
    note         TEXT NOT NULL DEFAULT '',
    FOREIGN KEY (customer_id) REFERENCES customers(id)
);

CREATE TABLE IF NOT EXISTS customers (
    id         INTEGER PRIMARY KEY,
    name       TEXT NOT NULL,
    phone      TEXT NOT NULL UNIQUE,
    tz_offset  INTEGER NOT NULL DEFAULT 0,           -- 客户本地相对 UTC 的分钟偏移
    created_at TEXT NOT NULL
);

-- 承诺主表。状态机：
--   pending(有效承诺) -> completed(回拨完成并填写结果)
--                     -> rescheduled(有依据改约，派生新承诺)
--                     -> cancelled_{wrong_number,refused,resolved_elsewhere,duplicate}
--                     -> expired(超过承诺时限未完成；不可被坐席删除)
--   expired -> dispute_closed(主管确认关闭争议)
CREATE TABLE IF NOT EXISTS promises (
    id               INTEGER PRIMARY KEY,
    customer_id      INTEGER NOT NULL,
    summary          TEXT NOT NULL,                  -- 问题摘要
    origin_session   TEXT NOT NULL,                  -- 原始会话标识
    promised_at      TEXT NOT NULL,                  -- 承诺回拨时刻
    deadline_at      TEXT NOT NULL,                  -- 承诺时限（逾期判定用）
    owner_id         INTEGER NOT NULL,               -- 当前负责坐席（有效负责人唯一）
    created_by_id    INTEGER NOT NULL,
    status           TEXT NOT NULL DEFAULT 'pending',
    result           TEXT,                           -- 回拨结果
    next_step        TEXT,                           -- 下一步
    reschedule_basis TEXT,                           -- 再次预约的依据（不通原因/客户留言等）
    dispute_note     TEXT,                           -- 主管关闭争议时的说明
    closed_by_id     INTEGER,                        -- 关闭争议的主管
    created_at       TEXT NOT NULL,
    completed_at     TEXT,
    closed_at        TEXT,
    parent_id        INTEGER,                        -- 改约链：上一条承诺
    owner_version    INTEGER NOT NULL DEFAULT 0,     -- 负责人版本，转派乐观锁
    supersedes_id    INTEGER,                        -- 本条改约所替代的承诺
    FOREIGN KEY (customer_id) REFERENCES customers(id),
    FOREIGN KEY (owner_id) REFERENCES agents(id),
    FOREIGN KEY (parent_id) REFERENCES promises(id)
);
CREATE INDEX IF NOT EXISTS idx_promises_status_time ON promises(status, promised_at);
CREATE INDEX IF NOT EXISTS idx_promises_customer ON promises(customer_id);

-- 所有权（转派）日志。并发转派通过 owner_version 乐观锁串行化：
-- 任何 UPDATE promises SET owner_id 的事务必须版本号匹配，
-- 因此同一时刻永远只会有一个有效负责人。
CREATE TABLE IF NOT EXISTS ownership_log (
    id              INTEGER PRIMARY KEY,
    promise_id      INTEGER NOT NULL,
    from_agent_id   INTEGER,
    to_agent_id     INTEGER NOT NULL,
    reason          TEXT NOT NULL,                   -- leave_handover / manual / duty_takeover
    occurred_at     TEXT NOT NULL,
    FOREIGN KEY (promise_id) REFERENCES promises(id)
);

-- 只追加时间线
CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY,
    promise_id  INTEGER NOT NULL,
    actor_id    INTEGER,
    type        TEXT NOT NULL,
    detail      TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL,
    FOREIGN KEY (promise_id) REFERENCES promises(id)
);
CREATE INDEX IF NOT EXISTS idx_events_promise ON events(promise_id, id);

-- 拨打记录（电话适配器写）
CREATE TABLE IF NOT EXISTS calls (
    id          INTEGER PRIMARY KEY,
    promise_id  INTEGER NOT NULL,
    agent_id    INTEGER NOT NULL,
    phone       TEXT NOT NULL,
    outcome     TEXT NOT NULL,                       -- connected/no_answer/refused/wrong_number
    duration_s  INTEGER NOT NULL DEFAULT 0,
    recording  TEXT NOT NULL DEFAULT '',            -- 仅保存本地虚构文件路径
    created_at  TEXT NOT NULL,
    FOREIGN KEY (promise_id) REFERENCES promises(id)
);

-- 短信记录（短信适配器写）
CREATE TABLE IF NOT EXISTS sms (
    id          INTEGER PRIMARY KEY,
    promise_id  INTEGER,
    customer_id INTEGER NOT NULL,
    direction   TEXT NOT NULL CHECK (direction IN ('outbound','inbound')),
    body        TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    FOREIGN KEY (promise_id) REFERENCES promises(id)
);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def to_iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def parse_iso(s: str) -> datetime:
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def connect(path: str = ":memory:", check_same_thread: bool = True) -> sqlite3.Connection:
    if path != ":memory:":
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    conn = sqlite3.connect(path, timeout=15, isolation_level=None,
                           check_same_thread=check_same_thread)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    try:
        conn.execute("BEGIN IMMEDIATE")
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def get(conn: sqlite3.Connection, sql: str, params=()) -> Optional[sqlite3.Row]:
    return conn.execute(sql, params).fetchone()
