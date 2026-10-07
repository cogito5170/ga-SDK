"""The VM budget policy (R1): read from ONE file at every call, never cached, never a constant.

Only the keys named here are read. ``cloud_top_baseline_usd_per_h`` and ``session_ctx_cap_tokens`` are cloud-only and
never applied. A policy that is missing, unreadable, not an object, has no ``vm_budget.caps`` object, or says
``halt: true`` (top level or in ``vm_budget``) refuses every call.
"""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

COORD, BASELINES, TOTAL_H, TOTAL_DAY, BUILD_ITEM = (
    "vm_coordination_dev_plus_ops_usd_per_h", "vm_baselines_usd_per_h", "vm_total_usd_per_h",
    "vm_total_usd_per_day", "build_per_item_usd")
CAP_NAMES = (COORD, BASELINES, TOTAL_H, TOTAL_DAY, BUILD_ITEM)
DEFAULT_APPLIES = {"coordination": [COORD, BASELINES], "intake": [BASELINES], "diagnosis": [BASELINES],
                   "opinion": [BASELINES], "build": [BUILD_ITEM]}
DEFAULT_WINDOWS = {"hour": "rolling_60m", "day": "rolling_24h"}
WINDOW_SECONDS = {"rolling_60m": 3600, "rolling_24h": 86400}


def default_path() -> Path:
    base = os.environ.get("GA_BASELINE_CHECKOUT") or str(Path.home() / "baseline")
    return Path(base) / "ops" / "flow" / "policy.json"


@dataclass
class Policy:
    ok: bool
    why: str = ""
    sha256: str = ""
    caps: dict[str, Any] = field(default_factory=dict)
    applies: dict[str, list[str]] = field(default_factory=dict)
    windows: dict[str, str] = field(default_factory=dict)

    def caps_for(self, purpose: str) -> list[str]:
        """The caps a purpose is checked against: its applies entry, plus total/h and total/day for every purpose."""
        names = list(self.applies.get(purpose, DEFAULT_APPLIES.get(purpose, [])))
        for n in (TOTAL_H, TOTAL_DAY):
            if n not in names:
                names.append(n)
        return [n for n in names if n in CAP_NAMES]

    def purposes_of(self, cap: str, seen: set[str]) -> set[str]:
        return {p for p in seen if cap in self.caps_for(p)}

    def window_s(self, cap: str) -> int:
        key = "day" if cap == TOTAL_DAY else "hour"
        return WINDOW_SECONDS.get(self.windows.get(key, ""), WINDOW_SECONDS[DEFAULT_WINDOWS[key]])


def load(path: str | Path) -> Policy:
    try:
        raw = Path(path).read_bytes()
        doc = json.loads(raw.decode("utf-8"))
    except (OSError, ValueError) as e:
        return Policy(False, f"policy unreadable: {type(e).__name__}")
    sha = hashlib.sha256(raw).hexdigest()
    if not isinstance(doc, dict):
        return Policy(False, "policy is not an object", sha)
    vb = doc.get("vm_budget")
    if not isinstance(vb, dict) or not isinstance(vb.get("caps"), dict):
        return Policy(False, "policy has no vm_budget.caps", sha)
    if doc.get("halt") is True or vb.get("halt") is True:
        return Policy(False, "policy halt:true", sha)
    applies = vb.get("applies") if isinstance(vb.get("applies"), dict) else {}
    windows = dict(DEFAULT_WINDOWS)
    if isinstance(vb.get("windows"), dict):
        windows.update({k: v for k, v in vb["windows"].items() if k in windows and v in WINDOW_SECONDS})
    caps = {k: v for k, v in vb["caps"].items() if k in CAP_NAMES}
    return Policy(True, "", sha, caps, {k: [c for c in v if isinstance(c, str)] for k, v in applies.items()
                                        if isinstance(v, list)}, windows)
