"""Remote MCP smoke test over Streamable HTTP — initialize + tools/list with a bearer token.
Run with a venv that has the `mcp` SDK installed (a generated server's venv).
Env: MCP_URL (default http://127.0.0.1:8089/mcp), MCP_TOKEN.
"""
import asyncio
import os
import sys

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

URL = os.environ.get("MCP_URL", "http://127.0.0.1:8089/mcp")
TOKEN = os.environ.get("MCP_TOKEN", "")


async def main():
    headers = {"Authorization": f"Bearer {TOKEN}"} if TOKEN else {}
    async with streamablehttp_client(URL, headers=headers) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = (await session.list_tools()).tools
            print(f"OK  remote tools/list over HTTP — {len(tools)} tool(s)")
            print("    sample:", [t.name for t in tools[:5]])
            print("PASS")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as e:  # noqa: BLE001
        print(f"FAIL: {type(e).__name__}: {e}")
        sys.exit(1)
