#!/usr/bin/env python3
"""Debug: print raw Swiggy get_addresses response."""

import asyncio
import json
import time

import httpx

from foodie import ProviderName
from foodie.secret_store import SecretStore


async def main():
    store = SecretStore()
    raw = await store.get_credential("foodie-swiggy", "tokens", ProviderName.SWIGGY)
    tokens = json.loads(raw)
    access_token = tokens["access_token"]

    payload = {
        "jsonrpc": "2.0",
        "id": time.time_ns() // 1000,
        "method": "tools/call",
        "params": {"name": "get_addresses", "arguments": {}},
    }
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }

    async with httpx.AsyncClient(timeout=30.0) as http:
        resp = await http.post(
            "https://mcp.swiggy.com/food", json=payload, headers=headers
        )

    print("Status:", resp.status_code)
    print("Content-Type:", resp.headers.get("content-type"))
    print()

    content_type = resp.headers.get("content-type", "")
    if "text/event-stream" in content_type:
        print("=== RAW SSE LINES ===")
        for i, line in enumerate(resp.text.splitlines()):
            print(f"  [{i}] {repr(line[:200])}")
        print()
        for line in resp.text.splitlines():
            if line.startswith("data:"):
                raw_data = line[5:].strip()
                if raw_data:
                    parsed = json.loads(raw_data)
                    print("=== PARSED JSON ===")
                    print(json.dumps(parsed, indent=2))
                    break
    else:
        print("=== RAW JSON ===")
        print(json.dumps(resp.json(), indent=2))


asyncio.run(main())
