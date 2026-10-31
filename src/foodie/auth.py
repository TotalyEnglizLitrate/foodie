from __future__ import annotations

import asyncio
import logging
import webbrowser
from collections.abc import Callable
from urllib.parse import parse_qs, urlparse

from foodie.providers.base import BaseProvider

logger = logging.getLogger(__name__)


async def authenticate_interactive(
    provider: BaseProvider,
    port: int = 8080,
    timeout: float = 180.0,
    log: Callable[[str], None] = print,
) -> bool:
    """
    Runs an interactive OAuth 2.1 PKCE login flow for a provider.
    1. Spins up a temporary local HTTP server to receive the callback.
    2. Opens the browser for phone + OTP login.
    3. Captures the authorization code and exchanges it for tokens.

    Progress messages go to ``log`` (stdout by default) so a TUI can redirect them.
    """
    redirect_uri = f"http://localhost:{port}/callback"
    logger.info("Starting OAuth login for %s on port %d", provider.display_name, port)
    auth_url, verifier = await provider.get_auth_url(redirect_uri)
    logger.debug("Authorization url: %s", auth_url)

    loop = asyncio.get_running_loop()
    code_future: asyncio.Future[str] = loop.create_future()

    async def handle_client(
        reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            line = await reader.readline()
            request_line = line.decode("utf-8", errors="replace").strip()
            parts = request_line.split(" ")
            if len(parts) >= 2:
                path = parts[1]
                parsed = urlparse(path)
                query = parse_qs(parsed.query)
                logger.debug("Callback request %s (params: %s)", parsed.path, sorted(query))

                if "code" in query:
                    code = query["code"][0]
                    if not code_future.done():
                        code_future.set_result(code)

                    body = (
                        "<!DOCTYPE html><html><head><title>Foodie - Login Successful</title></head>"
                        "<body style='font-family: -apple-system, system-ui, BlinkMacSystemFont, sans-serif; "
                        "text-align: center; padding: 60px 20px; background: #0d1117; color: #e6edf3;'>"
                        "<h1 style='color: #2ea043; font-size: 28px;'>Login Successful!</h1>"
                        f"<p style='color: #8b949e; font-size: 16px;'>{provider.display_name} authentication complete.</p>"
                        "<p style='margin-top: 24px; font-size: 14px;'>You can close this tab and return to your terminal.</p>"
                        "</body></html>"
                    )
                    content_bytes = body.encode("utf-8")
                    headers = (
                        "HTTP/1.1 200 OK\r\n"
                        "Content-Type: text/html; charset=utf-8\r\n"
                        f"Content-Length: {len(content_bytes)}\r\n"
                        "Connection: close\r\n\r\n"
                    )
                    writer.write(headers.encode("utf-8") + content_bytes)
                    await writer.drain()
                    return

                elif "error" in query:
                    err_msg = query.get(
                        "error_description", query.get("error", ["Unknown error"])
                    )[0]
                    if not code_future.done():
                        code_future.set_exception(
                            RuntimeError(f"OAuth error: {err_msg}")
                        )
                    body = f"<html><body><h2>Authentication failed: {err_msg}</h2></body></html>"
                    writer.write(
                        f"HTTP/1.1 400 Bad Request\r\nConnection: close\r\n\r\n{body}".encode()
                    )
                    await writer.drain()
                    return

            writer.write(
                b"HTTP/1.1 404 Not Found\r\nConnection: close\r\n\r\nNot Found"
            )
            await writer.drain()
        except Exception as exc:
            logger.warning("Error in auth callback handler: %s", exc)
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

    server = await asyncio.start_server(handle_client, "127.0.0.1", port)
    logger.info("Local auth callback server listening on %s", redirect_uri)

    log(f"Authenticating with {provider.display_name}. Opening browser; if it does not open, visit: {auth_url}")
    log(f"Waiting for authentication (timeout: {int(timeout)}s)...")

    webbrowser.open(auth_url)

    try:
        code = await asyncio.wait_for(code_future, timeout=timeout)
    except TimeoutError:
        logger.warning("OAuth login for %s timed out after %ss", provider.display_name, timeout)
        log("Authentication timed out. No authorization code received.")
        return False
    finally:
        server.close()
        await server.wait_closed()

    logger.debug("Authorization code received for %s", provider.display_name)
    log("Authorization code received. Exchanging for tokens...")
    success = await provider.complete_auth(code, verifier, redirect_uri)
    if success:
        log("Tokens received and stored securely.")
    else:
        log("Failed to exchange authorization code for tokens.")
    return success
