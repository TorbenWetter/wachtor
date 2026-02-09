"""Permission engine — signature building, input validation, policy evaluation."""

from __future__ import annotations

import re
from fnmatch import fnmatch
from typing import TYPE_CHECKING

from agent_gate.config import Permissions
from agent_gate.models import Decision

if TYPE_CHECKING:
    from agent_gate.registry import ToolRegistry

# Characters forbidden in ANY argument value (prevents glob/signature injection)
FORBIDDEN_CHARS_RE = re.compile(r"[*?\[\](),\x00-\x1f]")


def validate_args(tool_name: str, args: dict, registry: ToolRegistry | None = None) -> None:
    """Reject args with forbidden characters and validate against tool definition.

    When *registry* is provided and contains the tool, YAML-defined validation
    is used (required args, per-arg regex patterns).  Otherwise only global
    forbidden-character checks apply.
    """
    # Global: forbidden chars always checked first
    for key, value in args.items():
        if not isinstance(value, str):
            continue
        if FORBIDDEN_CHARS_RE.search(value):
            raise ValueError(f"Argument '{key}' contains forbidden characters")

    if registry:
        tool = registry.get_tool(tool_name)
        if tool:
            # Check required args
            required = registry.get_required_args(tool_name)
            for req_arg in required:
                if req_arg not in args:
                    raise ValueError(f"Missing required argument: {req_arg}")
            # Check per-arg validation patterns from YAML
            validators = registry.get_arg_validators(tool_name)
            for key, value in args.items():
                if not isinstance(value, str):
                    continue
                pattern = validators.get(key)
                if pattern and not pattern.match(value):
                    raise ValueError(f"Invalid value for {key}: {value!r}")


def build_signature(tool_name: str, args: dict, registry: ToolRegistry | None = None) -> str:
    """Build a deterministic, matchable signature string.

    When *registry* is provided and contains the tool, the YAML-defined
    signature template is used.  Otherwise sorted-keys fallback applies.

    Examples (with registry):
        build_signature("ha_get_state", {"entity_id": "sensor.temp"}, registry)
        -> "ha_get_state(sensor.temp)"

        build_signature("ha_call_service",
            {"domain": "light", "service": "turn_on", "entity_id": "light.bedroom"},
            registry)
        -> "ha_call_service(light.turn_on, light.bedroom)"
    """
    validate_args(tool_name, args, registry)

    if registry:
        parts = registry.get_signature_parts(tool_name, args)
        if parts is not None:  # Tool found in registry
            return f"{tool_name}({', '.join(parts)})" if parts else tool_name

    # Fallback for tools not in registry: sorted keys for determinism
    parts = [str(args[k]) for k in sorted(args.keys())]
    return f"{tool_name}({', '.join(parts)})" if parts else tool_name


class PermissionEngine:
    """Evaluates tool requests against permission rules."""

    def __init__(self, permissions: Permissions, registry: ToolRegistry | None = None) -> None:
        self._permissions = permissions
        self._registry = registry

    def evaluate(self, tool_name: str, args: dict) -> Decision:
        """Evaluate a tool request and return allow/deny/ask."""
        signature = build_signature(tool_name, args, self._registry)

        # Phase 1: Check explicit rules (deny > allow > ask)
        for action_type in ("deny", "allow", "ask"):
            for rule in self._permissions.rules:
                if rule.action == action_type and fnmatch(signature, rule.pattern):
                    return Decision(action_type)

        # Phase 2: Check defaults (first match wins)
        for default in self._permissions.defaults:
            if fnmatch(signature, default.pattern):
                return Decision(default.action)

        # Phase 3: Global fallback
        return Decision.ASK
