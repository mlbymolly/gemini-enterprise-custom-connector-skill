"""
smoke_test_mcp.py
-----------------
Verifies the Snowflake MCP server is reachable and that the
`sales_analyst` tool is discoverable BEFORE wiring it into Gemini
Enterprise. Use this whenever something feels broken.

Auth strategy:
  - Mints a JWT against the External OAuth integration using a
    test service-principal in your IdP (NOT the end-user flow).
  - For end-user OAuth, prefer testing through the Gemini Enterprise
    UI -- that's the only place the Authorization Code flow runs.

Requires:  pip install mcp httpx
"""
from __future__ import annotations

import asyncio
import os
import sys

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client


MCP_URL = os.environ["MCP_URL"]            # e.g. https://...snowflakecomputing.com/.../mcp
TOKEN_URL = os.environ["TOKEN_URL"]
CLIENT_ID = os.environ["CLIENT_ID"]
CLIENT_SECRET = os.environ["CLIENT_SECRET"]
SCOPE = os.environ.get("SCOPE", "offline_access")


async def get_access_token() -> str:
    """Client-credentials grant against the IdP for a smoke-test principal."""
    async with httpx.AsyncClient(timeout=30) as http:
        r = await http.post(
            TOKEN_URL,
            data={
                "grant_type": "client_credentials",
                "client_id": CLIENT_ID,
                "client_secret": CLIENT_SECRET,
                "scope": SCOPE,
            },
        )
        r.raise_for_status()
        return r.json()["access_token"]


async def main() -> int:
    token = await get_access_token()
    headers = {"Authorization": f"Bearer {token}"}

    print(f"Connecting to {MCP_URL} ...")
    async with streamablehttp_client(MCP_URL, headers=headers) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()

            tools = await session.list_tools()
            print("\nDiscovered tools:")
            for t in tools.tools:
                print(f"  - {t.name}: {t.description.splitlines()[0]}")

            if not any(t.name == "sales_analyst" for t in tools.tools):
                print("ERROR: sales_analyst tool not exposed", file=sys.stderr)
                return 1

            print("\nCalling sales_analyst with a sample question ...")
            result = await session.call_tool(
                "sales_analyst",
                {"messages": [
                    {"role": "user",
                     "content": "What was net revenue by region last quarter?"}
                ]},
            )
            for block in result.content:
                print(block.text if hasattr(block, "text") else block)

    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
