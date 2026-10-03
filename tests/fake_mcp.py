"""A fake MCP stdio server for tests (CMD-GA21): tools `echo` (returns its arguments as text) and `fail` (isError).
Each tools/call appends the tool name to FAKE_MCP_LOG when set."""
import json
import os
import sys

for line in sys.stdin:
    msg = json.loads(line)
    if "id" not in msg:
        continue
    if msg["method"] == "initialize":
        res = {"protocolVersion": msg["params"]["protocolVersion"], "capabilities": {"tools": {}},
               "serverInfo": {"name": "fake", "version": "0"}}
    elif msg["method"] == "tools/call":
        name, args = msg["params"]["name"], msg["params"].get("arguments", {})
        if os.environ.get("FAKE_MCP_LOG"):
            with open(os.environ["FAKE_MCP_LOG"], "a") as f:
                f.write(name + "\n")
        print(json.dumps({"jsonrpc": "2.0", "method": "notifications/message", "params": {"level": "info"}}), flush=True)
        if name == "fail":
            res = {"content": [{"type": "text", "text": "no"}], "isError": True}
        else:
            res = {"content": [{"type": "text", "text": json.dumps(args, sort_keys=True)}]}
    else:
        print(json.dumps({"jsonrpc": "2.0", "id": msg["id"], "error": {"code": -32601, "message": "no"}}), flush=True)
        continue
    print(json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": res}), flush=True)
