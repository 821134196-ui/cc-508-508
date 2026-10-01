"""并发转派竞争测试：同一承诺被两个坐席同时转派，只能有一个有效负责人。"""
from __future__ import annotations

import threading
from datetime import timedelta

from callback import db
from callback.service import (
    OwnershipConflict, PermissionDenied, PromiseService,
)
from callback.tests.fixtures import ServiceFixture, utc

BASE = utc(2026, 10, 7, 10, 0)


class ReassignConcurrencyTest(ServiceFixture):
    def _open_service(self) -> PromiseService:
        conn = db.connect(self.db_path, check_same_thread=False)
        self.addCleanup(conn.close)
        return PromiseService(conn)

    def test_concurrent_reassign_only_one_wins(self):
        cid = self.add_customer()
        pid = self.make_promise(cid, BASE + timedelta(hours=1))
        version = self.svc.get_promise(pid)["owner_version"]
        self.assertEqual(version, 0)

        svc_b = self._open_service()
        results: dict[str, object] = {}
        barrier = threading.Barrier(2)

        def move(name, svc, actor, target):
            try:
                barrier.wait(timeout=5)
                v = svc.reassign(pid, actor, target,
                                 expected_version=version, reason="manual")
                results[name] = ("ok", v)
            except OwnershipConflict as e:
                results[name] = ("conflict", str(e))
            except Exception as e:  # noqa: BLE001
                results[name] = ("error", repr(e))

        t1 = threading.Thread(target=move, args=("A", self.svc, self.agent, self.agent2))
        t2 = threading.Thread(target=move, args=("B", svc_b, self.agent, self.agent3))
        t1.start(); t2.start()
        t1.join(5); t2.join(5)

        outcomes = sorted(results.values(), key=lambda x: x[0])
        statuses = sorted(k for k, _ in results.values())
        self.assertIn("conflict", statuses, f"应有一个转派失败: {results}")
        self.assertIn("ok", statuses, f"应有一个转派成功: {results}")

        # 最终只有一个有效负责人，版本号只前进一次
        row = self.svc.get_promise(pid)
        self.assertEqual(row["owner_version"], 1)
        self.assertIn(row["owner_id"], (self.agent2, self.agent3))
        winners = [r for r in results.values() if r[0] == "ok"]
        self.assertEqual(len(winners), 1)

        # 所有权日志恰好一条，from -> 唯一新负责人
        logs = self.conn.execute(
            "SELECT * FROM ownership_log WHERE promise_id=?", (pid,)).fetchall()
        self.assertEqual(len(logs), 1)
        self.assertEqual(logs[0]["to_agent_id"], row["owner_id"])

    def test_stale_client_refresh_then_succeeds(self):
        """失败方刷新版本后重试：原坐席已交出负责人身份，由主管转派成功。"""
        cid = self.add_customer()
        pid = self.make_promise(cid, BASE + timedelta(hours=1))
        svc_b = self._open_service()

        self.svc.reassign(pid, self.agent, self.agent2, expected_version=0)
        with self.assertRaises(OwnershipConflict):
            svc_b.reassign(pid, self.agent, self.agent3, expected_version=0)
        fresh = svc_b.get_promise(pid)["owner_version"]
        v = svc_b.reassign(pid, self.supervisor, self.agent3, expected_version=fresh)
        self.assertEqual(v, 2)
        self.assertEqual(self.svc.get_promise(pid)["owner_id"], self.agent3)

    def test_non_owner_agent_cannot_reassign(self):
        cid = self.add_customer()
        pid = self.make_promise(cid, BASE + timedelta(hours=1), owner=self.agent)
        with self.assertRaises(PermissionDenied):
            self.svc.reassign(pid, self.agent2, self.agent3, expected_version=0)

    def test_cannot_reassign_to_leave_agent(self):
        cid = self.add_customer()
        pid = self.make_promise(cid, BASE + timedelta(hours=1))
        self.svc.handover_leave_to_duty(self.agent2)  # 阿乙请假
        with self.assertRaises(Exception):
            self.svc.reassign(pid, self.agent, self.agent2, expected_version=0)

    def test_leave_handover_moves_all_open_promises(self):
        """坐席请假：其未完成承诺全部转交值班组，且每一条记录交接链。"""
        cid = self.add_customer()
        p1 = self.make_promise(cid, BASE + timedelta(hours=1), owner=self.agent)
        p2 = self.make_promise(cid, BASE + timedelta(hours=2),
                               owner=self.agent, session="sess-x")
        # 已完成的不交接
        p3 = self.make_promise(cid, BASE - timedelta(hours=5),
                               owner=self.agent, session="sess-done")
        self.svc.complete(p3, self.agent, "完成", "关单")

        moved = self.svc.handover_leave_to_duty(self.agent)
        self.assertEqual(sorted(moved), sorted([p1, p2]))
        for pid in (p1, p2):
            row = self.svc.get_promise(pid)
            self.assertEqual(row["owner_id"], self.duty)
        self.assertEqual(self.svc.get_promise(p3)["owner_id"], self.agent)

        logs = self.conn.execute(
            "SELECT reason, to_agent_id FROM ownership_log WHERE promise_id=?",
            (p1,)).fetchall()
        self.assertEqual(len(logs), 1)
        self.assertEqual(logs[0]["reason"], "leave_handover")
        self.assertEqual(logs[0]["to_agent_id"], self.duty)
