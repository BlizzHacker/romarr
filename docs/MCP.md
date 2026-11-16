# Connect an AI agent to your ROMarr

![AI Agents MCP Connector view in ROMarr](img/ai-agents-mcp.png)

ROMarr provides a real authenticated MCP endpoint at:

```text
https://YOUR-ROMARR-SERVER/api/v1/mcp
```

Use **your own** server address and its API key from Settings → General. The
Library Shell → **AI agents (MCP)** panel prepares examples using the address
of the server you are currently viewing. Examples contain a placeholder;
ROMarr never inserts your real key into copied client configuration.

MCP clients can initialize the connection, discover tools and call them over
stateless Streamable HTTP with JSON responses. Supported protocol revisions
are `2025-11-25`, `2025-06-18` and `2025-03-26`; the stdio bridge also accepts
`2024-11-05`. There is no standalone SSE stream: GET requests asking for
`text/event-stream` return 405. Ordinary authenticated GET requests retain the
connection-manifest response for compatibility. Notifications return HTTP 202
without a JSON-RPC response body.

## Read tools and optional writes

| Tool | What it reads |
| --- | --- |
| `romarr_stats` | Cached counts, uptime, version and whether MCP writes are enabled |
| `romarr_platforms` | Supported platform names and slugs |
| `romarr_library` | Cached titles, platforms, years and ratings; filter by query/platform |
| `romarr_queue` | Cached download activity without probing download clients |
| `romarr_history` | Recent event metadata, excluding diagnostic details |

List tools accept `limit` (1–100) and `offset` (0–100000). The library tool also
accepts `query` and `platform`. Results describe cached state; they do not claim
a fresh filesystem or backend scan. Library filtering examines at most 100000
cached rows and reports `scan_limited` if more exist. Titles and release names
are untrusted data, not instructions to an agent.

Writes are absent by default. To deliberately enable the two supported write
tools, set **on the ROMarr server** and restart:

```text
ROMARR_MCP_WRITE=1
```

`romarr_add_wanted` takes a game title and a supported platform slug. Adding
Wanted entries may cause later scheduled searches and downloads; an agent
should obtain its owner's approval first. `romarr_refresh_library` wakes the
existing background cache reader and reports that refresh was requested, not
completed. Android remains read-only even with the opt-in set. MCP never
accepts arbitrary REST paths, commands, settings, file paths or shell input.

The ROMarr API key remains a full server API credential; the MCP tool allowlist
is not a separately scoped API key. Give it only to clients you trust.

## Authentication and network access

Every HTTP MCP request requires `X-Api-Key: YOUR_KEY` or
`Authorization: Bearer YOUR_KEY`. Cookies and query-string keys are not accepted
for the MCP transport. Explicit key authentication is still required when the
ordinary REST API has `ROMARR_AUTH=disabled` or a proxy supplies SSO identity.
No OAuth registration or authorization flow is provided by this endpoint.

Native MCP clients normally omit `Origin`. When one is present, it must match
an origin explicitly allowed by the server operator, for example:

```text
ROMARR_MCP_ORIGINS=https://romarr.example.com
```

Use comma-separated exact origins for more than one. The default rejects all
requests carrying an Origin header. There is no wildcard CORS policy. Use
HTTPS for remote API keys; for an HTTP-only server use an SSH tunnel and point
the client at its loopback end. Opening the MCP endpoint does not make a
private LAN server reachable by a cloud service: the client must have a route
to the server.

Requests are limited to 64 KiB with a ten-second body deadline; responses are
bounded to 256 KiB and concurrent tool work is limited. Read tools use local
cached state and do not wait on external indexers or library backends.

## Claude Code

In Claude Code's MCP settings, or a project `.mcp.json`, use:

```json
{
  "mcpServers": {
    "romarr": {
      "type": "http",
      "url": "https://YOUR-ROMARR-SERVER/api/v1/mcp",
      "headers": {"X-Api-Key": "<YOUR_ROMARR_API_KEY>"}
    }
  }
}
```

Claude Code also supports `claude mcp add --transport http` with an explicit
header. See [Claude Code's official MCP guide](https://code.claude.com/docs/en/mcp)
for configuration scopes and environment-based or dynamic headers. For Claude
Desktop or another client that accepts local stdio processes, use the portable
bridge below instead of assuming it supports the same HTTP configuration.

## Codex

Add this to your Codex user configuration, normally `~/.codex/config.toml`:

```toml
[mcp_servers.romarr]
url = "https://YOUR-ROMARR-SERVER/api/v1/mcp"
bearer_token_env_var = "ROMARR_API_KEY"
```

Set `ROMARR_API_KEY` in the environment used to start Codex, then restart the
client. This is a variable name, not the literal key. An alternative for clients
using custom headers is `env_http_headers = { "X-Api-Key" = "ROMARR_API_KEY" }`.
See [OpenAI's MCP configuration guide](https://learn.chatgpt.com/docs/extend/mcp).

## Hermes

In Hermes' `config.yaml`:

```yaml
mcp_servers:
  romarr:
    url: "https://YOUR-ROMARR-SERVER/api/v1/mcp"
    headers:
      X-Api-Key: "<YOUR_ROMARR_API_KEY>"
```

Use Hermes' protected configuration or its supported secret interpolation
rather than committing an actual key. See the
[official Hermes MCP reference](https://github.com/NousResearch/hermes-agent/blob/main/website/docs/reference/mcp-config-reference.md).

## Antigravity

Open Manage MCP Servers → View raw config and add:

```json
{
  "mcpServers": {
    "romarr": {
      "serverUrl": "https://YOUR-ROMARR-SERVER/api/v1/mcp",
      "headers": {"X-Api-Key": "<YOUR_ROMARR_API_KEY>"}
    }
  }
}
```

See [Google's official MCP guide](https://antigravity.google/docs/mcp) for the
configuration location in your Antigravity surface. It documents remote
Streamable HTTP as well as local stdio configuration.

## Portable stdio bridge, including Claude Desktop

No Node, npx, MCP SDK or installed ROMarr package is required on the client.
The bridge is the single public
[`romarr/mcp.py` file](https://raw.githubusercontent.com/BlizzHacker/romarr/main/romarr/mcp.py).
Download it as `romarr-mcp.py` to your client machine. Install Python 3.10 or
newer and `requests` in a client virtual environment. Running this single file
uses only its HTTP forwarding path; it does not import or start a local ROMarr
server. Alternatively, from a ROMarr checkout with its ordinary requirements,
`python -m romarr.mcp` runs the same bridge.

Use an absolute path to your Python interpreter and downloaded file:

```json
{
  "mcpServers": {
    "romarr": {
      "command": "/absolute/path/to/python",
      "args": ["/absolute/path/to/romarr-mcp.py"],
      "env": {
        "ROMARR_URL": "https://YOUR-ROMARR-SERVER",
        "ROMARR_API_KEY": "<YOUR_ROMARR_API_KEY>"
      }
    }
  }
}
```

On Windows, use absolute interpreter/file paths and escape backslashes in JSON
or use forward slashes. The key is passed in the process environment, never
in command-line arguments. `ROMARR_URL` may be the server origin or exact MCP
endpoint. The bridge sends the key in a real HTTP header and refuses redirects.
For plain HTTP it accepts loopback hosts only; use HTTPS or an SSH tunnel for
another machine. Its stdout contains only newline-delimited JSON-RPC; startup
errors go to stderr without credentials.

## Optional desktop control and verification

[Open-Eyes](https://github.com/BlizzHacker/open-eyes) is an independent optional
desktop-control connector. It is not needed for any ROMarr MCP tool and is not
included in the ROMarr connection configuration.

`tests/test_mcp.py` exercises real authenticated sockets, read-only behavior,
strict schemas, size limits, Origin checks and stdio framing. Optional
`tests/test_mcp_sdk.py` uses the official `mcp` Python SDK to initialize,
discover and call tools over both protected HTTP and a standalone stdio
subprocess launched from an unrelated directory. The SDK is a development-only
verification dependency, not a runtime requirement. These tests prove protocol
interoperability; they do not claim every version of every named client was
manually tested. Protocol references:
[Streamable HTTP](https://modelcontextprotocol.io/specification/2025-11-25/basic/transports),
[official Python SDK](https://github.com/modelcontextprotocol/python-sdk).
