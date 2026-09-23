from __future__ import annotations

from email.header import Header
from urllib.parse import quote

import httpx


class NotificationError(RuntimeError):
    pass


class NtfyNotifier:
    def __init__(self, url: str, topic: str, token: str = "", timeout_seconds: float = 15) -> None:
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        self._client = httpx.AsyncClient(
            base_url=url.rstrip("/"), timeout=timeout_seconds, headers=headers
        )
        self._topic = topic

    async def send(self, title: str, message: str, priority: str = "default") -> None:
        try:
            response = await self._client.post(
                f"/{quote(self._topic, safe='')}",
                content=message.encode("utf-8"),
                headers={
                    "Content-Type": "text/plain; charset=utf-8",
                    "Title": Header(title, "utf-8").encode(),
                    "Priority": priority,
                },
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise NotificationError("Impossible d'envoyer la notification ntfy") from exc

    async def close(self) -> None:
        await self._client.aclose()
