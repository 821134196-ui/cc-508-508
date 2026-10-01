"""模拟电话 / 短信适配器。

不接入任何真实通信：
- PhoneAdapter.dial() 按配置或伪随机产生通话结果，并在本地生成一个"虚构录音文件"
  （仅一个占位文本文件，绝不包含真实音频）；
- SmsAdapter.send() 只写本地数据库记录。

测试可通过 PhoneAdapter(outcome=...) 固定结果，复现"不接通"等场景。
"""
from __future__ import annotations

import hashlib
import os
import sqlite3
import threading
from datetime import datetime
from typing import Optional

from . import db

# 通话结果常量
CONNECTED = "connected"
NO_ANSWER = "no_answer"
REFUSED = "refused"          # 客户拒接 / 明确拒绝
WRONG_NUMBER = "wrong_number"


class PhoneAdapter:
    """本地模拟电话。录音是 recordings/ 下的虚构占位文件。"""

    def __init__(
        self,
        conn: sqlite3.Connection,
        recordings_dir: str,
        outcome: Optional[str] = None,
        seed: int = 0,
    ) -> None:
        self.conn = conn
        self.recordings_dir = recordings_dir
        self._fixed_outcome = outcome
        self._counter = seed
        self._lock = threading.Lock()
        os.makedirs(recordings_dir, exist_ok=True)

    def _pick_outcome(self, phone: str) -> str:
        if self._fixed_outcome is not None:
            return self._fixed_outcome
        # 无固定结果时做确定性"伪随机"，保证测试可复现
        with self._lock:
            self._counter += 1
            n = self._counter
        digest = hashlib.sha256(f"{phone}:{n}".encode()).digest()[0]
        table = (CONNECTED, CONNECTED, CONNECTED, NO_ANSWER, REFUSED, WRONG_NUMBER)
        return table[digest % len(table)]

    def _write_fake_recording(self, call_id: int, phone: str, outcome: str) -> str:
        """生成本地虚构录音占位文件（纯文本，非真实音频）。"""
        name = f"rec_call_{call_id:06d}.txt"
        path = os.path.join(self.recordings_dir, name)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(
                "[虚构录音占位 / SIMULATED RECORDING STUB]\n"
                f"call_id={call_id}\nphone={phone}\noutcome={outcome}\n"
                f"generated_at={datetime.utcnow().isoformat()}Z\n"
                "本文件不是真实音频，仅为本地模拟占位。\n"
            )
        return path

    def dial(
        self,
        promise_id: int,
        agent_id: int,
        phone: str,
        *,
        duration_s: int = 0,
        outcome: Optional[str] = None,
    ) -> sqlite3.Row:
        """发起一次模拟拨打并落拨打记录。"""
        result = outcome or self._pick_outcome(phone)
        if result not in (CONNECTED, NO_ANSWER, REFUSED, WRONG_NUMBER):
            raise ValueError(f"非法通话结果: {result}")
        dur = duration_s or (42 if result == CONNECTED else 0)
        with db.transaction(self.conn) as c:
            cur = c.execute(
                """INSERT INTO calls(promise_id, agent_id, phone, outcome,
                                     duration_s, recording, created_at)
                   VALUES (?,?,?,?,?,?,?)""",
                (promise_id, agent_id, phone, result, dur, "", db.now_iso()),
            )
            call_id = cur.lastrowid
            path = self._write_fake_recording(call_id, phone, result)
            c.execute("UPDATE calls SET recording=? WHERE id=?", (path, call_id))
        return db.get(self.conn, "SELECT * FROM calls WHERE id=?", (call_id,))


class SmsAdapter:
    """本地模拟短信：只写库，不外发。"""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def send(self, customer_id: int, body: str, promise_id: Optional[int] = None) -> sqlite3.Row:
        with db.transaction(self.conn) as c:
            cur = c.execute(
                """INSERT INTO sms(promise_id, customer_id, direction, body, created_at)
                   VALUES (?,?,?,?,?)""",
                (promise_id, customer_id, "outbound", body, db.now_iso()),
            )
            sid = cur.lastrowid
        return db.get(self.conn, "SELECT * FROM sms WHERE id=?", (sid,))

    def receive(self, customer_id: int, body: str, promise_id: Optional[int] = None) -> sqlite3.Row:
        with db.transaction(self.conn) as c:
            cur = c.execute(
                """INSERT INTO sms(promise_id, customer_id, direction, body, created_at)
                   VALUES (?,?,?,?,?)""",
                (promise_id, customer_id, "inbound", body, db.now_iso()),
            )
            sid = cur.lastrowid
        return db.get(self.conn, "SELECT * FROM sms WHERE id=?", (sid,))
