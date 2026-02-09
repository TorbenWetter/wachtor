# agent-gate

**An execution gateway for AI agents on untrusted devices.**

Agents request. Policies decide. Humans approve. The gateway executes.

---

## The Problem

AI agents running on untrusted devices (Raspberry Pi, edge hardware) need access to personal services like Home Assistant, but:

- The device has full shell access -- any credential stored on it can be extracted
- Prompt injection from untrusted content can compromise the agent
- Services like Home Assistant lack fine-grained permission scoping

No existing solution combines **credential isolation**, **policy-based decisions**, and **human-in-the-loop approval** in a single gateway that also executes actions. Existing projects gate the decision but the agent still holds credentials and executes actions itself.

## How It Works

The gateway runs on a **trusted device** (home server, NAS, cloud). The agent runs on an **untrusted device** (Pi). The agent never sees service credentials.

```
Untrusted Device (Pi)              Trusted Device (Gateway)
+--------------+                   +------------------------------+
|              |                   |  agent-gate                  |
|  AI Agent    |                   |  +------------------------+  |
|  (any agent) |-- WebSocket ----> |  |  Permission Engine     |  |
|              |                   |  |  deny > allow > ask    |  |
|  Holds:      |                   |  +----------+-------------+  |
|  - Agent     |                   |             |                 |
|    token     |                   |  +----------v-------------+  |
|  - LLM key   |                   |  |  Messenger Adapter     |  |
|              |<-- result ------- |  |  (Telegram)            |  |
|              |                   |  +----------+-------------+  |
+--------------+                   |             |                 |
                                   |  +----------v-------------+  |
      User <-- Telegram ---------- |  |  Action Executor       |  |
                                   |  |  (Home Assistant)      |  | -- credentials --> Services
                                   |  +------------------------+  |
                                   |                               |
                                   |  Holds: HA token, bot token,  |
                                   |  TLS cert, permission DB      |
                                   +------------------------------+
```

### Security Model

| Property                 | How                                                                                                                                    |
| ------------------------ | -------------------------------------------------------------------------------------------------------------------------------------- |
| **Credential isolation** | Service tokens (HA, Telegram bot) live only on the gateway. The agent device never sees them.                                          |
| **Policy engine**        | Every request is matched against YAML permission rules using glob patterns. Deny always wins.                                          |
| **Human-in-the-loop**    | Requests matching `ask` rules trigger a Telegram message with inline approve/deny buttons.                                             |
| **Transport security**   | WSS (TLS) is required by default. Plaintext only with an explicit `--insecure` flag.                                                   |
| **Input validation**     | Argument values are sanitized -- glob metacharacters, control characters, and malformed HA identifiers are rejected before processing. |
| **Rate limiting**        | Max 10 pending approvals, max 60 auto-allowed requests per minute.                                                                     |

Even if the agent device is fully compromised, it cannot: access service credentials, forge Telegram approval callbacks, bypass the permission engine, or execute actions directly on Home Assistant.

---

## Quick Start

### Installation

```bash
pip install agent-gate
```

For development:

```bash
git clone https://github.com/<user>/agent-gate.git
cd agent-gate
pip install -e ".[dev]"
```

### Configuration

Copy the example files and fill in your values:

```bash
cp config.example.yaml config.yaml
cp permissions.example.yaml permissions.yaml
```

Set the required environment variables (or replace `${...}` placeholders in `config.yaml`):

```bash
export AGENT_TOKEN="your-agent-bearer-token"
export GUARDIAN_BOT_TOKEN="your-telegram-bot-token"
export HA_TOKEN="your-home-assistant-long-lived-access-token"
```

### Running

```bash
# With TLS (production)
agent-gate

# Or via module
python -m agent_gate

# Development mode (no TLS required)
agent-gate --insecure
```

The gateway listens on `wss://0.0.0.0:8443` by default (or `ws://` with `--insecure`).

---

## SDK Usage (Python)

The SDK ships as part of the same package. Agents integrate in a few lines:

```python
from agent_gate import AgentGateClient, AgentGateDenied, AgentGateTimeout

async with AgentGateClient("wss://gateway:8443", token="your-agent-token") as gw:

    # Auto-allowed by policy -- returns immediately
    state = await gw.tool_request("ha_get_state", entity_id="sensor.living_room_temp")
    print(state)  # {"entity_id": "sensor.living_room_temp", "state": "21.3", ...}

    # Requires human approval -- blocks until approved/denied/timeout
    result = await gw.tool_request(
        "ha_call_service",
        domain="light",
        service="turn_on",
        entity_id="light.bedroom",
    )
```

### Error Handling

```python
from agent_gate import AgentGateClient, AgentGateDenied, AgentGateTimeout, AgentGateError

async with AgentGateClient("wss://gateway:8443", token="...") as gw:
    try:
        await gw.tool_request(
            "ha_call_service",
            domain="lock",
            service="lock",
            entity_id="lock.front_door",
        )
    except AgentGateDenied as e:
        print(f"Denied: {e.message}")  # Policy deny or user denied
    except AgentGateTimeout as e:
        print(f"Timed out: {e.message}")  # No response within approval timeout
    except AgentGateError as e:
        print(f"Error {e.code}: {e.message}")  # Other errors
```

### Retrieving Offline Results

If the agent disconnects while approvals are pending, results are queued on the gateway. After reconnection, they are fetched automatically. You can also retrieve them manually:

```python
import json

async with AgentGateClient("wss://gateway:8443", token="...") as gw:
    results = await gw.get_pending_results()
    for r in results:
        # Each row has a "result" column that is a JSON-encoded string
        result_data = json.loads(r["result"]) if isinstance(r["result"], str) else r["result"]
        print(r["request_id"], result_data.get("status"), result_data.get("data"))
```

### Auto-Reconnection

The client automatically reconnects with exponential backoff (1s to 30s) if the WebSocket connection drops. You can limit retry attempts:

```python
client = AgentGateClient("wss://gateway:8443", token="...", max_retries=5)
```

---

## Docker Deployment

### Using Docker Compose

```bash
# Build and start
docker compose up -d

# View logs
docker compose logs -f agent-gate

# Stop
docker compose down
```

The `docker-compose.yml` mounts four volumes:

| Mount                                         | Purpose                                |
| --------------------------------------------- | -------------------------------------- |
| `./config.yaml:/app/config.yaml:ro`           | Gateway configuration (read-only)      |
| `./permissions.yaml:/app/permissions.yaml:ro` | Permission rules (read-only)           |
| `./data:/app/data`                            | SQLite database + Telegram persistence |
| `./certs:/app/certs:ro`                       | TLS certificates (read-only)           |

Secrets are passed via `.env` file (referenced by `env_file: .env` in the compose file):

```env
AGENT_TOKEN=your-agent-bearer-token
GUARDIAN_BOT_TOKEN=your-telegram-bot-token
HA_TOKEN=your-home-assistant-long-lived-access-token
```

### Using Docker Directly

```bash
docker build -t agent-gate .
docker run -d \
  -p 8443:8443 \
  -v $(pwd)/config.yaml:/app/config.yaml:ro \
  -v $(pwd)/permissions.yaml:/app/permissions.yaml:ro \
  -v $(pwd)/data:/app/data \
  -v $(pwd)/certs:/app/certs:ro \
  --env-file .env \
  --restart unless-stopped \
  agent-gate
```

---

## Configuration Reference

### config.yaml

```yaml
gateway:
  host: "0.0.0.0" # Bind address
  port: 8443 # Listen port
  tls:
    cert: "/path/to/cert.pem" # TLS certificate (required unless --insecure)
    key: "/path/to/key.pem" # TLS private key

agent:
  token: "${AGENT_TOKEN}" # Bearer token for agent authentication

messenger:
  type: "telegram" # Messenger backend (only "telegram" in v1)
  telegram:
    token: "${GUARDIAN_BOT_TOKEN}" # Telegram Bot API token
    chat_id: 123456789 # Chat ID for approval messages (negative for groups)
    allowed_users: [123456789] # Telegram user IDs authorized to approve (required)

services:
  homeassistant:
    url: "http://homeassistant.local:8123" # HA base URL
    token: "${HA_TOKEN}" # HA long-lived access token

storage:
  type: "sqlite" # Storage backend (only "sqlite" in v1)
  path: "./data/agent-gate.db" # Database file path

approval_timeout: 900 # Seconds before pending approvals expire (default: 900 = 15 min)

rate_limit:
  max_pending_approvals: 10 # Max simultaneous pending approvals (default: 10)
  max_requests_per_minute: 60 # Max auto-allowed requests per minute (default: 60)
```

**Environment variable substitution:** Any string value containing `${VAR_NAME}` is replaced with the corresponding environment variable at load time. Missing variables cause a startup error.

### permissions.yaml

```yaml
# Defaults are evaluated in order -- put specific patterns before "*"
defaults:
  - pattern: "ha_get_*"
    action: allow
  - pattern: "ha_call_service*"
    action: ask
  - pattern: "*"
    action: ask

# Explicit rules -- deny always wins over allow/ask regardless of specificity
rules:
  - pattern: "ha_call_service(light.*)"
    action: ask
    description: "Light control requires approval"

  - pattern: "ha_call_service(lock.*)"
    action: deny
    description: "Lock control is always denied"

  - pattern: "ha_fire_event(*)"
    action: deny
    description: "Event firing is always denied"
```

### Rule Precedence

The permission engine evaluates in a strict priority order:

1. **Deny rules** -- if any deny rule matches, the request is denied (deny always wins)
2. **Allow rules** -- if any allow rule matches, the request is auto-allowed
3. **Ask rules** -- if any ask rule matches, the request requires human approval
4. **Defaults** -- evaluated in order; first matching pattern wins
5. **Global fallback** -- if nothing matches, the action is `ask`

Patterns use `fnmatch` glob syntax. The pattern is matched against a **signature string** built from the tool name and arguments:

| Tool              | Signature format                                   | Example                                         |
| ----------------- | -------------------------------------------------- | ----------------------------------------------- |
| `ha_call_service` | `ha_call_service({domain}.{service}, {entity_id})` | `ha_call_service(light.turn_on, light.bedroom)` |
| `ha_get_state`    | `ha_get_state({entity_id})`                        | `ha_get_state(sensor.living_room_temp)`         |
| `ha_get_states`   | `ha_get_states`                                    | `ha_get_states`                                 |
| `ha_fire_event`   | `ha_fire_event({event_type})`                      | `ha_fire_event(custom_event)`                   |

---

## JSON-RPC Protocol Reference

For non-Python agents, the gateway exposes a JSON-RPC 2.0 protocol over WebSocket. Any language with WebSocket support can integrate.

### 1. Authentication

Authentication must be the first message, sent within 10 seconds of connecting.

```json
// Agent -> Gateway
{
  "jsonrpc": "2.0",
  "method": "auth",
  "params": {"token": "your-agent-token"},
  "id": "auth-1"
}

// Gateway -> Agent (success)
{
  "jsonrpc": "2.0",
  "result": {"status": "authenticated"},
  "id": "auth-1"
}

// Gateway -> Agent (failure)
{
  "jsonrpc": "2.0",
  "error": {"code": -32005, "message": "Not authenticated"},
  "id": "auth-1"
}
```

### 2. Tool Request

```json
// Agent -> Gateway
{
  "jsonrpc": "2.0",
  "method": "tool_request",
  "params": {
    "tool": "ha_call_service",
    "args": {
      "domain": "light",
      "service": "turn_on",
      "entity_id": "light.bedroom"
    }
  },
  "id": "req-001"
}

// Gateway -> Agent (executed)
{
  "jsonrpc": "2.0",
  "result": {
    "status": "executed",
    "data": { }
  },
  "id": "req-001"
}
```

For `ask` policy decisions, the response is deferred until the human approves or denies (or the approval times out). The WebSocket request stays open -- the agent should await the response.

### 3. Get Pending Results

After reconnecting, retrieve results for requests that were resolved while the agent was offline:

```json
// Agent -> Gateway
{
  "jsonrpc": "2.0",
  "method": "get_pending_results",
  "params": {},
  "id": "reconn-1"
}

// Gateway -> Agent
{
  "jsonrpc": "2.0",
  "result": {
    "results": [
      {"request_id": "req-042", "result": "{\"status\":\"executed\",\"data\":{}}", "tool_name": "ha_call_service"},
      {"request_id": "req-043", "result": "{\"status\":\"denied\",\"data\":null}", "tool_name": "ha_call_service"}
    ]
  },
  "id": "reconn-1"
}
```

### Error Codes

| Code     | Meaning                                                             |
| -------- | ------------------------------------------------------------------- |
| `-32700` | Parse error (malformed JSON)                                        |
| `-32600` | Invalid request (missing fields, forbidden characters in arguments) |
| `-32601` | Method not found                                                    |
| `-32001` | Approval denied by user                                             |
| `-32002` | Approval timed out                                                  |
| `-32003` | Policy denied (no human involved)                                   |
| `-32004` | Action execution failed (service error)                             |
| `-32005` | Not authenticated                                                   |
| `-32006` | Rate limit exceeded                                                 |

### Available Tools (v1)

| Tool              | Description           | Args                             |
| ----------------- | --------------------- | -------------------------------- |
| `ha_get_state`    | Get entity state      | `entity_id`                      |
| `ha_get_states`   | Get all entity states | (none)                           |
| `ha_call_service` | Call an HA service    | `domain`, `service`, `entity_id` |
| `ha_fire_event`   | Fire an HA event      | `event_type`                     |

---

## Development

### Setup

```bash
git clone https://github.com/<user>/agent-gate.git
cd agent-gate
pip install -e ".[dev]"
```

### Running Tests

```bash
pytest
pytest --cov=agent_gate          # with coverage
pytest tests/test_engine.py -v   # single module
```

### Linting and Formatting

```bash
ruff check src/ tests/           # lint
ruff format src/ tests/          # format
```

### Project Structure

```
agent-gate/
├── src/agent_gate/           # Main package
│   ├── __init__.py
│   ├── __main__.py           # CLI entrypoint + orchestration
│   ├── config.py             # YAML loading + env var substitution
│   ├── models.py             # Dataclasses (Decision, ToolRequest, etc.)
│   ├── engine.py             # Permission engine (fnmatch, signature builders)
│   ├── executor.py           # Action dispatch (tool -> service mapping)
│   ├── server.py             # WebSocket server + pending request mgmt
│   ├── db.py                 # SQLite (audit_log, pending_requests)
│   ├── client.py             # Agent SDK (AgentGateClient)
│   ├── messenger/
│   │   ├── base.py           # MessengerAdapter ABC
│   │   └── telegram.py       # Telegram Guardian bot (PTB v21)
│   └── services/
│       ├── base.py           # ServiceHandler ABC
│       └── homeassistant.py  # HA REST API client
├── tests/                    # pytest tests
├── docs/                     # Specification + architecture docs
├── config.example.yaml
├── permissions.example.yaml
├── pyproject.toml
├── Dockerfile
├── docker-compose.yml
└── LICENSE
```

---

## License

MIT -- see [LICENSE](LICENSE) for details.
