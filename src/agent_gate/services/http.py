"""Generic HTTP service handler -- executes YAML-defined tool requests."""

from __future__ import annotations

import re
from typing import Any

import aiohttp

from agent_gate.config import ServiceConfig, ToolDefinition
from agent_gate.services.base import ServiceHandler


class HTTPServiceError(Exception):
    """Raised when a generic HTTP service call fails."""


class GenericHTTPService(ServiceHandler):
    """Service handler that executes YAML-defined HTTP tool requests.

    Each tool is defined in a YAML file with its HTTP method, path template,
    body exclusions, and response wrapping.  This handler resolves those
    definitions at runtime so that adding a new service or tool requires
    only YAML -- no Python code.
    """

    def __init__(self, config: ServiceConfig) -> None:
        self._config = config
        self._base_url = config.url.rstrip("/")
        self._session: aiohttp.ClientSession | None = None
        # Index tools by name for fast lookup
        self._tools: dict[str, ToolDefinition] = {t.name: t for t in config.tools}

    def _get_session(self) -> aiohttp.ClientSession:
        """Return existing session or create new one with auth headers."""
        if self._session is None or self._session.closed:
            headers: dict[str, str] = {}
            auth_obj: aiohttp.BasicAuth | None = None

            if self._config.auth.type == "bearer":
                headers["Authorization"] = f"Bearer {self._config.auth.token}"
            elif self._config.auth.type == "header":
                headers[self._config.auth.header_name] = self._config.auth.token
            elif self._config.auth.type == "basic":
                auth_obj = aiohttp.BasicAuth(self._config.auth.username, self._config.auth.password)
            # query auth is handled per-request in _execute_request

            self._session = aiohttp.ClientSession(headers=headers, auth=auth_obj)
        return self._session

    async def execute(self, tool_name: str, args: dict[str, Any]) -> dict[str, Any]:
        """Execute a tool request based on its YAML definition."""
        tool = self._tools.get(tool_name)
        if tool is None:
            raise HTTPServiceError(f"Unknown tool: {tool_name}")
        if tool.request is None:
            raise HTTPServiceError(f"Tool {tool_name} has no request definition")

        session = self._get_session()
        try:
            return await self._execute_request(session, tool, args)
        except HTTPServiceError:
            raise
        except aiohttp.ClientError as exc:
            raise HTTPServiceError(f"Service unreachable: {self._config.name} ({exc})") from exc

    async def _execute_request(
        self,
        session: aiohttp.ClientSession,
        tool: ToolDefinition,
        args: dict[str, Any],
    ) -> dict[str, Any]:
        """Build and send the HTTP request defined by the tool."""
        # 1. Interpolate path template
        path = self._interpolate_path(tool.request.path, args)
        url = f"{self._base_url}{path}"

        # 2. Build query params (for query auth)
        params: dict[str, str] = {}
        if self._config.auth.type == "query":
            params[self._config.auth.query_param] = self._config.auth.token

        # 3. Build body for POST/PUT/PATCH
        body: dict[str, Any] | None = None
        if tool.request.method in ("POST", "PUT", "PATCH"):
            body = self._build_body(tool, args)

        # 4. Make request
        method = tool.request.method.lower()
        method_fn = getattr(session, method)
        async with method_fn(url, json=body, params=params or None) as resp:
            await self._check_response(resp)
            data = await resp.json()

            # 5. Response wrapping
            if tool.response and tool.response.wrap:
                return {tool.response.wrap: data}
            return data

    @staticmethod
    def _interpolate_path(path: str, args: dict[str, Any]) -> str:
        """Replace {arg_name} placeholders in path with actual values."""

        def replacer(m: re.Match) -> str:
            key = m.group(1)
            val = args.get(key, "")
            return str(val)

        return re.sub(r"\{(\w+)\}", replacer, path)

    @staticmethod
    def _build_body(tool: ToolDefinition, args: dict[str, Any]) -> dict[str, Any]:
        """Build request body, excluding specified args."""
        if tool.request.body_exclude:
            return {k: v for k, v in args.items() if k not in tool.request.body_exclude}
        return dict(args)

    async def _check_response(self, resp: aiohttp.ClientResponse) -> None:
        """Check HTTP response status, using service-level error mappings."""
        if 200 <= resp.status < 300:
            return

        # Check service-level error mappings first
        for mapping in self._config.errors:
            if mapping.status == resp.status:
                body = await resp.text()
                msg = mapping.message.format(status=resp.status, body=body)
                raise HTTPServiceError(msg)

        # Default error handling (no mapping matched)
        if resp.status == 401:
            raise HTTPServiceError("Service authentication failed")
        if resp.status == 404:
            raise HTTPServiceError("Resource not found")
        body = await resp.text()
        raise HTTPServiceError(f"API error {resp.status}: {body}")

    async def health_check(self) -> bool:
        """Check service health using configured endpoint."""
        import logging

        try:
            session = self._get_session()
            health = self._config.health
            method_fn = getattr(session, health.method.lower())
            async with method_fn(
                f"{self._base_url}{health.path}",
                timeout=aiohttp.ClientTimeout(total=5),
            ) as resp:
                return resp.status == health.expect_status
        except Exception as e:
            logging.getLogger("agent_gate.services.http").debug(
                "Health check failed for %s: %s", self._config.name, e
            )
            return False

    async def close(self) -> None:
        """Close the aiohttp session."""
        if self._session is not None and not self._session.closed:
            await self._session.close()
        self._session = None
