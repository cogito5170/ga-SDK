"""tokens.json (W3C DTCG, the source) -> tokens.css (custom properties). No other input, no build tool.

    python -m ga.console.tokens            write ga/console/static/tokens.css
    python -m ga.console.tokens --check    exit 1 when tokens.css is not the generator's output

Fluid type is Utopia-style: each step has a min (at min-vw) and a max (at max-vw) in px and becomes
clamp(min, intercept + slope*vw, max). Nested groups flatten with '-': color.state.ok -> --state-ok.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

STATIC = Path(__file__).resolve().parent / "static"
SRC, OUT = STATIC / "tokens.json", STATIC / "tokens.css"
HEAD = "/* generated from tokens.json by `python -m ga.console.tokens` -- do not edit by hand */\n"


def _px(v: str) -> float:
    assert v.endswith("px"), v
    return float(v[:-2])


def _fluid(lo: float, hi: float, vw0: int, vw1: int) -> str:
    slope = (hi - lo) / (vw1 - vw0) * 100
    icpt = lo - slope * vw0 / 100
    return f"clamp({lo / 16:.4f}rem, {icpt / 16:.4f}rem + {slope:.4f}vw, {hi / 16:.4f}rem)"


def _leaves(node: dict, prefix: str = ""):
    for k, v in node.items():
        if k.startswith("$"):
            continue
        name = f"{prefix}-{k}" if prefix else k
        if isinstance(v, dict) and "$value" in v:
            yield name, v
        elif isinstance(v, dict):
            yield from _leaves(v, name)


def _family(names: list) -> str:
    generic = {"sans-serif", "serif", "monospace", "system-ui"}
    return ",".join(n if n in generic or " " not in n else f'"{n}"' for n in names)


def css(t: dict) -> str:
    fl = t["$extensions"]["ga.console"]["fluid"]
    out = []
    for name, v in _leaves(t["color"]):
        out.append(f"--{name}:{v['$value']}")
    for name, v in _leaves(t["fontFamily"]):
        out.append(f"--font-{name}:{_family(v['$value'])}")
    for name, v in _leaves(t["fontSize"]):
        f = v["$extensions"]["ga.console"]
        out.append(f"--{name}:{_fluid(_px(f['min']), _px(f['max']), fl['min-vw'], fl['max-vw'])}")
    for name, v in _leaves(t["space"]):
        out.append(f"--{name}:{v['$value']}")
    for name, v in _leaves(t["duration"]):
        out.append(f"--{name}:{v['$value']}")
    for name, v in _leaves(t["cubicBezier"]):
        out.append(f"--{name}:cubic-bezier({','.join(str(x) for x in v['$value'])})")
    return HEAD + ":root{" + ";".join(out) + "}\n"


def load(path: Path = SRC) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def main(argv: "list[str] | None" = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    want = css(load())
    if "--check" in argv:
        ok = OUT.exists() and OUT.read_text(encoding="utf-8") == want
        print("tokens.css: up to date" if ok else "tokens.css: differs from tokens.json -- run python -m ga.console.tokens")
        return 0 if ok else 1
    OUT.write_text(want, encoding="utf-8")
    print(f"wrote {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
