"""标准库 HTTP 服务：JSON API + 静态页面，零第三方依赖。

启动：python -m callback [--host 127.0.0.1] [--port 8000] [--reset]
首次启动自动建库并注入含逾期样例的种子数据。
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import threading
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from . import db, reports, seed
from .adapters import PhoneAdapter, SmsAdapter
from .service import (
    PromiseService, ServiceError, DncViolation, DuplicatePromiseError,
    OwnershipConflict, PermissionDenied, InvalidTransition,
)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
WEB_DIR = os.path.join(BASE_DIR, "web")
DEFAULT_DB = os.path.join(BASE_DIR, "data", "callback.db")
RECORDINGS_DIR = os.path.join(BASE_DIR, "data", "recordings")

STATUS_HTTP = {
    DncViolation: 422,
    DuplicatePromiseError: 409,
    OwnershipConflict: 409,
    PermissionDenied: 403,
    InvalidTransition: 400,
    ServiceError: 400,
}


class AppState:
    def __init__(self, db_path: str, reset: bool = False) -> None:
        if reset and os.path.exists(db_path):
            for suffix in ("", "-wal", "-shm"):
                p = db_path + suffix
                if os.path.exists(p):
                    os.remove(p)
        os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
        self.conn = sqlite3.connect(db_path, check_same_thread=False, timeout=15,
                                    isolation_level=None)
        self.lock = threading.RLock()  # 串行化对共享连接的访问
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA journal_mode = WAL")
        db.init_db(self.conn)
        self.svc = PromiseService(self.conn)
        self.phone = PhoneAdapter(self.conn, RECORDINGS_DIR)
        self.sms = SmsAdapter(self.conn)
        seed.seed(self.conn, RECORDINGS_DIR)
        self.db_path = db_path


def _iso_to_dt(s: str):
    return db.parse_iso(s)


class Handler(BaseHTTPRequestHandler):
    server_version = "CallbackMgr/1.0"

    # ---- 工具 ----
    @property
    def state(self) -> AppState:
        return self.server.app_state  # type: ignore[attr-defined]

    def _send_json(self, payload, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError:
            raise InvalidTransition("请求体不是合法 JSON")
        if not isinstance(data, dict):
            raise InvalidTransition("请求体必须是 JSON 对象")
        return data

    def _error(self, exc: Exception) -> None:
        status = STATUS_HTTP.get(type(exc), 500)
        self._send_json({"error": type(exc).__name__, "message": str(exc)}, status)

    def log_message(self, fmt, *args):  # 简洁日志
        sys.stderr.write("[http] " + (fmt % args) + "\n")

    # ---- 静态 ----
    def _serve_static(self, rel: str) -> None:
        if not rel:
            rel = "index.html"
        path = os.path.normpath(os.path.join(WEB_DIR, rel))
        if not path.startswith(WEB_DIR) or not os.path.isfile(path):
            self.send_error(404)
            return
        ctype = {
            ".html": "text/html; charset=utf-8",
            ".js": "application/javascript; charset=utf-8",
            ".css": "text/css; charset=utf-8",
            ".svg": "image/svg+xml",
        }.get(os.path.splitext(path)[1], "application/octet-stream")
        with open(path, "rb") as fh:
            body = fh.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # ---- 业务数据 ----
    def _agents(self):
        return [dict(r) for r in self.state.conn.execute(
            "SELECT id,name,role,on_leave,active FROM agents ORDER BY role, id"
        ).fetchall()]

    def _customers(self):
        return [dict(r) for r in self.state.conn.execute(
            "SELECT id,name,phone,tz_offset FROM customers ORDER BY id"
        ).fetchall()]

    def _timeline(self, promise_id: int):
        rows = self.state.conn.execute(
            """SELECT e.*, a.name AS actor_name FROM events e
               LEFT JOIN agents a ON a.id=e.actor_id
               WHERE e.promise_id=? ORDER BY e.id""",
            (promise_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    # ---- 路由 ----
    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path.strip("/")
        qs = parse_qs(parsed.query)
        try:
            with self.state.lock:
                if path == "api/state":
                    owner_id = qs.get("owner_id", [None])[0]
                    status = qs.get("status", [None])[0]
                    self._send_json({
                        "now": db.now_iso(),
                        "agents": self._agents(),
                        "customers": self._customers(),
                        "windows": reports.customer_windows(self.state.conn),
                        "queue": reports.promise_queue(
                            self.state.conn,
                            owner_id=int(owner_id) if owner_id else None,
                            status=status or None,
                        ),
                        "calls": reports.call_records(self.state.conn),
                        "report": reports.reassignment_and_breach_report(self.state.conn),
                        "sms": [dict(r) for r in self.state.conn.execute(
                            "SELECT * FROM sms ORDER BY id DESC LIMIT 50").fetchall()],
                    })
                elif path.startswith("api/customer/"):
                    cid = int(path.rsplit("/", 1)[1])
                    cust = db.get(self.state.conn, "SELECT * FROM customers WHERE id=?", (cid,))
                    if cust is None:
                        raise ServiceError("客户不存在")
                    promises = self.state.conn.execute(
                        """SELECT p.*, a.name AS owner_name, s.name AS closed_by_name
                           FROM promises p
                           JOIN agents a ON a.id=p.owner_id
                           LEFT JOIN agents s ON s.id=p.closed_by_id
                           WHERE p.customer_id=? ORDER BY p.promised_at DESC""",
                        (cid,),
                    ).fetchall()
                    out = []
                    for p in promises:
                        item = dict(p)
                        item["owner_name"] = p["owner_name"]
                        item["closed_by_name"] = p["closed_by_name"]
                        item["events"] = self._timeline(p["id"])
                        item["calls"] = reports.call_records(self.state.conn, promise_id=p["id"])
                        out.append(item)
                    self._send_json({
                        "customer": dict(cust),
                        "windows": reports.customer_windows(self.state.conn, cid),
                        "promises": out,
                    })
                elif path == "api/report":
                    self._send_json(reports.reassignment_and_breach_report(self.state.conn))
                elif path.startswith("api/"):
                    self.send_error(404)
                else:
                    self._serve_static(path)
        except Exception as exc:  # noqa: BLE001
            if isinstance(exc, ServiceError):
                self._error(exc)
            else:
                raise

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        parts = [p for p in parsed.path.strip("/").split("/") if p]
        data = {}
        try:
            data = self._read_json()
            with self.state.lock:
                self._route_post(parts, data)
        except Exception as exc:  # noqa: BLE001
            if isinstance(exc, ServiceError):
                self._error(exc)
            else:
                raise

    def _route_post(self, parts: list[str], data: dict) -> None:
        st = self.state
        # /api/sweep
        if parts == ["api", "sweep"]:
            ids = st.svc.sweep_overdue()
            return self._send_json({"expired_ids": ids})

        if parts[:1] != ["api"]:
            self.send_error(404)
            return

        # 集合级动作
        if parts[1:] == ["promises"]:
            pid = st.svc.create_promise(
                customer_id=int(data["customer_id"]),
                created_by_id=int(data["actor_id"]),
                owner_id=int(data.get("owner_id") or data["actor_id"]),
                summary=str(data["summary"]),
                origin_session=str(data["origin_session"]),
                promised_at=_iso_to_dt(str(data["promised_at"])),
            )
            return self._send_json({"id": pid}, 201)

        if parts[1] == "agents" and len(parts) == 4 and parts[3] == "leave":
            moved = st.svc.handover_leave_to_duty(int(parts[2]))
            return self._send_json({"handed_over": moved})

        # /api/promises/{id}/{action}
        if parts[1] == "promises" and len(parts) == 4:
            pid, action = int(parts[2]), parts[3]
            actor = int(data["actor_id"])
            if action == "complete":
                st.svc.complete(pid, actor, str(data["result"]), str(data["next_step"]))
            elif action == "reschedule":
                new_id = st.svc.reschedule(
                    pid, actor,
                    new_time=_iso_to_dt(str(data["new_time"])),
                    basis=str(data["basis"]),
                    basis_type=str(data.get("basis_type", "no_answer")),
                )
                return self._send_json({"new_promise_id": new_id})
            elif action == "cancel":
                reason = str(data["reason"])
                if reason == "wrong_number":
                    st.svc.cancel_wrong_number(pid, actor)
                elif reason == "refused":
                    st.svc.cancel_refused(pid, actor)
                elif reason == "resolved_elsewhere":
                    st.svc.cancel_resolved_elsewhere(pid, actor, str(data["channel"]))
                elif reason == "duplicate":
                    st.svc.cancel_duplicate(pid, actor, int(data["kept_promise_id"]))
                else:
                    raise InvalidTransition(f"未知取消原因 {reason}")
            elif action == "dial":
                outcome = data.get("outcome") or None
                row = st.phone.dial(
                    pid, actor, str(data["phone"]),
                    duration_s=int(data.get("duration_s") or 0),
                    outcome=outcome,
                )
                return self._send_json({"call": dict(row)}, 201)
            elif action == "sms":
                row = st.sms.send(int(data["customer_id"]), str(data["body"]), pid)
                return self._send_json({"sms": dict(row)}, 201)
            elif action == "reassign":
                version = st.svc.reassign(
                    pid, actor, int(data["to_agent_id"]),
                    expected_version=int(data["expected_version"]),
                    reason=str(data.get("reason", "manual")),
                )
                return self._send_json({"owner_version": version})
            elif action == "dispute":
                st.svc.close_dispute(pid, actor, str(data["note"]))
            elif action == "delete":
                st.svc.delete_promise(pid, actor)
            else:
                raise InvalidTransition(f"未知动作 {action}")
            return self._send_json({"ok": True})

        raise InvalidTransition("未知接口")


def build_server(host: str, port: int, db_path: str, reset: bool = False) -> ThreadingHTTPServer:
    app = AppState(db_path, reset=reset)
    httpd = ThreadingHTTPServer((host, port), Handler)
    httpd.app_state = app  # type: ignore[attr-defined]
    return httpd


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="客服回拨承诺管理系统（本地模拟）")
    parser.add_argument("--host", default=os.environ.get("CALLBACK_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("CALLBACK_PORT", "8000")))
    parser.add_argument("--db", default=os.environ.get("CALLBACK_DB", DEFAULT_DB))
    parser.add_argument("--reset", action="store_true", help="重置数据库并重新注入样例")
    args = parser.parse_args(argv)

    httpd = build_server(args.host, args.port, args.db, reset=args.reset)
    print(f"客服回拨承诺管理系统已启动: http://{args.host}:{args.port}")
    print(f"数据库: {os.path.abspath(args.db)}（录音占位: {RECORDINGS_DIR}）")
    print("按 Ctrl+C 停止。")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
