"""``ga judge`` (CMD-GA30): the mechanical verdict steps as a deterministic script, so a hub spends model tokens only on
deviations, proposals, failures and claim mismatches. No model call, no network of its own beyond ``git fetch``.

Steps: (1) ga check the report head; (2) fetch the commits, fast-forward from base or a dry merge listing conflicts;
(3) empty venv, pip install ``<dist>[extras] @ git+file://...@sha``, pip list of the package and its deps, pip check;
(4) fresh clone at the sha, the repo's test command with PYTHONDONTWRITEBYTECODE=1 and the network blocked, counts;
(5) k >= 1 baseline mutations from a spec file (seeded, the seed is recorded), each expected to make a named test fail;
(6) the report's claims (sha, test counts, versions) against what was measured.

The per-repo config is JSON (default ``<repo>/.ga-judge.json``, read from the local clone, never from the report)::

    {"dist": "pkg", "extras": ["x"], "test": ["{python}", "-m", "unittest", "discover"],
     "test_named": ["{python}", "-m", "unittest", "{tests}"],      # optional; mutation runs; else test + names
     "pinned": ["dep"], "pip_args": ["--no-index"], "pythonpath": ["."], "python": "/usr/bin/python3",
     "repo": "owner/name", "timeout": 1800}

Beyond pip (CMD-GA34 S6): ``"setup": [["npm", "ci"], ...]`` instead of ``dist`` runs each argv in the fresh clone
(network allowed: it installs) and skips step (3); ``"junit": "<path in the clone>"`` reads the counts and the failing
test ids from that JUnit XML file (vitest, jest, playwright and pytest --junitxml all write one). Without ``junit`` the
output is read by the unittest / pytest parsers and the vitest, jest and playwright summary parsers.

The mutation spec is a JSON list of ``{"file", "find", "replace", "tests": [names...], "id"?}``.
"""
from __future__ import annotations

import json
import os
import random
import re
import shutil
import subprocess
import sys
import tempfile
import venv
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from .forms import FormError, dump_wire, hard, parse_text, validate, wire_problems

RANK = {"success": 0, "partial": 1, "insufficient": 2, "failure": 3, "blocked": 4}
REF_RE = re.compile(r"^(?P<repo>[^@\s:]+)@(?P<sha>[0-9a-f]{7,40}):(?P<path>.+)$")
CAUSE_OF_BLOCKER = {"env": "environment", "permission": "environment", "credential": "environment",
                    "budget": "environment", "dependency": "dependency", "design": "requirement"}

_GUARD = '''"""ga judge: block non-loopback network use in the python under test."""
import ipaddress, socket
_real_connect, _real_gai, _real_sendto = socket.socket.connect, socket.getaddrinfo, socket.socket.sendto

def _loop(addr):
    try:
        return ipaddress.ip_address(addr).is_loopback
    except ValueError:
        return addr in ("localhost",)

def _check(sock, address):
    if sock.family in (socket.AF_INET, socket.AF_INET6) and not _loop(address[0]):
        raise OSError("network blocked by ga judge: %r" % (address[0],))

def connect(self, address):
    _check(self, address)
    return _real_connect(self, address)

def sendto(self, data, *a):
    _check(self, a[-1])
    return _real_sendto(self, data, *a)

def getaddrinfo(host, *a, **k):
    if host is not None and not _loop(host if isinstance(host, str) else host.decode()):
        raise socket.gaierror("network blocked by ga judge: %r" % (host,))
    return _real_gai(host, *a, **k)

socket.socket.connect, socket.socket.sendto, socket.getaddrinfo = connect, sendto, getaddrinfo
'''


class JudgeError(Exception):
    pass


@dataclass
class Judgement:
    cls: str = "success"
    cause: str | None = None
    subclass: str | None = None
    heads: dict[str, str] = field(default_factory=dict)
    tests: dict[str, dict[str, int]] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    claims: list[str] = field(default_factory=list)
    needs: list[str] = field(default_factory=list)
    seed: int = 0
    sha: str = ""
    repo: str = ""
    ff: bool = False
    base: str = ""
    report_ref: str = ""
    directive: str = ""
    failing: list[str] = field(default_factory=list)      # tests that fail on the report's sha and not on the base head
    preexisting: list[str] = field(default_factory=list)  # tests that fail on the report's sha and also on the base head

    def worse(self, cls: str, cause: str, sub: str | None = None) -> None:
        if RANK[cls] > RANK[self.cls]:
            self.cls, self.cause, self.subclass = cls, cause, sub

    @property
    def clean(self) -> bool:
        return self.cls == "success" and not self.needs


def run(argv, cwd=None, env=None, timeout=None, check=False, input=None) -> subprocess.CompletedProcess:
    try:
        p = subprocess.run(argv, cwd=cwd, env=env, capture_output=True, text=True, timeout=timeout, input=input)
    except subprocess.TimeoutExpired as e:
        p = subprocess.CompletedProcess(argv, 124, "", "timeout")
    except OSError as e:
        p = subprocess.CompletedProcess(argv, 127, "", str(e))
    if check and p.returncode:
        raise JudgeError(f"{' '.join(map(str, argv[:4]))}: {(p.stderr or p.stdout).strip()[-300:]}")
    return p


def git(repo, *args, **kw) -> subprocess.CompletedProcess:
    return run(["git", "-C", str(repo), *args], **kw)


# ---- test output counts

def parse_counts(out: str) -> dict[str, int] | None:
    """passed/failed/skipped from unittest ("Ran N tests", "FAILED (failures=a, errors=b, skipped=c)") or pytest
    ("3 passed, 1 failed, 2 skipped in 0.1s"); None when neither shape is found."""
    m = re.search(r"^Ran (\d+) tests? in ", out, re.M)
    if m:
        n = int(m.group(1))
        fields = dict((k, int(v)) for k, v in re.findall(r"(failures|errors|skipped|expected failures|unexpected successes)=(\d+)", out.split("Ran ")[-1]))
        failed = fields.get("failures", 0) + fields.get("errors", 0) + fields.get("unexpected successes", 0)
        skipped = fields.get("skipped", 0)
        return {"passed": max(n - failed - skipped, 0), "failed": failed, "skipped": skipped}
    m = re.search(r"^=+ (.*?) in [\d.]+s", out, re.M) or re.search(r"^(\d+ (?:passed|failed|error|skipped).*?) in [\d.]+s", out, re.M)
    if m:
        c = {k: int(v) for v, k in re.findall(r"(\d+) (passed|failed|errors?|skipped)", m.group(1))}
        return {"passed": c.get("passed", 0), "failed": c.get("failed", 0) + c.get("error", 0) + c.get("errors", 0),
                "skipped": c.get("skipped", 0)}
    return _js_counts(out)


_ANSI = re.compile(r"\x1b\[[0-9;]*m")
_VITEST = re.compile(r"^\s*Tests\s{2,}(.*?)\s*\((\d+)\)\s*$", re.M)  # " Tests  1 failed | 11 passed (12)"
_JEST = re.compile(r"^Tests:\s+(.*?\d+ total)\s*$", re.M)  # "Tests:       1 failed, 11 passed, 12 total"
_PW = re.compile(r"^\s+(\d+) (passed|failed|flaky|skipped|did not run|interrupted)(?: \([\d.]+m?s\))?\s*$", re.M)


def _js_counts(out: str) -> dict[str, int] | None:
    """vitest, jest or playwright summary lines (CMD-GA34 S6); None when none is found."""
    out = _ANSI.sub("", out)
    for rx in (_VITEST, _JEST):
        m = rx.search(out)
        if m:
            c = {k: int(v) for v, k in re.findall(r"(\d+) (passed|failed|skipped|todo|pending)", m.group(1))}
            return {"passed": c.get("passed", 0), "failed": c.get("failed", 0),
                    "skipped": c.get("skipped", 0) + c.get("todo", 0) + c.get("pending", 0)}
    rows = _PW.findall(out)
    if any(k in ("passed", "failed") for _, k in rows):
        c: dict[str, int] = {}
        for v, k in rows:
            c[k] = c.get(k, 0) + int(v)
        return {"passed": c.get("passed", 0) + c.get("flaky", 0), "failed": c.get("failed", 0) + c.get("interrupted", 0),
                "skipped": c.get("skipped", 0) + c.get("did not run", 0)}
    return None


def parse_junit(path: Path) -> tuple[dict[str, int], list[str]] | None:
    """(counts, failing test ids "classname.name") from a JUnit XML file; None when it is missing or not JUnit."""
    import xml.etree.ElementTree as ET
    try:
        root = ET.parse(str(path)).getroot()
    except (OSError, ET.ParseError):
        return None
    if root.tag not in ("testsuites", "testsuite"):
        return None
    c, failing = {"passed": 0, "failed": 0, "skipped": 0}, set()
    for tc in root.iter("testcase"):
        tid = ".".join(x for x in (tc.get("classname"), tc.get("name")) if x)
        if tc.find("failure") is not None or tc.find("error") is not None:
            c["failed"] += 1
            failing.add(tid)
        elif tc.find("skipped") is not None:
            c["skipped"] += 1
        else:
            c["passed"] += 1
    return c, sorted(failing)


_UT_RE = re.compile(r"^(?:FAIL|ERROR): (\S.*?)(?: \(\w+=.*\))?\s*$", re.M)
_PT_RE = re.compile(r"^(?:FAILED|ERROR) (\S+?::\S+|\S+\.py)(?: - .*)?$", re.M)
_VT_RE = re.compile(r"^\s*(?:FAIL|×|✗)\s+(\S+\.[cm]?[jt]sx? > .+?)(?: \d+m?s)?\s*$", re.M)  # vitest
_PWF_RE = re.compile(r"^\s+\d+\) (\[[^\]]+\] › .+?)\s*$", re.M)  # playwright "  1) [chromium] › a.spec.ts:3:5 › t"


def failing_tests(out: str) -> list[str]:
    """The ids of the failing or erroring tests in a unittest or pytest run, sorted, each once. unittest: the dotted id
    in "FAIL: test_x (pkg.mod.Class.test_x)" (a subtest suffix is dropped); pytest: the node id in the short summary."""
    ids: set[str] = set()
    for m in _UT_RE.finditer(out):
        t = m[1]
        inner = re.search(r"\(([\w.]+)\)$", t)
        ids.add(inner[1] if inner else t)
    for m in _PT_RE.finditer(out):
        ids.add(m[1])
    clean = _ANSI.sub("", out)
    for rx in (_VT_RE, _PWF_RE):
        ids.update(m[1] for m in rx.finditer(clean))
    return sorted(ids)


def split_failures(head: list[str], base: list[str], head_failed: int) -> tuple[list[str], list[str], int]:
    """(new, pre-existing, unnamed): a head failure is pre-existing only when the base head fails the same test by name.
    Failures the output counts but does not name cannot be matched, so they count as new."""
    base_set = set(base)
    new = [t for t in head if t not in base_set]
    pre = [t for t in head if t in base_set]
    return new, pre, max(head_failed - len(head), 0)


# ---- the pieces

def load_config(repo: Path, path: str | None) -> dict[str, Any]:
    p = Path(path) if path else repo / ".ga-judge.json"
    try:
        cfg = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise JudgeError(f"judge config {p}: {e}") from None
    if not isinstance(cfg, dict) or not isinstance(cfg.get("test"), list):
        raise JudgeError(f"judge config {p}: needs \"dist\" (or \"setup\") and a \"test\" argv list")
    setup = cfg.get("setup")
    if setup is not None and not (isinstance(setup, list) and all(
            isinstance(a, list) and a and all(isinstance(x, str) for x in a) for a in setup)):
        raise JudgeError(f"judge config {p}: \"setup\" is a list of argv lists")
    if not cfg.get("dist") and setup is None:
        raise JudgeError(f"judge config {p}: needs \"dist\" (or \"setup\") and a \"test\" argv list")
    if "junit" in cfg and not isinstance(cfg["junit"], str):
        raise JudgeError(f"judge config {p}: \"junit\" is a path in the clone")
    return cfg


def load_mutations(path: str | None) -> list[dict[str, Any]]:
    if not path:
        return []
    try:
        spec = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise JudgeError(f"mutation spec {path}: {e}") from None
    if not isinstance(spec, list) or not all(isinstance(m, dict) and {"file", "find", "replace", "tests"} <= m.keys()
                                             and isinstance(m["tests"], list) for m in spec):
        raise JudgeError(f"mutation spec {path}: a list of {{file, find, replace, tests}}")
    return spec


def test_names(names: list[str], argv: list[str], root: Path) -> list[str]:
    """CMD-GA39 S2: a spec's test names may be file paths (tests/test_x.py[::T::t]) or module paths (tests.test_x[.T.t]);
    each becomes the form the test command takes: module paths for unittest, file paths for pytest."""
    runner = "pytest" if any("pytest" in a for a in argv) else "unittest" if any("unittest" in a for a in argv) else ""
    out = []
    for n in names:
        if runner == "unittest" and (n.endswith(".py") or ".py::" in n or "/" in n):
            path, _, rest = n.partition("::")
            mod = path[:-3] if path.endswith(".py") else path
            out.append(".".join([mod.strip("/").replace("/", "."), *[x for x in rest.split("::") if x]]))
        elif runner == "pytest" and "/" not in n and "::" not in n and not n.endswith(".py"):
            parts = n.split(".")
            for i in range(len(parts), 0, -1):
                if (root / Path(*parts[:i])).with_suffix(".py").is_file():
                    out.append("::".join(["/".join(parts[:i]) + ".py", *parts[i:]]))
                    break
            else:
                out.append(n)
        else:
            out.append(n)
    return out


def read_report(ref: str, repo: Path, remote: str) -> str:
    m = REF_RE.match(ref) if not Path(ref).exists() else None
    if not m:
        try:
            return Path(ref).read_text(encoding="utf-8")
        except OSError as e:
            raise JudgeError(f"report {ref}: {e}") from None
    sha, path = m["sha"], m["path"]
    fetch(repo, remote, sha)
    p = git(repo, "show", f"{sha}:{path}")
    if p.returncode:
        raise JudgeError(f"report {ref}: {p.stderr.strip()[-200:]}")
    return p.stdout


def have(repo: Path, sha: str) -> bool:
    return git(repo, "cat-file", "-e", f"{sha}^{{commit}}").returncode == 0


def fetch(repo: Path, remote: str, sha: str, branch: str | None = None) -> bool:
    if have(repo, sha):
        return True
    if branch:
        git(repo, "fetch", "--quiet", remote, branch)
    if not have(repo, sha):
        git(repo, "fetch", "--quiet", remote, sha)
    return have(repo, sha)


def pick_commit(head: dict[str, Any], cfg: dict[str, Any], repo: Path) -> dict[str, str]:
    commits = [c for c in head.get("commits") or [] if isinstance(c, dict)]
    names = {cfg.get("repo"), repo.resolve().name, cfg.get("dist")} - {None}
    mine = [c for c in commits if c.get("repo") in names or str(c.get("repo", "")).rsplit("/", 1)[-1] in names]
    if len(mine) == 1:
        return mine[0]
    if len(commits) == 1:
        return commits[0]
    raise JudgeError(f"report names {len(commits)} commits and {len(mine)} match this repo: set \"repo\" in the judge config")


def merge_state(repo: Path, base_ref: str, sha: str) -> tuple[bool, list[str]]:
    """(fast-forward from base?, conflicts of a dry merge when not)."""
    if git(repo, "merge-base", "--is-ancestor", base_ref, sha).returncode == 0:
        return True, []
    p = git(repo, "merge-tree", "--write-tree", "--name-only", "--no-messages", base_ref, sha)
    if p.returncode == 0:
        return False, []
    lines = [x for x in p.stdout.splitlines()[1:] if x.strip()]
    return False, lines or [(p.stderr or "merge-tree failed").strip()[:200]]


def make_env(guard_dir: Path, pythonpath: list[str]) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "VIRTUAL_ENV")}
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTHONPATH"] = os.pathsep.join([str(guard_dir), *pythonpath])
    return env


_LO_UP = ("import fcntl,os,socket,struct,sys\n"
          "s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM)\n"
          "r=fcntl.ioctl(s,0x8913,struct.pack('16sH14x',b'lo',0))\n"  # SIOCGIFFLAGS
          "f=struct.unpack('16sH14x',r)[1]|1\n"  # IFF_UP
          "fcntl.ioctl(s,0x8914,struct.pack('16sH14x',b'lo',f))\n"  # SIOCSIFFLAGS
          "os.execvp(sys.argv[1],sys.argv[1:])\n")


def netless(argv: list[str]) -> tuple[list[str], str]:
    """argv wrapped in a network namespace (loopback up) when `unshare -rn` works here; the loopback-only guard is always on."""
    if shutil.which("unshare") and run(["unshare", "-rn", sys.executable, "-c", _LO_UP, "true"]).returncode == 0:
        return ["unshare", "-rn", sys.executable, "-c", _LO_UP, *argv], "unshare -rn (loopback up) + socket guard"
    return argv, "socket guard (no unshare)"


def expand(argv: list[str], python: str, tests: list[str] | None = None) -> list[str]:
    out: list[str] = []
    for a in argv:
        if a == "{tests}":
            out += tests or []
        else:
            out.append(a.replace("{python}", python))
    return out


def judge(report: str, repo: str | Path, base: str, *, mutations: str | None = None, seed: int | None = None, k: int = 1,
          config: str | None = None, remote: str = "origin", today: str | None = None) -> Judgement:
    repo = Path(repo).resolve()
    cfg = load_config(repo, config)
    j = Judgement(base=base, report_ref=report if REF_RE.match(report) else "")
    j.seed = random.SystemRandom().randrange(2**32) if seed is None else seed
    text = read_report(report, repo, remote)

    # (1) ga check on the head: a hard problem is insufficient, cause measurement
    try:
        head, _ = parse_text(text)
        probs = validate(head) + wire_problems(text)
    except FormError as e:
        probs, head = e.problems, {}
    if hard(probs):
        j.worse("insufficient", "measurement")
        j.notes += [f"ga check: {p}" for p in hard(probs)][:5]
        j.notes.append("report head refused by ga check; nothing measured")
        return j
    if head.get("schema") != "report/2":
        j.worse("insufficient", "measurement")
        j.notes.append(f"report head is {head.get('schema')}, not report/2; nothing measured")
        return j
    j.notes.append("ga check: head ok")
    j.directive = ",".join(h.get("id", "") for h in head.get("handled", []))
    for key, label in (("deviations", "deviation"), ("proposals", "proposal")):
        j.needs += [f"{label}: {x}" for x in head.get(key) or []]
    for it in head.get("items") or []:
        if it["state"] in ("unmet", "blocked"):
            j.needs.append(f"item {it['id']} is {it['state']}")
            blk = next(iter(head.get("blockers") or []), None)
            if it["state"] == "blocked":
                j.worse("blocked", CAUSE_OF_BLOCKER.get(blk["kind"], "requirement") if blk else "requirement")
            else:
                j.worse("partial", "requirement")
    for b in head.get("blockers") or []:
        j.needs.append(f"blocker {b['kind']}: {b['what']}")

    # (2) commits, fast-forward or conflicts
    c = pick_commit(head, cfg, repo)
    sha_claim = c["sha"]
    if not fetch(repo, remote, sha_claim, c.get("branch")):
        j.worse("insufficient", "measurement")
        j.notes.append(f"commit {sha_claim} not fetchable from {remote}")
        return j
    sha = git(repo, "rev-parse", f"{sha_claim}^{{commit}}", check=True).stdout.strip()
    j.sha, j.repo = sha, c.get("repo") or cfg.get("repo") or repo.name
    j.heads = {j.repo: sha}
    git(repo, "fetch", "--quiet", remote, base)
    base_ref = f"{remote}/{base}" if git(repo, "rev-parse", "--verify", "-q", f"{remote}/{base}").returncode == 0 else base
    if git(repo, "rev-parse", "--verify", "-q", base_ref).returncode:
        raise JudgeError(f"base {base!r} not found")
    j.ff, conflicts = merge_state(repo, base_ref, sha)
    if j.ff:
        j.notes.append(f"{sha[:7]} fast-forwards {base}")
    else:
        j.needs.append(f"non-ff: {sha[:7]} does not fast-forward {base}; dry merge " +
                       (f"conflicts in {', '.join(conflicts)}" if conflicts else "is clean"))
    if sha[: len(sha_claim)] != sha_claim:
        j.claims.append(f"commit sha {sha_claim} resolved to {sha}")
    j.claims.append(f"commit sha {sha[:7]} fetched: ok")

    work = Path(tempfile.mkdtemp(prefix="ga-judge-"))
    try:
        _measure(j, cfg, repo, sha, head, work, mutations, k, git(repo, "rev-parse", f"{base_ref}^{{commit}}").stdout.strip())
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return j


def judge_commit(repo: str | Path, sha: str, base: str, *, config: str | None = None, mutations: str | None = None,
                 seed: int | None = None, k: int = 1, remote: str | None = None) -> Judgement:
    """Steps (2)-(5) on a local commit, no report (CMD-GA34 S5: the pool judges a node's branch head against the
    integration branch). Both are refs of the local repository ``repo``; nothing is fetched unless ``remote`` is
    given (CMD-GA39 S2, ``ga judge --sha``: any commit, a hub-side merge too; base is then ``remote/base``)."""
    repo = Path(repo).resolve()
    cfg = load_config(repo, config)
    j = Judgement(base=base)
    j.seed = random.SystemRandom().randrange(2**32) if seed is None else seed
    if remote:
        if git(repo, "rev-parse", "--verify", "-q", f"{sha}^{{commit}}").returncode and not fetch(repo, remote, sha):
            raise JudgeError(f"commit {sha} not found locally nor fetchable from {remote}")
        git(repo, "fetch", "--quiet", remote, base)
        if git(repo, "rev-parse", "--verify", "-q", f"{remote}/{base}").returncode == 0:
            base = f"{remote}/{base}"
    for ref in (sha, base):
        if git(repo, "rev-parse", "--verify", "-q", f"{ref}^{{commit}}").returncode:
            raise JudgeError(f"{ref!r} is not a commit in {repo}")
    sha = git(repo, "rev-parse", f"{sha}^{{commit}}", check=True).stdout.strip()
    base_sha = git(repo, "rev-parse", f"{base}^{{commit}}", check=True).stdout.strip()
    j.sha, j.repo = sha, cfg.get("repo") or repo.name
    j.heads = {j.repo: sha}
    j.ff, conflicts = merge_state(repo, base_sha, sha)
    if not j.ff:
        j.worse("failure", "integration")
        j.notes.append(f"non-ff: {sha[:7]} does not fast-forward {base}" + (f"; conflicts in {', '.join(conflicts)}"
                                                                             if conflicts else ""))
        return j
    work = Path(tempfile.mkdtemp(prefix="ga-judge-"))
    try:
        _measure(j, cfg, repo, sha, {}, work, mutations, k, base_sha)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return j


def _measure(j: Judgement, cfg: dict[str, Any], repo: Path, sha: str, head: dict[str, Any], work: Path,
             mutations: str | None, k: int, base_sha: str = "") -> None:
    timeout = cfg.get("timeout", 1800)
    # a private bare copy that holds the sha on a ref, so pip and the clone can reach it without touching the repo
    src = work / "src.git"
    git(work, "init", "--bare", "--quiet", "-b", "judge", str(src), check=True)
    git(src, "fetch", "--quiet", str(repo), sha, check=True)  # a fetch (not a push) also works from a shallow repo
    git(src, "update-ref", "refs/heads/judge", sha, check=True)

    if cfg.get("setup") is not None:  # CMD-GA34 S6: no pip dist; the setup argv lists install in the clone
        installed, vpy = False, sys.executable
        j.notes.append("setup mode: no pip install (" + "; ".join(" ".join(a) for a in cfg["setup"]) + ")"
                       if cfg["setup"] else "setup mode: nothing to install")
    else:
        installed, vpy = _pip_step(j, cfg, src, sha, head, work, timeout)

    # (4) fresh clone, the repo's test command, network blocked
    clone = work / "clone"
    git(work, "clone", "--quiet", "--no-checkout", str(src), str(clone), check=True)
    git(clone, "checkout", "--quiet", "--detach", sha, check=True)
    guard = work / "guard"
    guard.mkdir()
    (guard / "sitecustomize.py").write_text(_GUARD, encoding="utf-8")
    py = vpy if installed or cfg.get("setup") is not None else sys.executable

    def setup_in(cwd: Path) -> bool:
        for a in cfg.get("setup") or []:
            sp = run(expand(a, py), cwd=cwd, timeout=timeout)
            if sp.returncode:
                j.worse("failure", "dependency")
                j.notes.append(f"setup {' '.join(a)} failed (exit {sp.returncode}): "
                               f"{(sp.stderr or sp.stdout).strip().splitlines()[-1:] or ['?']}"[:200])
                return False
        return True

    def run_tests(argv: list[str], cwd: Path = clone) -> tuple[subprocess.CompletedProcess, str]:
        if cfg.get("junit"):  # never read a file the commit carries or an earlier run left
            (cwd / cfg["junit"]).unlink(missing_ok=True)
        wrapped, how = netless(expand(argv, py))
        env_here = make_env(guard, [str(cwd / x) for x in cfg.get("pythonpath", [])])
        return run(wrapped, cwd=cwd, env=env_here, timeout=timeout), how

    def counted(p: subprocess.CompletedProcess, cwd: Path = clone) -> tuple[dict[str, int] | None, list[str]]:
        """(counts, failing ids): from the JUnit file when the config names one, else from the output."""
        if cfg.get("junit"):
            got = parse_junit(cwd / cfg["junit"])
            return got if got else (None, [])
        out = p.stdout + "\n" + p.stderr
        return parse_counts(out), failing_tests(out)

    if not setup_in(clone):
        return
    p, how = run_tests(cfg["test"])
    counts, names = counted(p)
    j.notes.append(f"tests run in fresh clone, PYTHONDONTWRITEBYTECODE=1, network blocked ({how})")
    if counts is None:
        j.worse("insufficient", "measurement")
        j.notes.append(f"tests: no counts in {'JUnit file ' + cfg['junit'] if cfg.get('junit') else 'output'} "
                       f"(exit {p.returncode})")
    else:
        j.tests[j.repo] = counts
        j.notes.append(f"tests: {counts['passed']} passed, {counts['failed']} failed, {counts['skipped']} skipped (exit {p.returncode})")
        if counts["failed"] or p.returncode:
            base_names: list[str] = []
            if base_sha and names:
                base_names = _base_failures(j, cfg, repo, src, base_sha, work, run_tests, setup_in, counted)
            new, pre, unnamed = split_failures(names, base_names, counts["failed"])
            j.failing, j.preexisting = new, pre
            if new:
                j.notes.append("failing tests (new): " + ", ".join(new[:20]) + (f" (+{len(new) - 20} more)" if len(new) > 20 else ""))
            if pre:
                j.notes.append(f"failing tests (pre-existing, also red on base {base_sha[:7]}; not caused by the report): "
                               + ", ".join(pre[:20]) + (f" (+{len(pre) - 20} more)" if len(pre) > 20 else ""))
            if unnamed:
                j.notes.append(f"{unnamed} failure(s) the output counts but does not name: counted as new")
            if new or unnamed or not names:  # no name at all (a crash, an unknown runner): nothing to excuse
                j.worse("failure", "implementation")
        want = head.get("tests")
        if isinstance(want, dict):
            diff = [f"{k} claimed {want[k]}, measured {counts[k]}" for k in ("passed", "failed", "skipped")
                    if k in want and want[k] != counts[k]]
            if diff:
                j.claims.append("tests: " + "; ".join(diff))
                j.needs.append("claim mismatch: tests " + "; ".join(diff))
            else:
                j.claims.append("tests match")

    # (5) baseline mutations ("auto": generated from base...sha, CMD-GA39 S1)
    if mutations == "auto":
        from .verify.mutate import MutateError, mutations as generate
        try:
            spec = generate(repo, base_sha, sha) if base_sha else []
        except MutateError as e:
            spec = []
            j.notes.append(f"mutations auto: {e}"[:200])
        j.notes.append(f"mutations auto: {len(spec)} generated from {base_sha[:7]}...{sha[:7]}; picking from the top 15")
        spec = spec[:max(15, k)]
    else:
        spec = load_mutations(mutations)
    if not spec:
        j.needs.append("no mutation run: no spec given")
        return
    rng = random.Random(j.seed)
    chosen = rng.sample(spec, min(max(k, 1), len(spec)))
    j.notes.append(f"mutation seed {j.seed}, picked {len(chosen)} of {len(spec)}")
    named = cfg.get("test_named") or [*cfg["test"], "{tests}"]
    for i, mu in enumerate(chosen):
        label = mu.get("id") or f"{mu['file']}:{mu['find'][:30]!r}"
        path = clone / mu["file"]
        try:
            orig = path.read_text(encoding="utf-8")
        except OSError:
            orig = ""
        if orig.count(mu["find"]) != 1:
            j.needs.append(f"mutation {label} does not apply (find text found {orig.count(mu['find'])} times); spec is stale")
            j.notes.append(f"mutation {label}: not applied")
            continue
        path.write_text(orig.replace(mu["find"], mu["replace"]), encoding="utf-8")
        try:
            names = test_names(list(mu["tests"]), named, clone)
            p, _ = run_tests(expand(named, py, names) if "{tests}" in named and names else
                             named if "{tests}" not in named else cfg["test"])
        finally:
            path.write_text(orig, encoding="utf-8")
            git(clone, "checkout", "--quiet", "--", mu["file"])
        c, _ = counted(p)
        if p.returncode and c is not None and c["failed"]:
            j.notes.append(f"mutation {label}: killed ({c['failed']} failed)")
        elif p.returncode and c is None:
            j.notes.append(f"mutation {label}: killed (exit {p.returncode}, no counts)")
        else:
            j.worse("insufficient", "measurement")
            j.notes.append(f"mutation {label}: SURVIVED (exit {p.returncode})")
            j.needs.append(f"mutation {label} survived: tests {' '.join(mu['tests'])} do not cover it")


def _pip_step(j: Judgement, cfg: dict[str, Any], src: Path, sha: str, head: dict[str, Any], work: Path,
              timeout: Any) -> tuple[bool, str]:
    """(3) empty venv, pip install of the dist at the sha, pip list, pip check: (installed?, the venv's python)."""
    vdir = work / "venv"
    venv.create(vdir, with_pip=True, clear=True, symlinks=os.name != "nt")
    vpy = str(vdir / ("Scripts/python.exe" if os.name == "nt" else "bin/python"))
    dist, extras = cfg["dist"], cfg.get("extras") or []
    req = f"{dist}{'[' + ','.join(extras) + ']' if extras else ''} @ git+file://localhost{src}@{sha}"  # pip needs a host in the URL
    p = run([vpy, "-m", "pip", "install", "--quiet", "--disable-pip-version-check", *cfg.get("pip_args", []), req],
            timeout=timeout)
    installed = p.returncode == 0
    if not installed:
        j.worse("failure", "implementation")
        j.notes.append(f"pip install failed: {(p.stderr or p.stdout).strip().splitlines()[-1:] or ['?']}"[:200])
    else:
        show = run([vpy, "-m", "pip", "show", dist]).stdout
        req_line = next((x for x in show.splitlines() if x.startswith("Requires:")), "Requires:")
        names = {n.strip().lower().replace("_", "-") for n in req_line.split(":", 1)[1].split(",") if n.strip()}
        names |= {dist.lower().replace("_", "-"), *[x.lower().replace("_", "-") for x in cfg.get("pinned", [])]}
        pl = json.loads(run([vpy, "-m", "pip", "list", "--format=json", "--disable-pip-version-check"]).stdout or "[]")
        got = {x["name"].lower().replace("_", "-"): x["version"] for x in pl if x["name"].lower().replace("_", "-") in names}
        j.notes.append("pip list: " + ", ".join(f"{n}=={v}" for n, v in sorted(got.items())))
        pc = run([vpy, "-m", "pip", "check", "--disable-pip-version-check"])
        if pc.returncode:
            j.worse("failure", "dependency")
            j.notes.append(f"pip check failed: {pc.stdout.strip().splitlines()[0] if pc.stdout.strip() else pc.stderr.strip()[:150]}")
        else:
            j.notes.append("pip check: ok")
        measured = got.get(dist.lower().replace("_", "-"))
        for r in head.get("results") or []:
            m = re.match(r"^(?:version[: ](\S+)|(\S+?)[ _]version)$", str(r.get("name", "")))
            if m:
                want = (m[1] or m[2]).lower().replace("_", "-")
                have_v = got.get(want)
                if have_v is None or str(r["value"]) != have_v:
                    j.claims.append(f"version {want}: claimed {r['value']}, measured {have_v}")
                    j.needs.append(f"claim mismatch: version {want} claimed {r['value']}, measured {have_v}")
                else:
                    j.claims.append(f"version {want} {have_v}: ok")
        if measured is None:
            j.notes.append(f"{dist} missing from pip list")

    return installed, vpy


def _base_failures(j: Judgement, cfg: dict[str, Any], repo: Path, src: Path, base_sha: str, work: Path, run_tests,
                   setup_in=None, counted=None) -> list[str]:
    """The tests that fail on the base head: the same suite, the same venv and guards, a clone of the base sha."""
    if git(src, "fetch", "--quiet", str(repo), base_sha).returncode:
        j.notes.append(f"base {base_sha[:7]} not fetchable: failures not compared")
        return []
    bclone = work / "baseclone"
    git(work, "clone", "--quiet", "--no-checkout", str(src), str(bclone), check=True)
    git(bclone, "checkout", "--quiet", "--detach", base_sha, check=True)
    if setup_in is not None and not setup_in(bclone):
        return []
    p, _ = run_tests(cfg["test"], bclone)
    names = counted(p, bclone)[1] if counted is not None else failing_tests(p.stdout + "\n" + p.stderr)
    j.notes.append(f"base {base_sha[:7]} run: {len(names)} failing test(s) named")
    return names


# ---- output

def template(directive: str, rev: int = 1) -> str:
    """S3: a valid report/2 head skeleton for directive ``directive``, for a session that cannot install ga: fill the
    placeholders, keep the shape. It passes ``ga check`` as printed."""
    if not re.match(r"^[A-Za-z][A-Za-z0-9_.-]*$", directive or ""):
        raise JudgeError(f"--template needs a directive id like CMD-GA32, got {directive!r}")
    head = {"schema": "report/2", "from": "GA", "handled": [{"id": directive, "rev_seen": rev, "status": "done"}],
            "commits": [{"repo": "owner/repo", "branch": "branch", "sha": "0" * 40}],
            "tests": {"passed": 0, "failed": 0, "skipped": 0}, "change_size": "interface",
            "items": [{"id": "D1", "state": "met", "evidence": ["what you ran and what it showed"]}]}
    return dump_wire(head)


def verdict_head(j: Judgement) -> dict[str, Any]:
    nxt = {"success": ("continue", "measured clean"), "partial": ("refine", "open items"),
           "insufficient": ("verify", "measurement gap"), "failure": ("refine", "failing evidence"),
           "blocked": ("wait", "blocked")}[j.cls]
    notes = list(j.notes)
    if j.needs:
        notes.append(f"needs_judgement: {len(j.needs)}")
    head: dict[str, Any] = {"schema": "verdict/1", "class": j.cls}
    if j.cause:
        head["cause"] = j.cause
    if j.subclass:
        head["subclass"] = j.subclass
    head["evidence"] = {"heads": j.heads, "tests": j.tests, "notes": notes}
    head["claims_vs_evidence"] = j.claims
    head["next"] = {"choice": nxt[0], "reason": nxt[1] if j.clean or not j.needs else "judgement needed"}
    if j.report_ref:
        head["report_ref"] = j.report_ref
    head["note"] = (f"ga judge draft; seed {j.seed}; " + ("clean" if j.clean else f"{len(j.needs)} for judgement"))[:280]
    return head


def decision_log_row(j: Judgement, today: str) -> str:
    t = j.tests.get(j.repo, {})
    return (f"| BD-? | {today} | ga judge {j.directive or '?'}: {j.repo}@{j.sha[:7]} {j.cls}"
            f"{' · ' + j.cause if j.cause else ''}; tests {t.get('passed', '?')}/{t.get('failed', '?')}/{t.get('skipped', '?')}; "
            f"seed {j.seed}; needs_judgement {len(j.needs)} | ga judge |")


def section13_line(j: Judgement, today: str) -> str:
    t = j.tests.get(j.repo, {})
    return (f"- {today} {j.directive or '?'}: {j.repo}@{j.sha[:7]} {j.cls}{' · ' + j.cause if j.cause else ''} — "
            f"tests {t.get('passed', '?')} passed {t.get('failed', '?')} failed {t.get('skipped', '?')} skipped; "
            f"mutation seed {j.seed}; {'ff' if j.ff else 'not ff'} {j.base}")


def render(j: Judgement, today: str | None = None) -> str:
    today = today or date.today().isoformat()
    out = [dump_wire(verdict_head(j)), "needs_judgement:"]
    out += [f"- {x}" for x in j.needs] or ["- (none)"]
    out += ["", "DECISION_LOG:", decision_log_row(j, today), "", "BASELINE 13:", section13_line(j, today)]
    return "\n".join(out) + "\n"


def apply(j: Judgement, repo: str | Path, remote: str = "origin") -> str:
    """S3: fast-forward the integration branch to the sha and push; refuse unless the class is success, nothing is
    left for judgement and the sha fast-forwards. Returns a one-line result; raises JudgeError on refusal."""
    repo = Path(repo).resolve()
    if j.cls != "success":
        raise JudgeError(f"apply refused: class is {j.cls}")
    if j.needs:
        raise JudgeError(f"apply refused: {len(j.needs)} item(s) for judgement")
    if not j.ff or not j.sha:
        raise JudgeError("apply refused: not a fast-forward")
    cur = git(repo, "symbolic-ref", "-q", "--short", "HEAD").stdout.strip()
    if cur == j.base:
        git(repo, "merge", "--ff-only", "--quiet", j.sha, check=True)
    else:
        old = git(repo, "rev-parse", "--verify", f"refs/heads/{j.base}")
        args = ["update-ref", f"refs/heads/{j.base}", j.sha] + ([old.stdout.strip()] if old.returncode == 0 else [])
        git(repo, *args, check=True)
    git(repo, "push", "--quiet", remote, f"{j.sha}:refs/heads/{j.base}", check=True)
    return f"pushed {j.sha[:7]} to {remote}/{j.base}"
