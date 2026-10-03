"""Record store (METHOD §3.4, G2): round/1 · decision/1 · stage/1 as canonical JSON files, rendered to Markdown.

The files are the source; Markdown is derived. Same records in, same bytes out.
Records are append-only: writing the same content again is a no-op, writing different
content over an existing record is refused unless ``replace=True``.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .forms import LABELS, FormError, Problem, canonical_json, load

KINDS = {"round/1": "rounds", "decision/1": "decisions", "stage/1": "stages", "review/1": "reviews"}


def _file_name(doc: dict[str, Any]) -> str:
    kind = doc["schema"]
    if kind == "round/1":
        return f"round-{doc['n']:04d}.json"
    if kind == "decision/1":
        return f"BD-{int(doc['id'][3:]):04d}.json"
    if kind == "review/1":
        return f"RV-{int(doc['id'][3:]):04d}.json"
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", doc["name"])
    return f"{safe}.json"


def _counts_text(c: dict[str, int] | None) -> str:
    if not c:
        return ""
    s = f"{c['passed']}"
    extra = [f"실패 {c['failed']}"] if c.get("failed") else []
    extra += [f"건너뜀 {c['skipped']}"] if c.get("skipped") else []
    return f"({s}{', ' + ', '.join(extra) if extra else ''})"


class RecordStore:
    def __init__(self, root: str | Path):
        self.root = Path(root)

    # ------------------------------------------------------------------ write

    def path_for(self, doc: dict[str, Any]) -> Path:
        return self.root / KINDS[doc["schema"]] / _file_name(doc)

    def put(self, doc: dict[str, Any], replace: bool = False) -> bool:
        """Write a record. Returns True if bytes changed on disk."""
        if doc.get("schema") not in KINDS:
            raise FormError([Problem("$.schema", f"not a record kind: {doc.get('schema')!r}")])
        load(doc)
        path = self.path_for(doc)
        data = canonical_json(doc)
        if path.exists():
            old = path.read_text(encoding="utf-8")
            if old == data:
                return False
            if not replace:
                raise FormError([Problem(str(path.name), "record exists with different content (append-only)")])
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(data, encoding="utf-8")
        return True

    # ------------------------------------------------------------------ read

    def all(self, kind: str) -> list[dict[str, Any]]:
        d = self.root / KINDS[kind]
        if not d.is_dir():
            return []
        docs = [json.loads(p.read_text(encoding="utf-8")) for p in sorted(d.glob("*.json"))]
        if kind == "round/1":
            docs.sort(key=lambda r: r["n"])
        elif kind in ("decision/1", "review/1"):
            docs.sort(key=lambda r: int(r["id"][3:]))
        else:
            docs.sort(key=lambda r: (r["date"], r["name"]))
        return docs

    def next_round(self) -> int:
        rounds = self.all("round/1")
        return rounds[-1]["n"] + 1 if rounds else 1

    def next_decision(self) -> str:
        ds = self.all("decision/1")
        return f"BD-{int(ds[-1]['id'][3:]) + 1 if ds else 1}"

    def next_review(self) -> str:
        rs = self.all("review/1")
        return f"RV-{int(rs[-1]['id'][3:]) + 1 if rs else 1}"

    def decision(self, bd: str) -> dict[str, Any] | None:
        return next((d for d in self.all("decision/1") if d["id"] == bd), None)

    # ------------------------------------------------------------------ render

    def render(self, slugs: dict[str, str] | None = None) -> dict[str, str]:
        """Markdown files derived from the records: {file name: text}."""
        slugs = slugs or {}
        return {
            "ROUNDS.md": self._render_rounds(),
            "DECISIONS.md": self._render_decisions(),
            "STAGES.md": self._render_stages(slugs),
        }

    def write_rendered(self, out_dir: str | Path, slugs: dict[str, str] | None = None) -> list[str]:
        """Write rendered files whose bytes changed. Returns the names written."""
        out = Path(out_dir)
        written = []
        for name, text in self.render(slugs).items():
            p = out / name
            if p.exists() and p.read_text(encoding="utf-8") == text:
                continue
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(text, encoding="utf-8")
            written.append(name)
        return written

    def _render_rounds(self) -> str:
        lines = ["# 회차 기록", "", "> ga-SDK 가 `rounds/*.json` 에서 만든 파일이다. 손으로 고치지 않는다.", ""]
        reviews: dict[int, list[dict[str, Any]]] = {}
        for rv in self.all("review/1"):
            reviews.setdefault(rv.get("round", 0), []).append(rv)
        for r in self.all("round/1"):
            repos = " · ".join(f"{x['repo']} `{x['sha'][:7]}`{_counts_text(x.get('tests'))}" for x in r["repos"]) or "저장소 변화 없음"
            dirs = ", ".join(r["directives"]) or "지시 없음"
            line = f"- {r['n']} 회차 ({r['date']}): {repos} — {dirs} — **{LABELS[r['verdict']]}** — {r['summary']} → 다음: {LABELS[r['next']]}"
            if r.get("decisions"):
                line += f" · 결정 {', '.join(r['decisions'])}"
            lines.append(line)
            for n in r.get("notices", []):
                lines.append(f"  - 알림: {n}")
            for rv in reviews.get(r["n"], []):  # outside verdicts are appended, the round record is never rewritten
                cause = f" · {LABELS[rv['cause']]}" if rv.get("cause") else ""
                fix = (f" → 이 회차 판정을 **{LABELS[rv['amends']['from']]} → {LABELS[rv['amends']['to']]}** 로 고침"
                       if rv.get("amends") else "")
                lines.append(f"  - 바깥 판정 {rv['id']} ({rv['by']}, {rv['date']}): `{rv['sha'][:7]}` **{LABELS[rv['class']]}**{cause} — {rv['why']}{fix}")
        for rv in reviews.get(0, []):  # a review of a sha no round integrated
            lines.append(f"- 바깥 판정 {rv['id']} ({rv['by']}, {rv['date']}): {rv['repo']} `{rv['sha'][:7]}` **{LABELS[rv['class']]}** — {rv['why']}")
        return "\n".join(lines) + "\n"

    def _render_decisions(self) -> str:
        lines = [
            "# 결정 기록",
            "",
            "> ga-SDK 가 `decisions/*.json` 에서 만든 파일이다. 손으로 고치지 않는다.",
            "",
            "| BD | 날짜 | 결정 | 근거 | 누가 | 대체 |",
            "|---|---|---|---|---|---|",
        ]
        for d in self.all("decision/1"):
            cells = [d["id"], d["date"], d["decision"], d["basis"], LABELS[d["by"]], ", ".join(d.get("supersedes", [])) or "-"]
            lines.append("| " + " | ".join(c.replace("|", "\\|").replace("\n", " ") for c in cells) + " |")
        return "\n".join(lines) + "\n"

    def _render_stages(self, slugs: dict[str, str]) -> str:
        lines = ["# 단계 마감 기록", "", "> ga-SDK 가 `stages/*.json` 에서 만든 파일이다. 태그는 sha 로 고정하고 명령은 사람이 돌린다.", ""]
        tags: list[str] = []
        for s in self.all("stage/1"):
            head = f"## {s['name']} — {s['date']}"
            if s.get("decision"):
                head += f" ({s['decision']})"
            lines += [head, "", "| 저장소 | 커밋 | 시험 |", "|---|---|---|"]
            for r in s["repos"]:
                lines.append(f"| {r['repo']} | `{r['sha']}` | {r['tests']} |")
                slug = slugs.get(r["repo"], r["repo"])
                tags.append(f"gh api repos/{slug}/git/refs -f ref=refs/tags/{s['name']} -f sha={r['sha']}")
            lines.append("")
            if s["established"]:
                lines.append("- 선 것: " + " · ".join(s["established"]))
            if s["deferred"]:
                lines.append("- 넘긴 것: " + " · ".join(s["deferred"]))
            lines.append("")
        if tags:
            lines += ["### 태그를 달려면 (사용자 컴퓨터에서)", "", "```", *tags, "```", ""]
        return "\n".join(lines).rstrip("\n") + "\n"
