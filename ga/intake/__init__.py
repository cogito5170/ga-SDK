"""Intake (CMD-GA37): the model-agnostic top layer. ``ga do "<request>"`` -> repo summary (code) -> one model turn on
any backend -> task/1 checked by code (ga/forms/task.py). See engine.py."""
from .engine import MAX_REPAIRS, Intake, Result, Turn, build_spec, task_id, usage_counts
from .prompt import INSTRUCTION, message, parse, repair, whole
from .summary import CAP, Summary, summarize

__all__ = ["Intake", "Result", "Turn", "MAX_REPAIRS", "build_spec", "task_id", "usage_counts", "INSTRUCTION",
           "message", "parse", "repair", "whole", "CAP", "Summary", "summarize"]
