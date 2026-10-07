"""The forms registry: the closed enums and form fields as data (registry.json)."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

FILE = Path(__file__).with_name('registry.json')


def load() -> Any:
    return json.loads(FILE.read_text(encoding='utf-8'))


def render_doc(reg: Any) -> str:
    out = ["# Forms", "", "Generated from ga/forms/registry.json. Do not edit.", "", "## Enums", ""]
    for name, values in reg.get('enums', {}).items():
        out.append(f"### {name}")
        out.append("")
        for v in values:
            out.append(f"- `{v}`")
        out.append("")
    out += ["## Forms", ""]
    for form, spec in reg.get('forms', {}).items():
        out.append(f"### {form}")
        out.append("")
        for fname, fspec in spec.get('fields', {}).items():
            req = "required" if fspec.get('required') else "optional"
            out.append(f"- `{fname}` ({req})")
        out.append("")
    return "\n".join(out) + "\n"
