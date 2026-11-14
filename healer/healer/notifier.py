"""Optional chat notifications (Slack and Discord incoming webhooks both accept this)."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

import httpx


def webhook_notifier(url: str) -> Callable[[str], Awaitable[None]] | None:
    if not url:
        return None

    async def send(text: str) -> None:
        async with httpx.AsyncClient(timeout=5.0) as client:
            await client.post(url, json={"text": text, "content": text})

    return send
