# Feature: Extensible YAML Tool System + CLI Client

> Replace hardcoded Home Assistant tools with a YAML-driven tool definition system, add a CLI client for agent integration, and enable any HTTP API to be connected without writing Python code.

## Overview

agent-gate v1 has hardcoded tool definitions (Home Assistant only) spread across `executor.py` (TOOL_SERVICE_MAP), `engine.py` (SIGNATURE_BUILDERS, HA_IDENTIFIER_RE), `services/homeassistant.py` (HTTP endpoint mapping), and `config.py` (HomeAssistantConfig). This spec replaces all of that with:

1. **YAML tool definitions** — Each tool's args, signature template, HTTP request, and response handling declared in YAML
2. **Generic HTTP service handler** — A single handler that reads YAML and constructs HTTP requests dynamically
3. **CLI client** — `agent-gate request <tool> key=value` for any agent with shell access (replaces mcporter for OpenClaw)
4. **Tool discovery** — `agent-gate tools` queries the gateway for available tools and their schemas
5. **Python plugin escape hatch** — `handler: python` in service config for non-HTTP protocols

This enables anyone to connect any HTTP API by writing a YAML file — zero Python code needed.

## Requirements

### Functional Requirements

- [ ] FR1: Tool definitions loaded from YAML files referenced in `config.yaml` per service
  - AC: A `tools/homeassistant.yaml` file defines all 4 HA tools and produces identical behavior to current implementation
  - AC: Adding a new tool YAML file + service config makes the tool available without code changes

- [ ] FR2: Generic HTTP service handler (`GenericHTTPService`) replaces `HomeAssistantService`
  - AC: Supports GET, POST, PUT, DELETE, PATCH methods
  - AC: Path template interpolation: `/api/states/{entity_id}` → `/api/states/sensor.temp`
  - AC: Body construction with `body_exclude` (args excluded from POST body)
  - AC: Response wrapping with optional `wrap` key
  - AC: Service-level error mapping (HTTP status → error message)

- [ ] FR3: Authentication support for Bearer token, custom header, query parameter, and Basic auth
  - AC: `auth.type: bearer` sends `Authorization: Bearer <token>`
  - AC: `auth.type: header` sends custom header (e.g., `X-API-Key: <token>`)
  - AC: `auth.type: query` appends query parameter (e.g., `?api_key=<token>`)
  - AC: `auth.type: basic` sends `Authorization: Basic base64(user:pass)`

- [ ] FR4: Signature templates in YAML replace hardcoded `SIGNATURE_BUILDERS`
  - AC: `signature: "{domain}.{service}, {entity_id}"` produces `ha_call_service(light.turn_on, light.bedroom)`
  - AC: Empty/absent signature produces tool name only (e.g., `ha_get_states`)
  - AC: Fallback for tools without a definition: sorted keys (existing behavior preserved)

- [ ] FR5: Per-arg validation patterns in YAML replace hardcoded `HA_IDENTIFIER_RE`
  - AC: `validate: "^[a-z_][a-z0-9_]*(\\.[a-z0-9_]+)?$"` on `entity_id` rejects invalid identifiers
  - AC: Global `FORBIDDEN_CHARS_RE` still applies to all args regardless of YAML definition
  - AC: Gateway validates required args before dispatch, returns clear JSON-RPC error if missing

- [ ] FR6: `ToolRegistry` aggregates all tool definitions and replaces `TOOL_SERVICE_MAP`
  - AC: `Executor` uses registry for tool→service lookup instead of hardcoded map
  - AC: `PermissionEngine` uses registry for signature building and arg validation
  - AC: Duplicate tool names across services raise `ConfigError` at startup

- [ ] FR7: CLI subcommand `agent-gate request <tool> [key=value ...]` sends one-shot tool requests
  - AC: Connects to gateway via WebSocket, authenticates, sends request, prints JSON result to stdout, exits
  - AC: Exit codes: 0=success, 1=denied, 2=timeout, 3=connection error, 4=invalid args
  - AC: Errors printed to stderr, JSON result to stdout (clean output for piping)
  - AC: Gateway URL from `--url` flag or `AGENT_GATE_URL` env var
  - AC: Token from `--token` flag or `AGENT_TOKEN` env var
  - AC: Timeout via `--timeout` flag (default: 900s)

- [ ] FR8: CLI subcommand `agent-gate serve` starts the gateway server (default when no subcommand)
  - AC: `agent-gate --insecure` still works (backward compatible)
  - AC: `agent-gate serve --insecure` also works
  - AC: Existing deployments unaffected

- [ ] FR9: CLI subcommand `agent-gate tools` lists available tools from the gateway
  - AC: Queries gateway via new `list_tools` JSON-RPC method
  - AC: Returns JSON array with tool name, description, service, and arg schema (name, required, validate pattern)
  - AC: Requires authentication (same `--url`/`--token` as `request`)

- [ ] FR10: CLI subcommand `agent-gate pending` retrieves offline results
  - AC: Queries gateway via existing `get_pending_results` JSON-RPC method
  - AC: Returns JSON array of results, exit code 0

- [ ] FR11: `list_tools` JSON-RPC method on the gateway server
  - AC: Returns `{"tools": [{"name": "...", "description": "...", "service": "...", "args": {...}}]}`
  - AC: Requires authentication (only accessible after auth handshake)
  - AC: Args include `required` and `validate` fields per argument

- [ ] FR12: Python plugin escape hatch via `handler: python` in service config
  - AC: `handler_class: "module.path:ClassName"` loads a Python class implementing `ServiceHandler`
  - AC: Plugin receives `ServiceConfig` and `list[ToolDefinition]` from YAML
  - AC: Falls back to generic HTTP if `handler` is not specified or is `http`

- [ ] FR13: Service-level health check configuration in YAML
  - AC: `health.method`, `health.path`, `health.expect_status` configurable per service
  - AC: Default: `GET /` expecting 200

### Non-Functional Requirements

- [ ] NFR1: All 276 existing tests pass after migration (with targeted modifications to test APIs)
- [ ] NFR2: HA YAML tools produce byte-identical signatures to current hardcoded builders
- [ ] NFR3: No new Python dependencies (aiohttp, pyyaml, websockets already available)
- [ ] NFR4: Config format is a clean break — no backward compatibility with v1 service config
- [ ] NFR5: CLI output is JSON only — no decorative text, no logging to stdout

## User Experience

### User Flows

1. **Adding a new API service (primary flow):**
   - Create `tools/my_api.yaml` defining tools with args, HTTP requests, responses
   - Add service to `config.yaml` with URL, auth, health check, and `tools: tools/my_api.yaml`
   - Add permission rules to `permissions.yaml` for the new tools
   - Restart gateway → new tools available

2. **CLI tool request (OpenClaw integration):**
   - `agent-gate request ha_call_service domain=light service=turn_on entity_id=light.bedroom`
   - Gateway evaluates permissions → if ask, sends Telegram approval → waits → executes
   - JSON result printed to stdout, agent parses it

3. **Tool discovery:**
   - `agent-gate tools --url wss://gateway:8443 --token mytoken`
   - Prints JSON array of available tools with descriptions and arg schemas
   - AI agent uses this to construct valid tool requests

4. **CLI pending results retrieval:**
   - `agent-gate pending --url wss://gateway:8443 --token mytoken`
   - Prints JSON array of results from requests resolved while agent was disconnected

### CLI States

- **Success:** JSON to stdout, exit 0
- **Denied:** `Error: Denied (-32003): Denied by policy` to stderr, exit 1
- **Timeout:** `Error: Timeout (-32002): Approval timed out` to stderr, exit 2
- **Connection error:** `Error: Connection failed: ...` to stderr, exit 3
- **Invalid args:** `Error: Invalid argument format...` to stderr, exit 4

### Edge Cases

| Scenario | Expected Behavior |
| -------- | ----------------- |
| Two services define the same tool name | `ConfigError` at startup — tool names must be globally unique |
| Tool YAML file referenced in config doesn't exist | `ConfigError` at startup |
| HTTP service returns non-JSON response | `HTTPServiceError` — "Expected JSON response" |
| Required arg missing in tool request | JSON-RPC error `-32600`: "Missing required argument: entity_id" |
| Arg fails validation regex | JSON-RPC error `-32600`: "Invalid value for entity_id" |
| YAML tool file has invalid regex in `validate` | `ConfigError` at startup (regex compiled at load time) |
| No `--url` and no `AGENT_GATE_URL` for CLI request | Error to stderr, exit 3 |
| Gateway unreachable during CLI request | Error to stderr, exit 3 (no auto-reconnect, `max_retries=0`) |
| `handler: python` with non-importable class | `ConfigError` at startup |
| Empty tools file (no tools defined) | Warning at startup, service has no tools |
| Service with no health check config | Default: GET / expecting 200 |

## Technical Design

### Affected Components

- `src/agent_gate/config.py` — New dataclasses, generic service loading, tool YAML loading
- `src/agent_gate/registry.py` — NEW: ToolRegistry mapping tool names → definitions + services
- `src/agent_gate/engine.py` — Registry-aware signature building + arg validation
- `src/agent_gate/executor.py` — Registry-based dispatch (remove TOOL_SERVICE_MAP)
- `src/agent_gate/services/http.py` — NEW: GenericHTTPService (replaces homeassistant.py)
- `src/agent_gate/services/homeassistant.py` — Deprecated compatibility wrapper
- `src/agent_gate/cli.py` — NEW: CLI request/tools/pending commands
- `src/agent_gate/__main__.py` — Subcommand routing, generic service init
- `src/agent_gate/server.py` — New `list_tools` method handler
- `tools/homeassistant.yaml` — NEW: HA tool definitions
- `config.example.yaml` — Updated service format

### Data Model

#### Tool Definition (loaded from YAML)

```python
@dataclass
class ArgDefinition:
    required: bool = False
    validate: str | None = None       # regex pattern string
    _compiled: re.Pattern | None = None  # compiled at load time, not serialized

@dataclass
class RequestDefinition:
    method: str                         # GET, POST, PUT, DELETE, PATCH
    path: str                           # "/api/states/{entity_id}"
    body_exclude: list[str] | None = None  # args excluded from POST body

@dataclass
class ResponseDefinition:
    wrap: str | None = None             # wrap response in {wrap: data}

@dataclass
class ToolDefinition:
    name: str
    service_name: str
    description: str = ""
    signature: str = ""                 # "{domain}.{service}, {entity_id}"
    args: dict[str, ArgDefinition] = field(default_factory=dict)
    request: RequestDefinition | None = None
    response: ResponseDefinition | None = None
```

#### Service Config (replaces HomeAssistantConfig)

```python
@dataclass
class AuthConfig:
    type: str                           # bearer, header, query, basic
    token: str = ""
    header_name: str = ""               # for type=header
    query_param: str = ""               # for type=query
    username: str = ""                  # for type=basic
    password: str = ""                  # for type=basic

@dataclass
class HealthCheckConfig:
    method: str = "GET"
    path: str = "/"
    expect_status: int = 200

@dataclass
class ErrorMapping:
    status: int
    message: str                        # supports {status}, {body} templates

@dataclass
class ServiceConfig:
    name: str
    url: str
    auth: AuthConfig
    handler: str = "http"               # "http" or "python"
    handler_class: str = ""             # for handler=python: "module:Class"
    health: HealthCheckConfig = field(default_factory=HealthCheckConfig)
    tools_file: str = ""
    tools: list[ToolDefinition] = field(default_factory=list)
    errors: list[ErrorMapping] = field(default_factory=list)
```

#### ToolRegistry

```python
class ToolRegistry:
    def __init__(self, tools: dict[str, ToolDefinition]) -> None: ...
    def get_tool(self, name: str) -> ToolDefinition | None: ...
    def get_service_name(self, name: str) -> str | None: ...
    def get_signature_parts(self, name: str, args: dict) -> list[str]: ...
    def get_arg_validators(self, name: str) -> dict[str, re.Pattern]: ...
    def get_required_args(self, name: str) -> set[str]: ...
    def all_tools(self) -> list[ToolDefinition]: ...
```

### YAML Schema

#### config.yaml (new service format)

```yaml
gateway:
  host: "0.0.0.0"
  port: 8443

agent:
  token: "${AGENT_TOKEN}"

messenger:
  type: "telegram"
  telegram:
    token: "${GUARDIAN_BOT_TOKEN}"
    chat_id: -1003460470806
    allowed_users: [242965295]

services:
  homeassistant:
    url: "https://homeassistant.osterstras.se"
    auth:
      type: bearer
      token: "${HA_TOKEN}"
    health:
      method: GET
      path: "/api/"
      expect_status: 200
    tools: tools/homeassistant.yaml
    errors:
      - status: 401
        message: "Service authentication failed (HA token expired?)"
      - status: 404
        message: "Entity not found"

storage:
  type: "sqlite"
  path: "./data/agent-gate.db"
```

#### tools/homeassistant.yaml

```yaml
tools:
  ha_get_state:
    description: "Get entity state from Home Assistant"
    signature: "{entity_id}"
    args:
      entity_id:
        required: true
        validate: "^[a-z_][a-z0-9_]*(\\.[a-z0-9_]+)?$"
    request:
      method: GET
      path: "/api/states/{entity_id}"

  ha_get_states:
    description: "Get all entity states from Home Assistant"
    request:
      method: GET
      path: "/api/states"
    response:
      wrap: "states"

  ha_call_service:
    description: "Call a Home Assistant service"
    signature: "{domain}.{service}, {entity_id}"
    args:
      domain:
        required: true
        validate: "^[a-z_][a-z0-9_]*$"
      service:
        required: true
        validate: "^[a-z_][a-z0-9_]*$"
      entity_id:
        required: false
        validate: "^[a-z_][a-z0-9_]*(\\.[a-z0-9_]+)?$"
    request:
      method: POST
      path: "/api/services/{domain}/{service}"
      body_exclude: [domain, service]
    response:
      wrap: "result"

  ha_fire_event:
    description: "Fire a Home Assistant event"
    signature: "{event_type}"
    args:
      event_type:
        required: true
        validate: "^[a-z_][a-z0-9_]*$"
    request:
      method: POST
      path: "/api/events/{event_type}"
      body_exclude: [event_type]
```

#### Python plugin service example

```yaml
services:
  custom_mqtt:
    url: "mqtt://broker:1883"
    auth:
      type: header
      token: "${MQTT_TOKEN}"
    handler: python
    handler_class: "my_plugin:MQTTService"
    tools: tools/mqtt.yaml
```

### Protocol Changes

New JSON-RPC method `list_tools`:

```json
// Request
{"jsonrpc": "2.0", "method": "list_tools", "params": {}, "id": 1}

// Response
{
  "jsonrpc": "2.0",
  "result": {
    "tools": [
      {
        "name": "ha_get_state",
        "description": "Get entity state from Home Assistant",
        "service": "homeassistant",
        "args": {
          "entity_id": {"required": true, "validate": "^[a-z_][a-z0-9_]*(\\.[a-z0-9_]+)?$"}
        }
      },
      {
        "name": "ha_call_service",
        "description": "Call a Home Assistant service",
        "service": "homeassistant",
        "args": {
          "domain": {"required": true, "validate": "^[a-z_][a-z0-9_]*$"},
          "service": {"required": true, "validate": "^[a-z_][a-z0-9_]*$"},
          "entity_id": {"required": false, "validate": "^[a-z_][a-z0-9_]*(\\.[a-z0-9_]+)?$"}
        }
      }
    ]
  },
  "id": 1
}
```

### CLI Structure

```
agent-gate
  |
  +-- (no subcommand)  --> serve (backward compatible)
  |     --insecure
  |     --config PATH
  |     --permissions PATH
  |
  +-- serve
  |     --insecure
  |     --config PATH
  |     --permissions PATH
  |
  +-- request <tool> [key=value ...]
  |     --url URL          (or AGENT_GATE_URL env var)
  |     --token TOKEN      (or AGENT_TOKEN env var)
  |     --timeout SECONDS  (default: 900)
  |
  +-- tools
  |     --url URL          (or AGENT_GATE_URL env var)
  |     --token TOKEN      (or AGENT_TOKEN env var)
  |
  +-- pending
        --url URL          (or AGENT_GATE_URL env var)
        --token TOKEN      (or AGENT_TOKEN env var)
```

### Dependencies

- **Existing:** aiohttp, pyyaml, websockets, python-telegram-bot, aiosqlite (all already in pyproject.toml)
- **New:** None

## Implementation Plan

### Task 1: Core Data Structures + Tool Loading + Registry

**Scope:** New dataclasses, YAML tool file loading, ToolRegistry, HA YAML file

Files:
- `src/agent_gate/config.py` — Add ArgDefinition, RequestDefinition, ResponseDefinition, ToolDefinition, AuthConfig, HealthCheckConfig, ErrorMapping, ServiceConfig. Add `load_tools_file()`.
- `src/agent_gate/registry.py` — Create ToolRegistry with all lookup methods + `build_registry()` factory.
- `tools/homeassistant.yaml` — Create HA tool definitions.
- `tests/test_tool_loading.py` — Tests for YAML loading, validation, regex compilation, env var substitution.
- `tests/test_registry.py` — Tests for ToolRegistry: lookup, duplicate detection, signature parts, arg validators.

**Acceptance:**
- [ ] `load_tools_file("tools/homeassistant.yaml", "homeassistant")` returns 4 ToolDefinition objects
- [ ] ToolRegistry correctly maps tool names to service names and definitions
- [ ] Duplicate tool names across services raise ConfigError
- [ ] Invalid regex in `validate` raises ConfigError at load time
- [ ] All 276 existing tests still pass (this is purely additive)

### Task 2: Engine Refactoring (Registry-Aware)

**Scope:** Make signature building and arg validation use ToolRegistry

Files:
- `src/agent_gate/engine.py` — Add optional `registry` param to `validate_args()` and `build_signature()`. Add `PermissionEngine(permissions, registry=...)`.
- `tests/test_engine.py` — Add registry fixtures, test both with and without registry, add parity test comparing old hardcoded output vs YAML-driven output.

**Acceptance:**
- [ ] `build_signature("ha_call_service", {"domain": "light", "service": "turn_on", "entity_id": "light.bedroom"}, registry)` produces `"ha_call_service(light.turn_on, light.bedroom)"` — identical to current
- [ ] `validate_args()` with registry checks required args and per-arg patterns from YAML
- [ ] Without registry (default), existing behavior preserved exactly
- [ ] Parity test: all 4 HA tools produce identical signatures via YAML vs hardcoded

### Task 3: Generic HTTP Service + HA Migration

**Scope:** GenericHTTPService replaces HomeAssistantService

Files:
- `src/agent_gate/services/http.py` — Create GenericHTTPService implementing ServiceHandler.
- `src/agent_gate/services/homeassistant.py` — Convert to deprecated wrapper.
- `tests/test_http_service.py` — Adapted from test_homeassistant.py, all URL/body/response/error assertions preserved.

**Acceptance:**
- [ ] GenericHTTPService produces identical HTTP requests as current HomeAssistantService
- [ ] Bearer, header, query, and basic auth types work
- [ ] Path interpolation: `/api/states/{entity_id}` → `/api/states/sensor.temp`
- [ ] Body exclude: `ha_call_service` body excludes domain/service
- [ ] Response wrapping: `ha_get_states` wraps as `{"states": data}`, `ha_call_service` wraps as `{"result": data}`
- [ ] Service-level error mapping: 401 → auth error, 404 → not found
- [ ] Health check: configurable method/path/expect_status

### Task 4: Executor + Config Wiring + Server

**Scope:** Wire everything together. Generic config loading, registry-based executor, list_tools method.

Files:
- `src/agent_gate/config.py` — Modify `load_config()` to parse generic ServiceConfig, call `load_tools_file()`.
- `src/agent_gate/executor.py` — Remove TOOL_SERVICE_MAP, use ToolRegistry.
- `src/agent_gate/server.py` — Add `list_tools` method handler.
- `src/agent_gate/__main__.py` — Generic service init loop, registry construction, pass registry to engine/executor.
- `tests/test_config.py` — Update for new service format.
- `tests/test_executor.py` — Update constructor, registry mock.
- `tests/test_server.py` — Add test for `list_tools` method.
- `tests/test_main.py` — Update mocks.
- `tests/test_integration.py` — Add registry fixture.

**Acceptance:**
- [ ] `load_config()` parses new service format with auth/health/tools/errors
- [ ] Executor uses registry, unknown tools still raise ExecutionError
- [ ] `list_tools` JSON-RPC method returns tool definitions with args
- [ ] Gateway starts with new config, all HA tools work identically
- [ ] All existing tests pass (with targeted modifications)

### Task 5: CLI Client

**Scope:** CLI subcommands: request, tools, pending, serve (backward compat)

Files:
- `src/agent_gate/cli.py` — Create: `run_request()`, `run_tools()`, `run_pending()`, `parse_key_value_args()`, exit code constants.
- `src/agent_gate/__main__.py` — Restructure `parse_args()` with subparsers, update `main()` dispatch.
- `tests/test_cli.py` — Create: unit tests for all CLI functions, exit codes, output format.
- `tests/test_main.py` — Add subcommand parsing tests, backward compat tests.

**Acceptance:**
- [ ] `agent-gate request ha_get_state entity_id=sensor.temp --url ... --token ...` prints JSON to stdout, exits 0
- [ ] Denied request prints error to stderr, exits 1
- [ ] Timeout prints error to stderr, exits 2
- [ ] Connection error prints error to stderr, exits 3
- [ ] Invalid key=value args print error to stderr, exit 4
- [ ] `agent-gate tools` prints JSON tool list
- [ ] `agent-gate pending` prints JSON pending results
- [ ] `agent-gate --insecure` still starts server (backward compat)
- [ ] `agent-gate serve --insecure` also starts server

### Task 6: Python Plugin Escape Hatch

**Scope:** Support `handler: python` in service config

Files:
- `src/agent_gate/__main__.py` — Add plugin loading logic in service init loop.
- `tests/test_plugin.py` — Test plugin loading, error cases.

**Acceptance:**
- [ ] `handler: python` + `handler_class: "module:Class"` loads the class
- [ ] Plugin receives ServiceConfig and list[ToolDefinition]
- [ ] Non-importable class raises ConfigError at startup
- [ ] Default `handler: http` (or absent) uses GenericHTTPService

### Task 7: Cleanup + Config Examples

**Scope:** Remove dead code, update examples

Files:
- `src/agent_gate/engine.py` — Remove SIGNATURE_BUILDERS, HA_IDENTIFIER_RE, _HA_IDENTIFIER_FIELDS constants.
- `src/agent_gate/executor.py` — Remove TOOL_SERVICE_MAP constant.
- `config.example.yaml` — Update to new service format.
- `permissions.example.yaml` — Add example patterns for generic tools.

**Acceptance:**
- [ ] No dead code referencing old hardcoded tool definitions
- [ ] Example configs are valid and demonstrate the new format
- [ ] All tests pass (final run)

### Task Dependencies

```
T1 (data structures + registry)
 ├── T2 (engine refactoring)        ← depends on T1
 ├── T3 (HTTP service + HA migration) ← depends on T1
 │
 T4 (wiring + server) ← depends on T1, T2, T3
 │
 ├── T5 (CLI client)   ← depends on T4
 ├── T6 (Python plugins) ← depends on T4
 │
 T7 (cleanup)          ← depends on T4, T5, T6
```

**Parallelizable:** T2 and T3 can run in parallel after T1. T5 and T6 can run in parallel after T4.

## Test Plan

### Unit Tests (per task)

- [ ] `test_tool_loading.py` — YAML parsing, validation, regex compilation, env vars, error cases
- [ ] `test_registry.py` — Tool lookup, service mapping, signature parts, arg validators, duplicates
- [ ] `test_engine.py` — Signature parity (YAML vs hardcoded), required arg validation, registry passthrough
- [ ] `test_http_service.py` — All 4 auth types, path interpolation, body construction, response wrapping, error mapping, health check
- [ ] `test_executor.py` — Registry-based dispatch, unknown tools, missing services
- [ ] `test_server.py` — `list_tools` method handler
- [ ] `test_cli.py` — Key=value parsing, all exit codes, stdout/stderr output, env var fallbacks
- [ ] `test_main.py` — Subcommand parsing, backward compat, dispatch routing
- [ ] `test_plugin.py` — Plugin loading, error cases

### Integration Tests

- [ ] Full flow: CLI request → gateway → permission engine → generic HTTP service → mock API → JSON response
- [ ] CLI tools → gateway → list_tools → JSON tool list
- [ ] Auto-allowed, ask+approve, deny flows via CLI (same as v1 integration tests but through CLI)

### Manual Testing

- [ ] Start gateway with new config format, verify HA tools work via real test
- [ ] Use `agent-gate tools` against running gateway
- [ ] Use `agent-gate request ha_call_service domain=light service=turn_on entity_id=light.bedroom` against real HA

## Open Questions

_None — all questions resolved during discovery._

## Decision Log

| Decision | Rationale | Date |
| -------- | --------- | ---- |
| HTTP-only YAML tools + Python ABC escape hatch | 99% of APIs agents call are HTTP/REST. MQTT/gRPC are rare and don't fit request-response model. ServiceHandler ABC already exists for exotic protocols. | 2026-02-08 |
| All common auth patterns (bearer, header, query, basic) | Maximum API compatibility with minimal code. Each is ~5 lines in GenericHTTPService. | 2026-02-08 |
| `list_tools` JSON-RPC method for tool discovery | Gateway is source of truth. Remote agents don't have local YAML files. Similar to mcporter's `list` command. | 2026-02-08 |
| Clean config break (no backward compat) | This is v2. Old configs are simple to migrate. Auto-detection adds permanent complexity for temporary value. | 2026-02-08 |
| Same 4 HA tools for YAML migration | Prove the system works with behavioral parity. Users can add more tools by editing YAML. | 2026-02-08 |
| JSON-only CLI output | CLI's primary consumer is AI agents (OpenClaw). Humans can use `jq`. Avoids format switching complexity. | 2026-02-08 |
| key=value CLI arg syntax | Generic (works for any tool), simple to parse, consistent with curl/httpie conventions. No need for per-tool --flags. | 2026-02-08 |
| Gateway validates required args | Single source of truth. Engine already validates format. Adding required-field checks is natural extension. | 2026-02-08 |
| Service-level error mapping only | Reduces duplication. Per-tool overrides add complexity for rare edge cases. | 2026-02-08 |
| Plugin receives ToolDefinition from YAML | YAML remains the schema source of truth. Plugin uses it for validation/routing. Clean separation. | 2026-02-08 |
| Wrap-key response handling only | Covers all current HA patterns. JMESPath/path extraction adds dependencies for no current use case. | 2026-02-08 |
| CLI replaces mcporter for OpenClaw | agent-gate's CLI is the tool interface. No need for mcporter as intermediary. Cleaner architecture. | 2026-02-08 |
