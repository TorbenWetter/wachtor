"""CLI client commands -- one-shot tool requests via the gateway."""

from __future__ import annotations

import asyncio
import json
import sys
from argparse import Namespace

from agent_gate.client import (
    AgentGateClient,
    AgentGateConnectionError,
    AgentGateDenied,
    AgentGateError,
    AgentGateTimeout,
)

# Exit codes
EXIT_SUCCESS = 0
EXIT_DENIED = 1
EXIT_TIMEOUT = 2
EXIT_CONNECTION_ERROR = 3
EXIT_INVALID_ARGS = 4


def parse_key_value_args(raw_args: list[str]) -> dict[str, str]:
    """Parse a list of 'key=value' strings into a dict.

    Raises ValueError if any arg is malformed.
    """
    result: dict[str, str] = {}
    for item in raw_args:
        if "=" not in item:
            raise ValueError(f"Invalid argument format (expected key=value): {item!r}")
        key, _, value = item.partition("=")
        if not key:
            raise ValueError(f"Empty key in argument: {item!r}")
        result[key] = value
    return result


async def run_request(args: Namespace) -> int:
    """Execute a one-shot tool request via the gateway.

    Returns exit code (0=success, 1=denied, 2=timeout, 3=connection, 4=invalid args).
    """
    url = args.url
    token = args.token
    tool = args.tool
    timeout = args.timeout

    if not url:
        print("Error: Gateway URL required (--url or AGENT_GATE_URL)", file=sys.stderr)
        return EXIT_CONNECTION_ERROR
    if not token:
        print("Error: Agent token required (--token or AGENT_TOKEN)", file=sys.stderr)
        return EXIT_CONNECTION_ERROR

    # Parse key=value arguments
    try:
        tool_args = parse_key_value_args(args.args)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        return EXIT_INVALID_ARGS

    try:
        async with AgentGateClient(url, token, max_retries=0) as client:
            result = await asyncio.wait_for(
                client.tool_request(tool, **tool_args),
                timeout=timeout,
            )
        print(json.dumps(result, indent=2))
        return EXIT_SUCCESS

    except AgentGateDenied as e:
        print(f"Error: Denied ({e.code}): {e.message}", file=sys.stderr)
        return EXIT_DENIED
    except AgentGateTimeout as e:
        print(f"Error: Timeout ({e.code}): {e.message}", file=sys.stderr)
        return EXIT_TIMEOUT
    except TimeoutError:
        print("Error: Request timed out waiting for response", file=sys.stderr)
        return EXIT_TIMEOUT
    except AgentGateConnectionError as e:
        print(f"Error: Connection failed ({e.code}): {e.message}", file=sys.stderr)
        return EXIT_CONNECTION_ERROR
    except AgentGateError as e:
        print(f"Error: Gateway error ({e.code}): {e.message}", file=sys.stderr)
        return EXIT_DENIED
    except OSError as e:
        print(f"Error: Connection failed: {e}", file=sys.stderr)
        return EXIT_CONNECTION_ERROR


async def run_tools(args: Namespace) -> int:
    """List available tools from the gateway. Returns exit code."""
    url = args.url
    token = args.token

    if not url:
        print("Error: Gateway URL required (--url or AGENT_GATE_URL)", file=sys.stderr)
        return EXIT_CONNECTION_ERROR
    if not token:
        print("Error: Agent token required (--token or AGENT_TOKEN)", file=sys.stderr)
        return EXIT_CONNECTION_ERROR

    try:
        async with AgentGateClient(url, token, max_retries=0) as client:
            tools = await client.list_tools()

        print(json.dumps(tools, indent=2))
        return EXIT_SUCCESS

    except AgentGateConnectionError as e:
        print(f"Error: Connection failed ({e.code}): {e.message}", file=sys.stderr)
        return EXIT_CONNECTION_ERROR
    except AgentGateError as e:
        print(f"Error: Gateway error ({e.code}): {e.message}", file=sys.stderr)
        return EXIT_DENIED
    except OSError as e:
        print(f"Error: Connection failed: {e}", file=sys.stderr)
        return EXIT_CONNECTION_ERROR
    except TimeoutError:
        print("Error: Timed out waiting for tool list", file=sys.stderr)
        return EXIT_TIMEOUT


async def run_pending(args: Namespace) -> int:
    """Retrieve pending results from the gateway. Returns exit code."""
    url = args.url
    token = args.token

    if not url:
        print("Error: Gateway URL required (--url or AGENT_GATE_URL)", file=sys.stderr)
        return EXIT_CONNECTION_ERROR
    if not token:
        print("Error: Agent token required (--token or AGENT_TOKEN)", file=sys.stderr)
        return EXIT_CONNECTION_ERROR

    try:
        async with AgentGateClient(url, token, max_retries=0) as client:
            results = await client.get_pending_results()
        print(json.dumps(results, indent=2))
        return EXIT_SUCCESS

    except AgentGateConnectionError as e:
        print(f"Error: Connection failed ({e.code}): {e.message}", file=sys.stderr)
        return EXIT_CONNECTION_ERROR
    except AgentGateError as e:
        print(f"Error: Gateway error ({e.code}): {e.message}", file=sys.stderr)
        return EXIT_DENIED
    except TimeoutError:
        print("Error: Connection timed out", file=sys.stderr)
        return EXIT_CONNECTION_ERROR
    except OSError as e:
        print(f"Error: Connection failed: {e}", file=sys.stderr)
        return EXIT_CONNECTION_ERROR
