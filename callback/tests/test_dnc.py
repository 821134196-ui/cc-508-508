"""禁扰规则测试：禁扰窗口、可接听窗口、边界、跨午夜、时区换算。"""
from __future__ import annotations

from datetime import timedelta

from callback import db
from callback.service import DncViolation, check_callable
from callback.tests.fixtures import ServiceFixture, utc


class DncRulesTest(ServiceFixture):
    def test_dnc_window_blocks_promise(self):
        cid = self.add_customer(tz_offset=0)
        self.add_window(cid, "dnc", 2, 12 * 60, 14 * 60, "午休")  # 周三
        # 周三 13:00 UTC（客户 tz=0，本地同为周三）
        self.assertIsNotNone(check_callable(self.conn, cid, utc(2026, 10, 7, 13, 0)))
        with self.assertRaises(DncViolation):
            self.make_promise(cid, utc(2026, 10, 7, 13, 0))
        # 周三 15:00 无禁扰，可建
        pid = self.make_promise(cid, utc(2026, 10, 7, 15, 0))
        self.assertGreater(pid, 0)

    def test_dnc_half_open_boundaries(self):
        # 窗口左闭右开：12:00 命中禁扰，14:00 已解禁
        cid = self.add_customer(tz_offset=0)
        self.add_window(cid, "dnc", 2, 12 * 60, 14 * 60)
        self.assertIsNotNone(check_callable(self.conn, cid, utc(2026, 10, 7, 12, 0)))
        self.assertIsNone(check_callable(self.conn, cid, utc(2026, 10, 7, 14, 0)))
        self.assertIsNone(check_callable(self.conn, cid, utc(2026, 10, 7, 11, 59)))

    def test_cross_midnight_window_same_day(self):
        # 周五 22:00 -> 次日 02:00（跨午夜）
        cid = self.add_customer(tz_offset=0)
        self.add_window(cid, "dnc", 4, 22 * 60, 2 * 60)
        self.assertIsNotNone(check_callable(self.conn, cid, utc(2026, 10, 9, 23, 30)))
        self.assertIsNotNone(check_callable(self.conn, cid, utc(2026, 10, 10, 1, 0)))
        # 周六 03:00 已出窗口
        self.assertIsNone(check_callable(self.conn, cid, utc(2026, 10, 10, 3, 0)))
        # 周五 21:59 未进窗口
        self.assertIsNone(check_callable(self.conn, cid, utc(2026, 10, 9, 21, 59)))

    def test_cross_midnight_window_hits_on_next_weekday(self):
        # 窗口定义在周日(6) 23:00 -> 01:00，周一(0) 00:30 仍应命中禁扰
        cid = self.add_customer(tz_offset=0)
        self.add_window(cid, "dnc", 6, 23 * 60, 1 * 60)
        self.assertIsNotNone(check_callable(self.conn, cid, utc(2026, 10, 5, 0, 30)))
        self.assertIsNone(check_callable(self.conn, cid, utc(2026, 10, 5, 1, 0)))

    def test_timezone_offset_applied(self):
        # 客户 UTC+8：其本地周三 12:30 = UTC 周三 04:30；本地 14:00 = UTC 06:00
        cid = self.add_customer(tz_offset=8 * 60)
        self.add_window(cid, "dnc", 2, 12 * 60, 14 * 60)
        self.assertIsNotNone(check_callable(self.conn, cid, utc(2026, 10, 7, 4, 30)))
        self.assertIsNone(check_callable(self.conn, cid, utc(2026, 10, 7, 6, 0)))

    def test_ok_windows_restrict(self):
        cid = self.add_customer(tz_offset=0)
        for wd in range(7):
            self.add_window(cid, "ok", wd, 9 * 60, 18 * 60)
        self.assertIsNone(check_callable(self.conn, cid, utc(2026, 10, 7, 10, 0)))
        # 18:00 为右端点开区间，不可拨
        reason = check_callable(self.conn, cid, utc(2026, 10, 7, 18, 0))
        self.assertIn("可接听时段", reason)
        self.assertIn("可接听时段",
                      check_callable(self.conn, cid, utc(2026, 10, 7, 8, 0)))

    def test_dnc_takes_priority_over_ok(self):
        cid = self.add_customer(tz_offset=0)
        self.add_window(cid, "ok", 2, 9 * 60, 18 * 60)
        self.add_window(cid, "dnc", 2, 12 * 60, 13 * 60, "午休勿扰")
        reason = check_callable(self.conn, cid, utc(2026, 10, 7, 12, 30))
        self.assertIn("禁扰", reason)

    def test_reschedule_also_validates_dnc(self):
        cid = self.add_customer(tz_offset=0)
        self.add_window(cid, "dnc", 3, 0, 1440, "周四全天勿扰")
        pid = self.make_promise(cid, utc(2026, 10, 6, 10, 0))  # 周二
        self.phone.dial(pid, self.agent, "139-00000001", outcome="no_answer")
        with self.assertRaises(DncViolation):
            self.svc.reschedule(
                pid, self.agent, new_time=utc(2026, 10, 8, 10, 0),
                basis="客户未接", basis_type="no_answer")
