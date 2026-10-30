from __future__ import annotations

import asyncio
import difflib
import re
from dataclasses import dataclass

from rich.text import Text
from textual import on
from textual.app import App, ComposeResult
from textual.containers import Horizontal
from textual.screen import Screen
from textual.widgets import Button, DataTable, Footer, Header, Input, Select, Static

from foodie.auth import authenticate_interactive
from foodie.customize import CartLine
from foodie.models import Address, MenuItem, Restaurant
from foodie.providers.base import BaseProvider
from foodie.providers.swiggy import SwiggyProvider
from foodie.providers.zomato import ZomatoProvider
from foodie.tui_cart import AddItemModal, CartScreen, CartState, ProviderContext
from foodie.tui_track import OrdersScreen


@dataclass
class RestaurantPair:
    swiggy: Restaurant | None
    zomato: Restaurant | None
    score: float


def _normalise(value: str) -> str:
    value = value.casefold().replace("&", " and ")
    return re.sub(r"[^a-z0-9]+", " ", value).strip()


def _location(restaurant: Restaurant) -> str:
    raw = restaurant.raw
    parts: list[str] = []
    for key in ("locality", "area", "address", "location", "locality_verbose",
                "areaName", "area_name", "city"):
        value = raw.get(key)
        if isinstance(value, dict):
            value = value.get("name") or value.get("title") or value.get("address")
        if isinstance(value, str) and value.strip():
            parts.append(value.strip())
    return _normalise(" ".join(parts))


def pair_restaurants(
    swiggy: list[Restaurant], zomato: list[Restaurant]
) -> list[RestaurantPair]:
    """Pair likely same restaurants, preferring name and then location similarity."""
    candidates: list[tuple[float, int, int]] = []
    for i, left in enumerate(swiggy):
        left_name = _normalise(left.name)
        for j, right in enumerate(zomato):
            right_name = _normalise(right.name)
            name_score = difflib.SequenceMatcher(None, left_name, right_name).ratio()
            left_loc, right_loc = _location(left), _location(right)
            loc_score = (
                difflib.SequenceMatcher(None, left_loc, right_loc).ratio()
                if left_loc and right_loc else 0.0
            )
            # Location can strengthen a name match, but should not pair unrelated names.
            score = name_score * 0.85 + loc_score * 0.15
            if name_score >= 0.62 and (not left_loc or not right_loc or score >= 0.62):
                candidates.append((score, i, j))

    used_s: set[int] = set()
    used_z: set[int] = set()
    pairs: list[RestaurantPair] = []
    for score, i, j in sorted(candidates, reverse=True):
        if i not in used_s and j not in used_z:
            used_s.add(i)
            used_z.add(j)
            pairs.append(RestaurantPair(swiggy[i], zomato[j], score))
    pairs.extend(RestaurantPair(item, None, 0.0) for i, item in enumerate(swiggy) if i not in used_s)
    pairs.extend(RestaurantPair(None, item, 0.0) for j, item in enumerate(zomato) if j not in used_z)
    return sorted(pairs, key=lambda pair: (
        -(pair.score), (pair.swiggy or pair.zomato).name.casefold()
    ))


def _price(value: float | None) -> str:
    return f"₹{value:,.0f}" if value is not None else "—"


def describe(exc: BaseException) -> str:
    """An error message that is never empty (e.g. httpx.ConnectError has no text)."""
    return str(exc) or f"{type(exc).__name__} (could not reach the server)"


def _address_label(address: Address) -> str:
    location = address.formatted_address or address.label or address.id
    return f"{address.label}: {location}"


def pair_menu_items(swiggy_menu: list, zomato_menu: list) -> list[tuple[str, object | None, object | None, float]]:
    candidates: list[tuple[float, int, int]] = []
    for i, left in enumerate(swiggy_menu):
        for j, right in enumerate(zomato_menu):
            a, b = _normalise(left.name), _normalise(right.name)
            score = difflib.SequenceMatcher(None, a, b).ratio()
            if a and b and score >= 0.68:
                candidates.append((score, i, j))
    used_s: set[int] = set()
    used_z: set[int] = set()
    rows: list[tuple[str, object | None, object | None, float]] = []
    for score, i, j in sorted(candidates, reverse=True):
        if i not in used_s and j not in used_z:
            left, right = swiggy_menu[i], zomato_menu[j]
            rows.append((left.category or right.category or "", left, right, score))
            used_s.add(i); used_z.add(j)
    rows.extend((item.category or "", item, None, 0.0) for i, item in enumerate(swiggy_menu) if i not in used_s)
    rows.extend((item.category or "", None, item, 0.0) for j, item in enumerate(zomato_menu) if j not in used_z)
    return sorted(rows, key=lambda row: (row[0].casefold(), (row[1] or row[2]).name.casefold()))



def _cost_for_two(pair: RestaurantPair) -> float | None:
    """Zomato supplies no cost for two, so the shared column uses Swiggy's value."""
    for restaurant in (pair.swiggy, pair.zomato):
        if restaurant is not None and restaurant.cost_for_two is not None:
            return restaurant.cost_for_two
    return None


class RestaurantsScreen(Screen[None]):
    CSS = """
    #search-row { height: 3; }
    #query { width: 1fr; }
    #search { width: 16; }
    #auth-row { height: 3; }
    #auth-row Button { width: 1fr; }
    #addresses { height: 3; }
    #swiggy-address, #zomato-address { width: 1fr; }
    #status { height: 2; padding: 0 1; }
    #results { height: 1fr; }
    """

    BINDINGS = [("ctrl+r", "search", "Search"), ("q", "app.quit", "Quit")]

    def __init__(self) -> None:
        super().__init__()
        self.pairs: list[RestaurantPair] = []

    @property
    def foodie(self) -> "FoodieTui":
        return self.app  # type: ignore[return-value]

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal(id="search-row"):
            yield Input(placeholder="Restaurant name or cuisine…", id="query")
            yield Button("Search both", id="search", variant="primary")
        with Horizontal(id="auth-row"):
            yield Button("Swiggy: checking…", id="auth-swiggy", disabled=True)
            yield Button("Zomato: checking…", id="auth-zomato", disabled=True)
        with Horizontal(id="addresses"):
            yield Select([], prompt="Swiggy delivery address", id="swiggy-address", allow_blank=True)
            yield Select([], prompt="Zomato delivery address", id="zomato-address", allow_blank=True)
        yield Static("Loading saved delivery addresses…", id="status")
        table = DataTable(id="results")
        table.cursor_type = "row"
        table.add_columns("Swiggy restaurant", "Zomato restaurant", "Cost for two", "Match")
        yield table
        yield Footer()

    async def on_mount(self) -> None:
        self.query_one("#results", DataTable).focus()
        await self.load_addresses()

    def _provider(self, key: str) -> BaseProvider:
        return getattr(self.foodie, key)

    async def load_addresses(self) -> None:
        results = await asyncio.gather(
            self.load_provider("swiggy"), self.load_provider("zomato")
        )
        errors = [e for e in results if e]
        self.query_one("#status", Static).update(
            " · ".join(errors) if errors
            else "Addresses loaded. Search, then press Enter on a row to open its menu."
        )

    async def load_provider(self, key: str) -> str | None:
        """Refresh a provider's auth button and address list; returns an error message."""
        provider = self._provider(key)
        button = self.query_one(f"#auth-{key}", Button)
        select = self.query_one(f"#{key}-address", Select)
        try:
            authed = await provider.is_authenticated()
        except Exception as exc:
            authed = False
            error: str | None = f"{provider.display_name}: {describe(exc)}"
        else:
            error = None
        button.label = f"{provider.display_name}: log out" if authed else f"{provider.display_name}: log in"
        button.variant = "success" if authed else "warning"
        button.disabled = False
        if not authed:
            select.set_options([])
            return error or f"{provider.display_name} is not authenticated — press its log in button"
        try:
            addresses = await provider.get_addresses()
        except Exception as exc:
            return f"{provider.display_name}: {describe(exc)}"
        select.set_options(((_address_label(a), a.id) for a in addresses))
        self.foodie.address_labels.update({a.id: _address_label(a) for a in addresses})
        if addresses:
            select.value = addresses[0].id
            self.foodie.address_ids[key] = addresses[0].id
            return None
        return f"{provider.display_name}: no saved addresses"

    @on(Button.Pressed, "#auth-swiggy, #auth-zomato")
    async def auth_pressed(self, event: Button.Pressed) -> None:
        key = (event.button.id or "").removeprefix("auth-")
        provider = self._provider(key)
        status = self.query_one("#status", Static)
        event.button.disabled = True
        try:
            if await provider.is_authenticated():
                await provider.logout()
                status.update(f"Logged out of {provider.display_name}.")
            else:
                ok = await authenticate_interactive(provider, log=status.update)
                if not ok:
                    return
            error = await self.load_provider(key)
            status.update(error or f"{provider.display_name} ready.")
        except Exception as exc:
            status.update(f"{provider.display_name} auth failed: {exc}")
        finally:
            event.button.disabled = False

    @on(Button.Pressed, "#search")
    async def search_pressed(self) -> None:
        await self.action_search()

    @on(Input.Submitted, "#query")
    async def query_submitted(self) -> None:
        await self.action_search()

    def _addresses(self) -> tuple[str, str] | None:
        s_address = self.query_one("#swiggy-address", Select).value
        z_address = self.query_one("#zomato-address", Select).value
        if s_address is Select.BLANK or z_address is Select.BLANK:
            return None
        self.foodie.address_ids.update(swiggy=str(s_address), zomato=str(z_address))
        return str(s_address), str(z_address)

    async def action_search(self) -> None:
        status = self.query_one("#status", Static)
        query = self.query_one("#query", Input).value.strip()
        if not query:
            status.update("Enter a restaurant name or cuisine to search.")
            return
        addresses = self._addresses()
        if addresses is None:
            status.update("Choose a saved delivery address for both providers.")
            return
        button = self.query_one("#search", Button)
        button.disabled = True
        status.update("Searching Swiggy and Zomato…")
        try:
            results = await asyncio.gather(
                self.foodie.swiggy.search_restaurants(query, addresses[0]),
                self.foodie.zomato.search_restaurants(query, addresses[1]),
                return_exceptions=True,
            )
            errors = [
                f"{name}: {describe(result)}"
                for name, result in zip(("Swiggy", "Zomato"), results)
                if isinstance(result, Exception)
            ]
            swiggy = results[0] if isinstance(results[0], list) else []
            zomato = results[1] if isinstance(results[1], list) else []
            self.pairs = pair_restaurants(swiggy, zomato)
            table = self.query_one("#results", DataTable)
            table.clear()
            for pair in self.pairs:
                s, z = pair.swiggy, pair.zomato
                table.add_row(
                    s.name if s else "—",
                    z.name if z else "—",
                    _price(_cost_for_two(pair)),
                    f"{pair.score:.0%}" if s and z else "—",
                )
            matches = sum(p.swiggy is not None and p.zomato is not None for p in self.pairs)
            summary = f"{len(swiggy)} Swiggy · {len(zomato)} Zomato · {matches} likely matches"
            if errors:
                summary += " | " + " | ".join(errors)
            status.update(summary)
            table.focus()
        except Exception as exc:
            status.update(f"Search failed: {exc}")
        finally:
            button.disabled = False

    @on(DataTable.RowSelected, "#results")
    def open_menu(self, event: DataTable.RowSelected) -> None:
        addresses = self._addresses()
        if addresses is None or not 0 <= event.cursor_row < len(self.pairs):
            return
        self.app.push_screen(MenuScreen(self.pairs[event.cursor_row], *addresses))


class MenuScreen(Screen[None]):
    CSS = """
    #menu-query { height: 3; }
    #menu-status { height: 2; padding: 0 1; }
    #menu-results { height: 1fr; }
    """

    BINDINGS = [("escape", "app.pop_screen", "Back")]

    def __init__(self, pair: RestaurantPair, swiggy_address: str, zomato_address: str) -> None:
        super().__init__()
        self.pair = pair
        self.swiggy_address = swiggy_address
        self.zomato_address = zomato_address
        self.menu_items: list[tuple[str, object | None, object | None, float]] = []
        self._rows: dict[str, tuple[MenuItem | None, MenuItem | None]] = {}

    @property
    def foodie(self) -> "FoodieTui":
        return self.app  # type: ignore[return-value]

    def compose(self) -> ComposeResult:
        yield Header()
        yield Input(placeholder="Filter menu…", id="menu-query")
        yield Static("Loading menus…", id="menu-status")
        table = DataTable(id="menu-results")
        table.cursor_type = "row"
        table.add_columns("Swiggy item", "Swiggy price", "Zomato item", "Zomato price", "Match")
        yield table
        yield Footer()

    async def on_mount(self) -> None:
        pair = self.pair
        self.sub_title = (pair.swiggy or pair.zomato).name
        self.query_one("#menu-results", DataTable).focus()

        async def fetch(provider: BaseProvider, restaurant: Restaurant | None, address: str) -> list:
            if restaurant is None:
                return []
            return await provider.get_menu(restaurant.id, address)

        results = await asyncio.gather(
            fetch(self.foodie.swiggy, pair.swiggy, self.swiggy_address),
            fetch(self.foodie.zomato, pair.zomato, self.zomato_address),
            return_exceptions=True,
        )
        swiggy_menu = results[0] if isinstance(results[0], list) else []
        zomato_menu = results[1] if isinstance(results[1], list) else []
        errors = [
            f"{'Swiggy' if i == 0 else 'Zomato'} menu: {describe(r)}"
            for i, r in enumerate(results)
            if isinstance(r, Exception)
        ]
        self.menu_items = pair_menu_items(swiggy_menu, zomato_menu)
        self.render_menu()
        summary = f"{len(swiggy_menu)} Swiggy items · {len(zomato_menu)} Zomato items"
        if errors:
            summary += " | " + " | ".join(errors)
        self.query_one("#menu-status", Static).update(summary)

    @on(Input.Changed, "#menu-query")
    def menu_filter_changed(self) -> None:
        self.render_menu()

    def render_menu(self) -> None:
        query = _normalise(self.query_one("#menu-query", Input).value)
        table = self.query_one("#menu-results", DataTable)
        table.clear()
        self._rows.clear()
        current: str | None = None
        for n, (category, swiggy_item, zomato_item, score) in enumerate(self.menu_items):
            names = " ".join(item.name for item in (swiggy_item, zomato_item) if item)
            if query and query not in _normalise(names + " " + category):
                continue
            if category != current:
                current = category
                table.add_row(Text(category or "Uncategorised", style="bold"), "", "", "", "", key=f"h{n}")
            self._rows[f"i{n}"] = (swiggy_item, zomato_item)
            table.add_row(
                swiggy_item.name if swiggy_item else "—",
                _price(swiggy_item.price) if swiggy_item else "—",
                zomato_item.name if zomato_item else "—",
                _price(zomato_item.price) if zomato_item else "—",
                f"{score:.0%}" if swiggy_item and zomato_item else "Unmatched",
                key=f"i{n}",
            )

    @on(DataTable.RowSelected, "#menu-results")
    def add_selected(self, event: DataTable.RowSelected) -> None:
        items = self._rows.get(event.row_key.value or "")
        if items is None:
            return
        pair = self.pair
        contexts: dict[str, ProviderContext] = {}
        addresses = {"swiggy": self.swiggy_address, "zomato": self.zomato_address}
        restaurants = {"swiggy": pair.swiggy, "zomato": pair.zomato}
        for key, item in zip(("swiggy", "zomato"), items):
            restaurant = restaurants[key]
            if item is not None and restaurant is not None and item.in_stock:
                contexts[key] = ProviderContext(item, restaurant.id, addresses[key])
        if not contexts:
            self.notify("That item is out of stock.", severity="warning")
            return
        name = (items[0] or items[1]).name
        self.app.push_screen(AddItemModal(name, contexts), self._added)

    def _added(self, line: CartLine | None) -> None:
        if line is None:
            return
        cart = self.foodie.cart
        restaurants = {
            k: r for k, r in (("swiggy", self.pair.swiggy), ("zomato", self.pair.zomato)) if r
        }
        cart.start(
            (self.pair.swiggy or self.pair.zomato).name,
            restaurants,
            {"swiggy": self.swiggy_address, "zomato": self.zomato_address},
        )
        if not cart.covering([*cart.lines, line]):
            self.notify(
                "No single provider offers every item in your cart, so this wasn't added.",
                severity="warning",
            )
            return
        cart.lines.append(line)
        providers = " + ".join(k.title() for k in line.entries)
        self.notify(f"Added {line.quantity} × {line.name} ({providers}). Press c to view the cart.")


class FoodieTui(App[None]):
    TITLE = "Foodie — Swiggy × Zomato"
    BINDINGS = [("c", "cart", "Cart"), ("o", "orders", "Orders")]

    def __init__(self) -> None:
        super().__init__()
        self.swiggy = SwiggyProvider()
        self.zomato = ZomatoProvider()
        self.providers: dict[str, BaseProvider] = {"swiggy": self.swiggy, "zomato": self.zomato}
        self.cart = CartState()
        self.address_labels: dict[str, str] = {}
        self.address_ids: dict[str, str] = {}

    def action_orders(self) -> None:
        if not isinstance(self.screen, OrdersScreen):
            self.push_screen(OrdersScreen())

    def action_cart(self) -> None:
        if isinstance(self.screen, CartScreen):
            return
        if not self.cart.lines:
            self.notify("Your cart is empty. Press Enter on a menu item to add it.")
            return
        self.push_screen(CartScreen())

    def on_mount(self) -> None:
        self.push_screen(RestaurantsScreen())

    async def on_unmount(self) -> None:
        await asyncio.gather(
            self.swiggy._http.aclose(), self.zomato._http.aclose(), return_exceptions=True
        )


def main() -> None:
    FoodieTui().run()


if __name__ == "__main__":
    main()
