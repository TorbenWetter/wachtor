# agentpass

**An execution gateway for AI agents on untrusted devices.**

Your agent asks, you approve, the gateway passes it through. Connect any HTTP API via YAML -- no Python code required. The agent never sees service credentials.

---

## Quick Start

### Gateway (trusted device — e.g., a home server, NAS, Raspberry Pi)

The gateway holds all service credentials, runs the permission engine, and talks to Telegram for human approvals. The agent never sees this configuration.

**1. Install**

```bash
pip install agentpass
```

**2. Configure**

Create a Telegram bot via [@BotFather](https://t.me/botfather) and get your bot token. Then create a `.env` file:

```env
AGENT_TOKEN=any-secret-string-you-choose
GUARDIAN_BOT_TOKEN=your-telegram-bot-token
HA_TOKEN=your-home-assistant-long-lived-access-token
```

Create `config.yaml`:

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
    chat_id: -100123456789 # your Telegram group chat ID
    allowed_users: [123456789] # Telegram user IDs who can approve

services:
  homeassistant:
    url: "http://homeassistant.local:8123"
    auth:
      type: bearer
      token: "${HA_TOKEN}"
    health:
      path: "/api/"
    tools: "tools/homeassistant.yaml"

storage:
  type: "sqlite"
  path: "./data/agentpass.db"
```

Create `permissions.yaml`:

```yaml
defaults:
  - pattern: "ha_get_*"
    action: allow
  - pattern: "*"
    action: ask

rules:
  - pattern: "ha_call_service(lock.*)"
    action: deny
```

**3. Start the gateway**

```bash
# Development (no TLS)
agentpass serve --insecure

# Production (TLS required)
agentpass serve
```

### Agent device (untrusted — e.g., a laptop, cloud VM, Raspberry Pi running an AI agent)

The agent device only needs the gateway URL and the agent token. It never sees service credentials, Telegram tokens, or permission rules.

**1. Install**

```bash
pip install agentpass
```

**2. Send requests**

```bash
# Use wss:// in production, ws:// only if the gateway was started with --insecure

# List available tools
agentpass tools --url ws://gateway:8443 --token $AGENT_TOKEN

# Auto-allowed -- returns immediately
agentpass request ha_get_state entity_id=sensor.temp \
  --url ws://gateway:8443 --token $AGENT_TOKEN

# Requires approval -- check Telegram for the button
agentpass request ha_call_service domain=light service=turn_on entity_id=light.bedroom \
  --url ws://gateway:8443 --token $AGENT_TOKEN
```

Or use the Python SDK — see [Python SDK](#python-sdk) below.

---

## How It Works

```
Agent Device (untrusted)            Gateway (trusted)
+-----------------+                 +-------------------------------+
|                 |                 |  agentpass                    |
|  AI Agent       |                 |  +-------------------------+  |
|  (any agent)    |-- WebSocket --> |  | Permission Engine       |  |
|                 |                 |  | deny > allow > ask      |  |
|  Holds:         |                 |  +-----------+-------------+  |
|  - Agent token  |                 |              |                |
|  - LLM key      |                 |  +-----------v-------------+  |
|                 |<-- result ----- |  | Telegram Messenger      |  |
|                 |                 |  | (human approval)        |  |
+-----------------+                 |  +-----------+-------------+  |
                                    |              |                |
      You <-- Telegram ------------ |  +-----------v-------------+  |
                                    |  | Generic HTTP Executor   |  |
                                    |  | (any service via YAML)  |  |
                                    |  +-------------------------+  |
                                    |                               |
                                    |  Holds: service credentials,  |
                                    |  bot token, TLS certs, DB     |
                                    +-------------------------------+
```

### Security Model

| Property                 | How                                                                                |
| ------------------------ | ---------------------------------------------------------------------------------- |
| **Credential isolation** | Service tokens live only on the gateway. The agent device never sees them.         |
| **Policy engine**        | Every request matches YAML permission rules using glob patterns. Deny always wins. |
| **Human-in-the-loop**    | `ask` rules trigger a Telegram message with inline approve/deny buttons.           |
| **Transport security**   | WSS (TLS) required by default. Plaintext only with explicit `--insecure`.          |
| **Input validation**     | Glob metacharacters, control chars, and invalid identifiers are rejected.          |
| **Rate limiting**        | Max 10 pending approvals, max 60 requests/minute (configurable).                   |

---

## CLI Reference

```bash
# Gateway (trusted device)
agentpass serve [--insecure] [--config config.yaml] [--permissions permissions.yaml]

# Agent device (untrusted)
agentpass request <tool> [key=value ...] --url <ws-url> --token <token> [--timeout 900]
agentpass tools --url <ws-url> --token <token>
agentpass pending --url <ws-url> --token <token>
```

| Command   | Runs on      | Description                                               |
| --------- | ------------ | --------------------------------------------------------- |
| `serve`   | Gateway      | Start the gateway server (default if no subcommand given) |
| `request` | Agent device | Send a one-shot tool request and print the JSON result    |
| `tools`   | Agent device | List available tools with their arguments                 |
| `pending` | Agent device | Retrieve results for requests resolved while offline      |

**Exit codes:** 0 = success, 1 = denied, 2 = timeout, 3 = connection error, 4 = invalid args.

**Environment variables:** `AGENTPASS_URL` and `AGENT_TOKEN` can replace `--url` and `--token`.

---

## Python SDK

Use this on the **agent device** to integrate agentpass into your Python agent code.

```python
from agentpass import AgentPassClient, AgentPassDenied, AgentPassTimeout

async with AgentPassClient("wss://gateway:8443", token="your-agent-token") as gw:

    # Auto-allowed by policy -- returns immediately
    state = await gw.tool_request("ha_get_state", entity_id="sensor.temp")

    # Requires human approval -- blocks until approved/denied/timeout
    try:
        await gw.tool_request(
            "ha_call_service",
            domain="light", service="turn_on", entity_id="light.bedroom",
        )
    except AgentPassDenied as e:
        print(f"Denied: {e.message}")
    except AgentPassTimeout as e:
        print(f"Timed out: {e.message}")

    # List available tools
    tools = await gw.list_tools()

    # Retrieve offline results
    results = await gw.get_pending_results()
```

Auto-reconnects with exponential backoff (1s to 30s). Limit retries with `max_retries=5`.

---

## OpenClaw Integration

agentpass ships with an [OpenClaw](https://github.com/openclaw/openclaw) skill that teaches the agent to control Home Assistant devices through the gateway. The skill is available on [ClawHub](https://clawhub.ai/) and as a bundled `SKILL.md` in this repo.

### Install from ClawHub

```bash
clawhub install agentpass
```

### Manual install

Copy the skill directory to your OpenClaw skills folder:

```bash
cp -r skills/openclaw ~/.openclaw/skills/agentpass
```

Or on a remote device (e.g., a Raspberry Pi running OpenClaw):

```bash
scp -r skills/openclaw user@agent-device:~/.openclaw/skills/agentpass
```

### Configure environment variables

Add the gateway URL and agent token to `~/.openclaw/openclaw.json`:

```json
{
  "skills": {
    "entries": {
      "agentpass": {
        "enabled": true,
        "env": {
          "AGENTPASS_URL": "wss://your-gateway-host:8443",
          "AGENT_TOKEN": "your-agent-token-here"
        }
      }
    }
  }
}
```

If the file already exists, merge the `agentpass` entry into `skills.entries`.

### Install the CLI on the agent device

The agent device needs the `agentpass` CLI. On systems with externally-managed Python environments (e.g., Raspberry Pi OS), use `pipx`:

```bash
pipx install agentpass
```

Otherwise:

```bash
pip install agentpass
```

Start a new OpenClaw session and the skill will be available. Read-only queries (states, history, config) execute instantly. State-changing actions (turning lights on/off, calling services) block until the Telegram guardian approves or denies.

---

## Adding a Service (YAML Only)

All service configuration happens on the **gateway**. Any HTTP API can be connected with just two files -- no Python code needed.

### 1. Define tools in a YAML file

Create `tools/my_api.yaml`:

```yaml
tools:
  get_item:
    description: "Fetch an item by ID"
    signature: "{item_id}"
    args:
      item_id:
        required: true
        validate: "^[a-zA-Z0-9_-]+$"
    request:
      method: GET
      path: "/api/items/{item_id}"

  create_item:
    description: "Create a new item"
    signature: "{name}"
    args:
      name:
        required: true
    request:
      method: POST
      path: "/api/items"
      body_exclude: []
    response:
      wrap: "result"
```

### 2. Add the service to config.yaml

```yaml
services:
  my_api:
    url: "https://api.example.com"
    auth:
      type: header
      header_name: "X-API-Key"
      token: "${MY_API_KEY}"
    health:
      path: "/health"
    tools: "tools/my_api.yaml"
```

### 3. Add permission rules

```yaml
defaults:
  - pattern: "get_*"
    action: allow
  - pattern: "create_*"
    action: ask
```

That's it. Restart the gateway and the tools are available.

---

## Configuration Reference

### config.yaml

```yaml
gateway:
  host: "0.0.0.0" # Bind address
  port: 8443 # Listen port
  tls: # Omit for --insecure mode
    cert: "/path/to/cert.pem"
    key: "/path/to/key.pem"

agent:
  token: "${AGENT_TOKEN}" # Bearer token for agent authentication

messenger:
  type: "telegram"
  telegram:
    token: "${GUARDIAN_BOT_TOKEN}" # Telegram Bot API token
    chat_id: -100123456789 # Chat ID (negative for groups)
    allowed_users: [123456789] # User IDs authorized to approve

services:
  <service_name>:
    url: "https://..." # Base URL
    auth: # Authentication (see below)
      type: bearer
      token: "${TOKEN}"
    handler: http # "http" (default) or "python"
    handler_class: "" # For handler=python: "module.path:ClassName"
    health: # Health check endpoint
      method: GET
      path: "/"
      expect_status: 200
    tools: "tools/my_api.yaml" # Path to tool definitions
    errors: # Custom error mappings
      - status: 401
        message: "Auth failed: {body}"
      - status: 404
        message: "Not found: {body}"

storage:
  type: "sqlite"
  path: "./data/agentpass.db"

approval_timeout: 900 # Seconds before approvals expire (default: 900)
rate_limit:
  max_pending_approvals: 10
  max_requests_per_minute: 60
```

### Authentication Types

| Type     | Fields                 | Header sent                     |
| -------- | ---------------------- | ------------------------------- |
| `bearer` | `token`                | `Authorization: Bearer <token>` |
| `header` | `token`, `header_name` | `<header_name>: <token>`        |
| `query`  | `token`, `query_param` | `?<query_param>=<token>`        |
| `basic`  | `username`, `password` | `Authorization: Basic <base64>` |

### Tool Definition YAML

```yaml
tools:
  <tool_name>:
    description: "Human-readable description"
    signature: "{arg1}.{arg2}, {arg3}" # Template for permission matching
    args:
      <arg_name>:
        required: true|false # Default: false
        validate: "^regex$" # Optional validation pattern
    request:
      method: GET|POST|PUT|DELETE|PATCH
      path: "/api/path/{arg_name}" # Path with {arg} interpolation
      body_exclude: [arg1, arg2] # Args excluded from POST body
    response:
      wrap: "key_name" # Wrap response in {"key_name": data}
```

**Signature templates** control how permission patterns match. For example, with `signature: "{domain}.{service}, {entity_id}"`, calling `ha_call_service` with `domain=light, service=turn_on, entity_id=light.bedroom` produces the signature `ha_call_service(light.turn_on, light.bedroom)`, which is matched against permission rules using glob patterns.

### permissions.yaml

```yaml
defaults: # Evaluated in order, first match wins
  - pattern: "ha_get_*"
    action: allow
  - pattern: "*"
    action: ask

rules: # Checked before defaults; deny always wins
  - pattern: "ha_call_service(lock.*)"
    action: deny
    description: "Lock control is always denied"
```

**Precedence:** deny rules > allow rules > ask rules > defaults (first match) > global fallback (ask)

Patterns use `fnmatch` glob syntax (`*` matches anything, `?` matches one character, `[seq]` matches character sets).

### Python Plugin Services

For non-HTTP protocols, use `handler: python`:

```yaml
services:
  mqtt_broker:
    url: "mqtt://broker.local"
    auth:
      type: bearer
      token: "${MQTT_TOKEN}"
    handler: python
    handler_class: "my_plugin:MQTTService"
    tools: "tools/mqtt.yaml"
```

The class must extend `ServiceHandler` and accept `(config, tools)`:

```python
from agentpass.config import ServiceConfig, ToolDefinition
from agentpass.services.base import ServiceHandler

class MQTTService(ServiceHandler):
    def __init__(self, config: ServiceConfig, tools: list[ToolDefinition]):
        ...
    async def execute(self, tool_name: str, args: dict) -> dict:
        ...
    async def health_check(self) -> bool:
        ...
    async def close(self) -> None:
        ...
```

---

## JSON-RPC Protocol

For non-Python agents on the **agent device**, the gateway uses JSON-RPC 2.0 over WebSocket. Any language with WebSocket support can integrate.

### Authentication (must be first message, within 10 seconds)

```json
{
  "jsonrpc": "2.0",
  "method": "auth",
  "params": { "token": "..." },
  "id": "auth-1"
}
```

### Tool Request

```json
{
  "jsonrpc": "2.0",
  "method": "tool_request",
  "params": { "tool": "ha_get_state", "args": { "entity_id": "sensor.temp" } },
  "id": 1
}
```

### List Tools

```json
{ "jsonrpc": "2.0", "method": "list_tools", "params": {}, "id": 2 }
```

### Get Pending Results

```json
{ "jsonrpc": "2.0", "method": "get_pending_results", "params": {}, "id": 3 }
```

### Error Codes

| Code     | Meaning                                                |
| -------- | ------------------------------------------------------ |
| `-32700` | Parse error (malformed JSON)                           |
| `-32600` | Invalid request (missing fields, forbidden characters) |
| `-32601` | Method not found                                       |
| `-32001` | Denied by user                                         |
| `-32002` | Approval timed out                                     |
| `-32003` | Policy denied                                          |
| `-32004` | Execution failed                                       |
| `-32005` | Not authenticated                                      |
| `-32006` | Rate limit exceeded                                    |

---

## Docker

Run the gateway in Docker on your **trusted device**:

```bash
docker compose up -d
docker compose logs -f agentpass
```

Mounts: `config.yaml`, `permissions.yaml`, `tools/` (read-only), `data/` (read-write), `certs/` (read-only). Secrets via `.env` file.

---

## Development

```bash
git clone https://github.com/TorbenWetter/agentpass.git
cd agentpass
pip install -e ".[dev]"
pytest                              # 377 tests
ruff check src/ tests/              # lint
ruff format src/ tests/             # format
```

---

## License

MIT -- see [LICENSE](LICENSE) for details.
