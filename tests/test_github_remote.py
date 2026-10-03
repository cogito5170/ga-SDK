"""CMD-GA8: the GitHub issue Channel against a fake HTTP server, and the remote-session Runner in a hub round."""
import json
import os
import socket
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from ga import config as gacfg
from ga.adapters.base import Post, TurnRequest
from ga.adapters.git import git
from ga.adapters.github import CallbackChannel, ChannelError, GitHubIssueChannel, RateLimited, split_author, with_author
from ga.adapters.runner import OutboxCallback, RemoteSessionRunner
from ga.forms import FormError, dump_text, parse_text
from ga.prompts import turn_prompt

from world import World, directive, proposal

TOKEN = "ghp_fake_token_for_tests_only_0123456789"


class FakeGitHub:
    """Just enough of the issue-comments API: list (paged, Link header), create, and planned failures."""

    def __init__(self, page_size: int = 2):
        self.page_size = page_size
        self.comments: dict[int, list[dict]] = {}
        self.next_id = 1000
        self.fail: list[tuple[int, dict]] = []  # (status, headers) answered to the next requests, in order
        self.requests: list[dict] = []
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, status, data=None, headers=None):
                body = json.dumps(data).encode() if data is not None else b""
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(body)

            def _route(self, method):
                u = urlparse(self.path)
                n = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(n) if n else b""
                fake.requests.append({"method": method, "path": u.path, "query": parse_qs(u.query),
                                      "auth": self.headers.get("Authorization"), "body": raw.decode()})
                if fake.fail:
                    status, hdrs = fake.fail.pop(0)
                    return self._send(status, {"message": "planned failure"}, hdrs)
                parts = u.path.strip("/").split("/")  # repos/o/r/issues/<n>/comments
                if len(parts) != 6 or parts[0] != "repos" or parts[3] != "issues" or parts[5] != "comments":
                    return self._send(404, {"message": "Not Found"})
                issue = int(parts[4])
                if issue not in fake.comments:
                    return self._send(404, {"message": "Not Found"})
                if method == "POST":
                    fake.next_id += 7
                    c = {"id": fake.next_id, "body": json.loads(raw)["body"], "user": {"login": "same-person"},
                         "created_at": f"2026-10-03T00:00:{len(fake.comments[issue]):02d}Z"}
                    fake.comments[issue].append(c)
                    return self._send(201, c)
                page = int(parse_qs(u.query).get("page", ["1"])[0])
                items = fake.comments[issue]
                chunk = items[(page - 1) * fake.page_size: page * fake.page_size]
                hdrs = {}
                if page * fake.page_size < len(items):
                    hdrs["Link"] = (f'<http://127.0.0.1:{fake.port}{u.path}?per_page=100&page={page + 1}>; rel="next", '
                                    f'<http://127.0.0.1:{fake.port}{u.path}?per_page=100&page=9>; rel="last"')
                return self._send(200, chunk, hdrs)

            def do_GET(self):
                self._route("GET")

            def do_POST(self):
                self._route("POST")

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def api(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class FakeGitHubCase(unittest.TestCase):
    def setUp(self):
        self.gh = FakeGitHub()
        self.addCleanup(self.gh.close)
        self.gh.comments = {1: [], 2: []}
        old = os.environ.get("GA_TEST_TOKEN")
        os.environ["GA_TEST_TOKEN"] = TOKEN
        self.addCleanup(lambda: os.environ.pop("GA_TEST_TOKEN", None) if old is None else os.environ.__setitem__("GA_TEST_TOKEN", old))

    def channel(self, **kw):
        return GitHubIssueChannel("owner/repo", {"A": 1, "B": 2}, token_env="GA_TEST_TOKEN", api=self.gh.api, timeout=5, **kw)


class GitHubChannelTest(FakeGitHubCase):
    def test_post_then_read_round_trips_ga_head_and_author(self):
        ch = self.channel()
        head = {"schema": "report/1", "from": "A", "handled": [{"id": "CMD-A1", "rev_seen": 1, "status": "done"}]}
        text = dump_text(head, "## Result\n했다\n")
        p = ch.post("A", "A", text)
        self.assertEqual(len(p.id), 20)
        self.assertEqual((p.channel, p.author, p.text), ("A", "A", text))
        stored = self.gh.comments[1][0]["body"]
        self.assertTrue(stored.rstrip().endswith("<!-- ga-author: A -->"))
        got = ch.read("A")
        self.assertEqual([(q.id, q.author) for q in got], [(p.id, "A")])
        self.assertEqual(parse_text(got[0].text)[0], head)  # the ```ga head survives the trip
        req = self.gh.requests[0]
        self.assertEqual((req["method"], req["path"]), ("POST", "/repos/owner/repo/issues/1/comments"))
        self.assertEqual(req["auth"], f"Bearer {TOKEN}")

    def test_read_follows_pages_and_after(self):
        ch = self.channel()
        ids = [ch.post("B", "hub" if i % 2 else "B", f"글 {i}\n").id for i in range(5)]
        self.gh.requests.clear()
        got = ch.read("B")
        self.assertEqual([p.id for p in got], ids)
        self.assertEqual([p.author for p in got], ["B", "hub", "B", "hub", "B"])
        self.assertEqual([r["query"].get("page", ["1"])[0] for r in self.gh.requests], ["1", "2", "3"])
        self.assertEqual(self.gh.requests[0]["query"]["per_page"], ["100"])
        self.assertEqual([p.id for p in ch.read("B", after=ids[2])], ids[3:])
        self.assertEqual(ch.read("B", after=ids[-1]), [])
        self.assertEqual(ch.read("A"), [])

    def test_comment_without_marker_is_by_login(self):
        self.gh.comments[1].append({"id": 5, "body": "그냥 글", "user": {"login": "someone"}, "created_at": ""})
        self.assertEqual([(p.author, p.text) for p in self.channel().read("A")], [("someone", "그냥 글")])
        self.assertEqual(split_author(with_author("x\n", "B.2"), "me"), ("B.2", "x\n"))
        self.assertEqual(split_author("<!-- ga-author: A --> 가운데\n", "me")[0], "me")  # a marker shares no line
        # a footer appended by the platform after the marker keeps the author; the last marker wins
        a, t = split_author("```ga\n{}\n```\n<!-- ga-author: A -->\n\n<!-- ga-author: B -->\n\n---\n_footer_\n", "me")
        self.assertEqual(a, "B")
        self.assertTrue(t.startswith("```ga\n{}\n```\n<!-- ga-author: A -->") and t.endswith("---\n_footer_\n"))

    def test_rate_limits(self):
        ch = self.channel()
        self.gh.fail = [(429, {"Retry-After": "7"})]
        with self.assertRaises(RateLimited) as e:
            ch.read("A")
        self.assertEqual((e.exception.status, e.exception.retry_after), (429, 7.0))
        self.gh.fail = [(403, {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": str(int(time.time()) + 30)})]
        with self.assertRaises(RateLimited) as e:
            ch.post("A", "A", "x")
        self.assertEqual(e.exception.status, 403)
        self.assertTrue(20 <= e.exception.retry_after <= 31)
        self.assertEqual(self.gh.comments[1], [])  # nothing half-written
        # a 403 that is not a rate limit is a plain error
        self.gh.fail = [(403, {"X-RateLimit-Remaining": "12"})]
        with self.assertRaises(ChannelError) as e:
            ch.read("A")
        self.assertNotIsInstance(e.exception, RateLimited)

    def test_errors_carry_status_never_the_token(self):
        ch = self.channel()
        for status in (401, 404, 422, 500):
            with self.subTest(status=status):
                self.gh.fail = [(status, {})]
                with self.assertRaises(ChannelError) as e:
                    ch.read("A")
                self.assertEqual(e.exception.status, status)
                self.assertNotIn(TOKEN, str(e.exception))
        # a rate limit in the middle of paging stops the read; nothing partial is returned
        for i in range(3):
            ch.post("A", "A", f"{i}\n")
        self.gh.requests.clear()
        self.gh.fail = []
        orig = self.gh.page_size
        try:
            self.gh.page_size = 1
            real = ch.opener
            calls = []

            def opener(req, timeout):
                calls.append(1)
                if len(calls) == 2:
                    self.gh.fail = [(429, {"Retry-After": "1"})]
                return real(req, timeout=timeout)
            ch.opener = opener
            with self.assertRaises(RateLimited):
                ch.read("A")
        finally:
            self.gh.page_size = orig
        with self.assertRaises(ChannelError):
            ch.read("Z")  # no issue configured

    def test_unreachable_and_no_token(self):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        ch = GitHubIssueChannel("owner/repo", {"A": 1}, token_env="GA_TEST_TOKEN", api=f"http://127.0.0.1:{port}", timeout=2)
        with self.assertRaises(ChannelError) as e:
            ch.read("A")
        self.assertIsNone(e.exception.status)
        os.environ.pop("GA_TEST_TOKEN")
        self.channel().read("A")
        self.assertIsNone(self.gh.requests[-1]["auth"])  # no token, no header (public read)
        with self.assertRaises(ValueError):
            GitHubIssueChannel("not a repo", {})

    def test_token_is_read_per_request_and_never_kept(self):
        ch = self.channel()
        self.assertNotIn(TOKEN, json.dumps(vars(ch), default=str))
        os.environ["GA_TEST_TOKEN"] = "second"
        ch.read("A")
        self.assertEqual(self.gh.requests[-1]["auth"], "Bearer second")

    def test_callback_channel(self):
        posted = []

        def post(channel, author, text):
            posted.append((channel, author, text))
            return Post(f"{len(posted):020d}", channel, author, "", text)

        ch = CallbackChannel(post, lambda channel, after: [p for p in [Post("1".zfill(20), channel, "A", "", "x")] if after is None or p.id > after])
        self.assertEqual(ch.post("A", "hub", "t").id, "1".zfill(20))
        self.assertEqual(len(ch.read("A")), 1)
        self.assertEqual(ch.read("A", "1".zfill(20)), [])


class RemoteRunnerTest(unittest.TestCase):
    def test_send_result_and_failure(self):
        seen = []
        r = RemoteSessionRunner(lambda req: seen.append(req) or {"session_id": "cse_1", "note": "sent"})
        res = r.run_turn(TurnRequest("A", "지시", Path(".")))
        self.assertEqual((res.ended, res.session_id, res.note, res.error, res.cost), (None, "cse_1", "sent", "", None))
        self.assertEqual(seen[0].prompt, "지시")
        self.assertEqual(RemoteSessionRunner(lambda req: None).run_turn(TurnRequest("A", "p", Path("."))).session_id, None)

        def boom(req):
            raise ConnectionError(f"secret {TOKEN}")
        res = RemoteSessionRunner(boom).run_turn(TurnRequest("A", "p", Path(".")))
        self.assertEqual((res.ended, res.error), (None, "send_failed:ConnectionError"))
        self.assertNotIn(TOKEN, res.note + res.error)

    def test_outbox_callback(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            cb = OutboxCallback(d)
            out = cb(TurnRequest("A", "지시 본문", Path("."), resume_id="cse_0"))
            self.assertIsNone(out["session_id"])
            files = sorted((Path(d) / "A").glob("*.json"))
            self.assertEqual(len(files), 1)
            self.assertEqual(json.loads(files[0].read_text()), {"session": "A", "prompt": "지시 본문", "resume_id": "cse_0"})
            (Path(d) / "A" / "session_id").write_text("cse_9\n")
            self.assertEqual(cb(TurnRequest("A", "다음", Path(".")))["session_id"], "cse_9")


class RemoteIsolationConfigTest(unittest.TestCase):
    def raw(self, **kw):
        r = {"schema": "ga-config/1", "hub": {"name": "hub"}, "integration_branch": "integ",
             "repos": {"alpha": {"path": "a", "remote": "origin"}},
             "sessions": {"A": {"prefix": "A", "branch": "sess-a", "repos": ["alpha"]}}, "isolation": "remote"}
        r.update(kw)
        return r

    def test_remote_needs_a_remote_and_push_is_bool(self):
        self.assertEqual(gacfg.from_dict(self.raw()).isolation, "remote")
        self.assertTrue(gacfg.from_dict(self.raw()).repos["alpha"].push)
        with self.assertRaises(FormError):
            gacfg.from_dict(self.raw(repos={"alpha": {"path": "a"}}))
        with self.assertRaises(FormError):
            gacfg.from_dict(self.raw(repos={"alpha": {"path": "a", "remote": "origin", "push": "no"}}))
        with self.assertRaises(FormError):
            gacfg.from_dict(self.raw(isolation="cloud"))

    def test_turn_prompt_for_remote_sessions(self):
        cfg = gacfg.from_dict(self.raw(repos={"alpha": {"path": "a", "remote": "origin", "slug": "o/alpha"}},
                                       sessions={"A": {"prefix": "A", "branch": "sess-a", "repos": ["alpha"], "channel": "o/alpha#7"}}))
        p = turn_prompt(cfg, "A", "```ga\n{}\n```\n")
        self.assertIn("o/alpha (브랜치 sess-a)", p)
        self.assertIn("그 브랜치만 push", p)
        self.assertIn("o/alpha#7", p)
        self.assertIn("<!-- ga-author: A -->", p)
        self.assertNotIn("ga post", p)


class RemoteHubRoundTest(FakeGitHubCase):
    """One hub round with remote isolation: directive on a GitHub issue (fake server), a 'cloud session' (here a
    plain clone of the remote) gets the turn through RemoteSessionRunner, pushes its branch, reports on its issue;
    the hub reads the report from the issue, fetches the remote and integrates — locally only (push: false)."""

    def test_round(self):
        w = World(remote=True, isolation="remote", server_hooks=False)
        self.addCleanup(w.close)
        w.cfg.repos["alpha"].push = False
        w.hub.channel = self.channel()
        bare = w.tmp / "remotes" / "alpha.git"
        remote_integ = git(bare, "rev-parse", "integ")
        cloud = w.tmp / "cloud"

        def send(req):
            # stands in for the remote session: its own checkout, its own branch, its report on its own issue
            self.assertNotIn(TOKEN, req.prompt)
            self.assertIn("CMD-A1", req.prompt)
            git(w.tmp, "clone", "--quiet", str(bare), str(cloud))
            git(cloud, "checkout", "--quiet", "-b", "sess-a", "origin/integ")
            (cloud / "remote.txt").write_text("from the cloud\n", encoding="utf-8")
            git(cloud, "add", "-A")
            git(cloud, "commit", "--quiet", "-m", "remote work")
            git(cloud, "push", "--quiet", "origin", "sess-a")
            sha = git(cloud, "rev-parse", "HEAD")
            head = {"schema": "report/1", "from": "A", "handled": [{"id": "CMD-A1", "rev_seen": 1, "status": "done"}],
                    "commits": [{"repo": "alpha", "branch": "sess-a", "sha": sha}]}
            self.channel().post("A", "A", dump_text(head, "## Result\n했다\n"))
            sent["sha"] = sha
            return {"session_id": "cse_remote_A"}

        sent = {}
        w.hub.runner = RemoteSessionRunner(send)
        post, findings, gates = w.hub.send(directive("CMD-A1", "A"))
        self.assertIsNotNone(post)
        self.assertEqual(gates, [])
        self.assertIn("CMD-A1", self.gh.comments[1][0]["body"])  # the directive went to A's issue
        self.assertTrue(self.gh.comments[1][0]["body"].rstrip().endswith("<!-- ga-author: hub -->"))
        st = w.hub.load_state()
        turn = st["turns"][0]
        self.assertEqual((turn["runner"], turn["ended"], turn["session_id"]), ("remote", None, "cse_remote_A"))
        self.assertEqual(st["resume"]["A"], "cse_remote_A")
        self.assertEqual(st["spent"]["cost_unknown_runs"], 1)
        self.assertFalse((w.ga / "worktrees" / "A").exists())  # the hub made no checkout for a remote session
        res = w.hub.tick()
        self.assertEqual(res.integrated, {"alpha": sent["sha"]})
        self.assertEqual(w.integ("alpha"), sent["sha"])
        self.assertEqual(git(bare, "rev-parse", "integ"), remote_integ)  # push: false — the remote is untouched
        turn = w.hub.load_state()["turns"][0]
        self.assertEqual(turn["diag"]["committed"], True)
        self.assertEqual(turn["diag"]["posts"], 1)

    def test_unreported_remote_push_is_not_integrated(self):
        w = World(remote=True, isolation="remote", server_hooks=False)
        self.addCleanup(w.close)
        w.cfg.repos["alpha"].push = False
        w.hub.channel = self.channel()
        w.hub.runner = RemoteSessionRunner(lambda req: {})
        w.hub.send(directive("CMD-A1", "A"))
        bare = w.tmp / "remotes" / "alpha.git"
        cloud = w.tmp / "cloud"
        git(w.tmp, "clone", "--quiet", str(bare), str(cloud))
        git(cloud, "checkout", "--quiet", "-b", "sess-a", "origin/integ")
        (cloud / "x.txt").write_text("x\n", encoding="utf-8")
        git(cloud, "add", "-A")
        git(cloud, "commit", "--quiet", "-m", "x")
        git(cloud, "push", "--quiet", "origin", "sess-a")
        start = w.integ("alpha")
        self.assertTrue(w.hub.tick().quiet)
        self.assertEqual(w.integ("alpha"), start)


if __name__ == "__main__":
    unittest.main()
