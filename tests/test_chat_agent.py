"""The chat agent against fake streaming providers and a real ROMarr HTTP server.

No test reaches a real model: every provider is a local fake that streams the
wire format the real one does (Ollama NDJSON, OpenAI SSE, Anthropic SSE).
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import ClassVar

import pytest
import requests

from romarr import chat_agent, chat_providers
from romarr.app import ROMarr, make_handler

KEY = "chat-fixture-key"
SECRET = "sk-provider-secret-1234"


class FakeProvider(BaseHTTPRequestHandler):
    """Replays scripted replies per path and records every request."""

    script: ClassVar[dict[str, list]] = {}
    seen: ClassVar[list[dict]] = []

    def log_message(self, *args):
        pass

    def _reply(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}") if length else {}
        type(self).seen.append({"path": self.path, "headers": dict(self.headers), "body": body})
        queue = type(self).script.get(self.path) or []
        status, ctype, payload = queue.pop(0) if len(queue) > 1 else (queue[0] if queue else (404, "application/json", b"{}"))
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    do_GET = _reply
    do_POST = _reply


def ndjson(*chunks):
    return (200, "application/x-ndjson", "".join(json.dumps(c) + "\n" for c in chunks).encode())


def sse(*events):
    return (200, "text/event-stream",
            "".join(f"event: {e.get('type', 'message')}\ndata: {json.dumps(e)}\n\n" for e in events).encode()
            + b"data: [DONE]\n\n")


def ollama_tool(name, arguments):
    return ndjson({"message": {"role": "assistant", "content": "",
                               "tool_calls": [{"function": {"name": name, "arguments": arguments}}]},
                   "done": True, "prompt_eval_count": 10, "eval_count": 5})


def ollama_text(*parts):
    chunks = [{"message": {"role": "assistant", "content": p}, "done": False} for p in parts]
    return ndjson(*chunks, {"message": {"content": ""}, "done": True, "prompt_eval_count": 10, "eval_count": 5})


@pytest.fixture
def provider():
    FakeProvider.script, FakeProvider.seen = {}, []
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), FakeProvider)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield "http://127.0.0.1:" + str(httpd.server_port)
    httpd.shutdown()
    httpd.server_close()


@pytest.fixture(autouse=True)
def _fresh_agent_state():
    for name in ("_pending", "_found", "_user_locks"):
        getattr(chat_agent, name).clear()
    chat_agent._no_native.clear()


@pytest.fixture
def service(tmp_path):
    svc = ROMarr({"ROMARR_DATA": str(tmp_path / "state.json"), "ROMARR_API_KEY": KEY})
    svc.calls = []
    svc.candidates = lambda game, platform="": {
        "game": game, "platform": platform or None, "found": 2, "accepted": 1,
        "items": [{"id": "abc01", "title": "Chrono Trigger (USA)", "platform": "snes", "size": 4194304,
                   "seeders": 12, "indexer": "Fixture", "score": 90, "accepted": True,
                   "reasons": ["title matches", "region USA preferred"], "grabbable": True,
                   "info_url": "https://indexer.example/t/1?apikey=leak"},
                  {"id": "abc02", "title": "Chrono Trigger (Beta)", "platform": "snes", "size": 1,
                   "seeders": 1, "indexer": "Fixture", "score": -50, "accepted": False,
                   "reasons": ["beta excluded"], "grabbable": True}]}

    def add_wanted(game, platform, monitored=True, search=False, parts=None):
        svc.calls.append(("add_wanted", game, platform, monitored, search))
        return {"ok": True, "game": game, "platform": platform}
    svc.add_wanted = add_wanted
    svc.grab_candidate = lambda rid, platform="": svc.calls.append(("grab", rid)) or {"ok": True}
    svc.drop_request = lambda game, platform: svc.calls.append(("drop", game, platform)) or True
    svc.health = lambda: {"ok": True, "prowlarr": True, "romm": True, "library": True,
                          "library_path": "/srv/roms/secret", "libraries": [], "libraries_ok": 0,
                          "libraries_total": 0, "queued": 0, "dats": {"loaded": 1}}
    return svc


def configure(service, url, **extra):
    """Enable the agent against the fake provider; keyword overrides win."""
    return chat_agent.save_config(service, {"enabled": True, "provider": "ollama", "base_url": url,
                                            "model": "qwen2.5:7b", **extra})


def run(events):
    return [e for e in events]


# -- configuration and secrets -------------------------------------------------

def test_key_is_masked_kept_on_blank_and_stripped_from_backups(service, provider):
    configure(service, provider, provider="openai", api_key=SECRET)
    public = chat_agent.public_config(service)
    assert public["api_key"] == chat_agent.MASK and public["api_key_set"] and SECRET not in json.dumps(public)
    chat_agent.save_config(service, {"api_key": "", "model": "gpt-x"})
    chat_agent.save_config(service, {"api_key": chat_agent.MASK})
    assert chat_agent.config(service)["api_key"] == SECRET
    assert SECRET not in json.dumps(service.safe_settings())
    from romarr.ops import make_backup
    assert SECRET not in json.dumps(make_backup(service.store.settings))
    echoed = service.restore_masked_items({"chat_agent": service.safe_settings()["chat_agent"]})
    assert echoed["chat_agent"]["api_key"] == SECRET
    chat_agent.save_config(service, {"clear_api_key": True})
    assert chat_agent.config(service)["api_key"] == ""


@pytest.mark.parametrize("patch", [{"provider": "bard"}, {"base_url": "ftp://x"},
                                   {"base_url": "http://user:pw@host"}, {"max_steps": 99},
                                   {"tool_mode": "magic"}, {"enabled": "yes"}])
def test_bad_settings_are_refused(service, patch):
    with pytest.raises(ValueError):
        chat_agent.save_config(service, patch)


def test_models_and_test_read_ollama_tags(service, provider):
    configure(service, provider)
    FakeProvider.script["/api/tags"] = [(200, "application/json",
                                         json.dumps({"models": [{"name": "qwen2.5:7b"}, {"name": "llama3.2:3b"}]}).encode())]
    assert chat_agent.models(service, {})["models"] == ["llama3.2:3b", "qwen2.5:7b"]
    assert chat_agent.test(service, {})["model_found"] is True
    assert chat_agent.test(service, {"model": "absent"})["model_found"] is False


def test_bad_key_is_reported_plainly_without_echoing_it(service, provider):
    configure(service, provider, provider="openai", api_key=SECRET)
    FakeProvider.script["/models"] = [(401, "application/json",
                                       json.dumps({"error": {"message": f"bad key {SECRET}"}}).encode())]
    result = chat_agent.test(service, {})
    assert result["ok"] is False and "refused the API key" in result["error"]
    assert SECRET not in result["error"]
    assert FakeProvider.seen[-1]["headers"]["Authorization"] == f"Bearer {SECRET}"


def test_saved_key_never_follows_an_unsaved_address(service, provider):
    configure(service, provider, provider="openai", api_key=SECRET)
    FakeProvider.script["/models"] = [(200, "application/json", b'{"data": [{"id": "m"}]}')]
    assert chat_agent.models(service, {})["ok"]
    assert FakeProvider.seen[-1]["headers"]["Authorization"] == f"Bearer {SECRET}"
    other = provider.replace("127.0.0.1", "localhost")
    assert chat_agent.models(service, {"base_url": other})["ok"]
    assert "Authorization" not in FakeProvider.seen[-1]["headers"]
    assert chat_agent.models(service, {"base_url": other, "api_key": "typed-key"})["ok"]
    assert FakeProvider.seen[-1]["headers"]["Authorization"] == "Bearer typed-key"
    assert chat_agent.config(service)["api_key"] == SECRET


def test_anthropic_turns_alternate_after_a_cancelled_action():
    system, messages = chat_providers._anthropic_messages([
        {"role": "system", "content": "sys"}, {"role": "user", "content": "grab it"},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "t1", "name": "grab_release",
                                                             "arguments": {"release_id": "a"}}]},
        {"role": "tool", "tool_call_id": "t1", "name": "grab_release", "content": "{}"},
        {"role": "user", "content": "never mind"}])
    assert system == "sys" and [m["role"] for m in messages] == ["user", "assistant", "user"]
    assert messages[-1]["content"][-1] == {"type": "text", "text": "never mind"}


def test_unreachable_provider_is_a_plain_error(service):
    chat_agent.save_config(service, {"enabled": True, "base_url": "http://127.0.0.1:9", "model": "m"})
    result = chat_agent.models(service, {})
    assert result["ok"] is False and "Could not connect to Ollama" in result["error"]


# -- the tool loop -------------------------------------------------------------

def test_disabled_agent_does_nothing(service, provider):
    configure(service, provider, enabled=False)
    events = run(chat_agent.chat(service, "owner", "hi"))
    assert events[0]["type"] == "error" and "disabled" in events[0]["message"]
    assert not FakeProvider.seen


def test_native_tool_loop_streams_results_and_keeps_history(service, provider):
    configure(service, provider)
    FakeProvider.script["/api/chat"] = [ollama_tool("search_releases", {"game": "Chrono Trigger", "platform": "snes"}),
                                        ollama_text("Best is ", "the USA release.")]
    events = run(chat_agent.chat(service, "owner", "find chrono trigger"))
    kinds = [e["type"] for e in events]
    assert kinds == ["tool_call", "tool_result", "text", "text", "done"]
    result = json.loads(events[1]["result"])
    assert result["releases"][0]["reasons"] == ["title matches", "region USA preferred"]
    assert "leak" not in events[1]["result"] and "info_url" not in events[1]["result"]
    first, second = FakeProvider.seen[0]["body"], FakeProvider.seen[1]["body"]
    assert any(t["function"]["name"] == "grab_release" for t in first["tools"])
    assert second["messages"][-1]["role"] == "tool" and second["messages"][-1]["tool_name"] == "search_releases"
    history = chat_agent.load_history(service, chat_agent.user_key("owner"))
    assert [m["role"] for m in history] == ["user", "assistant", "tool", "assistant"]
    assert history[-1]["content"] == "Best is the USA release."
    explain = run(chat_agent.chat(service, "owner", "x"))  # provider replays the last reply
    assert explain[-1]["type"] == "done"


def test_write_tools_wait_for_an_explicit_confirm(service, provider):
    configure(service, provider)
    FakeProvider.script["/api/chat"] = [ollama_tool("add_to_wanted", {"game": "Chrono Trigger", "platform": "snes",
                                                                      "search": "true"}),
                                        ollama_text("Added.")]
    events = run(chat_agent.chat(service, "owner", "add it"))
    confirm = next(e for e in events if e["type"] == "confirm")
    assert confirm["summary"] == "Add Chrono Trigger (snes) to Wanted" and not confirm["destructive"]
    assert service.calls == [] and len(FakeProvider.seen) == 1
    assert chat_agent.view_history(service, "owner")["pending"][0]["id"] == confirm["id"]
    # Someone else cannot answer it, and a model cannot either: only this endpoint can.
    assert run(chat_agent.confirm(service, "oidc:eve", confirm["id"], True))[0]["type"] == "error"
    after = run(chat_agent.confirm(service, "owner", confirm["id"], True))
    assert service.calls == [("add_wanted", "Chrono Trigger", "snes", True, True)]
    assert [e["type"] for e in after] == ["tool_call", "tool_result", "text", "done"]
    assert run(chat_agent.confirm(service, "owner", confirm["id"], True))[0]["type"] == "error"
    assert len(service.calls) == 1


def test_declined_and_destructive_actions_do_not_run(service, provider):
    configure(service, provider)
    FakeProvider.script["/api/chat"] = [ollama_tool("remove_from_wanted", {"game": "Zelda", "platform": "nes"}),
                                        ollama_text("Okay, left it.")]
    confirm = next(e for e in run(chat_agent.chat(service, "owner", "drop zelda")) if e["type"] == "confirm")
    assert confirm["destructive"] is True
    after = run(chat_agent.confirm(service, "owner", confirm["id"], False))
    assert service.calls == [] and "declined" in after[1]["result"]


def test_new_message_cancels_a_pending_confirm(service, provider):
    configure(service, provider)
    FakeProvider.script["/api/chat"] = [ollama_tool("grab_release", {"release_id": "abc01"}), ollama_text("ok")]
    confirm = next(e for e in run(chat_agent.chat(service, "owner", "grab")) if e["type"] == "confirm")
    run(chat_agent.chat(service, "owner", "never mind"))
    assert run(chat_agent.confirm(service, "owner", confirm["id"], True))[0]["type"] == "error"
    history = chat_agent.load_history(service, chat_agent.user_key("owner"))
    answered = {m["tool_call_id"] for m in history if m["role"] == "tool"}
    calls = {c["id"] for m in history for c in m.get("tool_calls") or []}
    assert calls <= answered and service.calls == []


def test_grab_only_accepts_ids_from_this_conversation(service, provider):
    configure(service, provider)
    tool = {t["name"]: t for t in chat_agent.tools()}["grab_release"]
    ok, text = chat_agent.execute(service, tool, {"release_id": "made-up"}, "u")
    assert service.calls == [] and "not from this conversation" in text
    explain = {t["name"]: t for t in chat_agent.tools()}["explain_release"]
    chat_agent._search_releases(service, {"game": "Chrono Trigger"}, "u")
    ok, text = chat_agent.execute(service, explain, {"release_id": "abc02"}, "u")
    assert ok and "beta excluded" in text and "refused" in text


def test_step_limit_bounds_the_loop(service, provider):
    configure(service, provider, max_steps=2)
    FakeProvider.script["/api/chat"] = [ollama_tool("health_check", {})]
    events = run(chat_agent.chat(service, "owner", "loop"))
    assert len(FakeProvider.seen) == 2 and "Stopped after 2 steps" in events[-2]["message"]
    assert "/srv/roms" not in json.dumps(events)


def test_token_budget_bounds_the_loop(service, provider):
    configure(service, provider, token_budget=1000)
    huge = ndjson({"message": {"tool_calls": [{"function": {"name": "health_check", "arguments": {}}}]},
                   "done": True, "prompt_eval_count": 5000, "eval_count": 1})
    FakeProvider.script["/api/chat"] = [huge]
    events = run(chat_agent.chat(service, "owner", "loop"))
    assert len(FakeProvider.seen) == 1 and "token budget" in events[-2]["message"]


def test_json_mode_fallback_for_models_without_tools(service, provider):
    configure(service, provider)
    FakeProvider.script["/api/chat"] = [
        (400, "application/json", json.dumps({"error": "qwen:0.5b does not support tools"}).encode()),
        ollama_text('{"tool": "health_check", "arguments": {}}'),
        ollama_text('```json\n{"answer": "Everything is healthy."}\n```')]
    events = run(chat_agent.chat(service, "owner", "health?"))
    assert [e["type"] for e in events] == ["notice", "tool_call", "tool_result", "text", "done"]
    assert events[3]["delta"] == "Everything is healthy."
    retry = FakeProvider.seen[1]["body"]
    assert "tools" not in retry and retry["format"] == "json"
    assert '"tool": "<name>"' in retry["messages"][0]["content"]
    assert FakeProvider.seen[2]["body"]["messages"][-1]["content"].startswith("Result of health_check:")


def test_forced_json_mode_and_text_tool_calls(service, provider):
    configure(service, provider, tool_mode="json")
    FakeProvider.script["/api/chat"] = [ollama_text("I'll just say hi.")]
    events = run(chat_agent.chat(service, "owner", "hi"))
    assert events[0] == {"type": "text", "delta": "I'll just say hi."}
    assert "tools" not in FakeProvider.seen[0]["body"]


def test_openai_stream_assembles_fragmented_tool_calls(service, provider):
    configure(service, provider, provider="openai", api_key=SECRET, base_url=provider + "/v1")
    FakeProvider.script["/v1/chat/completions"] = [
        sse({"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "c1", "function": {"name": "search_", "arguments": '{"game": "Chr'}}]}}]},
            {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"name": "releases", "arguments": 'ono Trigger"}'}}]}}]}),
        sse({"choices": [{"delta": {"content": "Found it."}}]}, {"choices": [], "usage": {"total_tokens": 42}})]
    events = run(chat_agent.chat(service, "owner", "find"))
    assert events[0] == {"type": "tool_call", "id": "c1", "name": "search_releases", "arguments": {"game": "Chrono Trigger"}}
    second = FakeProvider.seen[1]
    assert second["headers"]["Authorization"] == f"Bearer {SECRET}"
    tool_msg = second["body"]["messages"][-1]
    assert tool_msg["role"] == "tool" and tool_msg["tool_call_id"] == "c1"
    assert second["body"]["messages"][-2]["tool_calls"][0]["function"]["arguments"] == '{"game": "Chrono Trigger"}'
    assert events[-2] == {"type": "text", "delta": "Found it."}


def test_anthropic_stream_and_message_shape(service, provider):
    configure(service, provider, provider="anthropic", api_key=SECRET, model="claude-sonnet-5-5")
    FakeProvider.script["/v1/messages"] = [
        sse({"type": "message_start", "message": {"usage": {"input_tokens": 20}}},
            {"type": "content_block_start", "index": 0, "content_block": {"type": "tool_use", "id": "tu1", "name": "wanted_list"}},
            {"type": "content_block_delta", "index": 0, "delta": {"type": "input_json_delta", "partial_json": '{"view": "wish'}},
            {"type": "content_block_delta", "index": 0, "delta": {"type": "input_json_delta", "partial_json": 'list"}'}},
            {"type": "content_block_stop", "index": 0},
            {"type": "message_delta", "usage": {"output_tokens": 7}}),
        sse({"type": "content_block_start", "index": 0, "content_block": {"type": "text"}},
            {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "Your wishlist is empty."}})]
    events = run(chat_agent.chat(service, "owner", "wishlist?"))
    assert events[0]["arguments"] == {"view": "wishlist"}
    second = FakeProvider.seen[1]
    assert second["headers"]["x-api-key"] == SECRET and second["headers"]["anthropic-version"]
    body = second["body"]
    assert "ROMarr assistant" in body["system"] and body["messages"][0]["role"] == "user"
    assert body["messages"][-1]["content"][0]["type"] == "tool_result"
    assert body["messages"][-1]["content"][0]["tool_use_id"] == "tu1"
    assert body["tools"][0]["input_schema"]["type"] == "object"
    assert events[-2]["delta"] == "Your wishlist is empty."


def test_provider_errors_surface_without_the_key(service, provider):
    configure(service, provider, provider="openai", api_key=SECRET)
    FakeProvider.script["/chat/completions"] = [(500, "application/json",
                                                 json.dumps({"error": {"message": f"boom {SECRET}"}}).encode())]
    events = run(chat_agent.chat(service, "owner", "hi"))
    assert events[0]["type"] == "error" and "HTTP 500" in events[0]["message"]
    assert SECRET not in json.dumps(events)


def test_history_is_per_user_and_clearable(service, provider):
    configure(service, provider)
    FakeProvider.script["/api/chat"] = [ollama_text("hello")]
    run(chat_agent.chat(service, "owner", "one"))
    run(chat_agent.chat(service, "oidc:alice", "two"))
    assert chat_agent.view_history(service, "owner")["messages"][0]["content"] == "one"
    assert chat_agent.view_history(service, "oidc:alice")["messages"][0]["content"] == "two"
    chat_agent.clear_history(service, chat_agent.user_key("owner"))
    assert chat_agent.view_history(service, "owner")["messages"] == []
    assert chat_agent.view_history(service, "oidc:alice")["messages"]


def test_bad_tool_arguments_go_back_to_the_model(service, provider):
    configure(service, provider)
    FakeProvider.script["/api/chat"] = [ollama_tool("search_releases", {"title": "x"}), ollama_text("sorry")]
    events = run(chat_agent.chat(service, "owner", "x"))
    assert events[1]["ok"] is False and "unknown argument" in events[1]["result"]


def test_results_sent_to_providers_omit_paths_and_urls():
    cleaned = chat_agent._clean({"title": "See https://x.example/a?k=1", "where": "/mnt/roms/snes",
                                 "library_path": "C:\\roms", "nested": [{"apikey": "zzz", "ok": 1}]})
    assert cleaned == {"title": "See [URL omitted]", "where": "[path omitted]", "nested": [{"ok": 1}]}


# -- over HTTP -----------------------------------------------------------------

@pytest.fixture
def http(service):
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(service))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield "http://127.0.0.1:" + str(httpd.server_port)
    httpd.shutdown()
    httpd.server_close()


@pytest.mark.parametrize("method,path", [("GET", "/api/v1/chat/config"), ("GET", "/api/v1/chat/history"),
                                         ("POST", "/api/v1/chat"), ("POST", "/api/v1/chat/confirm"),
                                         ("POST", "/api/v1/chat/config"), ("POST", "/api/v1/chat/models"),
                                         ("POST", "/api/v1/chat/test"), ("DELETE", "/api/v1/chat/history")])
def test_every_chat_route_requires_authentication(http, method, path):
    answer = requests.request(method, http + path, json={"message": "hi", "id": "x", "approve": True}, timeout=5)
    assert answer.status_code == 401


def test_chat_streams_ndjson_over_http(http, service, provider):
    configure(service, provider, api_key=SECRET)
    FakeProvider.script["/api/chat"] = [ollama_tool("search_releases", {"game": "Chrono Trigger"}),
                                        ollama_text("Done.")]
    headers = {"X-Api-Key": KEY}
    config = requests.get(http + "/api/v1/chat/config", headers=headers, timeout=5).json()
    assert config["api_key"] == chat_agent.MASK and SECRET not in json.dumps(config)
    with requests.post(http + "/api/v1/chat", json={"message": "find"}, headers=headers,
                       stream=True, timeout=10) as answer:
        assert answer.status_code == 200
        assert answer.headers["Content-Type"].startswith("application/x-ndjson")
        events = [json.loads(line) for line in answer.iter_lines() if line]
    assert [e["type"] for e in events] == ["tool_call", "tool_result", "text", "done"]
    history = requests.get(http + "/api/v1/chat/history", headers=headers, timeout=5).json()
    assert history["messages"][0] == {"role": "user", "content": "find"}
    assert requests.delete(http + "/api/v1/chat/history", headers=headers, timeout=5).json() == {"ok": True}
    bad = requests.post(http + "/api/v1/chat/confirm", json={"id": "x", "approve": "yes"}, headers=headers, timeout=5)
    assert bad.status_code == 400
    refused = requests.post(http + "/api/v1/chat/config", json={"max_steps": 0}, headers=headers, timeout=5)
    assert refused.status_code == 400


def test_normalise_url_defaults():
    assert chat_providers.normalise_url("ollama", "") == "http://127.0.0.1:11434"
    assert chat_providers.normalise_url("anthropic", "https://api.anthropic.com/") == "https://api.anthropic.com"
