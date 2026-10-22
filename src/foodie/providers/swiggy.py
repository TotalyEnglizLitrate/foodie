from __future__ import annotations

import base64
import hashlib
import json
import re
import secrets
import time
from typing import Any
from urllib.parse import urlencode

import httpx

from foodie.keyring import SecretStore
from foodie.models import (
    Address,
    Cart,
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


class SwiggyProvider(BaseProvider):
    """
    Swiggy implementation of BaseProvider interacting with Swiggy's MCP endpoints
    over Streamable HTTP / JSON-RPC with OAuth 2.1 PKCE session management.
    """

    BASE_URL = "https://mcp.swiggy.com"
    SERVICE_KEY = "foodie-swiggy"
    ACCOUNT_KEY = "tokens"
    CLIENT_INFO_KEY = "client_info"
    MENU_PAGE_SIZE = 100
    MAX_MENU_PAGES = 100

    def __init__(self, store: SecretStore | None = None) -> None:
        self.store = store or SecretStore()
        self._http = httpx.AsyncClient(timeout=30.0)
        self._active_address_id: str | None = None

    @property
    def name(self) -> ProviderName:
        return ProviderName.SWIGGY

    @property
    def display_name(self) -> str:
        return "Swiggy"

    # --- Authentication ---

    async def _get_tokens(self) -> dict[str, Any] | None:
        raw = await self.store.get_credential(self.SERVICE_KEY, self.ACCOUNT_KEY)
        if not raw:
            return None
        try:
            return json.loads(raw)
        except Exception:
            return None

    async def _save_tokens(self, tokens: dict[str, Any]) -> None:
        await self.store.set_credential(
            self.SERVICE_KEY,
            self.ACCOUNT_KEY,
            json.dumps(tokens),
            label="Swiggy MCP Session Tokens",
        )

    async def is_authenticated(self) -> bool:
        tokens = await self._get_tokens()
        if not tokens or "access_token" not in tokens:
            return False

        # If expired or near expiration (within 60 seconds), attempt refresh
        expires_at = tokens.get("expires_at", 0)
        if time.time() >= expires_at - 60:
            return await self.refresh_auth()

        return True

    async def get_auth_url(self, redirect_uri: str) -> tuple[str, str]:
        # Generate PKCE verifier + challenge (S256)
        verifier_bytes = secrets.token_bytes(32)
        code_verifier = base64.urlsafe_b64encode(verifier_bytes).decode("utf-8").rstrip("=")
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
        raw_info = await self.store.get_credential(self.SERVICE_KEY, self.CLIENT_INFO_KEY)
        if raw_info:
            try:
                info = json.loads(raw_info)
                if info.get("redirect_uri") == redirect_uri and "client_id" in info:
                    return str(info["client_id"])
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
            data = resp.json()
            client_id = data.get("client_id", "foodie-client")
            await self.store.set_credential(
                self.SERVICE_KEY,
                self.CLIENT_INFO_KEY,
                json.dumps({"client_id": client_id, "redirect_uri": redirect_uri}),
            )
            return client_id

        # Fallback client identifier if DCR response doesn't return one
        return "foodie-client"

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

        data = resp.json()
        expires_in = data.get("expires_in", 432000)
        tokens = {
            "access_token": data.get("access_token"),
            "refresh_token": data.get("refresh_token"),
            "expires_at": time.time() + expires_in,
        }
        await self._save_tokens(tokens)
        return True

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

        data = resp.json()
        expires_in = data.get("expires_in", 2592000)
        tokens["access_token"] = data.get("access_token")
        if "refresh_token" in data:
            tokens["refresh_token"] = data.get("refresh_token")
        tokens["expires_at"] = time.time() + expires_in
        await self._save_tokens(tokens)
        return True

    async def logout(self) -> None:
        await self.store.delete_credential(self.SERVICE_KEY, self.ACCOUNT_KEY)

    # --- Tool Call Bridge (JSON-RPC) ---

    async def _call_tool(
        self, tool_name: str, arguments: dict[str, Any], server: str = "food"
    ) -> dict[str, Any]:
        tokens = await self._get_tokens()
        if not tokens or not tokens.get("access_token"):
            raise PermissionError("Not authenticated with Swiggy. Please login first.")

        access_token = tokens["access_token"]
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
                headers["Authorization"] = f"Bearer {tokens['access_token']}"
                resp = await self._http.post(url, json=payload, headers=headers)
            else:
                raise PermissionError("Swiggy session expired. Please re-authenticate.")

        if resp.status_code >= 400:
            raise RuntimeError(f"Swiggy tool call failed ({resp.status_code}): {resp.text}")

        content_type = resp.headers.get("content-type", "")
        if "text/event-stream" in content_type:
            res_json: dict[str, Any] = {}
            for line in resp.text.splitlines():
                if line.startswith("data:"):
                    raw_data = line[5:].strip()
                    if raw_data:
                        try:
                            res_json = json.loads(raw_data)
                            break
                        except Exception:
                            continue
            if not res_json:
                raise RuntimeError(f"Swiggy returned empty SSE response: {resp.text}")
        else:
            res_json = resp.json()

        if "error" in res_json:
            err = res_json["error"]
            raise RuntimeError(f"Swiggy tool error: {err.get('message', err)}")

        result = res_json.get("result", {})
        # Swiggy MCP returns structured data in result.structuredContent
        if isinstance(result, dict) and "structuredContent" in result:
            return result["structuredContent"]
        # Fallback: parse JSON from the text content block
        if isinstance(result, dict) and "content" in result:
            for c in result.get("content", []):
                if c.get("type") == "text":
                    try:
                        parsed = json.loads(c.get("text", ""))
                        if isinstance(parsed, dict) and "data" in parsed:
                            return parsed["data"]
                        return parsed
                    except Exception:
                        pass
        if isinstance(result, dict) and "data" in result:
            return result["data"]
        return result

    # --- Addresses ---

    async def get_addresses(self) -> list[Address]:
        res = await self._call_tool("get_addresses", {})
        addresses_raw = res.get("addresses", []) if isinstance(res, dict) else res
        addresses: list[Address] = []
        for a in addresses_raw:
            addresses.append(
                Address(
                    id=str(a.get("id") or a.get("addressId")),
                    label=a.get("addressTag") or a.get("addressCategory") or a.get("label", "Address"),
                    formatted_address=a.get("addressLine") or a.get("address") or a.get("formattedAddress", ""),
                    latitude=a.get("lat") or a.get("latitude"),
                    longitude=a.get("lng") or a.get("longitude"),
                    raw=a,
                )
            )
        return addresses

    # --- Discovery & Menu ---

    @staticmethod
    def _as_float(value: Any) -> float | None:
        """Accept MCP numeric fields that may be returned as display text."""
        if value is None:
            return None
        if isinstance(value, (int, float)):
            return float(value)
        match = re.search(r"\d+(?:\.\d+)?", str(value).replace(",", ""))
        return float(match.group()) if match else None

    async def search_restaurants(self, query: str, address_id: str) -> list[Restaurant]:
        if not query.strip():
            raise ValueError("Swiggy restaurant searches require a non-empty query.")
        self._active_address_id = address_id
        res = await self._call_tool(
            "search_restaurants", {"query": query, "addressId": address_id}
        )
        restaurants_raw = res.get("restaurants", []) if isinstance(res, dict) else res
        restaurants: list[Restaurant] = []
        for r in restaurants_raw:
            sla = r.get("sla", {})
            delivery_time = sla.get("deliveryTime") if isinstance(sla, dict) else None
            distance = sla.get("lastMileTravel") if isinstance(sla, dict) else None

            restaurants.append(
                Restaurant(
                    id=str(r.get("id") or r.get("restaurantId")),
                    name=r.get("name", "Unknown Restaurant"),
                    cuisines=r.get("cuisines", []),
                    rating=float(r.get("avgRating") or r.get("rating", 0.0)) or None,
                    delivery_time_mins=delivery_time,
                    distance_km=self._as_float(distance),
                    cost_for_two=self._as_float(r.get("costForTwo")),
                    is_open=r.get("availabilityStatus", "OPEN").upper() == "OPEN",
                    image_url=r.get("cloudinaryImageId"),
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
        return flattened

    @staticmethod
    def _has_more_menu_items(res: dict[str, Any]) -> bool:
        """Return whether any category advertises a following menu page."""
        return any(
            isinstance(category, dict) and category.get("hasMoreItems", False)
            for category in res.get("categories", [])
        )

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
                item_id = str(item.get("id") or item.get("itemId") or "")
                # "Recommended" repeats items found in their actual category.
                # Keep an ID-less item rather than silently dropping it.
                if item_id and item_id in seen_ids:
                    continue
                if item_id:
                    seen_ids.add(item_id)
                items_raw.append(item)
                new_items += 1

            if not isinstance(res, dict) or not self._has_more_menu_items(res):
                break
            # Protect against an endpoint that ignores page and repeats page 1.
            if not page_items or not new_items:
                break

        items: list[MenuItem] = []
        for item in items_raw:
            price = float(item.get("price", 0))
            items.append(
                MenuItem(
                    id=str(item.get("id") or item.get("itemId")),
                    name=item.get("name", ""),
                    price=price,
                    description=item.get("description", ""),
                    is_veg=bool(item.get("isVeg", True)),
                    in_stock=bool(item.get("inStock", True)),
                    category=item.get("category", ""),
                    variants=item.get("variants", []),
                    addons=item.get("addons", []),
                    raw=item,
                )
            )
        return items

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
                    id=str(item.get("id") or item.get("itemId")),
                    name=item.get("name", ""),
                    price=price,
                    description=item.get("description", ""),
                    is_veg=bool(item.get("isVeg", True)),
                    in_stock=bool(item.get("inStock", True)),
                    category=item.get("category", ""),
                    variants=item.get("variants", []),
                    addons=item.get("addons", []),
                    raw=item,
                )
            )
        return items

    # --- Cart ---

    async def get_cart(self, address_id: str | None = None) -> Cart:
        res = await self._call_tool(
            "get_food_cart", {"addressId": self._address_id(address_id)}
        )
        return self._parse_cart(res)

    async def update_cart(
        self,
        restaurant_id: str,
        items: list[CartItem],
        address_id: str,
        payment_method: str | None = None,
    ) -> Cart:
        payload_items = [
            {
                "menu_item_id": it.item_id,
                "quantity": it.quantity,
                "variants": it.selected_variants,
                "addons": it.selected_addons,
            }
            for it in items
        ]
        res = await self._call_tool(
            "update_food_cart",
            {
                "restaurantId": restaurant_id,
                "cartItems": payload_items,
                "addressId": self._address_id(address_id),
            },
        )
        return self._parse_cart(res)

    async def clear_cart(self) -> None:
        await self._call_tool("flush_food_cart", {})

    async def apply_coupon(self, code: str, address_id: str | None = None) -> Cart:
        res = await self._call_tool(
            "apply_food_coupon",
            {"couponCode": code, "addressId": self._address_id(address_id)},
        )
        return self._parse_cart(res)

    def _parse_cart(self, res: dict[str, Any]) -> Cart:
        cart_data = res.get("data", res)
        restaurant_id = str(
            cart_data.get("restaurantId")
            or cart_data.get("restaurant_id")
            or ""
        )
        items_raw = cart_data.get("items", [])
        parsed_items: list[CartItem] = []
        for it in items_raw:
            parsed_items.append(
                CartItem(
                    item_id=str(it.get("menu_item_id") or it.get("itemId") or it.get("id")),
                    name=it.get("name", ""),
                    price=float(it.get("final_price") or it.get("price") or 0),
                    quantity=int(it.get("quantity", 1)),
                    selected_variants=it.get("variants", []),
                    selected_addons=it.get("addons", []),
                )
            )

        bill = cart_data.get("pricing") or cart_data.get("bill") or cart_data
        offers = cart_data.get("offers", {}) or {}
        return Cart(
            restaurant_id=restaurant_id,
            items=parsed_items,
            item_total=float(bill.get("item_total") or bill.get("itemTotal") or 0),
            delivery_fee=float(bill.get("delivery_charge") or bill.get("deliveryFee") or 0),
            taxes=float(bill.get("taxes_and_charges") or bill.get("taxes") or 0),
            discount=float(offers.get("coupon_discount") or bill.get("discount") or 0),
            grand_total=float(
                bill.get("to_pay") or bill.get("grandTotal") or bill.get("total") or 0
            ),
            raw=cart_data,
        )

    # --- Checkout & Payments ---

    async def get_payment_options(self, address_id: str) -> list[PaymentMethod]:
        self._active_address_id = address_id
        res = await self._call_tool("get_payment_options", {"addressId": address_id})
        options: list[PaymentMethod] = []

        # Check COD
        if res.get("cod") or res.get("codAvailable"):
            options.append(PaymentMethod(id="COD", name="Cash on Delivery", method_type="COD"))

        # Platforms (desktop / mobile UPI)
        platforms = res.get("platforms", {})
        desktop = platforms.get("desktop", {})
        for m in desktop.get("methods", []):
            options.append(
                PaymentMethod(
                    id=m.get("id", "UPI_QR"),
                    name=m.get("name", "UPI Scan & Pay"),
                    method_type="UPI_QR",
                    details=m,
                )
            )

        mobile = platforms.get("mobile", {})
        for m in mobile.get("methods", []):
            options.append(
                PaymentMethod(
                    id=m.get("id", "UPI_INTENT"),
                    name=m.get("name", "UPI App"),
                    method_type="UPI_INTENT",
                    details=m,
                )
            )

        return options

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

        return OrderPlacement(
            order_id=order_id,
            status=OrderStatus.PENDING_PAYMENT if requires_payment else OrderStatus.PLACED,
            requires_payment=requires_payment,
            upi_qr_string=res.get("upiQrString") or res.get("qrData"),
            payment_url=res.get("paymentUrl"),
            max_poll_time_ms=res.get("maxTimeToPollForInMs", 180000),
            poll_interval_ms=res.get("pollingIntervalInMs", 5000),
            raw=res,
        )

    async def check_payment_status(self, order_id: str, **kwargs: Any) -> dict[str, Any]:
        paas_id = kwargs.pop("paasId", None) or kwargs.pop("paas_id", None)
        if not paas_id:
            raise ValueError("Swiggy payment-status checks require the checkout paasId.")
        args = {"paasId": paas_id, "orderId": order_id, **kwargs}
        return await self._call_tool("check_payment_status", args)

    async def confirm_order(self, order_id: str, **kwargs: Any) -> OrderSummary:
        res = await self._call_tool("confirm_order", {"orderId": order_id, **kwargs})
        return OrderSummary(
            order_id=str(res.get("orderId", order_id)),
            restaurant_name=res.get("restaurantName", ""),
            status=OrderStatus.CONFIRMED,
            total_amount=float(res.get("total", 0)),
            items_summary=res.get("itemsSummary", ""),
            raw=res,
        )

    # --- Orders & Tracking ---

    async def get_orders(self, address_id: str | None = None) -> list[OrderSummary]:
        res = await self._call_tool(
            "get_food_orders", {"addressId": self._address_id(address_id)}
        )
        orders_raw = res.get("orders", []) if isinstance(res, dict) else res
        summaries: list[OrderSummary] = []
        for o in orders_raw:
            summaries.append(
                OrderSummary(
                    order_id=str(o.get("orderId", "")),
                    restaurant_name=o.get("restaurantName", "Restaurant"),
                    status=OrderStatus.PLACED,
                    total_amount=float(o.get("total", 0)),
                    items_summary=o.get("itemsSummary", ""),
                    created_at=o.get("orderTime"),
                    raw=o,
                )
            )
        return summaries

    async def track_order(self, order_id: str) -> OrderTracking:
        res = await self._call_tool("track_food_order", {"orderId": order_id})
        partner = res.get("deliveryPartner", {}) or {}
        return OrderTracking(
            order_id=order_id,
            status=OrderStatus.OUT_FOR_DELIVERY,
            eta_mins=res.get("eta") or res.get("etaMinutes"),
            status_message=res.get("statusMessage", ""),
            delivery_partner_name=partner.get("name"),
            delivery_partner_phone=partner.get("phone"),
            raw=res,
        )
