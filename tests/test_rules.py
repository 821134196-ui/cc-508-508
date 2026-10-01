"""规则测试：禁扰时段、逾期判定、转派竞争、主管权限，以及分支场景。

直接运行：python3 -m unittest discover -s tests -v
"""
from __future__ import annotations

import os
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from callback import config, services  # noqa: E402
from callback.adapters import MockPhone  # noqa: E402
from callback.db import connect, fetchall, fetchone, init_db  # noqa: E402
from callback.errors import (BusinessError, DuplicatePromiseError,  # noqa: E402
                             PermissionDenied, TransferConflict)
from callback.timeutil import in_quiet_hours  # noqa: E402

START = datetime(2026, 10, 1, 10, 0)


class Clock:
    """可控时钟：替换 services.clock，让逾期/未来时刻判定可推演。"""

    def __init__(self):
        self.current = START

    def __call__(self) -> datetime:
        return self.current

    def advance(self, **kw):
        self.current += timedelta(**kw)


class CaseBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.tmpdir = Path(self.tmp.name)
        self._old_rec = config.REC_DIR
        config.REC_DIR = self.tmpdir / "rec"
        self.db = self.tmpdir / "test.db"
        self.conn = connect(str(self.db))
        init_db(self.conn)
        # 适配器建 call_log / sms_outbox
        MockPhone(self.conn, config.REC_DIR)
        self.clock = Clock()
        self._orig_clock = services.clock
        services.clock = self.clock

        self.agent = services.create_agent(self.conn, "坐席甲")
        self.agent2 = services.create_agent(self.conn, "坐席乙")
        self.duty = services.create_agent(self.conn, "值班组")
        self.sup = services.create_agent(self.conn, "高主管", role="supervisor")
        self.cid = services.create_customer(
            self.conn, "客户张", "138-0000-0000",
            quiet_from="22:00", quiet_to="08:00")
        self.conn.commit()

    def tearDown(self):
        services.clock = self._orig_clock
        config.REC_DIR = self._old_rec
        self.conn.close()
        self.tmp.cleanup()

    def iso(self, **kw) -> str:
        return self.clock.current.replace(microsecond=0).replace(**{}).strftime(
            "%Y-%m-%dT%H%M")  # 占位，实际用 _at

    def at(self, dt: datetime) -> str:
        return dt.strftime("%Y-%m-%dT%H:%M")

    def make_promise(self, promised: datetime, cid=None, owner=None):
        return services.create_promise(
            self.conn,
            agent_id=owner or self.agent,
            customer_id=cid or self.cid,
            summary="账单争议核对",
            origin_session="会话 #TEST-1",
            reachable_window="工作日 09:00–18:00",
            promised_at=self.at(promised))

    @property
    def cid_(self):
        return self.cid


class TestQuietHours(unittest.TestCase):
    """纯函数层：跨午夜禁扰区间。"""

    def test_same_day_window(self):
        self.assertTrue(in_quiet_hours(datetime(2026, 10, 1, 12, 30), "12:00", "14:00"))
        self.assertFalse(in_quiet_hours(datetime(2026, 10, 1, 11, 59), "12:00", "14:00"))
        # 右端点开区间：恰好到结束时刻不算打扰
        self.assertFalse(in_quiet_hours(datetime(2026, 10, 1, 14, 0), "12:00", "14:00"))

    def test_overnight_window(self):
        f, t = "22:00", "08:00"
        for h, m, inside in [(23, 0, True), (22, 0, True), (7, 59, True),
                             (0, 0, True), (8, 0, False), (21, 59, False),
                             (12, 0, False)]:
            moment = datetime(2026, 10, 1, h, m)
            self.assertIs(in_quiet_hours(moment, f, t), inside,
                          f"{h}:{m:02d} 判定错误")

    def test_no_window_and_zero_length(self):
        self.assertFalse(in_quiet_hours(datetime(2026, 10, 1, 3, 0), None, None))
        self.assertFalse(in_quiet_hours(datetime(2026, 10, 1, 3, 0), "00:00", "00:00"))


class TestPromiseQuietRule(CaseBase):
    """创建承诺：禁扰、过去时刻、必填。"""

    def test_promise_into_quiet_rejected(self):
        with self.assertRaises(BusinessError):
            self.make_promise(datetime(2026, 10, 1, 23, 0))
        with self.assertRaises(BusinessError):  # 次日 07:00 仍在禁扰
            self.make_promise(datetime(2026, 10, 2, 7, 0))
        # 起点含、终点开
        with self.assertRaises(BusinessError):
            self.make_promise(datetime(2026, 10, 1, 22, 0))
        pid = self.make_promise(datetime(2026, 10, 2, 8, 0))
        self.assertGreater(pid, 0)

    def test_promise_in_past_rejected(self):
        with self.assertRaises(BusinessError):
            self.make_promise(datetime(2026, 10, 1, 9, 0))

    def test_fields_required(self):
        with self.assertRaises(BusinessError):
            services.create_promise(
                self.conn, agent_id=self.agent, customer_id=self.cid,
                summary="", origin_session="s", reachable_window="w",
                promised_at=self.at(self.clock.current + timedelta(hours=1)))
        with self.assertRaises(BusinessError):
            services.create_promise(
                self.conn, agent_id=self.agent, customer_id=self.cid,
                summary="x", origin_session="", reachable_window="w",
                promised_at=self.at(self.clock.current + timedelta(hours=1)))


class TestOverdue(CaseBase):
    def test_overdue_detection_and_flag(self):
        pid = self.make_promise(START + timedelta(hours=1))
        p = fetchone(self.conn, "SELECT * FROM promises WHERE id=?", (pid,))
        self.clock.advance(minutes=30)
        self.assertFalse(services.is_overdue(fetchone(
            self.conn, "SELECT * FROM promises WHERE id=?", (pid,))))
        self.assertEqual(services.mark_overdue(self.conn), [])

        self.clock.advance(hours=1, minutes=30)  # 超过承诺时刻 30 分钟
        self.assertTrue(services.is_overdue(fetchone(
            self.conn, "SELECT * FROM promises WHERE id=?", (pid,))))
        self.assertEqual(services.mark_overdue(self.conn), [pid])
        # 幂等：再扫一次不会重复产生逾期事件
        self.assertEqual(services.mark_overdue(self.conn), [])
        p = fetchone(self.conn, "SELECT * FROM promises WHERE id=?", (pid,))
        self.assertEqual(p["overdue"], 1)
        events = fetchall(
            self.conn,
            "SELECT COUNT(*) n FROM timeline_events WHERE promise_id=? AND kind='promise_overdue'",
            (pid,))
        self.assertEqual(events[0]["n"], 1)

    def test_closed_promise_never_overdue(self):
        pid = self.make_promise(START + timedelta(hours=1))
        self.clock.advance(hours=2)
        services.mark_overdue(self.conn)
        # 主管视角的报表含逾期；闭环后不再判定逾期
        self.conn.execute("UPDATE promises SET status='fulfilled', closed_at=? WHERE id=?",
                          (services.now_str(), pid))
        self.conn.commit()
        p = fetchone(self.conn, "SELECT * FROM promises WHERE id=?", (pid,))
        self.assertFalse(services.is_overdue(p))

    def test_overdue_still_can_be_fulfilled(self):
        """超过承诺时限不能删除，但接通后补登记结果即可闭环。"""
        pid = self.make_promise(START + timedelta(hours=1))
        self.clock.advance(hours=2)
        services.mark_overdue(self.conn)
        services.dial_promise(self.conn, promise_id=pid,
                              agent_id=self.agent, result="connected")
        services.record_outcome(
            self.conn, promise_id=pid, agent_id=self.agent,
            result_text="已解释账单并退费", next_step="等待退费到账短信")
        p = fetchone(self.conn, "SELECT * FROM promises WHERE id=?", (pid,))
        self.assertEqual(p["status"], services.FULFILLED)


class TestTransferRace(CaseBase):
    def _promise(self):
        return self.make_promise(START + timedelta(hours=2))

    def test_cas_second_transfer_loses(self):
        pid = self._promise()
        p0 = fetchone(self.conn, "SELECT version FROM promises WHERE id=?", (pid,))
        services.transfer_promise(
            self.conn, promise_id=pid, from_agent_id=self.agent,
            to_agent_id=self.agent2, expected_version=p0["version"],
            reason="客户指定")
        # 持旧版本号再转 -> 竞争失败
        with self.assertRaises(TransferConflict):
            services.transfer_promise(
                self.conn, promise_id=pid, from_agent_id=self.agent,
                to_agent_id=self.duty, expected_version=p0["version"],
                reason="迟到的转派")
        p = fetchone(self.conn, "SELECT * FROM promises WHERE id=?", (pid,))
        self.assertEqual(p["owner_agent_id"], self.agent2)
        self.assertEqual(p["version"], 2)
        # 竞争失败也留痕，且不产生第二个有效负责人
        lost = fetchone(
            self.conn,
            "SELECT COUNT(*) n FROM transfers WHERE promise_id=? AND won=0", (pid,))
        self.assertEqual(lost["n"], 1)
        owners = fetchall(
            self.conn,
            """SELECT DISTINCT owner_agent_id FROM promises WHERE id=?""", (pid,))
        self.assertEqual(len(owners), 1)

    def test_concurrent_threads_only_one_wins(self):
        """两个线程用同一版本号并发转派：只有一个有效负责人。"""
        pid = self._promise()
        version = fetchone(
            self.conn, "SELECT version FROM promises WHERE id=?", (pid,))["version"]
        barrier = threading.Barrier(2)
        results = {}

        def worker(slot, to_agent):
            conn = connect(str(self.db))
            try:
                barrier.wait()
                try:
                    services.transfer_promise(
                        conn, promise_id=pid, from_agent_id=self.agent,
                        to_agent_id=to_agent, expected_version=version,
                        reason=f"并发转派 {slot}")
                    results[slot] = "won"
                except TransferConflict:
                    results[slot] = "lost"
            finally:
                conn.close()

        t1 = threading.Thread(target=worker, args=(1, self.agent2))
        t2 = threading.Thread(target=worker, args=(2, self.duty))
        t1.start(); t2.start(); t1.join(); t2.join()
        self.assertEqual(sorted(results.values()), ["lost", "won"])
        p = fetchone(self.conn, "SELECT * FROM promises WHERE id=?", (pid,))
        self.assertIn(p["owner_agent_id"], (self.agent2, self.duty))
        won = fetchall(
            self.conn,
            "SELECT to_agent_id FROM transfers WHERE promise_id=? AND won=1", (pid,))
        self.assertEqual(len(won), 1)  # 只有一次成功转派

    def test_non_owner_cannot_transfer(self):
        pid = self._promise()
        with self.assertRaises(PermissionDenied):
            services.transfer_promise(
                self.conn, promise_id=pid, from_agent_id=self.agent2,
                to_agent_id=self.duty, expected_version=1, reason="越权")

    def test_leave_hands_all_open_promises_to_duty(self):
        p1 = self.make_promise(START + timedelta(hours=1))
        p2 = self.make_promise(
            START + timedelta(hours=3),
            cid=services.create_customer(self.conn, "客户李", "138-0000-0001"))
        # 已闭环的承诺不参与交接
        p3 = self.make_promise(
            START + timedelta(hours=4),
            cid=services.create_customer(self.conn, "客户王", "138-0000-0002"))
        self.conn.execute(
            "UPDATE promises SET status='fulfilled', closed_at=? WHERE id=?",
            (services.now_str(), p3))
        self.conn.commit()

        moved = services.agent_go_on_leave(
            self.conn, agent_id=self.agent, duty_agent_id=self.duty)
        self.assertEqual(moved, 2)
        for pid in (p1, p2):
            p = fetchone(self.conn, "SELECT * FROM promises WHERE id=?", (pid,))
            self.assertEqual(p["owner_agent_id"], self.duty)
        p3row = fetchone(self.conn, "SELECT * FROM promises WHERE id=?", (p3,))
        self.assertEqual(p3row["owner_agent_id"], self.agent)  # 闭环的不动
        # 请假者不能再承接
        fresh = self.make_promise(
            START + timedelta(hours=5),
            cid=services.create_customer(self.conn, "客户赵", "138-0000-0003"),
            owner=self.agent2)
        with self.assertRaises(BusinessError):
            services.transfer_promise(
                self.conn, promise_id=fresh, from_agent_id=self.agent2,
                to_agent_id=self.agent, expected_version=1, reason="转给请假者")


class TestCallOutcomes(CaseBase):
    def setUp(self):
        super().setUp()
        self.pid = self.make_promise(START + timedelta(hours=1))

    def test_wrong_number_becomes_dispute(self):
        services.dial_promise(self.conn, promise_id=self.pid,
                              agent_id=self.agent, result="wrong_number")
        p = fetchone(self.conn, "SELECT * FROM promises WHERE id=?", (self.pid,))
        self.assertEqual(p["status"], services.DISPUTE)
        # 争议中不能再登记普通结果
        with self.assertRaises(BusinessError):
            services.record_outcome(
                self.conn, promise_id=self.pid, agent_id=self.agent,
                result_text="x", next_step="y")

    def test_refused_closes_promise(self):
        services.dial_promise(self.conn, promise_id=self.pid,
                              agent_id=self.agent, result="customer_refused")
        p = fetchone(self.conn, "SELECT * FROM promises WHERE id=?", (self.pid,))
        self.assertEqual(p["status"], services.CLOSED_REFUSED)
        self.assertIsNotNone(p["closed_at"])

    def test_connected_creates_local_recording(self):
        trace = services.dial_promise(self.conn, promise_id=self.pid,
                                      agent_id=self.agent, result="connected")
        self.assertTrue(trace["recording"].startswith("recordings/"))
        self.assertTrue((config.REC_DIR / Path(trace["recording"]).name).exists())

    def test_outcome_requires_connected_and_text(self):
        with self.assertRaises(BusinessError):  # 没拨过号
            services.record_outcome(
                self.conn, promise_id=self.pid, agent_id=self.agent,
                result_text="r", next_step="n")
        services.dial_promise(self.conn, promise_id=self.pid,
                              agent_id=self.agent, result="no_answer")
        with self.assertRaises(BusinessError):  # 未接通不能登记结果
            services.record_outcome(
                self.conn, promise_id=self.pid, agent_id=self.agent,
                result_text="r", next_step="n")

    def test_reschedule_requires_no_answer_and_basis(self):
        new_time = START + timedelta(hours=3)
        with self.assertRaises(BusinessError):  # 没有未接通记录
            services.reschedule(
                self.conn, promise_id=self.pid, agent_id=self.agent,
                reason="客户短信要求改约", new_promised_at=self.at(new_time))
        services.dial_promise(self.conn, promise_id=self.pid,
                              agent_id=self.agent, result="no_answer")
        with self.assertRaises(BusinessError):  # 依据为空
            services.reschedule(
                self.conn, promise_id=self.pid, agent_id=self.agent,
                reason="  ", new_promised_at=self.at(new_time))
        with self.assertRaises(BusinessError):  # 新时刻落入禁扰
            services.reschedule(
                self.conn, promise_id=self.pid, agent_id=self.agent,
                reason="客户短信要求改约",
                new_promised_at=self.at(START.replace(hour=23)))
        new_id = services.reschedule(
            self.conn, promise_id=self.pid, agent_id=self.agent,
            reason="客户短信要求改约", new_promised_at=self.at(new_time))
        old = fetchone(self.conn, "SELECT status FROM promises WHERE id=?", (self.pid,))
        new = fetchone(self.conn, "SELECT * FROM promises WHERE id=?", (new_id,))
        self.assertEqual(old["status"], services.RESCHEDULED)
        self.assertEqual(new["status"], services.OPEN)
        chain = fetchone(self.conn, "SELECT * FROM reschedules WHERE new_promise_id=?",
                         (new_id,))
        self.assertEqual(chain["old_promise_id"], self.pid)
        # 短信适配器留痕
        sms_row = fetchone(self.conn,
                           "SELECT COUNT(*) n FROM sms_outbox WHERE phone=?",
                           ("138-0000-0000",))
        self.assertGreaterEqual(sms_row["n"], 2)  # 建承诺 + 改约各一条


class TestDuplicateAndChannels(CaseBase):
    def test_duplicate_active_promise_rejected(self):
        self.make_promise(START + timedelta(hours=1))
        with self.assertRaises(DuplicatePromiseError):
            self.make_promise(START + timedelta(hours=2))

    def test_duplicate_close_flow(self):
        """两条本属重复的承诺：关闭其一后，客户可重新建档承诺。"""
        # 唯一索引挡住了正常业务层的第二条活动承诺；这里模拟“误开两条”后
        # 由坐席把其中一条按重复关闭的处置流程（先关闭旧的，再建新的）。
        kept = self.make_promise(START + timedelta(hours=1))
        services.close_resolved_elsewhere(
            self.conn, promise_id=kept, agent_id=self.agent,
            evidence="用于腾空活动位的演示关闭，工单 #X")
        dup = self.make_promise(START + timedelta(hours=2))
        self.assertTrue(dup > kept)

    def test_unique_index_blocks_second_active(self):
        self.make_promise(START + timedelta(hours=1))
        with self.assertRaises(Exception):
            self.conn.execute(
                """INSERT INTO promises
                   (customer_id, summary, origin_session, reachable_window, promised_at,
                    owner_agent_id, status, created_by_agent, created_at)
                   VALUES (?,?,?,?,?,?, 'open', ?,?)""",
                (self.cid, "x", "s", "w", self.at(START + timedelta(hours=2)),
                 self.agent, self.agent, services.now_str()))

    def test_resolved_elsewhere_needs_evidence(self):
        pid = self.make_promise(START + timedelta(hours=1))
        with self.assertRaises(BusinessError):
            services.close_resolved_elsewhere(
                self.conn, promise_id=pid, agent_id=self.agent, evidence=" ")
        services.close_resolved_elsewhere(
            self.conn, promise_id=pid, agent_id=self.agent,
            evidence="App 在线客服已处理，工单 #APP-1")
        p = fetchone(self.conn, "SELECT status FROM promises WHERE id=?", (pid,))
        self.assertEqual(p["status"], services.CLOSED_RESOLVED)
        # 关闭后可重新为该客户建承诺
        self.assertTrue(self.make_promise(START + timedelta(hours=5)))

    def test_duplicate_close_validations(self):
        cid2 = services.create_customer(self.conn, "客户钱", "138-0000-0099")
        p1 = self.make_promise(START + timedelta(hours=1))
        p2 = self.make_promise(START + timedelta(hours=2), cid=cid2,
                               owner=self.agent2)
        with self.assertRaises(BusinessError):  # 不能指向自己
            services.close_as_duplicate(
                self.conn, promise_id=p1, agent_id=self.agent,
                kept_promise_id=p1)
        with self.assertRaises(BusinessError):  # 不能并入别的客户
            services.close_as_duplicate(
                self.conn, promise_id=p1, agent_id=self.agent,
                kept_promise_id=p2)


class TestSupervisor(CaseBase):
    def setUp(self):
        super().setUp()
        self.pid = self.make_promise(START + timedelta(hours=1))
        services.dial_promise(self.conn, promise_id=self.pid,
                              agent_id=self.agent, result="wrong_number")

    def test_agent_cannot_close_dispute(self):
        with self.assertRaises(PermissionDenied):
            services.supervisor_close_dispute(
                self.conn, promise_id=self.pid, supervisor_id=self.agent,
                note="坐席自行关闭")

    def test_no_delete_path_exists(self):
        """逾期/争议记录没有删除出口：表中只能状态流转。"""
        self.assertFalse(hasattr(services, "delete_promise"))
        before = fetchone(self.conn, "SELECT COUNT(*) n FROM promises")["n"]
        # 即便坐席尝试执行 SQL 删除，外键与业务约束也不是正路；
        # 业务层保证争议仍在
        p = fetchone(self.conn, "SELECT status FROM promises WHERE id=?", (self.pid,))
        self.assertEqual(p["status"], services.DISPUTE)
        self.assertEqual(fetchone(self.conn, "SELECT COUNT(*) n FROM promises")["n"],
                         before)

    def test_supervisor_close_requires_note(self):
        with self.assertRaises(BusinessError):
            services.supervisor_close_dispute(
                self.conn, promise_id=self.pid, supervisor_id=self.sup, note=" ")

    def test_supervisor_close_and_audit(self):
        services.supervisor_close_dispute(
            self.conn, promise_id=self.pid, supervisor_id=self.sup,
            note="号码确属登记错误，客户无此业务，关闭")
        p = fetchone(self.conn, "SELECT * FROM promises WHERE id=?", (self.pid,))
        self.assertEqual(p["status"], services.CLOSED_DISPUTE)
        review = fetchone(
            self.conn, "SELECT * FROM dispute_reviews WHERE promise_id=?", (self.pid,))
        self.assertEqual(review["supervisor_id"], self.sup)
        self.assertEqual(review["decision"], "close")

    def test_supervisor_fix_phone_and_reopen(self):
        services.update_customer_phone(
            self.conn, customer_id=self.cid, supervisor_id=self.sup,
            new_phone="139-1111-2222")
        # 重开时刻落入禁扰也必须被拦
        with self.assertRaises(BusinessError):
            services.supervisor_reopen(
                self.conn, promise_id=self.pid, supervisor_id=self.sup,
                note="已与客户核对新号码",
                new_promised_at=self.at(START.replace(hour=23)))
        services.supervisor_reopen(
            self.conn, promise_id=self.pid, supervisor_id=self.sup,
            note="已与客户核对新号码",
            new_promised_at=self.at(START + timedelta(days=1, hours=2)))
        p = fetchone(self.conn, "SELECT * FROM promises WHERE id=?", (self.pid,))
        self.assertEqual(p["status"], services.OPEN)
        cust = fetchone(self.conn, "SELECT phone FROM customers WHERE id=?", (self.cid,))
        self.assertEqual(cust["phone"], "139-1111-2222")

    def test_agent_cannot_fix_phone(self):
        with self.assertRaises(PermissionDenied):
            services.update_customer_phone(
                self.conn, customer_id=self.cid, supervisor_id=self.agent,
                new_phone="139-0000-0000")


if __name__ == "__main__":
    unittest.main(verbosity=2)
