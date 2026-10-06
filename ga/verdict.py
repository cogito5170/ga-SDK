"""``ga verdict --dry-run`` (DEV-R3-DET, R2): ACCEPT | SEND_BACK(reason) | SHADOW(criterion) as a pure function of
executable checks. No model call, no push: this module and everything it imports stay clear of the gateway and every
backend (a test scans the import graph).

Checks, in the order the decision reads them (each is ``(ok, detail)``; ok is True, False, or None = not run):
  head       the branch head is the sha under judgement
  ancestry   the sha fast-forwards the base, or merges into it cleanly
  files      every changed file matches an allowed glob (none given: unrestricted)
  spec       the acceptance test at the sha is identical to the authoritative copy (``spec_ref``)
  red_base   the acceptance test, put on the base, fails
  suite      the repo's full test command is green at the sha
  mutations  seeded mutations generated from the diff by code (``ga verify mutations``) are each killed by the
             acceptance test plus the tests that cover the mutated file
  revert     with the non-test changes of the diff reverted, the acceptance test fails

``decide`` reads only that dict: a False check is SEND_BACK (the first in the order above gives the reason); with none
False, a None check the scope requires, or a non-executable criterion, is SHADOW; otherwise ACCEPT. The scope ``light``
leaves ``suite`` and ``mutations`` out (the replay of old verdicts runs it; nothing there installs a venv).
Same inputs, same output: mutations are picked with a seed derived from the sha, lists are sorted, no clock, no paths.
"""
from __future__ import annotations

import fnmatch
import json
import random
import shutil
import sys
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from . import judge as J

ORDER = ("head", "ancestry", "files", "spec", "red_base", "suite", "mutations", "revert")
LIGHT_SKIPS = ("suite", "mutations")
Check = tuple["bool | None", str]


def decide(checks: dict[str, Check], nonexec: list[str] | None = None, scope: str = "full") -> dict[str, str]:
    """The decision as data: {"decision": ACCEPT|SEND_BACK|SHADOW, "reason": ...}. Pure."""
    names = [n for n in ORDER if not (scope == "light" and n in LIGHT_SKIPS)]
    for n in names:
        ok, detail = checks.get(n, (None, "not run"))
        if ok is False:
            return {"decision": "SEND_BACK", "reason": f"{n}: {detail}"}
    for n in names:
        ok, detail = checks.get(n, (None, "not run"))
        if ok is None:
            return {"decision": "SHADOW", "reason": f"{n}: {detail}"}
    if nonexec:
        return {"decision": "SHADOW", "reason": "non-executable criterion: " + "; ".join(sorted(nonexec))}
    return {"decision": "ACCEPT", "reason": "all executable checks pass"}


# ---- gathering the checks (git, clones, test runs)

def _g(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return J.git(repo, *args)


def _rev(repo: Path, ref: str) -> str:
    p = _g(repo, "rev-parse", "--verify", "-q", f"{ref}^{{commit}}")
    return p.stdout.strip() if p.returncode == 0 else ""


def _blob(repo: Path, ref: str, path: str) -> str:
    p = _g(repo, "rev-parse", "--verify", "-q", f"{ref}:{path}")
    return p.stdout.strip() if p.returncode == 0 else ""


def _changed(repo: Path, base: str, sha: str) -> list[tuple[str, str]]:
    out = _g(repo, "diff", "--name-status", "--no-renames", f"{base}...{sha}").stdout
    return sorted((ln.split("\t", 1)[0][0], ln.split("\t", 1)[1]) for ln in out.splitlines() if "\t" in ln)


def _is_test(path: str) -> bool:
    return path.startswith("tests/") or "/tests/" in path


def _modules(spec: str) -> list[str]:
    """tests/test_x.py -> test_x (the suite imports test files with the tests dir on the path)."""
    return [Path(x.partition("::")[0]).stem for x in ([spec] if isinstance(spec, str) else spec)]


class Runner:
    """Runs unittest modules or the full suite in a throwaway checkout, network blocked; one per dry-run."""

    def __init__(self, repo: Path, cfg: dict[str, Any] | None):
        self.repo, self.cfg = repo, cfg
        self.work = Path(tempfile.mkdtemp(prefix="ga-verdict-"))
        self.guard = self.work / "guard"
        self.guard.mkdir()
        (self.guard / "sitecustomize.py").write_text(J._GUARD, encoding="utf-8")
        self.n = 0

    def close(self) -> None:
        shutil.rmtree(self.work, ignore_errors=True)

    def checkout(self, ref: str) -> Path:
        self.n += 1
        d = self.work / f"co{self.n}"
        d.mkdir()
        tar = subprocess.run(["git", "-C", str(self.repo), "archive", ref], capture_output=True)
        if tar.returncode:
            raise J.JudgeError(f"git archive {ref}: {tar.stderr.decode(errors='replace')[:200]}")
        subprocess.run(["tar", "-x", "-C", str(d)], input=tar.stdout, check=True)
        return d

    def _env(self, d: Path) -> dict[str, str]:
        pp = [str(d / x) for x in (self.cfg or {}).get("pythonpath", ["."])] + [str(d / "tests")]
        return J.make_env(self.guard, pp)

    def modules(self, d: Path, mods: list[str]) -> tuple[bool, str]:
        """(green?, detail) for the named test modules. No tests found or an import error counts as red."""
        argv, _ = J.netless([sys.executable, "-m", "unittest", *sorted(set(mods))])
        p = J.run(argv, cwd=d, env=self._env(d), timeout=(self.cfg or {}).get("timeout", 1800))
        c = J.parse_counts(p.stdout + "\n" + p.stderr)
        if p.returncode == 0 and c and c["failed"] == 0:
            return True, f"{c['passed']} passed"
        return False, f"exit {p.returncode}" + (f", {c['failed']} failed" if c else ", no counts")

    def suite(self, d: Path) -> tuple[bool, str]:
        if not self.cfg:
            return False, "no judge config"
        argv, _ = J.netless(J.expand(self.cfg["test"], sys.executable))
        p = J.run(argv, cwd=d, env=self._env(d), timeout=self.cfg.get("timeout", 1800))
        c = J.parse_counts(p.stdout + "\n" + p.stderr)
        if p.returncode == 0 and c and c["failed"] == 0:
            return True, f"{c['passed']} passed, {c['skipped']} skipped"
        return False, f"exit {p.returncode}" + (f", {c['failed']} failed" if c else ", no counts")


def _twice(fn) -> tuple[Check, bool]:
    """(check, flaky): a failure is rerun once; a rerun that passes is a pass counted as flaky."""
    first = fn()
    if first[0] is not False:
        return first, False
    second = fn()
    return (second, True) if second[0] else (first, False)


def gather(repo: str | Path, *, branch: str, sha: str, base: str, spec: str, allowed: list[str] | None = None,
           spec_ref: str | None = None, seed: int | None = None, k: int = 5, scope: str = "full",
           config: str | None = None) -> tuple[dict[str, Check], dict[str, Any]]:
    """Run the checks. Returns (checks, extra) where extra carries {seed, flaky, mutations}; nothing is pushed."""
    repo = Path(repo).resolve()
    extra: dict[str, Any] = {"seed": 0, "flaky": [], "mutations": []}
    checks: dict[str, Check] = {}
    full = _rev(repo, sha)
    base_sha = _rev(repo, base)
    head = _rev(repo, branch)
    if not full or not base_sha:
        raise J.JudgeError(f"{sha if not full else base!r} is not a commit in {repo}")
    checks["head"] = (True, "head is the sha") if head == full else (False, f"{branch} is {head[:12] or 'missing'}, want {full[:12]}")
    mb = _g(repo, "merge-base", base_sha, full).stdout.strip()
    ff, conflicts = J.merge_state(repo, base_sha, full)
    checks["ancestry"] = (True, "fast-forward" if ff else "clean merge") if (ff or not conflicts and mb) else \
        (False, "conflicts in " + ", ".join(sorted(conflicts)) if conflicts else "no merge base")
    changed = _changed(repo, base_sha, full)
    if allowed:
        out = sorted(p for _, p in changed if not any(fnmatch.fnmatch(p, g) for g in allowed))
        checks["files"] = (not out, "outside allowed: " + ", ".join(out)) if out else (True, f"{len(changed)} within allowed")
    else:
        checks["files"] = (True, "unrestricted")
    ref = spec_ref or branch
    if not _blob(repo, full, spec):
        checks["spec"] = (False, f"{spec} is not in {full[:12]}")
    else:
        want = _blob(repo, ref, spec)
        same = want == _blob(repo, full, spec)
        checks["spec"] = (True, f"identical to {ref}") if same else (False, f"differs from {ref}" if want else f"{spec} missing in {ref}")
    try:
        cfg = J.load_config(repo, config)
    except J.JudgeError:
        cfg = None
    run = Runner(repo, cfg)
    try:
        if checks["spec"][0]:
            def red() -> Check:
                d = run.checkout(base_sha)
                dst = d / spec
                dst.parent.mkdir(parents=True, exist_ok=True)
                dst.write_bytes(subprocess.run(["git", "-C", str(repo), "show", f"{full}:{spec}"], capture_output=True).stdout)
                ok, det = run.modules(d, _modules(spec))
                return (not ok, "spec fails on the base" if not ok else "spec is green on the base: it does not test the change")
            checks["red_base"], fl = _twice(red)
            if fl:
                extra["flaky"].append("red_base")

            def rev() -> Check:
                d = run.checkout(full)
                for st, p in changed:
                    if _is_test(p):
                        continue
                    if st == "A":
                        (d / p).unlink(missing_ok=True)
                    else:
                        blob = subprocess.run(["git", "-C", str(repo), "show", f"{mb}:{p}"], capture_output=True)
                        if st == "D" or blob.returncode:
                            continue
                        (d / p).write_bytes(blob.stdout)
                ok, det = run.modules(d, _modules(spec))
                return (not ok, "spec fails with the change reverted" if not ok else "spec is green with the change reverted")
            checks["revert"], fl = _twice(rev)
            if fl:
                extra["flaky"].append("revert")
        else:
            checks["red_base"] = checks["revert"] = (None, "no acceptance test")
        if scope == "light":
            checks["suite"] = checks["mutations"] = (None, "not run (light)")
        else:
            def suite() -> Check:
                ok, det = run.suite(run.checkout(full))
                return ok, det
            checks["suite"], fl = _twice(suite)
            if fl:
                extra["flaky"].append("suite")
            checks["mutations"] = _mutations(repo, run, base_sha, full, spec, seed, k, extra)
    finally:
        run.close()
    return checks, extra


def _mutations(repo: Path, run: Runner, base_sha: str, sha: str, spec: str, seed: int | None, k: int,
               extra: dict[str, Any]) -> Check:
    from .verify.mutate import MutateError, mutations as generate
    extra["seed"] = int(sha[:8], 16) if seed is None else seed
    try:
        pool = generate(repo, base_sha, sha)
    except MutateError as e:
        return None, f"mutation generation failed: {e}"[:200]
    if not pool:
        return None, "no mutable lines in the diff"
    rng = random.Random(extra["seed"])
    picked = [pool[i] for i in sorted(rng.sample(range(len(pool)), min(max(k, 1), len(pool))))]
    survived = []
    for mu in picked:
        d = run.checkout(sha)
        path = d / mu["file"]
        text = path.read_text(encoding="utf-8")
        if text.count(mu["find"]) != 1:
            survived.append(f"{mu['id']} (does not apply)")
            continue
        path.write_text(text.replace(mu["find"], mu["replace"]), encoding="utf-8")
        ok, _ = run.modules(d, sorted(set(_modules(spec)) | set(_modules(mu.get("tests") or []))))
        extra["mutations"].append({"id": mu["id"], "killed": not ok})
        if ok:
            survived.append(mu["id"])
    if survived:
        return False, "survived: " + ", ".join(sorted(survived))
    return True, f"{len(picked)} of {len(pool)} killed (seed {extra['seed']})"


def dry_run(repo: str | Path, *, branch: str, sha: str, base: str, spec: str, allowed: list[str] | None = None,
            spec_ref: str | None = None, nonexec: list[str] | None = None, seed: int | None = None, k: int = 5,
            scope: str = "full", config: str | None = None) -> dict[str, Any]:
    """The dry-run verdict as one JSON-able dict: decision, reason, checks, scope, seed, flaky, mutations. No push."""
    checks, extra = gather(repo, branch=branch, sha=sha, base=base, spec=spec, allowed=allowed, spec_ref=spec_ref,
                           seed=seed, k=k, scope=scope, config=config)
    d = decide(checks, nonexec, scope)
    return {**d, "scope": scope, "checks": {n: {"ok": checks[n][0], "detail": checks[n][1]} for n in ORDER if n in checks},
            "seed": extra["seed"], "flaky": sorted(extra["flaky"]), "mutations": sorted(extra["mutations"], key=lambda m: m["id"])}


def render(v: dict[str, Any]) -> str:
    return json.dumps(v, sort_keys=True, ensure_ascii=False, separators=(",", ":")) + "\n"


# ---- replay of recorded verdicts

def _recorded(row: dict[str, Any]) -> str:
    return str(row.get("decision") or row.get("verdict") or "")


def _derive(repo: Path, sha: str) -> tuple[str, str] | tuple[None, str]:
    """(base, spec) for a recorded sha that carries neither: its first parent and the first test file it adds."""
    parent = _rev(repo, f"{sha}^1")
    if not parent:
        return None, "no parent to use as base"
    added = [p for st, p in _changed(repo, parent, sha) if st == "A" and _is_test(p) and Path(p).name.startswith("test_")]
    if not added:
        return None, "no acceptance-test path recorded and the commit adds no test file"
    return parent, added[0]


def replay(verdicts: str | Path, repo: str | Path, *, specs: str | Path | None = None, scope: str = "light",
           remote: str = "origin", k: int = 5) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Per row of the recorded verdicts: {id, rev, sha, recorded, dry_run, agree, reason}. Rows whose acceptance test or
    commit cannot be found are not_reproducible (agree null) with the reason; a pass that needed a rerun is counted flaky."""
    repo = Path(repo).resolve()
    given: dict[tuple[str, int], dict[str, Any]] = {}
    if specs:
        for ln in Path(specs).read_text(encoding="utf-8").splitlines():
            if ln.strip():
                s = json.loads(ln)
                given[(s["id"], int(s.get("rev", 1)))] = s
    rows = [json.loads(ln) for ln in Path(verdicts).read_text(encoding="utf-8").splitlines() if ln.strip()]
    out: list[dict[str, Any]] = []
    for r in rows:
        row = {"id": r.get("id"), "rev": r.get("rev"), "sha": r.get("sha", ""), "recorded": _recorded(r),
               "dry_run": None, "agree": None, "reason": ""}
        out.append(row)
        if row["recorded"] not in ("ACCEPT", "SEND_BACK"):
            row["reason"] = f"not_reproducible: recorded {row['recorded'] or 'nothing'} is not a verdict"
            continue
        if not row["sha"]:
            row["reason"] = "not_reproducible: no sha recorded"
            continue
        full = _rev(repo, row["sha"])
        if not full and J.fetch(repo, remote, row["sha"]):
            full = _rev(repo, row["sha"])
        if not full:
            row["reason"] = "not_reproducible: sha not reachable in this repository"
            continue
        row["sha"] = full
        s = given.get((row["id"], int(row["rev"] or 1)))
        if s:
            base, spec, allowed = s.get("base") or _rev(repo, f"{full}^1"), s["spec"], s.get("allowed")
        else:
            base, spec = _derive(repo, full)
            allowed = None
            if base is None:
                row["reason"] = f"not_reproducible: {spec}"
                continue
        try:
            v = dry_run(repo, branch=full, sha=full, base=base, spec=spec, allowed=allowed, scope=scope, k=k)
        except J.JudgeError as e:
            row["reason"] = f"not_reproducible: {e}"[:200]
            continue
        row["dry_run"] = v["decision"] + (f"({v['reason']})" if v["decision"] != "ACCEPT" else "")
        row["flaky"] = v["flaky"]
        row["agree"] = v["decision"] == row["recorded"]
        row["reason"] = v["reason"] + ("" if s else "; base and spec derived from the commit")
    counts = {"agree": 0, "disagree": 0, "not_reproducible": 0, "flaky": 0}
    for row in out:
        if row["dry_run"] is None:
            counts["not_reproducible"] += 1
        elif row.get("flaky"):
            counts["flaky"] += 1
        else:
            counts["agree" if row["agree"] else "disagree"] += 1
    return out, counts


def main(args) -> int:
    if args.replay:
        rows, counts = replay(args.replay, args.repo or ".", specs=args.specs, scope=args.scope or "light", k=args.k)
        text = "".join(json.dumps(r, sort_keys=True, ensure_ascii=False, separators=(",", ":")) + "\n" for r in rows)
        if args.out:
            Path(args.out).write_text(text, encoding="utf-8")
        else:
            sys.stdout.write(text)
        print(json.dumps(counts, sort_keys=True), file=sys.stderr)
        return 0
    if not args.dry_run:
        print("ga verdict: only --dry-run exists (no push); pass --dry-run", file=sys.stderr)
        return 2
    if not (args.branch and args.sha and args.base and args.spec):
        print("ga verdict --dry-run: --branch, --sha, --base and --spec are required", file=sys.stderr)
        return 2
    try:
        v = dry_run(args.repo or ".", branch=args.branch, sha=args.sha, base=args.base, spec=args.spec,
                    allowed=args.allowed, spec_ref=args.spec_ref, nonexec=args.nonexec, seed=args.seed, k=args.k,
                    scope=args.scope or "full", config=args.judge_config)
    except J.JudgeError as e:
        print(f"ga verdict: {e}", file=sys.stderr)
        return 2
    sys.stdout.write(render(v))
    return {"ACCEPT": 0, "SEND_BACK": 1, "SHADOW": 3}[v["decision"]]
