"""Python tool steps for the ga gemini tests. GA_GEMINI_TOOL_LOG (env) logs each call, to show a tool is not run twice."""
import os


def _count(name):
    f = os.environ.get("GA_GEMINI_TOOL_LOG")
    if f:
        with open(f, "a") as h:
            h.write(name + "\n")


def add(a, b):
    _count("add")
    return {"sum": a + b}


def big(n):
    _count("big")
    return "x" * n
