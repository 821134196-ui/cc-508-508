"""基于标准库 http.server 的 Web 界面。

- 坐席视图：承诺队列按承诺时刻排序 + 实时倒计时；客户历史与当前承诺同屏。
- 主管视图：突出失约原因与责任交接，裁决争议。
配色：白底、墨灰文字与边线、少量琥珀色强调。
身份用 ?as=<agent_id> 切换（演示用，无鉴权），主管操作在服务端按角色校验。
"""
from __future__ import annotations

import html
import json
import urllib.parse
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from . import config, services
from .db import connect, fetchall, fetchone, init_db
from .errors import BusinessError, PermissionDenied, TransferConflict
from .timeutil import countdown_text, parse_dt

INK = "#26262a"
INK_SOFT = "#5b5b63"
LINE = "#e4e2dc"
AMBER = "#b45309"
AMBER_SOFT = "#fdf3e3"
BG = "#fbfaf7"
WHITE = "#ffffff"
DANGER = "#9b1c1c"
DANGER_SOFT = "#fdecec"
OK = "#166534"
OK_SOFT = "#eaf6ee"

STATUS_LABEL = {
    services.OPEN: "待回拨",
    services.DISPUTE: "争议待裁决",
    services.FULFILLED: "已完成",
    services.RESCHEDULED: "已改约",
    services.CLOSED_RESOLVED: "其他渠道已解决",
    services.CLOSED_DUPLICATE: "重复已关闭",
    services.CLOSED_REFUSED: "客户拒接",
    services.CLOSED_DISPUTE: "争议已关闭",
}
OUTCOME_LABEL = {
    "connected": "已接通",
    "wrong_number": "号码拨错",
    "customer_refused": "客户拒接",
    "no_answer": "未接通",
}


def esc(value: Any) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def layout(title: str, body: str, conn, as_id: int, active: str = "") -> str:
    agents = fetchall(conn, "SELECT * FROM agents ORDER BY role DESC, id")
    opts = "".join(
        f'<option value="{a["id"]}" {"selected" if a["id"] == as_id else ""}>'
        f'{esc(a["name"])} [{"主管" if a["role"] == "supervisor" else "坐席"}'
        f'{"/请假" if a["status"] == "on_leave" else ""}]</option>'
        for a in agents)
    nav = [
        ("/queue", "承诺队列", "queue"),
        ("/customers", "客户时段", "customers"),
        ("/calls", "拨打记录", "calls"),
        ("/transfers", "转派记录", "transfers"),
        ("/supervisor", "主管视图", "supervisor"),
    ]
    nav_html = "".join(
        f'<a class="nav {"on" if active == k else ""}" href="{u}?as={as_id}">{n}</a>'
        for u, n, k in nav)
    return f"""<!doctype html>
<html lang="zh-CN"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{esc(title)} · 回拨承诺</title>
<style>
:root{{--ink:{INK};--soft:{INK_SOFT};--line:{LINE};--amber:{AMBER};
--amber-soft:{AMBER_SOFT};--bg:{BG};}}
*{{box-sizing:border-box}}
body{{margin:0;background:var(--bg);color:var(--ink);
 font:14px/1.6 -apple-system,"Segoe UI","PingFang SC","Microsoft YaHei",sans-serif}}
header{{background:{WHITE};border-bottom:1px solid var(--line);padding:12px 24px;
 display:flex;align-items:center;gap:20px;flex-wrap:wrap}}
header h1{{font-size:16px;margin:0;letter-spacing:.5px}}
header h1 b{{color:var(--amber)}}
nav{{display:flex;gap:4px;flex-wrap:wrap}}
.nav{{padding:5px 12px;border-radius:6px;color:var(--soft);text-decoration:none}}
.nav:hover{{background:var(--amber-soft);color:var(--amber)}}
.nav.on{{background:var(--amber);color:#fff}}
.me{{margin-left:auto;display:flex;align-items:center;gap:8px;color:var(--soft)}}
.me select{{padding:4px 8px;border:1px solid var(--line);border-radius:6px;
 background:#fff;color:var(--ink)}}
main{{max-width:1180px;margin:0 auto;padding:20px 24px 60px}}
h2{{font-size:15px;margin:26px 0 10px;display:flex;align-items:center;gap:10px}}
h2 .hint{{font-weight:400;color:var(--soft);font-size:12px}}
.grid{{display:grid;grid-template-columns:1fr 380px;gap:18px;align-items:start}}
@media(max-width:900px){{.grid{{grid-template-columns:1fr}}}}
.card{{background:{WHITE};border:1px solid var(--line);border-radius:10px;
 padding:14px 16px;margin-bottom:14px}}
.card.promise{{border-left:3px solid var(--ink)}}
.card.promise.due{{border-left-color:var(--amber)}}
.card.promise.overdue{{border-left-color:{DANGER};background:{DANGER_SOFT}}}
.card.promise.dispute{{background:var(--amber-soft);border-left-color:var(--amber)}}
table{{width:100%;border-collapse:collapse;background:{WHITE};
 border:1px solid var(--line);border-radius:10px;overflow:hidden}}
th,td{{padding:9px 12px;text-align:left;border-bottom:1px solid var(--line);
 vertical-align:top;font-size:13px}}
th{{background:#f4f2ec;color:var(--soft);font-weight:600;font-size:12px;
 text-transform:none}}
tr:last-child td{{border-bottom:none}}
tr.overdue td{{background:{DANGER_SOFT}}}
tr.dispute td{{background:var(--amber-soft)}}
a{{color:var(--amber)}}
.badge{{display:inline-block;padding:1px 9px;border-radius:999px;font-size:12px;
 border:1px solid var(--line);color:var(--soft);background:#fff;white-space:nowrap}}
.badge.open{{color:var(--ink);border-color:var(--ink)}}
.badge.dispute{{color:var(--amber);border-color:var(--amber);background:var(--amber-soft)}}
.badge.overdue{{color:{DANGER};border-color:{DANGER};background:{DANGER_SOFT}}}
.badge.done{{color:{OK};border-color:{OK};background:{OK_SOFT}}}
.countdown{{font-variant-numeric:tabular-nums;font-weight:600;color:var(--ink)}}
.countdown.late{{color:{DANGER}}}
.countdown.soon{{color:var(--amber)}}
.muted{{color:var(--soft);font-size:12px}}
.meta{{display:grid;grid-template-columns:84px 1fr;gap:3px 10px;font-size:13px}}
.meta dt{{color:var(--soft)}}.meta dd{{margin:0}}
.timeline{{list-style:none;margin:0;padding:0}}
.timeline li{{position:relative;padding:0 0 12px 18px;border-left:2px solid var(--line);
 margin-left:5px;font-size:13px}}
.timeline li::before{{content:"";position:absolute;left:-5px;top:5px;width:8px;height:8px;
 border-radius:50%;background:var(--soft)}}
.timeline li b{{color:var(--amber)}}
.timeline .at{{color:var(--soft);font-size:12px;font-variant-numeric:tabular-nums}}
form.inline{{display:inline}}
input,textarea,select{{font:inherit;padding:6px 9px;border:1px solid var(--line);
 border-radius:6px;background:#fff;color:var(--ink);width:100%}}
label{{display:block;font-size:12px;color:var(--soft);margin:8px 0 3px}}
button,.btn{{display:inline-block;padding:6px 14px;border-radius:6px;border:1px solid var(--ink);
 background:var(--ink);color:#fff;cursor:pointer;font-size:13px;text-decoration:none}}
button.amber,.btn.amber{{border-color:var(--amber);background:var(--amber)}}
button.ghost,.btn.ghost{{background:#fff;color:var(--ink)}}
button.danger,.btn.danger{{border-color:{DANGER};background:{DANGER};color:#fff}}
.row{{display:flex;gap:8px;flex-wrap:wrap;margin-top:8px}}
.flash{{padding:10px 14px;border-radius:8px;margin-bottom:14px;font-size:13px}}
.flash.err{{background:{DANGER_SOFT};color:{DANGER};border:1px solid #f3c6c6}}
.flash.ok{{background:{OK_SOFT};color:{OK};border:1px solid #bfe3ca}}
details{{margin-top:8px}}summary{{cursor:pointer;color:var(--soft);font-size:13px}}
.kpis{{display:flex;gap:12px;flex-wrap:wrap;margin-bottom:8px}}
.kpi{{flex:1;min-width:140px;background:#fff;border:1px solid var(--line);
 border-radius:10px;padding:12px 14px}}
.kpi .n{{font-size:24px;font-weight:700}}
.kpi .n.amber{{color:var(--amber)}}.kpi .n.red{{color:{DANGER}}}
.kpi .l{{font-size:12px;color:var(--soft)}}
</style></head><body>
<header>
  <h1>回拨承诺台 · <b>Callback Promise</b></h1>
  <nav>{nav_html}</nav>
  <form class="me" method="get" action="/queue">
    <span>当前身份</span>
    <select name="as" onchange="this.form.submit()">{opts}</select>
    <noscript><button class="ghost" type="submit">切换</button></noscript>
  </form>
</header>
<main>{body}</main>
<script>
// 实时倒计时：页面把每条承诺的目标时刻放在 data-due 上
const NOW = new Date();
function tick(){{
  const now = new Date();
  document.querySelectorAll('[data-due]').forEach(el=>{{
    const due = new Date(el.dataset.due);
    let s = Math.round((due - now)/1000);
    let txt, cls='countdown';
    if (s < 0) {{ txt='超时 '+human(-s); cls+=' late'; }}
    else {{ txt='剩余 '+human(s); if (s <= 600) cls+=' soon'; }}
    el.className = cls; el.textContent = txt;
  }});
}}
function human(s){{
  if (s<60) return s+' 秒';
  const m=Math.floor(s/60), ss=s%60;
  if (m<60) return m+' 分 '+String(ss).padStart(2,'0')+' 秒';
  const h=Math.floor(m/60), mm=m%60;
  if (h<24) return h+' 小时 '+String(mm).padStart(2,'0')+' 分';
  const d=Math.floor(h/24); return d+' 天 '+(h%24)+' 小时';
}}
tick(); setInterval(tick,1000);
</script>
</body></html>"""


def flash(msg: str, ok: bool = False) -> str:
    return f'<div class="flash {"ok" if ok else "err"}">{esc(msg)}</div>'


def status_badge(status: str, overdue: int) -> str:
    if status == services.OPEN and overdue:
        return '<span class="badge overdue">已逾期</span>'
    cls = {services.OPEN: "open", services.DISPUTE: "dispute"}.get(status, "done")
    if status in (services.CLOSED_DISPUTE,):
        cls = "done"
    return f'<span class="badge {cls}">{esc(STATUS_LABEL.get(status, status))}</span>'


class App(BaseHTTPRequestHandler):
    server_version = "CallbackPromise/1.0"

    def log_message(self, fmt, *args):  # 静音，保持单命令输出干净
        pass

    # -- 框架辅助 --
    @property
    def conn(self):
        # handler 实例每请求新建、运行在各自线程，连接不跨线程共享
        if not hasattr(self, "_dbconn"):
            self._dbconn = connect()
        return self._dbconn

    def _params(self) -> dict[str, str]:
        q = urllib.parse.urlparse(self.path).query
        return {k: v[0] for k, v in urllib.parse.parse_qs(q).items()}

    def _identity(self, params: dict, form: dict | None = None) -> int:
        form = form or {}
        if "as" in form:
            return int(form["as"])
        if "as" in params:
            return int(params["as"])
        row = fetchone(self.conn, "SELECT id FROM agents ORDER BY id LIMIT 1")
        return int(row["id"])

    def _form(self) -> dict[str, str]:
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length).decode("utf-8") if length else ""
        return {k: v[0] for k, v in urllib.parse.parse_qs(
            raw, keep_blank_values=True).items()}

    def _send(self, html_text: str, code: int = 200) -> None:
        data = html_text.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _redirect(self, path: str) -> None:
        self.send_response(303)
        self.send_header("Location", path)
        self.end_headers()

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        if path == "/":
            return self._redirect("/queue")
        params = self._params()
        as_id = self._identity(params)
        try:
            if path == "/queue":
                return self.page_queue(as_id, params)
            if path == "/customers":
                return self.page_customers(as_id, params)
            if path == "/customer":
                return self.page_customer(as_id, params)
            if path == "/promise":
                return self.page_promise(as_id, params)
            if path == "/calls":
                return self.page_calls(as_id)
            if path == "/transfers":
                return self.page_transfers(as_id)
            if path == "/supervisor":
                return self.page_supervisor(as_id, params)
            self._send(flash("页面不存在"), 404)
        except BusinessError as e:
            self._send(layout("出错", flash(str(e)), self.conn, as_id), 400)

    def do_POST(self):
        form = self._form()
        as_id = self._identity({}, form)
        path = urllib.parse.urlparse(self.path).path
        try:
            redirect_to = self.handle_action(path, form, as_id)
            msg = urllib.parse.quote("操作成功")
            self._redirect(f"{redirect_to}?as={as_id}&ok={msg}")
        except (BusinessError, PermissionDenied, TransferConflict) as e:
            back = self._back_path(path, form, as_id)
            self._send(layout("操作被拒绝", flash(str(e)), self.conn, as_id), 409)
        except ValueError:
            self._send(layout("出错", flash("参数格式有误"), self.conn, as_id), 400)

    def _back_path(self, action: str, form: dict, as_id: int) -> str:
        pid = form.get("promise_id")
        return f"/promise?id={pid}" if pid else "/queue"

    # -- 动作处理 --
    def handle_action(self, path: str, f: dict, as_id: int) -> str:
        c = self.conn
        if path == "/act/dial":
            services.dial_promise(
                c, promise_id=int(f["promise_id"]), agent_id=as_id,
                result=f["result"])
            return f"/promise?id={f['promise_id']}"
        if path == "/act/outcome":
            services.record_outcome(
                c, promise_id=int(f["promise_id"]), agent_id=as_id,
                result_text=f["result"], next_step=f["next_step"])
            return f"/promise?id={f['promise_id']}"
        if path == "/act/reschedule":
            services.reschedule(
                c, promise_id=int(f["promise_id"]), agent_id=as_id,
                reason=f["reason"], new_promised_at=f["new_promised_at"],
                reachable_window=f.get("reachable_window") or None)
            return "/queue"
        if path == "/act/transfer":
            p = fetchone(c, "SELECT version FROM promises WHERE id=?",
                         (int(f["promise_id"]),))
            services.transfer_promise(
                c, promise_id=int(f["promise_id"]), from_agent_id=as_id,
                to_agent_id=int(f["to_agent_id"]),
                expected_version=int(f.get("version", p["version"])),
                reason=f["reason"])
            return f"/promise?id={f['promise_id']}"
        if path == "/act/resolved":
            services.close_resolved_elsewhere(
                c, promise_id=int(f["promise_id"]), agent_id=as_id,
                evidence=f["evidence"])
            return f"/promise?id={f['promise_id']}"
        if path == "/act/duplicate":
            services.close_as_duplicate(
                c, promise_id=int(f["promise_id"]), agent_id=as_id,
                kept_promise_id=int(f["kept_promise_id"]))
            return f"/promise?id={f['promise_id']}"
        if path == "/act/leave":
            services.agent_go_on_leave(
                c, agent_id=as_id, duty_agent_id=int(f["duty_agent_id"]))
            return "/transfers"
        if path == "/act/dispute-close":
            services.supervisor_close_dispute(
                c, promise_id=int(f["promise_id"]), supervisor_id=as_id,
                note=f["note"])
            return "/supervisor"
        if path == "/act/dispute-reopen":
            services.supervisor_reopen(
                c, promise_id=int(f["promise_id"]), supervisor_id=as_id,
                note=f["note"], new_promised_at=f["new_promised_at"])
            return "/supervisor"
        if path == "/act/fix-phone":
            services.update_customer_phone(
                c, customer_id=int(f["customer_id"]), supervisor_id=as_id,
                new_phone=f["new_phone"])
            return "/supervisor"
        raise BusinessError("未知操作")

    # -- 坐席：承诺队列 --
    def page_queue(self, as_id: int, params: dict):
        c = self.conn
        services.mark_overdue(c)
        me = services.get_agent(c, as_id)
        mine = me["role"] == "agent"
        rows = services.queue_promises(c, agent_id=as_id if mine else None)
        fl = flash(urllib.parse.unquote(params["ok"]), True) if params.get("ok") else ""

        cards = []
        n_overdue = sum(1 for r in rows if r["overdue"])
        n_due = sum(1 for r in rows if not r["overdue"])
        for r in rows:
            cls = "promise"
            if r["status"] == services.DISPUTE:
                cls += " dispute"
            elif r["overdue"]:
                cls += " overdue"
            else:
                cls += " due"
            window = esc(r["customer_phone"])
            quiet = ""
            cust = services.get_customer(c, r["customer_id"])
            if cust["quiet_from"]:
                quiet = f' · <span class="muted">禁扰 {esc(cust["quiet_from"])}–{esc(cust["quiet_to"])}</span>'
            cards.append(f"""
            <div class="card {cls}">
              <div style="display:flex;gap:10px;align-items:center;flex-wrap:wrap">
                {status_badge(r["status"], r["overdue"])}
                <span data-due="{esc(r["promised_at"])}:00" class="countdown"></span>
                <span class="muted">承诺 {esc(r["promised_at"].replace("T"," "))}</span>
                <span style="margin-left:auto" class="muted">负责人：{esc(r["owner_name"])}</span>
              </div>
              <h3 style="margin:9px 0 4px;font-size:15px">
                <a href="/customer?id={r['customer_id']}&as={as_id}">{esc(r["customer_name"])}</a>
                <span class="muted">{window}</span>{quiet}
              </h3>
              <div style="margin:2px 0 6px">{esc(r["summary"])}</div>
              <div class="muted">可接听：{esc(r["reachable_window"])}</div>
              <div class="row">
                <a class="btn amber" href="/promise?id={r['id']}&as={as_id}">处理 / 回拨</a>
              </div>
            </div>""")
        scope = "我的" if mine else "全部坐席的"
        body = f"""{fl}
        <div class="kpis">
          <div class="kpi"><div class="n red">{n_overdue}</div><div class="l">逾期未闭环</div></div>
          <div class="kpi"><div class="n amber">{n_due}</div><div class="l">{scope}待回拨</div></div>
        </div>
        <h2>承诺队列 <span class="hint">按承诺时刻排序，逾期置顶，倒计时实时刷新</span></h2>
        {''.join(cards) or '<div class="card muted">当前没有待处理承诺。</div>'}
        <h2>请假交接 <span class="hint">坐席请假时，名下未完成承诺一次性转交值班组</span></h2>
        {self._leave_form(as_id, me)}
        """
        self._send(layout("承诺队列", body, c, as_id, "queue"))

    def _leave_form(self, as_id: int, me) -> str:
        if me["role"] != "agent" or me["status"] == "on_leave":
            return '<div class="card muted">仅在岗坐席可发起请假交接。</div>'
        c = self.conn
        duties = fetchall(
            c, "SELECT * FROM agents WHERE status='active' AND id!=? ORDER BY id",
            (as_id,))
        opts = "".join(f'<option value="{d["id"]}">{esc(d["name"])}</option>'
                       for d in duties)
        return f"""<form class="card" method="post" action="/act/leave">
          <input type="hidden" name="as" value="{as_id}">
          <label>值班组接收人</label>
          <select name="duty_agent_id">{opts}</select>
          <div class="row"><button class="btn amber" type="submit">我要请假，转交全部未完成承诺</button></div>
        </form>"""

    # -- 客户历史 + 当前承诺同屏 --
    def page_customer(self, as_id: int, params: dict):
        c = self.conn
        cid = int(params["id"])
        customer = services.get_customer(c, cid)
        if not customer:
            raise BusinessError("客户不存在")
        promises, events = services.customer_history(c, cid)
        active = next((p for p in promises if p["status"]
                       in (services.OPEN, services.DISPUTE)), None)

        def promise_rows():
            out = []
            for p in promises:
                out.append(f"""<tr class="{'overdue' if p['overdue'] else ''}">
                  <td><a href="/promise?id={p['id']}&as={as_id}">#{p['id']}</a></td>
                  <td>{status_badge(p['status'], p['overdue'])}</td>
                  <td>{esc(p['promised_at'].replace('T',' '))}</td>
                  <td>{esc(p['summary'])}</td>
                  <td>{esc(p['owner_name'])}</td>
                </tr>""")
            return "".join(out)

        tl = "".join(
            f"""<li><b>{esc(EVENT_LABELS.get(e['kind'], e['kind']))}</b>
                <div>{esc(e['detail'])}</div>
                <div class="at">{esc(e['at'].replace('T',' '))}
                  {esc('· ' + e['actor_name'] if e['actor_name'] else '')}</div></li>"""
            for e in events)
        quiet = (f'{esc(customer["quiet_from"])}–{esc(customer["quiet_to"])}'
                 if customer["quiet_from"] else "无")
        active_html = ""
        if active:
            active_html = f"""
            <div class="card {'dispute' if active['status']==services.DISPUTE else 'promise due'}">
              <div class="row" style="margin-top:0">
                {status_badge(active['status'], active['overdue'])}
                <span data-due="{esc(active['promised_at'])}:00" class="countdown"></span>
              </div>
              <h3 style="margin:8px 0 4px">当前承诺 #{active['id']}</h3>
              <dl class="meta">
                <dt>承诺时刻</dt><dd>{esc(active['promised_at'].replace('T',' '))}</dd>
                <dt>负责人</dt><dd>{esc(active['owner_name'])}</dd>
                <dt>可接听</dt><dd>{esc(active['reachable_window'])}</dd>
                <dt>问题摘要</dt><dd>{esc(active['summary'])}</dd>
                <dt>原始会话</dt><dd>{esc(active['origin_session'])}</dd>
              </dl>
              <div class="row"><a class="btn amber" href="/promise?id={active['id']}&as={as_id}">进入处理</a></div>
            </div>"""
        else:
            active_html = '<div class="card muted">该客户当前没有进行中的承诺。</div>'

        body = f"""
        <h2>{esc(customer['name'])} <span class="hint">{esc(customer['phone'])} · 禁扰 {quiet}</span></h2>
        <div class="grid">
          <div>
            <h2>承诺历史</h2>
            <table><thead><tr><th>#</th><th>状态</th><th>承诺时刻</th><th>摘要</th><th>负责人</th></tr></thead>
            <tbody>{promise_rows()}</tbody></table>
            <h2>完整时间线</h2>
            <ul class="timeline">{tl or '<li class="muted">暂无事件</li>'}</ul>
          </div>
          <div>
            <h2>当前承诺 <span class="hint">与历史同屏</span></h2>
            {active_html}
          </div>
        </div>"""
        self._send(layout(customer["name"], body, c, as_id))

    # -- 承诺详情与操作 --
    def page_promise(self, as_id: int, params: dict):
        c = self.conn
        pid = int(params["id"])
        p = fetchone(
            c,
            """SELECT p.*, cu.name customer_name, cu.phone customer_phone,
                      cu.quiet_from, cu.quiet_to, a.name owner_name
               FROM promises p JOIN customers cu ON cu.id=p.customer_id
               JOIN agents a ON a.id=p.owner_agent_id WHERE p.id=?""", (pid,))
        if not p:
            raise BusinessError("承诺不存在")
        me = services.get_agent(c, as_id)
        can_act = (p["owner_agent_id"] == as_id or me["role"] == "supervisor")
        fl = flash(urllib.parse.unquote(params["ok"]), True) if params.get("ok") else ""

        calls = fetchall(
            c, "SELECT * FROM calls WHERE promise_id=? ORDER BY id", (pid,))
        calls_html = "".join(
            f"""<tr><td>{esc(ca['started_at'].replace('T',' '))}</td>
                <td>{esc(OUTCOME_LABEL.get(ca['outcome'], ca['outcome']))}</td>
                <td>{esc(ca['note'])}</td>
                <td>{('<span class="muted">'+esc(ca['recording'])+'</span>') if ca['recording'] else '—'}</td></tr>"""
            for ca in calls)
        outcome = fetchone(c, "SELECT * FROM outcomes WHERE promise_id=?", (pid,))
        events = services.timeline_for_promise(c, pid)
        tl = "".join(
            f"""<li><b>{esc(EVENT_LABELS.get(e['kind'], e['kind']))}</b>
                <div>{esc(e['detail'])}</div>
                <div class="at">{esc(e['at'].replace('T',' '))}
                  {esc('· '+e['actor_name'] if e['actor_name'] else '')}</div></li>"""
            for e in events)
        last_outcome = calls[-1]["outcome"] if calls else None
        active = p["status"] in (services.OPEN, services.DISPUTE)

        actions = ['<div class="card muted">该承诺已闭环，无可执行操作。</div>']
        if active and can_act:
            dial_buttons = "".join(
                f"""<button class="{cls}" type="submit" name="result" value="{val}">{label}</button>"""
                for val, label, cls in [
                    ("connected", "模拟拨打 · 已接通", "amber"),
                    ("no_answer", "模拟拨打 · 未接通", "ghost"),
                    ("wrong_number", "模拟拨打 · 号码拨错", "ghost"),
                    ("customer_refused", "模拟拨打 · 客户拒接", "danger")])
            blocks = [f"""
            <details {"open" if not calls else ""}>
              <summary>① 模拟回拨（电话适配器，仅本地记录）</summary>
              <form method="post" action="/act/dial" class="inline">
                <input type="hidden" name="as" value="{as_id}">
                <input type="hidden" name="promise_id" value="{pid}">
                <div class="row">{dial_buttons}</div>
              </form>
            </details>"""]
            if last_outcome == "connected" and not outcome:
                blocks.append(f"""
                <details open><summary>② 填写回拨结果与下一步</summary>
                <form method="post" action="/act/outcome">
                  <input type="hidden" name="as" value="{as_id}">
                  <input type="hidden" name="promise_id" value="{pid}">
                  <label>回拨结果（必填）</label><textarea name="result" rows="2"></textarea>
                  <label>下一步（必填）</label><textarea name="next_step" rows="2"></textarea>
                  <div class="row"><button class="amber" type="submit">登记结果，闭环承诺</button></div>
                </form></details>""")
            if last_outcome == "no_answer":
                blocks.append(f"""
                <details open><summary>② 未接通：有依据地再次预约</summary>
                <form method="post" action="/act/reschedule">
                  <input type="hidden" name="as" value="{as_id}">
                  <input type="hidden" name="promise_id" value="{pid}">
                  <label>再次预约依据（必填：客户短信 / 可接听时段 / 值班组安排）</label>
                  <select name="reason">
                    <option>客户短信要求改约</option>
                    <option>客户可接听时段调整</option>
                    <option>值班组安排</option>
                  </select>
                  <label>新可接听时段</label>
                  <input name="reachable_window" value="{esc(p['reachable_window'])}">
                  <label>新承诺时刻（YYYY-MM-DDTHH:MM，且不得落入禁扰时段）</label>
                  <input name="new_promised_at" placeholder="2026-10-02T20:30">
                  <div class="row"><button class="amber" type="submit">生成新承诺并短信告知</button></div>
                </form></details>""")
            agents = fetchall(
                c, "SELECT * FROM agents WHERE id!=? AND status='active' ORDER BY id",
                (p["owner_agent_id"],))
            opts = "".join(f'<option value="{a["id"]}">{esc(a["name"])}</option>'
                           for a in agents)
            blocks.append(f"""
            <details><summary>③ 转派（乐观锁，并发后转者失败）</summary>
            <form method="post" action="/act/transfer">
              <input type="hidden" name="as" value="{as_id}">
              <input type="hidden" name="promise_id" value="{pid}">
              <input type="hidden" name="version" value="{p['version']}">
              <label>转给</label><select name="to_agent_id">{opts}</select>
              <label>原因</label><input name="reason" placeholder="如：客户指定由该坐席跟进">
              <div class="row"><button type="submit">执行转派</button></div>
            </form></details>""")
            blocks.append(f"""
            <details><summary>④ 其他渠道已解决 / 重复创建关闭</summary>
            <form method="post" action="/act/resolved">
              <input type="hidden" name="as" value="{as_id}">
              <input type="hidden" name="promise_id" value="{pid}">
              <label>已由哪个渠道解决（凭据/工单号，必填）</label>
              <input name="evidence" placeholder="如 App 在线客服，工单 #APP-9920">
              <div class="row"><button class="ghost" type="submit">标记为其他渠道解决</button></div>
            </form>
            <form method="post" action="/act/duplicate">
              <input type="hidden" name="as" value="{as_id}">
              <input type="hidden" name="promise_id" value="{pid}">
              <label>并入的有效承诺编号</label>
              <input name="kept_promise_id" type="number">
              <div class="row"><button class="ghost" type="submit">标记为重复创建并关闭</button></div>
            </form></details>""")
            if p["status"] == services.DISPUTE and me["role"] != "supervisor":
                blocks.append('<p class="muted">该承诺处于争议状态，只有主管可以裁决或重开。</p>')
            actions = blocks

        outcome_html = ""
        if outcome:
            outcome_html = f"""
            <h2>回拨结果</h2>
            <div class="card"><dl class="meta">
              <dt>结果</dt><dd>{esc(outcome['result'])}</dd>
              <dt>下一步</dt><dd>{esc(outcome['next_step'])}</dd>
            </dl></div>"""

        body = f"""{fl}
        <p class="muted"><a href="/customer?id={p['customer_id']}&as={as_id}">← 返回 {esc(p['customer_name'])} 的客户档案</a></p>
        <div class="card {'dispute' if p['status']==services.DISPUTE else ''}">
          <div style="display:flex;gap:10px;align-items:center;flex-wrap:wrap">
            {status_badge(p['status'], p['overdue'])}
            <span data-due="{esc(p['promised_at'])}:00" class="countdown"></span>
            <span class="muted">承诺 {esc(p['promised_at'].replace('T',' '))}</span>
            <span style="margin-left:auto" class="muted">版本 v{p['version']} · 负责人 {esc(p['owner_name'])}</span>
          </div>
          <h3 style="margin:8px 0 2px">{esc(p['customer_name'])} · {esc(p['customer_phone'])}
            <span class="muted">禁扰 {esc(p['quiet_from'] or '—')}–{esc(p['quiet_to'] or '—')}</span></h3>
          <dl class="meta" style="margin-top:6px">
            <dt>问题摘要</dt><dd>{esc(p['summary'])}</dd>
            <dt>原始会话</dt><dd>{esc(p['origin_session'])}</dd>
            <dt>可接听时段</dt><dd>{esc(p['reachable_window'])}</dd>
          </dl>
        </div>
        <h2>处理动作</h2>
        <div class="card">{''.join(actions)}</div>
        {outcome_html}
        <h2>拨打记录</h2>
        <table><thead><tr><th>时间</th><th>结果</th><th>说明</th><th>录音（本地虚构文件）</th></tr></thead>
        <tbody>{calls_html or '<tr><td colspan="4" class="muted">尚未拨打</td></tr>'}</tbody></table>
        <h2>时间线</h2>
        <ul class="timeline">{tl}</ul>
        """
        self._send(layout(f"承诺 #{pid}", body, c, as_id))

    # -- 客户时段 --
    def page_customers(self, as_id: int, params: dict):
        rows = services.customer_windows(self.conn)
        trs = "".join(
            f"""<tr><td><a href="/customer?id={r['id']}&as={as_id}">{esc(r['name'])}</a></td>
                <td>{esc(r['phone'])}</td>
                <td>{esc(r['active_window'] or '—')}</td>
                <td>{(esc(r['quiet_from'])+' – '+esc(r['quiet_to'])) if r['quiet_from'] else '无'}</td>
                <td class="muted">{esc(r['note'])}</td></tr>"""
            for r in rows)
        body = f"""<h2>客户时段 <span class="hint">可接听时段与禁扰时段</span></h2>
        <table><thead><tr><th>客户</th><th>号码</th><th>当前可接听时段</th><th>每日禁扰</th><th>备注</th></tr></thead>
        <tbody>{trs}</tbody></table>"""
        self._send(layout("客户时段", body, self.conn, as_id, "customers"))

    # -- 拨打记录 --
    def page_calls(self, as_id: int):
        rows = services.calls_report(self.conn)
        trs = "".join(
            f"""<tr><td>{esc(r['started_at'].replace('T',' '))}</td>
                <td><a href="/promise?id={r['promise_id']}&as={as_id}">#{r['promise_id']}</a></td>
                <td>{esc(r['customer_name'])}</td>
                <td>{esc(OUTCOME_LABEL.get(r['outcome'], r['outcome']))}</td>
                <td>{esc(r['agent_name'])}</td>
                <td>{esc(r['recording'] or '—')}</td></tr>"""
            for r in rows)
        body = f"""<h2>拨打记录 <span class="hint">来自模拟电话适配器；录音为本地虚构文件</span></h2>
        <table><thead><tr><th>时间</th><th>承诺</th><th>客户</th><th>结果</th><th>坐席</th><th>录音</th></tr></thead>
        <tbody>{trs or '<tr><td colspan="6" class="muted">暂无拨打记录</td></tr>'}</tbody></table>"""
        self._send(layout("拨打记录", body, self.conn, as_id, "calls"))

    # -- 转派报表 --
    def page_transfers(self, as_id: int):
        rows = services.transfers_report(self.conn)
        trs = "".join(
            f"""<tr class="{'dispute' if not r['won'] else ''}">
                <td>{esc(r['created_at'].replace('T',' '))}</td>
                <td><a href="/promise?id={r['promise_id']}&as={as_id}">#{r['promise_id']}</a>
                    {esc(r['customer_name'])}</td>
                <td>{esc(r['from_name'])}</td><td>→</td>
                <td>{esc(r['to_name'])}</td>
                <td>{esc(r['reason'])}</td>
                <td>{'<span class="badge overdue">竞争失败</span>' if not r['won'] else '<span class="badge done">成功</span>'}
                    <span class="muted">v{r['expected_version']}</span></td></tr>"""
            for r in rows)
        body = f"""<h2>转派与责任交接 <span class="hint">含坐席请假交接与并发竞争失败尝试</span></h2>
        <table><thead><tr><th>时间</th><th>承诺</th><th>原负责人</th><th></th><th>新负责人</th><th>原因</th><th>结果</th></tr></thead>
        <tbody>{trs or '<tr><td colspan="7" class="muted">暂无转派记录</td></tr>'}</tbody></table>"""
        self._send(layout("转派记录", body, self.conn, as_id, "transfers"))

    # -- 主管视图 --
    def page_supervisor(self, as_id: int, params: dict):
        c = self.conn
        me = services.get_agent(c, as_id)
        fl = flash(urllib.parse.unquote(params["ok"]), True) if params.get("ok") else ""
        if me["role"] != "supervisor":
            body = fl + """<div class="flash err">当前身份不是主管。请在右上角切换为主管身份查看失约报表与争议裁决。</div>"""
            self._send(layout("主管视图", body, c, as_id, "supervisor"))
            return
        services.mark_overdue(c)
        missed = services.missed_report(c)
        disputes = [r for r in missed if r["status"] == services.DISPUTE]
        cat_color = {"wrong_number": AMBER, "refused": INK_SOFT}

        def miss_rows():
            out = []
            for r in missed:
                actions = ""
                if r["status"] == services.DISPUTE:
                    cust = services.get_customer(c, r["customer_id"])
                    actions = f"""
                    <details><summary>裁决 / 核实</summary>
                      <form method="post" action="/act/fix-phone">
                        <input type="hidden" name="as" value="{as_id}">
                        <input type="hidden" name="customer_id" value="{r['customer_id']}">
                        <label>更正客户号码（主管）</label>
                        <input name="new_phone" value="{esc(cust['phone'])}">
                        <div class="row"><button class="ghost" type="submit">更正号码</button></div>
                      </form>
                      <form method="post" action="/act/dispute-reopen">
                        <input type="hidden" name="as" value="{as_id}">
                        <input type="hidden" name="promise_id" value="{r['id']}">
                        <label>核实后重开，约定新时刻</label>
                        <input name="new_promised_at" placeholder="2026-10-02T15:00">
                        <input name="note" placeholder="核实结论（必填）">
                        <div class="row"><button class="amber" type="submit">重开承诺</button></div>
                      </form>
                      <form method="post" action="/act/dispute-close">
                        <input type="hidden" name="as" value="{as_id}">
                        <input type="hidden" name="promise_id" value="{r['id']}">
                        <label>关闭争议（主管确认，坐席无权删除）</label>
                        <input name="note" placeholder="关闭结论（必填）">
                        <div class="row"><button class="danger" type="submit">确认关闭争议</button></div>
                      </form>
                    </details>"""
                handover = ""
                tr = fetchone(
                    c,
                    """SELECT t.*, af.name fn, at2.name tn FROM transfers t
                       JOIN agents af ON af.id=t.from_agent_id
                       JOIN agents at2 ON at2.id=t.to_agent_id
                       WHERE t.promise_id=? ORDER BY t.id DESC LIMIT 1""", (r["id"],))
                if tr:
                    handover = f'<div class="muted">责任交接：{esc(tr["fn"])} → {esc(tr["tn"])}（{esc(tr["reason"])}）</div>'
                out.append(f"""<tr class="{'dispute' if r['status']==services.DISPUTE else 'overdue'}">
                  <td><a href="/promise?id={r['id']}&as={as_id}">#{r['id']}</a></td>
                  <td>{esc(r['customer_name'])}<div class="muted">{esc(r['customer_phone'])}</div></td>
                  <td>{status_badge(r['status'], r['overdue'])}</td>
                  <td><b style="color:{cat_color.get(r['miss_category'], DANGER)}">{esc(r['miss_reason'])}</b>
                      {handover}</td>
                  <td>{esc(r['owner_name'])}</td>
                  <td>{esc(r['promised_at'].replace('T',' '))}</td>
                  <td>{actions or '—'}</td></tr>""")
            return "".join(out)

        reviews = fetchall(
            c,
            """SELECT dr.*, a.name sup, cu.name cname FROM dispute_reviews dr
               JOIN agents a ON a.id=dr.supervisor_id
               JOIN promises p ON p.id=dr.promise_id
               JOIN customers cu ON cu.id=p.customer_id ORDER BY dr.id DESC""")
        rev_html = "".join(
            f"""<tr><td>{esc(r['created_at'].replace('T',' '))}</td>
                <td>#{r['promise_id']} {esc(r['cname'])}</td>
                <td>{esc(r['sup'])}</td>
                <td>{'关闭' if r['decision']=='close' else '重开'}</td>
                <td>{esc(r['note'])}</td></tr>"""
            for r in reviews)
        n_open_overdue = sum(1 for r in missed if r["status"] == services.OPEN)
        body = f"""{fl}
        <div class="kpis">
          <div class="kpi"><div class="n red">{n_open_overdue}</div><div class="l">逾期待回拨</div></div>
          <div class="kpi"><div class="n amber">{len(disputes)}</div><div class="l">争议待裁决（含拨错号）</div></div>
          <div class="kpi"><div class="n">{len(missed)}</div><div class="l">失约记录总数</div></div>
        </div>
        <h2>失约报表 <span class="hint">突出失约原因与责任交接；逾期记录坐席不可删除，只能主管裁决</span></h2>
        <table><thead><tr><th>承诺</th><th>客户</th><th>状态</th><th>失约原因 / 交接</th><th>当前责任人</th><th>承诺时刻</th><th>主管操作</th></tr></thead>
        <tbody>{miss_rows() or '<tr><td colspan="7" class="muted">暂无失约记录</td></tr>'}</tbody></table>
        <h2>裁决留痕</h2>
        <table><thead><tr><th>时间</th><th>承诺</th><th>主管</th><th>决定</th><th>结论</th></tr></thead>
        <tbody>{rev_html or '<tr><td colspan="5" class="muted">暂无裁决</td></tr>'}</tbody></table>"""
        self._send(layout("主管视图", body, c, as_id, "supervisor"))


EVENT_LABELS = {
    "promise_created": "创建承诺",
    "call_dialed": "发起回拨",
    "promise_fulfilled": "回拨完成",
    "promise_rescheduled": "再次预约",
    "promise_overdue": "判定逾期",
    "promise_transferred": "责任转派",
    "transfer_conflict": "转派竞争失败",
    "promise_closed": "关闭承诺",
    "dispute_raised": "升级争议",
    "dispute_closed": "主管关闭争议",
    "dispute_reopened": "主管重开",
    "customer_created": "建档",
    "customer_phone_fixed": "主管更正号码",
    "agent_created": "坐席入职",
    "agent_on_leave": "坐席请假",
}


def serve(host: str | None = None, port: int | None = None) -> None:
    host = host or config.HOST
    port = port or config.PORT
    init_db(connect())
    httpd = ThreadingHTTPServer((host, port), App)
    print(f"[server] 回拨承诺台已启动：http://{host}:{port}", flush=True)
    print("[server] 打开 /queue 坐席视图，/supervisor 主管视图（右上角切换身份）",
          flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n[server] 已停止")


if __name__ == "__main__":
    serve()
