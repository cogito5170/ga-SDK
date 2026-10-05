"""The project state store (CMD-GA37 rev 2 S7): what a short follow-up like '1번' or '어디까지 되었어?' refers to.

    <ga_dir>/state/decisions.jsonl   {"id": "D<n>", "text", "ts", "task", "affects": [work id]}   append only
    <ga_dir>/state/options.json      the last offered options: {"task", "ts", "items": [{"n", "text", "recommended"}]}
    <ga_dir>/state/work.json         open work: [{"id": task id, "title", "kind", "status"}]
    <ga_dir>/state/requests.json     open requests to a person: [{"id": "Q<n>", "text", "needs", "task"}]

``lines()`` is the capped summary the intake turn sees (inside the 2000-token context, ga/intake/summary.py). The last
request rows of the ledger (``<ga_dir>/ledger/requests-*.jsonl``) go in too, so a status question can cite them.
Text that looks like a secret is never stored (the request is withheld before it reaches here).
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Callable

RECENT = 5       # ledger request rows shown
MAX_ITEMS = 8    # per list in the summary
WIDTH = 160      # characters per summary line


def _cut(text: str, width: int = WIDTH) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= width else text[: width - 1] + "…"


class State:
    def __init__(self, ga_dir: str | Path, clock: Callable[[], float] = time.time):
        self.ga_dir = Path(ga_dir)
        self.dir = self.ga_dir / "state"
        self.clock = clock

    # ---- files
    def _json(self, name: str, default: Any) -> Any:
        p = self.dir / name
        try:
            return json.loads(p.read_text(encoding="utf-8")) if p.exists() else default
        except ValueError:
            return default

    def _put(self, name: str, value: Any) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        tmp = self.dir / (name + ".tmp")
        tmp.write_text(json.dumps(value, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
        tmp.replace(self.dir / name)

    def _ts(self) -> str:
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(self.clock()))

    # ---- reads
    def decisions(self) -> list[dict[str, Any]]:
        p = self.dir / "decisions.jsonl"
        if not p.exists():
            return []
        return [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines() if x.strip()]

    def options(self) -> dict[str, Any] | None:
        o = self._json("options.json", None)
        return o if isinstance(o, dict) and o.get("items") else None

    def work(self) -> list[dict[str, Any]]:
        return [w for w in self._json("work.json", []) if isinstance(w, dict)]

    def requests(self) -> list[dict[str, Any]]:
        return [r for r in self._json("requests.json", []) if isinstance(r, dict)]

    def recent(self, n: int = RECENT) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for p in sorted((self.ga_dir / "ledger").glob("requests-*.jsonl")):
            rows += [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines() if x.strip()]
        return rows[-n:]

    # ---- writes
    def add_decision(self, text: str, affects: list[str], task: str) -> str:
        did = f"D{len(self.decisions()) + 1}"
        self.dir.mkdir(parents=True, exist_ok=True)
        with (self.dir / "decisions.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps({"id": did, "text": text, "ts": self._ts(), "task": task, "affects": list(affects)},
                               ensure_ascii=False) + "\n")
        return did

    def set_options(self, task: str, items: list[dict[str, Any]]) -> None:
        self._put("options.json", {"task": task, "ts": self._ts(),
                                   "items": [{"n": o["n"], "text": o["text"],
                                              "recommended": bool(o.get("recommended"))} for o in items]})

    def add_work(self, task: str, title: str, kind: str) -> None:
        work = [w for w in self.work() if w.get("id") != task]
        work.append({"id": task, "title": title, "kind": kind, "status": "planned"})
        self._put("work.json", work)

    def add_requests(self, task: str, questions: list[dict[str, Any]]) -> None:
        reqs = self.requests()
        for q in questions:
            reqs.append({"id": f"Q{len(reqs) + 1}", "text": q["text"], "needs": q["needs"], "task": task})
        self._put("requests.json", reqs)

    # ---- the summary for the intake context
    def lines(self) -> list[str]:
        out: list[str] = []
        o = self.options()
        if o:
            out.append(f"last offered options (from {o['task']}):")
            out += [f"  {i['n']}. {_cut(i['text'])}" + (" (recommended)" if i.get("recommended") else "")
                    for i in o["items"][:MAX_ITEMS]]
        work = [w for w in self.work() if w.get("status") != "done"]
        if work:
            out.append("open work:")
            out += [f"  {w['id']} [{w.get('kind', '?')}, {w.get('status', '?')}] {_cut(w.get('title', ''))}"
                    for w in work[-MAX_ITEMS:]]
        dec = self.decisions()
        if dec:
            out.append("decisions:")
            out += [f"  {d['id']} {d['ts'][:10]} {_cut(d['text'])}" for d in dec[-MAX_ITEMS:]]
        reqs = self.requests()
        if reqs:
            out.append("open requests to the person:")
            out += [f"  {r['id']} [{r['needs']}] {_cut(r['text'])}" for r in reqs[-MAX_ITEMS:]]
        rec = self.recent()
        if rec:
            out.append("recent requests (ledger):")
            out += [f"  {r.get('task')} {r.get('kind')} {r.get('outcome')} {_cut(r.get('request', ''), 60)}"
                    for r in rec]
        return out


__all__ = ["State"]
