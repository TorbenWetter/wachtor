"""Tests for tool YAML loading — load_tools_file() and new config dataclasses."""

from __future__ import annotations

import textwrap

import pytest

from agent_gate.config import (
    ArgDefinition,
    ConfigError,
    RequestDefinition,
    ResponseDefinition,
    ToolDefinition,
    load_tools_file,
)

# --- Fixtures ---


HA_TOOLS_YAML = textwrap.dedent("""\
    tools:
      ha_get_state:
        description: "Get entity state from Home Assistant"
        signature: "{entity_id}"
        args:
          entity_id:
            required: true
            validate: "^[a-z_][a-z0-9_]*(\\\\.[a-z0-9_]+)?$"
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
            validate: "^[a-z_][a-z0-9_]*(\\\\.[a-z0-9_]+)?$"
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
""")


@pytest.fixture()
def ha_tools_file(tmp_path):
    p = tmp_path / "homeassistant.yaml"
    p.write_text(HA_TOOLS_YAML)
    return p


# --- Tests ---


class TestLoadValidHATools:
    def test_load_valid_ha_tools(self, ha_tools_file):
        """loads tools/homeassistant.yaml, returns 4 ToolDefinition."""
        tools = load_tools_file(str(ha_tools_file), "homeassistant")
        assert len(tools) == 4
        assert all(isinstance(t, ToolDefinition) for t in tools)

    def test_tool_definitions_have_correct_names(self, ha_tools_file):
        tools = load_tools_file(str(ha_tools_file), "homeassistant")
        names = {t.name for t in tools}
        assert names == {"ha_get_state", "ha_get_states", "ha_call_service", "ha_fire_event"}

    def test_tool_definitions_have_correct_service_name(self, ha_tools_file):
        tools = load_tools_file(str(ha_tools_file), "homeassistant")
        for t in tools:
            assert t.service_name == "homeassistant"

    def test_tool_definitions_have_correct_signatures(self, ha_tools_file):
        tools = load_tools_file(str(ha_tools_file), "homeassistant")
        by_name = {t.name: t for t in tools}
        assert by_name["ha_get_state"].signature == "{entity_id}"
        assert by_name["ha_call_service"].signature == "{domain}.{service}, {entity_id}"
        assert by_name["ha_fire_event"].signature == "{event_type}"
        # ha_get_states has no signature defined => defaults to ""
        assert by_name["ha_get_states"].signature == ""

    def test_arg_definitions_parsed(self, ha_tools_file):
        tools = load_tools_file(str(ha_tools_file), "homeassistant")
        by_name = {t.name: t for t in tools}

        # ha_get_state entity_id: required=true, has validate pattern
        entity_arg = by_name["ha_get_state"].args["entity_id"]
        assert isinstance(entity_arg, ArgDefinition)
        assert entity_arg.required is True
        assert entity_arg.validate is not None

        # ha_call_service: domain required, entity_id not required
        cs = by_name["ha_call_service"]
        assert cs.args["domain"].required is True
        assert cs.args["service"].required is True
        assert cs.args["entity_id"].required is False

    def test_request_definitions_parsed(self, ha_tools_file):
        tools = load_tools_file(str(ha_tools_file), "homeassistant")
        by_name = {t.name: t for t in tools}

        # ha_get_state: GET /api/states/{entity_id}
        req = by_name["ha_get_state"].request
        assert isinstance(req, RequestDefinition)
        assert req.method == "GET"
        assert req.path == "/api/states/{entity_id}"
        assert req.body_exclude is None

        # ha_call_service: POST with body_exclude
        req2 = by_name["ha_call_service"].request
        assert req2.method == "POST"
        assert req2.path == "/api/services/{domain}/{service}"
        assert req2.body_exclude == ["domain", "service"]

    def test_response_definitions_parsed(self, ha_tools_file):
        tools = load_tools_file(str(ha_tools_file), "homeassistant")
        by_name = {t.name: t for t in tools}

        # ha_get_states wraps in "states"
        resp = by_name["ha_get_states"].response
        assert isinstance(resp, ResponseDefinition)
        assert resp.wrap == "states"

        # ha_call_service wraps in "result"
        resp2 = by_name["ha_call_service"].response
        assert resp2.wrap == "result"

        # ha_get_state has no response definition
        assert by_name["ha_get_state"].response is None


class TestLoadToolsFileEdgeCases:
    def test_missing_file_raises_config_error(self):
        with pytest.raises(ConfigError, match="not found"):
            load_tools_file("/nonexistent/tools.yaml", "test")

    def test_invalid_regex_raises_config_error(self, tmp_path):
        yaml_text = textwrap.dedent("""\
            tools:
              bad_tool:
                args:
                  entity_id:
                    required: true
                    validate: "[invalid(regex"
                request:
                  method: GET
                  path: "/api/test"
        """)
        p = tmp_path / "bad_tools.yaml"
        p.write_text(yaml_text)
        with pytest.raises(ConfigError, match=r"[Rr]egex|[Pp]attern"):
            load_tools_file(str(p), "test")

    def test_env_var_substitution_in_tools(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CUSTOM_PATH", "/api/v2/states")
        yaml_text = textwrap.dedent("""\
            tools:
              custom_tool:
                description: "Custom tool"
                request:
                  method: GET
                  path: "${CUSTOM_PATH}"
        """)
        p = tmp_path / "tools.yaml"
        p.write_text(yaml_text)
        tools = load_tools_file(str(p), "test")
        assert len(tools) == 1
        assert tools[0].request.path == "/api/v2/states"

    def test_empty_tools_file(self, tmp_path):
        """An empty tools section returns empty list."""
        yaml_text = textwrap.dedent("""\
            tools: {}
        """)
        p = tmp_path / "tools.yaml"
        p.write_text(yaml_text)
        tools = load_tools_file(str(p), "test")
        assert tools == []

    def test_empty_yaml_file(self, tmp_path):
        """A YAML file with no 'tools' key returns empty list."""
        yaml_text = "# empty\n"
        p = tmp_path / "tools.yaml"
        p.write_text(yaml_text)
        tools = load_tools_file(str(p), "test")
        assert tools == []

    def test_missing_request_section(self, tmp_path):
        """ToolDefinition with request=None when no request section."""
        yaml_text = textwrap.dedent("""\
            tools:
              simple_tool:
                description: "A tool with no request"
                signature: "{name}"
                args:
                  name:
                    required: true
        """)
        p = tmp_path / "tools.yaml"
        p.write_text(yaml_text)
        tools = load_tools_file(str(p), "test")
        assert len(tools) == 1
        assert tools[0].request is None
        assert tools[0].name == "simple_tool"
        assert tools[0].description == "A tool with no request"

    def test_tool_with_description_only(self, tmp_path):
        """Minimal tool definition with just a description."""
        yaml_text = textwrap.dedent("""\
            tools:
              minimal_tool:
                description: "Minimal"
        """)
        p = tmp_path / "tools.yaml"
        p.write_text(yaml_text)
        tools = load_tools_file(str(p), "test")
        assert len(tools) == 1
        assert tools[0].name == "minimal_tool"
        assert tools[0].description == "Minimal"
        assert tools[0].args == {}
        assert tools[0].request is None
        assert tools[0].response is None
        assert tools[0].signature == ""
