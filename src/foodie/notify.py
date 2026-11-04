"""Desktop notifications for order status changes."""

from __future__ import annotations

import logging

from desktop_notifier import DesktopNotifier, Urgency

from foodie.models import OrderStatus

logger = logging.getLogger(__name__)

APP_NAME = "Foodie"

STATUS_TEXT = {
    OrderStatus.PENDING_PAYMENT: "Waiting for payment",
    OrderStatus.PLACED: "Order placed",
    OrderStatus.CONFIRMED: "Confirmed by the restaurant",
    OrderStatus.PREPARING: "Being prepared",
    OrderStatus.OUT_FOR_DELIVERY: "Out for delivery",
    OrderStatus.DELIVERED: "Delivered",
    OrderStatus.CANCELLED: "Cancelled",
    OrderStatus.FAILED: "Failed",
}

URGENT = {OrderStatus.CANCELLED, OrderStatus.FAILED}


def make_notifier() -> DesktopNotifier:
    return DesktopNotifier(app_name=APP_NAME)


async def send_order_update(
    notifier: DesktopNotifier, provider: str, restaurant: str, status: OrderStatus
) -> None:
    """Show one order update. Failures are logged, never raised, so polling carries on."""
    urgency = Urgency.Critical if status in URGENT else Urgency.Normal
    try:
        await notifier.send(
            title=f"{restaurant} · {provider}",
            message=STATUS_TEXT[status],
            urgency=urgency,
        )
    except Exception:
        logger.warning("Could not show order notification", exc_info=True)
