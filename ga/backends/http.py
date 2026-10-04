"""HTTP backends (CMD-GA28 S2/S5): openai_http (OpenAI-compatible chat completions; covers local servers) and
anthropic_http (the Messages API). Behind the ``http`` extra (``pip install 'ga-sdk[http]'``: httpx is the default
transport); without it a turn fails with ``http_extra_missing``. Every plan call is bare: the request carries the
``system`` text and one user message — no ``tools`` field.

The key: only ``os.environ[options.key_env]``, read when a request is built, sent only in the auth header, never kept on
the runner, logged, written to the state or put into an error (errors are labels; no response text). A config has no
place for a key itself. ``key_env`` may be absent for a local server that wants none.
"""
from __future__ import annotations

import json
import os
import re
import time
from typing import Any, Callable

from .base import API_VERSION, BackendError, BackendTurn, ConfigError, check_served, rate_limited

Transport = Callable[[str, dict[str, str], bytes, float], tuple[int, dict[str, str], bytes]]
ENV_NAME = re.compile(r"^[A-Z_][A-Z0-9_]{0,63}$")


def httpx_transport(url: str, headers: dict[str, str], body: bytes, timeout_s: float) -> tuple[int, dict[str, str], bytes]:
    try:
        import httpx
    except ImportError:
        raise BackendError("http_extra_missing") from None
    try:
        r = httpx.post(url, headers=headers, content=body, timeout=timeout_s)
    except httpx.TimeoutException:
        raise BackendError("timeout") from None
    except httpx.HTTPError as e:
        raise BackendError(f"http_{type(e).__name__}"[:60]) from None
    return r.status_code, {k.lower(): v for k, v in r.headers.items()}, r.content


class HttpRunner:
    resumes, bare = False, True
    usage_format = ""
    max_tokens = 4096

    def __init__(self, model: str, base_url: str, key_env: str | None, *, timeout_s: float = 600.0,
                 transport: Transport | None = None, max_tokens: int | None = None):
        self.model, self.base_url, self.key_env, self.timeout_s = model, base_url.rstrip("/"), key_env, timeout_s
        self.transport = transport or httpx_transport
        self.max_tokens = max_tokens or self.max_tokens

    def _key(self) -> str | None:
        if self.key_env is None:
            return None
        key = os.environ.get(self.key_env)
        if not key:
            raise BackendError("key_env_unset")
        return key

    def request(self, prompt: str, system: str | None) -> tuple[str, dict[str, str], dict[str, Any]]:
        raise NotImplementedError

    def parse(self, data: dict[str, Any]) -> tuple[str, list[str], dict[str, Any] | None]:
        raise NotImplementedError

    def run_turn(self, prompt: str, session_id: str | None = None, *, system: str | None = None,
                 on_wait: Callable[[float], None] | None = None, wait_every_s: float | None = None) -> BackendTurn:
        if system is None:
            raise BackendError("bare_without_system")
        t0 = time.monotonic()
        url, headers, body = self.request(prompt, system)
        status, rh, raw = self.transport(url, headers, json.dumps(body).encode("utf-8"), self.timeout_s)
        if status == 429:
            raise rate_limited((rh or {}).get("retry-after"))
        if status != 200:
            raise BackendError(f"http_{status}")
        try:
            data = json.loads(raw)
        except ValueError:
            raise BackendError("bad_json") from None
        if not isinstance(data, dict):
            raise BackendError("bad_json")
        answer, served, usage = self.parse(data)
        check_served(served, self.model)
        return BackendTurn(answer, served, usage, self.usage_format if usage is not None else None, None,
                           round(time.monotonic() - t0, 3), 1)


class OpenAIRunner(HttpRunner):
    usage_format = "openai"

    def request(self, prompt: str, system: str | None) -> tuple[str, dict[str, str], dict[str, Any]]:
        key = self._key()
        headers = {"content-type": "application/json", **({"authorization": f"Bearer {key}"} if key else {})}
        body = {"model": self.model, "max_tokens": self.max_tokens,
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}]}
        return f"{self.base_url}/chat/completions", headers, body

    def parse(self, data: dict[str, Any]) -> tuple[str, list[str], dict[str, Any] | None]:
        try:
            answer = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            raise BackendError("no_answer") from None
        served = [data["model"]] if isinstance(data.get("model"), str) and data["model"] else []
        return str(answer or ""), served, data.get("usage") if isinstance(data.get("usage"), dict) else None


class AnthropicRunner(HttpRunner):
    usage_format = "anthropic"
    version = "2023-06-01"

    def request(self, prompt: str, system: str | None) -> tuple[str, dict[str, str], dict[str, Any]]:
        key = self._key()
        headers = {"content-type": "application/json", "anthropic-version": self.version,
                   **({"x-api-key": key} if key else {})}
        body = {"model": self.model, "max_tokens": self.max_tokens, "system": system,
                "messages": [{"role": "user", "content": prompt}]}
        return f"{self.base_url}/v1/messages", headers, body

    def parse(self, data: dict[str, Any]) -> tuple[str, list[str], dict[str, Any] | None]:
        content = data.get("content")
        if not isinstance(content, list):
            raise BackendError("no_answer")
        answer = "".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text")
        served = [data["model"]] if isinstance(data.get("model"), str) and data["model"] else []
        return answer, served, data.get("usage") if isinstance(data.get("usage"), dict) else None


class _Http:
    api, version = API_VERSION, "1"
    runner: type = HttpRunner
    default_url = ""
    overhead = {"bare": True, "tokens": 0, "source": "documented: the request body is the system text and one user "
                "message, no tools; the provider's own message framing is not counted", "closest": "bare"}

    def create(self, model: str, options: dict[str, Any], ctx: dict[str, Any]) -> HttpRunner:
        extra = sorted(set(options) - {"base_url", "key_env", "max_tokens"})
        if extra:
            raise ConfigError(f"{self.name}: unknown option(s) {', '.join(extra)}")
        key_env = options.get("key_env")
        if key_env is not None and not (isinstance(key_env, str) and ENV_NAME.match(key_env)):
            raise ConfigError(f"{self.name}: options.key_env must name an environment variable (A-Z, 0-9, _)")
        url = options.get("base_url", self.default_url)
        if not (isinstance(url, str) and re.match(r"^https?://", url)):
            raise ConfigError(f"{self.name}: options.base_url must be an http(s) URL")
        mt = options.get("max_tokens")
        if mt is not None and not (isinstance(mt, int) and not isinstance(mt, bool) and mt > 0):
            raise ConfigError(f"{self.name}: options.max_tokens must be a positive integer")
        return self.runner(model, url, key_env, timeout_s=ctx.get("timeout_s", 600.0), transport=ctx.get("transport"),
                           max_tokens=mt)


class _OpenAIHttp(_Http):
    name, runner, default_url = "openai_http", OpenAIRunner, "https://api.openai.com/v1"


class _AnthropicHttp(_Http):
    name, runner, default_url = "anthropic_http", AnthropicRunner, "https://api.anthropic.com"


OPENAI_HTTP = _OpenAIHttp()
ANTHROPIC_HTTP = _AnthropicHttp()
