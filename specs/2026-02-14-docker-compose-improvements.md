# Feature: Docker Compose Improvements

> Add health check endpoint, configurable log level, resource limits, and log rotation to make agentpass production-ready for self-hosters.

## Overview

The current Docker Compose setup works but lacks production essentials: no health monitoring, no log level control, no resource limits, and no log rotation. This spec covers four improvements that make self-hosting agentpass reliable and observable.

## Requirements

### Functional Requirements

- [ ] FR1: Add HTTP `/healthz` endpoint that reports gateway health (DB connectivity, Telegram bot status, service reachability)
- [ ] FR2: Add `LOG_LEVEL` environment variable support (DEBUG, INFO, WARNING, ERROR) with INFO as default
- [ ] FR3: Add Docker `HEALTHCHECK` instruction to Dockerfile that calls `/healthz`
- [ ] FR4: Add resource limits (memory, CPU) to docker-compose.yml
- [ ] FR5: Add Docker log rotation config to docker-compose.yml

### Non-Functional Requirements

- [ ] NFR1: Health endpoint must respond within 2 seconds
- [ ] NFR2: Health endpoint must not require authentication
- [ ] NFR3: Health check HTTP server must not interfere with the WebSocket server

## Technical Design

### 1. Health Check Endpoint (`/healthz`)

A lightweight `aiohttp` HTTP server running alongside the WebSocket server on a separate port (default 8080).

**Response format:**
```json
{
  "status": "healthy",
  "checks": {
    "database": true,
    "telegram": true,
    "services": {
      "homeassistant": true
    }
  }
}
```

HTTP 200 if all checks pass, HTTP 503 if any critical check fails (database or telegram).

**Affected files:**

- `src/agentpass/__main__.py` — Start health HTTP server alongside WebSocket server, read `LOG_LEVEL` env var
- `src/agentpass/server.py` — Add `health_status()` method to `GatewayServer` that checks DB, Telegram, services

**Implementation:**

```python
# In server.py — add to GatewayServer
async def health_status(self) -> dict:
    """Return health status of all components."""
    db_ok = await self._db.health_check()
    telegram_ok = await self._messenger.health_check()
    services_status = {}
    for name, svc in self._executor.services.items():
        services_status[name] = await svc.health_check()

    all_critical_ok = db_ok and telegram_ok
    return {
        "status": "healthy" if all_critical_ok else "unhealthy",
        "checks": {
            "database": db_ok,
            "telegram": telegram_ok,
            "services": services_status,
        },
    }
```

```python
# In __main__.py — add health server
from aiohttp import web

async def _health_handler(request: web.Request) -> web.Response:
    status = await gateway.health_status()
    code = 200 if status["status"] == "healthy" else 503
    return web.json_response(status, status=code)

health_app = web.Application()
health_app.router.add_get("/healthz", _health_handler)
health_runner = web.AppRunner(health_app)
await health_runner.setup()
health_site = web.TCPSite(health_runner, "0.0.0.0", health_port)
await health_site.start()
```

**Health check methods to add:**

- `Database.health_check()` — run `SELECT 1` on the connection
- `TelegramAdapter.health_check()` — check if bot is running (PTB app.running)
- `GenericHTTPService.health_check()` — already exists, reuse it

### 2. LOG_LEVEL Environment Variable

Read `LOG_LEVEL` from environment in `main()`, default to `INFO`.

```python
# In __main__.py — replace hardcoded level
log_level = os.environ.get("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, log_level, logging.INFO),
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
)
```

### 3. Dockerfile HEALTHCHECK

```dockerfile
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8080/healthz')" || exit 1
```

Uses Python stdlib (already in image) — no extra dependencies like curl needed.

### 4. Docker Compose Improvements

```yaml
services:
  agentpass:
    build: .
    ports:
      - "8443:8443"
    volumes:
      - ./config.yaml:/app/config.yaml:ro
      - ./permissions.yaml:/app/permissions.yaml:ro
      - agentpass-data:/app/data
      - ./certs:/app/certs:ro
    env_file: .env
    environment:
      - LOG_LEVEL=INFO
    restart: unless-stopped
    deploy:
      resources:
        limits:
          memory: 256M
          cpus: "0.5"
    logging:
      driver: json-file
      options:
        max-size: "10m"
        max-file: "3"

volumes:
  agentpass-data:
```

**Changes from current:**
- Named volume `agentpass-data` instead of bind mount `./data` (Docker-managed, survives `docker compose down`)
- `LOG_LEVEL` environment variable with default
- Memory limit 256M, CPU limit 0.5 cores (agentpass is lightweight)
- Log rotation: 3 files of 10MB max (30MB total)

### 5. Config: Health Port

Add optional `health_port` to gateway config:

```yaml
gateway:
  host: "0.0.0.0"
  port: 8443
  health_port: 8080  # optional, default 8080
```

## Affected Components

| File | Change |
|------|--------|
| `src/agentpass/__main__.py` | Health HTTP server, LOG_LEVEL env var, health_port config |
| `src/agentpass/server.py` | `health_status()` method on GatewayServer |
| `src/agentpass/db.py` | `health_check()` method on Database |
| `src/agentpass/messenger/telegram.py` | `health_check()` method on TelegramAdapter |
| `src/agentpass/config.py` | `health_port` field on GatewayConfig |
| `Dockerfile` | HEALTHCHECK instruction |
| `docker-compose.yml` | Resource limits, log rotation, named volume, LOG_LEVEL |
| `config.example.yaml` | Document health_port option |

## Implementation Plan

### Phase 1: LOG_LEVEL env var
1. [ ] Read `LOG_LEVEL` from environment in `main()`
2. [ ] Add `LOG_LEVEL=INFO` to docker-compose.yml environment
3. [ ] Add tests

### Phase 2: Health check endpoint
1. [ ] Add `Database.health_check()` method
2. [ ] Add `TelegramAdapter.health_check()` method
3. [ ] Add `GatewayServer.health_status()` method
4. [ ] Add `health_port` to GatewayConfig with default 8080
5. [ ] Start aiohttp health server in `run()`
6. [ ] Shut down health server in shutdown sequence
7. [ ] Add tests for health endpoint

### Phase 3: Docker improvements
1. [ ] Add HEALTHCHECK to Dockerfile
2. [ ] Update docker-compose.yml (resource limits, log rotation, named volume)
3. [ ] Update config.example.yaml with health_port
4. [ ] Expose port 8080 in Dockerfile

## Test Plan

### Unit Tests
- [ ] `Database.health_check()` returns True with open connection, False when closed
- [ ] `TelegramAdapter.health_check()` returns True when bot running, False when stopped
- [ ] `GatewayServer.health_status()` aggregates component health correctly
- [ ] `LOG_LEVEL` env var is respected (DEBUG shows debug messages, WARNING suppresses info)

### Integration Tests
- [ ] Health endpoint returns 200 with all components healthy
- [ ] Health endpoint returns 503 when database is unavailable
- [ ] Health endpoint is accessible without authentication

## Decision Log

| Decision | Rationale | Date |
|----------|-----------|------|
| Separate HTTP port for health (8080) | Avoids mixing HTTP and WebSocket on same port; simpler implementation; standard practice for health endpoints | 2026-02-14 |
| Use aiohttp for health server | Already a dependency (used by GenericHTTPService); lightweight; async | 2026-02-14 |
| Named Docker volume over bind mount | Docker-managed persistence; survives `docker compose down`; portable across hosts | 2026-02-14 |
| Python stdlib for HEALTHCHECK | No need to install curl in slim image; python already available | 2026-02-14 |
| 256M memory limit | agentpass is lightweight (Python + SQLite + WebSocket); 256M is generous | 2026-02-14 |

## Open Questions

_None — all questions resolved during discovery._
