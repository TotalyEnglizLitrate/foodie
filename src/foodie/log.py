from __future__ import annotations

import logging
import os
import re
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path

import httpx

logger = logging.getLogger(__name__)

LOG_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
BODY_LIMIT = 2000

_SECRET_FIELD = re.compile(
    r"""(?P<key>["']?(?:access_token|refresh_token|code_verifier|client_secret|code)["']?\s*[:=]\s*["']?)(?P<value>[^"'&,\s}]+)""",
    re.IGNORECASE,
)
_BEARER = re.compile(r"(Bearer\s+)\S+", re.IGNORECASE)


def log_path() -> Path:
    """File the logs are written to; the TUI owns the terminal so it can't use stderr."""
    state_home = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state")
    return state_home / "foodie" / "foodie.log"


def redact(text: str) -> str:
    """Mask tokens, codes and secrets so they never reach a log."""
    text = _BEARER.sub(r"\1***", text)
    return _SECRET_FIELD.sub(r"\g<key>***", text)


def truncate(text: str, limit: int = BODY_LIMIT) -> str:
    if len(text) <= limit:
        return text
    return f"{text[:limit]}... [{len(text) - limit} more chars]"


def setup_logging(verbose: bool = False, console: bool = False) -> None:
    """Configure the ``foodie`` logger: INFO by default, DEBUG when ``verbose``.

    Always writes to the log file; also to stderr when ``console`` is set
    (non-TUI commands).
    """
    level = logging.DEBUG if verbose else logging.INFO
    root = logging.getLogger("foodie")
    root.setLevel(level)
    root.propagate = False
    for handler in list(root.handlers):
        root.removeHandler(handler)

    formatter = logging.Formatter(LOG_FORMAT)
    handlers: list[logging.Handler] = []
    try:
        path = log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(path, encoding="utf-8"))
    except OSError as exc:
        print(f"foodie: cannot write log file: {exc}", file=sys.stderr)
    if console:
        handlers.append(logging.StreamHandler(sys.stderr))
    for handler in handlers:
        handler.setFormatter(formatter)
        root.addHandler(handler)

    if verbose:
        # httpx/keyring internals are only useful when debugging.
        logging.getLogger("httpx").setLevel(logging.DEBUG)
    else:
        logging.getLogger("httpx").setLevel(logging.WARNING)
    logger.info("Logging at %s to %s", logging.getLevelName(level), log_path())


def http_event_hooks(
    provider: str,
) -> dict[str, list[Callable[..., Awaitable[None]]]]:
    """httpx event hooks that log every request and response with secrets redacted."""
    http_logger = logging.getLogger(f"foodie.http.{provider}")

    async def on_request(request: httpx.Request) -> None:
        if not http_logger.isEnabledFor(logging.DEBUG):
            return
        body = request.content.decode("utf-8", errors="replace")
        http_logger.debug(
            "-> %s %s body=%s", request.method, request.url, truncate(redact(body))
        )

    async def on_response(response: httpx.Response) -> None:
        request = response.request
        await response.aread()
        level = logging.WARNING if response.status_code >= 400 else logging.DEBUG
        if not http_logger.isEnabledFor(level):
            return
        http_logger.log(
            level,
            "<- %s %s %s in %.0fms body=%s",
            response.status_code,
            request.method,
            request.url,
            response.elapsed.total_seconds() * 1000,
            truncate(redact(response.text)),
        )

    return {"request": [on_request], "response": [on_response]}
