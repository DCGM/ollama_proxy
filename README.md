# Ollama Proxy

A minimal asynchronous proxy for **non-streaming** Ollama requests.

Exposes a single stable HTTP endpoint, distributes requests across healthy backends discovered automatically by scanning a configured localhost port range, and retries on failure.

## Features

- `POST /api/generate` — non-streaming only
- Least-in-flight load balancing with port tie-breaking
- Automatic backend discovery via periodic health probes (`GET /api/tags`)
- Immediate unhealthy marking on request or probe failure; re-enabled only after a successful scan
- Configurable retry with per-request backend exclusion
- Debug endpoints: `GET /__proxy/health`, `GET /__proxy/backends`

---

## Project layout

```
proxy/
    app.py          FastAPI app, routes, lifespan
    config.py       Settings loaded from environment variables
    state.py        Backend dataclass and registry helpers
    scanner.py      Background scan loop and health probes
    forwarder.py    Backend selection, request forwarding, retry logic
main.py             Entry point (uvicorn)
tests/
    test_proxy.py   Full test suite (25 tests)
```

---

## Requirements

- Python ≥ 3.10
- `fastapi`, `httpx`, `uvicorn[standard]`

Install:

```bash
pip install -r requirements.txt
```

---

## Running

```bash
python3 main.py
```

Or with uvicorn directly:

```bash
uvicorn proxy.app:app --host 0.0.0.0 --port 11434
```

---

## Configuration

All settings are read from environment variables. Defaults are shown.

| Variable | Default | Description |
|---|---|---|
| `PROXY_HOST` | `0.0.0.0` | Bind address for the proxy |
| `PROXY_PORT` | `11434` | Bind port for the proxy |
| `BACKEND_HOST` | `127.0.0.1` | Host where backends are reachable |
| `BACKEND_PORT_START` | `24000` | First port in the scan range (inclusive) |
| `BACKEND_PORT_END` | `24100` | Last port in the scan range (inclusive) |
| `SCAN_INTERVAL_SECONDS` | `5` | Seconds between full backend scans |
| `PROBE_TIMEOUT_SECONDS` | `1.0` | Timeout for each health probe |
| `MAX_RETRIES` | `2` | Max additional attempts after the first failure (3 total) |
| `BACKEND_CONNECT_TIMEOUT_SECONDS` | `2.0` | TCP connect timeout for forwarded requests |
| `BACKEND_READ_TIMEOUT_SECONDS` | `600.0` | Read timeout for forwarded requests |
| `REJECT_STREAMING` | `true` | Reject requests with `"stream": true` |
| `LOG_LEVEL` | `INFO` | Logging level (`DEBUG`, `INFO`, `WARNING`, …) |

---

## API

### `POST /api/generate`

Proxy endpoint. Accepts an Ollama generate request body and returns the backend response unchanged.

**Request body** (example):

```json
{
  "model": "my-model",
  "prompt": "Hello",
  "stream": false
}
```

- `"stream": true` is rejected with `400 Bad Request`.
- `"stream"` is defaulted to `false` if absent.

**Responses:**

| Status | Meaning |
|---|---|
| `200` | Backend response (JSON forwarded unchanged) |
| `400` | Invalid request (streaming requested or malformed JSON) |
| `503` | No healthy backends available |
| `502` | All backend attempts failed |

---

### `GET /__proxy/health`

```json
{
  "status": "ok",
  "healthy_backends": 3
}
```

### `GET /__proxy/backends`

```json
{
  "backends": [
    {"port": 24001, "healthy": true, "in_flight": 1},
    {"port": 24002, "healthy": false, "in_flight": 0}
  ]
}
```

---

## Backend discovery

Each Ollama worker is expected on a port within `[BACKEND_PORT_START, BACKEND_PORT_END]`, typically exposed via a reverse SSH tunnel terminating on localhost.

The scanner probes `GET http://127.0.0.1:<port>/api/tags` every `SCAN_INTERVAL_SECONDS`. A backend is healthy only when the probe returns HTTP 200. On any failure (connection error, timeout, non-200) the backend is marked unhealthy. Recovery happens only when a later scan succeeds.

On a forwarded request failure the backend is also marked unhealthy immediately and the request is retried on another healthy backend (up to `MAX_RETRIES` additional attempts).

---

## Running tests

```bash
pip install -r requirements-dev.txt
python3 -m pytest tests/ -v
```

The test suite (25 tests) covers:

- Streaming rejection
- No-healthy-backends 503
- Successful forwarding
- Retry on 5xx and transport errors
- No retry on 4xx
- Least-in-flight and port tie-breaking selection
- Immediate unhealthy marking on failures
- Backend exclusion within a single request
- Scanner probe success and failure cases
- State helper correctness
- Debug endpoint responses
