import asyncio
from dataclasses import dataclass, field

from .config import config


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


def backend_url(port: int, path: str) -> str:
    return f"http://{config.backend_host}:{port}{path}"


async def mark_healthy(port: int) -> None:
    async with state_lock:
        if port in backends:
            backends[port].healthy = True


async def mark_unhealthy(port: int) -> None:
    async with state_lock:
        if port in backends:
            backends[port].healthy = False


async def increment_in_flight(port: int) -> None:
    async with state_lock:
        if port in backends:
            backends[port].in_flight += 1


async def decrement_in_flight(port: int) -> None:
    async with state_lock:
        if port in backends:
            backends[port].in_flight = max(0, backends[port].in_flight - 1)


async def choose_backend(excluded: set[int]) -> Backend | None:
    async with state_lock:
        candidates = [
            b
            for b in backends.values()
            if b.healthy and b.port not in excluded
        ]
        if not candidates:
            return None
        return min(candidates, key=lambda b: (b.in_flight, b.port))


async def get_all_backends() -> list[Backend]:
    async with state_lock:
        return list(backends.values())
