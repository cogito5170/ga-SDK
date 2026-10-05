"""One Telemetry L0 record per session turn (CMD-GA29 S3): a ``run.end`` event in the l0-telemetry/1 envelope.

ga core stays standard library only, so the envelope is written here by hand; tests check it with
``telemetry.event.check`` (l0-telemetry, pinned through rlo-sdk 0.11.1). Only what the runner reported goes in:
a field it did not give is null and listed in ``unobserved`` (never 0).
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

SPEC = "l0-telemetry/1"
RUN_END = ("decision_ref", "model", "run_duration_ms", "api_duration_ms", "api_duration_without_retries_ms", "ttft_ms",
           "num_turns", "cost_usd", "terminal_reason", "result_subtype", "is_error", "api_error_status",
           "permission_denials", "tokens_sent", "tokens_received", "api_calls", "reported_input_tokens",
           "reported_output_tokens", "reported_cache_read_input_tokens", "reported_cache_creation_input_tokens")


def _int(v: Any) -> int | None:
    return v if isinstance(v, int) and not isinstance(v, bool) else None


def _num(v: Any) -> float | int | None:
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def run_end(run_id: str, result: Any, *, decision_ref: str | None = None, source: str = "ga_turn") -> dict[str, Any]:
    raw = result.raw if isinstance(getattr(result, "raw", None), dict) else {}
    u = result.usage or {}
    pd = raw.get("permission_denials")
    data: dict[str, Any] = {k: None for k in RUN_END}
    data.update(
        decision_ref=decision_ref, model=result.model,
        run_duration_ms=round(result.seconds * 1000, 3) if _num(result.seconds) is not None else None,
        api_duration_ms=_num(raw.get("duration_api_ms")), num_turns=_int(raw.get("num_turns")), cost_usd=_num(result.cost),
        terminal_reason=raw.get("terminal_reason") if isinstance(raw.get("terminal_reason"), str) else None,
        result_subtype=raw.get("subtype") if isinstance(raw.get("subtype"), str) else None,
        is_error=raw.get("is_error") if isinstance(raw.get("is_error"), bool) else None,
        permission_denials=len(pd) if isinstance(pd, list) else None,
        reported_input_tokens=_int(u.get("input")), reported_output_tokens=_int(u.get("output")),
        reported_cache_read_input_tokens=_int(u.get("cache_read")),
        reported_cache_creation_input_tokens=_int(u.get("cache_creation")),
    )
    return {"spec": SPEC, "id": f"{run_id}:0", "type": "run.end", "run_id": run_id, "seq": 0, "source": source,
            "at": None, "time_base": None, "data": data, "unobserved": [k for k in RUN_END if data[k] is None],
            "reported_null": []}


def event(typ: str, run_id: str, *, source: str = "ga_node", **data: Any) -> dict[str, Any]:
    """A plain fact event in the run.end envelope (CMD-GA34 S7: ``turn.started``, ``turn.progress``)."""
    return {"spec": SPEC, "id": f"{run_id}:{typ}", "type": typ, "run_id": run_id, "seq": 0, "source": source,
            "at": None, "time_base": None, "data": data}


def append(path: Path, event: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")


# CMD-GA31 S1 (NET1 contract): who sent what to whom, as facts only -- no pi, score, trust or usefulness here
PEER = ("from_session", "to_session", "msg_id", "in_reply_to", "schema", "bytes", "tokens_est")


def peer_message(direction: str, run_id: str, seq: int, *, sender: str, to: str, msg_id: str, schema: str | None,
                 nbytes: int, in_reply_to: str | None = None, source: str = "ga_node") -> dict[str, Any]:
    """``peer.message.sent`` / ``peer.message.received``; tokens_est = ceil(bytes / 4)."""
    if direction not in ("sent", "received"):
        raise ValueError("direction: sent or received")
    if not msg_id:
        raise ValueError("a peer message event needs msg_id")
    from .net import real
    if real.enabled() and schema is not None:  # l0-telemetry builds it (NET1): same envelope, bytes as ga counted them
        from telemetry import event
        return event.make(f"peer.message.{direction}", run_id, seq, source, from_session=sender, to_session=to,
                          msg_id=msg_id, in_reply_to=in_reply_to, schema=schema, bytes=nbytes,
                          tokens_est=-(-nbytes // 4))
    data = {"from_session": sender, "to_session": to, "msg_id": msg_id, "in_reply_to": in_reply_to,
            "schema": schema, "bytes": nbytes, "tokens_est": -(-nbytes // 4)}
    return {"spec": SPEC, "id": f"{run_id}:{seq}", "type": f"peer.message.{direction}", "run_id": run_id, "seq": seq,
            "source": source, "at": None, "time_base": None, "data": data,
            "unobserved": [k for k in PEER if data[k] is None and k != "in_reply_to"], "reported_null": []}
