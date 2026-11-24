"""Streaming chat adapters for the ROMarr chat agent.

Three wire formats cover what operators actually run: Ollama's own API (a
local model on the LAN), any OpenAI-compatible `/chat/completions` endpoint
(OpenAI, LiteLLM, LM Studio, vLLM, llama.cpp, OpenRouter ...), and Anthropic's
Messages API. Each adapter turns ROMarr's neutral message list into the
provider's shape and yields the same three event kinds back:

- ``("text", str)`` -- a piece of the reply, as it streams;
- ``("tool_call", {"id", "name", "arguments"})`` -- one complete tool call;
- ``("usage", int)`` -- tokens the provider reports for the call.

Only `requests` is used, with redirects refused so a key never follows a 30x
to another host. Errors are raised as `ProviderError` with a plain sentence
the chat panel can show as-is; the API key is scrubbed from anything quoted
back from the provider.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from urllib.parse import urlsplit

import requests

PROVIDERS = ("ollama", "openai", "anthropic")
LABELS = {"ollama": "Ollama", "openai": "OpenAI-compatible API", "anthropic": "Anthropic"}
DEFAULT_URLS = {"ollama": "http://127.0.0.1:11434",
                "openai": "https://api.openai.com/v1",
                "anthropic": "https://api.anthropic.com"}
URL_ENV = {provider: f"ROMARR_CHAT_{provider.upper()}_URLS" for provider in PROVIDERS}
ANTHROPIC_VERSION = "2023-06-01"
#: Connect fast, but give a local model on a small GPU time to load and think.
TIMEOUT = (5, 180)
LIST_TIMEOUT = (5, 15)
#: A single streamed line larger than this is not a chat delta.
MAX_LINE = 1 << 20
MAX_MODELS = 500


class ProviderError(Exception):
    """A provider failure, worded for the person reading the chat panel."""

    def __init__(self, message: str, *, unsupported_tools: bool = False):
        super().__init__(message)
        self.unsupported_tools = unsupported_tools


def normalise_url(provider: str, url: str) -> str:
    """Return a clean base URL, or raise ValueError naming what is wrong."""
    url = str(url or "").strip().rstrip("/") or DEFAULT_URLS.get(provider, "")
    if any(c.isspace() or ord(c) < 32 or c in ("\\", "\x7f") for c in url):
        raise ValueError("Base URL contains invalid characters")
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError("Base URL must start with http:// or https://")
    if parts.username is not None or parts.password is not None:
        raise ValueError("Put the API key in the key field, not in the URL")
    if "?" in url or "#" in url:
        raise ValueError("Base URL must not carry a query string or fragment")
    try:
        _ = parts.port  # Validate an explicitly specified port.
    except ValueError:
        raise ValueError("Base URL has an invalid port") from None
    return url


def approved_base_url(provider: str, url: str) -> str:
    """Select a built-in or deployment-approved URL for this provider."""
    if provider not in DEFAULT_URLS:
        raise ValueError("Unknown chat provider")
    requested = normalise_url(provider, url)
    default = DEFAULT_URLS[provider]
    if requested == default:
        return default
    env_var = URL_ENV[provider]
    for entry in os.environ.get(env_var, "").split():
        try:
            approved = normalise_url(provider, entry)
        except ValueError:
            continue
        if requested == approved:
            return approved
    raise ValueError(f"Base URL is not approved for {LABELS[provider]}. "
                     f"Add the exact URL to {env_var} on the ROMarr server and restart it.")


def _scrub(text: str, cfg: dict) -> str:
    """Shorten provider text and remove the configured key if it was echoed."""
    text = " ".join(str(text or "").split())[:300]
    key = str(cfg.get("api_key") or "")
    return text.replace(key, "[key]") if len(key) >= 4 else text


def _headers(cfg: dict) -> dict:
    """Authentication headers for the configured provider."""
    key = str(cfg.get("api_key") or "")
    headers = {"Content-Type": "application/json"}
    if cfg["provider"] == "anthropic":
        headers["anthropic-version"] = ANTHROPIC_VERSION
        if key:
            headers["x-api-key"] = key
    elif key:
        headers["Authorization"] = f"Bearer {key}"
    return headers


def _endpoint(cfg: dict, path: str) -> str:
    """Join a provider path onto the base URL without doubling `/v1`."""
    base = approved_base_url(cfg["provider"], cfg.get("base_url", ""))
    if cfg["provider"] == "anthropic" and not base.endswith("/v1"):
        base += "/v1"
    return base + path


def _detail(response) -> str:
    """The provider's own error message, when it sent one."""
    try:
        data = response.json()
    except ValueError:
        return response.text
    if isinstance(data, dict):
        err = data.get("error")
        if isinstance(err, dict):
            return str(err.get("message") or err.get("type") or err)
        if err:
            return str(err)
        return str(data.get("message") or data.get("detail") or "")
    return ""


def _request(cfg: dict, method: str, path: str, *, body=None, stream=False, timeout=TIMEOUT):
    """One provider request; every failure becomes a readable ProviderError."""
    try:
        url = _endpoint(cfg, path)
    except ValueError as exc:
        raise ProviderError(str(exc)) from None
    label = LABELS[cfg["provider"]]
    try:
        response = requests.request(method, url, headers=_headers(cfg), json=body,
                                    stream=stream, timeout=timeout, allow_redirects=False)
    except requests.Timeout:
        raise ProviderError(f"{label} did not answer in time at {urlsplit(url).netloc}.") from None
    except requests.RequestException:
        raise ProviderError(f"Could not connect to {label} at {urlsplit(url).netloc}. "
                            "Check the base URL and that the service is running.") from None
    if response.status_code >= 300:
        detail = _scrub(_detail(response), cfg)
        response.close()
        code = response.status_code
        if code in (401, 403):
            raise ProviderError(f"{label} refused the API key (HTTP {code}). {detail}".strip())
        if 300 <= code < 400:
            raise ProviderError(f"{label} answered with a redirect (HTTP {code}); use the final base URL.")
        lowered = detail.lower()
        unsupported = code in (400, 404, 422, 501) and "tool" in lowered and (
            "support" in lowered or "unknown" in lowered or "invalid" in lowered
            or "not " in lowered or "unrecognized" in lowered)
        raise ProviderError(f"{label} answered HTTP {code}: {detail or 'no detail'}",
                            unsupported_tools=unsupported)
    return response


def list_models(cfg: dict) -> list[str]:
    """Model names the provider offers (Ollama reads `/api/tags`)."""
    if cfg["provider"] == "ollama":
        response = _request(cfg, "GET", "/api/tags", timeout=LIST_TIMEOUT)
        rows = (response.json() or {}).get("models") or []
        names = [str(r.get("name") or r.get("model") or "") for r in rows if isinstance(r, dict)]
    else:
        response = _request(cfg, "GET", "/models", timeout=LIST_TIMEOUT)
        rows = (response.json() or {}).get("data") or []
        names = [str(r.get("id") or "") for r in rows if isinstance(r, dict)]
    return sorted({n for n in names if n})[:MAX_MODELS]


def _lines(response) -> Iterator[str]:
    """Decoded, bounded lines from a streamed response."""
    try:
        for raw in response.iter_lines(chunk_size=1024, decode_unicode=False):
            if len(raw) > MAX_LINE:
                raise ProviderError("The provider sent an oversized stream line.")
            yield raw.decode("utf-8", "replace")
    except requests.RequestException:
        raise ProviderError("The provider connection dropped mid-reply.") from None
    finally:
        response.close()


def _sse(response) -> Iterator[dict]:
    """JSON payloads of a server-sent-event stream (`data:` lines)."""
    for line in _lines(response):
        if not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if not data or data == "[DONE]":
            continue
        try:
            payload = json.loads(data)
        except ValueError:
            continue
        if isinstance(payload, dict):
            yield payload


def _parse_args(raw) -> dict:
    """Tool arguments as a dict, whatever form the model produced."""
    if isinstance(raw, dict):
        return raw
    try:
        value = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {"_unparsed": str(raw)[:500]}
    return value if isinstance(value, dict) else {"_unparsed": str(raw)[:500]}


# -- Ollama --------------------------------------------------------------------

def _ollama_messages(messages: list[dict]) -> list[dict]:
    out = []
    for m in messages:
        if m["role"] == "tool":
            out.append({"role": "tool", "content": m["content"], "tool_name": m.get("name", "")})
        elif m["role"] == "assistant" and m.get("tool_calls"):
            out.append({"role": "assistant", "content": m.get("content", ""),
                        "tool_calls": [{"function": {"name": c["name"], "arguments": c["arguments"]}}
                                       for c in m["tool_calls"]]})
        else:
            out.append({"role": m["role"], "content": m.get("content", "")})
    return out


def _ollama(cfg, messages, tools, max_tokens, json_mode):
    body = {"model": cfg["model"], "messages": _ollama_messages(messages), "stream": True,
            "options": {"num_predict": max_tokens}}
    if tools:
        body["tools"] = [{"type": "function", "function": t} for t in tools]
    if json_mode:
        body["format"] = "json"
    response = _request(cfg, "POST", "/api/chat", body=body, stream=True)
    calls = 0
    # Ollama sends a tool call whole, with dict arguments -- or, from builds
    # that stream like the OpenAI API, as indexed pieces: the first names the
    # function, later ones carry string fragments of its arguments. Pieces are
    # collected by index and emitted once, whole, when the reply ends.
    pending: dict[int, dict] = {}

    def flush():
        for index in sorted(pending):
            slot = pending[index]
            yield "tool_call", {"id": slot["id"] or f"call_{index + 1}", "name": slot["name"],
                                "arguments": _parse_args(slot["args"])}
        pending.clear()

    for line in _lines(response):
        if not line.strip():
            continue
        try:
            chunk = json.loads(line)
        except ValueError:
            continue
        if chunk.get("error"):
            raise ProviderError(f"Ollama: {_scrub(chunk['error'], cfg)}")
        message = chunk.get("message") or {}
        if message.get("content"):
            yield "text", message["content"]
        for call in message.get("tool_calls") or []:
            fn = call.get("function") or {}
            args = fn.get("arguments")
            if "index" in call or isinstance(args, str):
                slot = pending.setdefault(int(call.get("index") or 0), {"id": "", "name": "", "args": ""})
                slot["id"] = slot["id"] or str(call.get("id") or "")
                if fn.get("name"):
                    slot["name"] = str(fn["name"])
                if isinstance(args, str):
                    slot["args"] += args
                elif isinstance(args, dict):
                    slot["args"] = json.dumps(args)
                continue
            calls += 1
            yield "tool_call", {"id": f"call_{calls}", "name": str(fn.get("name") or ""),
                                "arguments": _parse_args(args)}
        if chunk.get("done"):
            yield from flush()
            yield "usage", int(chunk.get("prompt_eval_count") or 0) + int(chunk.get("eval_count") or 0)
    yield from flush()


# -- OpenAI-compatible ---------------------------------------------------------

def _openai_messages(messages: list[dict]) -> list[dict]:
    out = []
    for m in messages:
        if m["role"] == "tool":
            out.append({"role": "tool", "tool_call_id": m.get("tool_call_id", ""), "content": m["content"]})
        elif m["role"] == "assistant" and m.get("tool_calls"):
            out.append({"role": "assistant", "content": m.get("content") or None,
                        "tool_calls": [{"id": c["id"], "type": "function",
                                        "function": {"name": c["name"],
                                                     "arguments": json.dumps(c["arguments"])}}
                                       for c in m["tool_calls"]]})
        else:
            out.append({"role": m["role"], "content": m.get("content", "")})
    return out


def _openai(cfg, messages, tools, max_tokens, json_mode):
    body = {"model": cfg["model"], "messages": _openai_messages(messages), "stream": True,
            "max_tokens": max_tokens}
    if tools:
        body["tools"] = [{"type": "function", "function": t} for t in tools]
    response = _request(cfg, "POST", "/chat/completions", body=body, stream=True)
    pending: dict[int, dict] = {}
    for chunk in _sse(response):
        if chunk.get("error"):
            raise ProviderError(f"{LABELS['openai']}: {_scrub(_detail_of(chunk), cfg)}")
        usage = chunk.get("usage")
        if isinstance(usage, dict) and usage.get("total_tokens"):
            yield "usage", int(usage["total_tokens"])
        for choice in chunk.get("choices") or []:
            delta = choice.get("delta") or {}
            if delta.get("content"):
                yield "text", delta["content"]
            for part in delta.get("tool_calls") or []:
                slot = pending.setdefault(int(part.get("index") or 0), {"id": "", "name": "", "args": ""})
                fn = part.get("function") or {}
                slot["id"] = part.get("id") or slot["id"]
                slot["name"] += fn.get("name") or ""
                slot["args"] += fn.get("arguments") or ""
    for index in sorted(pending):
        slot = pending[index]
        yield "tool_call", {"id": slot["id"] or f"call_{index}", "name": slot["name"],
                            "arguments": _parse_args(slot["args"])}


def _detail_of(chunk: dict) -> str:
    err = chunk.get("error")
    return str(err.get("message") if isinstance(err, dict) else err)


# -- Anthropic -----------------------------------------------------------------

def _anthropic_messages(messages: list[dict]) -> tuple[str, list[dict]]:
    system, out = [], []
    for m in messages:
        if m["role"] == "system":
            system.append(m["content"])
            continue
        if m["role"] == "tool":
            block = {"type": "tool_result", "tool_use_id": m.get("tool_call_id", ""), "content": m["content"]}
            if out and out[-1]["role"] == "user" and isinstance(out[-1]["content"], list):
                out[-1]["content"].append(block)
            else:
                out.append({"role": "user", "content": [block]})
            continue
        if m["role"] == "assistant" and m.get("tool_calls"):
            blocks = [{"type": "text", "text": m["content"]}] if m.get("content") else []
            blocks += [{"type": "tool_use", "id": c["id"], "name": c["name"], "input": c["arguments"]}
                       for c in m["tool_calls"]]
            out.append({"role": "assistant", "content": blocks})
            continue
        text = m.get("content") or "(empty)"
        if m["role"] == "user" and out and out[-1]["role"] == "user":
            # Turns must alternate; a message after a cancelled tool result joins that turn.
            previous = out[-1]["content"]
            blocks = previous if isinstance(previous, list) else [{"type": "text", "text": previous}]
            out[-1]["content"] = [*blocks, {"type": "text", "text": text}]
            continue
        out.append({"role": m["role"], "content": text})
    return "\n\n".join(system), out


def _anthropic(cfg, messages, tools, max_tokens, json_mode):
    system, converted = _anthropic_messages(messages)
    body = {"model": cfg["model"], "max_tokens": max_tokens, "messages": converted, "stream": True}
    if system:
        body["system"] = system
    if tools:
        body["tools"] = [{"name": t["name"], "description": t["description"],
                          "input_schema": t["parameters"]} for t in tools]
    response = _request(cfg, "POST", "/messages", body=body, stream=True)
    blocks: dict[int, dict] = {}
    for event in _sse(response):
        kind = event.get("type")
        if kind == "error":
            raise ProviderError(f"Anthropic: {_scrub(_detail_of(event), cfg)}")
        if kind == "message_start":
            usage = (event.get("message") or {}).get("usage") or {}
            yield "usage", int(usage.get("input_tokens") or 0)
        elif kind == "content_block_start":
            block = event.get("content_block") or {}
            if block.get("type") == "tool_use":
                blocks[int(event.get("index") or 0)] = {"id": block.get("id", ""),
                                                        "name": block.get("name", ""), "args": ""}
        elif kind == "content_block_delta":
            delta = event.get("delta") or {}
            if delta.get("type") == "text_delta" and delta.get("text"):
                yield "text", delta["text"]
            elif delta.get("type") == "input_json_delta":
                slot = blocks.get(int(event.get("index") or 0))
                if slot is not None:
                    slot["args"] += delta.get("partial_json") or ""
        elif kind == "content_block_stop":
            slot = blocks.pop(int(event.get("index") or 0), None)
            if slot is not None:
                yield "tool_call", {"id": slot["id"], "name": slot["name"],
                                    "arguments": _parse_args(slot["args"] or "{}")}
        elif kind == "message_delta":
            usage = event.get("usage") or {}
            yield "usage", int(usage.get("output_tokens") or 0)


_ADAPTERS = {"ollama": _ollama, "openai": _openai, "anthropic": _anthropic}


def stream(cfg: dict, messages: list[dict], tools: list[dict] | None, *,
           max_tokens: int, json_mode: bool = False) -> Iterator[tuple[str, object]]:
    """Stream one model turn as neutral events; `tools=None` means no native tools."""
    if cfg.get("provider") not in _ADAPTERS:
        raise ProviderError("Choose a provider first.")
    if not cfg.get("model"):
        raise ProviderError("Choose a model first.")
    return _ADAPTERS[cfg["provider"]](cfg, messages, tools, max_tokens, json_mode)
