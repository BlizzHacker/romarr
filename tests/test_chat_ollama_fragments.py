"""Ollama builds that stream tool calls in OpenAI-style pieces are read whole."""
import json

from romarr import chat_providers


class _Resp:
    def __init__(self, chunks):
        self._lines = [json.dumps(c) for c in chunks]

    def iter_lines(self, **kwargs):
        return iter(line.encode() for line in self._lines)

    def close(self):
        pass


def _run(monkeypatch, chunks):
    monkeypatch.setattr(chat_providers, "_request", lambda *a, **k: _Resp(chunks))
    cfg = {"provider": "ollama", "base_url": "http://x", "model": "m"}
    return list(chat_providers._ollama(cfg, [{"role": "user", "content": "hi"}], [{"name": "search_releases"}], 64, False))


def test_fragmented_tool_call_is_assembled_once(monkeypatch):
    piece = lambda d: {"message": {"role": "assistant", "content": "", "tool_calls": [d]}, "done": False}
    chunks = [piece({"index": 0, "id": "call_abc", "type": "function",
                     "function": {"name": "search_releases", "arguments": ""}}),
              piece({"index": 0, "function": {"arguments": "{"}}),
              piece({"index": 0, "function": {"arguments": "\"game\":\"Super"}}),
              piece({"index": 0, "function": {"arguments": " Metroid\"}"}}),
              {"message": {"content": ""}, "done": True, "prompt_eval_count": 3, "eval_count": 4}]
    events = _run(monkeypatch, chunks)
    calls = [e[1] for e in events if e[0] == "tool_call"]
    assert calls == [{"id": "call_abc", "name": "search_releases", "arguments": {"game": "Super Metroid"}}]


def test_whole_tool_calls_still_work(monkeypatch):
    chunks = [{"message": {"role": "assistant", "content": "",
                           "tool_calls": [{"function": {"name": "health_check", "arguments": {}}}]},
               "done": True}]
    calls = [e[1] for e in _run(monkeypatch, chunks) if e[0] == "tool_call"]
    assert calls == [{"id": "call_1", "name": "health_check", "arguments": {}}]
