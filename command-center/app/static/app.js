"use strict";
/* Command Center front end — no build step, no dependencies. */

const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

const state = { view: "today", courses: [], courseFilter: "", statusFilter: "open", drillQueue: [], drillIdx: 0, revealed: false };

async function api(path, opts = {}) {
  const init = { headers: { "Content-Type": "application/json" }, ...opts };
  if (init.body && typeof init.body !== "string") init.body = JSON.stringify(init.body);
  const r = await fetch(path, init);
  if (r.status === 401) { location.href = "/login"; throw new Error("unauthorized"); }
  const ct = r.headers.get("content-type") || "";
  const data = ct.includes("json") ? await r.json() : await r.text();
  if (!r.ok) throw new Error((data && data.detail) || r.statusText);
  return data;
}

function toast(msg, undo) {
  $$(".toast").forEach((t) => t.remove());
  const t = document.createElement("div");
  t.className = "toast";
  t.innerHTML = `<span>${esc(msg)}</span>`;
  if (undo) {
    const b = document.createElement("button");
    b.textContent = "Undo";
    b.onclick = () => { t.remove(); undo(); };
    t.appendChild(b);
  }
  document.body.appendChild(t);
  setTimeout(() => t.remove(), 4500);
}

/* ---------- time formatting ---------- */
const d = (iso) => (iso ? new Date(iso) : null);
const fmtTime = (iso) => d(iso).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
function relDue(iso) {
  if (!iso) return { text: "no due date", cls: "" };
  const due = d(iso), now = new Date();
  const days = Math.round((startOfDay(due) - startOfDay(now)) / 864e5);
  const time = fmtTime(iso);
  if (due < now) {
    const h = (now - due) / 36e5;
    return { text: h < 24 ? `overdue ${Math.max(1, Math.round(h))}h` : `overdue ${Math.round(h / 24)}d`, cls: "due-overdue" };
  }
  if (days === 0) return { text: `today ${time}`, cls: "due-today" };
  if (days === 1) return { text: `tomorrow ${time}`, cls: "" };
  if (days < 7) return { text: `${due.toLocaleDateString([], { weekday: "short" })} ${time}`, cls: "" };
  return { text: due.toLocaleDateString([], { month: "short", day: "numeric" }) + " " + time, cls: "" };
}
const startOfDay = (x) => new Date(x.getFullYear(), x.getMonth(), x.getDate());
function ago(iso) {
  if (!iso) return "never";
  const m = (Date.now() - d(iso)) / 6e4;
  if (m < 1) return "just now";
  if (m < 60) return `${Math.round(m)}m ago`;
  if (m < 1440) return `${Math.round(m / 60)}h ago`;
  return `${Math.round(m / 1440)}d ago`;
}

/* ---------- shared row renderer ---------- */
const ICON = {
  link: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M14 3h7v7M10 14 21 3M21 14v7H3V3h7"/></svg>',
  edit: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M12 20h9M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4z"/></svg>',
  pin: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M12 17v5M9 3h6l-1 7 4 3H6l4-3z"/></svg>',
  hide: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M18 6 6 18M6 6l12 12"/></svg>',
};

function courseChip(code, color) {
  if (!code) return "";
  return `<span class="chip"><span class="dot" style="background:${esc(color || "#64748b")}"></span>${esc(code)}</span>`;
}

function itemRow(i, { compact = false } = {}) {
  const due = relDue(i.due_at);
  const done = i.status === "done";
  return `<li class="row" data-id="${i.id}">
    <button class="check ${done ? "done" : ""}" data-act="toggle" aria-label="Mark done"></button>
    <div class="body">
      <div class="title">${i.pinned ? "📌 " : ""}${esc(i.title)}</div>
      <div class="meta">${courseChip(i.course, i.color)}<span class="${due.cls}">${esc(due.text)}</span>
        <span class="kind">${esc(i.kind || "")}</span>${i.effort_hours ? `<span>~${i.effort_hours}h</span>` : ""}
        ${i.planned_hours !== undefined ? `<span>${i.planned_hours}h planned</span>` : ""}
        ${i.source === "gmail" ? "<span>from email</span>" : ""}</div>
    </div>
    <div class="actions">
      ${i.url ? `<a class="icon-btn" href="${esc(i.url)}" target="_blank" rel="noopener" title="Open">${ICON.link}</a>` : ""}
      ${compact ? "" : `<button class="icon-btn" data-act="pin" title="Pin">${ICON.pin}</button>
      <button class="icon-btn" data-act="edit" title="Edit">${ICON.edit}</button>
      <button class="icon-btn" data-act="hide" title="Hide">${ICON.hide}</button>`}
    </div></li>`;
}

const itemCache = new Map();
function remember(items) { items.forEach((i) => itemCache.set(i.id, i)); return items; }

document.addEventListener("click", async (e) => {
  const btn = e.target.closest("[data-act]");
  if (!btn) return;
  const row = btn.closest("[data-id]");
  const id = row && Number(row.dataset.id);
  const act = btn.dataset.act;
  if (act === "toggle") {
    const wasDone = btn.classList.contains("done");
    btn.classList.toggle("done");
    await api(`/api/items/${id}`, { method: "PATCH", body: { status: wasDone ? "open" : "done" } });
    if (!wasDone) {
      row.style.opacity = ".4";
      toast("Marked done — calendar blocks removed", async () => {
        await api(`/api/items/${id}`, { method: "PATCH", body: { status: "open" } });
        refresh();
      });
    }
    setTimeout(refresh, 600);
  } else if (act === "hide") {
    await api(`/api/items/${id}`, { method: "PATCH", body: { status: "dismissed" } });
    toast("Hidden", async () => { await api(`/api/items/${id}`, { method: "PATCH", body: { status: "open" } }); refresh(); });
    refresh();
  } else if (act === "pin") {
    const it = itemCache.get(id);
    await api(`/api/items/${id}`, { method: "PATCH", body: { pinned: it && it.pinned ? 0 : 1 } });
    refresh();
  } else if (act === "edit") {
    openItemDialog(itemCache.get(id));
  } else if (act === "inbox-done") {
    await api(`/api/inbox/${id}`, { method: "PATCH", body: { done: true } });
    row.remove();
  } else if (act === "mail-archive") {
    await api(`/api/mail/${id}`, { method: "PATCH" });
    row.remove();
  } else if (act === "doc") {
    openDoc(Number(btn.dataset.doc));
  } else if (act === "topic-archive") {
    await api(`/api/drill/topics/${id}`, { method: "PATCH", body: { archived: true } });
    row.remove();
  } else if (act === "topic-drill") {
    const t = await api(`/api/drill/topics/${id}`);
    state.drillQueue = [t]; state.drillIdx = 0; state.revealed = false;
    renderDrillCard(); window.scrollTo({ top: 0, behavior: "smooth" });
  }
});

/* ---------- navigation ---------- */
function show(view) {
  state.view = view;
  $$(".view").forEach((v) => v.classList.toggle("active", v.id === `view-${view}`));
  $$("#tabs button").forEach((b) => b.classList.toggle("active", b.dataset.view === view));
  const titles = { today: "Command Center", deadlines: "Deadlines", drill: "Drill", brain: "Second Brain", system: "System" };
  $("#title").textContent = titles[view];
  if (location.hash.slice(1) !== view) history.replaceState(null, "", `#${view}`);
  try { localStorage.setItem("cc-view", view); } catch {}
  refresh();
}
$$("#tabs button").forEach((b) => b.addEventListener("click", () => show(b.dataset.view)));
window.addEventListener("hashchange", () => show(location.hash.slice(1) || "today"));

async function refresh() {
  try {
    if (state.view === "today") await loadToday();
    else if (state.view === "deadlines") await loadDeadlines();
    else if (state.view === "drill") await loadDrill();
    else if (state.view === "brain") await loadBrain();
    else if (state.view === "system") await loadSystem();
  } catch (err) { console.error(err); toast(`Error: ${err.message}`); }
}

/* ---------- courses ---------- */
async function loadCourses() {
  state.courses = await api("/api/courses");
  const opts = `<option value="">All / none</option>` + state.courses.map((c) => `<option value="${esc(c.code)}">${esc(c.code)}</option>`).join("");
  $$(".course-select").forEach((s) => { const v = s.value; s.innerHTML = opts; s.value = v; });
}

/* ---------- TODAY ---------- */
async function loadToday() {
  const t = await api("/api/today");
  const now = new Date();
  $("#greeting").textContent = `${t.greeting}, Jackson`;
  $("#date-line").textContent = now.toLocaleDateString([], { weekday: "long", month: "long", day: "numeric" });
  $("#last-sync").textContent = `synced ${ago(t.last_sync)}`;
  const overdue = t.overdue.length, today = t.due_today.length;
  $("#stats").innerHTML = `
    <div class="stat ${overdue ? "bad" : ""}"><b>${overdue}</b><span>overdue</span></div>
    <div class="stat ${today ? "warn" : ""}"><b>${today}</b><span>due today</span></div>
    <div class="stat"><b>${t.counts.week}</b><span>this week</span></div>
    <div class="stat"><b>${t.drill.due}</b><span>drill due</span></div>`;
  setBadge("deadlines", overdue + today);
  setBadge("drill", t.drill.due);

  remember([...t.next_up, ...t.at_risk]);
  $("#next-list").innerHTML = t.next_up.length ? t.next_up.map((i) => itemRow(i, { compact: true })).join("") : `<li class="empty">Nothing open. Nice.</li>`;
  $("#card-risk").hidden = !t.at_risk.length;
  $("#risk-list").innerHTML = t.at_risk.map((i) => itemRow(i, { compact: true })).join("");

  $("#agenda").innerHTML = t.agenda.length ? t.agenda.map(agendaSlot).join("") : `<div class="empty">Nothing on the calendar today.</div>`;

  $("#drill-mini").innerHTML = t.drill.total
    ? `<div>${t.drill.due ? `<b>${t.drill.due}</b> weak topic${t.drill.due > 1 ? "s" : ""} ready for review.` : "All caught up."} <span class="muted">${t.drill.reviews_7d} reviews this week.</span></div>`
    : `<div class="empty">Log topics you struggle with and they'll come back here before they bite you on an exam.</div>`;

  $("#card-mail").hidden = !t.mail.length;
  $("#mail-list").innerHTML = t.mail.map((m) => `<li class="row" data-id="${m.id}"><div class="body">
      <div class="title"><a href="${esc(m.url)}" target="_blank" rel="noopener">${esc(m.subject)}</a></div>
      <div class="from">${esc(m.sender.replace(/<.*>/, "").trim())} · ${ago(m.received_at)} ${m.course ? "· " + esc(m.course) : ""}</div>
      <div class="meta">${esc(m.snippet)}</div></div>
      <div class="actions"><button class="icon-btn" data-act="mail-archive" title="Dismiss">${ICON.hide}</button></div></li>`).join("");

  $("#inbox-list").innerHTML = t.inbox.map((n) => `<li class="row" data-id="${n.id}">
      <button class="check" data-act="inbox-done" aria-label="Done"></button>
      <div class="body"><div>${esc(n.body)}</div><div class="meta">${ago(n.created_at)}</div></div></li>`).join("");
}

function agendaSlot(a) {
  const now = new Date();
  const past = a.type !== "due" && d(a.end) < now;
  const when = a.all_day ? "all day" : fmtTime(a.start);
  let sub = "";
  if (a.type === "event") sub = [a.location, a.calendar].filter(Boolean).join(" · ");
  if (a.type === "block") sub = `Study block · ${fmtTime(a.start)}–${fmtTime(a.end)}${a.course ? " · " + a.course : ""}`;
  if (a.type === "due") sub = `Due${a.course ? " · " + a.course : ""}`;
  const title = a.type === "due" ? `⏰ ${a.title}` : a.title;
  return `<div class="slot ${a.type} ${past ? "past" : ""}"><time>${esc(when)}</time><span class="bar"${a.type === "block" && a.color ? ` style="background:${esc(a.color)}"` : ""}></span>
    <div class="what">${a.url ? `<a href="${esc(a.url)}" target="_blank" rel="noopener">${esc(title)}</a>` : esc(title)}<small>${esc(sub)}</small></div></div>`;
}

function setBadge(name, n) {
  const b = $(`#badge-${name}`);
  b.hidden = !n; b.textContent = n > 99 ? "99+" : n;
}

$("#capture-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const v = $("#capture-input").value.trim();
  if (!v) return;
  await api("/api/inbox", { method: "POST", body: { body: v } });
  $("#capture-input").value = "";
  loadToday();
});

/* ---------- DEADLINES ---------- */
async function loadDeadlines() {
  if (!state.courses.length) await loadCourses();
  $("#course-filter").innerHTML = [`<button data-course="" class="${!state.courseFilter ? "active" : ""}">All</button>`,
    ...state.courses.map((c) => `<button data-course="${esc(c.code)}" class="${state.courseFilter === c.code ? "active" : ""}">
      <span class="chip" style="background:none;padding:0"><span class="dot" style="background:${esc(c.color)}"></span>${esc(c.code)}</span>${c.open_items ? ` · ${c.open_items}` : ""}</button>`)].join("");
  const q = new URLSearchParams({ status: state.statusFilter });
  if (state.courseFilter) q.set("course", state.courseFilter);
  const items = remember(await api(`/api/items?${q}`));
  const groups = state.statusFilter === "open"
    ? [["overdue", "Overdue"], ["today", "Today"], ["tomorrow", "Tomorrow"], ["week", "This week"], ["later", "Later"], ["someday", "No due date"]]
    : [[null, state.statusFilter === "done" ? "Completed" : "Hidden"]];
  if (state.statusFilter === "open") items.sort((a, b) => (a.due_at || "9") < (b.due_at || "9") ? -1 : 1);
  $("#deadline-groups").innerHTML = groups.map(([key, label]) => {
    const rows = key ? items.filter((i) => i.bucket === key) : items;
    if (!rows.length) return "";
    return `<div class="group-h ${key || ""}">${label} · ${rows.length}</div><div class="card" style="padding:4px 16px"><ul class="list">${rows.map((i) => itemRow(i)).join("")}</ul></div>`;
  }).join("") || `<div class="card empty">Nothing here.</div>`;

  const week = await api("/api/week?days=7");
  $("#week").innerHTML = week.map((day) => {
    const dt = new Date(day.date + "T12:00:00");
    const items = day.agenda.filter((a) => a.type !== "event");
    return `<div style="margin:10px 0 4px;font-weight:650">${dt.toLocaleDateString([], { weekday: "long", month: "short", day: "numeric" })}</div>` +
      (items.length ? items.map(agendaSlot).join("") : `<div class="empty">No deadlines or study blocks.</div>`);
  }).join("");
}

$("#course-filter").addEventListener("click", (e) => {
  const b = e.target.closest("button[data-course]");
  if (!b) return;
  state.courseFilter = b.dataset.course;
  loadDeadlines();
});
$("#status-filter").addEventListener("click", (e) => {
  const b = e.target.closest("button[data-status]");
  if (!b) return;
  state.statusFilter = b.dataset.status;
  $$("#status-filter button").forEach((x) => x.classList.toggle("active", x === b));
  loadDeadlines();
});

function toLocalInput(iso) {
  if (!iso) return "";
  const x = new Date(iso);
  const pad = (n) => String(n).padStart(2, "0");
  return `${x.getFullYear()}-${pad(x.getMonth() + 1)}-${pad(x.getDate())}T${pad(x.getHours())}:${pad(x.getMinutes())}`;
}

async function openItemDialog(item) {
  if (!state.courses.length) await loadCourses();
  const f = $("#item-form");
  f.reset();
  $("#item-dialog-title").textContent = item ? "Edit deadline" : "Add deadline";
  f.id.value = item ? item.id : "";
  f.title.value = item ? item.title : "";
  f.course.value = item ? item.course || "" : state.courseFilter;
  f.kind.value = item ? item.kind || "assignment" : "assignment";
  f.due.value = item ? toLocalInput(item.due_at) : "";
  f.effort_hours.value = item ? item.effort_hours || "" : "";
  $("#item-dialog").showModal();
}
$("#add-item-btn").addEventListener("click", () => openItemDialog(null));
$("#item-dialog").addEventListener("close", async () => {
  if ($("#item-dialog").returnValue !== "save") return;
  const f = $("#item-form");
  // Send the local wall-clock time; the server interprets it in your configured timezone.
  const body = { title: f.title.value, course: f.course.value || null, kind: f.kind.value, due: f.due.value || null };
  if (f.effort_hours.value) body.effort_hours = Number(f.effort_hours.value);
  if (f.id.value) await api(`/api/items/${f.id.value}`, { method: "PATCH", body });
  else await api("/api/items", { method: "POST", body });
  toast("Saved — next sync will plan time for it");
  refresh();
});

/* ---------- DRILL ---------- */
async function loadDrill() {
  if (!state.courses.length) await loadCourses();
  if (!state.drillQueue.length || state.drillIdx >= state.drillQueue.length) {
    state.drillQueue = await api("/api/drill/due");
    state.drillIdx = 0; state.revealed = false;
  }
  renderDrillCard();
  const topics = await api("/api/drill/topics");
  $("#topic-count").textContent = topics.length;
  $("#topic-list").innerHTML = topics.length ? topics.map((t) => {
    const due = t.due_at && d(t.due_at) > new Date() ? `next ${relDue(t.due_at).text.replace(/ \d.*$/, "")}` : "due now";
    return `<li class="row" data-id="${t.id}"><div class="body"><div class="title">${esc(t.name)}</div>
      <div class="meta">${courseChip(t.course, colorFor(t.course))}<span>${due}</span><span>struggled ${t.struggle_count}×</span>
      <span>${t.card_count} card${t.card_count === 1 ? "" : "s"}</span>${t.exam_date ? `<span>exam ${d(t.exam_date).toLocaleDateString([], { month: "short", day: "numeric" })}</span>` : ""}
      ${t.source !== "manual" ? `<span>via ${esc(t.source)}</span>` : ""}</div></div>
      <div class="actions"><button class="btn secondary small" data-act="topic-drill">Drill</button>
      <button class="icon-btn" data-act="topic-archive" title="Mastered — archive">${ICON.hide}</button></div></li>`;
  }).join("") : `<li class="empty">No weak topics yet.</li>`;
}

const colorFor = (code) => (state.courses.find((c) => c.code === code) || {}).color;

function renderDrillCard() {
  const box = $("#drill-session");
  const q = state.drillQueue;
  if (!q.length || state.drillIdx >= q.length) {
    box.innerHTML = `<div class="flash" style="align-items:center;justify-content:center;text-align:center">
      <div style="font-size:40px">✅</div><div class="q">Nothing due right now</div>
      <div class="muted">Topics come back on a spaced-repetition schedule — sooner if you have an exam coming.</div></div>`;
    return;
  }
  const t = q[state.drillIdx];
  const card = (t.cards || [])[0];
  box.innerHTML = `<div class="flash">
    <div class="topic">${esc(t.course || "")} · ${state.drillIdx + 1} of ${q.length}</div>
    <div class="q">${esc(card ? card.question : t.name)}</div>
    ${card ? `<div class="muted" style="font-size:13px">Topic: ${esc(t.name)}</div>` : `<div class="notes">No practice problems yet. Work one from your notes or generate some — then grade how it went.</div>`}
    ${t.notes ? `<div class="notes">Last time: ${esc(t.notes)}</div>` : ""}
    ${state.revealed && card ? `<div class="a">${esc(card.answer || "(no answer saved)")}</div>` : ""}
    <div class="form-row">
      ${card && !state.revealed ? `<button class="btn secondary" id="reveal-btn">Show answer</button>` : ""}
      <button class="btn secondary" id="gen-btn">✨ Generate problems</button>
      <button class="btn secondary" id="add-card-btn">+ Card</button>
    </div>
    <div class="grades">
      <button class="g0" data-grade="0">Again<small>blanked</small></button>
      <button class="g1" data-grade="1">Hard<small>struggled</small></button>
      <button class="g2" data-grade="2">Good<small>got it</small></button>
      <button class="g3" data-grade="3">Easy<small>trivial</small></button>
    </div></div>`;
  const reveal = $("#reveal-btn");
  if (reveal) reveal.onclick = () => { state.revealed = true; renderDrillCard(); };
  $$("[data-grade]", box).forEach((b) => b.onclick = async () => {
    await api(`/api/drill/topics/${t.id}/review`, { method: "POST", body: { grade: Number(b.dataset.grade), card_id: card ? card.id : null } });
    state.drillIdx++; state.revealed = false;
    renderDrillCard();
    if (state.drillIdx >= q.length) loadDrill();
  });
  $("#gen-btn").onclick = async (e) => {
    e.target.disabled = true; e.target.textContent = "Generating…";
    try {
      const cards = await api(`/api/drill/topics/${t.id}/generate`, { method: "POST", body: { n: 3 } });
      t.cards = [...cards, ...(t.cards || [])];
      toast(`Added ${cards.length} problems`);
    } catch (err) { toast(`Local AI: ${err.message}`); }
    renderDrillCard();
  };
  $("#add-card-btn").onclick = async () => {
    const question = prompt("Question / problem:");
    if (!question) return;
    const answer = prompt("Answer (optional):") || "";
    const c = await api(`/api/drill/topics/${t.id}/cards`, { method: "POST", body: { question, answer } });
    t.cards = [c, ...(t.cards || [])];
    renderDrillCard();
  };
}

$("#topic-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const f = e.target;
  await api("/api/drill/topics", { method: "POST", body: { name: f.name.value, course: f.course.value || null, notes: f.notes.value || null, exam_date: f.exam_date.value || null } });
  f.reset();
  state.drillQueue = [];
  toast("Added — it's due now");
  loadDrill();
});

/* ---------- BRAIN ---------- */
async function loadBrain() {
  if (!state.courses.length) await loadCourses();
  const [recent, stats, ai] = await Promise.all([api("/api/notes/recent"), api("/api/notes/stats"), api("/api/ai/status")]);
  $("#doc-stats").textContent = `${stats.docs} files indexed`;
  $("#recent").innerHTML = recent.length ? recent.map((r) => `<div class="hit"><a href="#" class="t" data-act="doc" data-doc="${r.id}">${esc(r.title)}</a>
    <div class="p">${r.course ? esc(r.course) + " · " : ""}${esc(r.path)}</div></div>`).join("") : `<div class="empty">No notes indexed yet — add folders in config.toml.</div>`;
  $("#ai-status").innerHTML = ai.ok ? `<span class="pill-ok">●</span> ${esc(ai.model)}` : `<span class="pill-bad">●</span> local AI offline`;
}

const hl = (s) => esc(s).replace(/\[\[/g, "<mark>").replace(/\]\]/g, "</mark>");

async function doSearch() {
  const q = $("#search-input").value.trim();
  if (!q) return;
  const course = $("#search-course").value;
  const res = await api(`/api/search?${new URLSearchParams({ q, course })}`);
  renderHits(res);
}
function renderHits(res) {
  $("#results-card").hidden = false;
  $("#result-count").textContent = res.length;
  $("#results").innerHTML = res.length ? res.map((h) => `<div class="hit">
    <a href="#" class="t" data-act="doc" data-doc="${h.doc_id}">${esc(h.title)}</a>
    <div class="p">${h.course ? esc(h.course) + " · " : ""}${h.pages.length ? "p. " + h.pages.join(", ") + " · " : ""}${esc(h.path)}</div>
    ${h.snippets.map((s) => `<div class="s">${hl(s)}</div>`).join("")}</div>`).join("") : `<div class="empty">No matches.</div>`;
}
$("#search-form").addEventListener("submit", (e) => { e.preventDefault(); doSearch(); });
$("#search-btn").addEventListener("click", doSearch);
$("#ask-btn").addEventListener("click", async (e) => {
  const q = $("#search-input").value.trim();
  if (!q) return;
  e.target.disabled = true; e.target.textContent = "Thinking…";
  $("#answer-card").hidden = false;
  $("#answer").textContent = "…";
  $("#answer-sources").innerHTML = "";
  try {
    const r = await api("/api/ask", { method: "POST", body: { question: q, course: $("#search-course").value || null } });
    if (r.answer) {
      $("#answer").textContent = r.answer;
      $("#answer-sources").innerHTML = r.sources.map((s) => `<a href="#" data-act="doc" data-doc="${s.doc_id}">[${s.n}] ${esc(s.title)}${s.page ? " p." + s.page : ""}</a>`).join("");
    } else {
      $("#answer").textContent = `Local AI isn't reachable (${r.error}). Here's what your notes say:`;
      renderHits(r.hits || []);
    }
  } catch (err) { $("#answer").textContent = `Error: ${err.message}`; }
  e.target.disabled = false; e.target.textContent = "Ask my notes";
});

async function openDoc(id) {
  const doc = await api(`/api/docs/${id}`);
  $("#doc-title").textContent = doc.title;
  $("#doc-path").textContent = doc.path;
  $("#doc-text").textContent = doc.text || "(no text extracted — scanned PDF or image?)";
  $("#doc-open").href = `/api/docs/${id}/file`;
  $("#doc-dialog").showModal();
}
document.addEventListener("click", (e) => { if (e.target.closest('a[data-act="doc"]')) e.preventDefault(); }, true);

/* ---------- SYSTEM ---------- */
async function loadSystem() {
  const s = await api("/api/sync/status");
  $("#sync-table").innerHTML = `<tr><td>Last full sync</td><td>${ago(s.last_sync)}</td><td class="msg">Scheduled at ${esc(s.configured.times.join(", "))}</td></tr>` +
    (s.sources.length ? s.sources.map((r) => `<tr><td>${esc(r.source)}</td><td class="${r.ok ? "pill-ok" : "pill-bad"}">${r.ok ? "OK" : "Error"} · ${ago(r.finished_at)}</td><td class="msg">${esc(r.message)}</td></tr>`).join("") : `<tr><td colspan=3 class="msg">No syncs yet.</td></tr>`);
  const c = s.configured;
  const ai = await api("/api/ai/status");
  const ok = (v) => (v ? `<span class="pill-ok">connected</span>` : `<span class="pill-bad">not set up</span>`);
  $("#config-table").innerHTML = `
    <tr><td>Canvas</td><td>${ok(c.canvas)}${c.canvas_mode ? ` (${c.canvas_mode})` : ""}</td></tr>
    <tr><td>Google</td><td>${ok(c.google)}</td></tr>
    <tr><td>Notes</td><td class="msg">${c.notes_folders.map(esc).join("<br>") || "none"}</td></tr>
    <tr><td>Local AI</td><td>${ai.ok ? `<span class="pill-ok">${esc(ai.model)}</span>` : `<span class="pill-bad">${esc(ai.reason)}</span>`}</td></tr>`;
  await loadCourses();
  $("#course-list").innerHTML = state.courses.map((co) => `<li class="row"><div class="body"><div class="title">${courseChip(co.code, co.color)} ${esc(co.name || "")}</div>
    <div class="meta"><span>${co.open_items} open</span><span>${co.docs} notes</span><span>${co.topics} weak topics</span></div></div></li>`).join("") || `<li class="empty">Courses appear after the first Canvas sync.</li>`;
}

async function runSync(only) {
  const btn = $("#sync-btn");
  btn.querySelector("svg").classList.add("spin");
  await api("/api/sync", { method: "POST", body: only ? { only: [only] } : {} });
  toast("Sync started");
  const started = Date.now();
  const poll = setInterval(async () => {
    const s = await api("/api/sync/status");
    if ((s.last_sync && d(s.last_sync) >= new Date(started - 2000)) || Date.now() - started > 120000) {
      clearInterval(poll);
      btn.querySelector("svg").classList.remove("spin");
      state.courses = [];
      refresh();
    }
  }, 2500);
}
$("#sync-btn").addEventListener("click", () => runSync(""));
$$("[data-sync]").forEach((b) => b.addEventListener("click", () => runSync(b.dataset.sync)));

/* ---------- boot ---------- */
let saved = "today";
try { saved = localStorage.getItem("cc-view") || "today"; } catch {}
show(location.hash.slice(1) || saved);
loadCourses();
setInterval(() => { if (document.visibilityState === "visible" && state.view === "today") loadToday(); }, 5 * 60 * 1000);
document.addEventListener("visibilitychange", () => { if (document.visibilityState === "visible") refresh(); });
if ("serviceWorker" in navigator) navigator.serviceWorker.register("/sw.js").catch(() => {});
