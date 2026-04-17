import asyncio
from contextlib import asynccontextmanager
from collections.abc import AsyncGenerator
from dataclasses import dataclass, field

from .config import config


class BackendUnavailableError(Exception):
    """Raised when no backend becomes available within the timeout."""


@dataclass
class Backend:
    port: int
    healthy: bool = False
    in_flight: int = 0


# Global registry
backends: dict[int, Backend] = {
    port: Backend(port=port)
    for port in range(config.backend_port_start, config.backend_port_end + 1)
}
state_lock: asyncio.Lock = asyncio.Lock()
_slots_available: asyncio.Condition = asyncio.Condition(state_lock)


def backend_url(port: int, path: str) -> str:
    return f"http://{config.backend_host}:{port}{path}"


def _find_available(excluded: set[int]) -> Backend | None:
    """Pick the least-loaded backend under the concurrency cap.

    Must be called while holding *state_lock*.
    """
    candidates = [
        b
        for b in backends.values()
        if b.healthy
        and b.port not in excluded
        and b.in_flight < config.max_concurrent_per_backend
    ]
    if not candidates:
        return None
    return min(candidates, key=lambda b: (b.in_flight, b.port))


async def mark_healthy(port: int) -> None:
    async with _slots_available:
        if port in backends:
            backends[port].healthy = True
        # A new healthy backend means waiting requests may proceed.
        _slots_available.notify_all()


async def mark_unhealthy(port: int) -> None:
    async with _slots_available:
        if port in backends:
            backends[port].healthy = False


async def increment_in_flight(port: int) -> None:
    async with _slots_available:
        if port in backends:
            backends[port].in_flight += 1


async def decrement_in_flight(port: int) -> None:
    async with _slots_available:
        if port in backends:
            backends[port].in_flight = max(0, backends[port].in_flight - 1)
        _slots_available.notify_all()


async def choose_backend(excluded: set[int]) -> Backend | None:
    async with _slots_available:
        candidates = [
            b
            for b in backends.values()
            if b.healthy and b.port not in excluded
        ]
        if not candidates:
            return None
        return min(candidates, key=lambda b: (b.in_flight, b.port))


@asynccontextmanager
async def acquire_backend(
    excluded: set[int],
    timeout: float,
) -> AsyncGenerator[Backend, None]:
    """Atomically wait for and reserve a slot on the least-loaded backend.

    Blocks (FIFO via the underlying ``asyncio.Condition``) until a healthy
    backend with spare capacity is available, or *timeout* seconds elapse.

    Usage::

        async with acquire_backend(excluded={...}, timeout=3600) as backend:
            # backend.in_flight has already been incremented
            await do_work(backend)
        # in_flight is decremented and waiters notified on exit
    """
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout
    chosen: Backend | None = None

    async with _slots_available:
        while True:
            chosen = _find_available(excluded)
            if chosen is not None:
                chosen.in_flight += 1
                break
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise BackendUnavailableError(
                    "No backend became available within the timeout"
                )
            try:
                await asyncio.wait_for(
                    _slots_available.wait(), timeout=remaining
                )
            except asyncio.TimeoutError:
                raise BackendUnavailableError(
                    "No backend became available within the timeout"
                )

    try:
        yield chosen
    finally:
        async with _slots_available:
            chosen.in_flight = max(0, chosen.in_flight - 1)
            _slots_available.notify_all()


async def get_all_backends() -> list[Backend]:
    async with state_lock:
        return list(backends.values())
