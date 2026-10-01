"""报表查询：承诺队列、客户时段、拨打记录、转派与失约报表。"""
from __future__ import annotations

import sqlite3
from datetime import timedelta
from typing import Optional

from . import db
from .service import CLOSED_STATUSES, EXPIRED, PENDING

WEEKDAY_CN = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]


def _fmt_window(row: sqlite3.Row) -> str:
    def hm(m: int) -> str:
        return f"{m // 60:02d}:{m % 60:02d}"

    end = row["end_minute"]
    end_txt = hm(end) if end < 1440 else "24:00"
    span = f"{hm(row['start_minute'])}–{end_txt}"
    if row["end_minute"] <= row["start_minute"]:
        span += "（跨午夜）"
    kind = "禁扰" if row["kind"] == "dnc" else "可接听"
    note = f" {row['note']}" if row["note"] else ""
    return f"{WEEKDAY_CN[row['weekday']]} {span} {kind}{note}"


def promise_queue(
    conn: sqlite3.Connection,
    *,
    owner_id: Optional[int] = None,
    status: Optional[str] = None,
    now: Optional[db.datetime] = None,
) -> list[dict]:
    """承诺队列，按承诺时刻排序；附倒计时秒数与逾期标记。"""
    now = now or db.parse_iso(db.now_iso())
    sql = [
        """SELECT p.*, c.name AS customer_name, c.phone AS customer_phone,
                  a.name AS owner_name
           FROM promises p
           JOIN customers c ON c.id = p.customer_id
           JOIN agents a ON a.id = p.owner_id
           WHERE 1=1"""
    ]
    params: list = []
    if owner_id is not None:
        sql.append("AND p.owner_id=?")
        params.append(owner_id)
    if status is not None:
        sql.append("AND p.status=?")
        params.append(status)
    sql.append("ORDER BY p.promised_at ASC")
    rows = conn.execute(" ".join(sql), params).fetchall()
    out: list[dict] = []
    for r in rows:
        promised = db.parse_iso(r["promised_at"])
        deadline = db.parse_iso(r["deadline_at"])
        countdown = int((promised - now).total_seconds())
        overdue = r["status"] == PENDING and deadline < now
        out.append({
            "id": r["id"],
            "customer_name": r["customer_name"],
            "customer_phone": r["customer_phone"],
            "summary": r["summary"],
            "origin_session": r["origin_session"],
            "owner_id": r["owner_id"],
            "owner_name": r["owner_name"],
            "status": r["status"],
            "promised_at": r["promised_at"],
            "deadline_at": r["deadline_at"],
            "countdown_seconds": countdown,
            "is_overdue": overdue,
            "owner_version": r["owner_version"],
            "result": r["result"],
            "next_step": r["next_step"],
            "reschedule_basis": r["reschedule_basis"],
            "dispute_note": r["dispute_note"],
        })
    return out


def customer_windows(conn: sqlite3.Connection, customer_id: Optional[int] = None) -> list[dict]:
    sql = (
        "SELECT w.*, c.name AS customer_name, c.phone AS customer_phone, c.tz_offset "
        "FROM availability_windows w JOIN customers c ON c.id=w.customer_id"
    )
    params: list = []
    if customer_id is not None:
        sql += " WHERE w.customer_id=?"
        params.append(customer_id)
    sql += " ORDER BY w.customer_id, w.weekday, w.kind DESC, w.start_minute"
    return [
        {
            "customer_id": r["customer_id"],
            "customer_name": r["customer_name"],
            "customer_phone": r["customer_phone"],
            "tz_offset_min": r["tz_offset"],
            "kind": r["kind"],
            "weekday": r["weekday"],
            "weekday_cn": WEEKDAY_CN[r["weekday"]],
            "start_minute": r["start_minute"],
            "end_minute": r["end_minute"],
            "label": _fmt_window(r),
            "note": r["note"],
        }
        for r in conn.execute(sql, params).fetchall()
    ]


def call_records(conn: sqlite3.Connection, *, promise_id: Optional[int] = None) -> list[dict]:
    sql = (
        "SELECT cl.*, a.name AS agent_name, c.name AS customer_name "
        "FROM calls cl "
        "JOIN agents a ON a.id=cl.agent_id "
        "JOIN promises p ON p.id=cl.promise_id "
        "JOIN customers c ON c.id=p.customer_id"
    )
    params: list = []
    if promise_id is not None:
        sql += " WHERE cl.promise_id=?"
        params.append(promise_id)
    sql += " ORDER BY cl.created_at DESC, cl.id DESC"
    return [
        {
            "id": r["id"],
            "promise_id": r["promise_id"],
            "customer_name": r["customer_name"],
            "agent_name": r["agent_name"],
            "phone": r["phone"],
            "outcome": r["outcome"],
            "duration_s": r["duration_s"],
            "recording": r["recording"],
            "created_at": r["created_at"],
        }
        for r in conn.execute(sql, params).fetchall()
    ]


BREACH_REASON = {
    EXPIRED: "超过承诺时限未回拨",
}


def reassignment_and_breach_report(
    conn: sqlite3.Connection, *, now: Optional[db.datetime] = None
) -> dict:
    """转派与失约报表：失约（逾期）突出原因与责任交接链。"""
    now = now or db.parse_iso(db.now_iso())

    # 失约：所有逾期或由逾期关闭争议的承诺，按失约严重度排序
    breached_rows = conn.execute(
        """SELECT p.*, c.name AS customer_name, a.name AS owner_name,
                  s.name AS closed_by_name
           FROM promises p
           JOIN customers c ON c.id=p.customer_id
           JOIN agents a ON a.id=p.owner_id
           LEFT JOIN agents s ON s.id=p.closed_by_id
           WHERE p.status IN ('expired','dispute_closed')
           ORDER BY p.deadline_at ASC"""
    ).fetchall()
    breached: list[dict] = []
    for r in breached_rows:
        handovers = conn.execute(
            """SELECT o.*, f.name AS from_name, t.name AS to_name
               FROM ownership_log o
               LEFT JOIN agents f ON f.id=o.from_agent_id
               JOIN agents t ON t.id=o.to_agent_id
               WHERE o.promise_id=? ORDER BY o.id""",
            (r["id"],),
        ).fetchall()
        lateness = int((now - db.parse_iso(r["deadline_at"])).total_seconds())
        breached.append({
            "promise_id": r["id"],
            "customer_name": r["customer_name"],
            "summary": r["summary"],
            "status": r["status"],
            "deadline_at": r["deadline_at"],
            "lateness_seconds": max(lateness, 0),
            "reason": BREACH_REASON.get(EXPIRED, "失约"),
            "current_owner": r["owner_name"],
            "dispute_note": r["dispute_note"],
            "closed_by": r["closed_by_name"],
            "handovers": [
                {
                    "from": h["from_name"] or "—",
                    "to": h["to_name"],
                    "reason": h["reason"],
                    "at": h["occurred_at"],
                }
                for h in handovers
            ],
        })

    # 转派流水
    moves = conn.execute(
        """SELECT o.*, f.name AS from_name, t.name AS to_name,
                  c.name AS customer_name, p.summary
           FROM ownership_log o
           LEFT JOIN agents f ON f.id=o.from_agent_id
           JOIN agents t ON t.id=o.to_agent_id
           JOIN promises p ON p.id=o.promise_id
           JOIN customers c ON c.id=p.customer_id
           ORDER BY o.occurred_at DESC, o.id DESC"""
    ).fetchall()
    reassignments = [
        {
            "promise_id": m["promise_id"],
            "customer_name": m["customer_name"],
            "summary": m["summary"],
            "from": m["from_name"] or "—",
            "to": m["to_name"],
            "reason": m["reason"],
            "at": m["occurred_at"],
        }
        for m in moves
    ]

    stats = {
        "total_promises": conn.execute("SELECT COUNT(*) AS n FROM promises").fetchone()["n"],
        "open": conn.execute(
            "SELECT COUNT(*) AS n FROM promises WHERE status=?", (PENDING,)
        ).fetchone()["n"],
        "closed": conn.execute(
            f"SELECT COUNT(*) AS n FROM promises WHERE status IN ({','.join('?' * len(CLOSED_STATUSES))})",
            tuple(CLOSED_STATUSES),
        ).fetchone()["n"],
        "overdue_open": conn.execute(
            "SELECT COUNT(*) AS n FROM promises WHERE status='expired'"
        ).fetchone()["n"],
        "reassignments": len(reassignments),
    }
    return {"breached": breached, "reassignments": reassignments, "stats": stats}
