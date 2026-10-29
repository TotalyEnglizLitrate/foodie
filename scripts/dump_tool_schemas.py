#!/usr/bin/env python3
"""
Dump the full tools/list schema for Swiggy or Zomato MCP server.
Each tool's inputSchema gives you the exact request spec.
The response schema has to be inferred from structuredContent but
tool descriptions often document the shape too.

Usage:
    uv run python scripts/dump_tool_schemas.py [--provider swiggy|zomato] [--tool TOOL_NAME]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time

import httpx

from foodie.secret_store import SecretStore

ENDPOINTS = {
    "swiggy": "https://mcp.swiggy.com/food",
    "zomato": "https://mcp-server.zomato.com/mcp",
}
SERVICE_KEYS = {
    "swiggy": "foodie-swiggy",
    "zomato": "foodie-zomato",
}


async def get_token(provider: str) -> str:
    store = SecretStore()
    raw = await store.get_credential(SERVICE_KEYS[provider], "tokens")
    if not raw:
        raise SystemExit(f"No stored tokens for {provider}. Run test_oauth.py first.")
    return json.loads(raw)["access_token"]


async def tools_list(provider: str, token: str) -> list[dict]:
    url = ENDPOINTS[provider]
    payload = {
        "jsonrpc": "2.0",
        "id": time.time_ns() // 1000,
        "method": "tools/list",
        "params": {},
    }
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    async with httpx.AsyncClient(timeout=30.0) as http:
        resp = await http.post(url, json=payload, headers=headers)

    resp.raise_for_status()

    content_type = resp.headers.get("content-type", "")
    if "text/event-stream" in content_type:
        for line in resp.text.splitlines():
            if line.startswith("data:"):
                data = line[5:].strip()
                if data:
                    return json.loads(data).get("result", {}).get("tools", [])
        return []
    else:
        return resp.json().get("result", {}).get("tools", [])


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--provider", choices=["swiggy", "zomato"], default="swiggy")
    parser.add_argument("--tool", help="Filter to a specific tool name")
    args = parser.parse_args()

    token = await get_token(args.provider)
    tools = await tools_list(args.provider, token)

    if args.tool:
        tools = [t for t in tools if t["name"] == args.tool]
        if not tools:
            raise SystemExit(f"Tool '{args.tool}' not found.")

    print(f"\n{'='*60}")
    print(f"{args.provider.upper()} MCP — {len(tools)} tool(s)")
    print(f"{'='*60}\n")

    for tool in tools:
        name = tool.get("name", "")
        desc = tool.get("description", "").strip()
        schema = tool.get("inputSchema", {})
        props = schema.get("properties", {})
        required = schema.get("required", [])

        print(f"🔧 {name}")
        if desc:
            # Print just the first sentence of the description
            first_line = desc.split("\n")[0][:120]
            print(f"   {first_line}")
        if props:
            print(f"   Parameters:")
            for param, info in props.items():
                req_marker = " *" if param in required else ""
                ptype = info.get("type", "any")
                pdesc = info.get("description", "")[:80]
                print(f"     - {param}{req_marker} ({ptype}): {pdesc}")
        else:
            print("   Parameters: none")

        # Dump full inputSchema to a separate JSON file per tool
        out_path = f"/home/engliz/Documents/repos/foodie/scripts/schemas/{args.provider}/{name}.json"
        import os

        os.makedirs(os.path.dirname(out_path), exist_ok=True)
        with open(out_path, "w") as f:
            json.dump(tool, f, indent=2)

        print()

    print(f"Full schemas written to scripts/schemas/{args.provider}/")


asyncio.run(main())
