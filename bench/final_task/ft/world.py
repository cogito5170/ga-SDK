"""Tasks T1-T5 (FINAL_TASK section 2): fixtures (3 nodes each) and deterministic graders. No LLM grading anywhere."""
from __future__ import annotations

import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from ga.net.state import State

HERE = Path(__file__).resolve().parents[1]
FIX = HERE / "fixtures"
IMG_CODE = "471926"
IMG_PATH = FIX / "t1_image.png"

_DISTRACT = [("svc.timeout_ms", "3000"), ("svc.log_level", "info"), ("db.pool_size", "12"), ("cache.ttl_s", "600"),
             ("ui.theme", "dark"), ("ci.runner", "linux-x64")]


@dataclass
class Node:
    name: str
    caps: list[str]
    facts: dict[str, tuple[str, str]] = field(default_factory=dict)  # ref -> (value, evidence id)
    evidence: dict[str, str] = field(default_factory=dict)           # evidence id -> text
    local_obs: dict[str, str] = field(default_factory=dict)          # refs the node can observe itself, with the result


@dataclass
class Task:
    id: str
    title: str
    question: str
    required: list[str]
    nodes: dict[str, Node]
    needs: list[str] = field(default_factory=list)                  # capabilities the answering node needs
    ref_needs: dict[str, dict] = field(default_factory=dict)        # ref -> {"capabilities": [...]} to observe it
    image: str | None = None
    ref_files: list[str] = field(default_factory=list)              # selective: the files the task is about
    observe_prompt: dict[str, str] = field(default_factory=dict)
    answer_kind: str = "text"                                       # text | patch
    grade: Callable[..., Any] = None  # (answer, tool_calls, peer_messages) -> (correct, unknown_correct | None)
    target: str = "N1"


def _node(name, caps, own=None, evidence=None, local=None, k=0):
    facts = {r: v for r, v in (own or {}).items()}
    for r, v in _DISTRACT[k:k + 4]:
        facts[r] = (v, f"e:{name.lower()}-cfg-{r}")
    ev = dict(evidence or {})
    for r, (v, e) in facts.items():
        ev.setdefault(e, f"{name} config snapshot: {r} = {v}")
    return Node(name, caps, facts, ev, local or {})


def build_state(node: Node, required: list[str]) -> State:
    st = State(node.name)
    for ref, (val, evid) in node.facts.items():
        st.observe(ref, val, evid, "self", [ref])
    return st


# ---- graders ---------------------------------------------------------------------------------------------------------

def _s(a: Any) -> str:
    return str(a if a is not None else "").strip()


def grade_t1(a, tools, peers):
    return _s(a).replace(" ", "") == IMG_CODE, None


def grade_t3(a, tools, peers):
    s = _s(a)
    return ("8081" in s and "9090" not in s), None


def grade_t4(a, tools, peers):
    ok = _s(a) == "5" and tools == 0 and peers == 0
    return ok, None


def grade_t5(a, tools, peers):
    u = _s(a).upper() == "UNKNOWN"
    return u, u


def grade_t2(a, tools, peers):
    """Append the answer's function and test to the fixture copy, run the hidden check in a subprocess."""
    if not isinstance(a, dict) or not isinstance(a.get("function"), str) or not isinstance(a.get("test"), str):
        return False, None
    d = Path(tempfile.mkdtemp(prefix="ft1_t2_"))
    try:
        for f in ("textutil.py", "test_textutil.py"):
            shutil.copy(FIX / "t2" / f, d / f)
        with (d / "textutil.py").open("a", encoding="utf-8") as f:
            f.write("\n\n" + a["function"].strip("\n") + "\n")
        with (d / "test_textutil.py").open("a", encoding="utf-8") as f:
            f.write("\n\n" + a["test"].strip("\n") + "\n")
        p = subprocess.run([sys.executable, str(FIX / "t2" / "hidden_check.py"), str(d)], capture_output=True, text=True,
                           timeout=60)
        return (p.returncode == 0 and "HIDDEN_OK" in p.stdout), None
    except (OSError, subprocess.TimeoutExpired):
        return False, None
    finally:
        shutil.rmtree(d, ignore_errors=True)


# ---- the five tasks ---------------------------------------------------------------------------------------------------

def make_tasks() -> dict[str, Task]:
    t: dict[str, Task] = {}
    t["T1"] = Task(
        "T1", "capability gap",
        f"What is the 6-digit code shown in the image {IMG_PATH.name}? (ref img.code). Node N1 has no image input.",
        ["img.code"],
        {"N1": _node("N1", ["text"], k=0), "N2": _node("N2", ["text", "vision"], local={"img.code": IMG_CODE}, k=1),
         "N3": _node("N3", ["text"], k=2)},
        needs=["vision"], ref_needs={"img.code": {"capabilities": ["vision"]}}, image=str(IMG_PATH),
        observe_prompt={"img.code": "Read the 6-digit code shown in the attached image. Answer with the digits only."},
        grade=grade_t1)
    t["T2"] = Task(
        "T2", "repo change",
        "In the repository, add a function `slugify(text)` to textutil.py (lower-case; every run of non-alphanumeric "
        "characters becomes one '-'; no leading or trailing '-') and add one test `test_slugify` to test_textutil.py "
        "(plain function, like the others). Answer with answer = {\"function\": \"<the def slugify source>\", "
        "\"test\": \"<the def test_slugify source>\"} (JSON strings; the harness appends them to the files).",
        [], {"N1": _node("N1", ["text", "repo"], k=0), "N2": _node("N2", ["text"], k=1), "N3": _node("N3", ["text"], k=2)},
        needs=["repo"], ref_files=["textutil.py", "test_textutil.py"], answer_kind="patch", grade=grade_t2)
    ev_old = "docs/ops.md (snapshot dated 2026-03-01): 'the service listens on port 9090'"
    ev_new = "git log config/service.yaml, commit 3fa9c1 dated 2026-09-30: 'move service to port 8081' (current file: port: 8081)"
    t["T3"] = Task(
        "T3", "contradiction",
        "On which port does the service listen now? (ref svc.port). Two nodes hold different values; decide from their "
        "evidence, or answer UNKNOWN if the evidence does not settle it.",
        ["svc.port"],
        {"N1": _node("N1", ["text"], k=0),
         "N2": _node("N2", ["text"], {"svc.port": ("9090", "e:ops-doc-0301")}, {"e:ops-doc-0301": ev_old}, k=1),
         "N3": _node("N3", ["text"], {"svc.port": ("8081", "e:git-3fa9c1")}, {"e:git-3fa9c1": ev_new}, k=2)},
        grade=grade_t3)
    t["T4"] = Task(
        "T4", "already known",
        "What is the retry limit of the service? (ref svc.retry_limit)",
        ["svc.retry_limit"],
        {"N1": _node("N1", ["text"], {"svc.retry_limit": ("5", "e:cfg-retry")},
                     {"e:cfg-retry": "config/service.yaml line 14: retry_limit: 5 (read 2026-10-04)"}, k=0),
         "N2": _node("N2", ["text"], {"svc.retry_limit": ("5", "e:cfg-retry")},
                     {"e:cfg-retry": "config/service.yaml line 14: retry_limit: 5 (read 2026-10-04)"}, k=1),
         "N3": _node("N3", ["text"], k=2)},
        grade=grade_t4)
    t["T5"] = Task(
        "T5", "no information",
        "What is the monthly request quota of the billing region eu-north? (ref billing.region_quota)",
        ["billing.region_quota"],
        {"N1": _node("N1", ["text"], k=0), "N2": _node("N2", ["text"], k=1), "N3": _node("N3", ["text"], k=2)},
        grade=grade_t5)
    return t


def ensure_image() -> Path:
    from .png import digits_png
    if not IMG_PATH.exists():
        IMG_PATH.write_bytes(digits_png(IMG_CODE))
    return IMG_PATH
