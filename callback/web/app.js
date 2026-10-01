/* 回拨承诺管理台前端逻辑（无框架、无依赖） */
"use strict";

const STATUS_CN = {
  pending: "待回拨",
  completed: "已完成",
  rescheduled: "已改约",
  expired: "已逾期",
  dispute_closed: "争议关闭",
  cancelled_wrong_number: "号码拨错",
  cancelled_refused: "客户拒接",
  cancelled_resolved_elsewhere: "他渠道已解决",
  cancelled_deduplicate: "重复创建",
  cancelled_duplicate: "重复创建",
};
const OUTCOME_CN = {
  connected: "已接通", no_answer: "未接通", refused: "客户拒接", wrong_number: "号码拨错",
};
const REASSIGN_REASON_CN = {
  manual: "手动转派", leave_handover: "请假交接", duty_takeover: "值班组接管",
};

const state = {
  now: null, agents: [], customers: [], windows: [],
  queue: [], calls: [], report: null,
  role: "agent", actorId: null, selected: null,
};

// ---- 工具 ----
const $ = (s) => document.querySelector(s);
const el = (tag, cls, html) => {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (html !== undefined) n.innerHTML = html;
  return n;
};
const fmtDT = (iso) => {
  const d = new Date(iso);
  const p = (x) => String(x).padStart(2, "0");
  return `${d.getMonth() + 1}/${d.getDate()} ${p(d.getHours())}:${p(d.getMinutes())}`;
};
const fmtDTfull = (iso) => {
  const d = new Date(iso);
  const p = (x) => String(x).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
};
const fmtDur = (sec) => {
  if (sec < 0) sec = 0;
  const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = sec % 60;
  if (h > 0) return `${h}时${m}分`;
  if (m > 0) return `${m}分${s}秒`;
  return `${s}秒`;
};
function toast(msg, isErr = false) {
  const t = $("#toast");
  t.textContent = msg;
  t.className = "toast" + (isErr ? " err" : "");
  setTimeout(() => t.classList.add("hidden"), 3200);
}
async function api(path, body) {
  const opt = body
    ? { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) }
    : {};
  const res = await fetch(path, opt);
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.message || `请求失败 (${res.status})`);
  return data;
}
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

// ---- 数据加载 ----
async function loadState() {
  const params = new URLSearchParams();
  if (state.role === "agent" && state.actorId) params.set("owner_id", state.actorId);
  const status = $("#statusFilter").value;
  if (status) params.set("status", status);
  const d = await api("/api/state?" + params.toString());
  Object.assign(state, {
    now: d.now, agents: d.agents, customers: d.customers, windows: d.windows,
    queue: d.queue, calls: d.calls, report: d.report,
  });
  renderAll();
}

// ---- 顶栏 ----
function renderActors() {
  const sel = $("#actorSelect");
  const list = state.role === "supervisor"
    ? state.agents
    : state.agents.filter((a) => a.role !== "duty_group");
  const prev = state.actorId;
  sel.innerHTML = "";
  for (const a of list) {
    const o = el("option", null, esc(a.name) + (a.on_leave ? "（请假）" : ""));
    o.value = a.id;
    sel.add(o);
  }
  if (list.some((a) => a.id === prev)) sel.value = prev;
  else state.actorId = Number(sel.value);
  $("#actorWrap").style.display = state.role === "supervisor" ? "none" : "";
}

// ---- 承诺队列 ----
function countdownOf(p, nowMs) {
  const tProm = new Date(p.promised_at).getTime();
  const tDead = new Date(p.deadline_at).getTime();
  if (p.status !== "pending") return null;
  if (nowMs >= tDead)
    return { cls: "overdue", text: "已逾期 " + fmtDur(Math.floor((nowMs - tDead) / 1000)) };
  if (nowMs >= tProm)
    return { cls: "urgent", text: "到承诺时刻·宽限 " + fmtDur(Math.floor((tDead - nowMs) / 1000)) };
  const sec = Math.floor((tProm - nowMs) / 1000);
  const cls = sec < 15 * 60 ? "soon" : "calm";
  return { cls, text: "倒计时 " + fmtDur(sec) };
}

function renderQueue(nowMs) {
  const box = $("#queue");
  box.innerHTML = "";
  const pending = state.queue.filter((p) => p.status === "pending");
  const others = state.queue.filter((p) => p.status !== "pending");
  pending.sort((a, b) => new Date(a.promised_at) - new Date(b.promised_at));
  const list = [...pending, ...others];
  $("#queueMeta").textContent = `${pending.length} 条待回拨 / 共 ${list.length} 条`;

  for (const p of list) {
    const card = el("div", "card" + (state.selected === p.id ? " selected" : ""));
    const cd = countdownOf(p, nowMs);
    const badge = `<span class="badge ${p.status}">${STATUS_CN[p.status] || p.status}</span>`;
    const right = cd
      ? `<span class="countdown ${cd.cls}">${cd.text}</span>`
      : p.is_overdue ? `<span class="countdown overdue">已逾期</span>` : badge;
    card.innerHTML = `
      <div class="card-row1">
        <span class="card-name">${esc(p.customer_name)}</span>
        ${right}
      </div>
      <div class="card-summary">${esc(p.summary)}</div>
      <div class="card-meta">
        <span>${esc(p.owner_name)} · #${p.id}</span>
        <span>${fmtDT(p.promised_at)} · 时限 ${fmtDT(p.deadline_at)}</span>
      </div>`;
    card.onclick = () => selectPromise(p.id);
    box.appendChild(card);
  }
}

// ---- 详情 ----
async function selectPromise(id) {
  state.selected = id;
  renderAll();
  const p = state.queue.find((x) => x.id === id);
  if (!p) return;
  const d = await api(`/api/customer/${p.customer_id}`);
  renderDetail(d, id);
}

function actionButtons(p) {
  const isSup = state.role === "supervisor";
  const btns = [];
  if (p.status === "pending") {
    btns.push(`<button class="btn btn-sm btn-amber" data-act="dial">模拟拨打</button>`);
    btns.push(`<button class="btn btn-sm" data-act="complete">回填完成</button>`);
    btns.push(`<button class="btn btn-sm" data-act="reschedule">未接通·再次预约</button>`);
    btns.push(`<button class="btn btn-sm" data-act="sms">发短信</button>`);
    `<div class="actions">`;
    btns.push(`<button class="btn btn-sm btn-danger" data-act="cancelmenu">取消承诺</button>`);
    btns.push(`<button class="btn btn-sm" data-act="reassign">转派</button>`);
  }
  if (p.status === "expired" && isSup)
    btns.push(`<button class="btn btn-sm btn-amber" data-act="dispute">主管关闭争议</button>`);
  if (isSup)
    btns.push(`<button class="btn btn-sm btn-danger" data-act="delete">删除记录</button>`);
  return btns;
}

function renderDetail(d, promiseId) {
  const box = $("#detail");
  box.innerHTML = "";
  const p = d.promises.find((x) => x.id === promiseId);
  if (!p) {
    box.textContent = "承诺不存在或已被移除。";
    return;
  }
  const cust = d.customer;
  const cd = countdownOf(p, Date.now());

  const head = el("div", "detail-head");
  head.innerHTML = `
    <h3>${esc(cust.name)}
      <span class="badge ${p.status}">${STATUS_CN[p.status] || p.status}</span>
      ${cd ? `<span class="countdown ${cd.cls}">${cd.text}</span>` : ""}
    </h3>
    <dl class="detail-grid">
      <dt>电话</dt><dd>${esc(cust.phone)}</dd>
      <dt>承诺</dt><dd>${fmtDTfull(p.promised_at)}（时限 ${fmtDTfull(p.deadline_at)}）</dd>
      <dt>负责人</dt><dd>${esc(p.owner_name)} <span class="mono">v${p.owner_version}</span></dd>
      <dt>问题摘要</dt><dd>${esc(p.summary)}</dd>
      <dt>原始会话</dt><dd class="mono">${esc(p.origin_session)}</dd>
      ${p.result ? `<dt>回拨结果</dt><dd>${esc(p.result)}</dd>` : ""}
      ${p.next_step ? `<dt>下一步</dt><dd>${esc(p.next_step)}</dd>` : ""}
      ${p.reschedule_basis ? `<dt>改约依据</dt><dd>${esc(p.reschedule_basis)}</dd>` : ""}
      ${p.dispute_note ? `<dt>争议说明</dt><dd>${esc(p.dispute_note)}${p.closed_by_name ? "（主管 " + esc(p.closed_by_name) + "）" : ""}</dd>` : ""}
    </dl>`;
  box.appendChild(head);

  // 客户可接听/禁扰时段
  box.appendChild(el("div", "section-title", `客户时段（UTC${cust.tz_offset >= 0 ? "+" : ""}${cust.tz_offset / 60}）`));
  const winBox = el("div");
  for (const w of d.windows) {
    winBox.appendChild(el("div", "win-row",
      `<span>${esc(w.label)}</span><span class="mono">${esc(w.note || "")}</span>`));
  }
  box.appendChild(winBox);

  // 操作
  const btns = actionButtons(p);
  if (btns.length) {
    const ab = el("div", "actions");
    ab.innerHTML = btns.join("");
    ab.querySelectorAll("button").forEach((b) => {
      b.onclick = () => onAction(b.dataset.act, p, cust);
    });
    box.appendChild(ab);
  }

  // 拨打记录
  box.appendChild(el("div", "section-title", `拨打记录（${p.calls.length}）· 录音仅本地虚构文件`));
  if (!p.calls.length) box.appendChild(el("div", "call-row mono", "暂无拨打记录"));
  for (const c of p.calls) {
    box.appendChild(el("div", "call-row",
      `<span><span class="tag-outcome ${c.outcome}">${OUTCOME_CN[c.outcome]}</span>
              ${esc(c.agent_name)} · ${c.duration_s ? c.duration_s + "秒" : "—"}</span>
       <span class="mono" title="${esc(c.recording)}">${fmtDT(c.created_at)} ${esc((c.recording || "").split("/").pop())}</span>`));
  }

  // 时间线
  box.appendChild(el("div", "section-title", "时间线"));
  const tl = el("div", "timeline");
  for (const e of p.events) {
    const danger = ["expired", "cancel_wrong_number", "cancel_refused"].includes(e.type);
    const amber = ["reassigned", "rescheduled"].includes(e.type);
    const green = e.type === "completed" || e.type === "dispute_closed";
    const item = el("div", "tl-item" + (danger ? " danger" : amber ? " amber" : green ? " green" : ""));
    item.innerHTML = `<span class="tl-type">${esc(e.type)}</span>
      <span class="tl-detail">${esc(e.detail)}</span><br>
      <span class="tl-time">${fmtDTfull(e.created_at)} · ${esc(e.actor_name || "系统")}</span>`;
    tl.appendChild(item);
  }
  box.appendChild(tl);

  // 客户历史与当前承诺同屏
  box.appendChild(el("div", "section-title", `客户历史（${d.promises.length} 条承诺）`));
  for (const h of d.promises) {
    const row = el("div", "hist-row call-row" + (h.id === p.id ? " hist-current" : ""));
    row.innerHTML = `<span>#${h.id} <span class="badge ${h.status}">${STATUS_CN[h.status] || h.status}</span>
        ${esc(h.summary)}</span>
      <span class="mono">${fmtDT(h.promised_at)} · ${esc(h.owner_name)}</span>`;
    row.style.cursor = "pointer";
    row.onclick = () => selectPromise(h.id);
    box.appendChild(row);
  }
}

// ---- 动作 ----
async function onAction(act, p, cust) {
  try {
    if (act === "dial") return openDial(p, cust);
    if (act === "complete") return openComplete(p);
    if (act === "reschedule") return openReschedule(p);
    if (act === "reassign") return openReassign(p);
    if (act === "sms") return openSms(p, cust);
    if (act === "cancelmenu") return openCancel(p);
    if (act === "dispute") return openDispute(p);
    if (act === "delete") {
      if (!confirm("确认删除该承诺？逾期记录不可删除（需主管关闭争议）。")) return;
      await api(`/api/promises/${p.id}/delete`, { actor_id: state.actorId });
      toast("已删除"); state.selected = null; return loadState();
    }
  } catch (e) { toast(e.message, true); }
}

function defaultLocalTime(offsetMin = 60) {
  const d = new Date(Date.now() + offsetMin * 60000);
  const p = (x) => String(x).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}T${p(d.getHours())}:${p(d.getMinutes())}`;
}

function openModal(title, fieldsHtml, onOk) {
  $("#modalTitle").textContent = title;
  const body = $("#modalBody");
  body.innerHTML = fieldsHtml;
  $("#modal").classList.remove("hidden");
  const get = (name) => body.querySelector(`[name="${name}"]`).value.trim();
  $("#modalOk").onclick = async () => {
    try {
      await onOk(get, body);
      $("#modal").classList.add("hidden");
      await loadState();
      if (state.selected) selectPromise(state.selected);
    } catch (e) { toast(e.message, true); }
  };
}
function closeModal() { $("#modal").classList.add("hidden"); }

function customerOptions() {
  return state.customers.map((c) => `<option value="${c.id}">${esc(c.name)}（${esc(c.phone)}）</option>`).join("");
}
function agentOptions(excludeId) {
  return state.agents
    .filter((a) => a.role !== "duty_group" && a.id !== excludeId && !a.on_leave && a.active)
    .map((a) => `<option value="${a.id}">${esc(a.name)}</option>`).join("");
}

function openDial(p, cust) {
  openModal(`模拟拨打 #${p.id} · ${cust.name}`, `
    <div class="field"><label>结果</label>
      <select name="outcome">
        <option value="connected">已接通</option>
        <option value="no_answer">未接通</option>
        <option value="refused">客户拒接</option>
        <option value="wrong_number">号码拨错</option>
      </select></div>
    <div class="field"><label>通话时长（秒，接通时）</label><input name="duration_s" value="60"></div>
    <div class="field hint">录音将保存为本地虚构占位文本文件，不产生真实通话。</div>
  `, async (get) => {
    const r = await api(`/api/promises/${p.id}/dial`, {
      actor_id: state.actorId, phone: cust.phone,
      outcome: get("outcome"), duration_s: Number(get("duration_s") || 0),
    });
    toast(`拨打已记录：${OUTCOME_CN[r.call.outcome]}`);
  });
}

function openComplete(p) {
  openModal(`回填完成 #${p.id}`, `
    <div class="field"><label>回拨结果（必填）</label><textarea name="result" rows="3"></textarea></div>
    <div class="field"><label>下一步（必填）</label><textarea name="next_step" rows="2"></textarea></div>
  `, async (get) => {
    await api(`/api/promises/${p.id}/complete`, {
      actor_id: state.actorId, result: get("result"), next_step: get("next_step"),
    });
    toast("已回填完成");
  });
}

function openReschedule(p) {
  openModal(`有依据再次预约 #${p.id}`, `
    <div class="field"><label>新承诺时刻（须避开禁扰时段）</label>
      <input type="datetime-local" name="new_time" value="${defaultLocalTime(120)}"></div>
    <div class="field"><label>依据类型</label>
      <select name="basis_type">
        <option value="no_answer">不接通（须已有未接拨打记录）</option>
        <option value="customer_request">客户要求改期</option>
        <option value="line_fault">线路故障</option>
      </select></div>
    <div class="field"><label>依据说明（必填）</label>
      <textarea name="basis" rows="2" placeholder="如：两次拨打未接，客户短信回复明日可接"></textarea></div>
  `, async (get) => {
    const r = await api(`/api/promises/${p.id}/reschedule`, {
      actor_id: state.actorId,
      new_time: new Date(get("new_time")).toISOString(),
      basis_type: get("basis_type"), basis: get("basis"),
    });
    state.selected = r.new_promise_id;
    toast(`已改约，新承诺 #${r.new_promise_id}`);
  });
}

function openReassign(p) {
  openModal(`转派 #${p.id}`, `
    <div class="field"><label>新负责人</label><select name="to">${agentOptions(p.owner_id)}</select></div>
    <div class="field"><label>原因</label>
      <select name="reason">
        <option value="manual">手动转派</option>
        <option value="duty_takeover">值班组接管</option>
      </select></div>
    <div class="field hint">并发转派以版本号乐观锁控制，同时只会有一个有效负责人。</div>
  `, async (get, body) => {
    let target = Number(get("to"));
    if (get("reason") === "duty_takeover") {
      const duty = state.agents.find((a) => a.role === "duty_group" && !a.on_leave);
      if (!duty) throw new Error("没有可用值班组");
      target = duty.id;
    }
    const r = await api(`/api/promises/${p.id}/reassign`, {
      actor_id: state.actorId, to_agent_id: target,
      expected_version: p.owner_version, reason: get("reason"),
    });
    toast(`转派成功，新版本 v${r.owner_version}`);
  });
}

function openSms(p, cust) {
  openModal(`模拟短信 · ${cust.name}`, `
    <div class="field"><label>短信内容（仅本地模拟，不外发）</label>
      <textarea name="body" rows="3"></textarea></div>
  `, async (get) => {
    await api(`/api/promises/${p.id}/sms`, {
      actor_id: state.actorId, customer_id: cust.id, body: get("body"),
    });
    toast("短信已记录（模拟）");
  });
}

function openCancel(p) {
  openModal(`取消承诺 #${p.id}`, `
    <div class="field"><label>原因</label>
      <select name="reason">
        <option value="wrong_number">号码拨错（须有 wrong_number 拨打记录）</option>
        <option value="refused">客户拒接（须有 refused 拨打记录）</option>
        <option value="resolved_elsewhere">问题已由其他渠道解决</option>
        <option value="duplicate">重复创建</option>
      </select></div>
    <div class="field" id="chanWrap"><label>解决渠道</label>
      <input name="channel" placeholder="如：App 在线客服 / 门店"></div>
    <div class="field hidden" id="dupWrap"><label>保留的承诺 ID</label>
      <input name="kept" type="number"></div>
  `, async (get, body) => {
    const reason = get("reason");
    body.querySelector("#chanWrap").classList.toggle("hidden", reason !== "resolved_elsewhere");
    body.querySelector("#dupWrap").classList.toggle("hidden", reason !== "duplicate");
    const payload = { actor_id: state.actorId, reason };
    if (reason === "resolved_elsewhere") payload.channel = get("channel");
    if (reason === "duplicate") payload.kept_promise_id = Number(get("kept"));
    await api(`/api/promises/${p.id}/cancel`, payload);
    toast("承诺已取消");
  });
  // 动态显隐
  setTimeout(() => {
    const sel = document.querySelector("#modalBody [name='reason']");
    sel.onchange = () => {
      const reason = sel.value;
      $("#chanWrap").classList.toggle("hidden", reason !== "resolved_elsewhere");
      $("#dupWrap").classList.toggle("hidden", reason !== "duplicate");
    };
    sel.onchange();
  }, 0);
}

function openDispute(p) {
  openModal(`主管关闭争议 #${p.id}`, `
    <div class="field hint" style="color:var(--danger)">该承诺已逾期，坐席不可删除；主管确认后关闭争议。</div>
    <div class="field"><label>主管确认说明（必填）</label>
      <textarea name="note" rows="3" placeholder="失约原因认定与处理结论"></textarea></div>
  `, async (get) => {
    await api(`/api/promises/${p.id}/dispute`, { actor_id: state.actorId, note: get("note") });
    toast("争议已关闭");
  });
}

function openNew() {
  openModal("新建回拨承诺", `
    <div class="field"><label>客户</label><select name="customer_id">${customerOptions()}</select></div>
    <div class="field"><label>承诺回拨时刻（系统校验禁扰/可接听时段）</label>
      <input type="datetime-local" name="promised_at" value="${defaultLocalTime(60)}"></div>
    <div class="field"><label>问题摘要</label><textarea name="summary" rows="2"></textarea></div>
    <div class="field"><label>原始会话标识</label>
      <input name="origin_session" placeholder="如 chat-20261001-0000" value="web-${Date.now()}"></div>
    <div class="field"><label>负责坐席</label>
      <select name="owner_id">${state.agents.filter((a) => !a.on_leave && a.role !== "duty_group")
        .map((a) => `<option value="${a.id}"${a.id === state.actorId ? " selected" : ""}>${esc(a.name)}</option>`).join("")}</select></div>
  `, async (get) => {
    const r = await api("/api/promises", {
      customer_id: Number(get("customer_id")),
      actor_id: state.actorId,
      owner_id: Number(get("owner_id")),
      summary: get("summary"), origin_session: get("origin_session"),
      promised_at: new Date(get("promised_at")).toISOString(),
    });
    state.selected = r.id;
    toast(`承诺 #${r.id} 已创建`);
  });
}

// ---- 右侧报表 ----
function renderReport() {
  const r = state.report;
  const box = $("#tab-breach");
  box.innerHTML = "";
  const s = r.stats;
  const sg = el("div", "stat-grid");
  sg.innerHTML = `
    <div class="stat"><div class="num">${s.open}</div><div class="lbl">待回拨</div></div>
    <div class="stat warn"><div class="num">${s.overdue_open}</div><div class="lbl">逾期争议待处理</div></div>
    <div class="stat amber"><div class="num">${s.reassignments}</div><div class="lbl">转派次数</div></div>
    <div class="stat"><div class="num">${s.total_promises}</div><div class="lbl">承诺总数</div></div>`;
  box.appendChild(sg);

  if (!r.breached.length) box.appendChild(el("div", "meta", "暂无失约记录。"));
  for (const b of r.breached) {
    const card = el("div", "breach-card is-" + b.status);
    const chain = b.handovers.map((h) =>
      `<div class="handover-step">${esc(h.from)}
        <span class="handover-arrow">→</span>${esc(h.to)}
        <span class="mono">（${REASSIGN_REASON_CN[h.reason] || h.reason} · ${fmtDT(h.at)}）</span></div>`
    ).join("");
    card.innerHTML = `
      <div class="breach-title"><span>#${b.promise_id} ${esc(b.customer_name)}</span>
        <span class="badge ${b.status}">${STATUS_CN[b.status] || b.status}</span></div>
      <div class="breach-reason">失约原因：${esc(b.reason)}</div>
      <div class="breach-meta">${esc(b.summary)}</div>
      <div class="breach-meta">超时限 ${fmtDur(b.lateness_seconds)} · 当前责任人：<b>${esc(b.current_owner)}</b></div>
      ${b.dispute_note ? `<div class="breach-meta">主管结论：${esc(b.dispute_note)}${b.closed_by ? "（" + esc(b.closed_by) + "）" : ""}</div>` : ""}
      <div class="handover-chain">责任交接：${chain || "无转派"}</div>
      ${b.status === "expired" && state.role === "supervisor"
        ? `<div class="actions" style="padding:8px 0 0">
             <button class="btn btn-sm btn-amber" data-dispute="${b.promise_id}">主管关闭争议</button></div>` : ""}`;
    box.appendChild(card);
  }
  box.querySelectorAll("[data-dispute]").forEach((btn) => {
    btn.onclick = async () => {
      const id = Number(btn.dataset.dispute);
      const p = state.queue.find((x) => x.id === id);
      if (p) { await selectPromise(id); openDispute(p); }
      else toast("请先在筛选中选择对应承诺", true);
    };
  });

  // 客户时段
  const wbox = $("#tab-windows");
  wbox.innerHTML = "";
  const byCust = {};
  for (const w of state.windows) (byCust[w.customer_id] ??= []).push(w);
  for (const c of state.customers) {
    wbox.appendChild(el("div", "section-title", esc(c.name)));
    for (const w of byCust[c.id] || [])
      wbox.appendChild(el("div", "win-row", `<span>${esc(w.label)}</span>`));
  }

  // 拨打记录
  const cbox = $("#tab-calls");
  cbox.innerHTML = "";
  for (const c of state.calls) {
    cbox.appendChild(el("div", "call-row",
      `<span><span class="tag-outcome ${c.outcome}">${OUTCOME_CN[c.outcome]}</span>
        ${esc(c.customer_name)} ← ${esc(c.agent_name)}</span>
       <span class="mono">#${c.promise_id} · ${fmtDT(c.created_at)}</span>`));
  }
  if (!state.calls.length) cbox.appendChild(el("div", "meta", "暂无拨打记录。"));
}

// ---- 总渲染 ----
function renderAll() {
  renderActors();
  const nowMs = Date.now();
  renderQueue(nowMs);
  renderReport();
  $("#clock").textContent = "服务器时间 " + fmtDTfull(state.now);
  if (state.selected) {
    const p = state.queue.find((x) => x.id === state.selected);
    if (p) api(`/api/customer/${p.customer_id}`).then((d) => renderDetail(d, p.id));
    else $("#detail").innerHTML = '<div class="detail-empty">该承诺不在当前筛选范围内。</div>';
  }
}

// ---- 事件绑定 ----
$("#roleSelect").onchange = (e) => {
  state.role = e.target.value;
  if (state.role === "supervisor") {
    state.actorId = state.agents.find((a) => a.role === "supervisor")?.id || state.actorId;
  } else {
    state.actorId = state.agents.find((a) => a.role === "agent" && !a.on_leave)?.id || state.actorId;
  }
  loadState();
};
$("#actorSelect").onchange = (e) => { state.actorId = Number(e.target.value); loadState(); };
$("#statusFilter").onchange = loadState;
$("#refreshBtn").onclick = loadState;
$("#newBtn").onclick = openNew;
$("#modalCancel").onclick = closeModal;
$("#modal").onclick = (e) => { if (e.target.id === "modal") closeModal(); };
document.querySelectorAll(".tab").forEach((t) => {
  t.onclick = () => {
    document.querySelectorAll(".tab").forEach((x) => x.classList.remove("active"));
    t.classList.add("active");
    ["breach", "windows", "calls"].forEach((k) =>
      $("#tab-" + k).classList.toggle("hidden", k !== t.dataset.tab));
  };
});

// 每秒本地刷新倒计时（不请求后端）；每 30 秒拉取后端数据
setInterval(() => {
  renderQueue(Date.now());
}, 1000);
setInterval(loadState, 30000);

// 启动
(async function init() {
  const d = await api("/api/state");
  state.agents = d.agents;
  const first = state.agents.find((a) => a.role === "agent");
  state.actorId = first ? first.id : state.agents[0]?.id;
  await loadState();
})();
