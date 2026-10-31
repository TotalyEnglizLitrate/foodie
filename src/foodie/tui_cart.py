"""Cart-related Textual screens: the customisation modal and the (dual) cart."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, cast

from textual import on, work
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen, Screen
from textual.widget import Widget
from textual.widgets import (
    Button,
    DataTable,
    Footer,
    Header,
    Input,
    Label,
    OptionList,
    RadioButton,
    RadioSet,
    Select,
    SelectionList,
    Static,
    TabbedContent,
    TabPane,
)
from textual.widgets.option_list import Option
from textual.widgets.selection_list import Selection

from foodie import customize
from foodie.customize import CartLine, OptionGroup
from foodie.models import Cart, CartCoupon, MenuItem, Restaurant
from foodie.tui_order import PAYMENT_LABELS, OrderConfirmModal, OrderStatusScreen, place_order

if TYPE_CHECKING:
    from foodie.tui import FoodieTui

PROVIDERS = ("swiggy", "zomato")
TITLES = {"swiggy": "Swiggy", "zomato": "Zomato"}
PAYMENT_OPTIONS = [(label, value) for value, label in PAYMENT_LABELS.items()]


logger = logging.getLogger(__name__)


def _price(value: float | None) -> str:
    return f"₹{value:,.0f}" if value is not None else "—"


@dataclass
class ProviderContext:
    """What the modal needs to customise an item for one provider."""

    item: MenuItem
    restaurant_id: str
    address_id: str


@dataclass
class CartState:
    restaurant_name: str = ""
    restaurants: dict[str, Restaurant] = field(default_factory=dict)
    addresses: dict[str, str] = field(default_factory=dict)
    lines: list[CartLine] = field(default_factory=list)

    def start(
        self, name: str, restaurants: dict[str, Restaurant], addresses: dict[str, str]
    ) -> None:
        """Bind the cart to a restaurant, dropping lines from any other one."""
        same = {k: r.id for k, r in restaurants.items()} == {
            k: r.id for k, r in self.restaurants.items()
        }
        if not same:
            self.lines = []
        self.restaurant_name = name
        self.restaurants = restaurants
        self.addresses = addresses

    def covering(self, lines: list[CartLine] | None = None) -> list[str]:
        """Providers that can fulfil every line."""
        lines = self.lines if lines is None else lines
        return [
            key
            for key in PROVIDERS
            if key in self.restaurants and all(key in line.entries for line in lines)
        ]


class OptionsPanel(VerticalScroll):
    """Variant and add-on pickers for one provider's version of an item."""

    DEFAULT_CSS = """
    OptionsPanel { height: 1fr; }
    OptionsPanel Label { margin-top: 1; text-style: bold; }
    OptionsPanel .note { color: $text-muted; }
    """

    def __init__(self, key: str, ctx: ProviderContext, provider: object) -> None:
        super().__init__()
        self.key = key
        self.ctx = ctx
        self.provider = provider
        self.item = ctx.item
        self.ready = False
        self._variant_widgets: list[tuple[OptionGroup, Widget, int]] = []
        self._addon_widgets: list[tuple[OptionGroup, Widget, int]] = []
        self._last_selected: dict[Widget, set[str]] = {}

    def compose(self) -> ComposeResult:
        yield Static("Loading options…", classes="note", id=f"{self.key}-note")
        yield Vertical(id=f"{self.key}-variants")
        yield Vertical(id=f"{self.key}-addons")
        yield Static("", id=f"{self.key}-price")

    async def on_mount(self) -> None:
        for box in ("variants", "addons"):
            self.query_one(f"#{self.key}-{box}", Vertical).styles.height = "auto"
        note = self.query_one(f"#{self.key}-note", Static)
        if customize.needs_details(self.key, self.item):
            try:
                results = await cast("FoodieTui", self.app).providers[self.key].search_menu(
                    self.ctx.restaurant_id, self.item.name, self.ctx.address_id
                )
                match = next((m for m in results if m.id == self.item.id), None)
            except Exception as exc:
                logger.warning("on_mount failed: %s", exc, exc_info=True)
                note.update(f"Could not load options: {exc}")
                return
            if match is None:
                note.update("Could not load this item's options.")
                return
            self.item = match
        await self._mount_groups(
            self._variant_widgets, customize.variant_groups(self.key, self.item), "variants"
        )
        await self._refresh_addons()
        has_options = bool(self._variant_widgets or self._addon_widgets)
        note.update("" if has_options else "No customisation options.")
        self.ready = True
        self._update_price()

    def _group_widget(self, group: OptionGroup) -> tuple[Widget, int]:
        if group.max_select == 1:
            offset = 0 if group.min_select >= 1 else 1
            defaults = [i for i, o in enumerate(group.options) if o.default]
            pick = defaults[0] if defaults else (0 if group.min_select >= 1 else -1)
            buttons = [RadioButton(self._label(group, o), value=(i == pick)) for i, o in enumerate(group.options)]
            if offset:
                buttons.insert(0, RadioButton("None", value=pick == -1))
            return RadioSet(*buttons), offset
        widget = SelectionList[str](
            *(Selection(self._label(group, o), o.id, o.default) for o in group.options)
        )
        self._last_selected[widget] = {o.id for o in group.options if o.default}
        return widget, 0

    @staticmethod
    def _label(group: OptionGroup, option: customize.Option) -> str:
        if option.price is None:
            return option.name
        return f"{option.name} ({'+' if group.kind == 'addon' else ''}{_price(option.price)})"

    async def _mount_groups(
        self,
        store: list[tuple[OptionGroup, Widget, int]],
        groups: list[OptionGroup],
        box: str,
    ) -> None:
        container = self.query_one(f"#{self.key}-{box}", Vertical)
        await container.remove_children()
        store.clear()
        widgets: list[Widget] = []
        for group in groups:
            widget, offset = self._group_widget(group)
            widget.styles.height = "auto"
            store.append((group, widget, offset))
            rule = "required" if group.min_select else "optional"
            if group.max_select > 1:
                rule += f", up to {group.max_select}"
            widgets.extend([Label(f"{group.name} ({rule})"), widget])
        if widgets:
            await container.mount(*widgets)

    async def _refresh_addons(self) -> None:
        groups = customize.addon_groups(self.key, self.item, self.chosen())
        await self._mount_groups(self._addon_widgets, groups, "addons")

    def chosen(self) -> dict[str, list[str]]:
        out: dict[str, list[str]] = {}
        for group, widget, offset in self._variant_widgets + self._addon_widgets:
            if isinstance(widget, RadioSet):
                index = widget.pressed_index - offset if widget.pressed_index >= 0 else -1
                out[group.key] = (
                    [group.options[index].id] if 0 <= index < len(group.options) else []
                )
            else:
                out[group.key] = [str(v) for v in cast(SelectionList[str], widget).selected]
        return out

    def error(self) -> str | None:
        if not self.ready:
            return f"{TITLES[self.key]} options aren't available."
        return customize.validate(self.key, self.item, self.chosen())

    def _update_price(self) -> None:
        price = customize.unit_price(self.key, self.item, self.chosen())
        error = customize.validate(self.key, self.item, self.chosen())
        text = f"\nEstimated price each: {_price(price)}"
        if error:
            text += f"\n[b]{error}[/b]"
        self.query_one(f"#{self.key}-price", Static).update(text)

    @on(RadioSet.Changed)
    async def radio_changed(self, event: RadioSet.Changed) -> None:
        if not self.ready:
            return
        if any(w is event.radio_set for _, w, _ in self._variant_widgets):
            await self._refresh_addons()
        self._update_price()

    @on(SelectionList.SelectedChanged)
    def selection_changed(self, event: SelectionList.SelectedChanged) -> None:
        widget = event.selection_list
        group = next((g for g, w, _ in self._addon_widgets if w is widget), None)
        if group is None:
            return
        selected = {str(v) for v in widget.selected}
        added = selected - self._last_selected.get(widget, set())
        if group.max_select and len(selected) > group.max_select:
            for value in added:
                widget.deselect(value)
            selected -= added
            self.notify(f"“{group.name}” allows at most {group.max_select}.", severity="warning")
        self._last_selected[widget] = selected
        self._update_price()

    def customization(self, quantity: int) -> customize.Customization:
        return customize.build(self.key, self.item, quantity, self.chosen())


class AddItemModal(ModalScreen["CartLine | None"]):
    DEFAULT_CSS = """
    AddItemModal { align: center middle; }
    #dialog { width: 90%; height: 90%; border: thick $primary; background: $surface; padding: 1 2; }
    #provider-tabs { height: 1fr; }
    #provider-tabs TabPane { padding: 0; }
    #qty-row { height: 3; }
    #qty-row Label { padding: 1 1 0 0; }
    #qty { width: 12; }
    #modal-error { height: auto; color: $error; }
    #modal-buttons { height: 3; align-horizontal: right; }
    """

    BINDINGS = [("escape", "dismiss_modal", "Cancel")]

    def __init__(self, name: str, contexts: dict[str, ProviderContext]) -> None:
        super().__init__()
        self.item_name = name
        self.contexts = contexts
        self.panels: dict[str, OptionsPanel] = {}

    def compose(self) -> ComposeResult:
        providers = cast("FoodieTui", self.app).providers
        for key, ctx in self.contexts.items():
            self.panels[key] = OptionsPanel(key, ctx, providers[key])
        with Vertical(id="dialog"):
            yield Label(f"[b]{self.item_name}[/b]")
            with Horizontal(id="qty-row"):
                yield Label("Quantity")
                yield Input("1", id="qty", type="integer")
            if len(self.panels) > 1:
                with TabbedContent(id="provider-tabs"):
                    for key, panel in self.panels.items():
                        with TabPane(TITLES[key], id=f"tab-{key}"):
                            yield panel
            else:
                yield from self.panels.values()
            yield Static("", id="modal-error")
            with Horizontal(id="modal-buttons"):
                yield Button("Add to cart", id="add", variant="primary")
                yield Button("Cancel", id="cancel")

    def action_dismiss_modal(self) -> None:
        self.dismiss(None)

    @on(Button.Pressed, "#cancel")
    def cancel(self) -> None:
        self.dismiss(None)

    @on(Button.Pressed, "#add")
    def add(self) -> None:
        error = self.query_one("#modal-error", Static)
        try:
            quantity = int(self.query_one("#qty", Input).value)
            if quantity < 1:
                raise ValueError
        except ValueError:
            error.update("Quantity must be a whole number of at least 1.")
            return
        for key, panel in self.panels.items():
            problem = panel.error()
            if problem:
                error.update(f"{TITLES[key]}: {problem}")
                if len(self.panels) > 1:
                    self.query_one(TabbedContent).active = f"tab-{key}"
                return
        entries = {key: panel.customization(quantity) for key, panel in self.panels.items()}
        self.dismiss(CartLine(self.item_name, quantity, entries))


class CouponModal(ModalScreen["str | None"]):
    """Pick or type a coupon. Dismisses with the code, "" to remove, or None to cancel."""

    DEFAULT_CSS = """
    CouponModal { align: center middle; }
    #coupon-dialog { width: 80; height: auto; max-height: 90%; border: thick $primary; background: $surface; padding: 1 2; }
    #coupon-list { height: auto; max-height: 14; margin: 1 0; }
    #coupon-buttons { height: 3; align-horizontal: right; margin-top: 1; }
    """

    BINDINGS = [("escape", "dismiss_modal", "Cancel")]

    def __init__(self, provider_name: str, coupons: list[CartCoupon], applied: str | None) -> None:
        super().__init__()
        self.provider_name = provider_name
        self.coupons = coupons
        self.applied = applied

    def compose(self) -> ComposeResult:
        with Vertical(id="coupon-dialog"):
            yield Label(f"[b]{self.provider_name} coupons[/b]")
            if self.applied:
                yield Static(f"Applied: [b]{self.applied}[/b]")
            if self.coupons:
                yield OptionList(
                    *(
                        Option(self._prompt(c), id=c.code, disabled=not c.applicable)
                        for c in self.coupons
                    ),
                    id="coupon-list",
                )
            else:
                yield Static("No coupons were listed for this cart. You can still enter a code.", id="coupon-none")
            yield Input(placeholder="Coupon code", id="coupon-code")
            with Horizontal(id="coupon-buttons"):
                yield Button("Apply", id="coupon-apply", variant="primary")
                if self.applied:
                    yield Button("Remove", id="coupon-remove")
                yield Button("Cancel", id="coupon-cancel")

    @staticmethod
    def _prompt(coupon: CartCoupon) -> str:
        text = f"[b]{coupon.code}[/b]" + (" ★" if coupon.recommended else "")
        detail = coupon.title or coupon.description
        if detail:
            text += f" — {detail}"
        if coupon.discount:
            text += f" (save {_price(coupon.discount)})"
        if not coupon.applicable:
            text += " [i](not applicable)[/i]"
        return text

    def action_dismiss_modal(self) -> None:
        self.dismiss(None)

    @on(OptionList.OptionSelected, "#coupon-list")
    def picked(self, event: OptionList.OptionSelected) -> None:
        self.query_one("#coupon-code", Input).value = event.option.id or ""

    @on(Input.Submitted, "#coupon-code")
    @on(Button.Pressed, "#coupon-apply")
    def apply_code(self) -> None:
        code = self.query_one("#coupon-code", Input).value.strip()
        if code:
            self.dismiss(code)
        else:
            self.notify("Pick a coupon or type a code.", severity="warning")

    @on(Button.Pressed, "#coupon-remove")
    def remove_coupon(self) -> None:
        self.dismiss("")

    @on(Button.Pressed, "#coupon-cancel")
    def cancel_coupon(self) -> None:
        self.dismiss(None)


class CartPanel(Vertical):
    DEFAULT_CSS = """
    CartPanel { width: 1fr; border: round $primary; padding: 0 1; }
    CartPanel DataTable { height: 1fr; }
    CartPanel .totals { height: auto; padding: 1 0 0 0; }
    CartPanel .panel-buttons { height: 3; }
    """

    def __init__(self, key: str, title: str) -> None:
        super().__init__(id=f"panel-{key}")
        self.key = key
        self.title = title

    def compose(self) -> ComposeResult:
        yield Label(f"[b]{self.title}[/b]")
        table = DataTable(id=f"lines-{self.key}")
        table.cursor_type = "row"
        table.add_columns("Item", "Options", "Qty", "Est.")
        yield table
        yield Static("Pricing…", classes="totals", id=f"totals-{self.key}")
        with Horizontal(classes="panel-buttons"):
            # Zomato's MCP server has no coupon tool; it applies offers on its own.
            if self.key == "swiggy":
                yield Button("Coupon", id=f"coupon-{self.key}")
            yield Button(f"Order with {self.title}", id=f"order-{self.key}", variant="warning")


class CartScreen(Screen[None]):
    CSS = """
    #payment-row { height: 3; }
    #payment-row Label { padding: 1 1 0 1; }
    #payment { width: 36; }
    #cart-status { height: 2; padding: 0 1; }
    #panels { height: 1fr; }
    """

    BINDINGS = [
        ("escape", "app.pop_screen", "Back"),
        ("plus,equals_sign", "qty(1)", "Qty +"),
        ("minus", "qty(-1)", "Qty −"),
        ("d", "remove", "Remove"),
        ("x", "clear", "Clear cart"),
        ("r", "sync", "Re-price"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self.payment: str | None = None
        self.providers: list[str] = []
        self.priced: dict[str, Cart] = {}
        self.coupons: dict[str, str] = {}  # applied code per provider

    @property
    def foodie(self) -> "FoodieTui":
        return cast("FoodieTui", self.app)

    def compose(self) -> ComposeResult:
        cart = self.foodie.cart
        self.providers = cart.covering()
        yield Header()
        # One payment choice for every provider: Zomato needs it to price the
        # cart, and placing an order with either provider uses it.
        with Horizontal(id="payment-row"):
            yield Label(f"Pay for {' & '.join(TITLES[k] for k in self.providers)} with")
            select = Select(
                PAYMENT_OPTIONS,
                prompt="Choose a payment type",
                id="payment",
                allow_blank=True,
            )
            if self.payment:
                select.value = self.payment
            yield select
        yield Static("", id="cart-status")
        with Horizontal(id="panels"):
            for key in self.providers:
                yield CartPanel(key, TITLES[key])
        yield Footer()

    async def on_mount(self) -> None:
        await self._refresh()

    def _title(self) -> str:
        cart = self.foodie.cart
        if len(self.providers) > 1:
            return f"Dual cart — {cart.restaurant_name}"
        if self.providers:
            return f"{TITLES[self.providers[0]]} cart — {cart.restaurant_name} (only provider with every item)"
        return "Cart"

    async def _refresh(self) -> None:
        self.sub_title = self._title()
        self._fill_tables()
        await self._sync()

    def _fill_tables(self) -> None:
        for key in self.providers:
            table = self.query_one(f"#lines-{key}", DataTable)
            table.clear()
            for line in self.foodie.cart.lines:
                entry = line.entries[key]
                unit = entry.cart_item.price + sum(
                    float(a.get("price") or 0) for a in entry.cart_item.selected_addons
                )
                table.add_row(line.name, entry.summary or "—", str(line.quantity), _price(unit * line.quantity))

    @on(Select.Changed, "#payment")
    async def payment_changed(self, event: Select.Changed) -> None:
        self.payment = None if event.value is Select.BLANK else str(event.value)
        await self._sync(only="zomato")

    async def action_sync(self) -> None:
        await self._sync()

    async def _sync(self, only: str | None = None) -> None:
        cart = self.foodie.cart
        status = self.query_one("#cart-status", Static)
        if not cart.lines:
            status.update("Cart is empty.")
            for key in self.providers:
                self.query_one(f"#totals-{key}", Static).update("")
            return
        status.update("Pricing cart with the providers…")

        async def price(key: str) -> None:
            totals = self.query_one(f"#totals-{key}", Static)
            self.priced.pop(key, None)
            if key == "zomato" and self.payment is None:
                totals.update("Choose a payment type above to price this cart.")
                return
            try:
                result = await self.foodie.providers[key].update_cart(
                    cart.restaurants[key].id,
                    [line.entries[key].cart_item for line in cart.lines],
                    cart.addresses[key],
                    payment_method=self.payment if key == "zomato" else None,
                )
                code = self.coupons.get(key)
                if code:
                    # update_cart rebuilds the cart, so put the coupon back.
                    try:
                        reapplied = await self.foodie.providers[key].apply_coupon(
                            code, cart.addresses[key]
                        )
                        if reapplied.discount > 0:
                            result = reapplied
                        else:
                            self.coupons.pop(key, None)
                    except Exception as exc:
                        logger.debug("%s: re-applying coupon %s failed: %s", key, code, exc)
                        self.coupons.pop(key, None)
                self.priced[key] = result
                totals.update(self._totals(result, self.coupons.get(key)))
            except Exception as exc:
                logger.warning("price failed: %s", exc, exc_info=True)
                totals.update(f"[b]{TITLES[key]} could not price the cart:[/b] {exc}")

        async with self.foodie.busy("Pricing your cart with the providers…"):
            await asyncio.gather(*(price(k) for k in self.providers if only in (None, k)))
        status.update("Prices above come from each provider's live cart. Use the Order button to place an order with one provider.")

    @staticmethod
    def _totals(cart: Cart, coupon: str | None = None) -> str:
        rows = [("Items", cart.item_total), ("Delivery", cart.delivery_fee)]
        if cart.fees:
            rows.append(("Fees", cart.fees))
        rows.append(("Taxes", cart.taxes))
        if cart.discount:
            rows.append(("Discount", -cart.discount))
        lines = [f"{label:<10}{_price(value):>10}" for label, value in rows]
        lines.append(f"[b]{'Total':<10}{_price(cart.grand_total):>10}[/b]")
        # Zomato reports offers it applied itself in promo_code.
        promo = coupon or cart.raw.get("promo_code")
        if promo:
            lines.append(f"Coupon: {promo}")
        return "\n".join(lines)

    @on(Button.Pressed, "#panels Button")
    def panel_button(self, event: Button.Pressed) -> None:
        action, _, key = (event.button.id or "").partition("-")
        if action == "order":
            self.order(key)
        elif action == "coupon":
            self.coupon(key)

    @work(exclusive=True, group="coupon")
    async def coupon(self, key: str) -> None:
        cart = self.foodie.cart
        priced = self.priced.get(key)
        if priced is None:
            self.notify(f"Price the {TITLES[key]} cart first.", severity="warning")
            return
        provider = self.foodie.providers[key]
        address_id = cart.addresses[key]
        try:
            async with self.foodie.busy(f"Fetching {TITLES[key]} coupons…"):
                available = await provider.get_cart_coupons(cart.restaurants[key].id, address_id, priced)
        except Exception as exc:
            logger.warning("coupon failed: %s", exc, exc_info=True)
            available = []
            self.notify(f"Could not list coupons: {exc}", severity="warning")
        code = await self.app.push_screen_wait(
            CouponModal(TITLES[key], available, self.coupons.get(key))
        )
        if code is None:
            return
        if code == "":
            self.coupons.pop(key, None)
            await self._sync(only=key)
            self.notify("Coupon removed and the cart re-priced.")
            return
        try:
            async with self.foodie.busy(f"Applying {code}…"):
                result = await provider.apply_coupon(code, address_id)
        except Exception as exc:
            logger.warning("coupon failed: %s", exc, exc_info=True)
            self.notify(f"{TITLES[key]} rejected {code}: {exc}", severity="error", timeout=10)
            return
        if result.discount <= 0:
            # An auto-suggested coupon shows no discount; don't claim it was applied.
            self.notify(f"{code} gave no discount on this cart, so it wasn't applied.", severity="warning")
            return
        self.coupons[key] = code
        self.priced[key] = result
        self.query_one(f"#totals-{key}", Static).update(self._totals(result, code))
        self.notify(f"{code} applied — saving {_price(result.discount)}.")

    @work(exclusive=True)
    async def order(self, key: str) -> None:
        cart = self.foodie.cart
        priced = self.priced.get(key)
        if priced is None:
            self.notify(
                f"Price the {TITLES[key]} cart first"
                + (" (choose a payment type)." if key == "zomato" else "."),
                severity="warning",
            )
            return
        provider = self.foodie.providers[key]
        address_id = cart.addresses[key]
        payment = self.payment
        if payment is None:
            self.notify("Choose a payment type first.", severity="warning")
            return
        if key == "swiggy":
            # Swiggy lists what this cart can actually pay with; Zomato was priced for it.
            try:
                async with self.foodie.busy("Checking Swiggy's payment options…"):
                    methods = await provider.get_payment_options(address_id)
            except Exception as exc:
                logger.warning("order failed: %s", exc, exc_info=True)
                self.notify(f"Could not load payment options: {exc}", severity="error")
                return
            offered = {
                "upi" if m.method_type == "UPI_QR" else "cash"
                for m in methods
                if m.method_type in ("UPI_QR", "COD")
            }
            if payment not in offered:
                self.notify(
                    f"Swiggy doesn't offer “{PAYMENT_LABELS[payment]}” for this cart.",
                    severity="error",
                )
                return
        lines = [
            f"{line.quantity} × {line.name}" + (f" ({line.entries[key].summary})" if line.entries[key].summary else "")
            for line in cart.lines
        ]
        choice = await self.app.push_screen_wait(
            OrderConfirmModal(
                TITLES[key],
                cart.restaurant_name,
                self.foodie.address_labels.get(address_id, address_id),
                lines,
                priced.grand_total,
                payment,
            )
        )
        if choice is None:
            return
        self.query_one("#cart-status", Static).update(f"Placing your {TITLES[key]} order…")
        try:
            async with self.foodie.busy(f"Placing your {TITLES[key]} order…"):
                placement = await place_order(key, provider, choice, address_id)
        except Exception as exc:
            logger.warning("order failed: %s", exc, exc_info=True)
            self.notify(f"{TITLES[key]} couldn't place the order: {exc}", severity="error", timeout=10)
            self.query_one("#cart-status", Static).update("Order not placed.")
            return
        placed = await self.app.push_screen_wait(
            OrderStatusScreen(key, provider, placement, priced.grand_total)
        )
        if placed:
            cart.lines.clear()
            self.app.pop_screen()
            self.notify(f"{TITLES[key]} order {placement.order_id} placed. Press o to track it.")
        else:
            self.query_one("#cart-status", Static).update(
                f"{TITLES[key]} order {placement.order_id} is not confirmed — check the {TITLES[key]} app."
            )

    def _cursor_index(self) -> int | None:
        focused = self.focused
        table = focused if isinstance(focused, DataTable) else (
            self.query_one(f"#lines-{self.providers[0]}", DataTable) if self.providers else None
        )
        if table is None or table.row_count == 0:
            return None
        return table.cursor_row

    async def action_qty(self, delta: int) -> None:
        index = self._cursor_index()
        lines = self.foodie.cart.lines
        if index is None or not 0 <= index < len(lines):
            return
        line = lines[index]
        line.quantity = max(1, line.quantity + delta)
        for entry in line.entries.values():
            entry.cart_item.quantity = line.quantity
        await self._refresh()
        self._restore_cursor(index)

    async def action_remove(self) -> None:
        index = self._cursor_index()
        cart = self.foodie.cart
        if index is None or not 0 <= index < len(cart.lines):
            return
        del cart.lines[index]
        await self._after_removal()

    async def action_clear(self) -> None:
        self.foodie.cart.lines.clear()
        await self._after_removal()

    async def _after_removal(self) -> None:
        cart = self.foodie.cart
        if not cart.lines:
            await self._clear_remote()
            self.app.pop_screen()
            self.notify("Cart is empty.")
            return
        # Removing an item can make the other provider eligible, so rebuild the panels.
        await self.recompose()
        await self._refresh()

    async def _clear_remote(self) -> None:
        cart = self.foodie.cart
        await asyncio.gather(
            *(self.foodie.providers[k].clear_cart() for k in cart.restaurants),
            return_exceptions=True,
        )

    def _restore_cursor(self, index: int) -> None:
        for key in self.providers:
            table = self.query_one(f"#lines-{key}", DataTable)
            if table.row_count:
                table.move_cursor(row=min(index, table.row_count - 1))
