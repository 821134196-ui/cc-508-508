"""HTTP API 端到端测试：在真实端口上启动服务并打 JSON 接口。"""
from __future__ import annotations

import json
import threading
import urllib.request
import urllib.error
from datetime import timedelta
from urllib.parse import quote

from callback.server import build_server
from callback import db
from callback.tests.fixtures import ServiceFixture, utc

BASE = utc(2026, 10, 7, 10, 0)


def _req(method, url, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        url, data=data, method=method,
        headers={"Content-Type": "application/json"} if data else {})
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode())


class HttpApiTest(ServiceFixture):
    def setUp(self):
        super().setUp()
        self.conn.close()  # HTTP 服务独占使用该库文件
        self.httpd = build_server("127.0.0.1", 0, self.db_path, reset=False)
        # 库已被夹具初始化，AppState 会直接复用；补入最小业务数据
        self.port = self.httpd.server_address[1]
        self.base = f"http://127.0.0.1:{self.port}"
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        rows = self.httpd.app_state.conn.execute(
            "SELECT id,name,role FROM agents ORDER BY id").fetchall()
        self.ids = {r["name"]: r["id"] for r in rows}
        self.agent = self.ids["阿甲"]
        self.agent2 = self.ids["阿乙"]
        self.supervisor = self.ids["主管"]

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=3)
        super().tearDown()

    def _new_customer_via_db(self, name="接口客户", phone="139-77770001"):
        c = self.httpd.app_state.conn
        with db.transaction(c):
            return c.execute(
                "INSERT INTO customers(name,phone,tz_offset,created_at) VALUES(?,?,0,?)",
                (name, phone, db.now_iso())).lastrowid

    def test_state_endpoint(self):
        status, d = _req("GET", self.base + "/api/state")
        self.assertEqual(status, 200)
        self.assertIn("queue", d)
        self.assertIn("report", d)

    def test_create_and_complete_flow(self):
        cid = self._new_customer_via_db()
        when = (BASE + timedelta(hours=1)).isoformat()
        status, d = _req("POST", self.base + "/api/promises", {
            "customer_id": cid, "actor_id": self.agent,
            "summary": "接口建单", "origin_session": "api-1",
            "promised_at": when})
        self.assertEqual(status, 201, d)
        pid = d["id"]

        status, d = _req("POST", f"{self.base}/api/promises/{pid}/complete", {
            "actor_id": self.agent, "result": "已解决", "next_step": "关单"})
        self.assertEqual(status, 200, d)

    def test_dnc_rejected_over_http(self):
        cid = self._new_customer_via_db("禁扰客户", "139-77770002")
        c = self.httpd.app_state.conn
        with db.transaction(c):
            c.execute(
                """INSERT INTO availability_windows(customer_id,kind,weekday,
                    start_minute,end_minute,note) VALUES (?,?,?,?,?,?)""",
                (cid, "dnc", 2, 0, 1440, "周三全天"))
        status, d = _req("POST", self.base + "/api/promises", {
            "customer_id": cid, "actor_id": self.agent,
            "summary": "撞禁扰", "origin_session": "api-dnc",
            "promised_at": utc(2026, 10, 7, 12, 0).isoformat()})
        self.assertEqual(status, 422)
        self.assertEqual(d["error"], "DncViolation")

    def test_concurrent_reassign_over_http_one_conflict(self):
        cid = self._new_customer_via_db("并发客户", "139-77770003")
        status, d = _req("POST", self.base + "/api/promises", {
            "customer_id": cid, "actor_id": self.agent,
            "summary": "并发转派", "origin_session": "api-race",
            "promised_at": (BASE + timedelta(hours=1)).isoformat()})
        pid = d["id"]

        results = {}
        barrier = threading.Barrier(2)

        def call(key, target_name):
            barrier.wait(5)
            results[key] = _req("POST", f"{self.base}/api/promises/{pid}/reassign", {
                "actor_id": self.agent,
                "to_agent_id": self.ids[target_name],
                "expected_version": 0, "reason": "manual"})

        t1 = threading.Thread(target=call, args=("a", "阿乙"))
        t2 = threading.Thread(target=call, args=("b", "阿丙"))
        t1.start(); t2.start(); t1.join(5); t2.join(5)
        codes = sorted(r[0] for r in results.values())
        self.assertEqual(codes, [200, 409], results)

        status, d = _req("GET", f"{self.base}/api/customer/{cid}")
        p = next(x for x in d["promises"] if x["id"] == pid)
        self.assertEqual(p["owner_version"], 1)
        self.assertIn(p["owner_name"], ("阿乙", "阿丙"))

    def test_agent_close_dispute_forbidden(self):
        cid = self._new_customer_via_db("逾期客户", "139-77770004")
        status, d = _req("POST", self.base + "/api/promises", {
            "customer_id": cid, "actor_id": self.agent,
            "summary": "将要逾期", "origin_session": "api-exp",
            "promised_at": (BASE - timedelta(hours=2)).isoformat()})
        pid = d["id"]
        _req("POST", self.base + "/api/sweep", {})
        status, d = _req("POST", f"{self.base}/api/promises/{pid}/dispute", {
            "actor_id": self.agent, "note": "坐席尝试关闭"})
        self.assertEqual(status, 403)

    def test_static_index_served(self):
        with urllib.request.urlopen(self.base + "/", timeout=5) as resp:
            body = resp.read().decode()
        self.assertIn("回拨承诺管理台", body)
