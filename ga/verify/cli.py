"""``ga verify``: the subcommand wiring (CMD-GA39), kept here so ga/__main__.py gains two lines."""
from __future__ import annotations


def add_parser(sub) -> None:
    p = sub.add_parser("verify", help="GA Verifier: mutations from a diff, lint a code item (CMD-GA39)")
    vs = p.add_subparsers(dest="verify_cmd", required=True)
    m = vs.add_parser("mutations", help="a ga judge mutation spec from the code operators on base...head")
    m.add_argument("--base", required=True); m.add_argument("--head", default="HEAD")
    m.add_argument("--repo", default="."); m.add_argument("--max", type=int, help="keep the N best-ranked")
    m.add_argument("--out", help="write the spec here (default stdout)")
    m.set_defaults(fn=_mutations)
    i = vs.add_parser("item", help="lint a code item before it is sent (exit 1 on errors)")
    i.add_argument("item"); i.add_argument("--repo", help="the repo the item works in (for the schema check)")
    i.set_defaults(fn=_item)


def _mutations(args) -> int:
    from .mutate import main
    return main(args)


def _item(args) -> int:
    from .items import main
    return main(args)
