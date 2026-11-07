"""Menu items as a grid of photo cards, for terminals with Kitty or sixel graphics."""

from __future__ import annotations

import asyncio
import hashlib
import io
import logging
import os
import time
from collections.abc import Callable, Iterable
from datetime import timedelta
from math import log
from pathlib import Path

import httpx
from PIL import Image as PILImage
from platformdirs import user_state_dir
from textual.app import ComposeResult
from textual import on
from textual.binding import Binding
from textual.containers import Vertical, VerticalScroll
from textual.message import Message
from textual.widgets import Label
from textual_image._terminal import TerminalError, get_cell_size, probe_terminal
from textual_image.widget import SixelImage, TGPImage

from foodie.models import MenuItem

logger = logging.getLogger(__name__)

CARD_WIDTH = 30
"""Cells per card, including its border and gutter; sets how many fit across."""

PHOTO_WIDTH = CARD_WIDTH - 4
"""Cells for a photo: the card's border and padding take the rest."""

CARD_CHROME_ROWS = 4
"""Rows a card takes besides its photo: border, name and price."""

PHOTO_ROWS = range(4, 17)
DEFAULT_ASPECT = 4 / 3

MenuRow = tuple[MenuItem | None, MenuItem | None]


def photo_rows(aspects: Iterable[float]) -> int:
    """The photo height in rows that stretches the fewest photos.

    Each photo's aspect ratio is compared with the box's on a log scale, so a photo
    twice as wide as the box counts the same as one twice as tall. The box is always
    the full photo width; only its height varies.
    """
    ratios = list(aspects) or [DEFAULT_ASPECT]
    cell = get_cell_size()

    def stretch(rows: int) -> float:
        box = PHOTO_WIDTH * cell.width / (rows * cell.height)
        return sum(abs(log(box / ratio)) for ratio in ratios)

    return min(PHOTO_ROWS, key=stretch)


def graphics_protocol() -> str | None:
    """The image protocol to draw photos with: "tgp" (Kitty), "sixel", or None.

    TGP is preferred where both are reported, except under Zellij, which passes
    TGP through badly, so sixel is used there when available. The probe reads
    stdin, so it must run before Textual takes over the terminal.
    """
    try:
        caps = probe_terminal()
    except TerminalError:
        return None
    if caps.sixel and os.environ.get("ZELLIJ"):
        return "sixel"
    if caps.tgp:
        return "tgp"
    if caps.sixel:
        return "sixel"
    return None


def image_widget(protocol: str | None) -> type[TGPImage] | type[SixelImage]:
    """The image widget class for a protocol from graphics_protocol()."""
    return SixelImage if protocol == "sixel" else TGPImage


def photo_url(row: MenuRow) -> str | None:
    """The first photo a provider supplied for these items."""
    for item in row:
        if item is None:
            continue
        url = item.raw.get("imageUrl") or item.raw.get("image_url") or item.raw.get("image_link")
        if url:
            return str(url)
    return None


def _decode(data: bytes) -> PILImage.Image:
    image = PILImage.open(io.BytesIO(data))
    image.thumbnail((480, 480))
    return image.convert("RGB")


def photo_cache_dir() -> Path:
    """Where downloaded menu photos are kept between runs, per the platform's state convention."""
    return Path(user_state_dir("foodie")) / "photos"


def _cache_path(url: str) -> Path:
    return photo_cache_dir() / hashlib.sha256(url.encode()).hexdigest()


PHOTO_MAX_AGE = timedelta(days=30)
"""A cached photo unused for this long is deleted."""


def prune_photo_cache(max_age: timedelta = PHOTO_MAX_AGE) -> int:
    """Delete cached photos not used within max_age; returns how many were removed."""
    cutoff = time.time() - max_age.total_seconds()
    try:
        entries = list(photo_cache_dir().iterdir())
    except OSError:
        return 0
    removed = 0
    for path in entries:
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink()
                removed += 1
        except OSError as exc:
            logger.warning("Could not prune menu photo %s: %s", path.name, exc)
    logger.info("Pruned %d menu photo(s) unused for %s", removed, max_age)
    return removed


def _read_cached(path: Path) -> bytes | None:
    try:
        data = path.read_bytes()
        path.touch()  # mtime is the last use, so pruning keeps photos still in use
    except OSError:
        return None
    return data


def _write_cached(path: Path, data: bytes) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        # Write beside the target and rename, so a crash never leaves a partial photo behind.
        tmp = path.with_suffix(".tmp")
        tmp.write_bytes(data)
        tmp.replace(path)
    except OSError as exc:
        logger.warning("Could not cache menu photo %s: %s", path.name, exc)


async def _fetch(client: httpx.AsyncClient, url: str) -> PILImage.Image | None:
    path = _cache_path(url)
    data = await asyncio.to_thread(_read_cached, path)
    if data is None:
        try:
            response = await client.get(url)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            logger.warning("Menu photo %s failed: %r", url, exc)
            return None
        data = response.content
        await asyncio.to_thread(_write_cached, path, data)
    try:
        return await asyncio.to_thread(_decode, data)
    except (OSError, ValueError) as exc:
        logger.warning("Menu photo %s could not be decoded: %s", url, exc)
        path.unlink(missing_ok=True)
        return None


async def fetch_photos(
    urls: Iterable[str],
    on_photo: Callable[[str, PILImage.Image | None], None] | None = None,
) -> dict[str, PILImage.Image | None]:
    """Download and decode all photos at once; one that fails maps to None.

    `on_photo` is called as each photo finishes, so slow ones don't hold up the rest.
    """
    unique = list(dict.fromkeys(urls))
    photos: dict[str, PILImage.Image | None] = {}

    async def fetch_one(client: httpx.AsyncClient, url: str) -> None:
        photo = await _fetch(client, url)
        photos[url] = photo
        if on_photo is not None:
            on_photo(url, photo)

    async with httpx.AsyncClient(timeout=10, follow_redirects=True) as client:
        await asyncio.gather(*(fetch_one(client, url) for url in unique))
    return photos


class MenuCard(Vertical):
    DEFAULT_CSS = """
    MenuCard { width: 100%; border: round $panel; padding: 0 1; }
    MenuCard.-cursor { border: round $accent; }
    MenuCard .photo { width: 100%; }
    MenuCard Label { width: 100%; text-wrap: nowrap; text-overflow: ellipsis; }
    """

    class Clicked(Message):
        def __init__(self, card: MenuCard) -> None:
            super().__init__()
            self.card = card

    def __init__(
        self,
        row: MenuRow,
        name: str,
        prices: str,
        rows: int,
        image_cls: type[TGPImage] | type[SixelImage] = TGPImage,
    ) -> None:
        super().__init__()
        self.row = row
        self.photo_url = photo_url(row)
        self._name = name
        self._prices = prices
        self._photo = image_cls(classes="photo")
        self.set_photo_rows(rows)

    def set_photo_rows(self, rows: int) -> None:
        self.styles.height = rows + CARD_CHROME_ROWS
        self._photo.styles.height = rows

    def compose(self) -> ComposeResult:
        yield self._photo
        yield Label(self._name)
        yield Label(self._prices)

    def set_photo(self, photo: PILImage.Image | None) -> None:
        self._photo.image = photo

    def on_click(self) -> None:
        self.post_message(self.Clicked(self))


class MenuGrid(VerticalScroll, can_focus=True):
    DEFAULT_CSS = """
    MenuGrid { layout: grid; grid-columns: 1fr; grid-rows: auto; grid-gutter: 0 1; }
    """

    BINDINGS = [
        Binding("left", "step(-1)", "Previous", show=False),
        Binding("right", "step(1)", "Next", show=False),
        Binding("up", "row(-1)", "Up", show=False),
        Binding("down", "row(1)", "Down", show=False),
        Binding("enter", "select", "Add to cart"),
    ]

    class Selected(Message):
        def __init__(self, row: MenuRow) -> None:
            super().__init__()
            self.row = row

    def __init__(self, id: str | None = None) -> None:
        super().__init__(id=id)
        self.cards: list[MenuCard] = []
        self.cursor = 0
        self.columns = 1

    def on_resize(self) -> None:
        self.columns = max(1, self.size.width // CARD_WIDTH)
        self.styles.grid_size_columns = self.columns

    async def set_cards(self, cards: list[MenuCard]) -> None:
        await self.remove_children()
        self.cards = cards
        self.cursor = 0
        await self.mount_all(cards)
        self._highlight()

    def _move(self, delta: int) -> None:
        if self.cards:
            self.cursor = min(max(self.cursor + delta, 0), len(self.cards) - 1)
            self._highlight()

    def _highlight(self) -> None:
        for n, card in enumerate(self.cards):
            card.set_class(n == self.cursor, "-cursor")
        if self.cards:
            self.cards[self.cursor].scroll_visible()

    def action_step(self, delta: int) -> None:
        self._move(delta)

    def action_row(self, delta: int) -> None:
        self._move(delta * self.columns)

    def action_select(self) -> None:
        if self.cards:
            self.post_message(self.Selected(self.cards[self.cursor].row))

    @on(MenuCard.Clicked)
    def _card_clicked(self, event: MenuCard.Clicked) -> None:
        event.stop()
        if event.card in self.cards:
            self.cursor = self.cards.index(event.card)
            self._highlight()
            self.action_select()
