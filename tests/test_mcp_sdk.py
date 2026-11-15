"""Optional real official-SDK interoperability proof; no SDK runtime dependency."""
from pathlib import Path
import sys

import pytest

sdk = pytest.importorskip("mcp", reason="Install mcp>=1.30,<2 in a development environment for official SDK proof")
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamable_http_client
import httpx
import anyio

from test_mcp import server  # noqa: F401 - production HTTP fixture


def test_official_sdk_authenticated_streamable_http(server):
    """An actual SDK negotiates, discovers tools and calls the protected HTTP server."""
    base, _ = server
    async def connect():
        """Exercise initialization and real structured tool results over HTTP."""
        async with httpx.AsyncClient(headers={"Authorization": "Bearer mcp-fixture-key"}) as client, streamable_http_client(base + "/api/v1/mcp", http_client=client) as streams:
            async with ClientSession(streams[0], streams[1]) as session:
                initialized = await session.initialize()
                assert initialized.serverInfo.name == "ROMarr"
                listing = await session.list_tools()
                assert len(listing.tools) == 5
                answer = await session.call_tool("romarr_library", {"query": "Test", "limit": 5})
                assert not answer.isError
                assert answer.structuredContent["items"][0]["name"] == "Test Game"
                await session.send_ping()
    anyio.run(connect)


def test_official_sdk_standalone_stdio_bridge_from_unrelated_directory(server, tmp_path):
    """The standalone single Python file needs no ROMarr installation or checkout cwd."""
    base, _ = server
    # Copy only the public bridge/server module; bridge execution imports no ROMarr modules.
    bridge = tmp_path / "romarr-mcp.py"
    bridge.write_bytes((Path(__file__).resolve().parents[1] / "romarr/mcp.py").read_bytes())
    params = StdioServerParameters(command=sys.executable, args=[str(bridge)], cwd=str(tmp_path),
                                  env={"ROMARR_URL": base, "ROMARR_API_KEY": "mcp-fixture-key"})
    async def connect():
        """Use the official stdio transport to prove clean framing and real remote calls."""
        async with stdio_client(params) as streams:
            async with ClientSession(*streams) as session:
                initialized = await session.initialize()
                assert initialized.serverInfo.name == "ROMarr"
                listing = await session.list_tools()
                assert {tool.name for tool in listing.tools} >= {"romarr_stats", "romarr_queue"}
                answer = await session.call_tool("romarr_queue", {"limit": 2})
                assert not answer.isError and answer.structuredContent["items"][0]["game"] == "Test Game"
    anyio.run(connect)
