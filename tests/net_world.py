"""A peer network on fakes for CMD-GA31 tests: a git mailbox (bare remote + one clone), a ga-config/1 with a network
section, and fake backend plugins (no network, no model call). The fakes answer from the prompt they are given."""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest import mock

from ga import config as gacfg
from ga.backends.base import BackendTurn

DESCRIPTION = "a red bicycle leaning on a blue door"
CHECK_BICYCLE = [sys.executable, "-c", "import sys; sys.exit(0 if 'bicycle' in sys.stdin.read() else 1)"]


def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout


def ntok(s):
    return -(-len((s or "").encode("utf-8")) // 4)


def entry(model, caps, price, tiers=None, effort=None, family="claude", window=200000):
    return {"model": model, "family": family, "capabilities": caps, "context_window": window, "effort_option": effort,
            "tiers": tiers or {"R0": None, "R1": None, "R2": None, "R3": None}, "price": price, "quota_family": "fake"}


def _peer_template(system):
    m = re.search(r"```peer\n(.*?)\n```", system or "", re.S)
    return json.loads(m.group(1)) if m else None


def _head_template(system):
    m = re.search(r"```ga\n(.*?)\n```", system or "", re.S)
    return m.group(1) if m else None


def answer(system, prompt, *, result="", peer=None, state="done: answered / next: none", head=None):
    out = "```ga\n" + (head or _head_template(system)) + "\n```\n## Result\n" + result + "\n"
    if peer is not None:
        out += "```peer\n" + json.dumps(peer) + "\n```\n"
    if state is not None:
        out += "```state\n" + state + "\n```\n"
    return out


def default_script(prompt, system, model, options):
    """Observe jobs: fill the peer template with what a vision model 'sees'. Tasks: a caption from the facts."""
    tpl = _peer_template(system)
    if tpl is not None:
        if "vision" not in model:
            return answer(system, prompt, result="UNKNOWN")
        tpl["value"] = DESCRIPTION
        return answer(system, prompt, result=DESCRIPTION, peer=tpl)
    m = re.search(r"image\.img1\.description = ([^(]+) \(", prompt)
    return answer(system, prompt, result=("Caption: " + m.group(1).strip()) if m else "UNKNOWN")


class FakeRunner:
    resumes, usage_format = False, "anthropic"

    def __init__(self, backend, model, options):
        self.b, self.model, self.options = backend, model, options
        self.bare = backend.bare

    def run_turn(self, prompt, session_id=None, *, system=None, on_wait=None, wait_every_s=None):
        self.b.calls.append({"model": self.model, "options": dict(self.options), "prompt": prompt, "system": system})
        text = self.b.script(prompt, system, self.model, self.options)
        served = [self.b.serve_as or self.model]
        usage = {"input_tokens": ntok(prompt) + ntok(system), "output_tokens": ntok(text),
                 "cache_read_input_tokens": self.b.cache_read, "cache_creation_input_tokens": 0}
        return BackendTurn(text, served, usage, "anthropic", None, 0.01, 1)


class FakeBackend:
    version, api = "1", 1
    overhead = {"bare": True, "tokens": 0, "source": "fake", "closest": "bare"}

    def __init__(self, name, catalog, script=default_script, bare=True):
        self.name, self.catalog, self.script, self.bare = name, catalog, script, bare
        self.calls, self.serve_as, self.cache_read = [], None, 0

    def create(self, model, options, ctx):
        return FakeRunner(self, model, options)


def fakes():
    return {"fake_text": FakeBackend("fake_text", [entry("claude-fake-haiku", ["text"], {"in": 1, "out": 5})]),
            "fake_vision": FakeBackend("fake_vision", [entry("claude-fake-vision", ["text", "vision"],
                                                             {"in": 3, "out": 15})])}


def t1_network(**over):
    net = {"mode": "peer", "mailbox": "mail", "theta": 0.3,
           "refs": {"image.img1.description": {"needs": {"capabilities": ["vision"]}, "input": "img1.png"}},
           "nodes": {
               "A": {"backends": ["fake_text"], "pack_max_tokens": 3000,
                     "task": {"id": "CMD-T1", "goal": "write a one-line caption for image img1", "class": "caption",
                              "uses": ["image.img1.description"], "needs": {"capabilities": ["text"], "tier": "R0"},
                              "check": CHECK_BICYCLE}},
               "B": {"backends": ["fake_text", "fake_vision"], "pack_max_tokens": 3000},
               "C": {"backends": ["fake_text"], "pack_max_tokens": 3000,
                     "facts": {"doc.title": {"value": "NETWORK", "evidence": "NETWORK.md#L1"}},
                     "task": {"id": "CMD-T4", "goal": "the document's title", "uses": ["doc.title"],
                              "answer": "doc.title"}}}}
    net.update(over)
    return net


class World:
    def __init__(self, test, network=None, *, mode=None):
        self.tmp = Path(tempfile.mkdtemp())
        test.addCleanup(shutil.rmtree, self.tmp, True)
        gc = self.tmp / "gitconfig"
        gc.write_text("[init]\n\tdefaultBranch = main\n")
        p = mock.patch.dict(os.environ, {"GIT_CONFIG_GLOBAL": str(gc), "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                                         "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"})
        p.start()
        test.addCleanup(p.stop)
        bare = self.tmp / "mail.git"
        git(self.tmp, "init", "-q", "--bare", str(bare))
        git(self.tmp, "clone", "-q", str(bare), str(self.tmp / "mail"))
        (self.tmp / "img1.png").write_bytes(b"\x89PNG fake")
        self.network = network if network is not None else t1_network()
        if mode:
            self.network = dict(self.network, mode=mode)
        self.raw = {"schema": "ga-config/1", "hub": {"name": "baseline"}, "integration_branch": "integration",
                    "repos": {}, "sessions": {}, "network": self.network}
        self.config = self.tmp / "ga.json"
        self.config.write_text(json.dumps(self.raw))
        self.ga = self.tmp / ".ga"
        self.backends = fakes()
        self.t = [1000.0]

    def cfg(self):
        return gacfg.load(self.config)

    def clock(self):
        return self.t[0]

    def get(self, name):
        try:
            return self.backends[name]
        except KeyError:
            raise KeyError(f"no ga.backends plugin {name!r}") from None

    def node(self, me):
        from ga.net.node import Node
        return Node(self.cfg(), me, ga_dir=self.ga, get_backend=self.get, clock=self.clock)

    def step(self, me):
        self.t[0] += 10
        return self.node(me).step()

    def rounds(self, n, nodes=("A", "B", "C")):
        out = []
        for _ in range(n):
            for me in nodes:
                out.append(self.step(me))
        return out

    def read(self, me, name):
        return json.loads((self.ga / "nodes" / me / name).read_text())

    def l0(self, me):
        f = self.ga / "nodes" / me / "telemetry.jsonl"
        return [json.loads(x) for x in f.read_text().splitlines()] if f.exists() else []
