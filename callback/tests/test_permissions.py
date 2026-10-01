"""主管权限测试：逾期记录不可删除；只有主管能关闭争议；
误建记录的删除也仅限主管，且有拨打记录后一律不可删除。"""
from __future__ import annotations

from datetime import timedelta

from callback.service import (
    DISPUTE_CLOSED, EXPIRED, PermissionDenied, InvalidTransition,
)
from callback.tests.fixtures import ServiceFixture, utc

BASE = utc(2026, 10, 7, 10, 0)


class SupervisorPermissionTest(ServiceFixture):
    def test_agent_cannot_delete_overdue(self):
        cid = self.add_customer()
        pid = self.make_promise(cid, BASE - timedelta(hours=2))
        self.svc.sweep_overdue(now=BASE)
        with self.assertRaises(PermissionDenied):
            self.svc.delete_promise(pid, self.agent)
        # 记录仍然存在
        self.assertEqual(self.svc.get_promise(pid)["status"], EXPIRED)

    def test_supervisor_also_cannot_delete_overdue_must_close_dispute(self):
        """逾期记录对任何角色都不可物理删除；主管只能走关闭争议。"""
        cid = self.add_customer()
        pid = self.make_promise(cid, BASE - timedelta(hours=2))
        self.svc.sweep_overdue(now=BASE)
        with self.assertRaises(PermissionDenied):
            self.svc.delete_promise(pid, self.supervisor)
        self.svc.close_dispute(pid, self.supervisor, "核实为客户电话停机，责任豁免")
        self.assertEqual(self.svc.get_promise(pid)["status"], DISPUTE_CLOSED)
        # 关闭后仍不可删除
        with self.assertRaises(PermissionDenied):
            self.svc.delete_promise(pid, self.supervisor)

    def test_agent_cannot_close_dispute(self):
        cid = self.add_customer()
        pid = self.make_promise(cid, BASE - timedelta(hours=2))
        self.svc.sweep_overdue(now=BASE)
        with self.assertRaises(PermissionDenied):
            self.svc.close_dispute(pid, self.agent, "坐席想自行关闭")

    def test_close_dispute_requires_note(self):
        cid = self.add_customer()
        pid = self.make_promise(cid, BASE - timedelta(hours=2))
        self.svc.sweep_overdue(now=BASE)
        with self.assertRaises(InvalidTransition):
            self.svc.close_dispute(pid, self.supervisor, "   ")

    def test_only_expired_can_be_dispute_closed(self):
        cid = self.add_customer()
        pid = self.make_promise(cid, BASE + timedelta(hours=1))
        with self.assertRaises(InvalidTransition):
            self.svc.close_dispute(pid, self.supervisor, "正常承诺无需关闭")

    def test_agent_cannot_delete_even_pending(self):
        cid = self.add_customer()
        pid = self.make_promise(cid, BASE + timedelta(hours=1))
        with self.assertRaises(PermissionDenied):
            self.svc.delete_promise(pid, self.agent)

    def test_supervisor_can_delete_miscreated_without_calls(self):
        """主管可以删除完全没有拨打记录的误建承诺。"""
        cid = self.add_customer()
        pid = self.make_promise(cid, BASE + timedelta(hours=1))
        self.svc.delete_promise(pid, self.supervisor)
        with self.assertRaises(Exception):
            self.svc.get_promise(pid)

    def test_record_with_calls_cannot_be_deleted_by_anyone(self):
        cid = self.add_customer()
        pid = self.make_promise(cid, BASE + timedelta(hours=1))
        self.phone.dial(pid, self.agent, "139-00000001", outcome="no_answer")
        with self.assertRaises(PermissionDenied):
            self.svc.delete_promise(pid, self.supervisor)

    def test_supervisor_can_complete_for_agent(self):
        """主管可代坐席回填结果。"""
        cid = self.add_customer()
        pid = self.make_promise(cid, BASE + timedelta(hours=1), owner=self.agent2)
        self.svc.complete(pid, self.supervisor, "主管代为联系已解决", "回访关单")
        row = self.svc.get_promise(pid)
        self.assertEqual(row["result"], "主管代为联系已解决")
