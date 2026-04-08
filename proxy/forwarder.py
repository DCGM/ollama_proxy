import logging
from typing import Any

import httpx
from fastapi import HTTPException

from .config import config
from .state import (
    backend_url,
    choose_backend,
    decrement_in_flight,
    increment_in_flight,
    mark_unhealthy,
)

logger = logging.getLogger(__name__)

_RETRYABLE_EXCEPTIONS = (
    httpx.ConnectError,
    httpx.TimeoutException,
    httpx.TransportError,
    httpx.RemoteProtocolError,
)


async def forward_generate(
    client: httpx.AsyncClient,
    body: dict[str, Any],
) -> dict[str, Any]:
    attempted: set[int] = set()

    for attempt in range(config.max_retries + 1):
        backend = await choose_backend(excluded=attempted)
        if backend is None:
            raise HTTPException(
                status_code=503,
                detail="No healthy Ollama backends available",
            )

        port = backend.port
        attempted.add(port)
        await increment_in_flight(port)

        try:
            url = backend_url(port, "/api/generate")
            response = await client.post(url, json=body)

            if response.status_code >= 500:
                logger.warning(
                    "Backend %d returned %d on attempt %d",
                    port,
                    response.status_code,
                    attempt,
                )
                await mark_unhealthy(port)
                continue

            return response.json()

        except _RETRYABLE_EXCEPTIONS as exc:
            logger.warning(
                "Backend %d transport error on attempt %d: %s",
                port,
                attempt,
                exc,
            )
            await mark_unhealthy(port)
            continue

        finally:
            await decrement_in_flight(port)

    raise HTTPException(status_code=502, detail="All backend attempts failed")
