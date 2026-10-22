from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from dbus_next.constants import BusType
from dbus_next.signature import Variant
from dbus_next.aio.message_bus import MessageBus

logger = logging.getLogger(__name__)

SECRET_SERVICE_DEST = "org.freedesktop.secrets"
SECRET_SERVICE_PATH = "/org/freedesktop/secrets"
DEFAULT_COLLECTION_PATH = "/org/freedesktop/secrets/aliases/default"
FALLBACK_CONFIG_DIR = Path.home() / ".config" / "foodie"
FALLBACK_CREDENTIALS_FILE = FALLBACK_CONFIG_DIR / "tokens.json"


class SecretStore:
    """
    Asynchronous credential store utilizing the FreeDesktop Secret Service specification
    over D-Bus (e.g. GNOME Keyring, KWallet, KeePassXC) with a secure fallback file.
    """

    def __init__(self) -> None:
        self._bus: MessageBus | None = None

    async def _get_bus(self) -> MessageBus:
        if self._bus is None or not self._bus.connected:
            self._bus = await MessageBus(bus_type=BusType.SESSION).connect()
        return self._bus

    async def set_credential(
        self, service: str, account: str, secret: str, label: str | None = None
    ) -> None:
        """Store a secret for the given service and account."""
        try:
            bus = await self._get_bus()

            # Open session
            intro_svc = await bus.introspect(SECRET_SERVICE_DEST, SECRET_SERVICE_PATH)
            svc_obj = bus.get_proxy_object(SECRET_SERVICE_DEST, SECRET_SERVICE_PATH, intro_svc)
            svc = svc_obj.get_interface("org.freedesktop.Secret.Service")
            _, session = await svc.call_open_session("plain", Variant("s", ""))

            # Access default collection
            intro_col = await bus.introspect(SECRET_SERVICE_DEST, DEFAULT_COLLECTION_PATH)
            col_obj = bus.get_proxy_object(SECRET_SERVICE_DEST, DEFAULT_COLLECTION_PATH, intro_col)
            col = col_obj.get_interface("org.freedesktop.Secret.Collection")

            item_label = label or f"foodie:{service}:{account}"
            props = {
                "org.freedesktop.Secret.Item.Label": Variant("s", item_label),
                "org.freedesktop.Secret.Item.Attributes": Variant(
                    "a{ss}", {"service": service, "account": account}
                ),
            }
            secret_struct = [session, b"", secret.encode("utf-8"), "text/plain; charset=utf8"]

            # Create or replace item
            await col.call_create_item(props, secret_struct, True)
            return
        except Exception as exc:
            logger.warning("Failed to store credential via D-Bus Secret Service: %s. Using file fallback.", exc)
            self._set_fallback_credential(service, account, secret)

    async def get_credential(self, service: str, account: str) -> str | None:
        """Retrieve a secret for the given service and account."""
        try:
            bus = await self._get_bus()

            intro_svc = await bus.introspect(SECRET_SERVICE_DEST, SECRET_SERVICE_PATH)
            svc_obj = bus.get_proxy_object(SECRET_SERVICE_DEST, SECRET_SERVICE_PATH, intro_svc)
            svc = svc_obj.get_interface("org.freedesktop.Secret.Service")
            _, session = await svc.call_open_session("plain", Variant("s", ""))

            # Search items matching service and account attributes
            unlocked, locked = await svc.call_search_items({"service": service, "account": account})

            target_items = unlocked
            if not target_items and locked:
                unlocked_res, _ = await svc.call_unlock(locked)
                target_items = unlocked_res

            if not target_items:
                return self._get_fallback_credential(service, account)

            secrets_map = await svc.call_get_secrets(target_items, session)
            item_path = target_items[0]
            if item_path in secrets_map:
                secret_bytes = secrets_map[item_path][2]
                return secret_bytes.decode("utf-8")
        except Exception as exc:
            logger.warning("Failed to retrieve credential via D-Bus Secret Service: %s. Checking fallback.", exc)

        return self._get_fallback_credential(service, account)

    async def delete_credential(self, service: str, account: str) -> bool:
        """Delete a secret matching the service and account."""
        deleted = False
        try:
            bus = await self._get_bus()
            intro_svc = await bus.introspect(SECRET_SERVICE_DEST, SECRET_SERVICE_PATH)
            svc_obj = bus.get_proxy_object(SECRET_SERVICE_DEST, SECRET_SERVICE_PATH, intro_svc)
            svc = svc_obj.get_interface("org.freedesktop.Secret.Service")

            unlocked, locked = await svc.call_search_items({"service": service, "account": account})
            items = unlocked + locked
            for item_path in items:
                intro_item = await bus.introspect(SECRET_SERVICE_DEST, item_path)
                item_obj = bus.get_proxy_object(SECRET_SERVICE_DEST, item_path, intro_item)
                item_iface = item_obj.get_interface("org.freedesktop.Secret.Item")
                await item_iface.call_delete()
                deleted = True
        except Exception as exc:
            logger.warning("Failed to delete credential via D-Bus Secret Service: %s", exc)

        if self._delete_fallback_credential(service, account):
            deleted = True

        return deleted

    # --- File Fallback Helpers ---

    def _get_fallback_credential(self, service: str, account: str) -> str | None:
        if not FALLBACK_CREDENTIALS_FILE.exists():
            return None
        try:
            data = json.loads(FALLBACK_CREDENTIALS_FILE.read_text(encoding="utf-8"))
            return data.get(f"{service}:{account}")
        except Exception:
            return None

    def _set_fallback_credential(self, service: str, account: str, secret: str) -> None:
        FALLBACK_CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        data: dict[str, Any] = {}
        if FALLBACK_CREDENTIALS_FILE.exists():
            try:
                data = json.loads(FALLBACK_CREDENTIALS_FILE.read_text(encoding="utf-8"))
            except Exception:
                data = {}
        data[f"{service}:{account}"] = secret
        FALLBACK_CREDENTIALS_FILE.write_text(json.dumps(data), encoding="utf-8")
        FALLBACK_CREDENTIALS_FILE.chmod(0o600)

    def _delete_fallback_credential(self, service: str, account: str) -> bool:
        if not FALLBACK_CREDENTIALS_FILE.exists():
            return False
        try:
            data = json.loads(FALLBACK_CREDENTIALS_FILE.read_text(encoding="utf-8"))
            key = f"{service}:{account}"
            if key in data:
                del data[key]
                FALLBACK_CREDENTIALS_FILE.write_text(json.dumps(data), encoding="utf-8")
                return True
        except Exception:
            pass
        return False
