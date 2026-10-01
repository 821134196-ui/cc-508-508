"""核心业务规则。

覆盖需求中的关键判定：
- 承诺时刻不得落入客户禁扰时段（按客户时区换算本地墙上时钟，支持跨午夜窗口）；
- 重复创建检测（同一原始会话 / 时间过近的未完成承诺）；
- 回拨完成必须填写结果与下一步；不接通仅可"有依据"再次预约；
- 号码拨错 / 客户拒接 / 其他渠道已解决 / 重复创建 分别走不同取消原因；
- 逾期由扫描器判定；逾期记录坐席不得删除，主管确认后关闭争议；
- 转派使用 owner_version 乐观锁，并发下只有一个有效负责人；
- 坐席请假，其未完成承诺自动转交值班组。
"""
from __future__ import annotations

import sqlite3
from datetime import timedelta
from typing import Optional

from . import db

# ---- 状态 / 原因常量 ----
PENDING = "pending"
COMPLETED = "completed"
RESCHEDULED = "rescheduled"
EXPIRED = "expired"
DISPUTE_CLOSED = "dispute_closed"
CANCEL_WRONG_NUMBER = "cancelled_wrong_number"
CANCEL_REFUSED = "cancelled_refused"
CANCEL_RESOLVED = "cancelled_resolved_elsewhere"
CANCEL_DUPLICATE = "cancelled_duplicate"

CLOSED_STATUSES = {
    COMPLETED, RESCHEDULED, EXPIRED, DISPUTE_CLOSED,
    CANCEL_WRONG_NUMBER, CANCEL_REFUSED, CANCEL_RESOLVED, CANCEL_DUPLICATE,
}

DEFAULT_DEADLINE_GRACE = timedelta(minutes=30)   # 承诺时限 = 承诺时刻 + 30 分钟
DEFAULT_DUP_WINDOW = timedelta(minutes=60)       # 同一客户 60 分钟内的新承诺视为重复


class ServiceError(Exception):
    """业务规则错误基类。"""


class DncViolation(ServiceError):
    """承诺时刻落在客户禁扰时段（或可接听时段之外）。"""


class DuplicatePromiseError(ServiceError):
    def __init__(self, existing_id: int, reason: str):
        super().__init__(f"与未完成承诺 #{existing_id} 重复（{reason}）")
        self.existing_id = existing_id


class OwnershipConflict(ServiceError):
    """并发转派竞争：负责人版本已变化，本次转派失败。"""


class PermissionDenied(ServiceError):
    """权限不足（如坐席删除逾期记录 / 越权转派）。」"""


class InvalidTransition(ServiceError):
    """承诺当前状态不允许该操作，或必填依据缺失。"""


# ---------------------------------------------------------------- 时段判定

def local_wall_time(customer: sqlite3.Row, moment_utc: db.datetime):
    """UTC 时刻 -> 客户本地 (weekday, minute_of_day)。"""
    local = moment_utc + timedelta(minutes=customer["tz_offset"])
    return local.weekday(), local.hour * 60 + local.minute


def _window_contains(start_min: int, end_min: int, minute: int) -> bool:
    """点是否落在窗口内。end<=start 表示跨午夜窗口。左闭右开。"""
    if end_min <= start_min:
        return minute >= start_min or minute < end_min
    return start_min <= minute < end_min


def check_callable(
    conn: sqlite3.Connection, customer_id: int, moment_utc: db.datetime
) -> Optional[str]:
    """返回 None 表示可拨打；否则返回不可拨打的原因（禁扰/不在可接听时段）。"""
    customer = db.get(conn, "SELECT * FROM customers WHERE id=?", (customer_id,))
    weekday, minute = local_wall_time(customer, moment_utc)
    prev_weekday = (weekday - 1) % 6 if weekday else 6
    # 跨午夜窗口定义在"起始日"：周日 23:00–01:00 在周一凌晨仍生效，
    # 因此同时取当天与前一天的窗口（跨午夜谓词会自动排除不相关的部分）。
    windows = conn.execute(
        "SELECT * FROM availability_windows WHERE customer_id=? AND weekday IN (?, ?)",
        (customer_id, weekday, prev_weekday),
    ).fetchall()

    dnc = [w for w in windows if w["kind"] == "dnc"]
    oks = [w for w in windows if w["kind"] == "ok"]

    for w in dnc:
        if _window_contains(w["start_minute"], w["end_minute"], minute):
            note = f"：{w['note']}" if w["note"] else ""
            return f"客户禁扰时段{note}"
    if oks:
        if not any(
            _window_contains(w["start_minute"], w["end_minute"], minute) for w in oks
        ):
            return "不在客户可接听时段内"
    return None


# ---------------------------------------------------------------- 承诺创建

class PromiseService:
    def __init__(
        self,
        conn: sqlite3.Connection,
        deadline_grace: timedelta = DEFAULT_DEADLINE_GRACE,
        dup_window: timedelta = DEFAULT_DUP_WINDOW,
    ) -> None:
        self.conn = conn
        self.deadline_grace = deadline_grace
        self.dup_window = dup_window

    # ---- 基础读取 ----
    def get_promise(self, promise_id: int) -> sqlite3.Row:
        row = db.get(self.conn, "SELECT * FROM promises WHERE id=?", (promise_id,))
        if row is None:
            raise ServiceError(f"承诺 #{promise_id} 不存在")
        return row

    def _require_agent(self, agent_id: int, *, allow_leave: bool = False) -> sqlite3.Row:
        row = db.get(self.conn, "SELECT * FROM agents WHERE id=? AND active=1", (agent_id,))
        if row is None:
            raise ServiceError(f"坐席 #{agent_id} 不存在或已停用")
        if row["on_leave"] and not allow_leave:
            raise InvalidTransition(f"坐席 {row['name']} 正在请假，不能承担承诺")
        return row

    def _add_event(self, c, promise_id: int, actor_id: Optional[int], etype: str, detail: str = ""):
        c.execute(
            "INSERT INTO events(promise_id, actor_id, type, detail, created_at) VALUES (?,?,?,?,?)",
            (promise_id, actor_id, etype, detail, db.now_iso()),
        )

    # ---- 创建 ----
    def create_promise(
        self,
        *,
        customer_id: int,
        created_by_id: int,
        owner_id: Optional[int],
        summary: str,
        origin_session: str,
        promised_at: db.datetime,
    ) -> int:
        if not summary.strip():
            raise InvalidTransition("问题摘要不能为空")
        if not origin_session.strip():
            raise InvalidTransition("必须记录原始会话")
        owner_id = owner_id if owner_id is not None else created_by_id
        self._require_agent(owner_id)
        creator = self._require_agent(created_by_id, allow_leave=True)
        _ = creator

        # 禁扰 / 可接听时段校验
        reason = check_callable(self.conn, customer_id, promised_at)
        if reason:
            raise DncViolation(f"承诺时刻 {db.to_iso(promised_at)} {reason}")

        deadline = promised_at + self.deadline_grace

        with db.transaction(self.conn) as c:
            # 重复创建检测：同一客户存在未关闭承诺，且同一会话或承诺时刻过近
            dup = c.execute(
                """SELECT * FROM promises
                   WHERE customer_id=? AND status=?
                     AND (origin_session=? OR ABS(strftime('%s', promised_at) - strftime('%s', ?)) < ?)""",
                (customer_id, PENDING, origin_session, db.to_iso(promised_at),
                 int(self.dup_window.total_seconds())),
            ).fetchone()
            if dup is not None:
                why = "同一原始会话" if dup["origin_session"] == origin_session else "承诺时刻过近"
                raise DuplicatePromiseError(dup["id"], why)

            cur = c.execute(
                """INSERT INTO promises(customer_id, summary, origin_session, promised_at,
                                        deadline_at, owner_id, created_by_id, status, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (customer_id, summary.strip(), origin_session.strip(),
                 db.to_iso(promised_at), db.to_iso(deadline),
                 owner_id, created_by_id, PENDING, db.now_iso()),
            )
            pid = cur.lastrowid
            self._add_event(c, pid, created_by_id, "created",
                            f"承诺 {db.to_iso(promised_at)} 前回拨；负责人 #{owner_id}")
        return pid

    # ---- 回拨完成 ----
    def complete(self, promise_id: int, agent_id: int, result: str, next_step: str) -> None:
        if not result.strip():
            raise InvalidTransition("回拨完成必须填写结果")
        if not next_step.strip():
            raise InvalidTransition("回拨完成必须填写下一步")
        with db.transaction(self.conn) as c:
            row = c.execute("SELECT * FROM promises WHERE id=?", (promise_id,)).fetchone()
            if row is None:
                raise ServiceError("承诺不存在")
            if row["status"] != PENDING:
                raise InvalidTransition(f"承诺 #{promise_id} 当前状态 {row['status']}，不能回填完成")
            if row["owner_id"] != agent_id:
                actor = db.get(c, "SELECT * FROM agents WHERE id=?", (agent_id,))
                if actor is None or actor["role"] != "supervisor":
                    raise PermissionDenied("只有负责坐席或主管可以回填结果")
            c.execute(
                """UPDATE promises SET status=?, result=?, next_step=?,
                                       completed_at=?, closed_at=? WHERE id=?""",
                (COMPLETED, result.strip(), next_step.strip(),
                 db.now_iso(), db.now_iso(), promise_id),
            )
            self._add_event(c, promise_id, agent_id, "completed",
                            f"结果：{result.strip()}；下一步：{next_step.strip()}")

    # ---- 不接通 -> 有依据再次预约 ----
    def reschedule(
        self,
        promise_id: int,
        agent_id: int,
        *,
        new_time: db.datetime,
        basis: str,
        basis_type: str = "no_answer",
    ) -> int:
        if basis_type not in ("no_answer", "customer_request", "line_fault"):
            raise InvalidTransition("未知改约依据类型")
        if not basis.strip():
            raise InvalidTransition("不接通再次预约必须给出依据（未接记录/客户留言/线路故障）")
        with db.transaction(self.conn) as c:
            row = c.execute("SELECT * FROM promises WHERE id=?", (promise_id,)).fetchone()
            if row is None:
                raise ServiceError("承诺不存在")
            if row["status"] != PENDING:
                raise InvalidTransition(f"承诺 #{promise_id} 已关闭，不能改约")
            if basis_type == "no_answer":
                ev = c.execute(
                    "SELECT 1 FROM calls WHERE promise_id=? AND outcome IN ('no_answer','refused') LIMIT 1",
                    (promise_id,),
                ).fetchone()
                if ev is None:
                    raise InvalidTransition("以'不接通'改约必须先有未接通拨打记录作为依据")

            reason = check_callable(c, row["customer_id"], new_time)
            if reason:
                raise DncViolation(f"新承诺时刻 {reason}")

            c.execute(
                "UPDATE promises SET status=?, reschedule_basis=?, closed_at=? WHERE id=?",
                (RESCHEDULED, f"[{basis_type}] {basis.strip()}", db.now_iso(), promise_id),
            )
            cur = c.execute(
                """INSERT INTO promises(customer_id, summary, origin_session, promised_at,
                                        deadline_at, owner_id, created_by_id, status,
                                        created_at, parent_id, supersedes_id)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (row["customer_id"], row["summary"], row["origin_session"],
                 db.to_iso(new_time), db.to_iso(new_time + self.deadline_grace),
                 agent_id, agent_id, PENDING, db.now_iso(),
                 promise_id, promise_id),
            )
            new_id = cur.lastrowid
            self._add_event(c, promise_id, agent_id, "rescheduled",
                            f"依据[{basis_type}]：{basis.strip()}；新承诺 #{new_id}")
            self._add_event(c, new_id, agent_id, "created",
                            f"由承诺 #{promise_id} 有依据改约；{db.to_iso(new_time)} 前回拨")
        return new_id

    # ---- 各类取消 ----
    def _cancel(self, c, promise_id: int, agent_id: int, status: str, event_type: str, detail: str):
        row = c.execute("SELECT * FROM promises WHERE id=?", (promise_id,)).fetchone()
        if row is None:
            raise ServiceError("承诺不存在")
        if row["status"] != PENDING:
            raise InvalidTransition(f"承诺 #{promise_id} 当前状态 {row['status']}，不能取消")
        c.execute(
            "UPDATE promises SET status=?, closed_at=? WHERE id=?",
            (status, db.now_iso(), promise_id),
        )
        self._add_event(c, promise_id, agent_id, event_type, detail)

    def cancel_wrong_number(self, promise_id: int, agent_id: int) -> None:
        with db.transaction(self.conn) as c:
            hit = c.execute(
                "SELECT 1 FROM calls WHERE promise_id=? AND outcome='wrong_number' LIMIT 1",
                (promise_id,),
            ).fetchone()
            if hit is None:
                raise InvalidTransition("标记'号码拨错'前须有一次 wrong_number 拨打记录")
            self._cancel(c, promise_id, agent_id, CANCEL_WRONG_NUMBER,
                         "cancel_wrong_number", "号码拨错，承诺取消")

    def cancel_refused(self, promise_id: int, agent_id: int) -> None:
        with db.transaction(self.conn) as c:
            hit = c.execute(
                "SELECT 1 FROM calls WHERE promise_id=? AND outcome='refused' LIMIT 1",
                (promise_id,),
            ).fetchone()
            if hit is None:
                raise InvalidTransition("标记'客户拒接'前须有一次 refused 拨打记录")
            self._cancel(c, promise_id, agent_id, CANCEL_REFUSED,
                         "cancel_refused", "客户明确拒接，承诺取消")

    def cancel_resolved_elsewhere(self, promise_id: int, agent_id: int, channel: str) -> None:
        if not channel.strip():
            raise InvalidTransition("须注明问题已由哪个其他渠道解决")
        with db.transaction(self.conn) as c:
            self._cancel(c, promise_id, agent_id, CANCEL_RESOLVED,
                         "cancel_resolved_elsewhere",
                         f"问题已通过其他渠道解决：{channel.strip()}")

    def cancel_duplicate(self, promise_id: int, agent_id: int, kept_promise_id: int) -> None:
        """把重复创建出来的那条关掉，保留 kept_promise_id。"""
        if promise_id == kept_promise_id:
            raise InvalidTransition("不能将承诺标记为自身的重复")
        with db.transaction(self.conn) as c:
            kept = c.execute("SELECT * FROM promises WHERE id=?", (kept_promise_id,)).fetchone()
            if kept is None or kept["customer_id"] != self.get_promise(promise_id)["customer_id"]:
                raise InvalidTransition("保留的承诺不存在或不属于同一客户")
            self._cancel(c, promise_id, agent_id, CANCEL_DUPLICATE,
                         "cancel_duplicate", f"重复创建，保留承诺 #{kept_promise_id}")

    # ---- 逾期扫描 ----
    def sweep_overdue(self, *, now: Optional[db.datetime] = None) -> list[int]:
        now = now or db.parse_iso(db.now_iso())
        expired: list[int] = []
        with db.transaction(self.conn) as c:
            rows = c.execute(
                "SELECT * FROM promises WHERE status=? AND deadline_at < ?",
                (PENDING, db.to_iso(now)),
            ).fetchall()
            for row in rows:
                c.execute(
                    "UPDATE promises SET status=? WHERE id=? AND status=?",
                    (EXPIRED, row["id"], PENDING),
                )
                self._add_event(c, row["id"], None, "expired",
                                f"超过承诺时限 {row['deadline_at']} 仍未完成")
                expired.append(row["id"])
        return expired

    def is_overdue(self, promise_id: int, *, now: Optional[db.datetime] = None) -> bool:
        now = now or db.parse_iso(db.now_iso())
        row = self.get_promise(promise_id)
        return row["status"] == PENDING and db.parse_iso(row["deadline_at"]) < now

    # ---- 主管：关闭争议 ----
    def close_dispute(self, promise_id: int, supervisor_id: int, note: str) -> None:
        if not note.strip():
            raise InvalidTransition("关闭争议必须填写主管确认说明")
        with db.transaction(self.conn) as c:
            actor = c.execute("SELECT * FROM agents WHERE id=?", (supervisor_id,)).fetchone()
            if actor is None or actor["role"] != "supervisor":
                raise PermissionDenied("只有主管可以关闭争议")
            row = c.execute("SELECT * FROM promises WHERE id=?", (promise_id,)).fetchone()
            if row is None:
                raise ServiceError("承诺不存在")
            if row["status"] != EXPIRED:
                raise InvalidTransition(
                    f"承诺 #{promise_id} 状态为 {row['status']}，只有逾期争议可关闭")
            c.execute(
                """UPDATE promises SET status=?, dispute_note=?, closed_by_id=?, closed_at=?
                   WHERE id=?""",
                (DISPUTE_CLOSED, note.strip(), supervisor_id, db.now_iso(), promise_id),
            )
            self._add_event(c, promise_id, supervisor_id, "dispute_closed",
                            f"主管确认关闭：{note.strip()}")

    # ---- 删除（严格限制） ----
    def delete_promise(self, promise_id: int, actor_id: int) -> None:
        """逾期记录对任何角色都不可物理删除；坐席连误建记录也不能删。"""
        with db.transaction(self.conn) as c:
            actor = c.execute("SELECT * FROM agents WHERE id=?", (actor_id,)).fetchone()
            row = c.execute("SELECT * FROM promises WHERE id=?", (promise_id,)).fetchone()
            if row is None:
                raise ServiceError("承诺不存在")
            if row["status"] in (EXPIRED, DISPUTE_CLOSED):
                raise PermissionDenied(
                    "逾期/争议记录不可删除：须由主管确认后关闭争议（close_dispute）")
            calls = c.execute("SELECT COUNT(*) AS n FROM calls WHERE promise_id=?",
                              (promise_id,)).fetchone()["n"]
            if calls > 0:
                raise PermissionDenied("该承诺已有拨打记录，按时间线审计要求不可删除")
            if actor is None or actor["role"] != "supervisor":
                raise PermissionDenied("坐席不能删除承诺；如系误建请联系主管")
            c.execute("DELETE FROM events WHERE promise_id=?", (promise_id,))
            c.execute("DELETE FROM promises WHERE id=?", (promise_id,))

    # ---- 转派（乐观锁，防并发双负责人） ----
    def reassign(
        self,
        promise_id: int,
        actor_id: int,
        to_agent_id: int,
        *,
        expected_version: int,
        reason: str = "manual",
    ) -> int:
        """转派成功返回新版本号；版本不匹配抛 OwnershipConflict。"""
        if reason not in ("manual", "leave_handover", "duty_takeover"):
            raise InvalidTransition("未知转派原因")
        with db.transaction(self.conn) as c:
            actor = c.execute("SELECT * FROM agents WHERE id=?", (actor_id,)).fetchone()
            row = c.execute("SELECT * FROM promises WHERE id=?", (promise_id,)).fetchone()
            if row is None:
                raise ServiceError("承诺不存在")
            if row["status"] != PENDING:
                raise InvalidTransition(f"承诺 #{promise_id} 已关闭，不能转派")
            if actor is None:
                raise PermissionDenied("操作人不存在")
            # 并发落败方持有的版本已过期：优先报冲突，提示刷新重试
            if expected_version != row["owner_version"]:
                raise OwnershipConflict(
                    f"承诺 #{promise_id} 负责人已被其他转派更新（期望版本 {expected_version}，"
                    f"当前版本 {row['owner_version']}），请刷新后重试；本次转派未生效"
                )
            if actor["role"] != "supervisor" and row["owner_id"] != actor_id:
                raise PermissionDenied("只有当前负责坐席或主管可以转派")
            target = c.execute(
                "SELECT * FROM agents WHERE id=? AND active=1", (to_agent_id,)
            ).fetchone()
            if target is None:
                raise ServiceError("目标负责人不存在或已停用")
            if target["on_leave"]:
                raise InvalidTransition(f"{target['name']} 正在请假，不能承接转派")

            cur = c.execute(
                "UPDATE promises SET owner_id=?, owner_version=owner_version+1 "
                "WHERE id=? AND owner_version=?",
                (to_agent_id, promise_id, expected_version),
            )
            if cur.rowcount == 0:
                raise OwnershipConflict(
                    f"承诺 #{promise_id} 负责人已被其他转派更新（期望版本 {expected_version}），"
                    "请刷新后重试；本次转派未生效"
                )
            c.execute(
                """INSERT INTO ownership_log(promise_id, from_agent_id, to_agent_id,
                                             reason, occurred_at)
                   VALUES (?,?,?,?,?)""",
                (promise_id, row["owner_id"], to_agent_id, reason, db.now_iso()),
            )
            self._add_event(c, promise_id, actor_id, "reassigned",
                           f"{row['owner_id']} -> {to_agent_id}（{reason}）")
            return expected_version + 1

    # ---- 请假交接：未完成承诺整体转交值班组 ----
    def handover_leave_to_duty(self, agent_id: int) -> list[int]:
        with db.transaction(self.conn) as c:
            agent = c.execute("SELECT * FROM agents WHERE id=?", (agent_id,)).fetchone()
            if agent is None:
                raise ServiceError("坐席不存在")
            duty = c.execute(
                "SELECT * FROM agents WHERE role='duty_group' AND active=1 AND on_leave=0 "
                "ORDER BY id LIMIT 1"
            ).fetchone()
            if duty is None:
                raise ServiceError("没有可承接的值班组")
            c.execute("UPDATE agents SET on_leave=1 WHERE id=?", (agent_id,))
            rows = c.execute(
                "SELECT * FROM promises WHERE owner_id=? AND status=?",
                (agent_id, PENDING),
            ).fetchall()
            moved: list[int] = []
            for row in rows:
                c.execute(
                    "UPDATE promises SET owner_id=?, owner_version=owner_version+1 WHERE id=?",
                    (duty["id"], row["id"]),
                )
                c.execute(
                    """INSERT INTO ownership_log(promise_id, from_agent_id, to_agent_id,
                                                 reason, occurred_at)
                       VALUES (?,?,?,?,?)""",
                    (row["id"], agent_id, duty["id"], "leave_handover", db.now_iso()),
                )
                self._add_event(c, row["id"], None, "reassigned",
                                f"坐席请假，自动交接值班组 {duty['name']}（leave_handover）")
                moved.append(row["id"])
            return moved
