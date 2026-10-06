"""ga.ops — the ops tick (CMD-GA57): close operational anomalies with rlo's loop instead of recording them.

    observe (code, zero tokens) -> rule/1 table -> action-spec/1 -> guard -> execute -> VERIFY next tick -> escalate

A model is asked only when no rule picks an action (``core.Ops._ask_model``). See docs/OPS.md.
"""
from .core import Effects, Ops, guard, load_table, match, observe  # noqa: F401
