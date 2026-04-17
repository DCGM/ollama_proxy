import asyncio
import logging


from contextlib import asynccontextmanager
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from .config import config

logging.basicConfig(format='%(asctime)s - %(levelname)s - %(module)s - %(message)s', level=config.log_level)
logger = logging.getLogger(__name__)
from .forwarder import forward_generate
from .scanner import scan_loop
from .state import get_all_backends



@asynccontextmanager
async def lifespan(app: FastAPI):
    timeout = httpx.Timeout(
        connect=config.backend_connect_timeout_seconds,
        read=config.backend_read_timeout_seconds,
        write=config.backend_read_timeout_seconds,
        pool=60,
    )
    app.state.http_client = httpx.AsyncClient(timeout=timeout)
    scan_task = asyncio.create_task(scan_loop())
    try:
        yield
    finally:
        scan_task.cancel()
        try:
            await scan_task
        except asyncio.CancelledError:
            pass
        await app.state.http_client.aclose()


app = FastAPI(title="Ollama Proxy", lifespan=lifespan)


@app.post("/api/generate")
async def api_generate(request: Request) -> JSONResponse:
    try:
        body: dict[str, Any] = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    if body.get("stream") is True:
        raise HTTPException(
            status_code=400,
            detail="Streaming is not supported by this proxy; use stream=false",
        )

    body.setdefault("stream", False)

    result = await forward_generate(request.app.state.http_client, body)
    return JSONResponse(content=result)


@app.get("/__proxy/health")
async def proxy_health() -> JSONResponse:
    all_backends = await get_all_backends()
    healthy_count = sum(1 for b in all_backends if b.healthy)
    return JSONResponse(content={"status": "ok", "healthy_backends": healthy_count})


@app.get("/__proxy/backends")
async def proxy_backends() -> JSONResponse:
    all_backends = await get_all_backends()
    return JSONResponse(
        content={
            "backends": [
                {
                    "port": b.port,
                    "healthy": b.healthy,
                    "in_flight": b.in_flight,
                }
                for b in sorted(all_backends, key=lambda b: b.port)
            ]
        }
    )
