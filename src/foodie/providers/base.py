from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from foodie.models import (
    Address,
    Cart,
    CartItem,
    MenuItem,
    OrderPlacement,
    OrderSummary,
    OrderTracking,
    PaymentMethod,
    ProviderName,
    Restaurant,
)


class BaseProvider(ABC):
    """
    Abstract Base Class for food ordering providers (e.g. Swiggy, Zomato).

    Defines a unified async interface for authentication, restaurant discovery,
    cart management, checkout, and live order tracking.
    """

    @property
    @abstractmethod
    def name(self) -> ProviderName:
        """Unique provider identifier (e.g. ProviderName.SWIGGY, ProviderName.ZOMATO)."""
        ...

    @property
    @abstractmethod
    def display_name(self) -> str:
        """Human-readable provider name (e.g. 'Swiggy', 'Zomato')."""
        ...

    # --- Authentication Lifecycle ---

    @abstractmethod
    async def is_authenticated(self) -> bool:
        """Check if an active, valid authentication session exists."""
        ...

    @abstractmethod
    async def get_auth_url(self, redirect_uri: str) -> tuple[str, str]:
        """
        Generate PKCE challenge and return (authorization_url, code_verifier).
        The caller opens authorization_url in the browser for OTP verification.
        """
        ...

    @abstractmethod
    async def complete_auth(self, code: str, verifier: str, redirect_uri: str) -> bool:
        """
        Exchange the authorization code and PKCE verifier for session tokens.
        Persists the tokens to local secure storage.
        """
        ...

    @abstractmethod
    async def refresh_auth(self) -> bool:
        """Silently refresh an expired access token using the stored refresh token."""
        ...

    @abstractmethod
    async def logout(self) -> None:
        """Revoke and delete all stored credentials and session tokens."""
        ...

    @abstractmethod
    async def get_addresses(self) -> list[Address]:
        """Fetch saved delivery addresses for the authenticated user."""
        ...

    @abstractmethod
    async def search_restaurants(self, query: str, address_id: str) -> list[Restaurant]:
        """Search for restaurants matching a query near a specific saved address."""
        ...

    @abstractmethod
    async def get_menu(
        self, restaurant_id: str, address_id: str | None = None
    ) -> list[MenuItem]:
        """Retrieve the menu items available at a specific restaurant."""
        ...

    @abstractmethod
    async def search_menu(
        self, restaurant_id: str, query: str, address_id: str | None = None
    ) -> list[MenuItem]:
        """Search for specific dishes or keywords within a restaurant's menu."""
        ...

    @abstractmethod
    async def get_cart(self, address_id: str | None = None) -> Cart:
        """Get current cart contents and pricing breakdown."""
        ...

    @abstractmethod
    async def update_cart(
        self,
        restaurant_id: str,
        items: list[CartItem],
        address_id: str,
        payment_method: str | None = None,
    ) -> Cart:
        """
        Add, remove, or update items in the cart.
        Note: Carts are tied to a single restaurant.
        """
        ...

    @abstractmethod
    async def clear_cart(self) -> None:
        """Clear all items from the active cart."""
        ...

    @abstractmethod
    async def apply_coupon(self, code: str, address_id: str | None = None) -> Cart:
        """Apply a discount coupon code to the current cart and return updated totals."""
        ...

    @abstractmethod
    async def get_payment_options(self, address_id: str) -> list[PaymentMethod]:
        """Fetch available payment methods (e.g. COD, UPI Intent, UPI QR)."""
        ...

    @abstractmethod
    async def place_order(
        self, payment_method: str, address_id: str, **kwargs: Any
    ) -> OrderPlacement:
        """
        Place the order. For COD, order is placed immediately.
        For UPI, returns pending state with payment QR / URL for terminal display.
        """
        ...

    @abstractmethod
    async def check_payment_status(self, order_id: str, **kwargs: Any) -> dict[str, Any]:
        """Check payment status iteration for in-flight UPI payments."""
        ...

    @abstractmethod
    async def confirm_order(self, order_id: str, **kwargs: Any) -> OrderSummary:
        """Confirm and finalize the order after payment has succeeded."""
        ...

    @abstractmethod
    async def get_orders(self, address_id: str | None = None) -> list[OrderSummary]:
        """Fetch order history for the user."""
        ...

    @abstractmethod
    async def track_order(self, order_id: str) -> OrderTracking:
        """Track delivery progress, ETA, and delivery partner status for an active order."""
        ...
