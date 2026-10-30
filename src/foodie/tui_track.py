"""Order history and live tracking screens."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, cast

from rich.text import Text
from textual import on, work
from textual.app import ComposeResult
from textual.screen import Screen
from textual.widgets import DataTable, Footer, Header, Static

from foodie.models import OrderStatus, OrderSummary, OrderTracking

if TYPE_CHECKING:
    from foodie.tui import FoodieTui

TITLES = {"swiggy": "Swiggy", "zomato": "Zomato"}
TERMINAL = {OrderStatus.DELIVERED, OrderStatus.CANCELLED, OrderStatus.FAILED}
REFRESH_SECONDS = 20.0


def _price(value: float | None) -> str:
    return f"₹{value:,.0f}" if value else "—"


def _label(status: OrderStatus) -> str:
    return status.value.replace("_", " ").title()


class OrdersScreen(Screen[None]):
    CSS = """
    #orders-status { height: 2; padding: 0 1; }
    #orders { height: 1fr; }
    """

    BINDINGS = [("escape", "app.pop_screen", "Back"), ("r", "reload", "Refresh")]

    def __init__(self) -> None:
        super().__init__()
        self.rows: list[tuple[str, OrderSummary]] = []

    @property
    def foodie(self) -> "FoodieTui":
        return cast("FoodieTui", self.app)

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static("Loading orders…", id="orders-status")
        table = DataTable(id="orders")
        table.cursor_type = "row"
        table.add_columns("Provider", "Restaurant", "Items", "Total", "Status", "Placed")
        yield table
        yield Footer()

    def on_mount(self) -> None:
        self.sub_title = "Orders"
        self.query_one("#orders", DataTable).focus()
        self.load()

    async def action_reload(self) -> None:
        self.load()

    async def _address_id(self, key: str) -> str | None:
        known = self.foodie.address_ids.get(key)
        if known:
            return known
        addresses = await self.foodie.providers[key].get_addresses()
        return addresses[0].id if addresses else None

    @work(exclusive=True)
    async def load(self) -> None:
        status = self.query_one("#orders-status", Static)
        status.update("Loading orders…")

        async def fetch(key: str) -> list[tuple[str, OrderSummary]]:
            address_id = await self._address_id(key)
            orders = await self.foodie.providers[key].get_orders(address_id)
            return [(key, order) for order in orders]

        results = await asyncio.gather(
            *(fetch(k) for k in TITLES), return_exceptions=True
        )
        errors = [
            f"{TITLES[k]}: {r}" for k, r in zip(TITLES, results) if isinstance(r, Exception)
        ]
        rows = [row for r in results if not isinstance(r, Exception) for row in r]
        # Active orders first; history keeps each provider's own (newest-first) order.
        self.rows = sorted(rows, key=lambda row: row[1].status in TERMINAL)
        table = self.query_one("#orders", DataTable)
        table.clear()
        for key, order in self.rows:
            active = order.status not in TERMINAL
            table.add_row(
                TITLES[key],
                order.restaurant_name,
                order.items_summary,
                _price(order.total_amount),
                Text(_label(order.status), style="bold green" if active else ""),
                order.created_at or "—",
            )
        active_count = sum(order.status not in TERMINAL for _, order in self.rows)
        summary = f"{len(self.rows)} orders · {active_count} active · Enter to track"
        status.update(summary + (" | " + " | ".join(errors) if errors else ""))

    @on(DataTable.RowSelected, "#orders")
    def track(self, event: DataTable.RowSelected) -> None:
        if 0 <= event.cursor_row < len(self.rows):
            key, order = self.rows[event.cursor_row]
            self.app.push_screen(TrackScreen(key, order))


class TrackScreen(Screen[None]):
    CSS = """
    #track { padding: 1 2; height: auto; }
    #track-note { padding: 0 2; color: $text-muted; height: auto; }
    """

    BINDINGS = [("escape", "app.pop_screen", "Back"), ("r", "refresh", "Refresh")]

    def __init__(self, key: str, order: OrderSummary) -> None:
        super().__init__()
        self.key = key
        self.order = order

    @property
    def foodie(self) -> "FoodieTui":
        return cast("FoodieTui", self.app)

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static("", id="track")
        yield Static("", id="track-note")
        yield Footer()

    def on_mount(self) -> None:
        self.sub_title = f"{TITLES[self.key]} · {self.order.restaurant_name}"
        self._show(None, "Loading tracking…")
        self.refresh_loop()

    async def action_refresh(self) -> None:
        await self._fetch()

    @work(exclusive=True)
    async def refresh_loop(self) -> None:
        while True:
            tracking = await self._fetch()
            if tracking is None or tracking.status in TERMINAL:
                return
            await asyncio.sleep(REFRESH_SECONDS)

    async def _fetch(self) -> OrderTracking | None:
        try:
            tracking = await self.foodie.providers[self.key].track_order(self.order.order_id)
        except LookupError as exc:
            self._show(None, f"No live tracking for this order ({exc}).")
            return None
        except Exception as exc:
            self._show(None, f"Could not load tracking: {exc}")
            return None
        note = (
            "Order finished."
            if tracking.status in TERMINAL
            else f"Refreshes every {int(REFRESH_SECONDS)}s · r to refresh now."
        )
        self._show(tracking, note)
        return tracking

    def _show(self, tracking: OrderTracking | None, note: str) -> None:
        order = self.order
        status = tracking.status if tracking else order.status
        lines = [
            f"[b]{order.restaurant_name}[/b]  ·  order {order.order_id}",
            f"{order.items_summary}" if order.items_summary else "",
            f"Total: {_price(order.total_amount)}",
            "",
            f"Status:   [b]{_label(status)}[/b]",
        ]
        if tracking:
            if tracking.status_message:
                lines.append(f"Message:  {tracking.status_message}")
            if tracking.eta_mins is not None and status not in TERMINAL:
                lines.append(f"ETA:      {tracking.eta_mins} min")
            if tracking.delivery_partner_name:
                partner = tracking.delivery_partner_name
                if tracking.delivery_partner_phone:
                    partner += f" ({tracking.delivery_partner_phone})"
                lines.append(f"Rider:    {partner}")
        self.query_one("#track", Static).update("\n".join(lines))
        self.query_one("#track-note", Static).update(note)
