"""SUMMARY.md from runs.jsonl and ledger.jsonl: per arm x model x context, H1 (bulk/selective), H2 (Sonnet/Haiku), H3 (C vs A)."""
from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path
from typing import Any

HAIKU, SONNET = "claude-haiku-4-5-20251001", "claude-sonnet-5-5"
SHORT = {HAIKU: "Haiku", SONNET: "Sonnet"}


def rows_of(p: Path) -> list[dict[str, Any]]:
    return [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines() if x.strip()] if p.exists() else []


def mr(vals: list[float], fmt: str = "{:.0f}") -> str:
    if not vals:
        return "-"
    m = sum(vals) / len(vals)
    return fmt.format(m) if len(vals) == 1 else f"{fmt.format(m)} [{fmt.format(min(vals))}-{fmt.format(max(vals))}]"


def agg(rs: list[dict]) -> dict[str, Any]:
    n = len(rs)
    corr = sum(r["result_correct"] for r in rs)
    q = sum(r["quota_usd"] for r in rs)
    qc = sum(cli(r) for r in rs)
    tt = sum(r["total_tokens"] for r in rs)
    return {"n": n, "correct": corr, "acc": corr / n if n else None, "q": q, "qc": qc, "tt": tt,
            "qpc": (q / corr) if corr else None, "qcpc": (qc / corr) if corr else None, "tpc": (tt / corr) if corr else None}


def cli(r: dict) -> float:
    """claude -p's own cost of a run (sum of total_cost_usd)."""
    return r.get("quota_cli_usd", r.get("total_cost_usd_sum", 0.0))


def f(x, fmt="{:.4f}"):
    return "n/a" if x is None else fmt.format(x)


def ratio(a, b):
    return None if a is None or b in (None, 0) else a / b


def table(rows: list[dict]) -> list[str]:
    L = ["| arm | model | context | runs | accuracy | total_tokens | quota_usd | quota_cli_usd | max_call_input | quota_per_correct | cli_per_correct | repeated_information | llm_calls |",
         "|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    g: dict[tuple, list] = defaultdict(list)
    for r in rows:
        g[(r["arm"], r["model"], r["mode"])].append(r)
    for (arm, model, mode), rs in sorted(g.items(), key=lambda kv: (kv[0][2] != "bulk", kv[0][0], kv[0][1])):
        a = agg(rs)
        L.append(f"| {arm} | {SHORT[model]} | {mode}{' (1 rep)' if mode == 'bulk' else ''} | {a['n']} | {a['correct']}/{a['n']} | "
                 f"{mr([r['total_tokens'] for r in rs])} | {mr([r['quota_usd'] for r in rs], '{:.4f}')} | "
                 f"{mr([cli(r) for r in rs], '{:.4f}')} | {mr([r['max_call_input'] for r in rs])} | {f(a['qpc'])} | {f(a['qcpc'])} | {mr([r['repeated_information'] for r in rs], '{:.1f}')} | "
                 f"{mr([r['llm_calls'] for r in rs], '{:.1f}')} |")
    return L


def render(rows: list[dict], ledger: list[dict], stopped: str | None = None) -> str:
    L = ["# FINAL_TASK results (CMD-FT1 rev 2)", ""]
    void = [r for r in rows if r.get("void")]
    rows = [r for r in rows if not r.get("void")]
    r2 = [r for r in ledger if r.get("rev", 1) == 2]
    probe = [r for r in ledger if r.get("run_id") == "probe"]
    cum_cli = sum((r.get("total_cost_usd") or r["quota_usd"]) for r in ledger)
    L += [f"- valid runs: {len(rows)} of 100 planned ({len(void)} rev-1 T2 rows are void: kept in runs.jsonl, excluded from every table); "
          f"`claude -p` calls: rev 1 {len(ledger) - len(r2)} ({len(probe)} probes) + rev 2 {len(r2)} of 130 = {len(ledger)}.",
          f"- cost: summed over all calls, quota_usd (from `usage`) ${sum(r['quota_usd'] for r in ledger):.4f}; "
          f"quota_cli_usd (claude -p total_cost_usd) ${cum_cli:.4f} of the $35 cumulative cap (rev 1 ${sum((r.get('total_cost_usd') or r['quota_usd']) for r in ledger if r.get('rev', 1) == 1):.4f} + rev 2 ${sum((r.get('total_cost_usd') or r['quota_usd']) for r in r2):.4f}).",
          f"- stop: {stopped or 'matrix complete'}.",
          "- quota_usd = API list-price conversion of the `usage` object (Haiku $1/$5, Sonnet $2/$10 per Mtok; cache_creation x1.25, "
          "cache_read x0.1). quota_cli_usd = sum of `claude -p` total_cost_usd, priced from `modelUsage`, which also counts a small "
          "internal Haiku call per `claude -p` that `usage` omits (see ledger.jsonl model_usage). The real subscription weights are not public, "
          "so both are shown. Every ratio is a mean over the reps that ran; `[a-b]` is the range; a single run is shown without a range.", ""]
    flagged = sum(len(r.get("cost_disagree_calls", [])) for r in rows)
    ncalls = sum(r["llm_calls"] for r in rows)
    L += [f"- claude -p `total_cost_usd` vs our arithmetic: {flagged} of {ncalls} calls disagree by more than 5%.", ""]
    errs = [r for r in rows if r.get("error")]
    if errs:
        L += [f"- runs ended by a failed call: {len(errs)} (" + ", ".join(r["run_id"] for r in errs) + ")", ""]

    L += ["## 1. Per arm x model x context", ""] + table(rows)
    cu, qu = sum(cli(r) for r in rows), sum(r["quota_usd"] for r in rows)
    L += ["", f"- Valid runs only: quota_usd ${qu:.4f}; quota_cli_usd ${cu:.4f} (x{(cu / qu if qu else 0):.2f}). The CLI figure is higher, "
          "most for the cheap selective calls (the internal Haiku call is a fixed overhead per `claude -p`)."]
    L += ["", "## 2. Per task (selective)", "", "| arm | model | task | runs | correct | quota_usd | quota_cli_usd | total_tokens | calls | tool/peer |",
          "|---|---|---|---|---|---|---|---|---|---|"]
    gt: dict[tuple, list] = defaultdict(list)
    for r in rows:
        if r["mode"] == "selective":
            gt[(r["arm"], r["model"], r["task"])].append(r)
    for (arm, model, t), rs in sorted(gt.items(), key=lambda kv: (kv[0][0], kv[0][2], kv[0][1])):
        L.append(f"| {arm} | {SHORT[model]} | {t} | {len(rs)} | {sum(r['result_correct'] for r in rs)}/{len(rs)} | "
                 f"{mr([r['quota_usd'] for r in rs], '{:.4f}')} | {mr([cli(r) for r in rs], '{:.4f}')} | {mr([r['total_tokens'] for r in rs])} | "
                 f"{mr([r['llm_calls'] for r in rs], '{:.1f}')} | {sum(r['tool_calls'] for r in rs)}/{sum(r['peer_messages'] for r in rs)} |")

    L += ["", "## 3. H1 bulk vs selective (arm A, same model, same task; bulk is 1 run per task)", "",
          "| model | pairs | quota_usd bulk/selective | quota_cli_usd bulk/selective | max_call_input bulk/selective | accuracy bulk | accuracy selective |", "|---|---|---|---|---|---|---|"]
    for model in (HAIKU, SONNET):
        pairs = []
        for t in ("T1", "T2", "T3", "T4", "T5"):
            b = [r for r in rows if (r["arm"], r["model"], r["mode"], r["task"]) == ("A", model, "bulk", t)]
            s = [r for r in rows if (r["arm"], r["model"], r["mode"], r["task"], r["rep"]) == ("A", model, "selective", t, 1)]
            if b and s:
                pairs.append((b[0], s[0]))
        if not pairs:
            L.append(f"| {SHORT[model]} | 0 | n/a | n/a | n/a | n/a | n/a |")
            continue
        qb, qs = sum(p[0]["quota_usd"] for p in pairs), sum(p[1]["quota_usd"] for p in pairs)
        cb, cs = sum(cli(p[0]) for p in pairs), sum(cli(p[1]) for p in pairs)
        mb = sum(p[0]["max_call_input"] for p in pairs) / len(pairs)
        ms = sum(p[1]["max_call_input"] for p in pairs) / len(pairs)
        L.append(f"| {SHORT[model]} | {len(pairs)} | {f(ratio(qb, qs), '{:.1f}x')} (${qb:.4f} / ${qs:.4f}) | {f(ratio(cb, cs), '{:.1f}x')} (${cb:.4f} / ${cs:.4f}) | {f(ratio(mb, ms), '{:.1f}x')} "
                 f"({mb:.0f} / {ms:.0f}) | {sum(p[0]['result_correct'] for p in pairs)}/{len(pairs)} | "
                 f"{sum(p[1]['result_correct'] for p in pairs)}/{len(pairs)} |")

    L += ["", "## 4. H2 Sonnet vs Haiku, quota_per_correct (selective)", "",
          "| task | arm | Haiku (correct/runs, usage $/correct, cli $/correct) | Sonnet (correct/runs, usage $/correct, cli $/correct) | Sonnet/Haiku usage | Sonnet/Haiku cli | cheaper per correct (usage / cli) |", "|---|---|---|---|---|---|---|---|"]
    for t in ("T1", "T2", "T3", "T4", "T5"):
        for arm in ("A", "B", "C", "all"):
            sel = lambda m: [r for r in rows if r["mode"] == "selective" and r["task"] == t and r["model"] == m  # noqa: E731
                             and (arm == "all" or r["arm"] == arm)]
            h, s = agg(sel(HAIKU)), agg(sel(SONNET))
            if not h["n"] and not s["n"]:
                continue
            def cheaper(hq, sq):
                rt = ratio(sq, hq)
                if rt is None and (hq is None) != (sq is None):
                    return "Sonnet" if hq is None else "Haiku"   # only one model got any right
                return "-" if rt is None else ("Haiku" if rt > 1 else "Sonnet" if rt < 1 else "tie")
            rt, rc = ratio(s["qpc"], h["qpc"]), ratio(s["qcpc"], h["qcpc"])
            L.append(f"| {t} | {arm} | {h['correct']}/{h['n']}, {f(h['qpc'])}, {f(h['qcpc'])} | {s['correct']}/{s['n']}, {f(s['qpc'])}, {f(s['qcpc'])} | "
                     f"{f(rt, '{:.2f}x')} | {f(rc, '{:.2f}x')} | {cheaper(h['qpc'], s['qpc'])} / {cheaper(h['qcpc'], s['qcpc'])} |")

    L += ["", "## 5. H3 C vs A, FINAL_TASK section 4 criteria per model (selective)", ""]
    for model in (HAIKU, SONNET):
        A = [r for r in rows if r["arm"] == "A" and r["model"] == model and r["mode"] == "selective"]
        C = [r for r in rows if r["arm"] == "C" and r["model"] == model and r["mode"] == "selective"]
        L.append(f"### {SHORT[model]}")
        if not A or not C:
            L += [f"- not judged: arm A has {len(A)} runs, arm C has {len(C)}.", ""]
            continue
        a, c = agg(A), agg(C)
        # compare only on the (task, rep) cells both arms ran
        cells = {(r["task"], r["rep"]) for r in A} & {(r["task"], r["rep"]) for r in C}
        Ac, Cc = [r for r in A if (r["task"], r["rep"]) in cells], [r for r in C if (r["task"], r["rep"]) in cells]
        ac, cc = agg(Ac), agg(Cc)
        t4 = [r for r in C if r["task"] == "T4"]
        t5 = [r for r in C if r["task"] == "T5"]
        ra, rc = sum(r["repeated_information"] for r in Ac) / len(Ac), sum(r["repeated_information"] for r in Cc) / len(Cc)
        cr1 = (cc["tpc"] is not None and ac["tpc"] is not None and cc["tpc"] <= ac["tpc"]) and cc["acc"] >= ac["acc"]
        cr2 = bool(t4) and bool(t5) and all(r["result_correct"] and r["tool_calls"] == 0 and r["peer_messages"] == 0 for r in t4) \
            and all(r["unknown_correct"] for r in t5)
        cr3 = rc < ra
        L += [f"- quota per correct, C vs A on the same cells: usage ${f(cc['qpc'])} vs ${f(ac['qpc'])}; cli ${f(cc['qcpc'])} vs ${f(ac['qcpc'])} (information, not a section 4 criterion).",
              f"- compared on {len(cells)} (task, rep) cells that both arms ran (A {len(Ac)} runs, C {len(Cc)} runs).",
              f"- tokens_per_correct C <= A and accuracy C >= A: **{'PASS' if cr1 else 'FAIL'}** "
              f"(tokens_per_correct C {f(cc['tpc'], '{:.0f}')} vs A {f(ac['tpc'], '{:.0f}')}; accuracy C {cc['correct']}/{cc['n']} vs A {ac['correct']}/{ac['n']})",
              f"- T4 tool_calls = peer_messages = 0 and correct, T5 UNKNOWN: **{'PASS' if cr2 else 'FAIL'}** "
              f"(C T4: {sum(r['result_correct'] and r['tool_calls'] == 0 and r['peer_messages'] == 0 for r in t4)}/{len(t4)}; "
              f"C T5 UNKNOWN: {sum(bool(r['unknown_correct']) for r in t5)}/{len(t5)})",
              f"- repeated_information C < A: **{'PASS' if cr3 else 'FAIL'}** (mean C {rc:.2f} vs A {ra:.2f})",
              "- spec 15 (connectivity and message count are not evidence of intelligence): C's peer messages are reported per run, "
              "not scored as a merit.", ""]
    L += ["## 6. Which quota measure bound each conclusion", ""]
    for model in (HAIKU, SONNET):
        B = agg([r for r in rows if r["arm"] == "A" and r["model"] == model and r["mode"] == "bulk"])
        S_ = agg([r for r in rows if r["arm"] == "A" and r["model"] == model and r["mode"] == "selective" and r["rep"] == 1])
        ru, rc = ratio(B["q"], S_["q"]), ratio(B["qc"], S_["qc"])
        L.append(f"- H1 {SHORT[model]}: bulk/selective x{f(ru, '{:.1f}')} by usage, x{f(rc, '{:.1f}')} by cli; direction "
                 f"{'agrees' if ru and rc and (ru > 1) == (rc > 1) else 'DISAGREES'}; the smaller ratio (the conservative one) is the one that binds the claim.")
    diff, tot = [], 0
    for t in ("T1", "T2", "T3", "T4", "T5"):
        for arm in ("A", "B", "C", "all"):
            sel = lambda m: agg([r for r in rows if r["mode"] == "selective" and r["task"] == t and r["model"] == m  # noqa: E731
                                 and (arm == "all" or r["arm"] == arm)])
            h, s = sel(HAIKU), sel(SONNET)
            if not h["n"] or not s["n"] or h["qpc"] is None or s["qpc"] is None:
                continue
            tot += 1
            if (s["qpc"] > h["qpc"]) != (s["qcpc"] > h["qcpc"]):
                diff.append(f"{t}/{arm}")
    L.append(f"- H2: {tot} (task, arm) cells where both models got something right; the cheaper-per-correct model differs between usage and cli in "
             f"{len(diff)}" + (f" ({', '.join(diff)}); those verdicts are bound by the measure chosen and are marked as such." if diff else "; the verdicts do not depend on the measure."))
    L.append("- Which one is the real subscription quota is not public; usage is the list-price model of what the call is, cli is what `claude -p` says it "
             "cost including its own overhead. Where they agree the conclusion stands on both; where they differ it is stated for each.")
    return "\n".join(L) + "\n"


def write(results: Path, stopped: str | None = None) -> Path:
    out = results / "SUMMARY.md"
    out.write_text(render(rows_of(results / "runs.jsonl"), rows_of(results / "ledger.jsonl"), stopped), encoding="utf-8")
    return out
