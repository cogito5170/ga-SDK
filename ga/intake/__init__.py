"""Intake (CMD-GA37), the first stage of the GA Engine: the model-agnostic top layer. ``ga do "<request>"`` -> repo
summary and project state (code) -> one model turn on any backend -> task/1 checked by code (ga/forms/task.py) ->
routed by kind. See engine.py."""
from .engine import MAX_REPAIRS, Intake, Result, Turn, build_spec, handle, task_id, usage_counts
from .fragment import Resolution, resolve, withhold
from .prompt import INSTRUCTION, message, parse, repair, whole
from .route import planner_stub, route
from .state import State
from .summary import CAP, Summary, summarize

__all__ = ["Intake", "Result", "Turn", "MAX_REPAIRS", "build_spec", "handle", "task_id", "usage_counts", "Resolution",
           "resolve", "withhold", "INSTRUCTION", "message", "parse", "repair", "whole", "planner_stub", "route",
           "State", "CAP", "Summary", "summarize"]
