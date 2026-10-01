"""业务规则层：承诺、回拨、改约、转派、争议与报表。

所有写操作都在事务内完成并追加 timeline_events；不存在“删除承诺”的
操作——逾期记录只能由主管在争议裁决中关闭。
"""
from __future__ import annotations

import sqlite3
from datetime import datetime
from typing import Optional

from .adapters import MockPhone, MockSMS
from .db import fetchall, fetchone, log_event, now, transaction
from .errors import (BusinessError, DuplicatePromiseError, PermissionDenied,
                     TransferConflict)
from .timeutil import fmt_dt, in_quiet_hours, parse_dt
from . import config

# 状态常量
OPEN = "open"
FULFILLED = "fulfilled"
RESCHEDULED = "rescheduled"
CLOSED_RESOLVED = "closed_resolved"        # 其他渠道已解决
CLOSED_DUPLICATE = "closed_duplicate"      # 重复创建
CLOSED_REFUSED = "closed_refused"          # 客户拒接
DISPUTE = "dispute"                        # 有争议，待主管
CLOSED_DISPUTE = "closed_dispute"          # 主管裁决关闭
ACTIVE_STATUSES = (OPEN, DISPUTE)
CLOSED_STATUSES = (FULFILLED, RESCHEDULED, CLOSED_RESOLVED,
                   CLOSED_DUPLICATE, CLOSED_REFUSED, CLOSED_DISPUTE)


# -- 可替换时钟（测试用） ----------------------------------------------------
def clock() -> datetime:
    return datetime.now().replace(microsecond=0)


def now_str() -> str:
    return fmt_dt(clock())


# -- 基础资料 ----------------------------------------------------------------
def create_agent(conn, name: str, role: str = "agent") -> int:
    cur = conn.execute(
        "INSERT INTO agents (name, role, created_at) VALUES (?,?,?)",
        (name, role, now()),
    )
    aid = int(cur.lastrowid)
    log_event(conn, "agent_created", actor_id=aid, customer_id=None,
              detail=f"{role}: {name}")
    return aid


def create_customer(conn, name: str, phone: str, *,
                    quiet_from: Optional[str] = None,
                    quiet_to: Optional[str] = None,
                    note: str = "") -> int:
    if bool(quiet_from) != bool(quiet_to):
        raise BusinessError("禁扰起止时间必须同时提供")
    cur = conn.execute(
        """INSERT INTO customers (name, phone, quiet_from, quiet_to, note, created_at)
           VALUES (?,?,?,?,?,?)""",
        (name, phone, quiet_from, quiet_to, note, now()),
    )
    cid = int(cur.lastrowid)
    q = f"，禁扰 {quiet_from}–{quiet_to}" if quiet_from else ""
    log_event(conn, "customer_created", customer_id=cid, detail=f"{name} {phone}{q}")
    return cid


def get_agent(conn, agent_id: int):
    return fetchone(conn, "SELECT * FROM agents WHERE id=?", (agent_id,))


def get_customer(conn, customer_id: int):
    return fetchone(conn, "SELECT * FROM customers WHERE id=?", (customer_id,))


def find_active_promise(conn, customer_id: int):
    return fetchone(
        conn,
        "SELECT * FROM promises WHERE customer_id=? AND status IN (?,?)",
        (customer_id, OPEN, DISPUTE),
    )


# -- 权限辅助 ----------------------------------------------------------------
def _require_supervisor(conn, agent_id: int):
    agent = get_agent(conn, agent_id)
    if not agent or agent["role"] != "supervisor":
        raise PermissionDenied("该操作需要主管权限")
    return agent


def _require_owner_or_supervisor(conn, promise, agent_id: int):
    agent = get_agent(conn, agent_id)
    if not agent:
        raise PermissionDenied("坐席不存在")
    if promise["owner_agent_id"] != agent_id and agent["role"] != "supervisor":
        raise PermissionDenied("只有负责人或主管可以操作该承诺")
    return agent


# -- 创建承诺 ----------------------------------------------------------------
def create_promise(conn, *, agent_id: int, customer_id: int, summary: str,
                   origin_session: str, reachable_window: str,
                   promised_at: str) -> int:
    customer = get_customer(conn, customer_id)
    if not customer:
        raise BusinessError("客户不存在")
    if not summary.strip():
        raise BusinessError("问题摘要不能为空")
    if not origin_session.strip():
        raise BusinessError("必须记录原始会话")
    if not reachable_window.strip():
        raise BusinessError("必须记录客户可接听时段")
    try:
        target = parse_dt(promised_at)
    except ValueError:
        raise BusinessError("承诺时间格式应为 YYYY-MM-DDTHH:MM")
    if target <= clock():
        raise BusinessError("承诺时间必须晚于当前时间")
    # 规则：承诺时间不能落在客户禁扰时段
    if in_quiet_hours(target, customer["quiet_from"], customer["quiet_to"]):
        raise BusinessError(
            f"承诺时刻 {promised_at} 落在客户禁扰时段 "
            f"{customer['quiet_from']}–{customer['quiet_to']}，禁止创建")

    with transaction(conn):
        existing = find_active_promise(conn, customer_id)
        if existing:
            raise DuplicatePromiseError(existing["id"])
        cur = conn.execute(
            """INSERT INTO promises
               (customer_id, summary, origin_session, reachable_window,
                promised_at, owner_agent_id, status, created_by_agent, created_at)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (customer_id, summary.strip(), origin_session.strip(),
             reachable_window.strip(), promised_at, agent_id, OPEN,
             agent_id, now_str()),
        )
        pid = int(cur.lastrowid)
        log_event(conn, "promise_created", promise_id=pid,
                  customer_id=customer_id, actor_id=agent_id,
                  detail=f"承诺 {promised_at} 回拨；摘要：{summary}")
        MockSMS(conn).send(
            customer["phone"],
            f"【模拟短信】已为您预约 {promised_at} 的回拨，请留意来电。")
    return pid


# -- 拨号（模拟） ------------------------------------------------------------
def dial_promise(conn, *, promise_id: int, agent_id: int,
                 result: str = "connected") -> dict:
    """执行一次模拟回拨。不同结果分别处理。"""
    promise = _get_open_promise(conn, promise_id)
    _require_owner_or_supervisor(conn, promise, agent_id)
    customer = get_customer(conn, promise["customer_id"])
    phone_adapter = MockPhone(conn)

    with transaction(conn):
        trace = phone_adapter.dial(customer["phone"], result, promise_id=promise_id)
        cur = conn.execute(
            """INSERT INTO calls
               (promise_id, agent_id, started_at, outcome, recording, note)
               VALUES (?,?,?,?,?,?)""",
            (promise_id, agent_id, trace["at"], result, trace["recording"],
             trace["detail"]),
        )
        call_id = int(cur.lastrowid)
        log_event(conn, "call_dialed", promise_id=promise_id,
                  customer_id=promise["customer_id"], actor_id=agent_id,
                  detail=f"呼叫结果：{result}（通话 #{call_id}）{trace['detail']}")

        if result == "wrong_number":
            # 号码拨错：不得当作完成，也不得丢弃 -> 升级争议，待主管核实号码
            conn.execute(
                "UPDATE promises SET status=? WHERE id=?", (DISPUTE, promise_id))
            log_event(conn, "dispute_raised", promise_id=promise_id,
                      customer_id=promise["customer_id"], actor_id=agent_id,
                      detail="号码拨错，升级主管核实客户联系方式")
        elif result == "customer_refused":
            # 客户拒接（明确拒绝回拨）：闭环但区别于“已解决”
            conn.execute(
                """UPDATE promises SET status=?, closed_at=? WHERE id=?""",
                (CLOSED_REFUSED, now_str(), promise_id))
            log_event(conn, "promise_closed", promise_id=promise_id,
                      customer_id=promise["customer_id"], actor_id=agent_id,
                      detail="客户拒接，无需继续回拨")
    return {"call_id": call_id, "result": result,
            "recording": trace["recording"]}


def record_outcome(conn, *, promise_id: int, agent_id: int,
                   result_text: str, next_step: str) -> int:
    """回拨完成：填写结果与下一步，承诺闭环为 fulfilled。"""
    if not result_text.strip() or not next_step.strip():
        raise BusinessError("回拨结果与下一步都必须填写")
    promise = _get_open_promise(conn, promise_id)
    _require_owner_or_supervisor(conn, promise, agent_id)

    last_call = fetchone(
        conn,
        "SELECT * FROM calls WHERE promise_id=? ORDER BY id DESC LIMIT 1",
        (promise_id,))
    if not last_call or last_call["outcome"] != "connected":
        raise BusinessError("只有在接通客户后才能登记回拨结果")
    if fetchone(conn, "SELECT 1 FROM outcomes WHERE promise_id=?", (promise_id,)):
        raise BusinessError("该承诺已登记结果")

    with transaction(conn):
        cur = conn.execute(
            """INSERT INTO outcomes
               (promise_id, call_id, result, next_step, agent_id, recorded_at)
               VALUES (?,?,?,?,?,?)""",
            (promise_id, last_call["id"], result_text.strip(),
             next_step.strip(), agent_id, now_str()))
        oid = int(cur.lastrowid)
        conn.execute(
            "UPDATE promises SET status=?, closed_at=? WHERE id=?",
            (FULFILLED, now_str(), promise_id))
        late = "（逾期后接通）" if promise["overdue"] else ""
        log_event(conn, "promise_fulfilled", promise_id=promise_id,
                  customer_id=promise["customer_id"], actor_id=agent_id,
                  detail=f"结果：{result_text}；下一步：{next_step}{late}")
    return oid


# -- 不接通 -> 有依据的再次预约 ---------------------------------------------
RESCHEDULE_REASONS = ("客户短信要求改约", "客户可接听时段调整", "值班组安排")


def reschedule(conn, *, promise_id: int, agent_id: int, reason: str,
               new_promised_at: str,
               reachable_window: Optional[str] = None) -> int:
    promise = _get_open_promise(conn, promise_id)
    _require_owner_or_supervisor(conn, promise, agent_id)
    if not reason.strip():
        raise BusinessError("再次预约必须写明依据")
    last_call = fetchone(
        conn, "SELECT * FROM calls WHERE promise_id=? ORDER BY id DESC LIMIT 1",
        (promise_id,))
    # 只有“不接通”才允许走再次预约，且必须有一次未接通呼叫作为依据
    if not last_call or last_call["outcome"] != "no_answer":
        raise BusinessError("仅在客户未接通时，才能依据未接记录再次预约")
    customer = get_customer(conn, promise["customer_id"])
    try:
        target = parse_dt(new_promised_at)
    except ValueError:
        raise BusinessError("新承诺时间格式应为 YYYY-MM-DDTHH:MM")
    if in_quiet_hours(target, customer["quiet_from"], customer["quiet_to"]):
        raise BusinessError("新承诺时间仍落在客户禁扰时段")
    lead = (target - clock()).total_seconds()
    if lead < config.RESCHEDULE_LEAD_SECONDS:
        raise BusinessError(
            f"再次预约至少要留出 {config.RESCHEDULE_LEAD_SECONDS} 秒准备时间")

    window = (reachable_window or promise["reachable_window"]).strip()
    with transaction(conn):
        existing = find_active_promise(conn, promise["customer_id"])
        if existing and existing["id"] != promise_id:
            raise DuplicatePromiseError(existing["id"])
        # 先释放活动承诺的唯一占位，再插入承接的新承诺
        conn.execute(
            "UPDATE promises SET status=?, closed_at=? WHERE id=?",
            (RESCHEDULED, now_str(), promise_id))
        cur = conn.execute(
            """INSERT INTO promises
               (customer_id, summary, origin_session, reachable_window,
                promised_at, owner_agent_id, status, created_by_agent, created_at)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (promise["customer_id"], promise["summary"],
             promise["origin_session"], window, new_promised_at,
             promise["owner_agent_id"], OPEN, agent_id, now_str()))
        new_id = int(cur.lastrowid)
        conn.execute(
            """INSERT INTO reschedules
               (old_promise_id, new_promise_id, reason, created_by_agent, created_at)
               VALUES (?,?,?,?,?)""",
            (promise_id, new_id, reason.strip(), agent_id, now_str()))
        log_event(conn, "promise_rescheduled", promise_id=promise_id,
                  customer_id=promise["customer_id"], actor_id=agent_id,
                  detail=f"未接通，依据“{reason}”改约为 #{new_id} @ {new_promised_at}")
        log_event(conn, "promise_created", promise_id=new_id,
                  customer_id=promise["customer_id"], actor_id=agent_id,
                  detail=f"由 #{promise_id} 再次预约，承诺 {new_promised_at}")
        MockSMS(conn).send(
            customer["phone"],
            f"【模拟短信】您的回拨已改约至 {new_promised_at}，请留意来电。")
    return new_id


# -- 其他渠道已解决 / 重复创建 ----------------------------------------------
def close_resolved_elsewhere(conn, *, promise_id: int, agent_id: int,
                             evidence: str) -> None:
    if not evidence.strip():
        raise BusinessError("必须说明问题已在哪个渠道解决（凭据）")
    promise = _get_open_promise(conn, promise_id)
    _require_owner_or_supervisor(conn, promise, agent_id)
    with transaction(conn):
        conn.execute(
            "UPDATE promises SET status=?, closed_at=? WHERE id=?",
            (CLOSED_RESOLVED, now_str(), promise_id))
        log_event(conn, "promise_closed", promise_id=promise_id,
                  customer_id=promise["customer_id"], actor_id=agent_id,
                  detail=f"问题已由其他渠道解决：{evidence}")


def close_as_duplicate(conn, *, promise_id: int, agent_id: int,
                       kept_promise_id: int) -> None:
    promise = _get_open_promise(conn, promise_id)
    _require_owner_or_supervisor(conn, promise, agent_id)
    if kept_promise_id == promise_id:
        raise BusinessError("不能把承诺标记为自身的重复")
    kept = fetchone(conn, "SELECT * FROM promises WHERE id=?", (kept_promise_id,))
    if not kept:
        raise BusinessError("被指向的有效承诺不存在")
    if kept["customer_id"] != promise["customer_id"]:
        raise BusinessError("重复承诺必须属于同一客户")
    with transaction(conn):
        conn.execute(
            "UPDATE promises SET status=?, closed_at=? WHERE id=?",
            (CLOSED_DUPLICATE, now_str(), promise_id))
        log_event(conn, "promise_closed", promise_id=promise_id,
                  customer_id=promise["customer_id"], actor_id=agent_id,
                  detail=f"重复创建，并入有效承诺 #{kept_promise_id}")


# -- 逾期扫描 ----------------------------------------------------------------
def is_overdue(promise, at: Optional[datetime] = None) -> bool:
    at = at or clock()
    if promise["status"] not in ACTIVE_STATUSES:
        return False
    return parse_dt(promise["promised_at"]) < at


def mark_overdue(conn) -> list[int]:
    """把已过承诺时刻仍未闭环的承诺标记逾期（仅状态翻转时记一次事件）。"""
    overdue_ids: list[int] = []
    rows = fetchall(
        conn,
        "SELECT * FROM promises WHERE status IN (?,?) AND overdue=0",
        (OPEN, DISPUTE))
    for row in rows:
        if parse_dt(row["promised_at"]) < clock():
            with transaction(conn):
                conn.execute(
                    "UPDATE promises SET overdue=1 WHERE id=? AND overdue=0",
                    (row["id"],))
                log_event(conn, "promise_overdue", promise_id=row["id"],
                          customer_id=row["customer_id"],
                          detail=f"超过承诺时限 {row['promised_at']} 仍未闭环")
            overdue_ids.append(row["id"])
    return overdue_ids


# -- 转派（CAS，并发安全） ---------------------------------------------------
def transfer_promise(conn, *, promise_id: int, from_agent_id: int,
                     to_agent_id: int, expected_version: int,
                     reason: str) -> None:
    if not reason.strip():
        raise BusinessError("转派必须注明原因")
    target = get_agent(conn, to_agent_id)
    if not target:
        raise BusinessError("目标负责人不存在")
    if target["status"] != "active":
        raise BusinessError(f"坐席 {target['name']} 正在请假，不能承接承诺")
    promise = fetchone(conn, "SELECT * FROM promises WHERE id=?", (promise_id,))
    if not promise:
        raise BusinessError("承诺不存在")
    if promise["status"] not in ACTIVE_STATUSES:
        raise BusinessError("承诺已闭环，无需转派")
    is_current_owner = promise["owner_agent_id"] == from_agent_id
    was_owner = fetchone(
        conn,
        """SELECT 1 FROM transfers
           WHERE promise_id=? AND from_agent_id=? AND won=1 LIMIT 1""",
        (promise_id, from_agent_id))
    if not is_current_owner and not was_owner:
        # 从未持有过该承诺 -> 越权
        raise PermissionDenied("只有当前负责人可以转出")

    conflict: Optional[str] = None
    with transaction(conn):
        row = fetchone(conn, "SELECT owner_agent_id, version FROM promises WHERE id=?",
                       (promise_id,))
        won = (row["version"] == expected_version
               and row["owner_agent_id"] == from_agent_id)
        if not won:
            # 竞争失败也要留痕：说明发生过并发转派冲突（随事务提交保留）
            conn.execute(
                """INSERT INTO transfers
                   (promise_id, from_agent_id, to_agent_id, reason,
                    expected_version, won, created_at)
                   VALUES (?,?,?,?,?,0,?)""",
                (promise_id, from_agent_id, to_agent_id, reason,
                 expected_version, now_str()))
            log_event(conn, "transfer_conflict", promise_id=promise_id,
                      customer_id=promise["customer_id"], actor_id=from_agent_id,
                      detail=f"并发转派失败：版本已变化（期望 v{expected_version}，实际 v{row['version']}）")
            conflict = (
                f"承诺 #{promise_id} 已被他人先转派（当前 v{row['version']}）")
        else:
            conn.execute(
                "UPDATE promises SET owner_agent_id=?, version=version+1 WHERE id=?",
                (to_agent_id, promise_id))
            conn.execute(
                """INSERT INTO transfers
                   (promise_id, from_agent_id, to_agent_id, reason,
                    expected_version, won, created_at)
                   VALUES (?,?,?,?,?,1,?)""",
                (promise_id, from_agent_id, to_agent_id, reason,
                 expected_version, now_str()))
            log_event(conn, "promise_transferred", promise_id=promise_id,
                      customer_id=promise["customer_id"], actor_id=from_agent_id,
                      detail=f"转派 {from_agent_id} -> {to_agent_id}：{reason}")
    if conflict:
        raise TransferConflict(conflict)


def agent_go_on_leave(conn, *, agent_id: int, duty_agent_id: int) -> int:
    """坐席请假：其名下未完成承诺一次性转交值班组。返回转交条数。"""
    agent = get_agent(conn, agent_id)
    duty = get_agent(conn, duty_agent_id)
    if not agent or not duty:
        raise BusinessError("坐席或值班组不存在")
    if duty["status"] != "active":
        raise BusinessError("值班组负责人必须在岗")
    moved = 0
    with transaction(conn):
        conn.execute("UPDATE agents SET status='on_leave' WHERE id=?", (agent_id,))
        rows = fetchall(
            conn,
            "SELECT * FROM promises WHERE owner_agent_id=? AND status IN (?,?)",
            (agent_id, OPEN, DISPUTE))
        for row in rows:
            conn.execute(
                "UPDATE promises SET owner_agent_id=?, version=version+1 WHERE id=?",
                (duty_agent_id, row["id"]))
            conn.execute(
                """INSERT INTO transfers
                   (promise_id, from_agent_id, to_agent_id, reason,
                    expected_version, won, created_at)
                   VALUES (?,?,?,?,?,1,?)""",
                (row["id"], agent_id, duty_agent_id,
                 "坐席请假，未完成承诺转交值班组", row["version"], now_str()))
            log_event(conn, "promise_transferred", promise_id=row["id"],
                      customer_id=row["customer_id"], actor_id=agent_id,
                      detail=f"坐席请假，责任交接给值班组 {duty['name']}")
            moved += 1
        log_event(conn, "agent_on_leave", actor_id=agent_id,
                  detail=f"{agent['name']} 请假，{moved} 条承诺转交值班组")
    return moved


# -- 主管裁决 ----------------------------------------------------------------
def supervisor_close_dispute(conn, *, promise_id: int, supervisor_id: int,
                             note: str) -> None:
    _require_supervisor(conn, supervisor_id)
    if not note.strip():
        raise BusinessError("主管裁决必须写明结论")
    promise = fetchone(conn, "SELECT * FROM promises WHERE id=?", (promise_id,))
    if not promise or promise["status"] != DISPUTE:
        raise BusinessError("只有争议中的承诺可以裁决关闭")
    with transaction(conn):
        conn.execute(
            "UPDATE promises SET status=?, closed_at=? WHERE id=?",
            (CLOSED_DISPUTE, now_str(), promise_id))
        conn.execute(
            """INSERT INTO dispute_reviews
               (promise_id, supervisor_id, decision, note, created_at)
               VALUES (?,?, 'close', ?,?)""",
            (promise_id, supervisor_id, note.strip(), now_str()))
        log_event(conn, "dispute_closed", promise_id=promise_id,
                  customer_id=promise["customer_id"], actor_id=supervisor_id,
                  detail=f"主管确认关闭争议：{note}")


def supervisor_reopen(conn, *, promise_id: int, supervisor_id: int,
                      note: str, new_promised_at: str) -> None:
    """主管核实后（如纠正了拨错的号码）重开承诺并约定新时刻。"""
    _require_supervisor(conn, supervisor_id)
    customer = get_customer(conn,
                            fetchone(conn, "SELECT customer_id FROM promises WHERE id=?",
                                     (promise_id,))["customer_id"])
    target = parse_dt(new_promised_at)
    if in_quiet_hours(target, customer["quiet_from"], customer["quiet_to"]):
        raise BusinessError("新承诺时间落在客户禁扰时段")
    with transaction(conn):
        conn.execute(
            """UPDATE promises SET status=?, overdue=0, promised_at=? WHERE id=?""",
            (OPEN, new_promised_at, promise_id))
        conn.execute(
            """INSERT INTO dispute_reviews
               (promise_id, supervisor_id, decision, note, created_at)
               VALUES (?,?, 'reopen', ?,?)""",
            (promise_id, supervisor_id, note.strip(), now_str()))
        log_event(conn, "dispute_reopened", promise_id=promise_id,
                  customer_id=customer["id"], actor_id=supervisor_id,
                  detail=f"主管重开并约定 {new_promised_at}：{note}")


def update_customer_phone(conn, *, customer_id: int, supervisor_id: int,
                          new_phone: str) -> None:
    """纠正号码属于主管操作（配合号码拨错争议）。"""
    _require_supervisor(conn, supervisor_id)
    if not new_phone.strip():
        raise BusinessError("新号码不能为空")
    with transaction(conn):
        conn.execute("UPDATE customers SET phone=? WHERE id=?",
                     (new_phone.strip(), customer_id))
        log_event(conn, "customer_phone_fixed", customer_id=customer_id,
                  actor_id=supervisor_id, detail=f"主管更正号码为 {new_phone}")


# -- 查询与报表 --------------------------------------------------------------
def _get_open_promise(conn, promise_id: int):
    promise = fetchone(conn, "SELECT * FROM promises WHERE id=?", (promise_id,))
    if not promise:
        raise BusinessError("承诺不存在")
    if promise["status"] not in ACTIVE_STATUSES:
        raise BusinessError(f"承诺 #{promise_id} 当前状态为 {promise['status']}，无法操作")
    return promise


def queue_promises(conn, *, agent_id: Optional[int] = None):
    """承诺队列：坐席按承诺时刻排序（倒计时由界面计算）。"""
    sql = """
        SELECT p.*, c.name AS customer_name, c.phone AS customer_phone,
               a.name AS owner_name
        FROM promises p
        JOIN customers c ON c.id = p.customer_id
        JOIN agents a ON a.id = p.owner_agent_id
        WHERE p.status IN (?,?)
    """
    params: list = [OPEN, DISPUTE]
    if agent_id is not None:
        sql += " AND p.owner_agent_id=?"
        params.append(agent_id)
    sql += " ORDER BY (p.promised_at < ?) DESC, p.promised_at ASC"
    params.append(now_str())
    return fetchall(conn, sql, tuple(params))


def customer_history(conn, customer_id: int):
    """客户历史：全部承诺 + 追加式时间线，与当前承诺同屏展示。"""
    promises = fetchall(
        conn,
        """SELECT p.*, a.name AS owner_name FROM promises p
           JOIN agents a ON a.id=p.owner_agent_id
           WHERE p.customer_id=? ORDER BY p.id DESC""",
        (customer_id,))
    events = fetchall(
        conn,
        """SELECT t.*, a.name AS actor_name FROM timeline_events t
           LEFT JOIN agents a ON a.id=t.actor_id
           WHERE t.customer_id=? ORDER BY t.id DESC""",
        (customer_id,))
    return promises, events


def calls_report(conn):
    """拨打记录。"""
    return fetchall(
        conn,
        """SELECT ca.*, p.id AS promise_id, c.name AS customer_name,
                  c.phone AS customer_phone, a.name AS agent_name
           FROM calls ca
           JOIN promises p ON p.id=ca.promise_id
           JOIN customers c ON c.id=p.customer_id
           JOIN agents a ON a.id=ca.agent_id
           ORDER BY ca.id DESC""")


def transfers_report(conn):
    """转派与责任交接记录（含竞争失败的尝试）。"""
    return fetchall(
        conn,
        """SELECT t.*, p.customer_id, c.name AS customer_name,
                  af.name AS from_name, at2.name AS to_name
           FROM transfers t
           JOIN promises p ON p.id=t.promise_id
           JOIN customers c ON c.id=p.customer_id
           JOIN agents af ON af.id=t.from_agent_id
           JOIN agents at2 ON at2.id=t.to_agent_id
           ORDER BY t.id DESC""")


def missed_report(conn):
    """失约报表：逾期/拨错/拒接等未按承诺闭环的情形与原因。"""
    rows = fetchall(
        conn,
        """SELECT p.*, c.name AS customer_name, c.phone AS customer_phone,
                  a.name AS owner_name,
                  (SELECT outcome FROM calls WHERE promise_id=p.id
                   ORDER BY id DESC LIMIT 1) AS last_outcome
           FROM promises p
           JOIN customers c ON c.id=p.customer_id
           JOIN agents a ON a.id=p.owner_agent_id
           WHERE p.overdue=1
              OR p.status IN (?,?)
           ORDER BY p.promised_at""",
        (DISPUTE, CLOSED_REFUSED))
    result = []
    for r in rows:
        reason, category = _miss_reason(r)
        result.append(dict(dict(r), miss_reason=reason, miss_category=category))
    return result


def _miss_reason(r) -> tuple[str, str]:
    status = r["status"]
    if status == DISPUTE and r["last_outcome"] == "wrong_number":
        return "号码拨错，待主管核实", "wrong_number"
    if status == CLOSED_REFUSED:
        return "客户拒接", "refused"
    if status == OPEN:
        return "超过承诺时限仍未回拨", "overdue_open"
    if status == DISPUTE:
        return "超过承诺时限且存在争议", "overdue_dispute"
    if status == FULFILLED:
        return "逾期后才接通并完成", "late_fulfilled"
    if status == RESCHEDULED:
        return "逾期未接通，已再次预约", "late_rescheduled"
    return "超过承诺时限", "overdue"


def customer_windows(conn):
    """客户时段报表：可接听时段与禁扰时段。"""
    return fetchall(
        conn,
        """SELECT c.id, c.name, c.phone, c.quiet_from, c.quiet_to,
                  c.note,
                  (SELECT p.reachable_window FROM promises p
                   WHERE p.customer_id=c.id AND p.status IN ('open','dispute')
                   ORDER BY p.id DESC LIMIT 1) AS active_window
           FROM customers c ORDER BY c.id""")


def timeline_for_promise(conn, promise_id: int):
    return fetchall(
        conn,
        """SELECT t.*, a.name AS actor_name FROM timeline_events t
           LEFT JOIN agents a ON a.id=t.actor_id
           WHERE t.promise_id=? ORDER BY t.id""",
        (promise_id,))
