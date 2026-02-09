"""Configuration loading with env var substitution and validation."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


class ConfigError(Exception):
    """Raised on configuration loading or validation errors."""


# --- Env var substitution ---

_ENV_VAR_RE = re.compile(r"\$\{(\w+)\}")


def _replacer(match: re.Match) -> str:
    var = match.group(1)
    val = os.environ.get(var)
    if val is None:
        raise ConfigError(f"Environment variable {var} is not set")
    return val


def substitute_env_vars(obj: Any) -> Any:
    """Recursively substitute ${VAR} in all string values."""
    if isinstance(obj, str):
        return _ENV_VAR_RE.sub(_replacer, obj)
    if isinstance(obj, dict):
        return {k: substitute_env_vars(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [substitute_env_vars(item) for item in obj]
    return obj


# --- Config dataclasses ---


@dataclass
class TLSConfig:
    cert: str
    key: str


@dataclass
class GatewayConfig:
    host: str
    port: int
    tls: TLSConfig | None = None


@dataclass
class AgentConfig:
    token: str


@dataclass
class TelegramConfig:
    token: str
    chat_id: int
    allowed_users: list[int]


@dataclass
class MessengerConfig:
    type: str
    telegram: TelegramConfig | None = None


@dataclass
class StorageConfig:
    type: str
    path: str


@dataclass
class RateLimitConfig:
    max_pending_approvals: int = 10
    max_requests_per_minute: int = 60


@dataclass
class Config:
    gateway: GatewayConfig
    agent: AgentConfig
    messenger: MessengerConfig
    services: dict[str, ServiceConfig]
    storage: StorageConfig
    approval_timeout: int = 900
    rate_limit: RateLimitConfig = field(default_factory=RateLimitConfig)


# --- Tool / Service dataclasses (extensible tools) ---


@dataclass
class ArgDefinition:
    required: bool = False
    validate: str | None = None  # regex pattern string


@dataclass
class RequestDefinition:
    method: str  # GET, POST, PUT, DELETE, PATCH
    path: str  # "/api/states/{entity_id}"
    body_exclude: list[str] | None = None


@dataclass
class ResponseDefinition:
    wrap: str | None = None  # wrap response in {wrap: data}


@dataclass
class ToolDefinition:
    name: str
    service_name: str
    description: str = ""
    signature: str = ""  # "{domain}.{service}, {entity_id}"
    args: dict[str, ArgDefinition] = field(default_factory=dict)
    request: RequestDefinition | None = None
    response: ResponseDefinition | None = None


@dataclass
class AuthConfig:
    type: str  # bearer, header, query, basic
    token: str = ""
    header_name: str = ""  # for type=header
    query_param: str = ""  # for type=query
    username: str = ""  # for type=basic
    password: str = ""  # for type=basic


@dataclass
class HealthCheckConfig:
    method: str = "GET"
    path: str = "/"
    expect_status: int = 200


@dataclass
class ErrorMapping:
    status: int
    message: str  # supports {status}, {body} templates


@dataclass
class ServiceConfig:
    name: str
    url: str
    auth: AuthConfig
    handler: str = "http"
    handler_class: str = ""
    health: HealthCheckConfig = field(default_factory=HealthCheckConfig)
    tools_file: str = ""
    tools: list[ToolDefinition] = field(default_factory=list)
    errors: list[ErrorMapping] = field(default_factory=list)


# --- Permission dataclasses ---


@dataclass
class PermissionRule:
    pattern: str
    action: str
    description: str = ""


@dataclass
class Permissions:
    defaults: list[PermissionRule]
    rules: list[PermissionRule]


# --- Helpers ---


def _require(data: dict, key: str, context: str) -> Any:
    """Get a required key from a dict or raise ConfigError."""
    if key not in data or data[key] is None:
        raise ConfigError(f"Missing required config: {context}.{key}")
    return data[key]


def _coerce_int(value: Any, field_name: str) -> int:
    """Coerce a value to int (handles env-substituted strings)."""
    try:
        return int(value)
    except (TypeError, ValueError):
        raise ConfigError(f"Cannot convert {field_name} to int: {value!r}") from None


# --- Loaders ---


def load_config(path: str = "config.yaml") -> Config:
    """Load and validate config.yaml, returning a typed Config."""
    p = Path(path)
    if not p.exists():
        raise ConfigError(f"Config file not found: {path}")

    with open(p) as f:
        raw = yaml.safe_load(f)

    raw = substitute_env_vars(raw)

    # Gateway
    gw_raw = _require(raw, "gateway", "")
    host = _require(gw_raw, "host", "gateway")
    port = _coerce_int(_require(gw_raw, "port", "gateway"), "gateway.port")

    tls = None
    if gw_raw.get("tls"):
        tls_raw = gw_raw["tls"]
        tls = TLSConfig(
            cert=_require(tls_raw, "cert", "gateway.tls"),
            key=_require(tls_raw, "key", "gateway.tls"),
        )

    gateway = GatewayConfig(host=host, port=port, tls=tls)

    # Agent
    agent_raw = _require(raw, "agent", "")
    token = _require(agent_raw, "token", "agent")
    if not token:
        raise ConfigError("Missing required config: agent.token")
    agent = AgentConfig(token=token)

    # Messenger
    msg_raw = _require(raw, "messenger", "")
    msg_type = _require(msg_raw, "type", "messenger")
    if msg_type != "telegram":
        raise ConfigError(
            f"Unsupported messenger type: {msg_type!r} (only 'telegram' is supported)"
        )

    telegram_cfg = None
    if msg_type == "telegram":
        tg_raw = _require(msg_raw, "telegram", "messenger")
        tg_token = _require(tg_raw, "token", "messenger.telegram")
        chat_id = _coerce_int(
            _require(tg_raw, "chat_id", "messenger.telegram"), "messenger.telegram.chat_id"
        )
        allowed_users = _require(tg_raw, "allowed_users", "messenger.telegram")
        if not allowed_users:
            raise ConfigError("messenger.telegram.allowed_users must be a non-empty list")
        allowed_users = [
            _coerce_int(u, "messenger.telegram.allowed_users[]") for u in allowed_users
        ]
        telegram_cfg = TelegramConfig(token=tg_token, chat_id=chat_id, allowed_users=allowed_users)

    messenger = MessengerConfig(type=msg_type, telegram=telegram_cfg)

    # Services
    svc_raw = _require(raw, "services", "")
    services: dict[str, ServiceConfig] = {}
    for svc_name, svc_data in svc_raw.items():
        if not isinstance(svc_data, dict):
            raise ConfigError(f"Service '{svc_name}' must be a mapping")
        url = _require(svc_data, "url", f"services.{svc_name}")

        # Parse auth
        auth_raw = _require(svc_data, "auth", f"services.{svc_name}")
        auth_type = _require(auth_raw, "type", f"services.{svc_name}.auth")
        auth = AuthConfig(
            type=auth_type,
            token=auth_raw.get("token", ""),
            header_name=auth_raw.get("header_name", ""),
            query_param=auth_raw.get("query_param", ""),
            username=auth_raw.get("username", ""),
            password=auth_raw.get("password", ""),
        )

        # Parse health check
        health_raw = svc_data.get("health", {})
        health = HealthCheckConfig(
            method=health_raw.get("method", "GET"),
            path=health_raw.get("path", "/"),
            expect_status=_coerce_int(
                health_raw.get("expect_status", 200),
                f"services.{svc_name}.health.expect_status",
            ),
        )

        # Parse errors
        errors = []
        for i, err in enumerate(svc_data.get("errors", [])):
            if not isinstance(err, dict):
                raise ConfigError(f"services.{svc_name}.errors[{i}] must be a mapping")
            if "status" not in err:
                raise ConfigError(
                    f"Missing required key 'status' in services.{svc_name}.errors[{i}]"
                )
            if "message" not in err:
                raise ConfigError(
                    f"Missing required key 'message' in services.{svc_name}.errors[{i}]"
                )
            errors.append(
                ErrorMapping(
                    status=_coerce_int(err["status"], f"services.{svc_name}.errors[{i}].status"),
                    message=err["message"],
                )
            )

        # Load tools
        tools_file = svc_data.get("tools", "")
        tools: list[ToolDefinition] = []
        if tools_file:
            config_dir = Path(path).parent
            tools_path = str(config_dir / tools_file)
            tools = load_tools_file(tools_path, svc_name)

        services[svc_name] = ServiceConfig(
            name=svc_name,
            url=url,
            auth=auth,
            handler=svc_data.get("handler", "http"),
            handler_class=svc_data.get("handler_class", ""),
            health=health,
            tools_file=tools_file,
            tools=tools,
            errors=errors,
        )

    if not services:
        raise ConfigError("At least one service must be configured")

    # Storage
    stor_raw = _require(raw, "storage", "")
    stor_type = _require(stor_raw, "type", "storage")
    if stor_type != "sqlite":
        raise ConfigError(f"Unsupported storage type: {stor_type!r} (only 'sqlite' is supported)")
    storage = StorageConfig(
        type=stor_type,
        path=_require(stor_raw, "path", "storage"),
    )

    # Optional top-level
    approval_timeout = raw.get("approval_timeout", 900)
    if not isinstance(approval_timeout, int) or approval_timeout <= 0:
        raise ConfigError(f"approval_timeout must be a positive integer, got: {approval_timeout!r}")
    rate_limit_raw = raw.get("rate_limit", {})
    rate_limit = RateLimitConfig(
        max_pending_approvals=rate_limit_raw.get("max_pending_approvals", 10),
        max_requests_per_minute=rate_limit_raw.get("max_requests_per_minute", 60),
    )

    return Config(
        gateway=gateway,
        agent=agent,
        messenger=messenger,
        services=services,
        storage=storage,
        approval_timeout=approval_timeout,
        rate_limit=rate_limit,
    )


def load_permissions(path: str = "permissions.yaml") -> Permissions:
    """Load and parse permissions.yaml into typed Permissions."""
    p = Path(path)
    if not p.exists():
        raise ConfigError(f"Permissions file not found: {path}")

    with open(p) as f:
        raw = yaml.safe_load(f)

    raw = substitute_env_vars(raw)

    _VALID_ACTIONS = {"allow", "deny", "ask"}

    defaults = []
    for item in raw.get("defaults", []):
        action = item["action"]
        if action not in _VALID_ACTIONS:
            raise ConfigError(f"Invalid permission action: {action!r} (must be allow/deny/ask)")
        defaults.append(
            PermissionRule(
                pattern=item["pattern"],
                action=action,
                description=item.get("description", ""),
            )
        )

    rules = []
    for item in raw.get("rules", []) or []:
        action = item["action"]
        if action not in _VALID_ACTIONS:
            raise ConfigError(f"Invalid permission action: {action!r} (must be allow/deny/ask)")
        rules.append(
            PermissionRule(
                pattern=item["pattern"],
                action=action,
                description=item.get("description", ""),
            )
        )

    return Permissions(defaults=defaults, rules=rules)


def load_tools_file(path: str, service_name: str) -> list[ToolDefinition]:
    """Load and parse a tools YAML file, returning typed ToolDefinition objects.

    - Reads YAML file
    - Runs substitute_env_vars() on it
    - Validates each tool entry
    - Compiles validation regexes at load time (raise ConfigError if invalid)
    - Returns list of ToolDefinition objects
    """
    p = Path(path)
    if not p.exists():
        raise ConfigError(f"Tools file not found: {path}")

    with open(p) as f:
        raw = yaml.safe_load(f)

    if raw is None:
        return []

    raw = substitute_env_vars(raw)

    tools_raw = raw.get("tools")
    if not tools_raw:
        return []

    result: list[ToolDefinition] = []
    for tool_name, tool_data in tools_raw.items():
        if tool_data is None:
            tool_data = {}

        # Parse args
        args: dict[str, ArgDefinition] = {}
        for arg_name, arg_data in (tool_data.get("args") or {}).items():
            if arg_data is None:
                arg_data = {}
            validate = arg_data.get("validate")
            if validate is not None:
                # Compile regex at load time to catch errors early
                try:
                    re.compile(validate)
                except re.error as e:
                    raise ConfigError(
                        f"Invalid regex pattern for tool '{tool_name}' arg '{arg_name}': {e}"
                    ) from None
            args[arg_name] = ArgDefinition(
                required=bool(arg_data.get("required", False)),
                validate=validate,
            )

        # Parse request
        request: RequestDefinition | None = None
        req_raw = tool_data.get("request")
        if req_raw is not None:
            request = RequestDefinition(
                method=req_raw.get("method", "GET"),
                path=req_raw.get("path", "/"),
                body_exclude=req_raw.get("body_exclude"),
            )

        # Parse response
        response: ResponseDefinition | None = None
        resp_raw = tool_data.get("response")
        if resp_raw is not None:
            response = ResponseDefinition(
                wrap=resp_raw.get("wrap"),
            )

        result.append(
            ToolDefinition(
                name=tool_name,
                service_name=service_name,
                description=tool_data.get("description", ""),
                signature=tool_data.get("signature", ""),
                args=args,
                request=request,
                response=response,
            )
        )

    return result
