// GA UI (CMD-GA36 S4). No remote script, no inline script: this file only. The token stays in this page.
"use strict";
const TOKEN = new URLSearchParams(location.search).get("t") || "";
const $ = (id) => document.getElementById(id);
let source = null;

async function api(path, body) {
  const opt = body === undefined ? { headers: { "X-GA-Token": TOKEN } }
    : { method: "POST", headers: { "X-GA-Token": TOKEN, "Content-Type": "application/json" }, body: JSON.stringify(body) };
  const r = await fetch(path + (body === undefined ? "?t=" + encodeURIComponent(TOKEN) : ""), opt);
  let data = {};
  try { data = await r.json(); } catch (e) { data = { error: "HTTP " + r.status }; }
  return { status: r.status, data };
}

async function refresh() {
  const { status, data } = await api("/api/state");
  if (status !== 200) { $("today").textContent = "연결 안 됨 (주소의 토큰을 확인하세요)"; return; }
  const d = data.today;
  $("today").textContent = `오늘 agy 턴 ${data.agy_turns} / ${data.cap} · 입력 ${d.input.toLocaleString()} · 출력 ${d.output.toLocaleString()} 토큰 · ${d.seconds}초`;
  const box = $("intents");
  if (!box.childElementCount) {
    for (const it of data.intents) {
      const b = document.createElement("button");
      b.type = "button"; b.textContent = it.label; b.title = "예: " + it.example; b.dataset.cost = it.cost;
      b.addEventListener("click", () => runIntent(it.name, false));
      box.appendChild(b);
    }
    $("sbackend").value = data.solve.backend || ""; $("smodel").value = data.solve.model || "";
    $("scap").value = data.solve.cap || ""; $("sbase").value = data.solve.base_url || "";
  }
}

function confirmLines(lines) {
  return new Promise((resolve) => {
    const ul = $("confirm-lines");
    ul.replaceChildren(...lines.map((t) => { const li = document.createElement("li"); li.textContent = t; return li; }));
    const dlg = $("confirm");
    const done = (v) => { dlg.close(); $("confirm-yes").onclick = null; $("confirm-no").onclick = null; resolve(v); };
    $("confirm-yes").onclick = () => done(true);
    $("confirm-no").onclick = () => done(false);
    dlg.showModal();
  });
}

function follow(run) {
  if (source) source.close();
  $("log").textContent = ""; $("run-id").textContent = run;
  source = new EventSource(`/api/log?run=${encodeURIComponent(run)}&t=${encodeURIComponent(TOKEN)}`);
  source.onmessage = (ev) => { $("log").textContent += JSON.parse(ev.data) + "\n"; $("log").scrollTop = 1e9; };
  source.addEventListener("end", () => { source.close(); source = null; refresh(); });
}

async function act(path, body) {
  let { status, data } = await api(path, body);
  if (status === 409 && data.needs_confirm) {
    if (!(await confirmLines(data.lines))) { $("answer").textContent = "취소했습니다 (아무것도 실행하지 않음)"; return; }
    ({ status, data } = await api(path, { ...body, confirm: true }));
  }
  if (status === 200 && data.run) { follow(data.run); return; }
  $("answer").textContent = data.refuse || data.error || `HTTP ${status}`;
}

function runIntent(name) { return act("/api/run", { intent: name }); }

$("ask-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const q = $("q").value.trim();
  if (!q) return;
  const { data } = await api("/api/route", { q });
  if (!data.intent) {
    $("answer").textContent = "무슨 뜻인지 정하지 못했습니다. 가까운 것: " + data.suggestions.map((s) => s.label).join(" · ");
    return;
  }
  $("answer").textContent = `→ ${data.label}`;
  if (data.refuse) { $("answer").textContent += ` — ${data.refuse}`; return; }
  runIntent(data.intent);
});

$("solve-form").addEventListener("submit", (ev) => {
  ev.preventDefault();
  const q = $("sq").value.trim();
  if (!q) return;
  act("/api/solve", { q, backend: $("sbackend").value.trim(), model: $("smodel").value.trim(),
                      cap: $("scap").value.trim(), base_url: $("sbase").value.trim() });
});

refresh();
