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


class ZomatoToolError(RuntimeError):
    """A Zomato MCP tool answered with an application-level error payload.

    Zomato tools report most failures as a *successful* JSON-RPC response whose
    body is ``{"error_message", "error_code", "success": false}`` (or, for
    ``create_cart``/``checkout_cart``, ``{"success": false, "message": ...}``).
    """

    def __init__(self, message: str, code: str | None = None) -> None:
        super().__init__(f"Zomato tool error{f' [{code}]' if code else ''}: {message}")
        self.code: str | None = code
        self.message: str = message


def _num(
    value: Any,
    default: float = 0.0,  # pyright: ignore[reportAny, reportExplicitAny]
) -> float:
    """Coerce numbers / numeric strings (e.g. '₹1,250.50') to float."""
    if value is None or isinstance(value, bool):
        return default
    if isinstance(value, (int, float)):
        return float(value)
    match = re.search(r"-?\d[\d,]*(?:\.\d+)?", str(value))
    return float(match.group().replace(",", "")) if match else default


def _int_or_none(
    value: Any,  # pyright: ignore[reportAny, reportExplicitAny]
) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    match = re.search(r"\d+", str(value))
    return int(match.group()) if match else None


def _flag(
    value: Any,
    default: bool,  # pyright: ignore[reportAny, reportExplicitAny]
) -> bool:
    """Schema booleans are frequently nullable; null means 'unknown', not False."""
    return default if value is None else bool(value)


def _status_from_text(
    text: str | None, *, paid: bool | None = None, placed: bool | None = None
) -> OrderStatus:
    """Map Zomato's free-form ``order_status`` string onto OrderStatus."""
    t = (text or "").lower()
    if "cancel" in t or "reject" in t:
        return OrderStatus.CANCELLED
    if "fail" in t:
        return OrderStatus.FAILED
    if "deliver" in t and not any(
        w in t for w in ("out", "way", "pick", "being", "partner")
    ):
        return OrderStatus.DELIVERED
    if any(
        w in t
        for w in (
            "out for",
            "on the way",
            "on its way",
            "being deliver",
            "picked",
            "arriv",
        )
    ):
        return OrderStatus.OUT_FOR_DELIVERY
    if any(w in t for w in ("prepar", "cook", "ready")):
        return OrderStatus.PREPARING
    if "confirm" in t or "accept" in t:
        return OrderStatus.CONFIRMED
    if "pending" in t or (paid is False and placed is False):
        return OrderStatus.PENDING_PAYMENT
    return OrderStatus.PLACED


class ZomatoProvider(BaseProvider):
    """
    Zomato implementation of BaseProvider interacting with Zomato's MCP server
    at https://mcp-server.zomato.com/mcp using OAuth 2.1 PKCE and D-Bus credential storage.
    """

    BASE_URL: str = "https://mcp-server.zomato.com"
    MCP_ENDPOINT: str = "https://mcp-server.zomato.com/mcp"
    SERVICE_KEY: str = "foodie"
    ACCOUNT_KEY: str = "tokens"
    CLIENT_INFO_KEY: str = "client_info"

    def __init__(self, store: SecretStore | None = None) -> None:
        self.store: SecretStore = store or SecretStore()
        self._http: httpx.AsyncClient = httpx.AsyncClient(timeout=30.0)
        self._active_address_id: str | None = None
        self._cart_response: dict[str, Any] | None = None  # pyright: ignore[reportExplicitAny]
        self._cart_payment_type: str | None = None
        self._cart_restaurant_id: int | None = None
        self._order_totals: dict[str, float] = {}

    @property
    @override
    def name(self) -> ProviderName:
        return ProviderName.ZOMATO

    @property
    @override
    def display_name(self) -> str:
        return "Zomato"

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

    async def _save_tokens(self, tokens: dict[str, Any]) -> None:  # pyright: ignore[reportExplicitAny]
        await self.store.set_credential(
            self.SERVICE_KEY,
            self.ACCOUNT_KEY,
            json.dumps(tokens),
            self.name,
        )

    @override
    async def is_authenticated(self) -> bool:
        tokens = await self._get_tokens()
        if not tokens or "access_token" not in tokens:
            return False

        expires_at = cast(int, tokens.get("expires_at", 0))
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
        raw_info = await self.store.get_credential(
            self.SERVICE_KEY, self.CLIENT_INFO_KEY, self.name
        )
        if raw_info:
            try:
                info = json.loads(raw_info)
                if info.get("redirect_uri") == redirect_uri and "client_id" in info:
                    return str(info["client_id"]), str(
                        info.get("client_secret", "Z-MCP")
                    )
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
                self.name,
            )
            return client_id, client_secret

        return "foodie-client", "Z-MCP"

    @override
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

    @override
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

    @override
    async def logout(self) -> None:
        await self.store.delete_credential(
            self.SERVICE_KEY, self.ACCOUNT_KEY, self.name
        )

    # --- Tool Call Bridge (JSON-RPC over Streamable HTTP) ---

    async def _call_tool(
        self, tool_name: str, arguments: dict[str, Any]
    ) -> dict[str, Any]:
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
                assert tokens is not None
                headers["Authorization"] = f"Bearer {tokens['access_token']}"
                resp = await self._http.post(
                    self.MCP_ENDPOINT, json=payload, headers=headers
                )
            else:
                raise PermissionError("Zomato session expired. Please re-authenticate.")

        if resp.status_code >= 400:
            raise RuntimeError(
                f"Zomato tool call failed ({resp.status_code}): {resp.text}"
            )

        content_type = resp.headers.get("content-type", "")
        if "text/event-stream" in content_type:
            res_json = self._parse_sse(resp.text, payload["id"])
        else:
            res_json = resp.json()

        if not isinstance(res_json, dict):
            raise RuntimeError(
                f"Zomato returned a malformed response for {tool_name}: {res_json!r}"
            )

        if "error" in res_json:
            err = res_json["error"]
            msg = err.get("message", err) if isinstance(err, dict) else err
            raise RuntimeError(f"Zomato tool error: {msg}")

        return self._unwrap_result(tool_name, res_json.get("result", {}))

    @staticmethod
    def _parse_sse(body: str, request_id: Any) -> dict[str, Any]:
        """Pick the JSON-RPC response for ``request_id`` out of an SSE stream.

        The stream may carry notifications before the actual response, so the
        first ``data:`` line is not necessarily the one we want.
        """
        fallback: dict[str, Any] | None = None
        for line in body.splitlines():
            if not line.startswith("data:"):
                continue
            raw = line[5:].strip()
            if not raw:
                continue
            try:
                msg = json.loads(raw)
            except ValueError:
                continue
            if not isinstance(msg, dict):
                continue
            if msg.get("id") == request_id:
                return msg
            if fallback is None and ("result" in msg or "error" in msg):
                fallback = msg
        if fallback is None:
            raise RuntimeError(f"Zomato returned empty SSE response: {body}")
        return fallback

    @staticmethod
    def _unwrap_result(tool_name: str, result: Any) -> dict[str, Any]:
        """Turn an MCP ``tools/call`` result into the tool's payload dict.

        Contract notes (see scripts/schemas/zomato):
        * Tools whose return type is not an object are wrapped by FastMCP as
          ``{"result": <payload>}`` in ``structuredContent``; the text block
          carries the bare payload. Both forms are accepted.
        * Query tools wrap their payload as ``{"data": ..., "success": ...}``.
        * Any tool may answer ``{"error_message", "error_code", "success": false}``.
        """
        if not isinstance(result, dict):
            return result

        text_parts = [
            c.get("text", "")
            for c in result.get("content") or []
            if isinstance(c, dict) and c.get("type") == "text"
        ]

        if result.get("isError"):
            raise ZomatoToolError(
                "\n".join(text_parts).strip() or f"{tool_name} failed"
            )

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

        if isinstance(payload, dict) and set(payload) == {"result"}:
            payload = payload["result"]

        if isinstance(payload, dict):
            if payload.get("error_code") or (
                payload.get("success") is False and payload.get("error_message")
            ):
                raise ZomatoToolError(
                    str(
                        payload.get("error_message")
                        or payload.get("message")
                        or "unknown error"
                    ),
                    payload.get("error_code"),
                )
            if isinstance(payload.get("data"), dict):
                return payload["data"]
        return payload

    @override
    async def get_addresses(self) -> list[Address]:
        # Contract: {"addresses": [{"address_id": str | null, "location_name": str}]}.
        # Coordinates and structured lines are not part of this tool's output.
        res = await self._call_tool("get_saved_addresses_for_user", {})
        addresses_raw = (
            res.get("addresses") or [] if isinstance(res, dict) else res or []
        )
        addresses: list[Address] = []
        for a in addresses_raw:
            if not isinstance(a, dict) or not a.get("address_id"):
                # An address without an id can't be passed to any other tool.
                continue
            name = a.get("location_name") or "Address"
            addresses.append(
                Address(
                    id=str(a["address_id"]),
                    label=name,
                    formatted_address=name,
                    raw=a,
                )
            )
        return addresses

    @override
    async def search_restaurants(self, query: str, address_id: str) -> list[Restaurant]:
        self._active_address_id = address_id
        args = {"keyword": query, "address_id": address_id}
        res = await self._call_tool("get_restaurants_for_keyword", args)
        # Contract: {"results": [...], "postback_params": {...}}
        restaurants_raw = (
            (res.get("results") or []) if isinstance(res, dict) else res or []
        )
        restaurants: list[Restaurant] = []
        for r in restaurants_raw:
            if not isinstance(r, dict) or r.get("res_id") is None:
                continue
            rating = r.get("rating")
            restaurants.append(
                Restaurant(
                    id=str(r["res_id"]),
                    name=r.get("name") or "Unknown Restaurant",
                    cuisines=r.get("cuisines") or [],
                    # The search response may omit pricing; preserve it when supplied.
                    # 0.0 is the schema default for "no rating yet".
                    rating=float(rating) if rating else None,
                    # `eta` is a string such as "30 mins" / "25-30 mins".
                    delivery_time_mins=_int_or_none(r.get("eta")),
                    distance_km=(
                        _num(r.get("distance"))
                        if r.get("distance") is not None
                        else None
                    ),
                    # Search results often use 0 as a placeholder when this
                    # field is not supplied. Do not present that as a real price.
                    cost_for_two=(
                        _num(
                            r.get("cost_for_two")
                            if r.get("cost_for_two") is not None
                            else r.get("costForTwo")
                            if r.get("costForTwo") is not None
                            else r.get("average_cost_for_two"),
                            default=0.0,
                        )
                        or None
                    ),
                    is_open=r.get("serviceability_status") != "unserviceable",
                    image_url=r.get("res_image"),
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
        if not candidates:
            candidates = [
                category
                for item in res.get("item_mappings", [])
                if isinstance(item, dict)
                for category in item.get("categories", [])
            ]
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
        return list(
            dict.fromkeys(
                (
                    str(
                        category.get("name")
                        or category.get("category_name")
                        or category.get("id")
                    )
                    if isinstance(category, dict)
                    else str(category)
                )
                for category in candidates
                if category
            )
        )

    @staticmethod
    def _addon_groups(response: dict[str, Any]) -> dict[str, dict[str, Any]]:
        groups = response.get("all_addons_data") or []
        return {
            str(g["id"]): g
            for g in groups
            if isinstance(g, dict) and g.get("id") is not None
        }

    @staticmethod
    def _merge_variant(
        detail: dict[str, Any], variant: dict[str, Any]
    ) -> dict[str, Any]:
        """Overlay a variant onto its parent item without letting nulls win.

        Price fields are the exception: they belong to the variant, so a null
        ``discounted_price`` must not inherit the parent's discount.
        """
        merged = {**detail, **{k: v for k, v in variant.items() if v is not None}}
        merged["discounted_price"] = variant.get("discounted_price")
        return merged

    def _menu_item(
        self,
        detail: dict[str, Any],
        *,
        name: str = "",
        category: str = "",
        addon_groups: dict[str, dict[str, Any]] | None = None,
        raw_extra: dict[str, Any] | None = None,
    ) -> MenuItem | None:
        # The orderable id is `variant_id` ("v_..."); `item_id` ("ctl_...") is a
        # catalogue id that create_cart rejects.
        variant_id = detail.get("variant_id")
        if not variant_id:
            return None
        groups = addon_groups or {}
        addons = [
            groups.get(str(gid), {"id": str(gid)})
            for gid in detail.get("available_add_ons") or []
        ]
        categories = detail.get("categories") or []
        return MenuItem(
            id=str(variant_id),
            name=detail.get("name") or name,
            price=_num(detail.get("discounted_price") or detail.get("price")),
            description=detail.get("description") or "",
            is_veg=_flag(detail.get("is_veg"), True),
            in_stock=_flag(detail.get("in_stock"), True),
            category=category or (categories[0] if categories else ""),
            variants=detail.get("variants") or [],
            addons=addons,
            raw={**(raw_extra or {}), **detail},
        )

    async def _fetch_menu_details(
        self, res_id: int, categories: list[str], address_id: str
    ) -> list[dict[str, Any]]:
        # Small batches keep individual responses (and latency) bounded.
        batches = [categories[i : i + 4] for i in range(0, len(categories), 4)]
        return await asyncio.gather(
            *(
                self._call_tool(
                    "get_restaurant_menu_by_categories",
                    {"res_id": res_id, "categories": batch, "address_id": address_id},
                )
                for batch in batches
            )
        )

    @override
    async def get_menu(
        self, restaurant_id: str, address_id: str | None = None
    ) -> list[MenuItem]:
        res_id = self._restaurant_id(restaurant_id)
        resolved_address_id = self._address_id(address_id)
        listing = await self._call_tool(
            "get_menu_items_listing",
            {"res_id": res_id, "address_id": resolved_address_id},
        )
        if not isinstance(listing, dict):
            return []

        items: list[MenuItem] = []
        seen: set[str] = set()

        def add(item: MenuItem | None) -> None:
            if item and item.id not in seen:
                seen.add(item.id)
                items.append(item)

        # get_menu_items_listing returns one of three shapes (see schema):
        # a full menu, a bare category list, or item_mappings + partial_menu.
        if listing.get("menu_items"):
            groups = self._addon_groups(listing)
            for detail in listing["menu_items"]:
                if isinstance(detail, dict):
                    add(self._menu_item(detail, addon_groups=groups))
            return items

        categories = self._categories(listing)
        if not categories:
            return []
        responses = await self._fetch_menu_details(
            res_id, categories, resolved_address_id
        )

        groups: dict[str, dict[str, Any]] = {}
        details_by_variant_id: dict[str, dict[str, Any]] = {}
        details: list[dict[str, Any]] = []
        for response in responses:
            if not isinstance(response, dict):
                continue
            groups.update(self._addon_groups(response))
            for detail in response.get("menu_items") or []:
                if not isinstance(detail, dict):
                    continue
                details.append(detail)
                if detail.get("variant_id"):
                    details_by_variant_id[str(detail["variant_id"])] = detail
                for variant in detail.get("variants") or []:
                    if isinstance(variant, dict) and variant.get("variant_id"):
                        details_by_variant_id[str(variant["variant_id"])] = (
                            self._merge_variant(detail, variant)
                        )

        mappings = listing.get("item_mappings") or []
        if mappings:
            # The listing gives exact orderable variant IDs (but no prices); join
            # the detail responses back on by variant ID.
            for mapping in mappings:
                if not isinstance(mapping, dict) or not mapping.get("variant_id"):
                    continue
                variant_id = str(mapping["variant_id"])
                detail = details_by_variant_id.get(variant_id) or {
                    "variant_id": variant_id
                }
                add(
                    self._menu_item(
                        detail,
                        name=mapping.get("item_name", ""),
                        category=(mapping.get("categories") or [""])[0],
                        addon_groups=groups,
                        raw_extra=mapping,
                    )
                )
            return items

        for detail in details:
            add(self._menu_item(detail, addon_groups=groups))
        return items

    @override
    async def search_menu(
        self, restaurant_id: str, query: str, address_id: str | None = None
    ) -> list[MenuItem]:
        # Filter items matching query
        all_items = await self.get_menu(restaurant_id, address_id)
        q = query.lower()
        return [
            item
            for item in all_items
            if q in item.name.lower() or q in (item.description or "").lower()
        ]

    @staticmethod
    def _extract_cart(res: Any) -> dict[str, Any]:
        """Return the ``cart`` object of a create_cart response or raise.

        Contract: ``{"cart": Cart | null, "success": bool, "message": str}``.
        A failed create_cart (unserviceable address, out-of-stock item, ...) is a
        normal response with ``success: false`` and ``cart: null``.
        """
        if isinstance(res, dict):
            cart = res.get("cart")
            if isinstance(cart, dict) and res.get("success") is not False:
                return cart
            raise ZomatoToolError(
                str(res.get("message") or "Zomato did not return a cart.")
            )
        raise ZomatoToolError("Zomato returned an unexpected cart response.")

    @override
    async def get_cart(self, address_id: str | None = None) -> Cart:
        # Zomato exposes no read-cart tool; create_cart returns the authoritative
        # cart payload, so retain that cart for the current provider session.
        if address_id:
            self._active_address_id = address_id
        if self._cart_response is None:
            raise RuntimeError("Zomato has no cart-read endpoint; create a cart first.")
        return self._parse_cart(self._cart_response)

    @override
    async def update_cart(
        self,
        restaurant_id: str,
        items: list[CartItem],
        address_id: str,
        payment_method: str | None = None,
    ) -> Cart:
        if not items:
            raise ValueError(
                "Zomato carts need at least one item; use clear_cart() to discard the cart."
            )
        for it in items:
            if not str(it.item_id).startswith("v_"):
                raise ValueError(
                    f"Zomato cart items must use orderable variant IDs ('v_...'), got {it.item_id!r}."
                )
            if it.quantity < 1:
                raise ValueError(
                    f"Invalid quantity {it.quantity} for item {it.item_id!r}."
                )

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
        cart = self._extract_cart(res)
        self._cart_response = cart
        self._cart_payment_type = payment_type
        self._cart_restaurant_id = res_id
        return self._parse_cart(cart)

    @override
    async def clear_cart(self) -> None:
        # Zomato has no delete/flush-cart tool. A cart only exists server-side as
        # the snapshot create_cart returned, and is never charged until
        # checkout_cart is called with its cart_id, so discarding it locally is
        # sufficient (and avoids sending create_cart an empty item list).
        self._cart_response = None
        self._cart_payment_type = None
        self._cart_restaurant_id = None

    @override
    async def apply_coupon(self, code: str, address_id: str | None = None) -> Cart:
        if self._cart_response is None or self._cart_restaurant_id is None:
            raise RuntimeError("Create a Zomato cart before applying a coupon.")

        cart = self._parse_cart(self._cart_response)
        payload_items = [
            {
                "variant_id": item.item_id,
                "quantity": item.quantity,
                "add_ons": [
                    {
                        "id": str(addon.get("id") or addon.get("choice_id")),
                        "quantity": int(addon.get("quantity", 1)),
                    }
                    for addon in item.selected_addons
                    if addon.get("id") or addon.get("choice_id")
                ]
                or None,
            }
            for item in cart.items
        ]
        resolved_address_id = self._address_id(address_id)
        if not self._cart_payment_type:
            raise RuntimeError("Zomato cart is missing its payment type.")
        res = await self._call_tool(
            "create_cart",
            {
                "res_id": self._cart_restaurant_id,
                "address_id": resolved_address_id,
                "items": payload_items,
                "payment_type": self._cart_payment_type,
                "promo_code": code,
            },
        )
        updated_cart = self._extract_cart(res)
        self._cart_response = updated_cart
        return self._parse_cart(updated_cart)

    @override
    async def get_cart_coupons(
        self,
        restaurant_id: str,
        address_id: str,
        cart: Cart | None = None,
    ) -> list[CartCoupon]:
        if str(restaurant_id) != str(self._cart_restaurant_id):
            raise ValueError("The requested restaurant does not match the Zomato cart.")
        self._address_id(address_id)

        cart_data = cart.raw if cart is not None else self._cart_response
        if not isinstance(cart_data, dict):
            raise RuntimeError("Create a Zomato cart before listing its promos.")
        offers = cart_data.get("offers", [])
        if isinstance(offers, dict):
            offers = offers.get("offers") or offers.get("coupons") or []
        coupons: list[CartCoupon] = []
        for offer in offers or []:
            if not isinstance(offer, dict):
                continue
            code = (
                offer.get("coupon_code")
                or offer.get("couponCode")
                or offer.get("code")
                or offer.get("offer_id")
                or offer.get("id")
            )
            if not code:
                continue
            coupons.append(
                CartCoupon(
                    code=str(code),
                    title=str(offer.get("title") or offer.get("name") or ""),
                    description=str(
                        offer.get("description")
                        or offer.get("subtitle")
                        or offer.get("terms")
                        or ""
                    ),
                    discount=(
                        _num(
                            offer.get("discount")
                            or offer.get("discount_amount")
                            or offer.get("discountAmount")
                        )
                        or None
                    ),
                    applicable=bool(
                        offer.get("applicable", offer.get("is_applicable", True))
                    ),
                    recommended=bool(
                        offer.get("recommended")
                        or offer.get("is_recommended")
                        or offer.get("isRecommended")
                    ),
                    raw=offer,
                )
            )
        return coupons

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
            raise ValueError(
                "Zomato cart requires payment_method 'upi' or 'cash_on_delivery'."
            )
        return aliases[normalized]

    def _parse_cart(self, cart: dict[str, Any]) -> Cart:
        """Parse the ``cart`` object from create_cart (not the response wrapper).

        Items are ``{"item": {...}, "quantity": n, "add_ons": [...]}`` and the
        bill lives in ``item_total``, ``final_amount``, ``promo_discount_amount``
        and ``charge_breakdown`` (``base_charges[]`` + ``taxes[]``).
        """
        if isinstance(cart.get("cart"), dict):  # tolerate the response wrapper
            cart = cart["cart"]

        parsed_items: list[CartItem] = []
        for entry in cart.get("items") or []:
            if not isinstance(entry, dict):
                continue
            item = entry.get("item") or {}
            parsed_items.append(
                CartItem(
                    item_id=str(item.get("variant_id") or item.get("item_id") or ""),
                    name=item.get("name", ""),
                    price=_num(item.get("discounted_price") or item.get("price")),
                    quantity=int(entry.get("quantity") or 1),
                    selected_addons=[
                        dict(a)
                        for a in entry.get("add_ons") or []
                        if isinstance(a, dict)
                    ],
                )
            )

        breakdown = cart.get("charge_breakdown") or {}
        base_charges = [
            c for c in breakdown.get("base_charges") or [] if isinstance(c, dict)
        ]
        delivery_fee = sum(
            _num(c.get("amount"))
            for c in base_charges
            if "deliver" in str(c.get("charge_type", "")).lower()
        )
        taxes = sum(
            _num(t.get("tax_amount"))
            for t in breakdown.get("taxes") or []
            if isinstance(t, dict)
        ) + sum(
            _num(t.get("tax_amount"))
            for c in base_charges
            for t in c.get("taxes") or []
            if isinstance(t, dict)
        )
        # A charge's own ``discount`` (e.g. a waived delivery fee) is already
        # reflected in its ``amount``, so only the promo discount is subtracted.
        discount = _num(cart.get("promo_discount_amount"))
        item_total = _num(cart.get("item_total"))
        # The backend applies offers/charges, so trust final_amount rather
        # than recomputing from line items (per create_cart's description).
        grand_total = _num(cart.get("final_amount"))
        # Whatever the breakdown doesn't itemise (platform fee, packaging, ...)
        # is reported as fees so the displayed lines add up to the total.
        fees = grand_total - item_total - delivery_fee - taxes + discount

        return Cart(
            restaurant_id=str(cart.get("res_id") or ""),
            items=parsed_items,
            item_total=item_total,
            delivery_fee=delivery_fee,
            taxes=taxes,
            fees=max(fees, 0.0),
            discount=discount,
            grand_total=grand_total,
            raw=cart,
        )

    @override
    async def get_payment_options(self, address_id: str) -> list[PaymentMethod]:
        self._active_address_id = address_id
        # Zomato MCP checkout uses UPI QR or online link
        return [
            PaymentMethod(
                id="UPI_QR", name="UPI QR Code (Scan to pay)", method_type="UPI_QR"
            ),
            PaymentMethod(id="COD", name="Cash on Delivery", method_type="COD"),
        ]

    @override
    async def place_order(
        self, payment_method: str, address_id: str, **kwargs: Any
    ) -> OrderPlacement:
        self._active_address_id = address_id
        cart_id = kwargs.pop("cart_id", None)
        if not cart_id and self._cart_response:
            # cart_id lives on the cart object returned by create_cart.
            cart_id = self._cart_response.get("cart_id")
        if not cart_id:
            raise ValueError("Zomato checkout requires a cart_id from create_cart.")
        args = {
            "cart_id": str(cart_id),
            "payment_method_type": self._payment_type(payment_method),
        }
        res = await self._call_tool("checkout_cart", args)

        # Contract: {success, is_upi, qr_code (base64 image), upi_link, message,
        # order_id, final_payment_amount}. order_id is only set on success.
        if (
            not isinstance(res, dict)
            or not res.get("success")
            or not res.get("order_id")
        ):
            msg = res.get("message") if isinstance(res, dict) else None
            raise ZomatoToolError(str(msg or "Zomato checkout failed."))

        order_id = str(res["order_id"])
        if res.get("final_payment_amount") is not None:
            self._order_totals[order_id] = _num(res["final_payment_amount"])
        requires_payment = bool(
            res.get("is_upi") or res.get("upi_link") or res.get("qr_code")
        )

        return OrderPlacement(
            order_id=order_id,
            status=(
                OrderStatus.PENDING_PAYMENT if requires_payment else OrderStatus.PLACED
            ),
            requires_payment=requires_payment,
            # `upi_link` is the payable UPI URI (render it as a QR); the base64
            # PNG from the server is left in `raw["qr_code"]`.
            upi_qr_string=res.get("upi_link"),
            payment_url=res.get("upi_link"),
            raw=res,
        )

    async def _tracking_item(self, order_id: str) -> dict[str, Any]:
        """Fetch the tracking entry for ``order_id``.

        Contract: input ``order_id`` is a string, but each output item's
        ``order_id`` is an *integer*, so ids must be compared as strings.
        """
        res = await self._call_tool(
            "get_order_tracking_info", {"order_id": str(order_id)}
        )
        tracking = res.get("order_tracking") if isinstance(res, dict) else None
        items = [
            i
            for i in (tracking or {}).get("order_tracking_items") or []
            if isinstance(i, dict)
        ]
        for item in items:
            if str(item.get("order_id")) == str(order_id):
                return item
        if len(items) == 1:  # the server already filtered by order_id
            return items[0]
        raise LookupError(f"Zomato has no active order {order_id} to report on.")

    @override
    async def check_payment_status(
        self, order_id: str, **kwargs: Any
    ) -> dict[str, Any]:
        item = await self._tracking_item(order_id)
        return {
            "order_id": str(item.get("order_id", order_id)),
            "is_order_paid": bool(item.get("is_order_paid")),
            "is_order_placed": bool(item.get("is_order_placed")),
            "order_status": item.get("order_status", ""),
            "message": item.get("message", ""),
            "pending_payment_qr_code": item.get("pending_payment_qr_code"),
            "raw": item,
        }

    @override
    async def confirm_order(self, order_id: str, **kwargs: Any) -> OrderSummary:
        item = await self._tracking_item(order_id)
        placed = item.get("is_order_placed")
        status = _status_from_text(
            item.get("order_status"), paid=item.get("is_order_paid"), placed=placed
        )
        if status is OrderStatus.PLACED and placed:
            status = OrderStatus.CONFIRMED
        return OrderSummary(
            order_id=str(item.get("order_id", order_id)),
            restaurant_name=item.get("restaurant_name") or "",
            status=status,
            # Tracking carries no amount; use the one checkout reported, if any.
            total_amount=self._order_totals.get(str(order_id), 0.0),
            raw=item,
        )

    @override
    async def get_orders(self, address_id: str | None = None) -> list[OrderSummary]:
        res = await self._call_tool(
            "get_order_history", {"address_id": self._address_id(address_id)}
        )
        # Contract: {"order_history": {"order_history_items": [...], "has_more",
        # "postback_params"}}. Only the first page is fetched.
        history = res.get("order_history") if isinstance(res, dict) else None
        orders_raw = (history or {}).get("order_history_items") or []
        summaries: list[OrderSummary] = []
        for o in orders_raw:
            if not isinstance(o, dict):
                continue
            items_summary = ", ".join(
                f"{ci.get('quantity', 1)}x {ci.get('name', '')}"
                for ci in o.get("catalogue_items") or []
                if isinstance(ci, dict)
            )
            summaries.append(
                OrderSummary(
                    order_id=str(o.get("order_id", "")),
                    restaurant_name=o.get("res_name") or "Restaurant",
                    status=_status_from_text(o.get("payment_status")),
                    # payment_amount is a display string, e.g. "₹450".
                    total_amount=_num(o.get("payment_amount")),
                    items_summary=items_summary,
                    created_at=o.get("payment_date"),
                    raw=o,
                )
            )
        return summaries

    @override
    async def track_order(self, order_id: str) -> OrderTracking:
        item = await self._tracking_item(order_id)
        rider: dict[str, Any] = item.get("rider") or {}  # pyright: ignore[reportExplicitAny]
        return OrderTracking(
            order_id=str(cast(int, item.get("order_id", order_id))),
            status=_status_from_text(
                item.get("order_status"),
                paid=item.get("is_order_paid"),
                placed=item.get("is_order_placed"),
            ),
            eta_mins=_int_or_none(item.get("edt")),
            status_message=(item.get("message") or "").strip()
            or item.get("order_status")
            or "",
            delivery_partner_name=rider.get("name"),
            delivery_partner_phone=rider.get("phone_number"),
            raw=item,
        )
