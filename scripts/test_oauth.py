#!/usr/bin/env python3
"""
Test script to run the interactive OAuth flow for Swiggy or Zomato and verify credentials.
Usage:
    uv run python scripts/test_oauth.py [--provider swiggy|zomato] [--reauth] [--port PORT]
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from foodie.auth import authenticate_interactive
from foodie.providers.base import BaseProvider
from foodie.providers.swiggy import SwiggyProvider
from foodie.providers.zomato import ZomatoProvider


async def main() -> int:
    parser = argparse.ArgumentParser(
        description="Test provider OAuth flow and tool calls"
    )
    parser.add_argument(
        "--provider",
        choices=["swiggy", "zomato"],
        default="swiggy",
        help="Provider to authenticate with (default: swiggy)",
    )
    parser.add_argument(
        "--reauth",
        action="store_true",
        help="Force re-authentication even if tokens exist in keyring",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8080,
        help="Local port for callback redirect (default: 8080)",
    )
    args = parser.parse_args()

    provider: BaseProvider = (
        SwiggyProvider() if args.provider == "swiggy" else ZomatoProvider()
    )

    print(
        f"Checking existing credentials for {provider.display_name} in D-Bus keyring..."
    )
    already_authed = await provider.is_authenticated()

    if already_authed and not args.reauth:
        print(f"✓ Already authenticated with {provider.display_name}!")
        choice = (
            input(
                "Fetch saved addresses to test token? [Y/n] (or 'r' to re-authenticate): "
            )
            .strip()
            .lower()
        )
        if choice == "r":
            already_authed = False
        elif choice == "n":
            return 0

    if not already_authed or args.reauth:
        if args.reauth:
            await provider.logout()

        success = await authenticate_interactive(provider, port=args.port)
        if not success:
            print("❌ Authentication flow failed.")
            return 1

    # Verify token by making a live tool call
    print("\n" + "=" * 60)
    print(f"🧪 Verifying live tool call: get_addresses() on {provider.display_name}")
    print("=" * 60)
    try:
        addresses = await provider.get_addresses()
        print(
            f"\n✓ Successfully fetched {len(addresses)} address(es) from {provider.display_name}:\n"
        )
        for idx, addr in enumerate(addresses, 1):
            print(f"  [{idx}] {addr.label} (ID: {addr.id})")
            print(f"      {addr.formatted_address}")
            if addr.latitude and addr.longitude:
                print(f"      Coordinates: {addr.latitude}, {addr.longitude}")
            print()
    except Exception as exc:
        print(f"\n❌ Error calling {provider.display_name} API: {exc}")
        return 1

    print("🎉 All checks passed! OAuth flow and MCP tool call are working end-to-end.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
