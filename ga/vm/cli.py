"""``ga vm`` argument parsing (CMD-OPS1)."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import core


def main(argv: list[str] | None = None, *, runner: core.Runner | None = None, free=core.disk_free, tmp: str = "/tmp") -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    ap = argparse.ArgumentParser(prog="ga vm", description="GA Engine on a VM: user-level systemd services (docs/VM.md)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(p, urls=False):
        p.add_argument("--home", default=str(Path.home()))
        if urls:
            p.add_argument("--baseline-url", default=core.BASELINE_URL)
            p.add_argument("--sdk-url", default=core.SDK_URL)
            p.add_argument("--branch", default=core.BRANCH)
        return p

    p = common(sub.add_parser("check", help="one verdict JSON; exit 1 when not ready"), True)
    p.add_argument("--min-free-gb", type=float, default=core.MIN_FREE_GB)
    p.add_argument("--disk-only", action="store_true", help="only the disk guard (the units' ExecStartPre)")
    p = common(sub.add_parser("install", help="idempotent install; never runs sudo"), True)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--min-free-gb", type=float, default=core.MIN_FREE_GB)
    common(sub.add_parser("status"))
    common(sub.add_parser("enable-hub", help="enable the shadow hub timer when this ga has `ga hub tick --shadow`"))
    common(sub.add_parser("uninstall", help="stop and remove the units and ~/.ga/console.json; checkouts stay"))
    a = ap.parse_args(argv)
    home = Path(a.home)
    if a.cmd == "check":
        if a.disk_only:
            d = core.check_disk(home, a.min_free_gb, free)
            print(json.dumps({"ready": d["ok"], "disk": d}))
            if not d["ok"]:
                print(f"ga vm: disk guard: {d['free_gb']} GB free < {d['min_gb']} GB on {home} — service not started", file=sys.stderr)
            return 0 if d["ok"] else 1
        v = core.check(home, min_free_gb=a.min_free_gb, runner=runner, free=free, tmp=tmp,
                       baseline_url=a.baseline_url, sdk_url=a.sdk_url)
        print(json.dumps(v, ensure_ascii=False))
        return 0 if v["ready"] else 1
    if a.cmd == "install":
        return core.install(home, dry_run=a.dry_run, min_free_gb=a.min_free_gb, runner=runner, free=free, tmp=tmp,
                            baseline_url=a.baseline_url, sdk_url=a.sdk_url, branch=a.branch)
    if a.cmd == "status":
        return core.status(home, runner=runner, free=free)
    if a.cmd == "enable-hub":
        return core.enable_hub(home, runner=runner)
    return core.uninstall(home, runner=runner)
