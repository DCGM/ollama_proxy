"""
Tests for the Ollama Proxy.

Uses httpx.AsyncClient with FastAPI's ASGI transport so no real network
connections are made. Backend HTTP calls are intercepted by patching the
app's http_client with a mock implementation.
"""
import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from fastapi.testclient import TestClient

from proxy.app import app
from proxy import state as state_module


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_response(status_code: int = 200, json_body: dict | None = None) -> httpx.Response:
    """Return a minimal httpx.Response-like mock."""
    json_body = json_body or {"response": "hello", "done": True}
    mock = MagicMock(spec=httpx.Response)
    mock.status_code = status_code
    mock.json.return_value = json_body
    return mock


def _reset_backends(ports: list[int], healthy: bool = False) -> None:
    """Replace the global backends registry with a controlled set."""
    state_module.backends.clear()
    for port in ports:
        state_module.backends[port] = state_module.Backend(port=port, healthy=healthy)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def reset_state():
    """Isolate backend state between tests."""
    original = dict(state_module.backends)
    yield
    state_module.backends.clear()
    state_module.backends.update(original)


@pytest.fixture()
def client():
    """Synchronous TestClient that suppresses the background scan loop."""
    with patch("proxy.app.scan_loop", new=AsyncMock()):
        with TestClient(app, raise_server_exceptions=False) as c:
            yield c


# ---------------------------------------------------------------------------
# POST /api/generate
# ---------------------------------------------------------------------------

class TestGenerate:
    def test_streaming_rejected(self, client):
        _reset_backends([24001], healthy=True)
        resp = client.post("/api/generate", json={"model": "m", "prompt": "hi", "stream": True})
        assert resp.status_code == 400
        assert "stream=false" in resp.json()["detail"]

    def test_no_healthy_backends_returns_503(self, client):
        _reset_backends([24001], healthy=False)
        resp = client.post("/api/generate", json={"model": "m", "prompt": "hi"})
        assert resp.status_code == 503

    def test_invalid_json_returns_400(self, client):
        _reset_backends([24001], healthy=True)
        resp = client.post(
            "/api/generate",
            content=b"not-json",
            headers={"Content-Type": "application/json"},
        )
        assert resp.status_code == 400

    def test_successful_generate(self, client):
        _reset_backends([24001], healthy=True)
        expected = {"response": "world", "done": True}
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=_make_response(200, expected))
        app.state.http_client = mock_client

        resp = client.post("/api/generate", json={"model": "m", "prompt": "hi"})

        assert resp.status_code == 200
        assert resp.json() == expected
        mock_client.post.assert_called_once()

    def test_stream_false_is_forwarded(self, client):
        _reset_backends([24001], healthy=True)
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=_make_response(200))
        app.state.http_client = mock_client

        client.post("/api/generate", json={"model": "m", "prompt": "hi"})

        _, kwargs = mock_client.post.call_args
        assert kwargs["json"]["stream"] is False

    def test_stream_defaults_to_false_when_absent(self, client):
        _reset_backends([24001], healthy=True)
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=_make_response(200))
        app.state.http_client = mock_client

        resp = client.post("/api/generate", json={"model": "m", "prompt": "hi"})

        assert resp.status_code == 200
        _, kwargs = mock_client.post.call_args
        assert kwargs["json"].get("stream") is False


# ---------------------------------------------------------------------------
# Retry behaviour
# ---------------------------------------------------------------------------

class TestRetry:
    def test_retries_on_5xx_and_succeeds(self, client):
        _reset_backends([24001, 24002], healthy=True)
        good_response = _make_response(200, {"response": "ok", "done": True})
        bad_response = _make_response(500)
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(side_effect=[bad_response, good_response])
        app.state.http_client = mock_client

        resp = client.post("/api/generate", json={"model": "m", "prompt": "hi"})

        assert resp.status_code == 200
        assert mock_client.post.call_count == 2

    def test_retries_on_transport_error_and_succeeds(self, client):
        _reset_backends([24001, 24002], healthy=True)
        good_response = _make_response(200, {"response": "ok", "done": True})
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(
            side_effect=[httpx.ConnectError("refused"), good_response]
        )
        app.state.http_client = mock_client

        resp = client.post("/api/generate", json={"model": "m", "prompt": "hi"})

        assert resp.status_code == 200

    def test_marks_backend_unhealthy_on_5xx(self, client):
        _reset_backends([24001, 24002], healthy=True)
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(
            side_effect=[_make_response(500), _make_response(200)]
        )
        app.state.http_client = mock_client

        client.post("/api/generate", json={"model": "m", "prompt": "hi"})

        # Port 24001 is tried first (lowest port), should be marked unhealthy
        assert state_module.backends[24001].healthy is False

    def test_marks_backend_unhealthy_on_transport_error(self, client):
        _reset_backends([24001, 24002], healthy=True)
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(
            side_effect=[httpx.ConnectError("refused"), _make_response(200)]
        )
        app.state.http_client = mock_client

        client.post("/api/generate", json={"model": "m", "prompt": "hi"})

        assert state_module.backends[24001].healthy is False

    def test_all_retries_exhausted_returns_502(self, client):
        _reset_backends([24001, 24002, 24003], healthy=True)
        mock_client = AsyncMock()
        # MAX_RETRIES=2 → 3 total attempts, all fail
        mock_client.post = AsyncMock(
            side_effect=[
                httpx.ConnectError("refused"),
                httpx.ConnectError("refused"),
                httpx.ConnectError("refused"),
            ]
        )
        app.state.http_client = mock_client

        resp = client.post("/api/generate", json={"model": "m", "prompt": "hi"})

        assert resp.status_code == 502

    def test_does_not_retry_on_4xx(self, client):
        _reset_backends([24001, 24002], healthy=True)
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=_make_response(400, {"error": "bad"}))
        app.state.http_client = mock_client

        resp = client.post("/api/generate", json={"model": "m", "prompt": "hi"})

        assert resp.status_code == 200  # proxy returns backend 4xx body as-is
        assert mock_client.post.call_count == 1  # no retry

    def test_same_backend_not_tried_twice(self, client):
        # Only one healthy backend; once it fails we cannot retry
        _reset_backends([24001], healthy=True)
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(side_effect=httpx.ConnectError("refused"))
        app.state.http_client = mock_client

        resp = client.post("/api/generate", json={"model": "m", "prompt": "hi"})

        # First attempt fails → no more healthy backends → 502
        assert resp.status_code in (502, 503)
        assert mock_client.post.call_count == 1


# ---------------------------------------------------------------------------
# Load balancing
# ---------------------------------------------------------------------------

class TestLoadBalancing:
    def test_selects_least_inflight(self, client):
        _reset_backends([24001, 24002], healthy=True)
        state_module.backends[24001].in_flight = 5
        state_module.backends[24002].in_flight = 1

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=_make_response(200))
        app.state.http_client = mock_client

        client.post("/api/generate", json={"model": "m", "prompt": "hi"})

        called_url: str = mock_client.post.call_args[0][0]
        assert ":24002/" in called_url

    def test_breaks_tie_by_port(self, client):
        _reset_backends([24003, 24001, 24002], healthy=True)
        # All in_flight = 0

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=_make_response(200))
        app.state.http_client = mock_client

        client.post("/api/generate", json={"model": "m", "prompt": "hi"})

        called_url: str = mock_client.post.call_args[0][0]
        assert ":24001/" in called_url


# ---------------------------------------------------------------------------
# Debug endpoints
# ---------------------------------------------------------------------------

class TestDebugEndpoints:
    def test_health_endpoint(self, client):
        _reset_backends([24001, 24002, 24003], healthy=False)
        state_module.backends[24001].healthy = True
        state_module.backends[24002].healthy = True

        resp = client.get("/__proxy/health")

        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert data["healthy_backends"] == 2

    def test_backends_endpoint(self, client):
        _reset_backends([24001, 24002], healthy=False)
        state_module.backends[24001].healthy = True
        state_module.backends[24001].in_flight = 3

        resp = client.get("/__proxy/backends")

        assert resp.status_code == 200
        data = resp.json()
        backends_list = data["backends"]
        ports = [b["port"] for b in backends_list]
        assert ports == sorted(ports)

        b1 = next(b for b in backends_list if b["port"] == 24001)
        assert b1["healthy"] is True
        assert b1["in_flight"] == 3

        b2 = next(b for b in backends_list if b["port"] == 24002)
        assert b2["healthy"] is False


# ---------------------------------------------------------------------------
# Scanner unit tests
# ---------------------------------------------------------------------------

class TestScanner:
    @pytest.mark.asyncio
    async def test_probe_marks_healthy_on_200(self):
        _reset_backends([24001])
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_response)

        from proxy.scanner import scan_once
        await scan_once(mock_client)

        assert state_module.backends[24001].healthy is True

    @pytest.mark.asyncio
    async def test_probe_marks_unhealthy_on_connection_error(self):
        _reset_backends([24001])
        state_module.backends[24001].healthy = True
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(side_effect=httpx.ConnectError("refused"))

        from proxy.scanner import scan_once
        await scan_once(mock_client)

        assert state_module.backends[24001].healthy is False

    @pytest.mark.asyncio
    async def test_probe_marks_unhealthy_on_non_200(self):
        _reset_backends([24001])
        state_module.backends[24001].healthy = True
        mock_response = MagicMock()
        mock_response.status_code = 503
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_response)

        from proxy.scanner import scan_once
        await scan_once(mock_client)

        assert state_module.backends[24001].healthy is False


# ---------------------------------------------------------------------------
# State helpers
# ---------------------------------------------------------------------------

class TestStateHelpers:
    @pytest.mark.asyncio
    async def test_choose_backend_excludes_attempted(self):
        _reset_backends([24001, 24002], healthy=True)

        backend = await state_module.choose_backend(excluded={24001})

        assert backend is not None
        assert backend.port == 24002

    @pytest.mark.asyncio
    async def test_choose_backend_none_when_all_excluded(self):
        _reset_backends([24001], healthy=True)

        backend = await state_module.choose_backend(excluded={24001})

        assert backend is None

    @pytest.mark.asyncio
    async def test_choose_backend_none_when_none_healthy(self):
        _reset_backends([24001, 24002], healthy=False)

        backend = await state_module.choose_backend(excluded=set())

        assert backend is None

    @pytest.mark.asyncio
    async def test_increment_decrement_in_flight(self):
        _reset_backends([24001])

        await state_module.increment_in_flight(24001)
        await state_module.increment_in_flight(24001)
        assert state_module.backends[24001].in_flight == 2

        await state_module.decrement_in_flight(24001)
        assert state_module.backends[24001].in_flight == 1

    @pytest.mark.asyncio
    async def test_decrement_in_flight_does_not_go_negative(self):
        _reset_backends([24001])

        await state_module.decrement_in_flight(24001)
        assert state_module.backends[24001].in_flight == 0
