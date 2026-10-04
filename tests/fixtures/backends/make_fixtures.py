"""GA28 recorded fixtures: one scripted task (add 2 and 3, then report) as each built-in backend's host prints it.

    python3 tests/fixtures/backends/make_fixtures.py

Shapes: claude_cli and gemini_cli as their JSON output is documented (claude -p --output-format json; Gemini CLI 0.62
stream-json, gemini_cli.py); openai_http / anthropic_http as the Chat Completions / Messages responses. A (assumed, in
the adapters' docs): agv's stream-json with a ``model`` and a ``usage`` (gpt, claude) or ``usageMetadata`` (gemini) in
the family's provider shape; codex exec --json naming the model on ``thread.started``. No network, no model call.
"""
import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
PLAN1 = {"schema": "ga-plan/1", "steps": [{"id": "a", "tool": "add", "args": {"a": 2, "b": 3}}],
         "next": {"prompt": "report the sum", "after": ["a"]}, "say": "adding"}
PLAN2 = {"schema": "ga-plan/1", "steps": [], "next": None, "say": "5"}
ANSWERS = ["```json\n" + json.dumps(p) + "\n```" for p in (PLAN1, PLAN2)]
AGV = {"gpt": ("gpt-5.1", "usage", lambda i: {"prompt_tokens": 1200 + i, "completion_tokens": 40, "total_tokens": 1240 + i,
                                              "prompt_tokens_details": {"cached_tokens": 0}}),
       "claude": ("claude-sonnet-4-5", "usage", lambda i: {"input_tokens": 1150 + i, "output_tokens": 45,
                                                           "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}),
       "gemini": ("gemini-3.8-flash-high", "usageMetadata", lambda i: {"promptTokenCount": 1100 + i,
                                                                       "candidatesTokenCount": 50, "totalTokenCount": 1150 + i})}
USAGE_TEXT = "Gemini models: 96% remaining, resets 2026-10-11 00:56 KST (weekly)\n" \
             "Claude and GPT models: 100% remaining, resets 2026-10-11 01:29 KST (weekly)\n"


def lines(*objs):
    return "".join(json.dumps(o) + "\n" for o in objs)


def write(name, i, text):
    d = HERE / name
    d.mkdir(exist_ok=True)
    (d / f"{i}.out").write_text(text, encoding="utf-8")


def main():
    for fam, (slug, key, usage) in AGV.items():
        for i, a in enumerate(ANSWERS, 1):
            write(f"agv_{fam}", i, lines({"type": "init", "session": "s"},
                                         {"type": "message", "role": "assistant", "content": a[:20], "delta": True},
                                         {"type": "message", "role": "assistant", "content": a[20:], "delta": True},
                                         {"type": "result", "status": "success", "model": slug, key: usage(i)}))
        write(f"agv_{fam}", "usage", USAGE_TEXT)
    for i, a in enumerate(ANSWERS, 1):
        write("gemini_cli", i, lines({"type": "init", "session_id": "g1", "model": "gemini-3-flash-preview"},
                                     {"type": "message", "role": "assistant", "content": a},
                                     {"type": "result", "status": "success", "stats": {
                                         "input_tokens": 12900 + i, "output_tokens": 60, "total_tokens": 12960 + i,
                                         "models": {"gemini-3-flash-preview": {}}}}))
        write("claude_cli", i, json.dumps({"type": "result", "subtype": "success", "is_error": False, "result": a,
                                           "session_id": "c1", "num_turns": 1, "total_cost_usd": 0.001,
                                           "usage": {"input_tokens": 1050 + i, "cache_read_input_tokens": 0,
                                                     "cache_creation_input_tokens": 0, "output_tokens": 55},
                                           "modelUsage": {"claude-haiku-4-5-20251001": {"inputTokens": 1050 + i,
                                                                                        "outputTokens": 55}}}) + "\n")
        write("codex_cli", i, lines({"type": "thread.started", "thread_id": "t1", "model": "gpt-5.1-codex"},
                                    {"type": "turn.started"},
                                    {"type": "item.completed", "item": {"id": "i0", "type": "agent_message", "text": a}},
                                    {"type": "turn.completed", "usage": {"input_tokens": 9000 + i,
                                                                         "cached_input_tokens": 4000, "output_tokens": 70}}))
        write("openai_http", i, json.dumps({"id": f"r{i}", "object": "chat.completion", "model": "gpt-5.1",
                                            "choices": [{"index": 0, "message": {"role": "assistant", "content": a},
                                                         "finish_reason": "stop"}],
                                            "usage": {"prompt_tokens": 1000 + i, "completion_tokens": 50,
                                                      "total_tokens": 1050 + i}}))
        write("anthropic_http", i, json.dumps({"id": f"m{i}", "type": "message", "role": "assistant",
                                               "model": "claude-haiku-4-5-20251001",
                                               "content": [{"type": "text", "text": a}], "stop_reason": "end_turn",
                                               "usage": {"input_tokens": 1000 + i, "output_tokens": 50}}))


if __name__ == "__main__":
    main()
