import asyncio
import logging
from typing import Any

import httpx
from fastapi import HTTPException
from uuid import uuid4

from .config import config
from .state import (
    BackendUnavailableError,
    acquire_backend,
    backend_url,
    mark_unhealthy,
)

logger = logging.getLogger(__name__)
logger.setLevel(config.log_level)

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
    request_id = str(uuid4())
    logger.info(f"{request_id} - Received.")
    loop = asyncio.get_event_loop()
    deadline = loop.time() + config.queue_timeout_seconds

    for attempt in range(config.max_retries + 1):
        remaining = deadline - loop.time()
        if remaining <= 0:
            raise HTTPException(
                status_code=503,
                detail="Queue timeout waiting for an available backend",
            )

        try:
            async with acquire_backend(excluded=attempted, timeout=remaining) as backend:
                port = backend.port
                attempted.add(port)
                logger.info(f"{request_id} - Trying port {port}.")

                try:
                    url = backend_url(port, "/api/generate")
                    response = await client.post(
                        url, json=body, timeout=config.backend_read_timeout_seconds
                    )
                    logger.info(f"{request_id} - Response {response.status_code}.")

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
                        f"{request_id} - Backend {port} transport error on attempt {attempt}: {exc}"
                    )
                    await mark_unhealthy(port)
                    continue

        except BackendUnavailableError:
            raise HTTPException(
                status_code=503,
                detail="No healthy Ollama backends available within timeout",
            )

    logger.info(f"{request_id} - All attempts exhausted.")
    raise HTTPException(status_code=502, detail="All backend attempts failed")
