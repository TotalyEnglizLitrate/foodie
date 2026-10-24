from __future__ import annotations

import asyncio
import logging

import keyring
import keyring.errors

logger = logging.getLogger(__name__)


class SecretStore:
    """
    Asynchronous credential store abstracted over the keyring module.

    Credentials are namespaced by label: the keyring service name is the
    service joined with the label (e.g. "myapp:work"), so one service/user
    pair can hold several independent secrets.
    """

    SEPARATOR = "."

    def __init__(self) -> None:
        pass

    @classmethod
    def _service_name(cls, service: str, label: str) -> str:
        return f"{service}{cls.SEPARATOR}{label}"

    async def set_credential(
        self, service: str, user: str, secret: str, label: str
    ) -> None:
        await asyncio.to_thread(
            keyring.set_password, self._service_name(service, label), user, secret
        )

    async def get_credential(self, service: str, user: str, label: str) -> str | None:
        return (
            await asyncio.to_thread(
                keyring.get_password, self._service_name(service, label), user
            )
            or "{}"
        )

    async def delete_credential(self, service: str, user: str, label: str) -> bool:
        deleted = False

        try:
            await asyncio.to_thread(
                keyring.delete_password, self._service_name(service, label), user
            )
            deleted = True
        except keyring.errors.PasswordDeleteError as exc:
            logger.warning("Failed to delete credential from keyring: %s", exc)

        return deleted
