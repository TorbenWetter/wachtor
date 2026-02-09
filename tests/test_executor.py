"""Tests for agent_gate.executor — action dispatch routing."""

import pytest

from agent_gate.config import AuthConfig, ServiceConfig, load_tools_file
from agent_gate.executor import ExecutionError, Executor
from agent_gate.registry import build_registry
from agent_gate.services.base import ServiceHandler


class MockServiceHandler(ServiceHandler):
    """Mock service handler that records calls."""

    def __init__(self):
        self.calls: list[tuple[str, dict]] = []

    async def execute(self, tool_name: str, args: dict) -> dict:
        self.calls.append((tool_name, args))
        return {"mock": True, "tool": tool_name}

    async def health_check(self) -> bool:
        return True

    async def close(self) -> None:
        pass


@pytest.fixture()
def ha_registry():
    """Build a ToolRegistry from the actual HA tools YAML file."""
    tools = load_tools_file("tools/homeassistant.yaml", "homeassistant")
    svc = ServiceConfig(
        name="homeassistant",
        url="http://ha",
        auth=AuthConfig(type="bearer", token="x"),
        tools=tools,
    )
    return build_registry({"homeassistant": svc})


class TestExecutor:
    async def test_dispatch_ha_get_state(self, ha_registry):
        handler = MockServiceHandler()
        executor = Executor({"homeassistant": handler}, ha_registry)
        result = await executor.execute("ha_get_state", {"entity_id": "sensor.temp"})
        assert result == {"mock": True, "tool": "ha_get_state"}
        assert handler.calls == [("ha_get_state", {"entity_id": "sensor.temp"})]

    async def test_dispatch_ha_call_service(self, ha_registry):
        handler = MockServiceHandler()
        executor = Executor({"homeassistant": handler}, ha_registry)
        args = {"domain": "light", "service": "turn_on", "entity_id": "light.bedroom"}
        result = await executor.execute("ha_call_service", args)
        assert result["tool"] == "ha_call_service"
        assert handler.calls[0] == ("ha_call_service", args)

    async def test_dispatch_ha_get_states(self, ha_registry):
        handler = MockServiceHandler()
        executor = Executor({"homeassistant": handler}, ha_registry)
        await executor.execute("ha_get_states", {})
        assert handler.calls == [("ha_get_states", {})]

    async def test_dispatch_ha_fire_event(self, ha_registry):
        handler = MockServiceHandler()
        executor = Executor({"homeassistant": handler}, ha_registry)
        await executor.execute("ha_fire_event", {"event_type": "test"})
        assert handler.calls[0][0] == "ha_fire_event"

    async def test_unknown_tool_raises(self, ha_registry):
        handler = MockServiceHandler()
        executor = Executor({"homeassistant": handler}, ha_registry)
        with pytest.raises(ExecutionError, match="Unknown tool"):
            await executor.execute("nonexistent_tool", {})

    async def test_missing_service_raises(self, ha_registry):
        # No services registered but registry knows the tools
        executor = Executor({}, ha_registry)
        with pytest.raises(ExecutionError, match="Service not configured"):
            await executor.execute("ha_get_state", {"entity_id": "sensor.temp"})

    async def test_passes_correct_args(self, ha_registry):
        handler = MockServiceHandler()
        executor = Executor({"homeassistant": handler}, ha_registry)
        args = {"entity_id": "light.kitchen", "extra": "data"}
        await executor.execute("ha_get_state", args)
        assert handler.calls[0][1] is args

    async def test_multiple_services(self, ha_registry):
        ha_handler = MockServiceHandler()
        other_handler = MockServiceHandler()
        executor = Executor({"homeassistant": ha_handler, "other": other_handler}, ha_registry)
        await executor.execute("ha_get_state", {"entity_id": "sensor.temp"})
        assert len(ha_handler.calls) == 1
        assert len(other_handler.calls) == 0

    async def test_no_registry_unknown_tool(self):
        """Without registry, all tools are unknown."""
        handler = MockServiceHandler()
        executor = Executor({"homeassistant": handler})
        with pytest.raises(ExecutionError, match="Unknown tool"):
            await executor.execute("ha_get_state", {"entity_id": "sensor.temp"})


class TestExecutorWithRegistry:
    """Tests for Executor with an explicit ToolRegistry."""

    async def test_dispatch_via_registry(self):
        """Tool routed via registry lookup."""
        tools = load_tools_file("tools/homeassistant.yaml", "homeassistant")
        svc = ServiceConfig(
            name="homeassistant",
            url="http://ha",
            auth=AuthConfig(type="bearer", token="x"),
            tools=tools,
        )
        registry = build_registry({"homeassistant": svc})

        handler = MockServiceHandler()
        executor = Executor({"homeassistant": handler}, registry)
        result = await executor.execute("ha_get_state", {"entity_id": "sensor.temp"})
        assert result == {"mock": True, "tool": "ha_get_state"}

    async def test_unknown_tool_with_registry(self):
        """Unknown tool raises even with empty registry."""
        from agent_gate.registry import ToolRegistry

        registry = ToolRegistry({})
        handler = MockServiceHandler()
        executor = Executor({"homeassistant": handler}, registry)
        with pytest.raises(ExecutionError, match="Unknown tool"):
            await executor.execute("nonexistent", {})

    async def test_registry_service_not_configured(self):
        """Registry knows the tool but no service handler registered."""
        tools = load_tools_file("tools/homeassistant.yaml", "homeassistant")
        svc = ServiceConfig(
            name="homeassistant",
            url="http://ha",
            auth=AuthConfig(type="bearer", token="x"),
            tools=tools,
        )
        registry = build_registry({"homeassistant": svc})

        # No services dict entry for "homeassistant"
        executor = Executor({}, registry)
        with pytest.raises(ExecutionError, match="Service not configured"):
            await executor.execute("ha_get_state", {"entity_id": "sensor.temp"})
