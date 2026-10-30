"""A modal "working…" popup shown while waiting on a provider."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress

from textual.app import App, ComposeResult
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import LoadingIndicator, Static

# Requests faster than this never show the popup, so quick calls don't flicker.
SHOW_AFTER_SECONDS = 0.25


class BusyScreen(ModalScreen[None]):
    DEFAULT_CSS = """
    BusyScreen { align: center middle; }
    #busy-box { width: 56; height: auto; border: round $primary; background: $surface; padding: 1 2; }
    #busy-box LoadingIndicator { height: 3; }
    #busy-message { text-align: center; width: 100%; }
    """

    def __init__(self, message: str) -> None:
        super().__init__()
        self.message = message

    def compose(self) -> ComposeResult:
        with Vertical(id="busy-box"):
            yield LoadingIndicator()
            yield Static(self.message, id="busy-message")

    def set_message(self, message: str) -> None:
        self.message = message
        if self.is_mounted:
            self.query_one("#busy-message", Static).update(message)


class Busy:
    """Reference-counted popup: overlapping requests share one screen."""

    def __init__(self, app: App[object]) -> None:
        self.app = app
        self._messages: list[str] = []
        self._screen: BusyScreen | None = None
        self._pending: asyncio.Task[None] | None = None

    @asynccontextmanager
    async def __call__(
        self, message: str, delay: float = SHOW_AFTER_SECONDS
    ) -> AsyncIterator[None]:
        self._messages.append(message)
        if delay <= 0:
            self._show_now()
        else:
            self._refresh()
        try:
            yield
        finally:
            self._messages.remove(message)
            await self._refresh_async()

    def _show_now(self) -> None:
        """Show the popup immediately, e.g. at startup when the screen is still empty."""
        if self._pending is not None:
            self._pending.cancel()
            self._pending = None
        if self._screen is None:
            self._screen = BusyScreen(self._messages[-1])
            self.app.push_screen(self._screen)
        else:
            self._screen.set_message(self._messages[-1])

    def _refresh(self) -> None:
        if self._screen is not None:
            self._screen.set_message(self._messages[-1])
        elif self._pending is None:
            self._pending = asyncio.create_task(self._show_later())

    async def _show_later(self) -> None:
        try:
            await asyncio.sleep(SHOW_AFTER_SECONDS)
            if self._messages and self._screen is None:
                self._screen = BusyScreen(self._messages[-1])
                self.app.push_screen(self._screen)
        finally:
            self._pending = None

    async def _refresh_async(self) -> None:
        if self._messages:
            self._refresh()
            return
        if self._pending is not None:
            self._pending.cancel()
            with suppress(asyncio.CancelledError):
                await self._pending
            self._pending = None
        screen, self._screen = self._screen, None
        # dismiss() pops the top screen, and this modal blocks anything being
        # pushed over it, so it is the one on top here.
        if screen is not None and self.app.screen is screen:
            await screen.dismiss()
