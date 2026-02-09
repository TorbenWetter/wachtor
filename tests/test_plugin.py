"""Tests for Python plugin service handler loading."""

from __future__ import annotations

from typing import Any

import pytest

from agent_gate.config import (
    AuthConfig,
    ConfigError,
    ServiceConfig,
    ToolDefinition,
)
from agent_gate.services.base import ServiceHandler

# ---------------------------------------------------------------------------
# A real plugin class for testing
# ---------------------------------------------------------------------------


class MockPluginService(ServiceHandler):
    """A test plugin that records its constructor args."""

    def __init__(self, config: ServiceConfig, tools: list[ToolDefinition]) -> None:
        self.config = config
        self.tools = tools

    async def execute(self, tool_name: str, args: dict[str, Any]) -> dict[str, Any]:
        return {"plugin": True, "tool": tool_name}

    async def health_check(self) -> bool:
        return True

    async def close(self) -> None:
        pass


# ---------------------------------------------------------------------------
# Unit tests for _load_plugin_service()
# ---------------------------------------------------------------------------


class TestLoadPluginService:
    """Tests for the _load_plugin_service() helper function."""

    def _make_config(
        self, handler_class: str = "tests.test_plugin:MockPluginService"
    ) -> ServiceConfig:
        return ServiceConfig(
            name="test_plugin",
            url="http://example.com",
            auth=AuthConfig(type="bearer", token="x"),
            handler="python",
            handler_class=handler_class,
            tools=[
                ToolDefinition(name="test_tool", service_name="test_plugin"),
            ],
        )

    def test_loads_valid_plugin(self):
        """Valid handler_class is imported and instantiated correctly."""
        from agent_gate.__main__ import _load_plugin_service

        config = self._make_config()
        service = _load_plugin_service(config)
        assert isinstance(service, MockPluginService)
        assert service.config is config
        assert len(service.tools) == 1

    def test_missing_handler_class_raises(self):
        """handler=python with empty handler_class raises ConfigError."""
        from agent_gate.__main__ import _load_plugin_service

        config = self._make_config(handler_class="")
        with pytest.raises(ConfigError, match="no handler_class"):
            _load_plugin_service(config)

    def test_invalid_format_raises(self):
        """handler_class without ':' separator raises ConfigError."""
        from agent_gate.__main__ import _load_plugin_service

        config = self._make_config(handler_class="module.without.colon")
        with pytest.raises(ConfigError, match=r"expected 'module\.path:ClassName'"):
            _load_plugin_service(config)

    def test_non_importable_module_raises(self):
        """Non-existent module in handler_class raises ConfigError."""
        from agent_gate.__main__ import _load_plugin_service

        config = self._make_config(handler_class="nonexistent.module:SomeClass")
        with pytest.raises(ConfigError, match="Cannot import"):
            _load_plugin_service(config)

    def test_missing_class_in_module_raises(self):
        """Existing module but missing class name raises ConfigError."""
        from agent_gate.__main__ import _load_plugin_service

        config = self._make_config(handler_class="tests.test_plugin:NonexistentClass")
        with pytest.raises(ConfigError, match="not found"):
            _load_plugin_service(config)

    def test_default_handler_uses_http(self):
        """handler='http' (default) should NOT trigger plugin loading."""
        config = ServiceConfig(
            name="regular",
            url="http://example.com",
            auth=AuthConfig(type="bearer", token="x"),
            handler="http",
        )
        # This just verifies the config field; actual dispatch is in __main__.py
        assert config.handler == "http"


# ---------------------------------------------------------------------------
# Integration tests for plugin dispatch in __main__.py
# ---------------------------------------------------------------------------


class TestPluginIntegration:
    """Test that __main__.py correctly dispatches to plugin when handler=python."""

    @pytest.mark.asyncio
    async def test_plugin_service_execute(self):
        """When loaded via _load_plugin_service, the plugin can execute tools."""
        from agent_gate.__main__ import _load_plugin_service

        config = ServiceConfig(
            name="test_svc",
            url="mqtt://broker:1883",
            auth=AuthConfig(type="header", token="x"),
            handler="python",
            handler_class="tests.test_plugin:MockPluginService",
            tools=[ToolDefinition(name="mqtt_publish", service_name="test_svc")],
        )
        service = _load_plugin_service(config)
        result = await service.execute("mqtt_publish", {"topic": "test"})
        assert result == {"plugin": True, "tool": "mqtt_publish"}

    @pytest.mark.asyncio
    async def test_plugin_health_check(self):
        """Plugin service health_check is callable."""
        from agent_gate.__main__ import _load_plugin_service

        config = ServiceConfig(
            name="test_svc",
            url="mqtt://broker:1883",
            auth=AuthConfig(type="header", token="x"),
            handler="python",
            handler_class="tests.test_plugin:MockPluginService",
            tools=[],
        )
        service = _load_plugin_service(config)
        assert await service.health_check() is True
