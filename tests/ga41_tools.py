"""Python tools for tests/test_ga41.py (ga supervise's tool table): no network, no model."""
from pathlib import Path

ROOT = {"dir": "."}


def read_file(path: str) -> str:
    p = Path(ROOT["dir"]) / path
    if not p.exists():
        raise ValueError(f"no such file: {path}")
    return p.read_text(encoding="utf-8")


def add(a: int, b: int) -> int:
    return a + b


SECRET = ""


def leak() -> str:
    raise ValueError("bad token " + SECRET)
