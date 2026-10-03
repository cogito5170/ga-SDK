"""CMD-GA20 (GA_UNIFIED U2, BD-206): ga-sdk pins rlo-sdk[sensor]; ga.rlo is GR's seam; `ga rlo ...` dispatches to it.

D2: the pin (pyproject = ga/_pins.py, and the installed metadata when ga-sdk is installed) · ga imports without rlo
· the dispatch (present, missing, a missing dependency of the module, options passed unchanged).
"""
import contextlib
import io
import json
import re
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
from importlib import metadata
from pathlib import Path
from unittest import mock

import ga.__main__ as gamain
from ga import _pins

ROOT = Path(__file__).resolve().parents[1]
RLO_REQ = "rlo-sdk[sensor] @ git+https://github.com/cogito5170/rlo-SDK@250a88e56a74d2776688d34ec732cbd0f4244ba0"


def _canon(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name.strip()).lower()  # PEP 503


def req(s: str) -> tuple:
    """A requirement string by meaning: (name, extras, url, marker). Spacing, case and `_` in names do not count, so
    the pyproject text and the installed Requires-Dist (whatever the build tool wrote) compare equal."""
    head, _, marker = s.partition(";")
    name, at, url = head.partition("@")
    extras = ()
    if "[" in name:
        name, _, ex = name.partition("[")
        extras = tuple(sorted(_canon(x) for x in ex.strip().rstrip("]").split(",") if x.strip()))
    return _canon(name), extras, url.strip() if at else None, "".join(marker.split()).replace("'", '"') or None


def pyproject_dependencies() -> list[str]:
    """[project] dependencies of pyproject.toml; tomllib where there is one, else the plain array (Python 3.10)."""
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    try:
        import tomllib
    except ImportError:
        project = text.split("[project]", 1)[1].split("\n[", 1)[0]
        body = re.search(r"^dependencies\s*=\s*\[(.*?)\]\s*$", project, re.S | re.M).group(1)
        return re.findall(r'"([^"]*)"', body)
    return tomllib.loads(text)["project"]["dependencies"]


class PinTest(unittest.TestCase):
    def test_the_pin_is_rlo_sdk_sensor_at_the_directives_sha(self):
        self.assertEqual(_pins.requirements(), [RLO_REQ])
        dist, extras, url, sha = _pins.PINS["rlo"]
        self.assertRegex(sha, r"^[0-9a-f]{40}$")
        self.assertEqual(_pins.VERSIONS, {"rlo-sdk": "0.7.0"})

    def test_pyproject_says_what_the_pins_say(self):
        self.assertEqual(sorted(map(req, pyproject_dependencies())), sorted(map(req, _pins.requirements())))

    def test_req_compares_by_meaning(self):
        self.assertEqual(req("RLO_sdk[Sensor]@ git+https://x/y@abc"), req("rlo-sdk[sensor] @ git+https://x/y@abc"))
        for other in ("rlo-sdk @ git+https://x/y@abc", "rlo-sdk[sensor] @ git+https://x/y@abd",
                      "rlo-sdk[sensor] @ git+https://x/Y@abc", "rlo-sdk[sensor] @ git+https://x/y@abc ; python_version<'3.11'"):
            self.assertNotEqual(req(other), req("rlo-sdk[sensor] @ git+https://x/y@abc"), other)

    def test_installed_metadata_matches_the_pins(self):
        """Runs where ga-sdk is installed (a clean venv); in a source checkout there is no metadata to read."""
        try:
            dist = metadata.distribution("ga-sdk")
        except metadata.PackageNotFoundError:
            self.skipTest("ga-sdk is not installed (source checkout)")
        plain = [r for r in dist.requires or [] if "extra ==" not in r]
        self.assertEqual(sorted(map(req, plain)), sorted(map(req, _pins.requirements())))
        rlo = metadata.distribution("rlo-sdk")
        self.assertEqual(rlo.version, _pins.VERSIONS["rlo-sdk"])
        direct = json.loads(rlo.read_text("direct_url.json") or "{}")
        self.assertEqual(direct.get("vcs_info", {}).get("commit_id"), _pins.PINS["rlo"][3])
        metadata.distribution("llmsensor")  # the [sensor] extra came in


class LazyImportTest(unittest.TestCase):
    def test_ga_imports_and_runs_without_importing_rlo(self):
        """A fake `rlo` is put first on the path, so an eager import would succeed and be seen in sys.modules.
        Every ga module is imported (but GR's ga.rlo.* below the seam), then `ga --help`, `ga check` and `ga rlo`."""
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp)
        (tmp / "rlo").mkdir()
        (tmp / "rlo" / "__init__.py").write_text("", encoding="utf-8")
        (tmp / "d.md").write_text("```ga\n" + json.dumps({"schema": "notify/1", "to": "GA", "kind": "report",
                                                           "ref": "https://x/y"}) + "\n```\n", encoding="utf-8")
        code = textwrap.dedent(f"""
            import contextlib, io, pkgutil, importlib, sys
            sys.path.insert(0, {str(tmp)!r})
            import ga
            for m in pkgutil.walk_packages(ga.__path__, "ga."):
                if not m.name.startswith("ga.rlo."):
                    importlib.import_module(m.name)
            from ga.__main__ import main
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                try:
                    main(["--help"])
                except SystemExit:
                    pass
                main(["--config", "/nonexistent.json", "check", {str(tmp / "d.md")!r}])
                main(["rlo"])
            print(sorted(m for m in sys.modules if m == "rlo" or m.startswith("rlo.")))
        """)
        out = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True)
        self.assertEqual(out.returncode, 0, out.stderr[-2000:])
        self.assertEqual(out.stdout.strip(), "[]")

    def test_no_tests_rlo_package_shadows_rlo(self):
        """BD-218: under `discover -s tests` a tests/rlo/ is a top-level `rlo` and hides rlo-sdk; GR's tests are in
        tests/test_rlo/."""
        self.assertFalse((ROOT / "tests" / "rlo").exists())

    def test_the_seam_is_there_and_empty(self):
        import ga.rlo
        self.assertRegex(ga.rlo.__version__, r"^\d+\.\d+\.\d+$")
        source = (ROOT / "ga" / "rlo" / "__init__.py").read_text(encoding="utf-8")
        self.assertNotRegex(source, r"(?m)^\s*(import|from)\s")  # the seam itself imports nothing


class DispatchTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        sys.path.insert(0, str(self.tmp))
        self.addCleanup(sys.path.remove, str(self.tmp))
        self.seen = []

    def module(self, name, body):
        (self.tmp / f"{name}.py").write_text(textwrap.dedent(body), encoding="utf-8")
        self.addCleanup(sys.modules.pop, name, None)
        return name

    def run_main(self, cli, *argv):
        err = io.StringIO()
        with mock.patch.object(gamain, "RLO_CLI", cli), contextlib.redirect_stderr(err):
            code = gamain.main(list(argv))
        return code, err.getvalue()

    def test_present_module_gets_the_rest_unchanged(self):
        name = self.module("ga_t_cli_ok", """
            SEEN = []
            def main(argv):
                SEEN.append(argv)
                return 3
        """)
        for argv, rest in ((["rlo", "--help", "x"], ["--help", "x"]),
                           (["--config", "c.json", "--ga-dir=d", "rlo", "doctor", "--now-ms", "1"], ["doctor", "--now-ms", "1"]),
                           (["rlo"], [])):
            with self.subTest(argv=argv):
                code, err = self.run_main(name, *argv)
                self.assertEqual((code, err), (3, ""))
                self.assertEqual(sys.modules[name].SEEN[-1], rest)

    def test_none_from_main_is_exit_0(self):
        name = self.module("ga_t_cli_none", "def main(argv):\n    return None\n")
        self.assertEqual(self.run_main(name, "rlo"), (0, ""))

    def test_a_missing_module_is_exit_2_naming_it(self):
        code, err = self.run_main("ga.rlo.no_such_cli", "rlo", "x")
        self.assertEqual(code, 2)
        self.assertEqual(err, "ga rlo: module ga.rlo.no_such_cli is not there yet\n")

    def test_the_real_entry_without_the_module(self):
        if (ROOT / "ga" / "rlo" / "cli.py").exists():
            self.skipTest("ga.rlo.cli is there (GR moved it in)")
        out = subprocess.run([sys.executable, "-m", "ga", "rlo", "doctor"], cwd=ROOT, capture_output=True, text=True)
        self.assertEqual((out.returncode, out.stdout, out.stderr), (2, "", "ga rlo: module ga.rlo.cli is not there yet\n"))

    def test_a_missing_dependency_of_the_module_is_named_not_hidden(self):
        name = self.module("ga_t_cli_dep", "import ga_t_no_such_dependency\ndef main(argv):\n    return 0\n")
        code, err = self.run_main(name, "rlo")
        self.assertEqual(code, 2)
        self.assertEqual(err, f"ga rlo: module ga_t_no_such_dependency is missing ({name} needs it)\n")

    def test_an_error_inside_main_is_not_swallowed(self):
        name = self.module("ga_t_cli_raise", "def main(argv):\n    import ga_t_late_missing\n")
        with self.assertRaises(ModuleNotFoundError):
            self.run_main(name, "rlo")

    def test_rlo_as_an_option_value_is_not_the_command(self):
        self.assertIsNone(gamain._rlo_argv(["--config", "rlo", "check", "f"]))
        self.assertIsNone(gamain._rlo_argv(["check", "rlo"]))
        self.assertEqual(gamain._rlo_argv(["--ga-dir", "d", "rlo", "a"]), ["a"])

    def test_help_lists_the_entry(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), self.assertRaises(SystemExit):
            gamain.main(["--help"])
        self.assertIn("rlo Autonomy commands (ga.rlo, owned by GR)", out.getvalue())


if __name__ == "__main__":
    unittest.main()
