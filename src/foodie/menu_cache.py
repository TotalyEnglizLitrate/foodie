"""On-disk cache of menus, valid while the provider reports the same category count."""

from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path
from typing import Any

from platformdirs import user_state_dir

logger = logging.getLogger(__name__)


def _path(key: str) -> Path:
    return Path(user_state_dir("foodie")) / "menus" / hashlib.sha256(key.encode()).hexdigest()


def load(key: str, total: int) -> list[dict[str, Any]] | None:
    """The cached items for `key`, or None if absent or stale.

    A cached menu whose category count differs from `total` is evicted at once.
    """
    path = _path(key)
    try:
        entry = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    if entry.get("total") != total:
        logger.info("Menu cache evicted for %s: categories %s -> %s", key, entry.get("total"), total)
        path.unlink(missing_ok=True)
        return None
    return entry["items"]


def store(key: str, total: int, items: list[dict[str, Any]]) -> None:
    path = _path(key)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        # Write beside the target and rename, so a crash never leaves a partial menu behind.
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"total": total, "items": items}))
        tmp.replace(path)
    except (OSError, TypeError, ValueError) as exc:
        logger.warning("Could not cache menu %s: %s", key, exc)
