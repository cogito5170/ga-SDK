"""venv Bundle (METHOD §4.4, F3 · F4).

(a) ``run_path``: export every repo at its head side by side and run each repo's tests with all of
    them on PYTHONPATH.
(b) ``run_install``: one clean venv, ``pip install`` every packaged repo pinned to its sha in a single
    resolve (so mismatched pins fail as ``pin_conflict``), then run the tests against the installed
    packages (PYTHONPATH removed).

Skipped counts are always reported; F4 was a skip hiding a packaging problem.

(b) also imports, from an empty directory, every module the source tree has under each top-level package the
distribution installed (``import_check``). The tests run in the exported checkout, whose own copy can shadow the
installed one; GA10's W1 sub-package was green that way and missing from the installed copy.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

from ..config import Config
from .base import BundleResult, Counts, RepoRun
from .git import GitVcs

UNITTEST_RAN = re.compile(r"^Ran (\d+) tests? in ", re.MULTILINE)
UNITTEST_END = re.compile(r"^(OK|FAILED)(?: \((.*)\))?\s*$", re.MULTILINE)
PYTEST_LINE = re.compile(r"^[=\s]*((?:\d+ (?:passed|failed|skipped|errors?|xfailed|xpassed|deselected|warnings?)(?:, )?)+) in [\d.]+s", re.MULTILINE)


def parse_counts(output: str) -> Counts | None:
    """Counts from unittest or pytest output; None when neither summary is found."""
    ran = UNITTEST_RAN.findall(output)
    ends = UNITTEST_END.findall(output)
    if ran and ends:
        total = int(ran[-1])
        _, detail = ends[-1]
        kv = dict((k.strip(), int(v)) for k, v in (x.split("=") for x in detail.split(",") if "=" in x)) if detail else {}
        failed, errors, skipped = kv.get("failures", 0), kv.get("errors", 0), kv.get("skipped", 0)
        return Counts(total - failed - errors - skipped, failed, skipped, errors)
    lines = PYTEST_LINE.findall(output)
    if lines:
        kv: dict[str, int] = {}
        for part in lines[-1].split(", "):
            n, word = part.split(" ", 1)
            kv[word.rstrip("s") if word in ("errors", "warnings") else word] = int(n)
        return Counts(kv.get("passed", 0), kv.get("failed", 0), kv.get("skipped", 0), kv.get("error", 0))
    return None


# run by the clean venv's python with an empty working directory: argv = distribution name, source root
IMPORT_CHECK = r"""
import importlib, importlib.metadata as md, json, pathlib, sys
dist, src = sys.argv[1], pathlib.Path(sys.argv[2])
skip = (".dist-info", ".egg-info", ".pth", ".data")
tops = sorted({(f.parts[0][:-3] if f.parts[0].endswith(".py") else f.parts[0]) for f in (md.distribution(dist).files or [])
               if f.parts and not f.parts[0].endswith(skip) and f.parts[0] not in ("..", "__pycache__")})
mods = []
for top in tops:
    if (src / top / "__init__.py").exists():
        for p in sorted((src / top).rglob("*.py")):
            parts = p.relative_to(src).with_suffix("").parts
            if parts[-1] == "__main__" or not all((src.joinpath(*parts[:i]) / "__init__.py").exists() for i in range(1, len(parts))):
                continue
            mods.append(".".join(parts[:-1] if parts[-1] == "__init__" else parts))
    elif (src / (top + ".py")).exists():
        mods.append(top)
missing, errors = [], []
for m in mods:
    try:
        importlib.import_module(m)
    except ModuleNotFoundError as e:
        (missing if e.name and (m == e.name or m.startswith(e.name + ".")) else errors).append(m)
    except Exception as e:
        errors.append(m + ":" + type(e).__name__)
print(json.dumps({"tops": tops, "checked": len(mods), "missing": missing, "errors": errors}))
"""


def file_url(path: Path) -> str:
    # pip's PEP 508 parser rejects file:/// (empty host); localhost is accepted
    return "file://localhost" + path.resolve().as_posix()


class VenvBundle:
    def __init__(self, cfg: Config, vcs: GitVcs, work_dir: str | Path, python: str = sys.executable):
        self.cfg = cfg
        self.vcs = vcs
        self.work = Path(work_dir)
        self.python = python
        self.timeout = float(cfg.bundle.get("timeout", 1800))
        self.pip_args = list(cfg.bundle.get("pip_args", []))

    # ------------------------------------------------------------------ helpers

    def _key(self, heads: dict[str, str]) -> str:
        return hashlib.sha1("|".join(f"{r}@{s}" for r, s in sorted(heads.items())).encode()).hexdigest()[:12]

    def _export_all(self, heads: dict[str, str], where: Path) -> dict[str, Path]:
        if where.exists():
            shutil.rmtree(where)
        return {repo: self.vcs.export(repo, sha, where / repo) for repo, sha in heads.items()}

    def _run_tests(self, repo: str, sha: str, cwd: Path, python: str, env: dict[str, str], mode: str) -> RepoRun:
        spec = self.cfg.repos[repo]
        if not spec.test:
            return RepoRun(repo, sha, mode, True, None, problem="no_test_command")
        argv = [python if a == "{python}" else a for a in spec.test]
        full = dict(env)
        full.update(spec.env)
        try:
            p = subprocess.run(argv, cwd=str(cwd), env=full, capture_output=True, timeout=self.timeout)
        except subprocess.TimeoutExpired:
            return RepoRun(repo, sha, mode, False, None, problem="timeout")
        out = p.stdout.decode(errors="replace") + p.stderr.decode(errors="replace")
        counts = parse_counts(out)
        ok = p.returncode == 0 and (counts is None or (counts.failed == 0 and counts.errors == 0))
        return RepoRun(repo, sha, mode, ok, counts, out[-4000:], "" if counts else "unparsed_output")

    # ------------------------------------------------------------------ (a) path

    def run_path(self, heads: dict[str, str]) -> BundleResult:
        where = self.work / "path" / self._key(heads)
        dirs = self._export_all(heads, where)
        pythonpath = os.pathsep.join(str((dirs[r] / self.cfg.repos[r].src).resolve()) for r in sorted(dirs))
        env = dict(os.environ)
        env["PYTHONPATH"] = pythonpath
        runs = [self._run_tests(r, heads[r], dirs[r], self.python, env, "path") for r in sorted(heads)]
        return BundleResult("path", all(r.ok for r in runs), runs)

    # ------------------------------------------------------------------ (b) install

    def requirements(self, heads: dict[str, str]) -> list[str]:
        reqs = []
        for repo in sorted(heads):
            spec = self.cfg.repos[repo]
            if not spec.package:
                continue
            extras = f"[{spec.extras}]" if spec.extras else ""
            reqs.append(f"{spec.package}{extras} @ git+{self.source_url(repo)}@{heads[repo]}")
        return reqs

    def source_url(self, repo: str) -> str:
        """Where pip clones from: the remote when there is one (it has the pushed heads), else the local repository."""
        spec = self.cfg.repos[repo]
        if spec.remote:
            url = self.vcs.remote_url(repo)
            if "://" in url or url.startswith("git@"):
                return url
            return file_url(Path(url) if Path(url).is_absolute() else self.vcs.repo_dir(repo) / url)
        return file_url(self.vcs.repo_dir(repo))

    def _seed_build_backend(self, vpy: str, env: dict[str, str]) -> str:
        """Installs build without isolation need setuptools in the venv; Python >= 3.12 venvs have none.
        Seed it with the configured pip args (online, or offline with --find-links). Returns an error text or ""."""
        if subprocess.run([vpy, "-c", "import setuptools"], capture_output=True).returncode == 0:
            return ""
        argv = [vpy, "-m", "pip", "install", "--quiet", "--disable-pip-version-check", *self.pip_args, "setuptools"]
        p = subprocess.run(argv, capture_output=True, env=env, timeout=self.timeout)
        if p.returncode != 0:
            return "no build backend in the clean venv and setuptools could not be installed: " + (p.stdout + p.stderr).decode(errors="replace")[-2000:]
        # setuptools < 70.1 builds wheels only with the separate 'wheel' package; best effort
        subprocess.run([vpy, "-m", "pip", "install", "--quiet", "--disable-pip-version-check", *self.pip_args, "wheel"], capture_output=True, env=env, timeout=self.timeout)
        return ""

    def run_install(self, heads: dict[str, str]) -> BundleResult:
        base = self.work / "install" / self._key(heads)
        if base.exists():
            shutil.rmtree(base)
        venv = base / "venv"
        subprocess.run([self.python, "-m", "venv", str(venv)], check=True, capture_output=True)
        vpy = str(venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python"))
        reqs = self.requirements(heads)
        env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
        seeded = self._seed_build_backend(vpy, env)
        if seeded:
            return BundleResult("install", False, [], "install_failed", seeded)
        if reqs:
            argv = [vpy, "-m", "pip", "install", "--quiet", "--disable-pip-version-check", "--no-build-isolation", *self.pip_args, *reqs]
            p = subprocess.run(argv, capture_output=True, env=env, timeout=self.timeout)
            out = p.stdout.decode(errors="replace") + p.stderr.decode(errors="replace")
            if p.returncode != 0:
                conflict = "ResolutionImpossible" in out or "conflicting dependencies" in out
                return BundleResult("install", False, [], "pin_conflict" if conflict else "install_failed", out[-4000:])
        dirs = self._export_all(heads, base / "src")
        runs = [self.import_check(r, heads[r], (dirs[r] / self.cfg.repos[r].src).resolve(), vpy, env, base)
                for r in sorted(heads) if self.cfg.repos[r].package]
        runs += [self._run_tests(r, heads[r], dirs[r], vpy, env, "install") for r in sorted(heads)]
        return BundleResult("install", all(r.ok for r in runs), runs)

    def import_check(self, repo: str, sha: str, src: Path, vpy: str, env: dict[str, str], base: Path) -> RepoRun:
        """Import, from an empty directory, each source module of the packages the distribution installed.
        A module the source has and the install lacks is ``missing_in_install``."""
        empty = base / "empty"
        empty.mkdir(exist_ok=True)
        p = subprocess.run([vpy, "-c", IMPORT_CHECK, self.cfg.repos[repo].package, str(src)], cwd=str(empty), env=env,
                           capture_output=True, timeout=self.timeout)
        try:
            got = json.loads(p.stdout.decode().strip().splitlines()[-1])
        except (ValueError, IndexError):
            return RepoRun(repo, sha, "install", False, None, (p.stdout + p.stderr).decode(errors="replace")[-2000:], "import_check_failed")
        if got["missing"] or not got["tops"]:
            what = ", ".join(got["missing"]) or f"{self.cfg.repos[repo].package} installed no package"
            return RepoRun(repo, sha, "install", False, None, what[:2000], "missing_in_install")
        if got["errors"]:
            return RepoRun(repo, sha, "install", False, None, ", ".join(got["errors"])[:2000], "import_error")
        return RepoRun(repo, sha, "install", True, None, f"imported {got['checked']}", "")
