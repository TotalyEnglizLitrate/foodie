"""Order confirmation and payment screens."""

from __future__ import annotations

import asyncio
from typing import Any

from rich.text import Text
from textual import on, work
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Label, Select, Static

from foodie.models import OrderPlacement, OrderStatus
from foodie.providers.base import BaseProvider
from foodie.qr import render_qr

PAID = {"SUCCESS", "SUCCESSFUL", "PAID", "COMPLETED", "CHARGED", "CAPTURED"}
DEFAULT_POLL_SECONDS = 300.0
MIN_POLL_INTERVAL = 3.0


def _price(value: float | None) -> str:
    return f"₹{value:,.0f}" if value is not None else "—"


async def place_order(
    key: str, provider: BaseProvider, choice: str, address_id: str
) -> OrderPlacement:
    """Place the order for the provider's current cart. ``choice`` is "upi" or "cash"."""
    if key == "swiggy":
        if choice == "cash":
            return await provider.place_order("Cash", address_id)
        return await provider.place_order("UPI", address_id, generateUPIQR=True)
    return await provider.place_order(
        "cash_on_delivery" if choice == "cash" else "upi", address_id
    )


def payment_state(result: dict[str, Any]) -> tuple[str, str]:
    """Reduce a provider's payment-status payload to (paid|failed|pending, text)."""
    if result.get("is_order_paid") or result.get("is_order_placed"):
        return "paid", str(result.get("order_status") or "paid")
    data = result.get("data") if isinstance(result.get("data"), dict) else {}
    text = ""
    for source in (result, data):
        for field in ("status", "paymentStatus", "payment_status", "state", "result"):
            if isinstance(source.get(field), str) and source[field]:
                text = source[field]
                break
        if text:
            break
    text = text or str(result.get("order_status") or "")
    upper = text.upper()
    if upper in PAID:
        return "paid", text
    if upper.startswith(("FAIL", "CANCEL", "EXPIRE", "REFUND", "DECLIN")):
        return "failed", text
    return "pending", text or "pending"


class OrderConfirmModal(ModalScreen["str | None"]):
    """Final confirmation before money moves; dismisses with the chosen payment."""

    DEFAULT_CSS = """
    OrderConfirmModal { align: center middle; }
    #confirm { width: 70; height: auto; max-height: 90%; border: thick $warning; background: $surface; padding: 1 2; }
    #confirm-summary { height: auto; margin-bottom: 1; }
    #confirm-buttons { height: 3; align-horizontal: right; margin-top: 1; }
    """

    BINDINGS = [("escape", "dismiss_modal", "Cancel")]

    def __init__(
        self,
        provider_name: str,
        restaurant: str,
        address: str,
        lines: list[str],
        total: float,
        payments: list[tuple[str, str]],
    ) -> None:
        super().__init__()
        self.provider_name = provider_name
        self.restaurant = restaurant
        self.address = address
        self.lines = lines
        self.total = total
        self.payments = payments

    def compose(self) -> ComposeResult:
        summary = "\n".join(
            [
                f"[b]Place order on {self.provider_name}[/b]",
                f"Restaurant: {self.restaurant}",
                f"Deliver to: {self.address}",
                "",
                *self.lines,
                "",
                f"[b]Total: {_price(self.total)}[/b]",
            ]
        )
        with Vertical(id="confirm"):
            yield Static(summary, id="confirm-summary")
            yield Select(self.payments, allow_blank=False, id="confirm-payment")
            with Horizontal(id="confirm-buttons"):
                yield Button(f"Place order · {_price(self.total)}", id="place", variant="warning")
                yield Button("Cancel", id="cancel")

    def action_dismiss_modal(self) -> None:
        self.dismiss(None)

    @on(Button.Pressed, "#cancel")
    def cancel(self) -> None:
        self.dismiss(None)

    @on(Button.Pressed, "#place")
    def place(self) -> None:
        self.dismiss(str(self.query_one("#confirm-payment", Select).value))


class OrderStatusScreen(ModalScreen[bool]):
    """Shows the UPI QR, polls for payment and finalises the order.

    Dismisses with True once the order is placed, False otherwise.
    """

    DEFAULT_CSS = """
    OrderStatusScreen { align: center middle; }
    #order-box { width: 90%; height: 90%; border: thick $primary; background: $surface; padding: 1 2; }
    #qr-scroll { height: 1fr; }
    #qr { width: auto; height: auto; }
    #order-info { height: auto; margin-top: 1; }
    #order-buttons { height: 3; align-horizontal: right; }
    """

    BINDINGS = [("escape", "close", "Close")]

    def __init__(
        self,
        key: str,
        provider: BaseProvider,
        placement: OrderPlacement,
        total: float,
    ) -> None:
        super().__init__()
        self.key = key
        self.provider = provider
        self.placement = placement
        self.total = total
        self.placed = not placement.requires_payment
        self._finalising = False

    @property
    def _poll_kwargs(self) -> dict[str, Any]:
        paas_id = self.placement.raw.get("paasId") or self.placement.raw.get("paas_id")
        return {"paasId": str(paas_id)} if paas_id else {}

    @property
    def _can_poll(self) -> bool:
        return self.key != "swiggy" or bool(self._poll_kwargs)

    def compose(self) -> ComposeResult:
        placement = self.placement
        with Vertical(id="order-box"):
            yield Label(f"[b]{self.provider.display_name} order {placement.order_id}[/b]")
            if placement.requires_payment:
                data = placement.upi_qr_string or placement.payment_url
                with VerticalScroll(id="qr-scroll"):
                    if data:
                        yield Static(Text(render_qr(data), style="black on white", no_wrap=True), id="qr")
                    else:
                        yield Static("The provider didn't return a payment QR.", id="qr")
                info = f"Scan with any UPI app and pay [b]{_price(self.total)}[/b]."
                if placement.payment_url:
                    info += f"\nOr open: {placement.payment_url}"
            else:
                info = f"Order placed — pay {_price(self.total)} in cash on delivery."
            yield Static(info, id="order-info")
            yield Static("", id="order-state")
            with Horizontal(id="order-buttons"):
                if placement.requires_payment:
                    yield Button("Check now", id="check", disabled=not self._can_poll)
                yield Button("Close", id="close", variant="primary")

    def on_mount(self) -> None:
        if not self.placement.requires_payment:
            self._state("Order placed ✓")
            return
        if self._can_poll:
            self._state("Waiting for payment…")
            self.poll()
        else:
            # confirm_order on an unpaid UPI order marks it failed, so never guess.
            self._state(
                "Swiggy didn't return a payment id, so payment can't be tracked here. "
                "Pay, then check the order in the Swiggy app."
            )

    def _state(self, text: str) -> None:
        self.query_one("#order-state", Static).update(text)

    @work(exclusive=True)
    async def poll(self) -> None:
        placement = self.placement
        loop = asyncio.get_running_loop()
        deadline = loop.time() + (
            (placement.max_poll_time_ms / 1000) if placement.max_poll_time_ms else DEFAULT_POLL_SECONDS
        )
        interval = max(placement.poll_interval_ms / 1000, MIN_POLL_INTERVAL)
        while not self.placed and loop.time() < deadline:
            await asyncio.sleep(interval)
            if await self.check(quiet=True):
                return
        if not self.placed:
            self._state("Timed out waiting for payment. If you paid, press Check now.")

    async def check(self, quiet: bool = False) -> bool:
        """Check payment once; finalise on success. Returns True when settled."""
        if self.placed or self._finalising:
            return self.placed
        try:
            if self._can_poll:
                result = await self.provider.check_payment_status(
                    self.placement.order_id, **self._poll_kwargs
                )
                state, text = payment_state(result)
            else:
                return False
        except Exception as exc:
            if not quiet:
                self._state(f"Could not check payment: {exc}")
            return False
        if state == "pending":
            self._state(f"Waiting for payment… ({text})")
            return False
        if state == "failed":
            self._state(f"Payment {text.lower() or 'failed'}. The order was not placed.")
            return True
        return await self._finalise()

    async def _finalise(self) -> bool:
        self._finalising = True
        try:
            summary = await self.provider.confirm_order(self.placement.order_id)
        except Exception as exc:
            self._state(f"Payment received but confirming failed: {exc}")
            self._finalising = False
            return False
        if summary.status in (OrderStatus.FAILED, OrderStatus.CANCELLED):
            self._state(f"Order {summary.order_id} was not placed ({summary.status.value}).")
            return True
        self.placed = True
        self._state(f"Payment received — order {summary.order_id} placed ✓ ({summary.status.value})")
        return True

    @on(Button.Pressed, "#check")
    async def check_pressed(self) -> None:
        await self.check()

    @on(Button.Pressed, "#close")
    def close_pressed(self) -> None:
        self.action_close()

    def action_close(self) -> None:
        if not self.placed:
            self.notify("The order isn't confirmed yet.", severity="warning")
        self.dismiss(self.placed)
