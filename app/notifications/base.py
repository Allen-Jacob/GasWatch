from __future__ import annotations

from typing import Protocol


class Notifier(Protocol):
    async def send(self, title: str, message: str, priority: str = "default") -> None: ...

    async def close(self) -> None: ...


class DisabledNotifier:
    async def send(self, title: str, message: str, priority: str = "default") -> None:
        return None

    async def close(self) -> None:
        return None
