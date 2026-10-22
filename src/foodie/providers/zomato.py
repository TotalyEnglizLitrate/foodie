from __future__ import annotations

import base64
import hashlib
import json
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


class ZomatoProvider(BaseProvider):
    """
    Zomato implementation of BaseProvider interacting with Zomato's MCP server
    at https://mcp-server.zomato.com/mcp using OAuth 2.1 PKCE and D-Bus credential storage.
    """

    BASE_URL = "https://mcp-server.zomato.com"
    MCP_ENDPOINT = "https://mcp-server.zomato.com/mcp"
    SERVICE_KEY = "foodie-zomato"
    ACCOUNT_KEY = "tokens"
    CLIENT_INFO_KEY = "client_info"

    def __init__(self, store: SecretStore | None = None) -> None:
        self.store = store or SecretStore()
        self._http = httpx.AsyncClient(timeout=30.0)
        self._active_address_id: str | None = None
        self._cart_response: dict[str, Any] | None = None
        self._cart_payment_type: str | None = None
        self._cart_restaurant_id: int | None = None

    @property
    def name(self) -> ProviderName:
        return ProviderName.ZOMATO

    @property
    def display_name(self) -> str:
        return "Zomato"

    # --- Authentication Lifecycle ---

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
            label="Zomato MCP Session Tokens",
        )

    async def is_authenticated(self) -> bool:
        tokens = await self._get_tokens()
        if not tokens or "access_token" not in tokens:
            return False

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
        client_id, _ = await self._get_or_register_client(redirect_uri)

        # State is mandatory for Zomato authorization server
        state = secrets.token_urlsafe(16)
        params = {
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
            "state": state,
            "scope": "mcp:tools",
        }
        auth_url = f"{self.BASE_URL}/authorize?{urlencode(params)}"
        return auth_url, code_verifier

    async def _get_or_register_client(self, redirect_uri: str) -> tuple[str, str]:
        raw_info = await self.store.get_credential(self.SERVICE_KEY, self.CLIENT_INFO_KEY)
        if raw_info:
            try:
                info = json.loads(raw_info)
                if info.get("redirect_uri") == redirect_uri and "client_id" in info:
                    return str(info["client_id"]), str(info.get("client_secret", "Z-MCP"))
            except Exception:
                pass

        resp = await self._http.post(
            f"{self.BASE_URL}/register",
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
            client_secret = data.get("client_secret", "Z-MCP")
            await self.store.set_credential(
                self.SERVICE_KEY,
                self.CLIENT_INFO_KEY,
                json.dumps(
                    {
                        "client_id": client_id,
                        "client_secret": client_secret,
                        "redirect_uri": redirect_uri,
                    }
                ),
            )
            return client_id, client_secret

        return "foodie-client", "Z-MCP"

    async def complete_auth(self, code: str, verifier: str, redirect_uri: str) -> bool:
        client_id, client_secret = await self._get_or_register_client(redirect_uri)
        # Zomato /token follows OAuth 2.1 spec: application/x-www-form-urlencoded
        payload = {
            "grant_type": "authorization_code",
            "code": code,
            "code_verifier": verifier,
            "redirect_uri": redirect_uri,
            "client_id": client_id,
            "client_secret": client_secret,
        }
        resp = await self._http.post(f"{self.BASE_URL}/token", data=payload)
        if resp.status_code != 200:
            return False

        data = resp.json()
        expires_in = data.get("expires_in", 2592000)
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

        payload = {
            "grant_type": "refresh_token",
            "refresh_token": tokens["refresh_token"],
        }
        # Zomato /token follows OAuth 2.1 spec: application/x-www-form-urlencoded
        resp = await self._http.post(f"{self.BASE_URL}/token", data=payload)
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

    # --- Tool Call Bridge (JSON-RPC over Streamable HTTP) ---

    async def _call_tool(self, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        tokens = await self._get_tokens()
        if not tokens or not tokens.get("access_token"):
            raise PermissionError("Not authenticated with Zomato. Please login first.")

        access_token = tokens["access_token"]
        payload = {
            "jsonrpc": "2.0",
            "id": time.time_ns() // 1000,
            "method": "tools/call",
            "params": {"name": tool_name, "arguments": arguments},
        }

        headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }

        resp = await self._http.post(self.MCP_ENDPOINT, json=payload, headers=headers)
        if resp.status_code == 401:
            refreshed = await self.refresh_auth()
            if refreshed:
                tokens = await self._get_tokens()
                headers["Authorization"] = f"Bearer {tokens['access_token']}"
                resp = await self._http.post(self.MCP_ENDPOINT, json=payload, headers=headers)
            else:
                raise PermissionError("Zomato session expired. Please re-authenticate.")

        if resp.status_code >= 400:
            raise RuntimeError(f"Zomato tool call failed ({resp.status_code}): {resp.text}")

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
                raise RuntimeError(f"Zomato returned empty SSE response: {resp.text}")
        else:
            res_json = resp.json()

        if "error" in res_json:
            err = res_json["error"]
            raise RuntimeError(f"Zomato tool error: {err.get('message', err)}")

        result = res_json.get("result", {})
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
        res = await self._call_tool("get_saved_addresses_for_user", {})
        addresses_raw = res.get("addresses", []) if isinstance(res, dict) else res
        addresses: list[Address] = []
        for a in addresses_raw:
            addresses.append(
                Address(
                    id=str(a.get("address_id") or a.get("id")),
                    label=a.get("type") or a.get("label", "Address"),
                    formatted_address=a.get("address_line1") or a.get("address", ""),
                    latitude=float(a.get("latitude")) if a.get("latitude") else None,
                    longitude=float(a.get("longitude")) if a.get("longitude") else None,
                    raw=a,
                )
            )
        return addresses

    # --- Discovery & Menu ---

    async def search_restaurants(self, query: str, address_id: str) -> list[Restaurant]:
        self._active_address_id = address_id
        args = {"keyword": query, "address_id": address_id}
        res = await self._call_tool("get_restaurants_for_keyword", args)
        restaurants_raw = (
            res.get("restaurants") or res.get("results", [])
            if isinstance(res, dict)
            else res
        )
        restaurants: list[Restaurant] = []
        for r in restaurants_raw:
            rating_val = None
            if "rating" in r:
                try:
                    rating_val = float(r["rating"])
                except (ValueError, TypeError):
                    pass

            restaurants.append(
                Restaurant(
                    id=str(r.get("res_id") or r.get("id")),
                    name=r.get("name", "Unknown Restaurant"),
                    cuisines=r.get("cuisines", "").split(", ") if isinstance(r.get("cuisines"), str) else r.get("cuisines", []),
                    rating=rating_val,
                    delivery_time_mins=r.get("delivery_time") or r.get("eta"),
                    distance_km=float(r.get("distance")) if r.get("distance") is not None else None,
                    cost_for_two=float(r.get("cost_for_two")) if r.get("cost_for_two") else None,
                    is_open=bool(r.get("is_open", True)),
                    image_url=r.get("image_url") or r.get("thumb"),
                    raw=r,
                )
            )
        return restaurants

    def _address_id(self, address_id: str | None = None) -> str:
        if address_id:
            self._active_address_id = address_id
        if not self._active_address_id:
            raise ValueError("An address_id is required for this Zomato operation.")
        return self._active_address_id

    @staticmethod
    def _restaurant_id(restaurant_id: str) -> int:
        try:
            return int(restaurant_id)
        except (TypeError, ValueError) as exc:
            raise ValueError("Zomato restaurant IDs must be numeric.") from exc

    @staticmethod
    def _categories(res: dict[str, Any]) -> list[str]:
        """Extract category names from the lightweight menu-listing response."""
        candidates = res.get("categories") or res.get("menu_categories") or []
        if isinstance(candidates, dict):
            candidates = list(candidates)
        if not candidates:
            items = res.get("items") or res.get("menu_items") or []
            if isinstance(items, dict):
                candidates = list(items.values())
            else:
                candidates = [
                    item.get("category") or item.get("category_name")
                    for item in items
                    if isinstance(item, dict)
                ]
        return list(dict.fromkeys(
            str(category.get("name") or category.get("category_name") or category.get("id"))
            if isinstance(category, dict) else str(category)
            for category in candidates
            if category
        ))

    async def get_menu(
        self, restaurant_id: str, address_id: str | None = None
    ) -> list[MenuItem]:
        res_id = self._restaurant_id(restaurant_id)
        resolved_address_id = self._address_id(address_id)
        listing = await self._call_tool(
            "get_menu_items_listing",
            {"res_id": res_id, "address_id": resolved_address_id},
        )
        categories = self._categories(listing)
        if not categories:
            return []
        res = await self._call_tool(
            "get_restaurant_menu_by_categories",
            {
                "res_id": res_id,
                "categories": categories,
                "address_id": resolved_address_id,
            },
        )
        items_raw = res.get("items", []) if isinstance(res, dict) else res
        items: list[MenuItem] = []
        for it in items_raw:
            items.append(
                MenuItem(
                    id=str(it.get("item_id") or it.get("id")),
                    name=it.get("name", ""),
                    price=float(it.get("price", 0)),
                    description=it.get("description", ""),
                    is_veg=bool(it.get("is_veg", True)),
                    in_stock=bool(it.get("in_stock", True)),
                    category=it.get("category_name", ""),
                    variants=it.get("variants", []),
                    addons=it.get("addons", []),
                    raw=it,
                )
            )
        return items

    async def search_menu(
        self, restaurant_id: str, query: str, address_id: str | None = None
    ) -> list[MenuItem]:
        # Filter items matching query
        all_items = await self.get_menu(restaurant_id, address_id)
        q = query.lower()
        return [item for item in all_items if q in item.name.lower() or q in item.description.lower()]

    # --- Cart Operations ---

    async def get_cart(self, address_id: str | None = None) -> Cart:
        # Zomato exposes no read-cart tool; create_cart returns the authoritative
        # cart payload, so retain that response for the current provider session.
        self._address_id(address_id)
        if self._cart_response is None:
            raise RuntimeError("Zomato has no cart-read endpoint; create a cart first.")
        return self._parse_cart(self._cart_response)

    async def update_cart(
        self,
        restaurant_id: str,
        items: list[CartItem],
        address_id: str,
        payment_method: str | None = None,
    ) -> Cart:
        payload_items = [
            {
                "variant_id": it.item_id,
                "quantity": it.quantity,
                "add_ons": [
                    {
                        "id": str(addon.get("id") or addon.get("choice_id")),
                        "quantity": int(addon.get("quantity", 1)),
                    }
                    for addon in it.selected_addons
                    if addon.get("id") or addon.get("choice_id")
                ]
                or None,
            }
            for it in items
        ]
        payment_type = self._payment_type(payment_method)
        res_id = self._restaurant_id(restaurant_id)
        args = {
            "res_id": res_id,
            "address_id": self._address_id(address_id),
            "items": payload_items,
            "payment_type": payment_type,
        }
        res = await self._call_tool("create_cart", args)
        self._cart_response = res
        self._cart_payment_type = payment_type
        self._cart_restaurant_id = res_id
        return self._parse_cart(res)

    async def clear_cart(self) -> None:
        if not self._cart_response or not self._cart_payment_type:
            raise RuntimeError("Zomato has no cart to clear in this provider session.")
        res_id = (
            self._cart_response.get("res_id")
            or self._cart_response.get("restaurant_id")
            or self._cart_restaurant_id
        )
        if res_id is None:
            raise RuntimeError("Zomato cart response did not include a restaurant ID.")
        self._cart_response = await self._call_tool(
            "create_cart",
            {
                "res_id": self._restaurant_id(str(res_id)),
                "items": [],
                "address_id": self._address_id(),
                "payment_type": self._cart_payment_type,
            },
        )

    async def apply_coupon(self, code: str, address_id: str | None = None) -> Cart:
        raise NotImplementedError("Zomato MCP does not expose a coupon-application tool.")

    def _payment_type(self, payment_method: str | None) -> str:
        normalized = (payment_method or "").lower()
        aliases = {
            "upi": "upi",
            "upi_qr": "upi",
            "cash": "cash_on_delivery",
            "cod": "cash_on_delivery",
            "cash_on_delivery": "cash_on_delivery",
        }
        if normalized not in aliases:
            raise ValueError("Zomato cart requires payment_method 'upi' or 'cash_on_delivery'.")
        return aliases[normalized]

    def _parse_cart(self, res: dict[str, Any]) -> Cart:
        res_id = str(res.get("res_id", ""))
        items_raw = res.get("items", [])
        parsed_items: list[CartItem] = []
        for it in items_raw:
            parsed_items.append(
                CartItem(
                    item_id=str(it.get("item_id") or it.get("id")),
                    name=it.get("name", ""),
                    price=float(it.get("price", 0)),
                    quantity=int(it.get("quantity", 1)),
                )
            )

        bill = res.get("bill", {}) or res
        return Cart(
            restaurant_id=res_id,
            items=parsed_items,
            item_total=float(bill.get("item_total", 0)),
            delivery_fee=float(bill.get("delivery_fee", 0)),
            taxes=float(bill.get("taxes", 0)),
            discount=float(bill.get("discount", 0)),
            grand_total=float(bill.get("grand_total") or bill.get("total", 0)),
            raw=res,
        )

    # --- Checkout & Payments ---

    async def get_payment_options(self, address_id: str) -> list[PaymentMethod]:
        self._active_address_id = address_id
        # Zomato MCP checkout uses UPI QR or online link
        return [
            PaymentMethod(id="UPI_QR", name="UPI QR Code (Scan to pay)", method_type="UPI_QR"),
            PaymentMethod(id="COD", name="Cash on Delivery", method_type="COD"),
        ]

    async def place_order(
        self, payment_method: str, address_id: str, **kwargs: Any
    ) -> OrderPlacement:
        self._active_address_id = address_id
        cart_id = kwargs.pop("cart_id", None)
        if not cart_id and self._cart_response:
            cart_id = self._cart_response.get("cart_id") or self._cart_response.get("cartId")
        if not cart_id:
            raise ValueError("Zomato checkout requires a cart_id from create_cart.")
        args = {"cart_id": str(cart_id), "payment_method_type": self._payment_type(payment_method)}
        res = await self._call_tool("checkout_cart", args)

        order_id = str(res.get("order_id", ""))
        requires_payment = bool(res.get("payment_qr") or res.get("payment_url") or res.get("upi_intent"))

        return OrderPlacement(
            order_id=order_id,
            status=OrderStatus.PENDING_PAYMENT if requires_payment else OrderStatus.PLACED,
            requires_payment=requires_payment,
            upi_qr_string=res.get("payment_qr") or res.get("qr_data"),
            payment_url=res.get("payment_url"),
            raw=res,
        )

    async def check_payment_status(self, order_id: str, **kwargs: Any) -> dict[str, Any]:
        return await self._call_tool("get_order_tracking_info", {"order_id": order_id})

    async def confirm_order(self, order_id: str, **kwargs: Any) -> OrderSummary:
        res = await self._call_tool("get_order_tracking_info", {"order_id": order_id})
        return OrderSummary(
            order_id=order_id,
            restaurant_name=res.get("restaurant_name", ""),
            status=OrderStatus.CONFIRMED,
            total_amount=float(res.get("total_amount", 0)),
            raw=res,
        )

    # --- History & Live Tracking ---

    async def get_orders(self, address_id: str | None = None) -> list[OrderSummary]:
        res = await self._call_tool(
            "get_order_history", {"address_id": self._address_id(address_id)}
        )
        orders_raw = res.get("orders", []) if isinstance(res, dict) else res
        summaries: list[OrderSummary] = []
        for o in orders_raw:
            summaries.append(
                OrderSummary(
                    order_id=str(o.get("order_id", "")),
                    restaurant_name=o.get("restaurant_name", "Restaurant"),
                    status=OrderStatus.PLACED,
                    total_amount=float(o.get("total", 0)),
                    items_summary=o.get("items_summary", ""),
                    created_at=o.get("order_time"),
                    raw=o,
                )
            )
        return summaries

    async def track_order(self, order_id: str) -> OrderTracking:
        res = await self._call_tool("get_order_tracking_info", {"order_id": order_id})
        rider = res.get("delivery_partner", {}) or {}
        return OrderTracking(
            order_id=order_id,
            status=OrderStatus.OUT_FOR_DELIVERY,
            eta_mins=res.get("eta_minutes"),
            status_message=res.get("status_text", ""),
            delivery_partner_name=rider.get("name"),
            delivery_partner_phone=rider.get("phone"),
            raw=res,
        )
