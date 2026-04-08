import asyncio
import logging

import httpx

from .config import config
from .state import backends, mark_healthy, mark_unhealthy, backend_url

logger = logging.getLogger(__name__)


async def probe_backend(client: httpx.AsyncClient, port: int) -> bool:
    url = backend_url(port, "/api/tags")
    try:
        response = await client.get(
            url,
            timeout=config.probe_timeout_seconds,
        )
        return response.status_code == 200
    except Exception:
        return False


async def scan_once(client: httpx.AsyncClient) -> None:
    for port in list(backends.keys()):
        healthy = await probe_backend(client, port)
        if healthy:
            await mark_healthy(port)
            logger.debug("Backend %d is healthy", port)
        else:
            await mark_unhealthy(port)
            logger.debug("Backend %d is unhealthy", port)


async def scan_loop() -> None:
    timeout = httpx.Timeout(connect=config.probe_timeout_seconds, read=config.probe_timeout_seconds, write=config.probe_timeout_seconds, pool=config.probe_timeout_seconds)
    async with httpx.AsyncClient(timeout=timeout) as client:
        while True:
            try:
                await scan_once(client)
            except Exception:
                logger.exception("Unexpected error in scan loop")
            await asyncio.sleep(config.scan_interval_seconds)
