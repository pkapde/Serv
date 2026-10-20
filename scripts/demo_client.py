"""Use the actual MCP transport and REST saved-query API."""

import asyncio
import json
import os

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client


async def main():
    url = os.getenv("SERVICE_URL", "http://127.0.0.1:8000")
    headers = {"Authorization": "Bearer " + os.getenv("SERVICE_TOKEN", "local-demo-token")}
    async with httpx.AsyncClient(headers=headers, timeout=90) as client:
        async with streamable_http_client(url + "/mcp", http_client=client) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                print("Tools:", [tool.name for tool in (await session.list_tools()).tools])
                answer = await session.call_tool(
                    "ask_question", {"question": "What is hold percentage by channel last fiscal month?"}
                )
                payload = answer.structuredContent
                print(json.dumps(payload, indent=2))
                if payload["status"] != "answered":
                    raise RuntimeError("Demo query failed")
                saved = await session.call_tool(
                    "save_query",
                    {"resolution_id": payload["resolution_id"], "name": "Hold percentage by channel"},
                )
                saved_id = saved.structuredContent["saved_query_id"]
                rerun = await client.post(url + f"/api/v1/saved-queries/{saved_id}/run")
                rerun.raise_for_status()
                print("Saved query rerun:", json.dumps(rerun.json(), indent=2))
                assert rerun.json()["provenance"]["llm_calls"] == 0


if __name__ == "__main__":
    asyncio.run(main())
