"""Capture today's gemini prompts byte for byte (CMD-GA26 D1). Run with the ga to capture on sys.path:

    PYTHONPATH=<ga-sdk checkout at 438a34a> python3 tests/fixtures/pspec/capture_prompts.py > tests/fixtures/pspec/ga_438a34a.json

It calls the real ``protocol()`` and ``Supervisor._prompt`` of that ga (first turn, --resume turn, no---resume turn) on
four tool tables, three tasks and four result sets. The supervisor is a stub that holds only what ``_prompt`` reads.
"""
import json
import subprocess
import sys
from pathlib import Path

from ga import gemini as G

TOOLS = {
    "six": {"read_file": {"about": "read a file from the workspace"},
            "search": {"about": "search the web; returns titles and urls"},
            "list_dir": {"about": "list a directory"},
            "write_note": {"about": "append a note to the user's notes"},
            "fetch_url": {"about": "fetch a url as text"},
            "noop": {}},
    "none": {},
    "empty_about": {"x": {"about": ""}},
    "edges": {"colon": {"python": "m:f", "about": "ends with a colon:"},
              "space": {"python": "m:f", "about": "trailing space "},
              "blank": {"python": "m:f", "about": " "},
              "mixed": {"mcp": "srv", "tool": "t", "about": "a: b :: "},
              "number": {"python": "m:f", "about": 7},
              "uni.code:+": {"mcp": "srv", "tool": "t", "about": "über — {{ not a tag }} {% if x %}"},
              "bare": {"python": "m:f"}},
}
TASKS = {
    "hero": "Find the three most recent design notes about the hero banner and summarise what changed between them.",
    "multiline": "Line one.\n\n  Line three with {braces} and {{ double }} and {% tag %}.\n",
    "unicode": "한글 과제 — café ☃",
}
RESULTS = {
    "none": [],
    "one": [("a", "search", "3 hits: notes/hero-v1.md, notes/hero-v2.md, notes/hero-v3.md")],
    "two": [("a", "search", "3 hits: notes/hero-v1.md, notes/hero-v2.md, notes/hero-v3.md"),
            ("b", "list_dir", "notes/: hero-v1.md hero-v2.md hero-v3.md footer.md")],
    "odd": [("s-1_X", "uni.code:+", "line 1\nline 2 {{ x }}\n"), ("z", "noop", ""), ("q", "colon", '{"k": [1, 2]}')],
}
ASKS = ["read the three hero notes and compare them", "Done?  {{ ask }}\n"]


class Stub:
    """What Supervisor._prompt reads: cfg, cli.resumes, st['steps'], _rec, _result_text."""

    def __init__(self, tools, task, results, resumes):
        self.cfg = G.GeminiConfig(root=Path("."), tools=tools)
        self.cli = type("Cli", (), {"resumes": resumes})()
        self.texts = {f"T1.r1.{pid}": text for pid, _tool, text in results}
        self.st = {"steps": [{"id": "T1.m1", "kind": "model", "first": True, "prompt": task, "after": []}]
                   + [{"id": f"T1.r1.{pid}", "kind": "tool", "plan_id": pid, "tool": tool, "args": {}, "after": []}
                      for pid, tool, _text in results]}

    _rec = G.Supervisor._rec

    def _result_text(self, sid):
        return self.texts[sid]


def main():
    cases = []
    for tn, tools in TOOLS.items():
        cases.append({"kind": "protocol", "tools": tn, "text": G.protocol(G.GeminiConfig(root=Path("."), tools=tools))})
        for kn, task in TASKS.items():
            first = {"id": "T1.m1", "kind": "model", "first": True, "prompt": task, "after": []}
            cases.append({"kind": "first", "tools": tn, "task": kn,
                          "text": G.Supervisor._prompt(Stub(tools, task, [], True), first)})
            for rn, results in RESULTS.items():
                for ai, ask in enumerate(ASKS):
                    rec = {"id": "T1.m2", "kind": "model", "prompt": ask, "after": [],
                           "needs": [f"T1.r1.{pid}" for pid, _t, _x in results]}
                    for kind, resumes in (("turn", True), ("turn_noresume", False)):
                        cases.append({"kind": kind, "tools": tn, "task": kn, "results": rn, "ask": ai,
                                      "text": G.Supervisor._prompt(Stub(tools, task, results, resumes), rec)})
    src = subprocess.run(["git", "rev-parse", "HEAD"], cwd=Path(G.__file__).parent, capture_output=True,
                         text=True).stdout.strip()
    json.dump({"schema": "ga-prompt-fixtures/1", "ga_sdk": src, "tools": TOOLS, "tasks": TASKS,
               "results": {k: [list(r) for r in v] for k, v in RESULTS.items()}, "asks": ASKS, "cases": cases},
              sys.stdout, ensure_ascii=False, indent=1, sort_keys=True)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
