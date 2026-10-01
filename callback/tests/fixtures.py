"""测试公共夹具。"""
from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime, timezone

from callback import db
from callback.adapters import PhoneAdapter
from callback.service import PromiseService


def utc(y, mo, d, h=0, mi=0) -> datetime:
    return datetime(y, mo, d, h, mi, tzinfo=timezone.utc)


class ServiceFixture(unittest.TestCase):
    """每个用例一个临时文件库（测试并发时文件库更真实）。"""

    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="cb-test-")
        self.db_path = os.path.join(self.tmp, "test.db")
        self.rec_dir = os.path.join(self.tmp, "rec")
        self.conn = db.connect(self.db_path, check_same_thread=False)
        db.init_db(self.conn)
        self.svc = PromiseService(self.conn)
        self.phone = PhoneAdapter(self.conn, self.rec_dir)
        self._phone_seq = 1000
        self._setup_people()

    def tearDown(self) -> None:
        self.conn.close()

    # ---- 构造数据 ----
    def _setup_people(self) -> None:
        with db.transaction(self.conn) as c:
            self.agent = c.execute(
                "INSERT INTO agents(name,role,on_leave,active,created_at) VALUES('阿甲','agent',0,1,?)",
                (db.now_iso(),)).lastrowid
            self.agent2 = c.execute(
                "INSERT INTO agents(name,role,on_leave,active,created_at) VALUES('阿乙','agent',0,1,?)",
                (db.now_iso(),)).lastrowid
            self.agent3 = c.execute(
                "INSERT INTO agents(name,role,on_leave,active,created_at) VALUES('阿丙','agent',0,1,?)",
                (db.now_iso(),)).lastrowid
            self.supervisor = c.execute(
                "INSERT INTO agents(name,role,on_leave,active,created_at) VALUES('主管','supervisor',0,1,?)",
                (db.now_iso(),)).lastrowid
            self.duty = c.execute(
                "INSERT INTO agents(name,role,on_leave,active,created_at) VALUES('值班组','duty_group',0,1,?)",
                (db.now_iso(),)).lastrowid

    def add_customer(self, name="客户", phone=None, tz_offset=0) -> int:
        self._phone_seq += 1
        with db.transaction(self.conn) as c:
            return c.execute(
                "INSERT INTO customers(name,phone,tz_offset,created_at) VALUES(?,?,?,?)",
                (name, phone or f"139-{self._phone_seq:08d}", tz_offset,
                 db.now_iso())).lastrowid

    def add_window(self, cid, kind, weekday, start_min, end_min, note="") -> None:
        with db.transaction(self.conn) as c:
            c.execute(
                """INSERT INTO availability_windows(customer_id,kind,weekday,start_minute,
                                                    end_minute,note)
                   VALUES (?,?,?,?,?,?)""",
                (cid, kind, weekday, start_min, end_min, note))

    def make_promise(self, cid, when, *, owner=None, summary="测试问题",
                     session="sess-1", by=None) -> int:
        return self.svc.create_promise(
            customer_id=cid,
            created_by_id=by or self.agent,
            owner_id=owner or self.agent,
            summary=summary, origin_session=session, promised_at=when)
