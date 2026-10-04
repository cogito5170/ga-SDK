"""The compact golden (CMD-GA26): every follow-up case of ga_438a34a.json, plus the first turn and the protocol, compiled
compact by ga's spec. A snapshot — a change to a compact branch of the spec shows here and needs a new baseline verdict.

    python3 tests/fixtures/pspec/make_compact_golden.py > tests/fixtures/pspec/compact_golden.json
"""
import json
import sys
from pathlib import Path

from ga import gemini as G

HERE = Path(__file__).resolve().parent


def inputs(d, c):
    tools = d["tools"][c["tools"]]
    task = d["tasks"].get(c.get("task"), "")
    res = [{"id": a, "tool": b, "text": t} for a, b, t in d["results"].get(c.get("results"), [])]
    ask = d["asks"][c["ask"]] if "ask" in c else ""
    return tools, task, res, ask


def section(c):
    return "once" if c["kind"] == "protocol" else c["kind"]


def main():
    d = json.loads((HERE / "ga_438a34a.json").read_text(encoding="utf-8"))
    cases = []
    for c in d["cases"]:
        tools, task, res, ask = inputs(d, c)
        cases.append({k: v for k, v in c.items() if k != "text"} |
                     {"text": G.prompt_text(section(c), tools, task, res, ask, "compact")})
    json.dump({"schema": "ga-prompt-fixtures/1", "mode": "compact", "spec_digest": G.plan_spec().digest,
               "cases": cases}, sys.stdout, ensure_ascii=False, indent=1, sort_keys=True)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
