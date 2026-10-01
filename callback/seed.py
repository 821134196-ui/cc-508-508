"""演示种子数据：单命令启动时自动注入，包含逾期样例。

场景设计（时间相对"现在"构造，保证每次启动都有逾期/即将到期样本）：
- 客户 王敏：禁扰工作日午休、工作日晚间可接听窗口；有一条已逾期承诺
  （负责坐席 林岚 即将请假 -> 交接值班组）；
- 客户 赵磊：一条 35 分钟后到期的正常承诺；
- 客户 陈晨：号码拨错已取消、客户拒接、其他渠道解决、重复创建 各一演示；
- 客户 周宁：多次未接后有依据改约。
"""
from __future__ import annotations

import sqlite3
from datetime import timedelta

from . import db
from .adapters import PhoneAdapter, REFUSED, WRONG_NUMBER, NO_ANSWER, CONNECTED
from .service import (
    PromiseService, PENDING, COMPLETED, CANCEL_WRONG_NUMBER, CANCEL_REFUSED,
    CANCEL_RESOLVED, CANCEL_DUPLICATE, EXPIRED, check_callable,
)

STEP = 900  # 以 15 分钟为步长在可接听窗口内寻找合适时刻


def find_past_callable(conn, customer_id: int, now, *, min_age_h: float = 3.5,
                       max_age_h: float = 7 * 24) -> "db.datetime":
    """向过去找一个落在可接听时段、且至少逾期 min_age_h 小时的时刻。"""
    from datetime import timedelta as _td
    minutes = int(max_age_h * 60 / 15)
    for i in range(2, minutes + 2):
        cand = now - _td(minutes=15 * i)
        if (now - cand).total_seconds() < min_age_h * 3600:
            continue
        if check_callable(conn, customer_id, cand) is None:
            return cand
    raise RuntimeError("找不到过去的可接听时刻")


def find_future_callable(conn, customer_id: int, now, *, min_ahead_min: int = 0,
                         max_ahead_h: float = 7 * 24) -> "db.datetime":
    """向未来找一个落在可接听时段的时刻。"""
    from datetime import timedelta as _td
    minutes = int(max_ahead_h * 60 / 15)
    for i in range(min_ahead_min // 15, minutes + 2):
        cand = now + _td(minutes=15 * i)
        if (cand - now).total_seconds() < min_ahead_min * 60:
            continue
        if check_callable(conn, customer_id, cand) is None:
            return cand
    raise RuntimeError("找不到未来的可接听时刻")


def _seed_agent(conn, name, role, on_leave=0):
    cur = conn.execute(
        "INSERT INTO agents(name, role, on_leave, active, created_at) VALUES (?,?,?,?,?)",
        (name, role, on_leave, 1, db.now_iso()),
    )
    return cur.lastrowid


def _seed_customer(conn, name, phone, tz_offset):
    cur = conn.execute(
        "INSERT INTO customers(name, phone, tz_offset, created_at) VALUES (?,?,?,?)",
        (name, phone, tz_offset, db.now_iso()),
    )
    return cur.lastrowid


def _window(conn, cid, kind, weekday, s, e, note=""):
    conn.execute(
        """INSERT INTO availability_windows(customer_id, kind, weekday, start_minute,
                                            end_minute, note)
           VALUES (?,?,?,?,?,?)""",
        (cid, kind, weekday, s, e, note),
    )


def seed(conn: sqlite3.Connection, recordings_dir: str, *, now=None) -> None:
    if db.get(conn, "SELECT COUNT(*) AS n FROM agents")["n"] > 0:
        return  # 已初始化过，幂等
    now = now or db.parse_iso(db.now_iso())
    svc = PromiseService(conn)
    phone = PhoneAdapter(conn, recordings_dir)

    # ---- 坐席 / 主管 / 值班组 ----
    a_linlan = _seed_agent(conn, "林岚", "agent")
    a_zhangwei = _seed_agent(conn, "张伟", "agent")
    a_sunqi = _seed_agent(conn, "孙琪", "agent")
    sup = _seed_agent(conn, "何主管", "supervisor")
    _seed_agent(conn, "夜间值班组", "duty_group")

    # ---- 客户 1：王敏（严格时段 + 逾期样例） ----
    wang = _seed_customer(conn, "王敏", "138-0000-0001", tz_offset=8 * 60)
    for wd in range(0, 5):  # 工作日
        _window(conn, wang, "dnc", wd, 12 * 60, 14 * 60, "午休勿扰")
        _window(conn, wang, "ok", wd, 9 * 60, 12 * 60)
        _window(conn, wang, "ok", wd, 14 * 60, 21 * 60, "工作时间可接")
    _window(conn, wang, "ok", 5, 10 * 60, 18 * 60, "周六可接")
    _window(conn, wang, "dnc", 6, 0, 1440, "周日全天勿扰")

    # 逾期样例：承诺时刻在 3.5 小时前的可接听窗口（时限 = +30 分钟，故已逾期）
    overdue_time = find_past_callable(conn, wang, now, min_age_h=3.5)
    pid_overdue = svc.create_promise(
        customer_id=wang, created_by_id=a_linlan, owner_id=a_linlan,
        summary="宽带工单 508 进度咨询，客户要求回告排期",
        origin_session="chat-20261001-0001",
        promised_at=overdue_time,
    )
    # 补一条未接拨打记录，说明失约背景
    phone.dial(pid_overdue, a_linlan, "138-0000-0001", outcome=NO_ANSWER)

    # 一条未来的正常承诺（最近的可接听窗口内）
    future = find_future_callable(conn, wang, now, min_ahead_min=30)
    svc.create_promise(
        customer_id=wang, created_by_id=a_zhangwei, owner_id=a_zhangwei,
        summary="确认宽带改约上门时间",
        origin_session="chat-20261001-0002",
        promised_at=future,
    )

    # ---- 客户 2：赵磊（35 分钟后承诺，用于倒计时） ----
    zhao = _seed_customer(conn, "赵磊", "138-0000-0002", tz_offset=8 * 60)
    # 全天可接（0–24:00 的窗口），保证任意启动时刻都有一条 ~35 分钟的倒计时样例
    for wd in range(7):
        _window(conn, zhao, "ok", wd, 0, 1440)
    svc.create_promise(
        customer_id=zhao, created_by_id=a_sunqi, owner_id=a_sunqi,
        summary="账单 9 月扣费疑问，需核对优惠",
        origin_session="call-20261001-0101",
        promised_at=now + timedelta(minutes=35),
    )

    # ---- 客户 3：陈晨（各类取消原因演示） ----
    chen = _seed_customer(conn, "陈晨", "138-0000-0003", tz_offset=8 * 60)
    for wd in range(7):
        _window(conn, chen, "ok", wd, 0, 1440)

    p_wrong = svc.create_promise(
        customer_id=chen, created_by_id=a_zhangwei, owner_id=a_zhangwei,
        summary="号码疑似有误：核实套餐到期时间",
        origin_session="chat-20261001-0201",
        promised_at=now + timedelta(hours=1),
    )
    phone.dial(p_wrong, a_zhangwei, "138-0000-0003", outcome=WRONG_NUMBER)
    svc.cancel_wrong_number(p_wrong, a_zhangwei)

    p_refuse = svc.create_promise(
        customer_id=chen, created_by_id=a_sunqi, owner_id=a_sunqi,
        summary="增值业务退订回访",
        origin_session="chat-20261001-0202",
        promised_at=now + timedelta(hours=1, minutes=30),
    )
    phone.dial(p_refuse, a_sunqi, "138-0000-0003", outcome=REFUSED)
    svc.cancel_refused(p_refuse, a_sunqi)

    p_else = svc.create_promise(
        customer_id=chen, created_by_id=a_linlan, owner_id=a_linlan,
        summary="发票抬头修改",
        origin_session="chat-20261001-0203",
        promised_at=now + timedelta(days=1),
    )
    svc.cancel_resolved_elsewhere(p_else, a_linlan, "App 在线客服已修改并回复客户")

    # 重复创建：保留 p_keep，取消 p_dup。
    # 两条由不同坐席几乎同时录入，绕过服务层去重守卫直接落库以还原"重复创建"现场
    p_keep = svc.create_promise(
        customer_id=chen, created_by_id=a_zhangwei, owner_id=a_zhangwei,
        summary="携号转网进度回告",
        origin_session="chat-20261001-0204",
        promised_at=now + timedelta(hours=2),
    )
    dup_time = now + timedelta(hours=2, minutes=5)
    with db.transaction(conn) as c:
        cur = c.execute(
            """INSERT INTO promises(customer_id, summary, origin_session, promised_at,
                                    deadline_at, owner_id, created_by_id, status, created_at)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (chen, "携号转网进度回告（重复录入）", "chat-20261001-0205",
             db.to_iso(dup_time), db.to_iso(dup_time + timedelta(minutes=30)),
             a_sunqi, a_sunqi, "pending", db.now_iso()),
        )
        p_dup = cur.lastrowid
        c.execute(
            "INSERT INTO events(promise_id, actor_id, type, detail, created_at) VALUES (?,?,?,?,?)",
            (p_dup, a_sunqi, "created", "并发录入的重复承诺（演示去重关闭）", db.now_iso()),
        )
    svc.cancel_duplicate(p_dup, a_zhangwei, p_keep)

    # ---- 客户 4：周宁（多次未接 -> 有依据改约） ----
    zhou = _seed_customer(conn, "周宁", "138-0000-0004", tz_offset=8 * 60)
    for wd in range(7):
        _window(conn, zhou, "ok", wd, 9 * 60, 20 * 60)
    old_time = find_past_callable(conn, zhou, now, min_age_h=0.75)
    p_old = svc.create_promise(
        customer_id=zhou, created_by_id=a_sunqi, owner_id=a_sunqi,
        summary="路由器售后换货确认",
        origin_session="chat-20261001-0301",
        promised_at=old_time,
    )
    phone.dial(p_old, a_sunqi, "138-0000-0004", outcome=NO_ANSWER)
    phone.dial(p_old, a_sunqi, "138-0000-0004", outcome=NO_ANSWER)
    # 改约到明天的可接听窗口
    svc.reschedule(
        p_old, a_sunqi,
        new_time=find_future_callable(conn, zhou, now, min_ahead_min=20 * 60),
        basis="两次拨打未接通，客户短信回复明日可接",
        basis_type="no_answer",
    )

    # ---- 客户 5：吴桐（已完成样例，填写结果与下一步） ----
    wu = _seed_customer(conn, "吴桐", "138-0000-0005", tz_offset=8 * 60)
    for wd in range(7):
        _window(conn, wu, "ok", wd, 8 * 60, 21 * 60)
    p_done = svc.create_promise(
        customer_id=wu, created_by_id=a_zhangwei, owner_id=a_zhangwei,
        summary="国际漫游开通确认",
        origin_session="chat-20261001-0401",
        promised_at=find_past_callable(conn, wu, now, min_age_h=1),
    )
    phone.dial(p_done, a_zhangwei, "138-0000-0005", outcome=CONNECTED, duration_s=120)
    svc.complete(
        p_done, a_zhangwei,
        result="已电话指导客户在 App 开通国际漫游，客户确认生效",
        next_step="下周一回访使用情况，如无问题关单",
    )

    # ---- 坐席林岚请假：其名下若还有未完成承诺（逾期样例）转交值班组 ----
    # 注意：逾期状态为 expired 的记录不参与交接，为演示交接我们再给林岚挂一条未来承诺
    p_handover = svc.create_promise(
        customer_id=zhao, created_by_id=a_linlan, owner_id=a_linlan,
        summary="林岚名下待办：补充合约续签资料说明",
        origin_session="chat-20261001-0501",
        promised_at=now + timedelta(hours=6),
    )
    svc.handover_leave_to_duty(a_linlan)

    # 逾期扫描：把超过时限的承诺置为 expired（演示单命令即含逾期数据）
    expired_ids = svc.sweep_overdue(now=now)

    conn.commit()
    return {
        "agents": {"linlan": a_linlan, "zhangwei": a_zhangwei,
                   "sunqi": a_sunqi, "supervisor": sup},
        "expired_ids": expired_ids,
        "overdue_promise": pid_overdue,
        "handover_promise": p_handover,
    }
