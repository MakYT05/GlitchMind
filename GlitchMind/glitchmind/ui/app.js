/* GlitchMind UI. Весь текст из разговоров и от модели вставляется через textContent (h()), без innerHTML. */
(() => {
"use strict";

const TOKEN = (window.__TOKEN__ && window.__TOKEN__ !== "{{GM_TOKEN}}") ? window.__TOKEN__
  : new URLSearchParams(location.search).get("t") || "";
const $ = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => Array.from(r.querySelectorAll(s));

function h(tag, attrs, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === false || v == null) continue;
    if (k === "class") el.className = v;
    else if (k === "text") el.textContent = v;
    else if (k.startsWith("on") && typeof v === "function") el.addEventListener(k.slice(2), v);
    else if (k === "style" && typeof v === "object") Object.assign(el.style, v);
    else el.setAttribute(k, v === true ? "" : v);
  }
  for (const kid of kids.flat()) {
    if (kid == null || kid === false) continue;
    el.appendChild(kid.nodeType ? kid : document.createTextNode(String(kid)));
  }
  return el;
}

async function api(path, body) {
  const opt = { headers: { "X-Token": TOKEN } };
  if (body !== undefined) {
    opt.method = "POST";
    opt.headers["Content-Type"] = "application/json";
    opt.body = JSON.stringify(body);
  }
  const r = await fetch(path, opt);
  let j = {};
  try { j = await r.json(); } catch (_) { /* пустой ответ */ }
  if (!r.ok) throw new Error(j.error || r.statusText);
  return j;
}

const store = {
  get(k, d) { try { const v = localStorage.getItem("gm." + k); return v === null ? d : v; } catch (_) { return d; } },
  set(k, v) { try { localStorage.setItem("gm." + k, v); } catch (_) { /* приватный режим */ } },
};

const S = {
  listening: false, channels: { mic: true, loop: true }, active: {},
  meters: { pressure: 0, manip: 0, deception: 0, deception_label: "" },
  settings: {}, library: [], llm: { state: "idle" }, asr: "idle", level: 0, thinking: false,
  books: {}, demo: false, hasSkin: false,
};

/* ---------------- уведомления ---------------- */
function toast(msg, kind = "") {
  const t = h("div", { class: "toast " + kind, text: msg });
  $("#toasts").appendChild(t);
  setTimeout(() => t.remove(), kind === "err" ? 9000 : 4500);
}

async function copyText(text) {
  try { await navigator.clipboard.writeText(text); }
  catch (_) {
    const ta = h("textarea", { style: { position: "fixed", opacity: "0" } });
    ta.value = text; document.body.appendChild(ta); ta.select();
    try { document.execCommand("copy"); } catch (__) { /* нет доступа */ }
    ta.remove();
  }
  toast("Скопировано в буфер");
}

/* ---------------- часы и вкладки ---------------- */
setInterval(() => { $("#clock").textContent = new Date().toLocaleTimeString("ru-RU"); }, 1000);

function showTab(name) {
  $$(".tab-btn").forEach(b => b.classList.toggle("active", b.dataset.tab === name));
  $$(".tab").forEach(t => t.classList.toggle("active", t.id === "tab-" + name));
  store.set("tab", name);
  if (name === "knowledge") loadKb();
  if (name === "library") loadLibrary();
  if (name === "settings") loadDevices();
}
$$(".tab-btn").forEach(b => b.addEventListener("click", () => showTab(b.dataset.tab)));

/* ---------------- маскот ---------------- */
const FACE = { yl: "#f4ff4a", ink: "#0a0d10" };
const MOUTH_N = 9;

function faceSVG(state, mode) {
  const line = mode === "line";
  const NS = "http://www.w3.org/2000/svg";
  const g = document.createElementNS(NS, "g");
  const stroke = line ? "currentColor" : FACE.ink;
  const add = (tag, a) => { const e = document.createElementNS(NS, tag); for (const k in a) e.setAttribute(k, a[k]); g.appendChild(e); return e; };

  add("circle", { cx: 100, cy: 100, r: 86, fill: line ? "none" : FACE.yl, stroke: line ? "currentColor" : "#fff8a0", "stroke-width": line ? 3 : 4 });
  if (!line) add("circle", { cx: 100, cy: 100, r: 76, fill: "none", stroke: "#0a0d10", "stroke-opacity": .12, "stroke-width": 2 });

  // глаза-сканеры в «визоре»
  add("rect", { x: 30, y: 66, width: 140, height: 38, rx: 8, fill: line ? "none" : "#0a0d10", stroke: line ? "currentColor" : "none", "stroke-width": 2 });
  const eyeCol = line ? "currentColor" : "#19e3ff";
  const eye = (cx) => {
    if (state === "think") {
      add("circle", { cx, cy: 85, r: 4, fill: eyeCol, class: "think-dot" });
      add("circle", { cx: cx + (cx < 100 ? 14 : -14), cy: 85, r: 3, fill: eyeCol, class: "think-dot", style: "animation-delay:.25s" });
    } else if (state === "alert") {
      add("path", { d: `M${cx - 14} ${cx < 100 ? 76 : 92} L${cx + 14} ${cx < 100 ? 92 : 76}`, stroke: "#ff2e88", "stroke-width": 5, "stroke-linecap": "square" });
    } else if (state === "warn") {
      add("circle", { cx, cy: 85, r: cx < 100 ? 11 : 6, fill: "none", stroke: "#ffb400", "stroke-width": 3 });
      add("circle", { cx, cy: 85, r: 3.5, fill: "#ffb400", class: "pupil" });
    } else if (state === "listen") {
      add("ellipse", { cx, cy: 85, rx: 14, ry: 9, fill: "none", stroke: eyeCol, "stroke-width": 2 });
      add("circle", { cx, cy: 85, r: 5, fill: eyeCol, class: "pupil" });
    } else {
      add("ellipse", { cx, cy: 85, rx: 11, ry: 7, fill: "none", stroke: eyeCol, "stroke-width": 2 });
      add("circle", { cx, cy: 85, r: 4, fill: eyeCol });
    }
  };
  eye(66); eye(134);

  // рот — эквалайзер: реагирует на громкость
  const w = 9, gap = 5, total = MOUTH_N * w + (MOUTH_N - 1) * gap;
  for (let i = 0; i < MOUTH_N; i++) {
    add("rect", { x: 100 - total / 2 + i * (w + gap), y: 136, width: w, height: 6, rx: 1.5,
      fill: line ? "currentColor" : FACE.ink, "data-mb": i, class: "mb" });
  }
  // «щёки»-метки
  add("path", { d: "M44 120 h14 M142 120 h14", stroke, "stroke-width": 3, "stroke-linecap": "round", "stroke-opacity": line ? 1 : .5 });
  return g;
}

let mascotState = "";
const MASCOT = {
  idle: ["😌", "Жду команды. Нажми «СЛУШАТЬ»."],
  listen: ["🎧", "Слушаю эфир…"],
  think: ["🧠", "Думаю над ответом…"],
  alert: ["😬", "Давление растёт. Не спеши отвечать."],
  warn: ["🧐", "Есть несостыковки — уточни, а не обвиняй."],
};

function setMascot(state) {
  if (state === mascotState) return;
  mascotState = state;
  const m = $("#mascot");
  m.dataset.state = state;
  $$("svg.layer", m).forEach(svg => {
    svg.replaceChildren(faceSVG(state, svg.classList.contains("main") || svg.classList.contains("slice") ? "fill" : "line"));
  });
  $("#mascot-emoji").textContent = MASCOT[state][0];
  $("#mascot-cap").textContent = MASCOT[state][1];
  $("#mascot-state").textContent = { idle: "ГОТОВ", listen: "ЭФИР", think: "АНАЛИЗ", alert: "ТРЕВОГА", warn: "ПРОВЕРЬ" }[state];
}

function pickMascot() {
  const m = S.meters;
  if (S.thinking) return "think";
  if (m.deception >= 50) return "warn";
  if (m.pressure >= 60 || m.manip >= 60) return "alert";
  return S.listening ? "listen" : "idle";
}
const refreshMascot = () => setMascot(pickMascot());

// анимация рта (громкость → высота столбиков)
let lvl = 0, t0 = performance.now();
function mouthLoop(now) {
  const t = (now - t0) / 1000;
  lvl += (S.level - lvl) * 0.35;
  const jag = mascotState === "alert";
  $$("rect.mb").forEach(r => {
    const i = +r.dataset.mb;
    const mid = (MOUTH_N - 1) / 2;
    const env = 1 - Math.abs(i - mid) / (mid + 1);
    const noise = 0.5 + 0.5 * Math.sin(t * 9 + i * 1.7) * Math.cos(t * 5 + i);
    let hgt = 5 + Math.sin(t * 1.4 + i) * 1 + lvl * 46 * (0.35 + env * 0.65) * (0.55 + noise * 0.45);
    if (jag) hgt = 6 + (i % 2 ? 14 : 4);
    r.setAttribute("height", hgt.toFixed(1));
    r.setAttribute("y", (139 - hgt / 2).toFixed(1));
  });
  requestAnimationFrame(mouthLoop);
}

function applySkin(has) {
  S.hasSkin = has;
  const img = $("#skin");
  if (has) { img.src = "/skin?t=" + encodeURIComponent(TOKEN); img.classList.remove("hidden"); }
  else img.classList.add("hidden");
  $("#mascot").classList.toggle("has-skin", has);
}

/* ---------------- чипы статуса ---------------- */
function chip(id, s, title) {
  const c = $("#chip-" + id);
  c.dataset.s = s;
  if (title) c.title = title;
}
function renderChips() {
  const a = S.asr;
  chip("asr", a === "ready" ? "ok" : a === "loading" ? "busy" : a === "error" ? "err" : a === "busy" ? "warn" : "off",
    { ready: "Модель распознавания готова", loading: "Загрузка модели…", error: "Ошибка модели", busy: "Не успевает", idle: "Модель загрузится при первом запуске" }[a]);
  const l = S.llm.state;
  chip("llm", l === "ready" ? "ok" : l === "thinking" ? "busy" : l === "error" ? "err" : l === "no_model" ? "warn" : "off",
    S.llm.message || ({ off: "ИИ-модель выключена — работают правила", idle: "ИИ-модель ещё не вызывалась", no_model: "Модель не выбрана" }[l] || ""));
  chip("mic", !S.listening ? "off" : S.active.mic ? "ok" : "err", S.channels.mic ? "Микрофон" : "Микрофон выключен");
  chip("loop", !S.listening ? "off" : S.active.loop ? "ok" : "err", S.channels.loop ? "Звук из наушников" : "Звук из наушников выключен");
}

/* ---------------- слушание ---------------- */
function renderListening() {
  const b = $("#btn-listen");
  b.classList.toggle("on", S.listening);
  $("#listen-label").textContent = S.listening ? "■ ОСТАНОВИТЬ" : "▶ СЛУШАТЬ";
  $("#ch-mic").checked = !!S.channels.mic;
  $("#ch-loop").checked = !!S.channels.loop;
  renderChips();
  refreshMascot();
}

$("#btn-listen").addEventListener("click", async () => {
  try {
    const r = await api("/api/listen", { on: !S.listening });
    S.listening = r.listening;
  } catch (e) { toast(e.message, "err"); }
  renderListening();
});
$("#ch-mic").addEventListener("change", e => api("/api/channels", { mic: e.target.checked }).catch(x => toast(x.message, "err")));
$("#ch-loop").addEventListener("change", e => api("/api/channels", { loop: e.target.checked }).catch(x => toast(x.message, "err")));

$("#btn-demo").addEventListener("click", () => api("/api/demo", { on: !S.demo }).catch(e => toast(e.message, "err")));
function renderDemo() { $("#btn-demo").textContent = S.demo ? "■ СТОП ДЕМО" : "▶ ДЕМО-РАЗГОВОР"; }

/* ---------------- шкалы ---------------- */
function buildGauges() {
  $$(".gauge .seg").forEach(seg => { for (let i = 0; i < 20; i++) seg.appendChild(h("span")); });
}
function renderMeters() {
  const m = S.meters;
  for (const key of ["pressure", "manip", "deception"]) {
    const g = $(`.gauge[data-key=${key}]`);
    const v = Math.max(0, Math.min(100, m[key] || 0));
    $("b", g).textContent = v;
    const n = Math.round(v / 5);
    $$(".seg span", g).forEach((s, i) => {
      s.className = i < n ? "on" + (i >= 14 ? " hi" : i >= 8 ? " mid" : "") : "";
    });
  }
  $("#dec-label").textContent = m.deception > 0 ? `${m.deception_label} · это не детектор лжи, а повод уточнить` : "это не детектор лжи, а повод уточнить";
  refreshMascot();
}

/* ---------------- транскрипт ---------------- */
const bookColor = id => (S.books[id] && S.books[id].color) || "";
const bookShort = id => (S.books[id] ? S.books[id].author.split(" ").slice(-1)[0] : "");

function tagEl(hit) {
  const grp = hit.group || "dec";
  const book = hit.book ? " · " + bookShort(hit.book) : "";
  return h("span", { class: "tg " + grp, title: hit.snippet ? "«" + hit.snippet + "»" : "" }, `${hit.emoji || "•"} ${hit.label}${book}`);
}

function uttEl(u) {
  const hits = (u.hits || []).filter(x => u.speaker === "me" ? x.group === "coach" : x.group !== "coach");
  const cues = u.cues || [];
  const hot = hits.some(x => x.weight >= 0.6 && (x.group === "manip" || x.group === "pressure" || x.group === "aggr"));
  const tags = [];
  hits.forEach(x => tags.push(tagEl(x)));
  cues.forEach(c => tags.push(h("span", { class: "tg dec", title: c.detail || "" }, "🔎 " + c.label + (c.detail ? " — " + c.detail : ""))));
  if (u.safety) tags.push(h("span", { class: "tg safety" }, "⚠ слова о вреде для себя"));
  const when = new Date((u.t || Date.now() / 1000) * 1000).toLocaleTimeString("ru-RU", { hour: "2-digit", minute: "2-digit", second: "2-digit" });
  const src = { mic: "🎙", loop: "🎧", manual: "⌨", demo: "▶" }[u.source] || "";
  return h("div", { class: `utt ${u.speaker}${hot ? " hot" : ""}`, "data-id": u.id },
    h("div", { class: "utt-h" }, h("b", { text: u.speaker === "me" ? "Я" : "Собеседник" }), h("span", { text: src + " " + when })),
    h("div", { class: "utt-t", text: u.text }),
    tags.length ? h("div", { class: "tags" }, tags) : null);
}

function addUtt(u, quiet) {
  const feed = $("#feed");
  const empty = $("#feed-empty");
  if (empty) empty.remove();
  const near = feed.scrollHeight - feed.scrollTop - feed.clientHeight < 120;
  feed.appendChild(uttEl(u));
  if (!quiet && near) feed.scrollTop = feed.scrollHeight;
}

function resetFeed() {
  $("#feed").replaceChildren(h("div", { class: "empty", id: "feed-empty" },
    h("div", { class: "empty-big", text: "⌁ ТИШИНА В ЭФИРЕ" }),
    "Нажми ", h("b", { text: "▶ СЛУШАТЬ" }), ", и фразы появятся здесь.", h("br"),
    "Нет микрофона под рукой? Запусти ", h("b", { text: "ДЕМО-РАЗГОВОР" }), " или добавь реплику вручную ниже."));
  $("#advice").replaceChildren(h("div", { class: "empty", text: "Подсказки появятся после первой реплики собеседника." }));
}

$("#manual").addEventListener("submit", async e => {
  e.preventDefault();
  const inp = $("#manual-text");
  const text = inp.value.trim();
  if (!text) return;
  inp.value = "";
  try { await api("/api/manual", { speaker: $("#manual-who").value, text }); } catch (x) { toast(x.message, "err"); }
});
$("#btn-clear").addEventListener("click", () => api("/api/clear", {}).catch(e => toast(e.message, "err")));
$("#btn-advice").addEventListener("click", () => api("/api/advice", { note: $("#adv-note").value }).catch(e => toast(e.message, "err")));
$("#adv-note").addEventListener("keydown", e => { if (e.key === "Enter") $("#btn-advice").click(); });

/* ---------------- подсказки ---------------- */
function styleClass(s) {
  s = (s || "").toLowerCase();
  if (s.startsWith("мягк")) return "st-soft";
  if (s.startsWith("твёрд") || s.startsWith("тверд")) return "st-firm";
  if (s.startsWith("такт")) return "st-tact";
  if (s.startsWith("границ")) return "st-bound";
  return "st-other";
}

function bookBadge(id) {
  if (!id || !S.books[id]) return null;
  return h("span", { class: "book", style: { color: bookColor(id) }, text: `${S.books[id].emoji} ${bookShort(id)}` });
}

function renderAdvice(a, root) {
  const out = [];
  if (!a) { root.replaceChildren(h("div", { class: "empty", text: "Пока нечего показать." })); return; }
  if (a.source === "llm_error") { return; }
  if (a.safety) out.push(h("div", { class: "banner", text: a.safety }));
  out.push(h("div", { class: "adv-src" },
    h("span", { text: a.source === "llm" ? "ГЛУБОКИЙ РАЗБОР" : "БЫСТРЫЕ ПОДСКАЗКИ" }),
    h("span", { class: "pill", text: a.source === "llm" ? "🧠 " + (a.model || "ИИ") : "⚙ правила" })));
  if (a.situation) out.push(h("div", { class: "sit", text: a.situation }));
  if (a.their_goal) out.push(h("div", { class: "goal", text: "🎯 Чего добивается: " + a.their_goal }));
  if (a.dynamics && a.dynamics.length) {
    out.push(h("div", { class: "sec", text: "Приёмы и динамика" }));
    a.dynamics.forEach(d => out.push(h("div", { class: "dyn" }, `${d.emoji || "🧩"} `, h("b", { text: d.label }), " ", bookBadge(d.book), d.why ? " — " + d.why : "")));
  }
  if (a.replies && a.replies.length) {
    out.push(h("div", { class: "sec", text: "Варианты ответа (клик — копировать)" }));
    a.replies.forEach(r => out.push(h("div", { class: "rep " + styleClass(r.style), onclick: () => copyText(r.text) },
      h("div", { class: "rep-h" }, h("span", { class: "st", text: r.style || "" }), h("span", { class: "tech", text: r.technique || "" }), bookBadge(r.book)),
      h("div", { class: "rep-t", text: r.text }),
      r.why ? h("div", { class: "rep-w", text: r.why }) : null,
      h("span", { class: "copy", text: "COPY ⧉" }))));
  }
  const list = (title, items, cls, click) => {
    if (!items || !items.length) return;
    out.push(h("div", { class: "sec", text: title }));
    out.push(h("ul", { class: "lst " + cls }, items.map(t => h("li", { text: t, onclick: click ? () => copyText(t) : null }))));
  };
  list("Что спросить", a.questions, "q", true);
  list("Чего лучше не делать", a.avoid, "av");
  list("Твоя речь", a.self_coaching, "co");
  if (a.deception && a.deception.length) {
    out.push(h("div", { class: "sec", text: "Что стоит уточнить" }));
    out.push(h("ul", { class: "lst de" }, a.deception.map(t => h("li", { text: t }))));
  }
  if (a.sources && a.sources.length) {
    out.push(h("div", { class: "sec", text: "Опора на твои книги" }));
    a.sources.forEach(s => out.push(h("div", { class: "src" }, h("b", { text: (s.book || s.doc) + ": " }), s.snippet + "…")));
  }
  if (a.unparsed) out.push(h("div", { class: "disclaimer", text: "Модель ответила не в нужном формате — показан сырой текст. Попробуй модель покрупнее или температуру пониже." }));
  root.replaceChildren(...out);
  root.scrollTop = 0;
}

/* ---------------- ситуация ---------------- */
$("#btn-sit").addEventListener("click", async () => {
  const text = $("#sit-text").value.trim();
  if (!text) { toast("Опиши ситуацию или вставь диалог", "warn"); return; }
  $("#sit-advice").replaceChildren(h("div", { class: "empty", text: "Разбираю…" }));
  try { await api("/api/situation", { text, note: $("#sit-note").value }); } catch (e) { toast(e.message, "err"); }
});

/* ---------------- знания ---------------- */
let kbLoaded = false;
async function loadKb() {
  if (kbLoaded) return;
  try {
    const { books } = await api("/api/kb");
    const root = $("#kb");
    root.replaceChildren(...books.map(b => h("div", { class: "kb-card", style: { "--bc": b.color } },
      h("div", { class: "au", text: `${b.emoji} ${b.author}` }),
      h("h3", { text: `«${b.title}»` }),
      h("p", { text: b.core }),
      b.principles && b.principles.length ? h("details", {}, h("summary", { text: `Ключевые идеи (${b.principles.length})` }),
        h("ul", { class: "lst" }, b.principles.map(p => h("li", { text: p, style: { paddingLeft: "6px" } })))) : null,
      b.techniques.map(t => h("details", {}, h("summary", { text: t.name }),
        h("div", { class: "w", text: t.what }), h("div", { class: "h", text: "Как: " + t.how }), h("div", { class: "e", text: t.example }))),
      b.deception && b.deception.length ? h("details", {}, h("summary", { text: "Признаки несоответствий (по книге)" }),
        h("ul", { class: "lst de" }, b.deception.map(d => h("li", { text: d })))) : null)));
    kbLoaded = true;
  } catch (e) { toast(e.message, "err"); }
}

/* ---------------- библиотека ---------------- */
function renderLibrary(status) {
  S.library = status;
  const root = $("#lib-status");
  root.replaceChildren(...status.map(b => h("div", { class: "lib-row", style: { "--bc": b.color } },
    h("div", { class: "nm" }, `${b.emoji} ${b.author}`, h("small", { text: b.title })),
    h("div", {},
      b.notes ? h("div", { class: "ok-note", text: `✔ встроенный конспект: ${b.notes} приёмов` }) : null,
      b.files.length
        ? b.files.map(f => h("div", { class: "lib-file" },
            "📄 ", h("span", { text: `${f.name} · ${f.chunks} фрагм.` }),
            h("select", { title: "К какой книге относится файл", onchange: e => api("/api/library/book", { id: f.id, book: e.target.value }).catch(x => toast(x.message, "err")) },
              h("option", { value: "" }, "— без привязки —"),
              Object.values(S.books).map(k => h("option", { value: k.id, selected: k.id === f.book }, k.author))),
            h("button", { class: "x", title: "Удалить из библиотеки", onclick: async () => {
              if (!confirm(`Удалить «${f.name}» из библиотеки?`)) return;
              await api("/api/library/delete", { id: f.id }).catch(x => toast(x.message, "err"));
            } }, "✕")))
        : h("div", { class: "none-note", text: b.id ? "Своей копии нет — загрузи файл, и поиск будет подтягивать фрагменты." : "" })))));
}

async function loadLibrary() {
  try { renderLibrary((await api("/api/library")).status); } catch (e) { toast(e.message, "err"); }
}

async function uploadFiles(files) {
  for (const f of files) {
    toast(`Индексирую «${f.name}»…`);
    try {
      const r = await fetch("/api/library/upload?name=" + encodeURIComponent(f.name), { method: "POST", headers: { "X-Token": TOKEN }, body: f });
      const j = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(j.error || r.statusText);
      toast(`✔ ${f.name}: ${j.doc.chunks} фрагментов`);
    } catch (e) { toast(`${f.name}: ${e.message}`, "err"); }
  }
  loadLibrary();
}
const drop = $("#drop");
["dragenter", "dragover"].forEach(ev => drop.addEventListener(ev, e => { e.preventDefault(); drop.classList.add("over"); }));
["dragleave", "drop"].forEach(ev => drop.addEventListener(ev, e => { e.preventDefault(); drop.classList.remove("over"); }));
drop.addEventListener("drop", e => uploadFiles(Array.from(e.dataTransfer.files)));
$("#file-any").addEventListener("change", e => { uploadFiles(Array.from(e.target.files)); e.target.value = ""; });
$("#btn-lib-open").addEventListener("click", () => api("/api/library/open", {}).catch(e => toast(e.message, "err")));
$("#btn-lib-rescan").addEventListener("click", async () => {
  try { const r = await api("/api/library/rescan", {}); toast(`Добавлено: ${r.added.length}` + (r.errors.length ? `, ошибок: ${r.errors.length}` : ""), r.errors.length ? "warn" : ""); loadLibrary(); }
  catch (e) { toast(e.message, "err"); }
});
async function libSearch() {
  const q = $("#lib-q").value.trim();
  if (!q) return;
  try {
    const { results } = await api("/api/library/search?q=" + encodeURIComponent(q));
    $("#lib-results").replaceChildren(...(results.length
      ? results.map(r => h("div", { class: "src" }, h("b", { text: `${r.doc} (${r.score}): ` }), r.text.slice(0, 420) + (r.text.length > 420 ? "…" : "")))
      : [h("div", { class: "none-note", text: "Ничего не найдено. Загрузи книги или переформулируй запрос." })]));
  } catch (e) { toast(e.message, "err"); }
}
$("#btn-lib-search").addEventListener("click", libSearch);
$("#lib-q").addEventListener("keydown", e => { if (e.key === "Enter") libSearch(); });

/* ---------------- настройки ---------------- */
function fillSettings(s) {
  S.settings = s;
  $$("[data-set]").forEach(el => {
    const k = el.dataset.set;
    if (k === "llm_api_key") { el.value = ""; el.placeholder = s.llm_api_key_set ? "•••••• сохранён (пусто — не менять)" : ""; return; }
    if (!(k in s)) return;
    if (el.type === "checkbox") el.checked = !!s[k]; else el.value = s[k];
  });
  $("#vad-out").textContent = (+s.vad_sensitivity).toFixed(2);
  $("#temp-out").textContent = (+s.llm_temperature).toFixed(1);
  document.body.classList.toggle("no-fx", !s.fx_glitch);
  S.channels = { mic: !!s.use_mic, loop: !!s.use_loop };
}

$$("[data-set]").forEach(el => {
  const send = async () => {
    const k = el.dataset.set;
    let v = el.type === "checkbox" ? el.checked : el.type === "range" || el.type === "number" ? Number(el.value) : el.value;
    if (k === "llm_api_key" && v === "") return;
    try { const r = await api("/api/settings", { [k]: v }); fillSettings(r.settings); renderListening(); }
    catch (e) { toast(e.message, "err"); }
  };
  if (el.type === "range") el.addEventListener("input", () => { const o = el.parentElement.querySelector("output"); if (o) o.textContent = el.value; });
  el.addEventListener("change", send);
});

async function loadDevices() {
  try {
    const d = await api("/api/devices");
    const fill = (sel, names, cur) => {
      sel.replaceChildren(h("option", { value: "" }, "по умолчанию"), ...names.map(n => h("option", { value: n, selected: n === cur }, n)));
      if (cur && !names.includes(cur)) sel.appendChild(h("option", { value: cur, selected: true }, cur + " (нет в списке)"));
    };
    fill($("#dev-mic"), d.mics, S.settings.mic_device);
    fill($("#dev-loop"), d.outputs, S.settings.loop_device);
    $("#dev-hint").textContent = d.loopback_supported
      ? `Захват: ${d.backend}. По умолчанию звук берётся с текущего устройства вывода — это и есть наушники, если они выбраны в Windows.`
      : "Перехват звука из наушников работает только в Windows (WASAPI loopback). Здесь доступен лишь микрофон.";
  } catch (e) { toast(e.message, "err"); }
}
$("#btn-dev-refresh").addEventListener("click", loadDevices);

$("#btn-llm-test").addEventListener("click", async () => {
  $("#llm-msg").textContent = "Проверяю…";
  try {
    const r = await api("/api/llm/test", {});
    $("#llm-msg").textContent = (r.ok ? "✔ " : "✖ ") + r.message;
    if (r.models && r.models.length) $("#llm-models").replaceChildren(...r.models.map(m => h("option", { value: m })));
  } catch (e) { $("#llm-msg").textContent = "✖ " + e.message; }
});
$("#btn-llm-models").addEventListener("click", async () => {
  try {
    const r = await api("/api/llm/models");
    if (!r.ok) { $("#llm-msg").textContent = "✖ " + r.message; return; }
    $("#llm-models").replaceChildren(...r.models.map(m => h("option", { value: m })));
    $("#llm-msg").textContent = r.models.length ? "Модели на сервере: " + r.models.join(", ") : "Сервер доступен, но моделей нет (ollama pull qwen2.5:7b).";
    if (r.models.length && !$("#llm-model").value) { $("#llm-model").value = r.models[0]; $("#llm-model").dispatchEvent(new Event("change")); }
  } catch (e) { $("#llm-msg").textContent = "✖ " + e.message; }
});

/* ---------------- завершение ---------------- */
$("#btn-quit").addEventListener("click", async () => {
  if (!confirm("Закрыть GlitchMind? Прослушка будет остановлена.")) return;
  try { await api("/api/quit", {}); } catch (_) { /* сервер уже закрывается */ }
  document.body.replaceChildren(h("div", { class: "empty", style: { marginTop: "30vh" } }, h("div", { class: "empty-big", text: "СЕАНС ЗАВЕРШЁН" }), "Можно закрыть это окно."));
  setTimeout(() => window.close(), 300);
});

/* ---------------- согласие ---------------- */
function showConsent() {
  const box = $("#consent"), ok = $("#consent-ok"), btn = $("#consent-btn");
  box.classList.remove("hidden");
  ok.addEventListener("change", () => { btn.disabled = !ok.checked; });
  btn.addEventListener("click", async () => {
    try { await api("/api/settings", { consent_ack: true }); } catch (_) { /* продолжим и так */ }
    box.classList.add("hidden");
  });
}

/* ---------------- события от ядра ---------------- */
function connectEvents() {
  const es = new EventSource("/events?t=" + encodeURIComponent(TOKEN));
  es.onopen = () => chip("link", "ok", "Связь с ядром установлена");
  es.onerror = () => chip("link", "err", "Связь потеряна, переподключаюсь…");
  const on = (name, fn) => es.addEventListener(name, ev => { try { fn(JSON.parse(ev.data)); } catch (e) { console.error(name, e); } });

  on("state", d => { S.listening = d.listening; S.channels = d.channels; S.active = d.active || {}; renderListening(); });
  on("status", d => {
    if (d.kind === "asr") { S.asr = d.state; if (d.state === "loading" || d.state === "error" || d.state === "busy") toast(d.message, d.state === "error" ? "err" : "warn"); }
    if (d.kind === "llm") {
      S.llm = d; S.thinking = d.state === "thinking";
      $("#adv-spin").classList.toggle("hidden", !S.thinking);
      $("#sit-spin").classList.toggle("hidden", !S.thinking);
      if (d.state === "error") toast("ИИ-модель: " + d.message, "err");
    }
    renderChips(); refreshMascot();
  });
  on("error", d => toast(d.message, "err"));
  on("levels", d => {
    const m = Math.max(d.mic || 0, d.loop || 0);
    S.level = m;
    $("#lv-mic").style.width = Math.round((d.mic || 0) * 100) + "%";
    $("#lv-loop").style.width = Math.round((d.loop || 0) * 100) + "%";
  });
  on("utterance", u => addUtt(u));
  on("meters", m => { S.meters = m; renderMeters(); });
  on("advice", a => renderAdvice(a, $("#advice")));
  on("situation", a => {
    if (a.source === "llm_error") { toast("Глубокий разбор не удался: " + a.message + " Показаны быстрые подсказки.", "warn"); return; }
    renderAdvice(a, $("#sit-advice"));
  });
  on("cleared", () => { resetFeed(); });
  on("library", d => { if ($("#tab-library").classList.contains("active")) renderLibrary(d.status); });
  on("demo", d => { S.demo = d.on; renderDemo(); });
}

/* ---------------- старт ---------------- */
async function init() {
  buildGauges();
  setMascot("idle");
  requestAnimationFrame(mouthLoop);
  try {
    const kb = await api("/api/kb");
    kb.books.forEach(b => { S.books[b.id] = b; });
    const st = await api("/api/state");
    $("#ver").textContent = "v" + st.version;
    S.listening = st.listening; S.channels = st.channels; S.active = st.active || {};
    S.meters = st.meters; S.llm = st.llm; S.demo = st.demo;
    fillSettings(st.settings);
    applySkin(st.has_skin);
    if (st.data_dir) $("#data-dir").textContent = st.data_dir;
    (st.transcript || []).forEach(u => addUtt(u, true));
    const feed = $("#feed"); feed.scrollTop = feed.scrollHeight;
    if (st.advice) renderAdvice(st.advice, $("#advice"));
    if (st.situation && st.situation.source !== "llm_error") renderAdvice(st.situation, $("#sit-advice"));
    renderMeters(); renderListening(); renderDemo();
    if (!st.settings.consent_ack) showConsent();
  } catch (e) {
    toast("Нет связи с ядром: " + e.message, "err");
  }
  connectEvents();
  showTab(store.get("tab", "live"));
}
init();
})();
