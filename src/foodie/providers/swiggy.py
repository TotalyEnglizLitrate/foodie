from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import re
import secrets
import time
from typing import Any, cast, override
from urllib.parse import urlencode

import httpx

from foodie.models import (
    Address,
    Cart,
    CartCoupon,
    CartItem,
    MenuItem,
    OrderPlacement,
    OrderStatus,
    OrderSummary,
    OrderTracking,
    PaymentMethod,
    ProviderName,
    Restaurant,
)
from foodie.providers.base import BaseProvider
from foodie.secret_store import SecretStore


class SwiggyProvider(BaseProvider):
    """
    Swiggy implementation of BaseProvider interacting with Swiggy's MCP endpoints
    over Streamable HTTP / JSON-RPC with OAuth 2.1 PKCE session management.
    """

    BASE_URL: str = "https://mcp.swiggy.com"
    SERVICE_KEY: str = "foodie"
    ACCOUNT_KEY: str = "tokens"
    CLIENT_INFO_KEY: str = "client_info"
    MENU_PAGE_SIZE: int = 8
    MAX_MENU_PAGES: int = 100

    def __init__(self, store: SecretStore | None = None) -> None:
        self.store: SecretStore = store or SecretStore()
        self._http: httpx.AsyncClient = httpx.AsyncClient(timeout=30.0)
        self._active_address_id: str | None = None
        self._address_coords: dict[str, tuple[float, float]] = {}
        self._cart_id: str | None = None
        self._order_contexts: dict[str, dict[str, Any]] = {}

    @property
    @override
    def name(self) -> ProviderName:
        return ProviderName.SWIGGY

    @property
    @override
    def display_name(self) -> str:
        return "Swiggy"

    async def _get_tokens(
        self,
    ) -> dict[str, Any] | None:  # pyright: ignore[reportExplicitAny]
        raw = await self.store.get_credential(
            self.SERVICE_KEY, self.ACCOUNT_KEY, self.name
        )
        if not raw:
            return None
        try:
            return json.loads(raw)  # pyright: ignore[reportAny]
        except Exception:
            return None

    async def _save_tokens(
        self,
        tokens: dict[str, Any],  # pyright: ignore[reportExplicitAny]
    ) -> None:
        await self.store.set_credential(
            self.SERVICE_KEY,
            self.ACCOUNT_KEY,
            json.dumps(tokens),
            label=self.name,
        )

    @override
    async def is_authenticated(self) -> bool:
        tokens = await self._get_tokens()
        if not tokens or "access_token" not in tokens:
            return False

        # If expired or near expiration (within 60 seconds), attempt refresh
        expires_at = tokens.get("expires_at", 0)  # pyright: ignore[reportAny]
        if time.time() >= expires_at - 60:
            return await self.refresh_auth()

        return True

    @override
    async def get_auth_url(self, redirect_uri: str) -> tuple[str, str]:
        # Generate PKCE verifier + challenge (S256)
        verifier_bytes = secrets.token_bytes(32)
        code_verifier = (
            base64.urlsafe_b64encode(verifier_bytes).decode("utf-8").rstrip("=")
        )
        digest = hashlib.sha256(code_verifier.encode("utf-8")).digest()
        code_challenge = base64.urlsafe_b64encode(digest).decode("utf-8").rstrip("=")

        # Dynamic Client Registration (RFC 7591)
        client_id = await self._get_or_register_client(redirect_uri)

        state = secrets.token_urlsafe(16)
        params = {
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
            "state": state,
            "scope": "mcp:tools mcp:resources mcp:prompts",
        }
        auth_url = f"{self.BASE_URL}/auth/authorize?{urlencode(params)}"
        return auth_url, code_verifier

    async def _get_or_register_client(self, redirect_uri: str) -> str:
        raw_info = await self.store.get_credential(
            self.SERVICE_KEY, self.CLIENT_INFO_KEY, self.name
        )
        if raw_info:
            try:
                info: dict[str, Any] = json.loads(raw_info)  # pyright: ignore[reportExplicitAny, reportAny]
                if info.get("redirect_uri") == redirect_uri and "client_id" in info:
                    return str(info["client_id"])  # pyright: ignore[reportAny]
            except Exception:
                pass

        # Register dynamically with Swiggy
        resp = await self._http.post(
            f"{self.BASE_URL}/auth/register",
            json={
                "client_name": "foodie-tui",
                "redirect_uris": [redirect_uri],
                "grant_types": ["authorization_code", "refresh_token"],
                "response_types": ["code"],
            },
        )
        if resp.status_code in (200, 201):
            data: dict[str, Any] = resp.json()  # pyright: ignore[reportExplicitAny, reportAny]
            client_id = cast(str, data.get("client_id", "foodie-client"))
            await self.store.set_credential(
                self.SERVICE_KEY,
                self.CLIENT_INFO_KEY,
                json.dumps({"client_id": client_id, "redirect_uri": redirect_uri}),
                self.name,
            )
            return client_id

        # Fallback client identifier if DCR response doesn't return one
        return "foodie-client"

    @override
    async def complete_auth(self, code: str, verifier: str, redirect_uri: str) -> bool:
        resp = await self._http.post(
            f"{self.BASE_URL}/auth/token",
            json={
                "grant_type": "authorization_code",
                "code": code,
                "code_verifier": verifier,
                "redirect_uri": redirect_uri,
            },
        )
        if resp.status_code != 200:
            return False

        data: dict[str, Any] = resp.json()  # pyright: ignore[reportAny, reportExplicitAny]
        expires_in = cast(int, data.get("expires_in", 432000))
        tokens = {
            "access_token": data.get("access_token"),
            "refresh_token": data.get("refresh_token"),
            "expires_at": time.time() + expires_in,
        }
        await self._save_tokens(tokens)
        return True

    @override
    async def refresh_auth(self) -> bool:
        tokens = await self._get_tokens()
        if not tokens or "refresh_token" not in tokens:
            return False

        resp = await self._http.post(
            f"{self.BASE_URL}/auth/token",
            json={
                "grant_type": "refresh_token",
                "refresh_token": tokens["refresh_token"],
            },
        )
        if resp.status_code != 200:
            return False

        data: dict[str, Any] = resp.json()  # pyright: ignore[reportAny, reportExplicitAny]
        expires_in = cast(int, data.get("expires_in", 2592000))
        tokens["access_token"] = data.get("access_token")
        if "refresh_token" in data:
            tokens["refresh_token"] = data.get("refresh_token")
        tokens["expires_at"] = time.time() + expires_in
        await self._save_tokens(tokens)
        return True

    @override
    async def logout(self) -> None:
        _ = await self.store.delete_credential(
            self.SERVICE_KEY, self.ACCOUNT_KEY, self.name
        )

    async def _call_tool(
        self,
        tool_name: str,
        arguments: dict[str, Any],  # pyright: ignore[reportExplicitAny]
        server: str = "food",
    ) -> dict[str, Any]:  # pyright: ignore[reportExplicitAny]
        tokens = await self._get_tokens()
        if not tokens or not tokens.get("access_token"):
            raise PermissionError("Not authenticated with Swiggy. Please login first.")

        access_token = tokens["access_token"]  # pyright: ignore[reportAny]
        payload = {
            "jsonrpc": "2.0",
            "id": time.time_ns() // 1000,
            "method": "tools/call",
            "params": {"name": tool_name, "arguments": arguments},
        }

        url = f"{self.BASE_URL}/{server}"
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }

        resp = await self._http.post(url, json=payload, headers=headers)
        if resp.status_code == 401:
            refreshed = await self.refresh_auth()
            if refreshed:
                tokens = await self._get_tokens()
                assert tokens is not None
                headers["Authorization"] = f"Bearer {tokens['access_token']}"
                resp = await self._http.post(url, json=payload, headers=headers)
            else:
                raise PermissionError("Swiggy session expired. Please re-authenticate.")

        if resp.status_code >= 400:
            raise RuntimeError(
                f"Swiggy tool call failed ({resp.status_code}): {resp.text}"
            )

        content_type = resp.headers.get("content-type", "")  # pyright: ignore[reportAny]
        if "text/event-stream" in content_type:
            res_json = self._parse_sse(resp.text, payload["id"])
        else:
            res_json = resp.json()  # pyright: ignore[reportAny]

        if not isinstance(res_json, dict):
            raise RuntimeError(
                f"Swiggy returned a malformed response for {tool_name}: {res_json!r}"
            )
        if "error" in res_json:
            err = res_json["error"]  # pyright: ignore[reportUnknownVariableType]
            message = (  # pyright: ignore[reportUnknownVariableType]
                err.get("message", err)  # pyright: ignore[reportUnknownMemberType]
                if isinstance(err, dict)
                else err
            )
            raise RuntimeError(f"Swiggy tool error: {message}")

        return self._unwrap_result(  # pyright: ignore[reportAny]
            tool_name,
            res_json.get("result", {}),  # pyright: ignore[reportUnknownMemberType]
        )

    @staticmethod
    def _parse_sse(
        body: str,
        request_id: Any,  # pyright: ignore[reportAny, reportExplicitAny]
    ) -> dict[str, Any]:  # pyright: ignore[reportExplicitAny]
        """Select the JSON-RPC response for this request from an SSE stream.

        Streamable HTTP can include notifications before the response, so the
        first data event is not necessarily the response to our request.
        """
        fallback: dict[str, Any] | None = None  # pyright: ignore[reportExplicitAny]
        for line in body.splitlines():
            if not line.startswith("data:"):
                continue
            raw = line[5:].strip()
            if not raw:
                continue
            try:
                message = json.loads(raw)  # pyright: ignore[reportAny]
            except ValueError:
                continue
            if not isinstance(message, dict):
                continue
            if message.get("id") == request_id:  # pyright: ignore[reportUnknownMemberType]
                return message  # pyright: ignore[reportUnknownVariableType]
            if fallback is None and ("result" in message or "error" in message):
                fallback = message  # pyright: ignore[reportUnknownVariableType]

        if fallback is None:
            raise RuntimeError(f"Swiggy returned empty SSE response: {body}")
        return fallback

    @staticmethod
    def _unwrap_result(  # pyright: ignore[reportAny]
        tool_name: str,
        result: Any,  # pyright: ignore[reportAny, reportExplicitAny]
    ) -> Any:  # pyright: ignore[reportExplicitAny]
        """Normalize an MCP tool result while preserving tool payloads.

        Swiggy's documented tool envelope is ``{success, data, message}``;
        failures use ``{success: false, error: {message}}``. MCP may expose
        that envelope through structuredContent or a JSON text content block.
        """
        if not isinstance(result, dict):
            return result  # pyright: ignore[reportAny]

        text_parts = [
            item.get("text", "")
            for item in result.get("content") or []
            if isinstance(item, dict) and item.get("type") == "text"
        ]
        if result.get("isError"):
            raise RuntimeError("\n".join(text_parts).strip() or f"{tool_name} failed")

        payload: Any = result.get("structuredContent")
        if payload is None:
            for text in text_parts:
                try:
                    payload = json.loads(text)
                    break
                except ValueError:
                    continue
        if payload is None:
            payload = result

        # FastMCP can wrap non-object return values in a result object.
        if isinstance(payload, dict) and set(payload) == {"result"}:
            payload = payload["result"]

        if isinstance(payload, dict):
            if payload.get("success") is False:
                error = payload.get("error")
                if isinstance(error, dict):
                    message = error.get("message") or payload.get("message")
                else:
                    message = error or payload.get("message")
                raise RuntimeError(
                    f"Swiggy {tool_name} failed: {message or 'unknown error'}"
                )

            # Unwrap the documented success envelope, but only when `data` is
            # present so metadata-only payloads remain intact.
            if payload.get("success") is True and "data" in payload:
                return payload["data"]
            if "data" in payload and len(payload) == 1:
                return payload["data"]

        return payload

    @override
    async def get_addresses(self) -> list[Address]:
        res = await self._call_tool("get_addresses", {})
        addresses_raw: list[dict[str, Any]] = res.get("addresses", [])  # pyright: ignore[reportExplicitAny, reportAny]
        addresses: list[Address] = []
        for a in addresses_raw:
            addr_id = str(a.get("id") or a.get("addressId") or "")
            lat = self._as_float(a.get("lat") or a.get("latitude"))
            lng = self._as_float(a.get("lng") or a.get("longitude"))
            if addr_id and lat is not None and lng is not None:
                self._address_coords[addr_id] = (lat, lng)

            addresses.append(
                Address(
                    id=addr_id,
                    label=a.get("addressTag")
                    or a.get("addressCategory")
                    or a.get("label", "Address"),
                    formatted_address=a.get("addressLine")
                    or a.get("address")
                    or a.get("formattedAddress", ""),
                    latitude=lat,
                    longitude=lng,
                    raw=a,
                )
            )
        return addresses

    @staticmethod
    def _as_float(
        value: Any,
    ) -> float | None:  # pyright: ignore[reportAny, reportExplicitAny]
        """Accept MCP numeric fields that may be returned as display text."""
        if value is None:
            return None
        if isinstance(value, (int, float)):
            return float(value)
        match = re.search(r"\d+(?:\.\d+)?", str(value).replace(",", ""))
        return float(match.group()) if match else None

    @override
    async def search_restaurants(self, query: str, address_id: str) -> list[Restaurant]:
        if not query.strip():
            raise ValueError("Swiggy restaurant searches require a non-empty query.")
        self._active_address_id = address_id
        res = await self._call_tool(
            "search_restaurants", {"query": query, "addressId": address_id}
        )
        restaurants_raw = res.get("restaurants", []) if isinstance(res, dict) else res
        restaurants: list[Restaurant] = []
        for r in restaurants_raw or []:
            if not isinstance(r, dict) or not (r.get("id") or r.get("restaurantId")):
                continue

            # The current Swiggy MCP contract exposes normalized fields rather
            # than the legacy app API's nested `sla`/Cloudinary fields.
            delivery_time_value = r.get("deliveryTimeMinutes")
            if delivery_time_value is None:
                time_range = r.get("deliveryTimeRange")
                if isinstance(time_range, dict):
                    delivery_time_value = time_range.get("min") or time_range.get("max")
                else:
                    delivery_time_value = time_range
            parsed_delivery_time = self._as_float(delivery_time_value)
            delivery_time = (
                int(parsed_delivery_time) if parsed_delivery_time is not None else None
            )

            rating = self._as_float(r.get("avgRating") or r.get("rating"))
            availability = r.get("availabilityStatus")
            restaurants.append(
                Restaurant(
                    id=str(r.get("id") or r.get("restaurantId")),
                    name=r.get("name") or "Unknown Restaurant",
                    cuisines=r.get("cuisines") or [],
                    rating=rating,
                    delivery_time_mins=delivery_time,
                    distance_km=self._as_float(r.get("distanceKm")),
                    cost_for_two=self._as_float(r.get("costForTwo")),
                    # Unknown availability must not be presented as confirmed open.
                    is_open=isinstance(availability, str)
                    and availability.upper() == "OPEN",
                    image_url=r.get("imageUrl") or r.get("cloudinaryImageId"),
                    raw=r,
                )
            )
        return restaurants

    def _address_id(self, address_id: str | None = None) -> str:
        if address_id:
            self._active_address_id = address_id
        if not self._active_address_id:
            raise ValueError("An address_id is required for this Swiggy operation.")
        return self._active_address_id

    @staticmethod
    def _menu_items(res: dict[str, Any]) -> list[dict[str, Any]]:
        """Flatten the category-based menu response returned by Swiggy MCP."""
        items = res.get("items")
        if isinstance(items, list):
            return items

        flattened: list[dict[str, Any]] = []
        for category in res.get("categories", []):
            if not isinstance(category, dict):
                continue
            category_name = category.get("title") or category.get("name") or ""
            for item in category.get("items", []):
                if isinstance(item, dict):
                    flattened.append({**item, "category": category_name})
            # Nested categories (MenuNestedCategory) carry subcategories[]
            for subcat in category.get("subcategories", []):
                if isinstance(subcat, dict):
                    subcat_name = (
                        subcat.get("title") or subcat.get("name") or category_name
                    )
                    for item in subcat.get("items", []):
                        if isinstance(item, dict):
                            flattened.append({**item, "category": subcat_name})
        return flattened

    @staticmethod
    def _has_more_menu_categories(res: dict[str, Any]) -> bool:
        """Return whether more category pages exist in get_restaurant_menu."""
        if "hasMore" in res:
            return bool(res.get("hasMore"))
        # Fallback if hasMore is not explicitly present
        categories = res.get("categories", [])
        return len(categories) >= SwiggyProvider.MENU_PAGE_SIZE

    @override
    async def get_menu(
        self, restaurant_id: str, address_id: str | None = None
    ) -> list[MenuItem]:
        resolved_address_id = self._address_id(address_id)
        items_raw: list[dict[str, Any]] = []
        seen_ids: set[str] = set()
        for page in range(1, self.MAX_MENU_PAGES + 1):
            res = await self._call_tool(
                "get_restaurant_menu",
                {
                    "restaurantId": restaurant_id,
                    "addressId": resolved_address_id,
                    "page": page,
                    "pageSize": self.MENU_PAGE_SIZE,
                },
            )
            page_items = self._menu_items(res) if isinstance(res, dict) else res
            new_items = 0
            for item in page_items:
                item_id = str(
                    item.get("id")
                    or item.get("itemId")
                    or item.get("menu_item_id")
                    or ""
                )
                # "Recommended" repeats items found in their actual category.
                # Keep an ID-less item rather than silently dropping it.
                if item_id and item_id in seen_ids:
                    continue
                if item_id:
                    seen_ids.add(item_id)
                items_raw.append(item)
                new_items += 1

            if not isinstance(res, dict) or not self._has_more_menu_categories(res):
                break
            # Protect against an endpoint that ignores page and repeats page 1.
            if not page_items or not new_items:
                break

        items: list[MenuItem] = []
        for item in items_raw:
            price = float(item.get("price", 0))
            items.append(
                MenuItem(
                    id=str(
                        item.get("id")
                        or item.get("itemId")
                        or item.get("menu_item_id")
                        or ""
                    ),
                    name=item.get("name", ""),
                    price=price,
                    description=item.get("description", ""),
                    is_veg=bool(item.get("isVeg", True)),
                    in_stock=bool(item.get("inStock", True)),
                    category=item.get("category", ""),
                    variants=item.get("variants")
                    or item.get("variantsV2")
                    or item.get("variations")
                    or [],
                    addons=item.get("addons", []),
                    raw=item,
                )
            )
        return items

    @override
    async def search_menu(
        self, restaurant_id: str, query: str, address_id: str | None = None
    ) -> list[MenuItem]:
        res = await self._call_tool(
            "search_menu",
            {
                "restaurantIdOfAddedItem": restaurant_id,
                "query": query,
                "addressId": self._address_id(address_id),
            },
        )
        items_raw = self._menu_items(res) if isinstance(res, dict) else res
        items: list[MenuItem] = []
        for item in items_raw:
            price = float(item.get("price", 0))
            items.append(
                MenuItem(
                    id=str(
                        item.get("menu_item_id")
                        or item.get("id")
                        or item.get("itemId")
                        or ""
                    ),
                    name=item.get("name", ""),
                    price=price,
                    description=item.get("description", ""),
                    is_veg=bool(item.get("isVeg", True)),
                    in_stock=bool(item.get("inStock", True)),
                    category=item.get("category", ""),
                    variants=item.get("variants")
                    or item.get("variantsV2")
                    or item.get("variations")
                    or [],
                    addons=item.get("addons", []),
                    raw=item,
                )
            )
        return items

    @override
    async def get_cart(self, address_id: str | None = None) -> Cart:
        res = await self._call_tool(
            "get_food_cart", {"addressId": self._address_id(address_id)}
        )
        cart_data = res.get("data", res) if isinstance(res, dict) else {}
        if isinstance(cart_data, dict) and cart_data.get("cart_id") is not None:
            self._cart_id = str(cart_data["cart_id"])
        return self._parse_cart(res)

    @override
    async def update_cart(
        self,
        restaurant_id: str,
        items: list[CartItem],
        address_id: str,
        payment_method: str | None = None,
    ) -> Cart:
        payload_items = []
        for it in items:
            # Items use either the legacy "variants" or the "variantsV2" field, never both.
            v2 = any(v.get("_format") == "v2" for v in it.selected_variants)
            variants = [
                {k: val for k, val in v.items() if k != "_format"}
                for v in it.selected_variants
            ]
            payload_items.append(
                {
                    "menu_item_id": it.item_id,
                    "quantity": it.quantity,
                    "variantsV2" if v2 else "variants": variants,
                    "addons": it.selected_addons,
                }
            )
        res = await self._call_tool(
            "update_food_cart",
            {
                "restaurantId": restaurant_id,
                "cartItems": payload_items,
                "addressId": self._address_id(address_id),
            },
        )
        cart_data = res.get("data", res) if isinstance(res, dict) else {}
        if isinstance(cart_data, dict) and cart_data.get("cart_id") is not None:
            self._cart_id = str(cart_data["cart_id"])
        return self._parse_cart(res)

    @override
    async def clear_cart(self) -> None:
        _ = await self._call_tool("flush_food_cart", {})
        self._cart_id = None

    @override
    async def apply_coupon(self, code: str, address_id: str | None = None) -> Cart:
        arguments: dict[str, Any] = {
            "couponCode": code.strip(),
            "addressId": self._address_id(address_id),
        }
        if self._cart_id is not None:
            arguments["cartId"] = self._cart_id
        res = await self._call_tool(
            "apply_food_coupon",
            arguments,
        )
        cart_data = res.get("data", res) if isinstance(res, dict) else {}
        if isinstance(cart_data, dict) and cart_data.get("cart_id") is not None:
            self._cart_id = str(cart_data["cart_id"])
        return self._parse_cart(res)

    @override
    async def get_cart_coupons(
        self,
        restaurant_id: str,
        address_id: str,
        cart: Cart | None = None,
    ) -> list[CartCoupon]:
        del cart
        res = await self._call_tool(
            "fetch_food_coupons",
            {"restaurantId": restaurant_id, "addressId": self._address_id(address_id)},
        )
        coupons: list[CartCoupon] = []
        sections = res.get("coupon_sections", []) if isinstance(res, dict) else []
        for section in sections:
            if not isinstance(section, dict):
                continue
            for offer in section.get("coupons", []):
                if not isinstance(offer, dict):
                    continue
                code = offer.get("id")
                if not code:
                    continue
                coupons.append(
                    CartCoupon(
                        code=str(code),
                        title=str(offer.get("title") or ""),
                        description=str(
                            offer.get("description")
                            or offer.get("subtitle")
                            or offer.get("ribbon_text")
                            or ""
                        ),
                        applicable=bool(offer.get("applicable", False)),
                        raw=offer,
                    )
                )
        return coupons

    def _parse_cart(self, res: dict[str, Any]) -> Cart:
        cart_data = res.get("data", res) if isinstance(res, dict) else {}
        restaurant_info = (
            cart_data.get("restaurant") if isinstance(cart_data, dict) else None
        )
        restaurant_id = ""
        if isinstance(restaurant_info, dict):
            restaurant_id = str(restaurant_info.get("id") or "")
        if not restaurant_id and isinstance(cart_data, dict):
            restaurant_id = str(
                cart_data.get("restaurantId") or cart_data.get("restaurant_id") or ""
            )

        items_raw = cart_data.get("items", []) if isinstance(cart_data, dict) else []
        parsed_items: list[CartItem] = []
        for it in items_raw:
            if not isinstance(it, dict):
                continue
            parsed_items.append(
                CartItem(
                    item_id=str(
                        it.get("menu_item_id") or it.get("itemId") or it.get("id") or ""
                    ),
                    name=it.get("name", ""),
                    price=float(it.get("final_price") or it.get("price") or 0),
                    quantity=int(it.get("quantity", 1)),
                    selected_variants=it.get("variants", []),
                    selected_addons=it.get("addons", []),
                )
            )

        bill = (
            cart_data.get("pricing") or cart_data.get("bill") or cart_data
            if isinstance(cart_data, dict)
            else {}
        )
        offers = cart_data.get("offers", {}) if isinstance(cart_data, dict) else {}
        if not isinstance(offers, dict):
            offers = {}
        if not isinstance(bill, dict):
            bill = {}

        return Cart(
            restaurant_id=restaurant_id,
            items=parsed_items,
            item_total=float(bill.get("item_total") or bill.get("itemTotal") or 0),
            delivery_fee=float(
                bill.get("delivery_charge")
                or bill.get("delivery_fee")
                or bill.get("deliveryFee")
                or 0
            ),
            taxes=float(
                bill.get("taxes_and_charges")
                or bill.get("taxes")
                or bill.get("packaging_fee")
                or 0
            ),
            discount=float(
                offers.get("coupon_discount")
                or bill.get("coupon_discount")
                or bill.get("discount")
                or 0
            ),
            grand_total=float(
                bill.get("to_pay") or bill.get("grandTotal") or bill.get("total") or 0
            ),
            raw=cart_data,
        )

    @override
    async def get_payment_options(self, address_id: str) -> list[PaymentMethod]:
        self._active_address_id = address_id
        res = await self._call_tool("get_payment_options", {"addressId": address_id})
        options: list[PaymentMethod] = []

        # Check COD
        cod = res.get("cod")
        if (
            (isinstance(cod, dict) and cod.get("available"))
            or (isinstance(cod, bool) and cod)
            or res.get("codAvailable")
        ):
            cod_id = cod.get("id", "COD") if isinstance(cod, dict) else "COD"
            cod_name = (
                cod.get("displayName", "Cash on Delivery")
                if isinstance(cod, dict)
                else "Cash on Delivery"
            )
            options.append(
                PaymentMethod(
                    id=cod_id,
                    name=cod_name,
                    method_type="COD",
                    details=cod if isinstance(cod, dict) else {},
                )
            )

        # Check Swiggy Money
        swiggy_money = res.get("swiggyMoney")
        if isinstance(swiggy_money, dict) and swiggy_money.get("available"):
            options.append(
                PaymentMethod(
                    id=swiggy_money.get("id", "SwiggyPay"),
                    name=swiggy_money.get("displayName", "Swiggy Money"),
                    method_type="WALLET",
                    details=swiggy_money,
                )
            )

        # Platforms (desktop / mobile UPI)
        platforms = res.get("platforms", {})
        if isinstance(platforms, dict):
            desktop = platforms.get("desktop", {})
            if isinstance(desktop, dict):
                for m in desktop.get("methods", []):
                    if isinstance(m, dict):
                        options.append(
                            PaymentMethod(
                                id=m.get("id", "UPI_QR"),
                                name=m.get("displayName")
                                or m.get("name", "UPI Scan & Pay"),
                                method_type="UPI_QR",
                                details=m,
                            )
                        )

            mobile = platforms.get("mobile", {})
            if isinstance(mobile, dict):
                for m in mobile.get("methods", []):
                    if isinstance(m, dict):
                        options.append(
                            PaymentMethod(
                                id=m.get("id", "UPI_INTENT"),
                                name=m.get("displayName") or m.get("name", "UPI App"),
                                method_type="UPI_INTENT",
                                details=m,
                            )
                        )

        # Fallback to allMethods if platforms wasn't populated
        if not options and "allMethods" in res:
            for m in res.get("allMethods", []):
                if isinstance(m, dict):
                    m_kind = (m.get("kind") or "").upper()
                    m_type = (
                        "UPI_QR"
                        if m_kind == "QR"
                        else ("UPI_INTENT" if m_kind == "INTENT" else "OTHER")
                    )
                    options.append(
                        PaymentMethod(
                            id=m.get("id", ""),
                            name=m.get("displayName")
                            or m.get("name", "Payment Option"),
                            method_type=m_type,
                            details=m,
                        )
                    )

        return options

    @override
    async def place_order(
        self, payment_method: str, address_id: str, **kwargs: Any
    ) -> OrderPlacement:
        self._active_address_id = address_id
        args: dict[str, Any] = {
            "addressId": address_id,
            "paymentMethod": payment_method,
            **kwargs,
        }
        res = await self._call_tool("place_food_order", args)

        order_id = str(res.get("orderId", ""))
        status_str = res.get("status", "PLACED").upper()
        requires_payment = status_str in ("PENDING_PAYMENT", "PENDING")

        payment_url = (
            res.get("bridgeUrl") or res.get("paymentUrl") or res.get("upiIntentUrl")
        )
        upi_qr = (
            res.get("upiQrString")
            or res.get("qrData")
            or (res.get("upiIntentUrl") if res.get("isQrFlow") else None)
        )

        # Cache order context for subsequent check_payment_status / confirm_order
        if order_id:
            coords = self._address_coords.get(address_id, (None, None))
            lat = res.get("lat") if res.get("lat") is not None else coords[0]
            lng = res.get("lng") if res.get("lng") is not None else coords[1]
            self._order_contexts[order_id] = {
                "addressId": res.get("addressId") or address_id,
                "cartId": res.get("cartId"),
                "lat": lat,
                "lng": lng,
                "restaurantName": res.get("restaurantName"),
                "totalAmount": self._as_float(res.get("totalAmount")),
            }

        return OrderPlacement(
            order_id=order_id,
            status=(
                OrderStatus.PENDING_PAYMENT if requires_payment else OrderStatus.PLACED
            ),
            requires_payment=requires_payment,
            upi_qr_string=upi_qr,
            payment_url=payment_url,
            max_poll_time_ms=cast(int, res.get("maxTimeToPollForInMs", 180000)),
            poll_interval_ms=cast(int, res.get("pollingIntervalInMs", 5000)),
            raw=res,
        )

    @override
    async def check_payment_status(
        self, order_id: str, **kwargs: Any
    ) -> dict[str, Any]:
        paas_id = cast(str, kwargs.pop("paasId", None) or kwargs.pop("paas_id", None))
        if not paas_id:
            raise ValueError(
                "Swiggy payment-status checks require the checkout paasId."
            )
        args: dict[str, Any] = {"paasId": paas_id, "orderId": order_id}

        ctx = self._order_contexts.get(order_id, {})
        address_id = (
            kwargs.pop("addressId", None)
            or ctx.get("addressId")
            or self._active_address_id
        )
        if address_id:
            args["addressId"] = address_id
        lat = kwargs.pop("lat", None) or ctx.get("lat")
        lng = kwargs.pop("lng", None) or ctx.get("lng")
        if lat is not None:
            args["lat"] = lat
        if lng is not None:
            args["lng"] = lng
        cart_id = kwargs.pop("cartId", None) or ctx.get("cartId")
        if cart_id:
            args["cartId"] = cart_id

        args.update(kwargs)
        return await self._call_tool("check_payment_status", args)

    @override
    async def confirm_order(self, order_id: str, **kwargs: Any) -> OrderSummary:
        ctx = self._order_contexts.get(order_id, {})
        args: dict[str, Any] = {"orderId": order_id}

        address_id = (
            kwargs.pop("addressId", None)
            or ctx.get("addressId")
            or self._active_address_id
        )
        if address_id:
            args["addressId"] = address_id

        lat = kwargs.pop("lat", None) or ctx.get("lat")
        lng = kwargs.pop("lng", None) or ctx.get("lng")
        if (
            (lat is None or lng is None)
            and address_id
            and address_id in self._address_coords
        ):
            lat, lng = self._address_coords[address_id]
        if lat is not None:
            args["lat"] = lat
        if lng is not None:
            args["lng"] = lng

        cart_id = kwargs.pop("cartId", None) or ctx.get("cartId")
        if cart_id:
            args["cartId"] = cart_id

        args.update(kwargs)
        res = await self._call_tool("confirm_order", args)

        returned_id = str(res.get("orderId") or order_id)
        result_status = (
            res.get("result") or res.get("orderStatus") or "CONFIRMED"
        ).upper()
        status = (
            OrderStatus.CONFIRMED
            if result_status in ("SUCCESS", "CONFIRMED", "PLACED")
            else OrderStatus.FAILED
        )

        total = ctx.get("totalAmount")
        if total is None:
            total = self._as_float(res.get("total")) or 0.0

        restaurant_name = str(
            ctx.get("restaurantName") or res.get("restaurantName") or ""
        )
        items_summary = str(ctx.get("itemsSummary") or res.get("itemsSummary") or "")

        return OrderSummary(
            order_id=returned_id,
            restaurant_name=restaurant_name,
            status=status,
            total_amount=total,
            items_summary=items_summary,
            raw=res,
        )

    @override
    async def get_orders(self, address_id: str | None = None) -> list[OrderSummary]:
        res = await self._call_tool(
            "get_food_orders",
            # The default is 5 orders; 15 is the documented maximum.
            {"addressId": self._address_id(address_id), "orderCount": 15},
        )
        orders_raw: list[dict[str, Any]] = (
            res.get("orders", []) if isinstance(res, dict) else res
        )
        summaries: list[OrderSummary] = []
        for o in orders_raw:
            if not isinstance(o, dict):
                continue
            order_id = str(o.get("orderId") or "")
            order_total = self._as_float(o.get("orderTotal") or o.get("total")) or 0.0
            items_summary = str(o.get("orderedItems") or o.get("itemsSummary") or "")
            created_at = o.get("orderedTime") or o.get("orderTime")

            raw_status = (
                o.get("orderStatus") or o.get("orderDeliveryStatus") or "PLACED"
            ).upper()
            if "DELIVER" in raw_status and "OUT" not in raw_status:
                status = OrderStatus.DELIVERED
            elif "CANCEL" in raw_status:
                status = OrderStatus.CANCELLED
            elif "OUT" in raw_status:
                status = OrderStatus.OUT_FOR_DELIVERY
            elif "PREPAR" in raw_status:
                status = OrderStatus.PREPARING
            elif "CONFIRM" in raw_status:
                status = OrderStatus.CONFIRMED
            else:
                status = OrderStatus.PLACED
            # isActiveOrder is the documented per-order flag; trust it over the text.
            if o.get("isActiveOrder") and status in (
                OrderStatus.DELIVERED,
                OrderStatus.CANCELLED,
            ):
                status = OrderStatus.OUT_FOR_DELIVERY

            summaries.append(
                OrderSummary(
                    order_id=order_id,
                    restaurant_name=str(o.get("restaurantName") or "Restaurant"),
                    status=status,
                    total_amount=order_total,
                    items_summary=items_summary,
                    created_at=created_at,
                    raw=o,
                )
            )
        return summaries

    @override
    async def track_order(self, order_id: str) -> OrderTracking:
        # track_food_order has the progress text; get_food_delivery_status adds the
        # terminal flags, an absolute ETA and the recommended polling interval.
        track_res, delivery_res = await asyncio.gather(
            self._call_tool("track_food_order", {"orderId": order_id}),
            self._call_tool("get_food_delivery_status", {"orderId": order_id}),
            return_exceptions=True,
        )
        if isinstance(track_res, BaseException):
            raise track_res
        res: dict[str, Any] = track_res if isinstance(track_res, dict) else {}
        delivery: dict[str, Any] = (
            delivery_res if isinstance(delivery_res, dict) else {}
        )

        order_data: dict[str, Any] = {}
        orders = res.get("orders")
        if isinstance(orders, list) and orders:
            # Never fall back to another order's tracking.
            order_data = next(
                (
                    o
                    for o in orders
                    if isinstance(o, dict) and str(o.get("orderId")) == str(order_id)
                ),
                {},
            )
        elif isinstance(res.get("order"), dict):
            order_data = res["order"]
        elif not isinstance(orders, list):
            order_data = res

        delivered = bool(delivery.get("delivered"))
        cancelled = bool(delivery.get("cancelled"))
        raw_status = order_data.get("orderStatus") or res.get("status")
        if not raw_status and not (delivered or cancelled):
            # Only active orders have live tracking; don't invent a status for the rest.
            raise LookupError(
                str(
                    res.get("statusMessage")
                    or delivery.get("statusText")
                    or f"No tracking information for order {order_id}"
                )
            )

        status_str = str(raw_status or "").upper()
        if delivered or "DELIVERED" in status_str:
            status = OrderStatus.DELIVERED
        elif cancelled or "CANCEL" in status_str:
            status = OrderStatus.CANCELLED
        elif "PREPAR" in status_str:
            status = OrderStatus.PREPARING
        elif "CONFIRM" in status_str:
            status = OrderStatus.CONFIRMED
        else:
            status = OrderStatus.OUT_FOR_DELIVERY

        eta_mins: int | None = None
        if status not in (OrderStatus.DELIVERED, OrderStatus.CANCELLED):
            delivery_by = self._as_float(delivery.get("deliveryBy"))
            server_now = self._as_float(delivery.get("serverNow"))
            if delivery_by is not None and server_now is not None:
                # Both are epoch milliseconds.
                eta_mins = max(0, round((delivery_by - server_now) / 60_000))
            else:
                eta_mins = self._eta_minutes(
                    delivery.get("etaText")
                    or order_data.get("etaText")
                    or res.get("eta")
                    or res.get("etaMinutes")
                )

        status_message = str(
            order_data.get("subtitle")
            or order_data.get("title")
            or delivery.get("statusText")
            or res.get("statusMessage")
            or ""
        )

        partner = res.get("deliveryPartner") or order_data.get("deliveryPartner") or {}
        partner_dict = partner if isinstance(partner, dict) else {}
        poll = self._as_float(delivery.get("pollIntervalSec"))

        return OrderTracking(
            order_id=order_id,
            status=status,
            eta_mins=eta_mins,
            status_message=status_message,
            delivery_partner_name=partner_dict.get("name"),
            delivery_partner_phone=partner_dict.get("phone"),
            poll_interval_secs=int(poll) if poll else None,
            raw={"tracking": res, "delivery_status": delivery},
        )

    def _eta_minutes(self, eta_raw: Any) -> int | None:  # pyright: ignore[reportAny, reportExplicitAny]
        if isinstance(eta_raw, str) and "min" in eta_raw.lower():
            # etaText is display text such as "35-40 mins"; use the upper bound.
            numbers = re.findall(r"\d+", eta_raw)
            return int(numbers[-1]) if numbers else None
        parsed = self._as_float(eta_raw)
        return int(parsed) if parsed is not None else None
