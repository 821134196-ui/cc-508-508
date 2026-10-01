"""模拟电话、短信适配器与本地虚构录音文件。

不接入任何真实通讯：电话只写一条“通话记录文本”，短信落在 outbox 表，
录音是本地生成的占位文件。全部调用都会进入 SQLite 时间线之外的模拟轨迹，
便于测试断言。
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

from . import config
from .db import now

# 模拟呼出可能出现的全部结果（与 calls.outcome 对齐）
OUTCOMES = ("connected", "wrong_number", "customer_refused", "no_answer")


class MockPhone:
    """模拟电话适配器。

    dial_result 可在测试中预设；默认 connected。每次拨号都会把
    拨号轨迹写入 call_log 表，并（接通时）生成本地虚构录音文件。
    """

    def __init__(self, conn: sqlite3.Connection, rec_dir: Path | None = None):
        self.conn = conn
        self.rec_dir = rec_dir or config.REC_DIR
        self._ensure_tables()

    def _ensure_tables(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS call_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                phone TEXT NOT NULL, at TEXT NOT NULL,
                result TEXT NOT NULL, detail TEXT DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS sms_outbox (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                phone TEXT NOT NULL, at TEXT NOT NULL,
                body TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'queued'
            );
            """
        )

    def dial(self, phone: str, result: str = "connected", *, promise_id: int) -> dict:
        if result not in OUTCOMES:
            raise ValueError(f"未知通话结果: {result}")
        ts = now()
        recording = None
        detail = ""
        if result == "connected":
            recording = self._make_recording(promise_id, ts)
            detail = f"通话已接通，录音 {recording}"
        elif result == "wrong_number":
            detail = "号码拨错：对方表示并非本人"
        elif result == "customer_refused":
            detail = "客户拒接/明确表示无需回拨"
        else:
            detail = "振铃未接通"
        cur = self.conn.execute(
            "INSERT INTO call_log (phone, at, result, detail) VALUES (?,?,?,?)",
            (phone, ts, result, detail),
        )
        return {"result": result, "at": ts, "recording": recording,
                "detail": detail, "log_id": cur.lastrowid}

    def _make_recording(self, promise_id: int, ts: str) -> str:
        """生成本地虚构录音占位文件（非真实音频）。"""
        self.rec_dir.mkdir(parents=True, exist_ok=True)
        safe_ts = ts.replace(":", "").replace("-", "")
        fname = f"rec_p{promise_id}_{safe_ts}.txt"
        path = self.rec_dir / fname
        if not path.exists():
            path.write_text(
                f"[模拟录音] 承诺 #{promise_id}\n时间: {ts}\n"
                "（本文件为虚构占位，不含真实音频）\n",
                encoding="utf-8",
            )
        return f"recordings/{fname}"


class MockSMS:
    """模拟短信适配器：只入本地 outbox，不外发。"""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        self.conn.execute(
            """CREATE TABLE IF NOT EXISTS sms_outbox (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                phone TEXT NOT NULL, at TEXT NOT NULL,
                body TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'queued'
            )"""
        )

    def send(self, phone: str, body: str) -> int:
        cur = self.conn.execute(
            "INSERT INTO sms_outbox (phone, at, body, status) VALUES (?,?,?,'sent')",
            (phone, now(), body),
        )
        return int(cur.lastrowid)
