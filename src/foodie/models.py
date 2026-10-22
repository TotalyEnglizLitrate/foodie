from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class ProviderName(str, Enum):
    SWIGGY = "swiggy"
    ZOMATO = "zomato"


class OrderStatus(str, Enum):
    PENDING_PAYMENT = "PENDING_PAYMENT"
    PLACED = "PLACED"
    CONFIRMED = "CONFIRMED"
    PREPARING = "PREPARING"
    OUT_FOR_DELIVERY = "OUT_FOR_DELIVERY"
    DELIVERED = "DELIVERED"
    CANCELLED = "CANCELLED"
    FAILED = "FAILED"


@dataclass
class Address:
    id: str
    label: str
    formatted_address: str
    latitude: float | None = None
    longitude: float | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class Restaurant:
    id: str
    name: str
    cuisines: list[str] = field(default_factory=list)
    rating: float | None = None
    delivery_time_mins: int | None = None
    distance_km: float | None = None
    cost_for_two: float | None = None
    is_open: bool = True
    image_url: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class MenuItem:
    id: str
    name: str
    price: float
    description: str = ""
    is_veg: bool = True
    in_stock: bool = True
    category: str = ""
    variants: list[dict[str, Any]] = field(default_factory=list)
    addons: list[dict[str, Any]] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class CartItem:
    item_id: str
    name: str
    price: float
    quantity: int = 1
    selected_variants: list[dict[str, Any]] = field(default_factory=list)
    selected_addons: list[dict[str, Any]] = field(default_factory=list)

    @property
    def subtotal(self) -> float:
        addons_total = sum(float(a.get("price", 0)) for a in self.selected_addons)
        return (self.price + addons_total) * self.quantity


@dataclass
class Cart:
    restaurant_id: str
    items: list[CartItem] = field(default_factory=list)
    item_total: float = 0.0
    delivery_fee: float = 0.0
    taxes: float = 0.0
    discount: float = 0.0
    grand_total: float = 0.0
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class PaymentMethod:
    id: str
    name: str
    method_type: str  # "COD", "UPI_INTENT", "UPI_QR", "WALLET", etc.
    details: dict[str, Any] = field(default_factory=dict)


@dataclass
class OrderPlacement:
    order_id: str
    status: OrderStatus
    requires_payment: bool = False
    upi_qr_string: str | None = None
    payment_url: str | None = None
    max_poll_time_ms: int | None = None
    poll_interval_ms: int = 5000
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class OrderTracking:
    order_id: str
    status: OrderStatus
    eta_mins: int | None = None
    status_message: str = ""
    delivery_partner_name: str | None = None
    delivery_partner_phone: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class OrderSummary:
    order_id: str
    restaurant_name: str
    status: OrderStatus
    total_amount: float
    items_summary: str = ""
    created_at: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)
