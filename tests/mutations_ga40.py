"""CMD-GA40 D2: apply each mutation to a copy of the tree and run tests/test_ga40.py; every one must be killed.

    python tests/mutations_ga40.py      (not part of the unittest discovery)
"""
import shutil, subprocess, sys, tempfile
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
PY = sys.executable
SIG = "(draft: dict[str, Any], ctx: dict[str, Any]) -> list[dict[str, str]]:\n"
M = [(f"lesson L{i} always passes", "ga/plan/lessons.py", f"def l{i}" + SIG, f"def l{i}" + SIG + "    return []\n")
     for i in range(1, 8)] + [
 ("card cap ignored (no limit)", "ga/plan/card.py", "          limit: int = CARD_MAX) -> Card:",
  "          limit: int = 10 ** 9) -> Card:"),
 ("card cap ignored (map never cut)", "ga/plan/card.py", "        if used + _b(ln) > max(room, 0):", "        if False:"),
 ("request cap raised (baseline GA40 rev 1)", "ga/plan/card.py", "REQUEST_MAX = 1500 ", "REQUEST_MAX = 1500 * 1000 "),
 ("truncation guard always cuts (baseline GA40 rev 1)", "ga/plan/card.py", "    if _b(s) <= limit:\n", "    if False:\n"),
 ("L5 ignores the default word (baseline GA40 rev 1)", "ga/plan/lessons.py",
  "if not _DEFAULT.search(text) or _SOURCED.search(text):", "if _SOURCED.search(text):"),
 ("several heads counted as one", "ga/plan/draft.py",
  'heads = len(doc["heads"]) if isinstance(doc.get("heads"), list) else 1', "heads = 1"),
 ("repair loop unbounded", "ga/plan/draft.py", "    for _ in range(MAX_REPAIRS):", "    while True:"),
 ("repair loop raised to 5", "ga/plan/draft.py", "MAX_REPAIRS = repair.MAX_REPAIRS", "MAX_REPAIRS = 5"),
 ("a draft mailed", "ga/plan/draft.py", "    d.path.write_text(render(d), encoding=\"utf-8\")\n",
  "    d.path.write_text(render(d), encoding=\"utf-8\")\n    from ..mailbox import Mailbox\n"
  "    Mailbox.send(None, \"baseline\", render(d))\n"),
]


def run(name, f=None, old=None, new=None):
    tmp = Path(tempfile.mkdtemp())
    for d in ("ga", "tests"):
        shutil.copytree(ROOT / d, tmp / d)
    shutil.copy(ROOT / "pyproject.toml", tmp / "pyproject.toml")
    if f is not None:
        p = tmp / f
        s = p.read_text()
        assert s.count(old) == 1, (name, s.count(old))
        p.write_text(s.replace(old, new))
    r = subprocess.run([PY, "-m", "unittest", "tests.test_ga40"], cwd=tmp,
                       capture_output=True, text=True, timeout=900)
    shutil.rmtree(tmp, ignore_errors=True)
    return r.returncode, (r.stderr.strip().splitlines() or ["?"])[-1]


code, last = run("control")  # the unmutated copy must pass, or a kill means nothing
print(f"control (no mutation): {'green' if code == 0 else 'RED'} ({last})", flush=True)
if code != 0:
    sys.exit(2)
ok = True
for name, f, old, new in M:
    code, last = run(name, f, old, new)
    killed = code != 0
    ok &= killed
    print(f"{'killed' if killed else 'SURVIVED'}: {name} ({last})", flush=True)
print("all killed" if ok else "SOME SURVIVED")
sys.exit(0 if ok else 1)
