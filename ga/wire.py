"""Forms as prompt-spec/1 (CMD-GA27 S2/S3, BD-298): directive/2, report/2, verdict/1 and notify/1 each have a
``ga/specs/forms/*.pspec`` file. Its ``out`` declares the form, so rlo.pspec's ``check`` reads it; its ``en`` and ``ko``
sections render a form as prose for people (``ga render``), with no model call. The wire carries only the head (S1).

ga.forms stays the runtime authority: ``spec_check`` runs beside it (tests compare both on every historical head).
pspec has no enum, pattern, number or union type, so ids, enums, regexes, number-or-string values and the cross-field
rules are ga.forms' alone. rlo is imported only when a spec is loaded.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from .forms import FormError, Problem, hard, validate

SPEC_DIR = Path(__file__).resolve().parent / "specs" / "forms"
FORM_SPECS = {"directive/2": "directive2.pspec", "report/2": "report2.pspec", "verdict/1": "verdict1.pspec",
              "notify/1": "notify1.pspec"}
LANGS = ("en", "ko")
_SPECS: dict[str, Any] = {}


def form_spec(schema: str) -> Any:
    """The loaded spec of a form (cached). A spec with a stray ``{{`` / ``{%`` is refused, as for ga gemini's."""
    if schema not in FORM_SPECS:
        raise FormError([Problem("$.schema", f"no form spec for {schema!r}; known: {', '.join(FORM_SPECS)}")])
    if schema not in _SPECS:
        from rlo import pspec
        from .gemini import stray_tags
        spec = pspec.load_file(SPEC_DIR / FORM_SPECS[schema])
        if stray_tags(spec) or spec.name != schema or spec.out_schema != schema:
            raise pspec.SpecError(f"{FORM_SPECS[schema]}: not a clean spec of {schema}")
        _SPECS[schema] = spec
    return _SPECS[schema]


def spec_check(head: dict[str, Any]) -> list[str]:
    """rlo.pspec's check of a head against its form spec (paths of the problems; empty = accepted)."""
    from rlo import pspec
    return pspec.check(form_spec(head.get("schema")), head, {})


def render(head: dict[str, Any], lang: str = "en") -> str:
    """Deterministic prose for a form head: ga.forms checks it first (a head with hard problems is not rendered), then
    the spec's ``lang`` section is compiled verbatim. Absent optional fields render as nothing."""
    if lang not in LANGS:
        raise ValueError(f"lang must be one of {LANGS}")
    probs = hard(validate(head))
    if probs:
        raise FormError(probs)
    from rlo import pspec
    spec = form_spec(head["schema"])
    values = {name: head.get(name) for name in spec.inputs}
    return pspec.compile(spec, lang, values, "verbatim")


# ---- S5: the token report, offline -----------------------------------------------------------------------------------

API_WRAPPER = 0.17  # baseline's measure (BD-297): the REST JSON around a comment adds 17% to its body (assumption here)


def _tokens(n_bytes: int) -> int:
    return -(-n_bytes // 4)  # rlo.pspec.tokens: ceil(utf-8 bytes / 4)


def _head(c: dict[str, Any]) -> dict[str, Any] | None:
    import json
    if not c.get("head_text"):
        return None
    try:
        v = json.loads(c["head_text"])
    except ValueError:
        return None
    return v if isinstance(v, dict) else None


def wire_bytes(c: dict[str, Any]) -> int:
    """A comment's bytes in the S1 form: its head minified in one block plus the footer; a comment without a ga head
    has nothing to minify and keeps its bytes."""
    from .forms import dump_wire
    h = _head(c)
    return len(dump_wire(h).encode("utf-8")) if h else c["bytes"]


def inbox_line_bytes(c: dict[str, Any]) -> int:
    from .inbox import line
    return len((line(c["id"], c.get("updated_at") or "", _head(c)) + "\n").encode("utf-8"))


def model_reads(comments: list[dict[str, Any]], *, start: str, end: str, reads: dict[int, int], notifies: int,
                notify_issue: int) -> list[dict[str, Any]]:
    """A read log modelled from baseline's totals (assumption): ``reads[issue]`` full reads of each issue, evenly
    spaced over [start, end]; ``notifies`` notify/1 messages, each followed by a read of ``notify_issue``."""
    from datetime import datetime, timezone

    def ts(s):
        return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()

    def iso(t):
        return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    a, b = ts(start), ts(end)
    log = []
    for issue, n in sorted(reads.items()):
        log += [{"at": iso(a + (b - a) * (k + 1) / n), "issue": issue, "kind": "read"} for k in range(n)]
    log += [{"at": iso(a + (b - a) * (k + 1) / notifies), "issue": notify_issue, "kind": "notify"} for k in range(notifies)]
    return sorted(log, key=lambda r: (r["at"], r["kind"]))


def replay(comments: list[dict[str, Any]], log: list[dict[str, Any]], *, notify_now_bytes: int,
           notify_wire_bytes: int) -> dict[str, Any]:
    """Bytes read now (each read fetches the whole issue through the REST API, with its wrapper; a notify is read
    as its body, then the issue) against ``ga inbox`` (each read prints only the comments new since that channel's
    cursor, one head line each; a notify is its minified kind + ref)."""
    by_issue: dict[int, list[dict[str, Any]]] = {}
    for c in sorted(comments, key=lambda c: c["created_at"]):
        by_issue.setdefault(c["issue"], []).append(c)
    cursor: dict[int, str] = {}
    now = inbox = 0
    for r in log:
        cs = [c for c in by_issue.get(r["issue"], []) if c["created_at"] <= r["at"]]
        full = sum(c["bytes"] for c in cs)
        new = [c for c in cs if c["created_at"] > cursor.get(r["issue"], "")]
        if r["kind"] == "notify":
            now += notify_now_bytes + round(full * (1 + API_WRAPPER))
            inbox += notify_wire_bytes + sum(inbox_line_bytes(c) for c in new)
        else:
            now += round(full * (1 + API_WRAPPER))
            inbox += sum(inbox_line_bytes(c) for c in new)
        if cs:
            cursor[r["issue"]] = max(cursor.get(r["issue"], ""), cs[-1]["created_at"])
    return {"reads": sum(1 for r in log if r["kind"] == "read"), "notifies": sum(1 for r in log if r["kind"] == "notify"),
            "bytes_now": now, "bytes_inbox": inbox, "tokens_now": _tokens(now), "tokens_inbox": _tokens(inbox),
            "saved_pct": round(100 * (1 - inbox / now), 1) if now else 0.0}


def token_report(corpus: dict[str, Any], *, upto: int = 279, reads: list[dict[str, Any]] | None = None,
                 notify_now_bytes: int = 0) -> dict[str, Any]:
    """S5: (a) the wire bytes of the first ``upto`` comments now and in the S1 form; (b) a read log replayed now and
    through ``ga inbox``; (c) the bytes a writer emits per directive and per report, now and in the S1 form."""
    from .forms import dump_wire
    cs = sorted(corpus["comments"], key=lambda c: c["created_at"])[:upto]
    now, s1 = sum(c["bytes"] for c in cs), sum(wire_bytes(c) for c in cs)
    a = {"comments": len(cs), "with_head": sum(1 for c in cs if _head(c)), "bytes_now": now, "bytes_s1": s1,
         "tokens_now": sum(_tokens(c["bytes"]) for c in cs), "tokens_s1": sum(_tokens(wire_bytes(c)) for c in cs),
         "saved_pct": round(100 * (1 - s1 / now), 1)}
    c_out = {}
    for kind in ("directive", "report"):
        ks = [c for c in cs if (_head(c) or {}).get("schema", "").startswith(kind + "/")]
        if ks:
            bn, bs = sum(c["bytes"] for c in ks), sum(wire_bytes(c) for c in ks)
            c_out[kind] = {"n": len(ks), "bytes_now_mean": round(bn / len(ks)), "bytes_s1_mean": round(bs / len(ks)),
                           "saved_pct": round(100 * (1 - bs / bn), 1)}
    out = {"schema": "ga-wire-report/1", "estimate": "ceil(utf-8 bytes / 4)", "a_wire": a, "c_writer": c_out}
    if reads is not None:
        note = {"schema": "notify/1", "to": "GA", "kind": "report",
                "ref": "https://github.com/cogito5170/baseline/issues/12#issuecomment-5982260506"}
        out["b_reads"] = replay(corpus["comments"], reads, notify_now_bytes=notify_now_bytes,
                                notify_wire_bytes=len(dump_wire(note).encode("utf-8")))
    return out
