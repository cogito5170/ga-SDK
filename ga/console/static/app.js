// GA Console (CMD-CON3): the static app. Plain DOM, no build, no framework, no request leaves 127.0.0.1.
// Data comes from GA API (ga/console/server.py) with the one-time token the server put in the opened URL; the page
// re-renders the part of the screen an /api/events message touches. Every data string goes in as text, never as HTML.

// ---- the token: read from the address once, kept for this tab, removed from the address bar ------------------------
const TOKEN_KEY = "ga-console-token";
const TOKEN = takeToken();

function takeToken() {
  const u = new URL(location.href);
  let t = u.searchParams.get("t");
  if (t) {
    try { sessionStorage.setItem(TOKEN_KEY, t); } catch (e) { /* private window: the token lives in memory only */ }
    u.searchParams.delete("t");
    const clean = u.pathname + (u.search === "?" ? "" : u.search) + u.hash;
    history.replaceState(null, "", clean);
  } else {
    try { t = sessionStorage.getItem(TOKEN_KEY) || ""; } catch (e) { t = ""; }
  }
  return t;
}

// ---- DOM: elements from data, text only ---------------------------------------------------------------------------
function h(tag, attrs, ...kids) {
  const n = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k === "class") n.className = v;
    else if (k === "on") for (const [ev, fn] of Object.entries(v)) n.addEventListener(ev, fn);
    else if (k === "prop") Object.assign(n, v);
    else n.setAttribute(k, v === true ? "" : String(v));
  }
  return add(n, kids);
}

function add(n, kids) {
  for (const k of kids.flat(Infinity)) {
    if (k === null || k === undefined || k === false) continue;
    n.append(k instanceof Node ? k : String(k));
  }
  return n;
}

const mark = (kind) => h("span", { class: "mark " + kind, "aria-hidden": "true" });
const state = (kind, word) => h("span", { class: "state" }, mark(kind), word);
const mono = (s) => h("span", { class: "mono" }, s);
const sha7 = (s) => (s ? String(s).slice(0, 7) : "—");

function section(id, title, sub, ...body) {
  return h("section", { "aria-labelledby": "h-" + id },
    h("h2", { id: "h-" + id }, title, sub ? h("span", { class: "sub" }, sub) : null), ...body);
}

function more(key, total, shown, what) {
  if (total <= shown) return null;
  return h("button", { type: "button", class: "more", "data-key": "more-" + key,
    on: { click: () => { S.limit[key] = shown + 20; changed("more"); } } }, `더 보기 ${what} (${total - shown}개 남음)`);
}

// ---- numbers and times, as DESIGN.md writes them ------------------------------------------------------------------
const NF = new Intl.NumberFormat("en-US");
const num = (v) => (v === null || v === undefined || v === "" || Number.isNaN(Number(v)) ? "—" : NF.format(Number(v)));
const pad = (x) => String(x).padStart(2, "0");

function when(iso, secs) {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "—";
  const hm = pad(d.getHours()) + ":" + pad(d.getMinutes()) + (secs ? ":" + pad(d.getSeconds()) : "");
  const day = (x) => new Date(x.getFullYear(), x.getMonth(), x.getDate()).getTime();
  const diff = Math.round((day(new Date()) - day(d)) / 86400000);
  if (diff === 0) return hm;
  if (diff === 1) return "어제 " + hm;
  return pad(d.getMonth() + 1) + "-" + pad(d.getDate()) + " " + hm;
}

function ago(iso) {
  const d = new Date(iso);
  if (!iso || Number.isNaN(d.getTime())) return null;
  const s = Math.max(0, Math.round((Date.now() - d.getTime()) / 1000));
  if (s < 60) return s + "초 전";
  if (s < 3600) return Math.floor(s / 60) + "분 전";
  if (s < 86400) return Math.floor(s / 3600) + "시간 전";
  return when(iso);
}

// ---- the store ----------------------------------------------------------------------------------------------------
const S = {
  state: null, work: null, branches: null, tokens: null, mail: null, dec: null, decQ: "",
  logs: {}, sel: null, turns: [], newMail: new Set(),
  err: null, downSince: null, denied: false, lastId: 0,
  limit: {}, pending: {}, said: {}, confirm: null,
  ask: { q: "", mode: "ask", plan: null, busy: false, said: null }, run: null, runBuf: {}, past: loadPast(),
};

function loadPast() {
  try { return JSON.parse(sessionStorage.getItem("ga-console-past") || "[]").slice(0, 20); } catch (e) { return []; }
}
function savePast() {
  try { sessionStorage.setItem("ga-console-past", JSON.stringify(S.past.slice(0, 20))); } catch (e) { /* fine */ }
}

// ---- the API ------------------------------------------------------------------------------------------------------
class ApiError extends Error {
  constructor(status, body) { super(`HTTP ${status}`); this.status = status; this.body = body; }
}

async function api(path, body) {
  const init = { headers: { "X-GA-Token": TOKEN }, cache: "no-store" };
  if (body !== undefined) {
    init.method = "POST";
    init.headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(body);
  }
  const r = await fetch(path, init);
  let data = null;
  try { data = await r.json(); } catch (e) { data = null; }
  if (!r.ok) throw new ApiError(r.status, data);
  return data;
}

const LOADERS = {
  state: () => api("/api/state"),
  work: () => api("/api/work"),
  branches: () => api("/api/branches"),
  tokens: () => api("/api/tokens"),
  mail: () => api("/api/mail?limit=20"),
  dec: () => api("/api/decisions?q=" + encodeURIComponent(S.decQ)),
};

async function load(key) {
  const q = S.decQ;
  try {
    const d = await LOADERS[key]();
    if (key === "dec") {
      if (q !== S.decQ) return;  // an older search answered late
      S.decFor = q;
    }
    S[key] = d;
    S.err = null;
  } catch (e) {
    S.err = { key, status: e.status || 0, text: e.message || String(e) };
    if (e.status === 403) S.denied = true;
    banner();
  }
  changed(key);
  changed("err");
}

// ---- live: one EventSource; reconnect with the last id ------------------------------------------------------------
// The stream opens once the first reads have answered (STREAM_AFTER_MS after load): the page shows fetched data first,
// then follows the stream. EventSource resends the last id as Last-Event-ID when it reconnects by itself; after a
// refusal (a dead server, a new token) the page reopens it with ?after=<last id>, slower each time.
const TYPES = ["state", "work", "mail", "service", "log", "act_turn", "bridge"];
const STREAM_AFTER_MS = 1500;
let retry = 1000;
let started = false;

function stream() {  // once: on the timer, or sooner when the user starts something whose answer comes by the stream
  if (started || !TOKEN) return;
  started = true;
  connect();
}

function connect() {
  const q = new URLSearchParams({ t: TOKEN });
  if (S.lastId) q.set("after", String(S.lastId));
  const es = new EventSource("/api/events?" + q.toString());
  es.onopen = () => {
    const was = S.downSince;
    S.downSince = null;
    retry = 1000;
    document.documentElement.dataset.live = "open";
    live();
    if (was) for (const k of route().need) load(k);  // what no event carries (branches, tokens) is read again
  };
  es.onerror = () => {
    if (!S.downSince) S.downSince = Date.now();
    document.documentElement.dataset.live = "down";
    live();
    if (es.readyState === EventSource.CLOSED) {
      es.close();
      setTimeout(connect, retry);
      retry = Math.min(retry * 2, 15000);
    }
  };
  for (const t of TYPES) {
    es.addEventListener(t, (ev) => {
      const n = Number(ev.lastEventId);
      if (n) S.lastId = n;
      let d;
      try { d = JSON.parse(ev.data); } catch (e) { return; }
      ON[t](d);
    });
  }
}

const ON = {
  state(d) { S.state = d; changed("state"); },
  work(d) { S.work = d; changed("work"); },
  mail(d) {
    if (S.mail) {
      const old = new Set(S.mail.map((m) => m.path));
      for (const m of d) if (!old.has(m.path)) S.newMail.add(m.path);
    }
    S.mail = d;
    changed("mail");
  },
  service(d) {
    if (!S.state) return;
    const l = S.state.services;
    const i = l.findIndex((s) => s.name === d.name);
    if (i >= 0) l[i] = d; else l.push(d);
    S.state.counts.services_running = l.filter((s) => s.state === "running").length;
    if (d.name === "bridge") S.state.bridge.running = d.state === "starting" || d.state === "running";
    changed("state");
  },
  log(d) {
    if (d.run_id) {
      if (S.run && S.run.id === d.run_id) runLine(S.run, d);
      else (S.runBuf[d.run_id] = S.runBuf[d.run_id] || []).push(d);
      changed("run");
      return;
    }
    const L = S.logs[d.service];
    if (L && d.n > L.next) {
      L.lines.push(d);
      L.next = d.n;
      if (L.lines.length > 400) L.lines.splice(0, L.lines.length - 400);
      changed("log");
    }
  },
  act_turn(d) {
    S.turns.unshift({ ...d, seen: new Date().toISOString() });
    S.turns.length = Math.min(S.turns.length, 20);
    changed("turns");
  },
  bridge(d) {
    if (!S.state) return;
    Object.assign(S.state.bridge, d);
    changed("state");
  },
};

function live() {
  document.body.classList.toggle("stale", Boolean(S.downSince));
  banner();
  changed("stale");
}

function banner() {
  const b = document.getElementById("banner");
  const t = document.getElementById("banner-text");
  let say = "";
  if (S.denied) say = "열쇠가 맞지 않아요. 터미널에서 ga console 을 다시 열고, 그 주소로 들어오세요.";
  else if (S.downSince) {
    const s = Math.round((Date.now() - S.downSince) / 1000);
    say = `실시간 연결이 끊겼어요. 화면은 ${s}초 전 값입니다. 다시 잇는 중 …`;
  }
  t.textContent = say;
  b.hidden = !say;
}


// ---- rendering: a screen is parts; an update re-renders only the parts that read what changed ----------------------
let current = null;

function changed(key) {
  nav();
  if (!current) return;
  const r = ROUTES[current];
  for (const p of r.parts) {
    if (!p.deps.includes(key)) continue;
    const old = document.querySelector(`[data-part="${p.id}"]`);
    if (!old) continue;
    const focus = document.activeElement && old.contains(document.activeElement)
      ? document.activeElement.getAttribute("data-key") : null;
    const fresh = part(p);
    old.replaceWith(fresh);
    if (focus) {
      const f = fresh.querySelector(`[data-key="${CSS.escape(focus)}"]`);
      if (f) f.focus();
    }
  }
}

function part(p) {
  const n = p.render() || h("div");
  n.setAttribute("data-part", p.id);
  return n;
}

function render() {
  const r = route();
  current = r.name;
  const view = document.getElementById("view");
  const mast = r.parts.find((p) => p.id === r.name + "-mast");
  const main = h("main", { id: "main" });
  const groups = {};
  for (const p of r.parts) {
    if (p === mast) continue;
    const n = part(p);
    if (p.pair) (groups[p.pair] = groups[p.pair] || h("div", { class: "two" })).append(n);
    else main.append(n);
    if (p.pair && groups[p.pair].parentNode !== main) main.append(groups[p.pair]);
  }
  view.replaceChildren(part(mast), main);
  document.getElementById("foot").textContent = r.foot;
  document.title = r.label + " · GA Console";
  nav();
}

function nav() {
  for (const a of document.querySelectorAll(".nav a")) {
    if (a.dataset.route === current) a.setAttribute("aria-current", "page");
    else a.removeAttribute("aria-current");
  }
  const c = S.state && S.state.counts;
  document.getElementById("n-work").textContent = c ? String(c.work_open) : "";
  document.getElementById("n-svc").textContent = S.state ? `${c.services_running}/${S.state.services.length}` : "";
}

const have = (r) => r.need.every((k) => S[k] !== null && S[k] !== undefined) && (r.name !== "decisions" || S.decFor === S.decQ);

// the masthead: caption, the protagonist, the hot rule, one plain line; loading and error are masts too
function mast(r, cap, h1, lede) {
  const capN = h("p", { class: "cap" }, cap);
  if (S.downSince) add(capN, [" · ", h("span", { class: "wait" }, `${Math.round((Date.now() - S.downSince) / 1000)}초 전 값`)]);
  return h("div", { class: "mast" }, capN, h("h1", null, h1), h("span", { class: "hot", "aria-hidden": "true" }),
    h("p", { class: "lede" }, lede));
}

function guarded(r, fn) {
  return () => {
    if (have(r)) return fn();
    if (S.err) {
      const denied = S.err.status === 403;
      return h("div", { class: "mast" }, h("p", { class: "cap" }, r.label),
        h("h1", null, mark("fail"), denied ? "열쇠가 맞지 않음" : "서버에 닿지 않음"),
        h("span", { class: "hot", "aria-hidden": "true" }),
        h("p", { class: "lede" }, "터미널에서 ga console 을 다시 여세요."),
        h("details", null, h("summary", null, "자세히"), h("pre", null, `${S.err.key}: ${S.err.text}`)));
    }
    const cap = h("p", { class: "cap", hidden: true }, r.label + " · 불러오는 중 …");
    setTimeout(() => { cap.hidden = false; }, 300);
    return h("div", { class: "mast" }, cap, h("h1", null, r.label), h("span", { class: "hot", "aria-hidden": "true" }),
      h("p", { class: "lede" }, " "));
  };
}

const body = (r, fn) => () => (have(r) ? fn() : null);

// ---- 지금 --------------------------------------------------------------------------------------------------------
const NOW = {
  mast() {
    const st = S.state;
    const b = st.bridge || {};
    const has = st.services.some((s) => s.name === "bridge");
    const seen = ago(b.last_seen);
    const now = new Date(st.now);
    const cap = ["지금 · ", h("b", null, `${now.getFullYear()}-${pad(now.getMonth() + 1)}-${pad(now.getDate())} ${when(st.now)}`)];
    if (!has) {
      return mast(ROUTES.now, cap, [mark("off"), "브리지 없음"],
        ["설정에 브리지가 없어요. ", h("span", { class: "sub" }, "브리지 = 이 Mac 의 agy 와 허브를 잇는 다리.")]);
    }
    return mast(ROUTES.now, cap, b.running ? [mark("live"), "브리지 살아 있음"] : [mark("off"), "브리지 멈춤"],
      [seen ? `마지막 소식 ${seen}. ` : "아직 브리지 소식이 없어요. ",
        b.running ? "" : "서비스 화면에서 켤 수 있어요. ",
        h("span", { class: "sub" }, "브리지 = 이 Mac 의 agy 와 허브를 잇는 다리.",
          b.config_path ? [" 설정 ", h("code", null, b.config_path)] : null)]);
  },
  figs() {
    const c = S.state.counts;
    return section("count", "한눈에", null, h("dl", { class: "figs" },
      h("div", null, h("dt", null, "열린 작업"), h("dd", null, num(c.work_open))),
      h("div", null, h("dt", null, "안 읽은 편지"), h("dd", null, num(c.mail_unread))),
      h("div", null, h("dt", null, "도는 서비스"), h("dd", null, num(c.services_running),
        h("small", null, ` / ${S.state.services.length}`)))));
  },
  run() {
    const running = (S.work || []).filter((w) => w.status === "running");
    const kids = [];
    if (running.length) {
      kids.push(h("ul", { class: "items" }, running.map((w) => h("li", null,
        h("div", { class: "row" }, h("span", { class: "id" }, w.id), state("live", "도는 중")),
        h("p", { class: "say" }, w.title || w.summary_ko || "")))));
    }
    if (S.turns.length) {
      kids.push(h("table", { class: "tbl" }, h("caption", null, `턴마다 쓴 토큰 (이 화면을 연 뒤 ${S.turns.length}턴, 최근 먼저)`),
        h("thead", null, h("tr", null, ["작업", "턴", "시각"].map((x) => h("th", null, x)),
          ["입력", "출력", "캐시", "합계"].map((x) => h("th", { class: "num" }, x)))),
        h("tbody", null, S.turns.map((t) => h("tr", t.error ? { class: "fail" } : null,
          h("td", { "data-label": "작업" }, h("span", { class: "id" }, t.item || "—")),
          h("td", { "data-label": "턴" }, num(t.turn)),
          h("td", { "data-label": "시각" }, when(t.seen, true)),
          h("td", { class: "num", "data-label": "입력" }, num(t.input)),
          h("td", { class: "num", "data-label": "출력" }, num(t.output)),
          h("td", { class: "num", "data-label": "캐시" }, num(t.cache_read)),
          h("td", { class: "num", "data-label": "합계" },
            t.input === null || t.input === undefined ? "—" : num((t.input || 0) + (t.output || 0))))))));
    } else {
      kids.push(h("p", { class: "note" }, running.length ? "이 화면을 연 뒤 새 턴은 아직 없어요. 턴이 끝나면 여기에 한 줄씩 쌓입니다."
        : "지금 돌고 있는 일이 없어요. 브리지가 지시를 받으면 여기에 보입니다."));
    }
    return section("run", "돌고 있는 일", running.length ? `agv · ${running.map((w) => w.id).join(" · ")}` : null, kids);
  },
  mail() {
    const l = (S.mail || []).slice(0, 5);
    if (!l.length) return section("mail", "우편함", null, h("p", { class: "note" }, "편지가 없어요. 누가 ga mail send 로 보내면 여기에 보입니다."));
    return section("mail", "우편함", `최근 ${l.length}통`, h("ul", { class: "items" }, l.map((m) => h("li", null,
      h("div", { class: "row" }, h("span", { class: "id" }, m.schema || m.form || "—"),
        m.valid ? state("ok", "형식 맞음") : m.schema ? state("fail", "형식 틀림") : state("wait", "머리 없음"),
        S.newMail.has(m.path) ? state("live", "새 편지") : null),
      h("p", { class: "meta" }, h("span", null, h("b", null, m.from || "—"), " → ", m.to || "—"),
        h("span", null, when(m.at)), h("span", { class: "mono" }, m.form || ""))))));
  },
};

// ---- 작업 --------------------------------------------------------------------------------------------------------
const OPEN = ["sent", "running", "reported"];
const STOP = { draft: -1, sent: 0, running: 1, reported: 2, integrated: 3, sent_back: 3, failed: 3 };
const WORD = { draft: "초안", sent: "보냄", running: "도는 중", reported: "보고됨", integrated: "통합됨", sent_back: "돌려보냄", failed: "실패" };

function track(w) {
  const i = STOP[w.status] ?? 0;
  const back = w.status === "sent_back" || w.status === "failed";
  const stops = [["보냄", w.sent_at], ["도는 중", null], ["보고됨", w.reported_at], ["통합됨", null]];
  return h("ol", { class: "track", "aria-label": "진행" }, stops.map(([word, at], k) => {
    const t = at && k <= i ? `${word} ${when(at)}` : word;
    if (w.status === "draft" && k === 0) return h("li", null, mark("wait"), "초안");
    if (k === 3 && back) return h("li", { class: "back" }, mark("fail"), WORD[w.status]);
    if (k < i) return h("li", { class: "done" }, t);
    if (k === i) {
      const m = w.status === "running" ? "live" : w.status === "sent" ? "wait" : "ok";
      return h("li", { class: w.status === "integrated" ? "done" : "now" }, w.status === "integrated" ? null : mark(m), t);
    }
    return h("li", null, word);
  }));
}

const bds = (w, word = "결정 ") => (w.bd && w.bd.length ? h("span", null, word,
  w.bd.map((b, k) => [k ? " " : "", h("a", { href: "#/decisions?q=" + encodeURIComponent(b) }, b)])) : null);

const WORKS = {
  mast() {
    const l = S.work;
    const open = l.filter((w) => OPEN.includes(w.status));
    const need = l.find((w) => w.status === "sent_back" || w.status === "failed") || l.find((w) => w.status === "reported");
    const cap = ["작업 · ", h("b", null, `열린 것 ${open.length}`), ` · 모두 ${l.length}`];
    const term = h("span", { class: "sub" }, "통합 = 공용 브랜치에 합침.");
    if (need && need.status === "reported") {
      return mast(ROUTES.work, cap, `${need.id} 보고됨`, ["보고가 왔어요. 확인하고 통합하면 끝납니다. ", term]);
    }
    if (need) {
      return mast(ROUTES.work, cap, `${need.id} ${WORD[need.status]}`,
        [need.status === "failed" ? "받는 쪽이 실패로 보고했어요. 보고를 읽고 다시 보낼지 정하세요. "
          : "다시 보냈어요. 고친 보고가 오면 여기서 통합할 수 있어요. ", term]);
    }
    if (!open.length) return mast(ROUTES.work, cap, "열린 작업 없음", "ga send 로 지시를 보내면 여기에 보입니다.");
    return mast(ROUTES.work, cap, `열린 작업 ${open.length}`, ["보낸 일이 돌고 있어요. 보고가 오면 여기서 먼저 알려 드려요. ", term]);
  },
  open() {
    const l = S.work.filter((w) => w.status !== "integrated")
      .sort((a, b) => String(b.sent_at || "").localeCompare(String(a.sent_at || "")));
    const n = S.limit.work || 20;
    if (!l.length) return section("open", "열린 작업", null, h("p", { class: "note" }, "열린 작업이 없어요."));
    return section("open", "열린 작업", "보낸 시각 순 · 최근 먼저",
      h("ol", { class: "items" }, l.slice(0, n).map((w) => h("li", w.status === "sent_back" || w.status === "failed" ? { class: "fail" } : null,
        h("div", { class: "row" }, h("span", { class: "id" }, w.id), h("h3", null, w.title || "—")),
        w.summary_ko ? h("p", { class: "say" }, w.summary_ko) : null,
        track(w),
        h("p", { class: "meta" }, h("span", null, "받는 쪽 ", h("b", null, w.to || "—")), h("span", null, w.kind || ""),
          h("span", null, w.tokens === null || w.tokens === undefined ? "—" : `${num(w.tokens)} 토큰`), bds(w),
          w.branch ? h("span", { class: "mono" }, w.branch) : null)))),
      more("work", l.length, n, "작업"));
  },
  done() {
    const l = S.work.filter((w) => w.status === "integrated");
    const n = S.limit.done || 20;
    if (!l.length) return section("done", "끝난 작업", null, h("p", { class: "note" }, "아직 통합된 작업이 없어요."));
    return section("done", "끝난 작업", `${l.length}개`, h("table", { class: "tbl" },
      h("thead", null, h("tr", null, ["작업", "상태", "보낸 때", "보고"].map((x) => h("th", null, x)), h("th", { class: "num" }, "토큰"), h("th", null, "결정"))),
      h("tbody", null, l.slice(0, n).map((w) => h("tr", null,
        h("td", { "data-label": "작업" }, h("span", { class: "id" }, w.id), " ", w.title || ""),
        h("td", { "data-label": "상태" }, state("ok", "통합됨")),
        h("td", { "data-label": "보낸 때" }, when(w.sent_at)),
        h("td", { "data-label": "보고" }, when(w.reported_at)),
        h("td", { class: "num", "data-label": "토큰" }, num(w.tokens)),
        h("td", { "data-label": "결정" }, bds(w, "") || "—"))))), more("done", l.length, n, "끝난 작업"));
  },
};

// ---- 브랜치 ------------------------------------------------------------------------------------------------------
function integrationHead(repo) {
  const b = (S.branches || []).find((x) => x.repo === repo.name && x.branch === repo.integration_branch);
  return b ? b : repo.branch === repo.integration_branch ? { head: repo.head, subject: repo.head_subject, at: null } : null;
}

const BRANCHES = {
  mast() {
    const repos = S.state.repos;
    const topic = S.branches.filter((b) => b.label_ko !== "통합 브랜치");
    const cap = ["브랜치 · ", h("b", null, `저장소 ${repos.length}`), ` · 안 합친 것 ${topic.filter((b) => !b.merged_into_integration).length}`];
    const first = repos.find((r) => integrationHead(r)) || repos[0];
    if (!first) return mast(ROUTES.branches, cap, "저장소 없음", "ga console init 의 설정에 저장소를 적으면 여기에 보입니다.");
    const ih = integrationHead(first);
    if (!ih) return mast(ROUTES.branches, cap, "공용 머리 모름", `${first.name} 을 찾을 수 없어요. 설정의 경로를 확인하세요.`);
    return mast(ROUTES.branches, cap, ["공용 머리 ", mono(sha7(ih.head))],
      [`${first.name} 의 공용 브랜치(${first.integration_branch})가 지금 가리키는 커밋입니다`,
        ih.at ? `, ${ago(ih.at)}` : "", `. ${ih.subject || ""} `, h("span", { class: "sub" }, "머리 = 브랜치의 가장 최근 커밋.")]);
  },
  repos() {
    return section("repos", "저장소마다 공용 머리", null, h("ul", { class: "items" }, S.state.repos.map((r) => {
      const ih = integrationHead(r);
      const st = r.dirty === null || r.dirty === undefined ? state("off", "찾을 수 없음") : r.dirty ? state("wait", "고친 파일 있음") : state("ok", "깨끗함");
      return h("li", null, h("div", { class: "row" }, h("h3", null, r.name), st),
        h("p", { class: "say" }, ih ? [mono(sha7(ih.head)), " ", ih.subject || ""] : "—"),
        h("p", { class: "meta" }, h("span", null, "공용 ", h("b", { class: "mono" }, r.integration_branch || "—")),
          h("span", null, "지금 ", h("b", { class: "mono" }, r.branch || "—")),
          h("span", null, `앞섬 ${num(r.ahead)} · 뒤짐 ${num(r.behind)}`), h("span", { class: "mono" }, r.path || "")));
    })));
  },
  table() {
    const l = S.branches.filter((b) => b.label_ko !== "통합 브랜치");
    const n = S.limit.branches || 20;
    if (!l.length) return section("br", "작업 브랜치", null, h("p", { class: "note" }, "공용 브랜치 말고는 브랜치가 없어요."));
    return section("br", "작업 브랜치", "claude/* · agv/*", h("table", { class: "tbl" },
      h("thead", null, h("tr", null, ["브랜치", "무엇", "합침"].map((x) => h("th", null, x)),
        ["앞섬", "뒤짐"].map((x) => h("th", { class: "num" }, x)), ["머리", "언제"].map((x) => h("th", null, x)))),
      h("tbody", null, l.slice(0, n).map((b) => h("tr", null,
        h("td", { "data-label": "브랜치" }, h("span", { class: "sub" }, b.repo + " "), mono(b.branch)),
        h("td", { "data-label": "무엇" }, b.subject || "—"),
        h("td", { "data-label": "합침" }, b.merged_into_integration ? state("ok", "합침") : state("wait", "안 합침")),
        h("td", { class: "num", "data-label": "앞섬" }, num(b.ahead)),
        h("td", { class: "num", "data-label": "뒤짐" }, num(b.behind)),
        h("td", { "data-label": "머리" }, mono(sha7(b.head))),
        h("td", { "data-label": "언제" }, when(b.at)))))),
    more("branches", l.length, n, "브랜치"),
    h("p", { class: "note" }, "합침 = 이 브랜치의 머리가 공용 브랜치 안에 들어 있음. 앞섬 = 공용에 없는 커밋 수, 뒤짐 = 이 브랜치에 없는 공용 커밋 수."));
  },
};

// ---- 서비스 ------------------------------------------------------------------------------------------------------
const disp = (name) => (name === "bridge" ? "브리지" : name);
const alive = (s) => s.state === "running" || s.state === "starting";
const SVC_STATE = { running: ["live", "도는 중"], starting: ["live", "켜는 중"], stopped: ["off", "멈춤"], failed: ["fail", "실패"] };
const HEALTH = { ok: ["ok", "응답 좋음"], down: ["off", "응답 없음"], unknown: ["wait", "확인 중"] };

function selected() {
  const l = S.state.services;
  if (S.sel && l.some((s) => s.name === S.sel)) return S.sel;
  const s = l.find((x) => x.state === "failed") || l.find(alive) || l[0];
  return s ? s.name : null;
}

async function svcAct(name, verb) {
  stream();
  const key = `${verb}-${name}`;
  S.confirm = null;
  S.pending[name] = verb;
  S.said[name] = null;
  changed("svc");
  const path = name === "bridge" ? `/api/bridge/${verb}` : `/api/services/${encodeURIComponent(name)}/${verb}`;
  try {
    if (verb === "restart") {
      ON.service(await api(path.replace(/restart$/, "stop"), {}));
      ON.service(await api(path.replace(/restart$/, "start"), {}));
    } else {
      ON.service(await api(path, {}));
    }
    const s = S.state.services.find((x) => x.name === name);
    const w = (SVC_STATE[s && s.state] || ["", s ? s.state : "모름"])[1];
    S.said[name] = { fail: s && s.state === "failed",
      text: `${disp(name)}: ${{ start: "켰어요", stop: "멈췄어요", restart: "다시 켰어요" }[verb]}. 지금 상태는 ${w}.` };
  } catch (e) {
    S.said[name] = { fail: true, text: `${disp(name)}: 하지 못했어요 (${e.message}). 터미널의 ga console 이 살아 있는지 보세요.` };
  }
  delete S.pending[name];
  changed("svc");
  return key;
}

async function loadLog(name) {
  try {
    const d = await api(`/api/services/${encodeURIComponent(name)}/logs?after=0`);
    S.logs[name] = { lines: d.lines || [], next: d.next || 0 };
  } catch (e) {
    S.logs[name] = { lines: [], next: 0, err: e.message };
  }
  changed("log");
}

const SERVICES = {
  mast() {
    const l = S.state.services;
    const r = l.filter((s) => s.state === "running").length;
    const failed = l.filter((s) => s.state === "failed").map((s) => disp(s.name));
    const cap = ["서비스 · ", h("b", null, "이 Mac 의 프로세스"), " 와 브리지"];
    if (!l.length) return mast(ROUTES.services, cap, "서비스 없음", "ga console init 의 설정에 서비스를 적으면 여기에 보입니다.");
    return mast(ROUTES.services, cap, [String(r), h("span", { class: "unit" }, `/ ${l.length} 도는 중`)],
      failed.length ? `${failed.join(", ")} 가 실패로 멈췄습니다. 아래 기록의 마지막 줄이 이유예요.`
        : r === l.length ? "모두 돌고 있어요." : "멈춘 서비스는 켜기 버튼으로 켤 수 있어요. 멈추기는 한 번 더 확인합니다.");
  },
  list() {
    const sel = selected();
    return section("svc", "켜고 끄기", null, h("ul", { class: "items" }, S.state.services.map((s) => {
      const [mk, word] = SVC_STATE[s.state] || ["wait", s.state];
      const hl = alive(s) ? HEALTH[s.health] || HEALTH.unknown : null;
      const d = disp(s.name);
      const pend = S.pending[s.name];
      const btn = (verb, label, extra) => h("button", { type: "button", "data-key": `${verb}-${s.name}`, disabled: Boolean(pend), ...extra,
        on: { click: () => (verb === "start" ? svcAct(s.name, "start") : (S.confirm = `${verb}-${s.name}`, changed("svc"))) } },
        pend === verb ? `${label} … 기다리는 중` : label);
      const acts = alive(s) ? [btn("stop", `멈추기 ${d}`), btn("restart", `다시 켜기 ${d}`)] : [btn("start", `켜기 ${d}`)];
      acts.push(h("button", { type: "button", "data-key": `log-${s.name}`, "aria-pressed": String(sel === s.name),
        on: { click: () => { S.sel = s.name; if (!S.logs[s.name]) loadLog(s.name); changed("svc"); changed("log"); } } }, `기록 보기 ${d}`));
      const conf = S.confirm && S.confirm.endsWith("-" + s.name) ? S.confirm.split("-")[0] : null;
      const said = S.said[s.name];
      const br = s.name === "bridge" && S.state.bridge ? ago(S.state.bridge.last_seen) : null;
      return h("li", s.state === "failed" ? { class: "fail" } : null,
        h("div", { class: "row" }, h("h3", null, d), state(mk, word), hl ? state(hl[0], hl[1]) : null),
        h("p", { class: "meta" }, h("span", null, "포트 ", s.port ? h("b", null, String(s.port)) : "—"),
          alive(s) && s.started_at ? h("span", null, `${when(s.started_at)} 부터`) : null,
          s.pid ? h("span", null, `pid ${s.pid}`) : null, br ? h("span", null, `마지막 소식 ${br}`) : null),
        h("div", { class: "actions" }, acts),
        conf ? h("div", { class: "actions", role: "group", "aria-label": `${d} 확인` },
          h("p", { class: "say" }, `${d} 를 ${conf === "stop" ? "멈출" : "다시 켤"}까요? 돌고 있는 일이 끊길 수 있어요.`),
          h("button", { type: "button", "data-key": `yes-${s.name}`, on: { click: () => svcAct(s.name, conf) } },
            `${conf === "stop" ? "멈추기" : "다시 켜기"} ${d} 확인`),
          h("button", { type: "button", "data-key": `no-${s.name}`, on: { click: () => { S.confirm = null; changed("svc"); } } }, `그대로 두기 ${d}`)) : null,
        h("p", { class: "said" + (said && said.fail ? " fail" : ""), role: "status" }, said ? said.text : ""));
    })));
  },
  log() {
    const name = selected();
    if (!name) return null;
    if (!S.logs[name]) { loadLog(name); return section("log", `${disp(name)} 기록`, "불러오는 중 …"); }
    const L = S.logs[name];
    const n = S.limit.log || 40;
    const l = L.lines.slice(-n);
    const s = S.state.services.find((x) => x.name === name);
    const hint = s && s.state === "failed" ? `${disp(name)} 가 멈췄어요. 마지막 줄을 읽고 고친 뒤 켜기 ${disp(name)} 를 눌러 보세요.`
      : s && !alive(s) ? `${disp(name)} 는 꺼져 있어요. 켜면 새 줄이 여기에 쌓입니다.` : "새 줄은 생기는 대로 아래에 붙습니다.";
    return section("log", `${disp(name)} 기록`, `마지막 ${l.length}줄 · stderr 는 굵게`,
      L.err ? h("p", { class: "note" }, `기록을 읽지 못했어요: ${L.err}`) : null,
      l.length ? h("ol", { class: "log", "aria-label": `${disp(name)} 기록` }, l.map((x) => h("li", x.stream === "stderr" ? { class: "err" } : null,
        h("span", { class: "ln" }, String(x.n)), h("span", { class: "at" }, when(x.at, true).replace(/^.* /, "")), h("span", { class: "tx" }, x.text))))
        : h("p", { class: "note" }, "아직 기록이 없어요."),
      L.lines.length > n ? h("button", { type: "button", class: "more", "data-key": "more-log",
        on: { click: () => { S.limit.log = n + 40; changed("log"); } } }, `더 보기 기록 (${L.lines.length - n}줄 남음)`) : null,
      h("p", { class: "note" }, hint));
  },
};

// ---- 토큰 --------------------------------------------------------------------------------------------------------
const TOKENS = {
  mast() {
    const t = S.tokens.today || {};
    const d = new Date();
    const cap = ["토큰 · ", h("b", null, "오늘"), ` · ${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`];
    const term = h("span", { class: "sub" }, "토큰 = 모델이 읽고 쓴 글자 조각. 많을수록 비쌉니다.");
    if (!t.total) return mast(ROUTES.tokens, cap, "오늘 0 토큰", ["agv 가 돌면 장부에 적힌 숫자가 여기에 보입니다. ", term]);
    return mast(ROUTES.tokens, cap, [num(t.total), h("span", { class: "unit" }, "토큰")], [`agv ${num(t.agy_turns)}턴. `, term]);
  },
  today() {
    const t = S.tokens.today || {};
    const day = new Date().toDateString();
    const costs = (S.tokens.rows || []).filter((r) => r.at && new Date(r.at).toDateString() === day && r.cost_usd !== null && r.cost_usd !== undefined);
    const cost = costs.length ? "$" + costs.reduce((a, r) => a + Number(r.cost_usd), 0).toFixed(2) : "—";
    return section("today", "오늘 나눠 보기", null, h("dl", { class: "figs" },
      [["agv 턴", num(t.agy_turns)], ["입력", num(t.input)], ["출력", num(t.output)], ["대략 비용", cost]]
        .map(([k, v]) => h("div", null, h("dt", null, k), h("dd", null, v)))));
  },
  per() {
    const sum = {};
    for (const r of S.tokens.rows || []) sum[r.id] = (sum[r.id] || 0) + (Number(r.total) || 0);
    const l = Object.entries(sum).sort((a, b) => b[1] - a[1]).slice(0, 20);
    if (!l.length) return section("per", "작업마다", null, h("p", { class: "note" }, "장부가 비어 있어요."));
    const top = l[0][1] || 1;
    return section("per", "작업마다", "장부 전체 · 합계 순", h("ul", { class: "bars" }, l.map(([id, v]) => {
      const i = h("i");
      i.style.width = Math.round((v / top) * 100) + "%";
      return h("li", null, h("span", { class: "id" }, id), h("span", { class: "bar", "aria-hidden": "true" }, i), h("span", { class: "v" }, num(v)));
    })));
  },
  ledger() {
    const l = S.tokens.rows || [];
    const n = S.limit.ledger || 20;
    const SRC = { act: "agv 턴", supervise: "감독", bridge: "브리지 보고", rw1: "허브" };
    return section("ledger", "장부", "agv 턴 장부 · 허브 장부", l.length ? h("table", { class: "tbl" },
      h("thead", null, h("tr", null, ["출처", "작업", "모델"].map((x) => h("th", null, x)),
        ["턴", "입력", "출력", "캐시 읽음", "합계", "비용"].map((x) => h("th", { class: "num" }, x)), h("th", null, "언제"))),
      h("tbody", null, l.slice(0, n).map((r) => h("tr", null,
        h("td", { "data-label": "출처" }, SRC[r.source] || r.source || "—"),
        h("td", { "data-label": "작업" }, h("span", { class: "id" }, r.id || "—")),
        h("td", { "data-label": "모델" }, mono(r.model || "—")),
        ["turns", "input", "output", "cache_read", "total"].map((k, j) =>
          h("td", { class: "num", "data-label": ["턴", "입력", "출력", "캐시 읽음", "합계"][j] }, num(r[k]))),
        h("td", { class: "num", "data-label": "비용" }, r.cost_usd === null || r.cost_usd === undefined ? "—" : "$" + Number(r.cost_usd).toFixed(2)),
        h("td", { "data-label": "언제" }, when(r.at))))))
      : h("p", { class: "note" }, "장부가 비어 있어요."),
    more("ledger", l.length, n, "장부"),
    h("p", { class: "note" }, "— = 그 출처가 숫자를 알려 주지 않음. 0 과 다릅니다."));
  },
};

// ---- 묻기 --------------------------------------------------------------------------------------------------------
function runLine(run, d) {
  if (d.end) run.done = true;
  else run.lines.push(d.text || "");
  const p = S.past.find((x) => x.run === run.id);
  if (p && run.done) { p.status = "답함"; p.say = run.lines.slice(-1)[0] || ""; savePast(); changed("past"); }
}

async function askPlan() {
  stream();
  const q = S.ask.q.trim();
  if (!q) { S.ask.said = { fail: true, text: "물음을 먼저 써 주세요." }; changed("ask"); return; }
  S.ask.busy = "plan";
  S.ask.said = null;
  S.ask.plan = null;
  changed("askbox"); changed("ask");
  try {
    S.ask.plan = { ...(await api("/api/ask", { q, mode: S.ask.mode })), q, mode: S.ask.mode };
  } catch (e) {
    const why = e.body && (e.body.refuse || e.body.error);
    S.ask.said = { fail: true, text: why ? `돌릴 수 없어요: ${why}` : `비용을 보지 못했어요 (${e.message}).` };
  }
  S.ask.busy = false;
  changed("askbox"); changed("ask");
}

async function askConfirm() {
  const p = S.ask.plan;
  S.ask.busy = "run";
  changed("ask");
  try {
    const r = await api("/api/ask/confirm", { confirm_id: p.confirm_id });
    S.run = { id: r.run_id, q: p.q, lines: [], done: false };
    S.past.unshift({ run: r.run_id, q: p.q, mode: p.mode, cost: p.cost_estimate, status: "도는 중", at: new Date().toISOString() });
    S.past.length = Math.min(S.past.length, 20);
    for (const d of S.runBuf[r.run_id] || []) runLine(S.run, d);
    delete S.runBuf[r.run_id];
    S.ask.plan = null;
    S.ask.said = { text: "돌리기 시작했어요. 아래에 줄이 쌓입니다." };
    savePast();
  } catch (e) {
    S.ask.said = { fail: true, text: e.status === 404 ? "확인 번호가 지났어요. 비용 보기를 다시 눌러 주세요." : `돌리지 못했어요 (${e.message}).` };
  }
  S.ask.busy = false;
  changed("ask"); changed("run"); changed("past");
}

function askCancel() {
  const p = S.ask.plan;
  S.past.unshift({ q: p.q, mode: p.mode, cost: p.cost_estimate, status: "취소함", at: new Date().toISOString() });
  S.ask.plan = null;
  S.ask.said = { text: "돌리지 않았어요." };
  savePast();
  changed("ask"); changed("past");
}

const costWord = (c) => (!c ? "—" : c.cost === "model" ? "모델 씀" : c.cost === "free" ? "0 토큰 · 모델 안 씀" : c.cost === "mail" ? "편지만 보냄" : String(c.cost));

const ASK = {
  mast() {
    return mast(ROUTES.ask, ["묻기 · ", h("b", null, "ga ask"), " · ", h("b", null, "ga do")], "무엇을 할까요?",
      "먼저 비용을 보여 드리고, 확인을 누르면 그때 돌립니다.");
  },
  box() {
    const radio = (v, word) => h("label", null, h("input", { type: "radio", name: "kind", value: v, "data-key": "kind-" + v,
      prop: { checked: S.ask.mode === v }, on: { change: () => { S.ask.mode = v; } } }), " " + word);
    return section("box", "새 물음", null, h("form", { class: "ask-q", on: { submit: (e) => { e.preventDefault(); askPlan(); } } },
      h("fieldset", { class: "choice" }, h("legend", null, "종류"), radio("ask", "묻기 — 읽기만"), radio("do", "하기 — 고칠 수 있음")),
      h("div", { class: "field" }, h("label", { for: "q" }, "물음"),
        h("textarea", { id: "q", name: "q", "data-key": "q", prop: { value: S.ask.q }, on: { input: (e) => { S.ask.q = e.target.value; } } })),
      h("div", { class: "actions" }, h("button", { type: "submit", "data-key": "plan", disabled: Boolean(S.ask.busy) },
        S.ask.busy === "plan" ? "비용 보는 중 …" : "비용 보기"))));
  },
  cost() {
    const p = S.ask.plan;
    const said = S.ask.said ? h("p", { class: "said" + (S.ask.said.fail ? " fail" : ""), role: "status" }, S.ask.said.text)
      : h("p", { class: "said", role: "status" });
    if (!p) return section("cost", "비용 먼저", null, said, h("p", { class: "note" }, "물음을 쓰고 비용 보기를 누르면, 돌리기 전에 여기에 먼저 보여 드려요."));
    const c = p.cost_estimate || {};
    const model = c.cost === "model";
    return section("cost", "비용 먼저", null, h("div", { class: "cost" },
      h("dl", { class: "figs" },
        h("div", null, h("dt", null, "예상 토큰"), h("dd", null, c.input_tokens === null || c.input_tokens === undefined ? "—" : "~" + num(c.input_tokens))),
        h("div", null, h("dt", null, "모델 호출"), h("dd", null, num(c.turns))),
        h("div", null, h("dt", null, "비용"), h("dd", null, costWord(c)))),
      h("p", { class: "say" }, p.mode === "do" ? "고칠 수 있어요. ga do 가 저장소의 파일을 바꿀 수 있습니다." : "읽기만 합니다. 파일은 고치지 않아요."),
      h("ol", { class: "plan", "aria-label": "할 일" }, (p.plan || []).map((x) => h("li", null, x))),
      h("div", { class: "actions" },
        h("button", { type: "button", class: model ? "primary" : null, "data-key": "confirm", disabled: Boolean(S.ask.busy), on: { click: askConfirm } },
          S.ask.busy === "run" ? "돌리는 중 …" : "확인하고 돌리기"),
        h("button", { type: "button", "data-key": "cancel", disabled: Boolean(S.ask.busy), on: { click: askCancel } }, "물음 취소")),
      c.input_tokens === null || c.input_tokens === undefined ? h("p", { class: "note" }, "— 는 0 과 다릅니다: 미리 셀 수 없는 양이에요.") : null),
    said);
  },
  run() {
    const r = S.run;
    if (!r) return null;
    return section("run", "돌리는 중", null, h("div", { class: "row" }, r.done ? state("ok", "끝남") : state("live", "도는 중"), h("h3", null, r.q)),
      h("pre", { class: "out", role: "log", "aria-label": "돌린 결과" }, r.lines.length ? r.lines.join("\n") : "…"));
  },
  past() {
    const l = S.past;
    if (!l.length) return section("past", "지난 물음", null, h("p", { class: "note" }, "이 창에서 물은 것이 여기에 남습니다."));
    return section("past", "지난 물음", `최근 ${Math.min(l.length, 20)}`, h("ol", { class: "items" }, l.slice(0, 20).map((x) => h("li", null,
      h("div", { class: "row" }, x.status === "도는 중" ? state("live", x.status) : x.status === "답함" ? state("ok", x.status) : state("wait", x.status),
        h("h3", null, x.q)),
      x.say ? h("p", { class: "say" }, x.say) : null,
      h("p", { class: "meta" }, h("span", null, x.mode === "do" ? "하기" : "묻기"), h("span", null, costWord(x.cost)), h("span", null, when(x.at)))))));
  },
};

// ---- 결정 --------------------------------------------------------------------------------------------------------
function marked(text, q) {
  if (!q) return [text];
  const out = [];
  const low = text.toLowerCase();
  const ql = q.toLowerCase();
  let i = 0;
  for (let j = low.indexOf(ql); j >= 0; j = low.indexOf(ql, i)) {
    out.push(text.slice(i, j), h("mark", null, text.slice(j, j + q.length)));
    i = j + q.length;
  }
  out.push(text.slice(i));
  return out;
}

const DECISIONS = {
  mast() {
    const l = S.dec;
    const q = S.decQ;
    const cap = ["결정 · ", h("b", null, q ? `“${q}”` : "모두"), " · 허브의 결정 기록"];
    if (!l.length) return mast(ROUTES.decisions, cap, q ? "찾은 결정 없음" : "결정 없음",
      q ? "다른 말로 찾아 보세요. 빈 칸으로 찾으면 모두 보입니다." : "허브가 DECISION_LOG.md 에 결정을 적으면 여기에 보입니다.");
    return mast(ROUTES.decisions, cap, q ? `“${q}” ${num(l.length)}건` : `결정 ${num(l.length)}개`,
      q ? "찾는 말이 들어 있는 결정입니다. 최근 것이 먼저." : "허브가 적은 결정입니다. 최근 것이 먼저.");
  },
  find() {
    return section("find", "찾기", null, h("form", { class: "actions", role: "search", on: { submit: (e) => {
      e.preventDefault();
      const v = e.target.querySelector("input").value.trim();
      location.hash = "#/decisions" + (v ? "?q=" + encodeURIComponent(v) : "");
    } } }, h("label", { class: "vh", for: "find" }, "결정 찾기"),
    h("input", { type: "search", id: "find", name: "q", class: "grow", "data-key": "find", prop: { value: S.decQ } }),
    h("button", { type: "submit", "data-key": "find-go" }, "결정 찾기")));
  },
  hits() {
    const l = S.dec;
    const n = S.limit.dec || 20;
    if (!l.length) return null;
    return section("hits", "찾은 결정", `${Math.min(n, l.length)} / ${l.length}`, h("ol", { class: "items hits" }, l.slice(0, n).map((d) => {
      const parts = String(d.text || "").split(" | ");
      const text = parts[0];
      const refs = [...new Set((String(d.text || "").match(/\b(?:CMD|GA|OPS)-[A-Za-z0-9]+\b/g) || []))];
      return h("li", null, h("p", null, h("span", { class: "id" }, d.id), " ", marked(text, S.decQ)),
        h("p", { class: "meta" }, h("span", null, refs.length ? refs.join(" · ") : "지시 언급 없음"),
          parts.slice(1).filter((x) => x.trim() !== d.id).length ? h("span", { class: "mono" }, parts.slice(1).join(" · ")) : null));
    })), more("dec", l.length, n, "결정"));
  },
};

// ---- routes -------------------------------------------------------------------------------------------------------
const P = (id, deps, render, pair) => ({ id, deps, render, pair });
const ROUTES = {};

function defineRoutes() {
  const r = (name, label, need, foot, parts) => {
    ROUTES[name] = { name, label, need, foot, parts: [] };
    const R = ROUTES[name];
    R.parts = parts.map(([id, deps, fn, pair], k) =>
      P(k === 0 ? name + "-mast" : name + "-" + id, [...deps, "err", "stale", ...need], k === 0 ? guarded(R, fn) : body(R, fn), pair));
  };
  const LOGIN = "로그인 없음: ga console 이 연 이 주소와 한 번 쓰는 열쇠로만 열립니다. 이 Mac 밖으로는 아무것도 보내지 않습니다.";
  r("now", "지금", ["state", "work", "mail"], LOGIN, [
    ["mast", [], NOW.mast], ["figs", [], NOW.figs], ["run", ["turns"], NOW.run, "a"], ["mail", [], NOW.mail, "a"]]);
  r("work", "작업", ["state", "work"], "상태는 다섯 칸으로만 읽습니다: 초안 → 보냄 → 도는 중 → 보고됨 → 통합됨. 옆으로 빠지는 길은 돌려보냄과 실패 둘뿐입니다.", [
    ["mast", [], WORKS.mast], ["open", ["more"], WORKS.open], ["done", ["more"], WORKS.done]]);
  r("branches", "브랜치", ["state", "branches"], "이 화면은 읽기만 합니다. 합치기와 지우기는 허브가 합니다.", [
    ["mast", [], BRANCHES.mast], ["repos", [], BRANCHES.repos], ["table", ["more"], BRANCHES.table]]);
  r("services", "서비스", ["state"], "버튼은 이 Mac 의 프로세스만 켜고 끕니다. 비밀 값은 보여 주지 않습니다: 기록에는 환경 변수의 이름만 남습니다.", [
    ["mast", [], SERVICES.mast], ["list", ["svc"], SERVICES.list], ["log", ["log", "svc"], SERVICES.log]]);
  r("tokens", "토큰", ["tokens"], "토큰 숫자는 장부에 적힌 그대로입니다. 이 화면은 새로 세지 않습니다.", [
    ["mast", [], TOKENS.mast], ["today", [], TOKENS.today], ["per", [], TOKENS.per], ["ledger", ["more"], TOKENS.ledger]]);
  r("ask", "묻기", [], "확인 없이 모델을 부르는 버튼은 없습니다. 기록만 읽어 답할 수 있으면 0 토큰으로 답합니다.", [
    ["mast", [], ASK.mast], ["box", ["askbox"], ASK.box, "a"], ["cost", ["ask"], ASK.cost, "a"], ["run", ["run"], ASK.run], ["past", ["past"], ASK.past]]);
  r("decisions", "결정", ["dec"], "결정은 허브의 기록 파일에서 읽습니다. 이 화면에서는 고칠 수 없습니다.", [
    ["mast", [], DECISIONS.mast], ["find", [], DECISIONS.find], ["hits", ["more"], DECISIONS.hits]]);
}

function route() {
  const m = /^#\/([a-z]+)(?:\?(.*))?$/.exec(location.hash);
  const name = m && ROUTES[m[1]] ? m[1] : "now";
  const params = new URLSearchParams(m && m[2] ? m[2] : "");
  return { ...ROUTES[name], params };
}

function go() {
  const r = route();
  if (!location.hash || !/^#\/[a-z]+/.test(location.hash)) history.replaceState(null, "", "#/now");
  if (r.name === "decisions") S.decQ = r.params.get("q") || "";
  S.confirm = null;
  render();
  for (const k of r.need) load(k);
  if (!r.need.includes("state")) load("state");  // the nav counts
  window.scrollTo(0, 0);
}

defineRoutes();
document.getElementById("where").textContent = `${location.host} · 이 Mac 에서만`;
window.addEventListener("hashchange", go);
go();
setInterval(() => { if (S.downSince) { banner(); changed("stale"); } }, 5000);
if (TOKEN) setTimeout(stream, STREAM_AFTER_MS);
else { S.denied = true; banner(); }
