"""A minimal MCP client over stdio (CMD-GA21 S2): ga calls an extension's MCP tools directly, not through a model.

JSON-RPC 2.0, one message per line: ``initialize`` → ``notifications/initialized`` → ``tools/call``. The server is one
child process per client, started on first use and closed with ``close()``. Standard library only.
"""
from __future__ import annotations

import json
import queue
import subprocess
import threading
from typing import Any

PROTOCOL_VERSION = "2025-06-18"


class MCPError(RuntimeError):
    pass


class MCPToolError(MCPError):
    """The tool ran and said it failed (``isError``)."""


class StdioClient:
    def __init__(self, command: list[str], *, cwd: str | None = None, env: dict[str, str] | None = None,
                 timeout_s: float = 120.0):
        self.command, self.cwd, self.env, self.timeout_s = list(command), cwd, env, timeout_s
        self.proc: subprocess.Popen | None = None
        self._lines: queue.Queue = queue.Queue()
        self._id = 0
        self._lock = threading.Lock()  # one request at a time per server: answers are read from one queue

    def _start(self) -> None:
        try:
            self.proc = subprocess.Popen(self.command, cwd=self.cwd, env=self.env, stdin=subprocess.PIPE,
                                         stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, bufsize=1)
        except OSError as e:
            raise MCPError(f"cannot start: {type(e).__name__}") from None
        threading.Thread(target=self._read, daemon=True).start()
        self._request("initialize", {"protocolVersion": PROTOCOL_VERSION, "capabilities": {},
                                     "clientInfo": {"name": "ga", "version": "0"}})
        self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def _read(self) -> None:
        assert self.proc and self.proc.stdout
        for line in self.proc.stdout:
            self._lines.put(line)
        self._lines.put(None)

    def _send(self, msg: dict) -> None:
        assert self.proc and self.proc.stdin
        try:
            self.proc.stdin.write(json.dumps(msg) + "\n")
            self.proc.stdin.flush()
        except OSError:
            raise MCPError("server closed its input") from None

    def _request(self, method: str, params: dict) -> dict:
        self._id += 1
        rid = self._id
        self._send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
        while True:
            try:
                line = self._lines.get(timeout=self.timeout_s)
            except queue.Empty:
                raise MCPError(f"{method}: no answer in {self.timeout_s:g}s") from None
            if line is None:
                raise MCPError(f"{method}: server exited")
            try:
                msg = json.loads(line)
            except ValueError:
                continue  # not a JSON-RPC line
            if not isinstance(msg, dict) or msg.get("id") != rid:
                continue  # a notification, or an answer to something else
            if "error" in msg:
                code = msg["error"].get("code") if isinstance(msg["error"], dict) else None
                raise MCPError(f"{method}: error {code}")
            return msg.get("result") or {}

    def call(self, tool: str, arguments: dict[str, Any]) -> Any:
        """tools/call -> the result's structuredContent if any, else the text of its content parts."""
        with self._lock:
            if self.proc is None:
                self._start()
            res = self._request("tools/call", {"name": tool, "arguments": arguments})
        if res.get("isError"):
            raise MCPToolError(tool)
        if res.get("structuredContent") is not None:
            return res["structuredContent"]
        parts = [c.get("text", "") for c in res.get("content") or [] if isinstance(c, dict) and c.get("type") == "text"]
        return "\n".join(parts)

    def close(self) -> None:
        if self.proc is None:
            return
        try:
            if self.proc.stdin:
                self.proc.stdin.close()
            self.proc.wait(timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            self.proc.kill()
            self.proc.wait()
        if self.proc.stdout:
            self.proc.stdout.close()
        self.proc = None
