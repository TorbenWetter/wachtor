# agent-gate

An execution gateway for AI agents on untrusted devices. Agents request, policies decide, humans approve, the gateway executes.

## Reference Docs

The project has been thoroughly designed before implementation:

- **[docs/0-spec.md](docs/0-spec.md)** — Project specification: problem, architecture, key decisions, v1 scope, interfaces, protocol
- **[docs/1-architecture.md](docs/1-architecture.md)** — Detailed component designs: all 11 modules with code, schemas, lifecycles, security model

These docs have been reviewed by 3 specialized critic agents (consistency, feasibility, security). All critical and major issues were resolved. Read these before writing any code.

## Tech Stack

- Python 3.12+
- `websockets` >= 14.0 — WebSocket server (agent connections)
- `python-telegram-bot[callback-data]` >= 21.0 — Telegram Guardian bot
- `aiosqlite` >= 0.20.0 — async SQLite
- `aiohttp` >= 3.10.0 — HTTP client for Home Assistant
- `pyyaml` >= 6.0 — config loading

## Project Structure

```
agent-gate/
├── src/agent_gate/           # Main package
│   ├── __init__.py
│   ├── __main__.py           # CLI entrypoint + orchestration
│   ├── config.py             # YAML loading + env var substitution
│   ├── models.py             # Dataclasses (Decision, ToolRequest, etc.)
│   ├── engine.py             # Permission engine (fnmatch, signature builders)
│   ├── executor.py           # Action dispatch (tool → service mapping)
│   ├── server.py             # WebSocket server + pending request mgmt
│   ├── db.py                 # SQLite (audit_log, pending_requests)
│   ├── client.py             # Agent SDK (AgentGateClient)
│   ├── messenger/
│   │   ├── base.py           # MessengerAdapter ABC
│   │   └── telegram.py       # Telegram Guardian bot (PTB v21)
│   └── services/
│       ├── base.py           # ServiceHandler ABC
│       └── homeassistant.py  # HA REST API client
├── tests/                    # pytest tests (TDD — write first)
├── specs/                    # /spec output files
├── docs/                     # Reference docs (spec + architecture)
├── config.example.yaml
├── permissions.example.yaml
├── pyproject.toml
├── Dockerfile
└── docker-compose.yml
```

## Implementation Plan

Three specs, implemented in order. Each produces its own tasks with acceptance criteria.

### Spec 1: Core Engine (pure logic, no I/O) — DONE

Models, config loading, permission engine, SQLite storage, action dispatcher. All modules are independently testable with no network dependencies.

- `models.py` — Decision enum, ToolRequest, ToolResult, PendingApproval, AuditEntry
- `config.py` — YAML loading, recursive env var substitution, typed dataclasses, validation
- `engine.py` — Signature builders (per-tool), input validation, fnmatch evaluation, deny > allow > ask
- `db.py` — Schema creation, audit logging, pending request CRUD, stale cleanup, resolution updates
- `executor.py` — TOOL_SERVICE_MAP, dispatch, unknown tool rejection

### Spec 2: Gateway Server (networking, depends on Spec 1) — DONE

WebSocket server, Telegram bot, Home Assistant client, main orchestration.

- `server.py` — WS server, JSON-RPC parsing, auth flow (10s deadline), rate limiting, pending approval persistence, offline result storage
- `telegram.py` — PTB manual lifecycle (NOT run_polling), PicklePersistence, approval messages, timeout tasks, InvalidCallbackData handling, asyncio.Lock race-safe resolution
- `homeassistant.py` — aiohttp session, HA REST API mapping, health check (GET /api/), error mapping (401/404/connection)
- `__main__.py` — Full orchestration: config → db → services → health checks → PTB start → WS serve → signal handling → shutdown

### Spec 3: SDK + Packaging (depends on Spec 2) — TODO

Client library, Docker deployment, integration tests, pyproject.toml.

- `client.py` — AgentGateClient with async context manager, auto-reconnection, typed errors
- Docker — Dockerfile + docker-compose.yml with TLS, volumes, env_file
- Integration test — end-to-end with mocked HA + Telegram
- `pyproject.toml` — package metadata, entry points, dependency pins

## Development Workflow

Use the YourVid claude-code-plugins:

```
/spec "Core engine — models, config, permissions, storage, executor"
  → produces specs/core-engine.md with acceptance criteria + tasks
/implement specs/core-engine.md
  → TDD: writes tests first, then implementation
  → creates .tasks/core-engine/ manifest
/review
  → verifies code quality + acceptance criteria
/commit
  → conventional commit with spec traceability
```

Repeat for each spec phase.

## Key Patterns

### PTB Event Loop

PTB v21's `run_polling()` creates its own event loop — DO NOT use it. Use manual lifecycle:

```python
async with ptb_app:
    await ptb_app.start()
    await ptb_app.updater.start_polling()
    # ... run websockets.serve() here ...
    await ptb_app.updater.stop()
    await ptb_app.stop()
```

### Permission Engine Precedence

Deny always wins: `deny rules → allow rules → ask rules → defaults (first match) → fallback (ask)`

A deny rule matching `ha_call_service(lock.*)` overrides a more specific allow rule. This is by design.

### Signature Builders

Each tool type has an explicit builder (not raw dict iteration):

```python
"ha_call_service" → f"ha_call_service({domain}.{service}, {entity_id})"
"ha_get_state"    → f"ha_get_state({entity_id})"
```

Fallback for unknown tools: sorted keys for determinism.

### Input Validation

Reject argument values containing: `* ? [ ] ( ) , \x00-\x1f`
HA identifiers must match: `^[a-z_][a-z0-9_]*(\.[a-z0-9_]+)?$`

### Transport Security

WSS (TLS) required by default. Plaintext `ws://` only with explicit `--insecure` flag.

## Linting & Formatting

- **Ruff** — single tool for linting + formatting, configured in `pyproject.toml`
- Lint: `ruff check src/ tests/`
- Format: `ruff format --check src/ tests/`
- Pre-commit hook (`.git/hooks/pre-commit`) runs: ruff format → ruff check → pytest

## Testing

- TDD: write tests BEFORE implementation
- Use `pytest` + `pytest-asyncio` (asyncio_mode = "auto")
- Mock external services: WebSocket (for server tests), Telegram Bot API (for telegram.py), HA REST API (for homeassistant.py)
- Unit tests per module, integration test for full flow
- 231 tests across 11 test files
