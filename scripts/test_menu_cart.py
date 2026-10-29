#!/usr/bin/env python3
"""Exercise a provider's restaurant-menu and add-to-cart MCP workflow.

The script uses the saved OAuth credentials from ``test_oauth.py``.  It lists
addresses, finds a restaurant, fetches its menu, and adds one selected item to
the live cart.  Adding an item changes the provider cart, so it asks for an
explicit confirmation unless ``--yes`` is supplied.

Examples:
    uv run python scripts/test_menu_cart.py --provider swiggy --query pizza
    uv run python scripts/test_menu_cart.py --provider zomato --query biryani \
        --payment-method cash_on_delivery
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Callable, Sequence
from typing import TypeVar

from foodie.models import Address, Cart, CartItem, MenuItem, Restaurant
from foodie.providers.base import BaseProvider
from foodie.providers.swiggy import SwiggyProvider
from foodie.providers.zomato import ZomatoProvider

T = TypeVar("T")


def select(prompt: str, values: Sequence[T], display: Callable[[T], str]) -> T:
    """Print indexed values and return the user's one-based selection."""
    if not values:
        raise RuntimeError("No results were returned.")
    for index, value in enumerate(values, start=1):
        print(f"[{index}] {display(value)}")
    while True:
        choice = input(prompt).strip()
        try:
            selected = int(choice)
            if 1 <= selected <= len(values):
                return values[selected - 1]
        except ValueError:
            pass
        print(f"Enter a number from 1 to {len(values)}.")


def print_cart(cart_items: list[CartItem], grand_total: float) -> None:
    print("\nCart:")
    for item in cart_items:
        print(f"  {item.quantity} × {item.name or item.item_id} — {item.price}")
    print(f"Grand total: {grand_total}")


async def choose_coupon(
    provider: BaseProvider, restaurant_id: str, address_id: str, cart: Cart
) -> Cart:
    coupons = await provider.get_cart_coupons(restaurant_id, address_id, cart)
    applied_code = cart.raw.get("promo_code")
    applied_discount = cart.raw.get("promo_discount_amount")
    if applied_code:
        print(
            f"Promo already applied: {applied_code}"
            f" (discount: {applied_discount or 0})"
        )
    applicable = [coupon for coupon in coupons if coupon.applicable]
    if not applicable:
        print("No applicable coupons were returned.")
        return cart
    coupon = select(
        "Choose a coupon: ",
        applicable,
        lambda item: (
            f"[{', '.join(label for label, enabled in (('recommended', item.recommended), ('eligible', item.applicable)) if enabled)}] "
            f"{item.code} - {item.title or item.description}"
        ),
    )
    if input(f"Apply {coupon.code}? [y/N] ").strip().lower() != "y":
        return cart
    return await apply_coupon(provider, coupon.code, address_id, cart)


async def apply_coupon(
    provider: BaseProvider, code: str, address_id: str, cart: Cart
) -> Cart:
    try:
        updated_cart = await provider.apply_coupon(code, address_id)
    except NotImplementedError:
        print(f"Selected {code}; this provider does not expose coupon application.")
        return cart
    print(f"Applied {code}.")
    return updated_cart


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provider", choices=["swiggy", "zomato"], required=True)
    parser.add_argument(
        "--query",
        required=True,
        help="Restaurant or cuisine search query (required by Swiggy)",
    )
    parser.add_argument("--address-id", help="Saved delivery address ID")
    parser.add_argument(
        "--restaurant-id", help="Restaurant ID, skipping restaurant selection"
    )
    parser.add_argument("--item-id", help="Menu item ID, skipping item selection")
    parser.add_argument("--quantity", type=int, default=1)
    parser.add_argument(
        "--payment-method",
        choices=["upi", "cash_on_delivery"],
        help="Required by Zomato when creating a cart; ignored by Swiggy",
    )
    parser.add_argument(
        "--yes", action="store_true", help="Skip add-to-cart confirmation"
    )
    parser.add_argument(
        "--coupon-code",
        help="Apply this coupon code after creating the cart, without listing offers",
    )
    args = parser.parse_args()

    if args.quantity < 1:
        parser.error("--quantity must be at least 1")
    if args.provider == "swiggy" and not args.query.strip():
        parser.error("Swiggy requires a non-empty --query, e.g. --query pizza")
    if args.provider == "zomato" and not args.payment_method:
        parser.error("Zomato requires --payment-method (upi or cash_on_delivery)")

    provider: BaseProvider
    provider = SwiggyProvider() if args.provider == "swiggy" else ZomatoProvider()
    if not await provider.is_authenticated():
        print("No valid session. Run scripts/test_oauth.py for this provider first.")
        return 1

    addresses = await provider.get_addresses()
    address: Address
    if args.address_id:
        address = next((item for item in addresses if item.id == args.address_id), None)
        if address is None:
            raise ValueError(
                f"Address {args.address_id!r} was not returned by the provider."
            )
    else:
        address = select(
            "Choose a delivery address: ",
            addresses,
            lambda item: f"{item.label}: {item.formatted_address} ({item.id})",
        )

    if args.restaurant_id:
        restaurant = Restaurant(id=args.restaurant_id, name=args.restaurant_id)
    else:
        restaurants = await provider.search_restaurants(args.query, address.id)
        restaurant = select(
            "Choose a restaurant: ",
            restaurants,
            lambda item: f"{item.name} ({item.id})",
        )

    menu = await provider.get_menu(restaurant.id, address.id)
    if args.item_id:
        menu_item = next((item for item in menu if item.id == args.item_id), None)
        if menu_item is None:
            raise ValueError(
                f"Menu item {args.item_id!r} was not returned by the provider."
            )
    else:
        menu_item = select(
            "Choose a menu item: ",
            menu,
            lambda item: f"{item.name} ({item.id}) — {item.price}",
        )

    print(
        f"\nSelected: {args.quantity} × {menu_item.name} from {restaurant.name} "
        f"for delivery to {address.label}."
    )
    if (
        not args.yes
        and input("Add this item to the live cart? [y/N] ").strip().lower() != "y"
    ):
        print("Cart unchanged.")
        return 0

    cart = await provider.update_cart(
        restaurant.id,
        [
            CartItem(
                item_id=menu_item.id,
                name=menu_item.name,
                price=menu_item.price,
                quantity=args.quantity,
            )
        ],
        address.id,
        payment_method=args.payment_method,
    )
    if args.coupon_code:
        cart = await apply_coupon(provider, args.coupon_code.strip(), address.id, cart)
    elif input("List applicable coupons? [Y/n] ").strip().lower() != "n":
        cart = await choose_coupon(provider, restaurant.id, address.id, cart)
    else:
        manual_code = input("Enter a coupon code to apply (blank to skip): ").strip()
        if manual_code:
            cart = await apply_coupon(provider, manual_code, address.id, cart)
    print_cart(cart.items, cart.grand_total)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
