# Ollama proxy

## Scope

Implement a minimal asynchronous proxy for **non-streaming** Ollama requests.

The proxy will:

* expose one stable HTTP address
* accept `POST /api/generate`
* forward the request to one healthy backend
* retry on another healthy backend if the selected backend fails
* discover healthy backends by periodically probing a fixed local port range
* remove a backend immediately on request/probe failure
* add it back only after a later scan succeeds

No streaming support.
No metadata.
No model-aware routing beyond passing through the client request unchanged.

---

# 1. Assumptions

* all reverse SSH tunnels terminate on the same host where the proxy runs
* each backend is reachable as `http://127.0.0.1:<port>`
* backend ports are within a fixed configured range
* only one proxy instance exists
* only `POST /api/generate` is required
* requests are non-streaming

The client must call Ollama with `"stream": false`.

---

# 2. Technology

Use:

* **FastAPI**
* **httpx.AsyncClient**
* **asyncio**
* **uvicorn**

This is appropriate and keeps the implementation small.

---

# 3. External API

## Supported endpoint

### `POST /api/generate`

Behavior:

* accepts the client JSON body
* forwards it unchanged to a selected backend
* returns the backend JSON response unchanged
* retries on another backend if forwarding fails before a valid response is obtained

## Optional debug endpoints

These are useful and still simple:

### `GET /__proxy/health`

Returns summary:

```json id="6tqad3"
{
  "status": "ok",
  "healthy_backends": 3
}
```

### `GET /__proxy/backends`

Returns backend state:

```json id="3s4z7q"
{
  "backends": [
    {"port": 24001, "healthy": true, "in_flight": 0},
    {"port": 24002, "healthy": false, "in_flight": 0}
  ]
}
```

---

# 4. Backend discovery

## Port range

Configured fixed range, for example:

* `24000-24100`

Each Ollama worker is expected to appear on one of these ports via reverse SSH tunnel.

## Probe method

Use:

* `GET /api/tags`

This is cheap and sufficient.

A backend is healthy only if:

* connection succeeds
* HTTP response status is `200`

No deeper validation is needed.

## Scan loop

Run a background task every few seconds.

Suggested default:

* scan interval: `5 s`

For each port in the range:

* send probe request to `http://127.0.0.1:<port>/api/tags`
* if probe succeeds: mark healthy
* if probe fails: mark unhealthy

This gives the exact behavior you asked for:
remove on failure, add again only after periodic scan succeeds.

---

# 5. Backend state

Maintain only minimal in-memory state.

## Backend record

```python id="y3vi9o"
@dataclass
class Backend:
    port: int
    healthy: bool = False
    in_flight: int = 0
```

## Global state

```python id="p3w597"
backends: dict[int, Backend]
state_lock: asyncio.Lock
```

That is enough.

---

# 6. Load balancing

Use **least in-flight requests**.

For each request:

* choose among `healthy=True` backends
* select the backend with the lowest `in_flight`
* break ties by port number

This is simple and works well for LLM requests.

If there are no healthy backends:

* return `503 Service Unavailable`

---

# 7. Retry policy

The proxy retries only for backend-side transport or server failures.

## Retryable failures

Retry on:

* connection refused
* timeout
* transport error
* backend HTTP `5xx`

Do not retry on:

* backend HTTP `4xx`
* malformed client request that the backend rejects normally

## Retry behavior

For each client request:

* try one selected healthy backend
* if it fails with a retryable error:

  * mark that backend unhealthy immediately
  * retry on another healthy backend
* stop after `MAX_RETRIES`

Suggested default:

* `MAX_RETRIES = 2`

Meaning up to 3 total attempts.

A backend already tried for one request must not be tried again for that same request.

---

# 8. Request forwarding

## Incoming request requirements

The proxy accepts JSON body for `POST /api/generate`.

Expected client usage:

```json id="97bh4g"
{
  "model": "my-model",
  "prompt": "Hello",
  "stream": false
}
```

The proxy does not modify the payload, except optionally enforcing `"stream": false`.

Recommended minimal behavior:

* parse JSON body
* if `"stream"` is missing, set it to `false`
* if `"stream": true`, reject with `400`

This keeps the implementation non-streaming by contract.

## Forwarding behavior

For the selected backend:

* send `POST http://127.0.0.1:<port>/api/generate`
* forward JSON body
* return backend status code and JSON body to client

No streaming and no chunked pass-through needed.

---

# 9. Failure semantics

## Probe failure

If a periodic health probe fails:

* mark backend unhealthy

## Request failure

If a proxied request to a backend fails:

* mark backend unhealthy immediately
* retry on another backend if allowed

## Recovery

A backend becomes healthy again only when a future scan succeeds.

No other recovery path is needed.

---

# 10. Concurrency rules

Use a lock only for short state changes:

* selecting backend
* incrementing `in_flight`
* decrementing `in_flight`
* marking healthy/unhealthy

Do not hold the lock while making HTTP requests.

This keeps the proxy simple and scalable enough.

---

# 11. Configuration

Use environment variables or one small config object.

## Required settings

```text id="oc1a9m"
PROXY_HOST=0.0.0.0
PROXY_PORT=11434

BACKEND_HOST=127.0.0.1
BACKEND_PORT_START=24000
BACKEND_PORT_END=24100

SCAN_INTERVAL_SECONDS=5
PROBE_TIMEOUT_SECONDS=1.0

MAX_RETRIES=2

BACKEND_CONNECT_TIMEOUT_SECONDS=2.0
BACKEND_READ_TIMEOUT_SECONDS=600.0
```

## Optional

```text id="uvn1n0"
REJECT_STREAMING=true
LOG_LEVEL=INFO
```

---

# 12. Internal module layout

Keep it small.

```text id="gmfrf3"
proxy/
  app.py
  config.py
  state.py
  scanner.py
  forwarder.py
```

## Responsibilities

### `config.py`

Loads settings.

### `state.py`

Defines `Backend` and backend registry helpers.

### `scanner.py`

Runs background scan loop and health probes.

### `forwarder.py`

Selects backend, forwards request, handles retries.

### `app.py`

Creates FastAPI app, startup task, routes.

---

# 13. Core logic definitions

## 13.1 Scan loop

```python id="rdm0vb"
while True:
    for port in configured_range:
        probe http://127.0.0.1:{port}/api/tags
        if success:
            mark healthy
        else:
            mark unhealthy
    sleep(scan_interval)
```

## 13.2 Backend selection

```python id="q8n1lo"
healthy_backends = [b for b in backends.values() if b.healthy and b.port not in excluded]
if not healthy_backends:
    raise NoBackendAvailable
return min(healthy_backends, key=lambda b: (b.in_flight, b.port))
```

## 13.3 Generate request handling

```python id="1l8l4m"
attempted = set()

for attempt in range(MAX_RETRIES + 1):
    backend = choose_backend(excluded=attempted)
    attempted.add(backend.port)
    increment in_flight
    try:
        forward POST /api/generate
        if success:
            return response
        if backend returned 5xx:
            mark unhealthy
            continue
        return response
    except retryable transport error:
        mark unhealthy
        continue
    finally:
        decrement in_flight

return 502
```

---

# 14. Client-visible errors

## No backends available

Return:

* `503 Service Unavailable`

Example:

```json id="xx5l5d"
{
  "error": "No healthy Ollama backends available"
}
```

## All retries exhausted

Return:

* `502 Bad Gateway`

Example:

```json id="5s7h7g"
{
  "error": "All backend attempts failed"
}
```

## Streaming requested

Return:

* `400 Bad Request`

Example:

```json id="j2r8ma"
{
  "error": "Streaming is not supported by this proxy; use stream=false"
}
```

---

# 15. Non-goals

Not included:

* streaming
* generic proxying of all Ollama endpoints
* model-based routing
* backend registration
* persistence
* multiple proxy instances
* request queueing
* authentication
* metrics backend

---

# 16. Final minimal definition

Implement a single-process async FastAPI service that:

* exposes `POST /api/generate`
* accepts only non-streaming requests
* keeps an in-memory list of healthy backends
* discovers backends by scanning a configured localhost port range
* uses `GET /api/tags` as the health probe
* routes requests to the healthy backend with the lowest `in_flight`
* marks a backend unhealthy immediately on probe or request failure
* retries the request on another healthy backend up to a fixed limit
* re-enables a backend only when a later periodic scan succeeds

This is the simplest design consistent with your requirements.

I can now turn this revised definition into a concrete FastAPI code skeleton.
