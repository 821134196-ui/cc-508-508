"""逾期判定与扫描器测试。"""
from __future__ import annotations

from datetime import timedelta

from callback import db
from callback.service import (
    EXPIRED, PENDING, COMPLETED, DISPUTE_CLOSED, InvalidTransition,
)
from callback.tests.fixtures import ServiceFixture, utc

BASE = utc(2026, 10, 7, 10, 0)   # 固定参考时刻


class OverdueTest(ServiceFixture):
    def test_not_overdue_before_deadline(self):
        cid = self.add_customer()
        pid = self.make_promise(cid, BASE - timedelta(minutes=20))
        # deadline = promised + 30 分钟 => 10:10，参考时刻 10:00 未逾期
        self.assertFalse(self.svc.is_overdue(pid, now=BASE))

    def test_overdue_after_deadline(self):
        cid = self.add_customer()
        pid = self.make_promise(cid, BASE - timedelta(minutes=40))  # deadline 09:50
        self.assertTrue(self.svc.is_overdue(pid, now=BASE))

    def test_sweep_marks_only_pending(self):
        cid = self.add_customer()
        p_old = self.make_promise(cid, BASE - timedelta(hours=2))
        p_fresh = self.make_promise(cid, BASE + timedelta(hours=1),
                                    session="sess-2")
        # 已完成的老承诺不被扫描影响
        p_done = self.make_promise(cid, BASE - timedelta(hours=3),
                                   session="sess-3")
        self.svc.complete(p_done, self.agent, "已解决", "关单")

        expired = self.svc.sweep_overdue(now=BASE)
        self.assertEqual(expired, [p_old])
        self.assertEqual(self.svc.get_promise(p_old)["status"], EXPIRED)
        self.assertEqual(self.svc.get_promise(p_fresh)["status"], PENDING)
        self.assertEqual(self.svc.get_promise(p_done)["status"], COMPLETED)

    def test_sweep_idempotent(self):
        cid = self.add_customer()
        pid = self.make_promise(cid, BASE - timedelta(hours=2))
        self.assertEqual(self.svc.sweep_overdue(now=BASE), [pid])
        self.assertEqual(self.svc.sweep_overdue(now=BASE), [])

    def test_expired_cannot_be_completed(self):
        cid = self.add_customer()
        pid = self.make_promise(cid, BASE - timedelta(hours=2))
        self.svc.sweep_overdue(now=BASE)
        with self.assertRaises(InvalidTransition):
            self.svc.complete(pid, self.agent, "结果", "下一步")

    def test_expired_has_timeline_event(self):
        cid = self.add_customer()
        pid = self.make_promise(cid, BASE - timedelta(hours=2))
        self.svc.sweep_overdue(now=BASE)
        types = [r["type"] for r in self.conn.execute(
            "SELECT type FROM events WHERE promise_id=? ORDER BY id", (pid,))]
        self.assertIn("expired", types)

    def test_expired_to_dispute_closed_flow(self):
        cid = self.add_customer()
        pid = self.make_promise(cid, BASE - timedelta(hours=2))
        self.svc.sweep_overdue(now=BASE)
        self.svc.close_dispute(pid, self.supervisor, "确认坐席线路故障导致失约，豁免")
        row = self.svc.get_promise(pid)
        self.assertEqual(row["status"], DISPUTE_CLOSED)
        self.assertEqual(row["closed_by_id"], self.supervisor)
