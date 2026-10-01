"""HTTP 层冒烟测试：身份切换、权限拒绝（409）、主管裁决闭环。

直接运行：python3 -m unittest discover -s tests -v
"""
from __future__ import annotations

import sys
import tempfile
import threading
import unittest
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from callback import config, services  # noqa: E402
from callback import web  # noqa: E402
from callback.adapters import MockPhone  # noqa: E402
from callback.db import connect, fetchone, init_db  # noqa: E402


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """覆盖重定向处理：让 301/302/303 直接作为 HTTPError 抛出。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class WebCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.dir = Path(cls.tmp.name)
        cls._old_db, cls._old_rec = config.DB_PATH, config.REC_DIR
        config.DB_PATH = cls.dir / "web.db"
        config.REC_DIR = cls.dir / "rec"
        conn = connect(str(config.DB_PATH))
        init_db(conn)
        MockPhone(conn)
        cls.agent = services.create_agent(conn, "坐席一")
        cls.agent2 = services.create_agent(conn, "坐席二")
        cls.sup = services.create_agent(conn, "主管", role="supervisor")
        cls.cid = services.create_customer(
            conn, "客户", "138-0000-0001", quiet_from="22:00", quiet_to="08:00")
        future = (datetime.now() + timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M")
        cls.pid = services.create_promise(
            conn, agent_id=cls.agent, customer_id=cls.cid,
            summary="测试问题", origin_session="会话 #1",
            reachable_window="工作时间", promised_at=future)
        # 第二条承诺拨错号 -> 争议
        cid2 = services.create_customer(conn, "客户二", "138-0000-0002")
        cls.dispute_pid = services.create_promise(
            conn, agent_id=cls.agent, customer_id=cid2,
            summary="实名问题", origin_session="会话 #2",
            reachable_window="全天", promised_at=future)
        services.dial_promise(conn, promise_id=cls.dispute_pid,
                              agent_id=cls.agent, result="wrong_number")
        conn.commit()
        conn.close()

        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), web.App)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        config.DB_PATH, config.REC_DIR = cls._old_db, cls._old_rec
        cls.tmp.cleanup()

    def url(self, path: str, as_id: int) -> str:
        return f"http://127.0.0.1:{self.port}{path}?as={as_id}"

    def get(self, path, as_id):
        with urllib.request.urlopen(self.url(path, as_id)) as r:
            return r.status, r.read().decode("utf-8")

    def post(self, path, data: dict, expect_error: bool = False):
        body = urllib.parse.urlencode(data).encode()
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}", data=body, method="POST")
        # 自定义 opener：不自动跟随 303，便于显式断言重定向
        opener = urllib.request.build_opener(_NoRedirectHandler())
        try:
            r = opener.open(req)
            return r.status, r.read().decode("utf-8")
        except urllib.error.HTTPError as e:
            # 409 等业务拒绝
            return e.code, e.read().decode("utf-8")

    def test_pages_render_for_agent_and_supervisor(self):
        for path in ("/queue", "/customers", "/calls", "/transfers"):
            status, _ = self.get(path, self.agent)
            self.assertEqual(status, 200)
        status, text = self.get("/supervisor", self.sup)
        self.assertEqual(status, 200)
        self.assertIn("失约报表", text)

    def test_agent_sees_supervisor_guard(self):
        status, text = self.get("/supervisor", self.agent)
        self.assertEqual(status, 200)
        self.assertIn("当前身份不是主管", text)

    def test_agent_cannot_close_dispute_http_409(self):
        status, text = self.post("/act/dispute-close", {
            "as": self.agent, "promise_id": self.dispute_pid,
            "note": "坐席强行关闭"}, expect_error=True)
        self.assertEqual(status, 409)
        self.assertIn("需要主管权限", text)

    def test_blank_note_rejected_not_500(self):
        status, _ = self.post("/act/dispute-close", {
            "as": self.sup, "promise_id": self.dispute_pid, "note": ""},
            expect_error=True)
        self.assertEqual(status, 409)

    def test_supervisor_closes_dispute(self):
        status, _ = self.post("/act/dispute-close", {
            "as": self.sup, "promise_id": self.dispute_pid,
            "note": "核实关闭"}, )
        self.assertEqual(status, 303)
        conn = connect(str(config.DB_PATH))
        row = fetchone(conn, "SELECT status FROM promises WHERE id=?",
                       (self.dispute_pid,))
        self.assertEqual(row["status"], services.CLOSED_DISPUTE)
        conn.close()

    def test_transfer_conflict_http_409(self):
        # 第一次转派成功
        self.post("/act/transfer", {
            "as": self.agent, "promise_id": self.pid,
            "to_agent_id": self.agent2, "version": "1", "reason": "交接"})
        # 持旧版本号再转（原负责人历史持有，走 CAS 失败而非越权）
        status, text = self.post("/act/transfer", {
            "as": self.agent, "promise_id": self.pid,
            "to_agent_id": self.sup, "version": "1",
            "reason": "迟到的转派"}, expect_error=True)
        self.assertEqual(status, 409)
        self.assertIn("已被他人先转派", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
