"""The CLI front end: prompt, check, post (with R1 / R6 refusals)."""
import contextlib
import io
import tempfile
import unittest
from pathlib import Path

from ga.__main__ import main
from ga.forms import dump_text

from examples import valid

CFG = str(Path(__file__).resolve().parent.parent / "examples" / "baseline" / "config.json")


def run(*argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = main(list(argv))
    return code, out.getvalue(), err.getvalue()


class CliTest(unittest.TestCase):
    def test_prompt(self):
        code, out, _ = run("--config", CFG, "prompt", "ga-SDK")
        self.assertEqual(code, 0)
        self.assertTrue(out.startswith("넌 이제부터 ga-SDK 세션이야."))
        code, out, _ = run("--config", CFG, "prompt", "--hub")
        self.assertIn("넌 이제부터 baseline 세션이야.", out)

    def test_check_and_post(self):
        with tempfile.TemporaryDirectory() as d:
            good = Path(d) / "r.md"
            good.write_text(dump_text(dict(valid("report/1"), **{"from": "ga-SDK"}), "## Result\n됐다\n"), encoding="utf-8")
            bad = Path(d) / "bad.md"
            bad.write_text(dump_text({"schema": "report/1", "from": "x"}) + "```\npip install a[b]\n```\n", encoding="utf-8")
            self.assertEqual(run("--config", CFG, "check", str(good))[0], 0)
            code, out, _ = run("--config", CFG, "check", str(bad))
            self.assertEqual(code, 2)
            self.assertIn("R13", out)
            ga_dir = str(Path(d) / ".ga")
            code, out, _ = run("--config", CFG, "--ga-dir", ga_dir, "post", "--channel", "ga-SDK", "--from", "ga-SDK", str(good))
            self.assertEqual(code, 0)
            self.assertTrue((Path(ga_dir) / "mailbox" / "ga-SDK" / f"{out.strip()}.md").exists())
            # R1: another session may not write in this channel
            code, _, err = run("--config", CFG, "--ga-dir", ga_dir, "post", "--channel", "ga-SDK", "--from", "MS", str(good))
            self.assertEqual(code, 0)
            self.assertIn("R1", err)  # soft: notified, still posted
            leak = Path(d) / "leak.md"
            leak.write_text(dump_text(valid("report/1"), "## Evidence\n" + "".join(["sk-", "ant-", "z" * 30]) + "\n"), encoding="utf-8")
            code, _, err = run("--config", CFG, "--ga-dir", ga_dir, "post", "--channel", "ga-SDK", "--from", "ga-SDK", str(leak))
            self.assertEqual(code, 2)
            self.assertIn("R6", err)


if __name__ == "__main__":
    unittest.main()
