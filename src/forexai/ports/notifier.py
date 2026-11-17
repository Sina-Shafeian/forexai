from __future__ import annotations

from typing import Protocol

from forexai.domain.models import Signal


class SignalNotifier(Protocol):
    async def send(self, signal: Signal) -> str: ...

