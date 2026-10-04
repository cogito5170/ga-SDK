"""CMD-GA27 S4 (BD-298): `ga inbox <channel>`, the one read path, against a fake GitHub REST server on localhost.

Only comments after the cursor come back; the cursor does not move when the output fails; the API wrapper and the prose
are gone; a notify-then-inbox flow delivers each message once; ga mail's channel uses its own cursor.
"""
import contextlib
import io
import json
import os
import shutil
import subprocess
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock
from urllib.parse import parse_qs, urlparse

from ga import forms as F
from ga import inbox
from ga.__main__ import main

PROSE = "## Result\nThis paragraph restates the head for people.\n"


class FakeGitHub:
    """GET /repos/o/r/issues/12/comments?per_page=&since=&page= with GitHub's rules: since is inclusive on updated_at,
    pages follow Link rel=next. Every request's query is recorded."""

    def __init__(self, test, per_page=100):
        self.comments, self.queries, self.per_page = [], [], per_page
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                u = urlparse(self.path)
                q = {k: v[0] for k, v in parse_qs(u.query).items()}
                fake.queries.append(q)
                if u.path != "/repos/o/r/issues/12/comments":
                    self.send_response(404)
                    self.end_headers()
                    return
                rows = [c for c in fake.comments if "since" not in q or c["updated_at"] >= q["since"]]
                page, per = int(q.get("page", 1)), min(int(q.get("per_page", 30)), fake.per_page)
                chunk = rows[(page - 1) * per: page * per]
                body = json.dumps(chunk).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                if page * per < len(rows):
                    nxt = dict(q, page=str(page + 1))
                    self.send_header("Link", f'<{fake.url}{u.path}?' + "&".join(f"{k}={v}" for k, v in nxt.items())
                                     + '>; rel="next"')
                self.end_headers()
                self.wfile.write(body)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        test.addCleanup(self.server.server_close)
        test.addCleanup(self.server.shutdown)

    def add(self, cid, at, head=None, prose=PROSE):
        body = ("```ga\n" + json.dumps(head, indent=1) + "\n```\n" + prose) if head else prose  # pretty, as written
        self.comments.append({"id": cid, "body": body, "created_at": at, "updated_at": at,
                              "user": {"login": "someone", "id": 1}, "html_url": f"https://github.com/o/r/issues/12#{cid}",
                              "reactions": {"total_count": 0}, "author_association": "OWNER"})
        return body


def report(n):
    return {"schema": "report/2", "from": "W", "handled": [{"id": f"CMD-W{n}", "rev_seen": 1, "status": "done"}],
            "items": [{"id": "D1", "state": "met"}]}


class Session:
    """A git repository (the cursor lives in its git dir) reading the fake channel."""

    def __init__(self, test, gh):
        self.dir = Path(tempfile.mkdtemp())
        test.addCleanup(shutil.rmtree, self.dir)
        subprocess.run(["git", "init", "-q", str(self.dir)], check=True)
        self.gh = gh

    def read(self, out=None):
        out = out or io.StringIO()
        n = inbox.read("github:o/r#12", out, repo=self.dir, api=self.gh.url)
        return n, [json.loads(x) for x in out.getvalue().splitlines()] if isinstance(out, io.StringIO) else None


class Broken(io.StringIO):
    def write(self, s):
        raise OSError("disk full")


class InboxTest(unittest.TestCase):
    def setUp(self):
        self.gh = FakeGitHub(self)
        self.s = Session(self, self.gh)

    def test_only_comments_after_the_cursor(self):
        self.gh.add(1, "2026-10-04T10:00:00Z", report(1))
        self.gh.add(2, "2026-10-04T11:00:00Z", report(2))
        n, rows = self.s.read()
        self.assertEqual((n, [r["id"] for r in rows]), (2, [1, 2]))
        self.assertEqual(self.s.read()[0], 0)
        self.gh.add(3, "2026-10-04T12:00:00Z", report(3))
        n, rows = self.s.read()
        self.assertEqual([r["id"] for r in rows], [3])
        self.assertEqual(self.gh.queries[-1].get("since"), "2026-10-04T11:00:00Z")  # since is sent
        self.assertNotIn("since", self.gh.queries[0])  # the first read has no cursor

    def test_since_is_inclusive_so_a_same_second_comment_is_not_lost_or_doubled(self):
        self.gh.add(1, "2026-10-04T10:00:00Z", report(1))
        self.s.read()
        self.gh.add(2, "2026-10-04T10:00:00Z", report(2))  # same second as the cursor
        n, rows = self.s.read()
        self.assertEqual([r["id"] for r in rows], [2])
        self.assertEqual(self.s.read()[0], 0)

    def test_the_cursor_does_not_advance_when_the_output_fails(self):
        self.gh.add(1, "2026-10-04T10:00:00Z", report(1))
        with self.assertRaises(OSError):
            inbox.read("github:o/r#12", Broken(), repo=self.s.dir, api=self.gh.url)
        n, rows = self.s.read()
        self.assertEqual([r["id"] for r in rows], [1])  # shown again, then read

    def test_no_wrapper_and_no_prose(self):
        self.gh.add(1, "2026-10-04T10:00:00Z", report(1))
        self.gh.add(2, "2026-10-04T10:30:00Z", None, prose="Just a note for people, no head.\n")
        out = io.StringIO()
        inbox.read("github:o/r#12", out, repo=self.s.dir, api=self.gh.url)
        text = out.getvalue()
        for gone in ("restates the head", "Just a note", "someone", "html_url", "reactions", "author_association",
                     '"body"', "## Result", "```"):
            self.assertNotIn(gone, text)
        rows = [json.loads(x) for x in text.splitlines()]
        self.assertEqual([sorted(r) for r in rows], [["at", "head", "id"]] * 2)
        self.assertEqual(rows[0]["head"], report(1))
        self.assertIsNone(rows[1]["head"])
        self.assertEqual(text.splitlines()[0], '{"id":1,"at":"2026-10-04T10:00:00Z","head":' + F.minify(report(1)) + "}")

    def test_pages_are_followed(self):
        gh = FakeGitHub(self, per_page=7)
        s = Session(self, gh)
        for i in range(1, 18):
            gh.add(i, f"2026-10-04T10:{i:02d}:00Z", report(i))
        n, rows = s.read()
        self.assertEqual([r["id"] for r in rows], list(range(1, 18)))

    def test_notify_then_inbox_reads_each_message_once(self):
        """The sender posts its report and sends a notify/1 (kind + ref only); the receiver reads through inbox."""
        delivered = []
        for k in range(1, 4):
            self.gh.add(10 + k, f"2026-10-04T1{k}:00:00Z", report(k))
            note = {"schema": "notify/1", "to": "B", "kind": "report",
                    "ref": f"https://github.com/o/r/issues/12#issuecomment-{10 + k}"}
            self.assertNotIn("handled", F.minify(note))  # the notify carries no copy of the message
            n, rows = self.s.read()  # woken by the notify: one inbox read, not the notify body and the issue
            delivered += [r["id"] for r in rows]
        self.assertEqual(delivered, [11, 12, 13])
        self.assertEqual(self.s.read()[0], 0)

    def test_cursor_is_in_the_git_dir_and_not_in_the_work_tree(self):
        self.gh.add(1, "2026-10-04T10:00:00Z", report(1))
        self.s.read()
        files = list((self.s.dir / ".git" / "ga-inbox").glob("*.json"))
        self.assertEqual(len(files), 1)
        self.assertEqual(json.loads(files[0].read_text()), {"since": "2026-10-04T10:00:00Z", "seen": [1]})
        st = subprocess.run(["git", "status", "--porcelain"], cwd=self.s.dir, capture_output=True, text=True).stdout
        self.assertEqual(st, "")

    def test_cli(self):
        self.gh.add(1, "2026-10-04T10:00:00Z", report(1))
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, {"GA_GITHUB_API": self.gh.url}), contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(err):
            code = main(["inbox", "https://github.com/o/r/issues/12", "--repo", str(self.s.dir)])
        self.assertEqual(code, 0, err.getvalue())
        self.assertEqual([json.loads(x)["id"] for x in out.getvalue().splitlines()], [1])
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            self.assertEqual(main(["inbox", "slack:#general", "--repo", str(self.s.dir)]), 2)


class MailInboxTest(unittest.TestCase):
    def test_mail_channel_uses_the_mailbox_cursor(self):
        from test_mailbox import Repo, report2
        r = Repo(self, "w1", "amp")
        r.box("w1").send("AMP", report2("W1"))
        out = io.StringIO()
        self.assertEqual(inbox.read("mail:AMP", out, repo=r.clones["amp"]), 1)
        row = json.loads(out.getvalue())
        self.assertEqual(row["head"]["schema"], "report/2")
        self.assertNotIn("##", out.getvalue())
        self.assertEqual(inbox.read("mail:AMP", io.StringIO(), repo=r.clones["amp"]), 0)


if __name__ == "__main__":
    unittest.main()
