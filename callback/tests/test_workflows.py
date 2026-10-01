"""业务流程测试：完成回填、有依据改约、四类取消、重复创建、适配器。"""
from __future__ import annotations

import os
from datetime import timedelta

from callback import db
from callback.adapters import NO_ANSWER, REFUSED, WRONG_NUMBER
from callback.service import (
    CANCEL_DUPLICATE, CANCEL_REFUSED, CANCEL_RESOLVED, CANCEL_WRONG_NUMBER,
    COMPLETED, DuplicatePromiseError, InvalidTransition, RESCHEDULED, PENDING,
)
from callback.tests.fixtures import ServiceFixture, utc

BASE = utc(2026, 10, 7, 10, 0)


class CompleteAndRescheduleTest(ServiceFixture):
    def test_complete_requires_result_and_next_step(self):
        cid = self.add_customer()
        pid = self.make_promise(cid, BASE + timedelta(hours=1))
        with self.assertRaises(InvalidTransition):
            self.svc.complete(pid, self.agent, "", "下一步")
        with self.assertRaises(InvalidTransition):
            self.svc.complete(pid, self.agent, "结果", "  ")

    def test_complete_writes_fields_and_event(self):
        cid = self.add_customer()
        pid = self.make_promise(cid, BASE + timedelta(hours=1))
        self.svc.complete(pid, self.agent, "客户确认方案", "明日回访")
        row = self.svc.get_promise(pid)
        self.assertEqual(row["status"], COMPLETED)
        self.assertEqual(row["result"], "客户确认方案")
        self.assertEqual(row["next_step"], "明日回访")
        self.assertIsNotNone(row["completed_at"])

    def test_reschedule_needs_basis_text(self):
        cid = self.add_customer()
        pid = self.make_promise(cid, BASE + timedelta(hours=1))
        self.phone.dial(pid, self.agent, "139-1", outcome=NO_ANSWER)
        with self.assertRaises(InvalidTransition):
            self.svc.reschedule(pid, self.agent, new_time=BASE + timedelta(hours=2),
                                basis="  ", basis_type="no_answer")

    def test_reschedule_no_answer_requires_call_evidence(self):
        cid = self.add_customer()
        pid = self.make_promise(cid, BASE + timedelta(hours=1))
        with self.assertRaises(InvalidTransition):
            self.svc.reschedule(pid, self.agent, new_time=BASE + timedelta(hours=2),
                                basis="声称未接", basis_type="no_answer")

    def test_reschedule_success_chains_promises(self):
        cid = self.add_customer()
        pid = self.make_promise(cid, BASE + timedelta(hours=1))
        self.phone.dial(pid, self.agent, "139-1", outcome=NO_ANSWER)
        new_id = self.svc.reschedule(
            pid, self.agent, new_time=BASE + timedelta(hours=3),
            basis="两次未接，客户留言晚上可接", basis_type="no_answer")
        self.assertEqual(self.svc.get_promise(pid)["status"], RESCHEDULED)
        new = self.svc.get_promise(new_id)
        self.assertEqual(new["status"], PENDING)
        self.assertEqual(new["supersedes_id"], pid)
        self.assertEqual(new["parent_id"], pid)

    def test_customer_request_reschedule_needs_no_call(self):
        cid = self.add_customer()
        pid = self.make_promise(cid, BASE + timedelta(hours=1))
        new_id = self.svc.reschedule(
            pid, self.agent, new_time=BASE + timedelta(hours=4),
            basis="客户主动来电要求改到下午", basis_type="customer_request")
        self.assertEqual(self.svc.get_promise(new_id)["status"], PENDING)


class CancellationTest(ServiceFixture):
    def test_wrong_number_requires_evidence(self):
        cid = self.add_customer()
        pid = self.make_promise(cid, BASE + timedelta(hours=1))
        with self.assertRaises(InvalidTransition):
            self.svc.cancel_wrong_number(pid, self.agent)
        self.phone.dial(pid, self.agent, "139-x", outcome=WRONG_NUMBER)
        self.svc.cancel_wrong_number(pid, self.agent)
        self.assertEqual(self.svc.get_promise(pid)["status"], CANCEL_WRONG_NUMBER)

    def test_refused_requires_evidence(self):
        cid = self.add_customer()
        pid = self.make_promise(cid, BASE + timedelta(hours=1))
        with self.assertRaises(InvalidTransition):
            self.svc.cancel_refused(pid, self.agent)
        self.phone.dial(pid, self.agent, "139-x", outcome=REFUSED)
        self.svc.cancel_refused(pid, self.agent)
        self.assertEqual(self.svc.get_promise(pid)["status"], CANCEL_REFUSED)

    def test_resolved_elsewhere_requires_channel(self):
        cid = self.add_customer()
        pid = self.make_promise(cid, BASE + timedelta(hours=1))
        with self.assertRaises(InvalidTransition):
            self.svc.cancel_resolved_elsewhere(pid, self.agent, "")
        self.svc.cancel_resolved_elsewhere(pid, self.agent, "App 在线客服")
        self.assertEqual(self.svc.get_promise(pid)["status"], CANCEL_RESOLVED)

    def test_duplicate_cancel_keeps_other(self):
        cid = self.add_customer()
        keep = self.make_promise(cid, BASE + timedelta(hours=2), session="a")
        dup = self.make_promise(cid, BASE + timedelta(hours=2, minutes=70),
                                session="b")  # 超出 60 分钟去重窗口
        with self.assertRaises(InvalidTransition):
            self.svc.cancel_duplicate(dup, self.agent, dup)  # 不能指向自己
        self.svc.cancel_duplicate(dup, self.agent, keep)
        self.assertEqual(self.svc.get_promise(dup)["status"], CANCEL_DUPLICATE)
        self.assertEqual(self.svc.get_promise(keep)["status"], PENDING)

    def test_closed_promise_cannot_cancel_again(self):
        cid = self.add_customer()
        pid = self.make_promise(cid, BASE + timedelta(hours=1))
        self.svc.cancel_resolved_elsewhere(pid, self.agent, "门店")
        with self.assertRaises(InvalidTransition):
            self.svc.cancel_refused(pid, self.agent)


class DuplicateCreateTest(ServiceFixture):
    def test_same_session_blocks_duplicate(self):
        cid = self.add_customer()
        self.make_promise(cid, BASE + timedelta(hours=1), session="same-sess")
        with self.assertRaises(DuplicatePromiseError) as cm:
            self.make_promise(cid, BASE + timedelta(days=2), session="same-sess")
        self.assertEqual(cm.exception.existing_id, 1)

    def test_close_in_time_blocks_duplicate(self):
        cid = self.add_customer()
        self.make_promise(cid, BASE + timedelta(hours=1), session="s1")
        with self.assertRaises(DuplicatePromiseError):
            self.make_promise(cid, BASE + timedelta(hours=1, minutes=20), session="s2")

    def test_outside_window_allowed(self):
        cid = self.add_customer()
        self.make_promise(cid, BASE + timedelta(hours=1), session="s1")
        pid2 = self.make_promise(cid, BASE + timedelta(hours=2, minutes=1),
                                 session="s2")
        self.assertGreater(pid2, 0)

    def test_different_customer_not_duplicate(self):
        c1 = self.add_customer("甲", phone="139-00000001")
        c2 = self.add_customer("乙", phone="139-00000002")
        self.make_promise(c1, BASE + timedelta(hours=1), session="s")
        pid = self.make_promise(c2, BASE + timedelta(hours=1), session="s")
        self.assertGreater(pid, 0)


class AdapterTest(ServiceFixture):
    def test_fake_recording_file_created(self):
        cid = self.add_customer()
        pid = self.make_promise(cid, BASE + timedelta(hours=1))
        call = self.phone.dial(pid, self.agent, "139-00000009", outcome=NO_ANSWER)
        self.assertEqual(call["outcome"], NO_ANSWER)
        self.assertTrue(os.path.isfile(call["recording"]))
        with open(call["recording"], encoding="utf-8") as fh:
            self.assertIn("虚构录音", fh.read())

    def test_fixed_outcome_dial(self):
        cid = self.add_customer()
        pid = self.make_promise(cid, BASE + timedelta(hours=1))
        call = self.phone.dial(pid, self.agent, "139-1", outcome="connected", duration_s=90)
        self.assertEqual(call["duration_s"], 90)

    def test_sms_is_local_only(self):
        cid = self.add_customer()
        pid = self.make_promise(cid, BASE + timedelta(hours=1))
        from callback.adapters import SmsAdapter
        sms = SmsAdapter(self.conn)
        row = sms.send(cid, "回拨提醒（模拟）", pid)
        self.assertEqual(row["direction"], "outbound")
        self.assertEqual(row["body"], "回拨提醒（模拟）")
