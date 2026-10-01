"""演示种子数据。

保证启动即含：逾期未回拨、号码拨错争议、客户拒接、逾期后接通、
未接通再次预约、其他渠道解决、重复创建、坐席请假转交值班组等样例。
时间均以“当前时刻”为锚点相对生成，界面倒计时直接可用。
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

from . import config
from .adapters import MockPhone, MockSMS
from .db import connect, init_db, log_event
from .timeutil import fmt_dt
from . import services

# 状态速记
OPEN, FULFILLED = services.OPEN, services.FULFILLED
RESCHEDULED = services.RESCHEDULED
CLOSED_RESOLVED, CLOSED_DUPLICATE, CLOSED_REFUSED = (
    services.CLOSED_RESOLVED, services.CLOSED_DUPLICATE, services.CLOSED_REFUSED)
DISPUTE = services.DISPUTE


def seed(db_path: Path | None = None, *, reset: bool = True) -> None:
    path = Path(db_path or config.DB_PATH)
    config.ensure_dirs()
    if reset and path.exists():
        path.unlink()
    for suffix in ("-wal", "-shm"):
        p = Path(str(path) + suffix)
        if p.exists():
            p.unlink()
    conn = connect(str(path))
    init_db(conn)
    # 适配器负责建 call_log / sms_outbox 表
    phone, sms = MockPhone(conn), MockSMS(conn)

    anchor = datetime.now().replace(microsecond=0)

    def t(hours: float = 0, minutes: int = 0) -> str:
        return fmt_dt(anchor + timedelta(hours=hours, minutes=minutes))

    def insert_agent(name, role, status="active", created=None):
        cur = conn.execute(
            "INSERT INTO agents (name, role, status, created_at) VALUES (?,?,?,?)",
            (name, role, status, created or t(-48)))
        return int(cur.lastrowid)

    a_lin = insert_agent("林晓（坐席）", "agent")
    a_chen = insert_agent("陈默（坐席·请假）", "agent", "on_leave")
    a_duty = insert_agent("周岚（值班组）", "agent")
    a_sup = insert_agent("高远（主管）", "supervisor")

    def insert_customer(name, phone, qf=None, qt=None, note=""):
        cur = conn.execute(
            """INSERT INTO customers (name, phone, quiet_from, quiet_to, note, created_at)
               VALUES (?,?,?,?,?,?)""",
            (name, phone, qf, qt, note, t(-48)))
        cid = int(cur.lastrowid)
        log_event(conn, "customer_created", customer_id=cid,
                  detail=f"{name} {phone}" + (f"，禁扰 {qf}–{qt}" if qf else ""))
        return cid

    c_zhang = insert_customer("张伟", "138-0000-0001", "22:00", "08:00",
                              "上班族，白天可接")
    c_li = insert_customer("李娜", "138-0000-0002", "12:00", "14:00")
    c_wang = insert_customer("王芳", "138-0000-0003")
    c_zhao = insert_customer("赵强", "138-0000-0004", "20:00", "09:00")
    c_sun = insert_customer("孙敏", "138-0000-0005")
    c_zhou = insert_customer("周杰", "138-0000-0006")
    c_wu = insert_customer("吴迪", "138-0000-0007")
    c_zheng = insert_customer("郑爽", "138-0000-0008")
    c_feng = insert_customer("冯磊", "138-0000-0009")
    c_chen_c = insert_customer("许晴", "138-0000-0010")

    def insert_promise(cid, owner, summary, session, window, promised,
                       status=OPEN, overdue=0, creator=None, created=None,
                       closed=None, version=1):
        cur = conn.execute(
            """INSERT INTO promises
               (customer_id, summary, origin_session, reachable_window,
                promised_at, owner_agent_id, version, status, overdue,
                created_by_agent, created_at, closed_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (cid, summary, session, window, promised, owner, version,
             status, overdue, creator or owner, created or t(-4), closed))
        pid = int(cur.lastrowid)
        log_event(conn, "promise_created", promise_id=pid, customer_id=cid,
                  actor_id=creator or owner, at=created or t(-4),
                  detail=f"承诺 {promised} 回拨；摘要：{summary}")
        return pid

    def add_call(pid, agent, outcome, at, recording=None, note=""):
        cur = conn.execute(
            """INSERT INTO calls (promise_id, agent_id, started_at, outcome, recording, note)
               VALUES (?,?,?,?,?,?)""",
            (pid, agent, at, outcome, recording, note))
        call_id = int(cur.lastrowid)
        log_event(conn, "call_dialed", promise_id=pid, actor_id=agent, at=at,
                  detail=f"呼叫结果：{outcome}（通话 #{call_id}）{note}")
        return call_id

    def event(kind, pid=None, cid=None, actor=None, detail="", at=None):
        log_event(conn, kind, promise_id=pid, customer_id=cid,
                  actor_id=actor, detail=detail, at=at or t())

    # 1) 待处理、即将到期（林晓负责）
    p_upcoming = insert_promise(
        c_zhang, a_lin,
        "上月账单多出一笔 39 元增值业务，要求核对并退费",
        "会话 #IM-88231：客户 10:02 进线，情绪平稳",
        "工作日 08:30–11:30、14:30–17:30",
        t(minutes=25), created=t(minutes=-20))
    sms.send("138-0000-0001", f"【模拟短信】已为您预约 {t(minutes=25)} 的回拨。")

    # 2) 逾期未回拨（核心逾期样例）
    p_overdue = insert_promise(
        c_li, a_lin,
        "宽带报障后工单 3 天无进展，询问维修排期",
        "会话 #IM-88104：客户两次催促未果",
        "工作日 09:00–12:00、14:00–18:00",
        t(-2), overdue=1, created=t(-5))
    event("promise_overdue", p_overdue, c_li, detail=f"超过承诺时限 {t(-2)} 仍未闭环",
          at=t(-2))

    # 3) 号码拨错 -> 争议，等主管核实
    p_wrong = insert_promise(
        c_wang, a_lin,
        "账户实名信息变更咨询",
        "会话 #IM-88077：客户要求工作时间回拨",
        "今天 09:00–18:00",
        t(-1.5), status=DISPUTE, overdue=1, created=t(-6))
    add_call(p_wrong, a_lin, "wrong_number", t(-1.4),
             note="对方称没有办理过该业务，疑似号码登记错误")
    event("dispute_raised", p_wrong, c_wang, a_lin,
          "号码拨错，升级主管核实客户联系方式", t(-1.4))
    event("promise_overdue", p_wrong, c_wang,
          detail=f"超过承诺时限 {t(-1.5)} 仍未闭环", at=t(-1.5))

    # 4) 客户拒接（明确表示不需要回拨）
    p_refused = insert_promise(
        c_zhao, a_lin,
        "套餐升级优惠介绍",
        "会话 #IM-87650：营销回访",
        "客户未明确",
        t(-24), status=CLOSED_REFUSED, created=t(-28), closed=t(-23))
    add_call(p_refused, a_lin, "customer_refused", t(-23),
             note="客户表示不需要，要求不要再打")
    event("promise_closed", p_refused, c_zhao, a_lin,
          "客户拒接，无需继续回拨", t(-23))

    # 5) 逾期后才接通并完成（失约但已补救）
    p_late = insert_promise(
        c_sun, a_lin,
        "发票抬头修改后一直未收到重开发票",
        "会话 #IM-87990：客户留了邮箱",
        "全天可接，偏下午",
        t(-3), status=FULFILLED, overdue=1, created=t(-8), closed=t(-0.5))
    rec = phone._make_recording(p_late, t(-0.5))
    call_late = add_call(p_late, a_lin, "connected", t(-0.5), recording=rec,
                         note="客户确认邮箱，承诺今日重开")
    conn.execute(
        """INSERT INTO outcomes (promise_id, call_id, result, next_step, agent_id, recorded_at)
           VALUES (?,?,?,?,?,?)""",
        (p_late, call_late, "已核实发票流程，财务今日重开并发送至客户邮箱",
         "工单 #FIN-2231 跟进，24 小时内确认客户是否收到", a_lin, t(-0.5)))
    event("promise_overdue", p_late, c_sun,
          detail=f"超过承诺时限 {t(-3)}", at=t(-3))
    event("promise_fulfilled", p_late, c_sun, a_lin,
          "结果：财务今日重开发票；下一步：24 小时内确认收件（逾期后接通）",
          t(-0.5))

    # 6) 未接通 -> 有依据再次预约（旧承诺 rescheduled，新承诺待处理）
    p_old = insert_promise(
        c_zhou, a_lin,
        "携号转网生效时间确认",
        "会话 #IM-88033",
        "客户称今晚 20:00 后方便",
        t(-1), status=RESCHEDULED, overdue=1, created=t(-6), closed=t(-0.75))
    add_call(p_old, a_lin, "no_answer", t(-0.9), note="振铃 6 声未接")
    event("promise_overdue", p_old, c_zhou, detail=f"超过承诺时限 {t(-1)}",
          at=t(-1))
    p_new = insert_promise(
        c_zhou, a_lin,
        "携号转网生效时间确认",
        "会话 #IM-88033",
        "客户短信回复：明晚 20:00–21:00 方便",
        t(2), created=t(-0.75))
    conn.execute(
        """INSERT INTO reschedules (old_promise_id, new_promise_id, reason, created_by_agent, created_at)
           VALUES (?,?,?,?,?)""",
        (p_old, p_new, "客户短信要求改约", a_lin, t(-0.75)))
    event("promise_rescheduled", p_old, c_zhou, a_lin,
          f"未接通，依据“客户短信要求改约”改约为 #{p_new} @ {t(2)}", t(-0.75))
    event("promise_created", p_new, c_zhou, a_lin,
          f"由 #{p_old} 再次预约，承诺 {t(2)}", t(-0.75))
    sms.send("138-0000-0006", f"【模拟短信】您的回拨已改约至 {t(2)}。")

    # 7) 其他渠道已解决
    p_resolved = insert_promise(
        c_wu, a_lin,
        "营业厅排队太久，咨询线上销户入口",
        "会话 #IM-88055",
        "随时",
        t(-20), status=CLOSED_RESOLVED, created=t(-22), closed=t(-18))
    event("promise_closed", p_resolved, c_wu, a_lin,
          "问题已由其他渠道解决：客户在 App 在线客服完成销户，工单 #APP-9920 截图已存档",
          t(-18))

    # 8) 重复创建：有效承诺保留，重复单关闭
    p_kept = insert_promise(
        c_zheng, a_lin,
        "副卡流量限速申诉",
        "会话 #IM-88120（首次进线）",
        "今天 15:00–18:00",
        t(3), created=t(-1))
    p_dup = insert_promise(
        c_zheng, a_lin,
        "副卡流量限速申诉（同一问题再次登记）",
        "会话 #IM-88121（重复进线）",
        "今天 15:00–18:00",
        t(3), status=CLOSED_DUPLICATE, created=t(-0.5), closed=t(-0.5))
    event("promise_closed", p_dup, c_zheng, a_lin,
          f"重复创建，并入有效承诺 #{p_kept}", t(-0.5))

    # 9-10) 坐席陈默请假，未完成承诺转交值班组周岚（责任交接样例）
    p_hand1 = insert_promise(
        c_feng, a_duty,
        "企业宽带固定 IP 配置远程协助",
        "会话 #BIZ-44102",
        "工作日 10:00–17:00",
        t(1), created=t(-10), creator=a_chen, version=2)
    conn.execute(
        """INSERT INTO transfers (promise_id, from_agent_id, to_agent_id, reason,
                                  expected_version, won, created_at)
           VALUES (?,?,?,?,1,1,?)""",
        (p_hand1, a_chen, a_duty, "坐席请假，未完成承诺转交值班组", t(-6)))
    event("promise_transferred", p_hand1, c_feng, a_chen,
          "坐席请假，责任交接给值班组 周岚（值班组）", t(-6))

    p_hand2 = insert_promise(
        c_chen_c, a_duty,
        "国际漫游开通确认",
        "会话 #IM-88180",
        "今晚 19:00–21:00",
        t(26), status=OPEN, overdue=0, created=t(-3), creator=a_chen,
        version=2)
    conn.execute(
        """INSERT INTO transfers (promise_id, from_agent_id, to_agent_id, reason,
                                  expected_version, won, created_at)
           VALUES (?,?,?,?,1,1,?)""",
        (p_hand2, a_chen, a_duty, "坐席请假，未完成承诺转交值班组", t(-2)))
    event("promise_transferred", p_hand2, c_chen_c, a_chen,
          "坐席请假，责任交接给值班组 周岚（值班组）", t(-2))
    event("agent_on_leave", actor=a_chen,
          detail="陈默（坐席·请假）请假，2 条承诺转交值班组", at=t(-2))

    # 让逾期视图与队列状态保持一致
    services.mark_overdue(conn)
    conn.commit()
    conn.close()
    print(f"[seed] 演示数据已写入 {path}")
    print(f"[seed] 坐席 {a_lin}=林晓 {a_duty}=周澜(值班组) 主管 {a_sup}=高远；"
          f"逾期样例承诺 #{p_overdue}，拨错争议 #{p_wrong}")


if __name__ == "__main__":
    seed()
