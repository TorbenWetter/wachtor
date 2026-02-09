"""Tests for agent_gate.cli — CLI client commands."""

from __future__ import annotations

import asyncio
import json
from argparse import Namespace
from unittest.mock import AsyncMock, patch

import pytest

from agent_gate.cli import (
    EXIT_CONNECTION_ERROR,
    EXIT_DENIED,
    EXIT_INVALID_ARGS,
    EXIT_SUCCESS,
    EXIT_TIMEOUT,
    parse_key_value_args,
    run_pending,
    run_request,
    run_tools,
)

# ---------------------------------------------------------------------------
# parse_key_value_args tests
# ---------------------------------------------------------------------------


class TestParseKeyValueArgs:
    """Tests for the parse_key_value_args() helper."""

    def test_basic_parsing(self):
        """Single key=value pair is parsed correctly."""
        result = parse_key_value_args(["entity_id=sensor.temp"])
        assert result == {"entity_id": "sensor.temp"}

    def test_multiple_args(self):
        """Multiple key=value pairs are parsed correctly."""
        result = parse_key_value_args(
            [
                "domain=light",
                "service=turn_on",
                "entity_id=light.kitchen",
            ]
        )
        assert result == {
            "domain": "light",
            "service": "turn_on",
            "entity_id": "light.kitchen",
        }

    def test_empty_list(self):
        """Empty list returns empty dict."""
        result = parse_key_value_args([])
        assert result == {}

    def test_value_with_equals(self):
        """Value containing = is preserved (only first = splits)."""
        result = parse_key_value_args(["key=value=more"])
        assert result == {"key": "value=more"}

    def test_missing_equals_raises(self):
        """Argument without = sign raises ValueError."""
        with pytest.raises(ValueError, match="Invalid argument format"):
            parse_key_value_args(["no_equals_here"])

    def test_empty_key_raises(self):
        """Argument with empty key (=value) raises ValueError."""
        with pytest.raises(ValueError, match="Empty key"):
            parse_key_value_args(["=value"])


# ---------------------------------------------------------------------------
# Helper to build a mock AgentGateClient
# ---------------------------------------------------------------------------


def _make_mock_client(**overrides):
    """Build an AsyncMock that works as an async context manager for AgentGateClient."""
    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.tool_request = AsyncMock(return_value={"state": "on"})
    mock_client.get_pending_results = AsyncMock(return_value=[])
    for key, value in overrides.items():
        setattr(mock_client, key, value)
    return mock_client


# ---------------------------------------------------------------------------
# run_request tests
# ---------------------------------------------------------------------------

_CLI_PATCH = "agent_gate.cli.AgentGateClient"


class TestRunRequest:
    """Tests for the run_request() function."""

    @pytest.mark.asyncio
    async def test_success_prints_json(self, capsys):
        """Successful tool request prints JSON result and returns exit 0."""
        args = Namespace(
            url="wss://gw:8443",
            token="test-token",
            tool="ha_get_state",
            args=["entity_id=sensor.temp"],
            timeout=900.0,
        )
        mock_client = _make_mock_client(
            tool_request=AsyncMock(return_value={"state": "on", "attributes": {}}),
        )

        with patch(_CLI_PATCH, return_value=mock_client):
            exit_code = await run_request(args)

        assert exit_code == EXIT_SUCCESS
        output = json.loads(capsys.readouterr().out)
        assert output == {"state": "on", "attributes": {}}

    @pytest.mark.asyncio
    async def test_denied_prints_error(self, capsys):
        """AgentGateDenied returns exit code 1 with error on stderr."""
        from agent_gate.client import AgentGateDenied

        args = Namespace(
            url="wss://gw:8443",
            token="test-token",
            tool="ha_call_service",
            args=["domain=lock", "service=unlock"],
            timeout=900.0,
        )
        mock_client = _make_mock_client(
            tool_request=AsyncMock(side_effect=AgentGateDenied(-32001, "Policy denied")),
        )

        with patch(_CLI_PATCH, return_value=mock_client):
            exit_code = await run_request(args)

        assert exit_code == EXIT_DENIED
        stderr = capsys.readouterr().err
        assert "Denied" in stderr

    @pytest.mark.asyncio
    async def test_timeout_prints_error(self, capsys):
        """AgentGateTimeout returns exit code 2 with error on stderr."""
        from agent_gate.client import AgentGateTimeout

        args = Namespace(
            url="wss://gw:8443",
            token="test-token",
            tool="ha_get_state",
            args=["entity_id=sensor.temp"],
            timeout=900.0,
        )
        mock_client = _make_mock_client(
            tool_request=AsyncMock(side_effect=AgentGateTimeout(-32002, "Approval timed out")),
        )

        with patch(_CLI_PATCH, return_value=mock_client):
            exit_code = await run_request(args)

        assert exit_code == EXIT_TIMEOUT
        stderr = capsys.readouterr().err
        assert "Timeout" in stderr

    @pytest.mark.asyncio
    async def test_connection_error_prints_error(self, capsys):
        """AgentGateConnectionError returns exit code 3 with error on stderr."""
        from agent_gate.client import AgentGateConnectionError

        args = Namespace(
            url="wss://gw:8443",
            token="bad-token",
            tool="ha_get_state",
            args=[],
            timeout=900.0,
        )
        # Connection error happens during context manager entry
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(
            side_effect=AgentGateConnectionError(-1, "Auth failed"),
        )
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch(_CLI_PATCH, return_value=mock_client):
            exit_code = await run_request(args)

        assert exit_code == EXIT_CONNECTION_ERROR
        stderr = capsys.readouterr().err
        assert "Connection failed" in stderr

    @pytest.mark.asyncio
    async def test_missing_url(self, capsys):
        """Empty URL returns exit code 3 with error on stderr."""
        args = Namespace(
            url="",
            token="test-token",
            tool="ha_get_state",
            args=[],
            timeout=900.0,
        )
        exit_code = await run_request(args)

        assert exit_code == EXIT_CONNECTION_ERROR
        stderr = capsys.readouterr().err
        assert "URL required" in stderr

    @pytest.mark.asyncio
    async def test_missing_token(self, capsys):
        """Empty token returns exit code 3 with error on stderr."""
        args = Namespace(
            url="wss://gw:8443",
            token="",
            tool="ha_get_state",
            args=[],
            timeout=900.0,
        )
        exit_code = await run_request(args)

        assert exit_code == EXIT_CONNECTION_ERROR
        stderr = capsys.readouterr().err
        assert "token required" in stderr

    @pytest.mark.asyncio
    async def test_invalid_args(self, capsys):
        """Malformed key=value args return exit code 4."""
        args = Namespace(
            url="wss://gw:8443",
            token="test-token",
            tool="ha_get_state",
            args=["not_key_value"],
            timeout=900.0,
        )
        exit_code = await run_request(args)

        assert exit_code == EXIT_INVALID_ARGS
        stderr = capsys.readouterr().err
        assert "Invalid argument format" in stderr

    @pytest.mark.asyncio
    async def test_client_timeout(self, capsys):
        """asyncio.TimeoutError from wait_for returns exit code 2."""
        args = Namespace(
            url="wss://gw:8443",
            token="test-token",
            tool="ha_get_state",
            args=["entity_id=sensor.temp"],
            timeout=0.001,
        )

        # Make tool_request hang forever so wait_for times out
        async def hang_forever(*args, **kwargs):
            await asyncio.sleep(999)

        mock_client = _make_mock_client(
            tool_request=AsyncMock(side_effect=hang_forever),
        )

        with patch(_CLI_PATCH, return_value=mock_client):
            exit_code = await run_request(args)

        assert exit_code == EXIT_TIMEOUT
        stderr = capsys.readouterr().err
        assert "timed out" in stderr.lower()


# ---------------------------------------------------------------------------
# run_tools tests
# ---------------------------------------------------------------------------


class TestRunTools:
    """Tests for the run_tools() function."""

    @pytest.mark.asyncio
    async def test_success_prints_tools(self, capsys):
        """Successful list_tools prints JSON tool list and returns exit 0."""
        args = Namespace(url="wss://gw:8443", token="test-token")

        tools_result = [
            {"name": "ha_get_state", "description": "Get entity state"},
            {"name": "ha_call_service", "description": "Call HA service"},
        ]

        mock_client = _make_mock_client(
            list_tools=AsyncMock(return_value=tools_result),
        )

        with patch(_CLI_PATCH, return_value=mock_client):
            exit_code = await run_tools(args)

        assert exit_code == EXIT_SUCCESS
        output = json.loads(capsys.readouterr().out)
        assert output == tools_result

    @pytest.mark.asyncio
    async def test_missing_url(self, capsys):
        """Empty URL returns exit code 3 with error on stderr."""
        args = Namespace(url="", token="test-token")
        exit_code = await run_tools(args)

        assert exit_code == EXIT_CONNECTION_ERROR
        stderr = capsys.readouterr().err
        assert "URL required" in stderr

    @pytest.mark.asyncio
    async def test_connection_error(self, capsys):
        """AgentGateConnectionError returns exit code 3."""
        from agent_gate.client import AgentGateConnectionError

        args = Namespace(url="wss://gw:8443", token="test-token")
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(
            side_effect=AgentGateConnectionError(-1, "Connection refused"),
        )
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch(_CLI_PATCH, return_value=mock_client):
            exit_code = await run_tools(args)

        assert exit_code == EXIT_CONNECTION_ERROR
        stderr = capsys.readouterr().err
        assert "Connection failed" in stderr


# ---------------------------------------------------------------------------
# run_pending tests
# ---------------------------------------------------------------------------


class TestRunPending:
    """Tests for the run_pending() function."""

    @pytest.mark.asyncio
    async def test_success_prints_results(self, capsys):
        """Successful get_pending_results prints JSON list and returns exit 0."""
        args = Namespace(url="wss://gw:8443", token="test-token")
        pending_data = [
            {"request_id": "1", "status": "executed", "data": {"state": "off"}},
        ]
        mock_client = _make_mock_client(
            get_pending_results=AsyncMock(return_value=pending_data),
        )

        with patch(_CLI_PATCH, return_value=mock_client):
            exit_code = await run_pending(args)

        assert exit_code == EXIT_SUCCESS
        output = json.loads(capsys.readouterr().out)
        assert output == pending_data

    @pytest.mark.asyncio
    async def test_missing_url(self, capsys):
        """Empty URL returns exit code 3 with error on stderr."""
        args = Namespace(url="", token="test-token")
        exit_code = await run_pending(args)

        assert exit_code == EXIT_CONNECTION_ERROR
        stderr = capsys.readouterr().err
        assert "URL required" in stderr

    @pytest.mark.asyncio
    async def test_connection_error(self, capsys):
        """AgentGateConnectionError returns exit code 3."""
        from agent_gate.client import AgentGateConnectionError

        args = Namespace(url="wss://gw:8443", token="test-token")
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(
            side_effect=AgentGateConnectionError(-1, "Connection refused"),
        )
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch(_CLI_PATCH, return_value=mock_client):
            exit_code = await run_pending(args)

        assert exit_code == EXIT_CONNECTION_ERROR
        stderr = capsys.readouterr().err
        assert "Connection failed" in stderr
