# Chat Agent

ROMarr has a built-in chat agent: talk to your own model about your library and let it use ROMarr's tools for you. Open **Hub → Chat Agent**.

It works with:

| Provider | What to enter | Notes |
| --- | --- | --- |
| **Ollama** | Base URL, e.g. `http://192.168.0.50:11434` | Click **Load models** to pick from `/api/tags`. No key needed. |
| **OpenAI-compatible** | Base URL ending in `/v1`, API key, model | OpenAI, LiteLLM, LM Studio, vLLM, llama.cpp server, OpenRouter, and others. |
| **Anthropic** | API key, model | Base URL defaults to `https://api.anthropic.com`. |

Fill in the provider, press **Test** (it lists the provider's models and says whether yours is among them), tick **Enable the chat agent** and **Save**.

## What it can do

The agent answers with real data from your server by calling these tools:

| Tool | What it does |
| --- | --- |
| `search_releases` | Interactive search across your indexers and enabled ROM Hub plugins, with the scorer's reasons and an id per release. |
| `explain_release` | Every scorer reason for one release from the last search, including why it was refused. |
| `explain_search` | Runs the automatic search and reports the pick and the top rejected releases, without grabbing anything. |
| `grab_release` | Downloads one release from the last search. **Asks you first.** |
| `add_to_wanted` / `add_to_wishlist` | Adds a game to Wanted (optionally searching now) or to the Wishlist. **Asks you first.** |
| `remove_from_wanted` | Takes a game off Wanted or the Wishlist. **Asks you first.** |
| `wanted_list`, `queue_status` | The Wanted list, the Wishlist and the live download queue. |
| `library_stats`, `romarr_library`, `romarr_history`, `romarr_queue`, `romarr_platforms` | Counts, cached library titles, recent history and platform slugs (the same read-only tools [MCP](MCP.md) offers). |
| `set_gaps` | What is missing from a set, against a loaded No-Intro or Redump DAT. |
| `metadata_lookup`, `discover_platform` | Metadata (IGDB) lookups and browsing one platform's catalogue. |
| `health_check` | Which indexers, libraries and DATs are working. |

Each tool call and its result appear in the chat as they happen; expand one to see the arguments and the data the model received.

Try: "What is missing from my SNES set?", "Find Chrono Trigger for snes", "Why was the beta refused?", "Add EarthBound to my wishlist".

## Confirmations

Anything that changes your library (grab, add to Wanted or the Wishlist) or removes something (drop from Wanted) stops and shows a **Confirm / Cancel** card. Nothing runs until you press **Confirm**. Typing "yes" does not confirm, and nothing the model writes can confirm on your behalf. A confirmation is single-use, belongs to the user it was shown to, and expires after ten minutes; sending a new message cancels it.

## Small local models

**Tool calling** has three modes:

- **Auto** (default): uses the provider's native tool calling. If the model says it does not support tools, the agent switches to JSON mode for that model and tells you so.
- **Native tool calls only.**
- **JSON mode**: for small models without tool calling. The agent describes the tools in the prompt and the model replies with one JSON object per step (`{"tool": ..., "arguments": ...}` or `{"answer": ...}`). With Ollama this also sets `format: json`.

Models in the 7B to 14B range with tool support (for example Qwen 2.5 or Llama 3.1) work well in Auto mode.

## Limits

Each message is bounded by **Max steps** (model turns, default 6), **Max tokens per reply** (default 1024) and a **Token budget per message** (default 16000, counted from what the provider reports, or estimated when it reports nothing), plus a five-minute wall clock. When one is reached the agent stops and says which. Provider failures (wrong key, unreachable URL, unknown model) are shown in the chat in plain words.

## Privacy and security

- Every chat endpoint needs the same sign-in as the rest of the UI.
- The API key is stored as a masked secret: it is never sent back to the browser, never logged, and left out of backups unless you export secrets.
- Tool results sent to the provider leave out filesystem paths and URLs. If you use a hosted provider, the game titles and release names in your questions and results are sent to it; use Ollama to keep everything on your network.
- History is kept per user (the owner, or each SSO/OIDC account) in `chat/` next to `romarr.json`. **Clear conversation** deletes it.

## Why it is built in

ROM Hub plugins run in a sandbox that may only reach the HTTPS hosts their manifest names. The agent has to reach a model on your LAN and ROMarr's own search, queue and library, so it lives in ROMarr (as the [Compressatorium](PLUGINS.md) connection does), with a plugin's surface: off until you enable it, configured on its own page.

## API

| Route | Purpose |
| --- | --- |
| `GET/POST /api/v1/chat/config` | Read (key masked) or save the settings. |
| `POST /api/v1/chat/models`, `POST /api/v1/chat/test` | List models, test the connection. |
| `POST /api/v1/chat` | `{"message": "..."}`; streams NDJSON events: `text`, `tool_call`, `tool_result`, `confirm`, `notice`, `error`, `done`. |
| `POST /api/v1/chat/confirm` | `{"id": "...", "approve": true}`; runs or declines the action, then streams the rest of the reply. |
| `GET/DELETE /api/v1/chat/history` | Read or clear your conversation. |
