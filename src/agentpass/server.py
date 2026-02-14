"""WebSocket server — JSON-RPC 2.0 gateway for agent communication."""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from websockets.exceptions import ConnectionClosed

if TYPE_CHECKING:
    from agentpass.registry import ToolRegistry

from agentpass.db import Database
from agentpass.engine import PermissionEngine, build_signature, validate_args
from agentpass.executor import ExecutionError, Executor
from agentpass.messenger.base import (
    ApprovalChoice,
    ApprovalRequest,
    ApprovalResult,
    MessengerAdapter,
)
from agentpass.models import AuditEntry, Decision, PendingApproval, ToolRequest

logger = logging.getLogger(__name__)

# JSON-RPC 2.0 error codes
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
APPROVAL_DENIED = -32001
APPROVAL_TIMEOUT = -32002
POLICY_DENIED = -32003
EXECUTION_FAILED = -32004
NOT_AUTHENTICATED = -32005
RATE_LIMIT_EXCEEDED = -32006

AUTH_TIMEOUT = 10  # seconds


def _epoch_to_iso(epoch: float) -> str:
    """Convert epoch float to ISO 8601 string."""
    return datetime.fromtimestamp(epoch, tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class RateLimiter:
    """Sliding-window rate limiter."""

    def __init__(self, max_per_minute: int) -> None:
        self._max = max_per_minute
        self._timestamps: list[float] = []

    def check(self) -> bool:
        """Return True if request is allowed, False if rate limited."""
        now = time.monotonic()
        # Remove timestamps older than 60 seconds
        self._timestamps = [t for t in self._timestamps if now - t < 60]
        if len(self._timestamps) >= self._max:
            return False
        self._timestamps.append(now)
        return True


class GatewayServer:
    """WebSocket gateway server managing agent connections and tool request dispatch."""

    def __init__(
        self,
        *,
        agent_token: str,
        engine: PermissionEngine,
        executor: Executor,
        messenger: MessengerAdapter,
        db: Database,
        approval_timeout: int = 900,
        rate_limit_config: Any = None,
        registry: ToolRegistry | None = None,
        services: dict[str, Any] | None = None,
    ) -> None:
        self._agent_token = agent_token
        self._engine = engine
        self._executor = executor
        self._messenger = messenger
        self._db = db
        self._approval_timeout = approval_timeout
        self._registry = registry
        self._services = services or {}
        self._rate_limiter = RateLimiter(
            rate_limit_config.max_requests_per_minute if rate_limit_config else 60
        )
        self._max_pending = rate_limit_config.max_pending_approvals if rate_limit_config else 10
        self._pending: dict[str, PendingApproval] = {}  # request_id -> PendingApproval
        self._background_tasks: set[asyncio.Task] = set()  # prevent GC of bg tasks
        self._agent_connected = False
        self._agent_ws: Any = None  # Current WebSocket connection
        self._resolve_lock = asyncio.Lock()

    async def health_status(self) -> dict[str, Any]:
        """Return health status of all components."""
        db_ok = await self._db.health_check()
        telegram_ok = await self._messenger.health_check()
        services_status = {}
        for name, svc in self._services.items():
            services_status[name] = await svc.health_check()

        all_critical_ok = db_ok and telegram_ok
        return {
            "status": "healthy" if all_critical_ok else "unhealthy",
            "checks": {
                "database": db_ok,
                "telegram": telegram_ok,
                "services": services_status,
            },
        }

    async def handle_connection(self, websocket: Any) -> None:
        """Handle a single agent WebSocket connection."""
        if self._agent_connected:
            await websocket.close(4000, "Another agent is already connected")
            return

        self._agent_connected = True
        self._agent_ws = websocket
        logger.info("Agent connected")

        try:
            # Phase 1: Authentication with deadline
            authenticated = await self._authenticate(websocket)
            if not authenticated:
                return

            # Phase 2: Message loop — each message gets its own task
            tasks: set[asyncio.Task] = set()
            async for raw_message in websocket:
                task = asyncio.create_task(self._handle_message(websocket, raw_message))
                tasks.add(task)
                task.add_done_callback(tasks.discard)

            # Wait for in-flight message tasks to finish
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
        except ConnectionClosed:
            logger.info("Agent disconnected")
        finally:
            self._agent_connected = False
            self._agent_ws = None
            logger.info("Agent session ended")

    async def _authenticate(self, websocket: Any) -> bool:
        """Handle auth flow. Returns True if authenticated."""
        try:
            raw = await asyncio.wait_for(websocket.recv(), timeout=AUTH_TIMEOUT)
        except TimeoutError:
            await self._send_error(websocket, NOT_AUTHENTICATED, "Authentication timeout", None)
            await websocket.close()
            return False

        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            await self._send_error(websocket, PARSE_ERROR, "Parse error", None)
            await websocket.close()
            return False

        if msg.get("method") != "auth":
            await self._send_error(websocket, NOT_AUTHENTICATED, "Not authenticated", msg.get("id"))
            await websocket.close()
            return False

        token = msg.get("params", {}).get("token", "")
        if token != self._agent_token:
            await self._send_error(websocket, NOT_AUTHENTICATED, "Invalid token", msg.get("id"))
            await websocket.close()
            return False

        # Auth success
        await self._send_result(websocket, {"status": "authenticated"}, msg.get("id"))
        logger.info("Agent authenticated")
        return True

    async def _handle_message(self, websocket: Any, raw_message: str) -> None:
        """Process a single JSON-RPC message."""
        # Parse
        try:
            msg = json.loads(raw_message)
        except json.JSONDecodeError:
            await self._send_error(websocket, PARSE_ERROR, "Parse error", None)
            return

        msg_id = msg.get("id")
        method = msg.get("method")

        if not method:
            await self._send_error(websocket, INVALID_REQUEST, "Missing method", msg_id)
            return

        if method == "tool_request":
            await self._handle_tool_request(websocket, msg, msg_id)
        elif method == "get_pending_results":
            await self._handle_get_pending_results(websocket, msg_id)
        elif method == "list_tools":
            await self._handle_list_tools(websocket, msg_id)
        else:
            await self._send_error(websocket, METHOD_NOT_FOUND, f"Unknown method: {method}", msg_id)

    async def _handle_tool_request(self, websocket: Any, msg: dict, msg_id: Any) -> None:
        """Process a tool_request."""
        # Validate msg_id is present (Major 1: prevents None collisions)
        if msg_id is None:
            await self._send_error(websocket, INVALID_REQUEST, "Missing request id", None)
            return

        params = msg.get("params", {})
        tool_name = params.get("tool")
        args = params.get("args", {})

        if not tool_name:
            await self._send_error(websocket, INVALID_REQUEST, "Missing tool name", msg_id)
            return

        # Rate limit check (before engine)
        if not self._rate_limiter.check():
            await self._send_error(websocket, RATE_LIMIT_EXCEEDED, "Rate limit exceeded", msg_id)
            return

        # Validate args
        try:
            validate_args(tool_name, args)
        except ValueError as e:
            await self._send_error(websocket, INVALID_REQUEST, str(e), msg_id)
            return

        # Build signature
        signature = build_signature(tool_name, args)

        # Generate unique request ID (decoupled from client msg_id)
        request_id = str(uuid.uuid4())

        # Create tool request
        request = ToolRequest(id=request_id, tool_name=tool_name, args=args, signature=signature)

        # Evaluate permission
        decision = self._engine.evaluate(tool_name, args)

        # Log audit (initial decision)
        audit = AuditEntry(
            request_id=request_id,
            tool_name=tool_name,
            args=args,
            signature=signature,
            decision=decision.value,
        )
        await self._db.log_audit(audit)

        if decision == Decision.ALLOW:
            await self._execute_and_respond(websocket, request, msg_id)
        elif decision == Decision.DENY:
            await self._send_error(websocket, POLICY_DENIED, "Denied by policy", msg_id)
        elif decision == Decision.ASK:
            # Check pending limit
            if len(self._pending) >= self._max_pending:
                await self._send_error(
                    websocket, RATE_LIMIT_EXCEEDED, "Too many pending approvals", msg_id
                )
                return
            await self._request_approval(websocket, request, msg_id)

    async def _execute_and_respond(
        self, websocket: Any, request: ToolRequest, msg_id: Any
    ) -> dict[str, Any] | None:
        """Execute a tool and send the result. Returns result data or None on error."""
        try:
            result_data = await self._executor.execute(request.tool_name, request.args)
            await self._send_result(websocket, {"status": "executed", "data": result_data}, msg_id)
            return result_data
        except ExecutionError as e:
            await self._send_error(websocket, EXECUTION_FAILED, str(e), msg_id)
            return None
        except Exception:
            logger.exception("Unexpected error executing %s", request.tool_name)
            await self._send_error(websocket, EXECUTION_FAILED, "Internal execution error", msg_id)
            return None

    async def _request_approval(self, websocket: Any, request: ToolRequest, msg_id: Any) -> None:
        """Send approval request to messenger and wait for response."""
        request_id = request.id

        # Store in DB
        expires_at_epoch = time.time() + self._approval_timeout
        expires_at_iso = _epoch_to_iso(expires_at_epoch)
        await self._db.insert_pending(
            request_id=request_id,
            tool_name=request.tool_name,
            args=request.args,
            signature=request.signature,
            expires_at=expires_at_iso,
        )

        # Send to messenger
        approval_req = ApprovalRequest(
            request_id=request_id,
            tool_name=request.tool_name,
            args=request.args,
            signature=request.signature,
        )
        choices = [
            ApprovalChoice(label="Allow", action="allow"),
            ApprovalChoice(label="Deny", action="deny"),
        ]
        message_id = await self._messenger.send_approval(approval_req, choices)

        # Create future and pending approval
        loop = asyncio.get_running_loop()
        future: asyncio.Future[ApprovalResult] = loop.create_future()
        pending = PendingApproval(
            request=request,
            future=future,
            message_id=message_id,
            expires_at=expires_at_epoch,
        )
        self._pending[request_id] = pending

        # Critical 2: Schedule timeout via messenger
        if hasattr(self._messenger, "schedule_timeout"):
            self._messenger.schedule_timeout(request_id, self._approval_timeout, message_id)

        # Wait for resolution
        try:
            result = await future

            # Major 5: Update audit log with resolution
            resolution = self._resolution_label(result)

            if result.action == "allow":
                exec_result = await self._execute_and_respond(websocket, request, msg_id)
                await self._db.update_audit_resolution(
                    request_id=request_id,
                    resolution=resolution,
                    resolved_by=result.user_id,
                    resolved_at=result.timestamp,
                    execution_result=exec_result,
                )
            else:
                await self._db.update_audit_resolution(
                    request_id=request_id,
                    resolution=resolution,
                    resolved_by=result.user_id,
                    resolved_at=result.timestamp,
                )
                error_code = APPROVAL_TIMEOUT if result.user_id == "timeout" else APPROVAL_DENIED
                error_msg = (
                    "Approval timed out" if result.user_id == "timeout" else "Denied by user"
                )
                await self._send_error(websocket, error_code, error_msg, msg_id)

            # Agent received the response, clean up DB
            self._pending.pop(request_id, None)
            await self._db.delete_pending(request_id)

        except ConnectionClosed:
            # Agent disconnected while waiting — keep pending in DB for offline retrieval
            logger.info("Agent disconnected while awaiting approval for %s", request_id)
            self._pending.pop(request_id, None)

            # Wait for the future to resolve (approval may still come in)
            if not future.done():
                # Create a background task to handle the resolution after disconnect
                bg_task = asyncio.create_task(
                    self._handle_offline_resolution(request_id, request, future)
                )
                self._background_tasks.add(bg_task)
                bg_task.add_done_callback(self._background_tasks.discard)
            else:
                # Future already resolved while disconnect was being handled
                result = future.result()
                await self._store_offline_result(request_id, request, result)

    async def _handle_offline_resolution(
        self,
        request_id: str,
        request: ToolRequest,
        future: asyncio.Future[ApprovalResult],
    ) -> None:
        """Handle approval resolution after agent disconnected."""
        try:
            result = await future
            await self._store_offline_result(request_id, request, result)
        except asyncio.CancelledError:
            # Gateway shutting down — clean up
            await self._db.delete_pending(request_id)
        except Exception:
            logger.exception("Error handling offline resolution for %s", request_id)
            await self._db.delete_pending(request_id)

    async def _store_offline_result(
        self,
        request_id: str,
        request: ToolRequest,
        result: ApprovalResult,
    ) -> None:
        """Execute tool (if approved) and store the result in DB for later retrieval."""
        resolution = self._resolution_label(result)

        # Update audit
        await self._db.update_audit_resolution(
            request_id=request_id,
            resolution=resolution,
            resolved_by=result.user_id,
            resolved_at=result.timestamp,
        )

        if result.action == "allow":
            try:
                exec_data = await self._executor.execute(request.tool_name, request.args)
                result_json = json.dumps({"status": "executed", "data": exec_data})
            except Exception:
                logger.exception("Offline execution failed for %s", request_id)
                result_json = json.dumps({"status": "error", "data": "Execution failed"})
        else:
            reason = "Approval timed out" if result.user_id == "timeout" else "Denied by user"
            result_json = json.dumps({"status": "denied", "data": reason})

        await self._db.update_pending_result(request_id, result_json)

    @staticmethod
    def _resolution_label(result: ApprovalResult) -> str:
        """Map an ApprovalResult to a human-readable resolution label."""
        if result.user_id == "timeout":
            return "timed_out"
        return "approved" if result.action == "allow" else "denied"

    async def resolve_approval(self, result: ApprovalResult) -> None:
        """Called by messenger when a human approves/denies or timeout fires."""
        async with self._resolve_lock:
            request_id = result.request_id
            pending = self._pending.get(request_id)

            if pending and not pending.future.done():
                pending.future.set_result(result)

    async def _handle_get_pending_results(self, websocket: Any, msg_id: Any) -> None:
        """Return any stored results from approvals resolved while agent was disconnected."""
        results = await self._db.get_completed_results()
        await self._send_result(websocket, {"results": results}, msg_id)

        # Clean up retrieved results from DB
        if results:
            request_ids = [r["request_id"] for r in results]
            await self._db.delete_completed_results(request_ids)

    async def _handle_list_tools(self, websocket: Any, msg_id: Any) -> None:
        """Return available tool definitions."""
        if self._registry is None:
            await self._send_result(websocket, {"tools": []}, msg_id)
            return

        tools = []
        for tool_def in self._registry.all_tools():
            args_schema: dict[str, dict[str, Any]] = {}
            for arg_name, arg_def in tool_def.args.items():
                arg_info: dict[str, Any] = {"required": arg_def.required}
                if arg_def.validate:
                    arg_info["validate"] = arg_def.validate
                args_schema[arg_name] = arg_info

            tools.append(
                {
                    "name": tool_def.name,
                    "description": tool_def.description,
                    "service": tool_def.service_name,
                    "args": args_schema,
                }
            )

        await self._send_result(websocket, {"tools": tools}, msg_id)

    async def resolve_all_pending(self, reason: str = "gateway_shutdown") -> None:
        """Resolve all pending approvals (called during shutdown)."""
        for request_id, pending in list(self._pending.items()):
            if not pending.future.done():
                result = ApprovalResult(
                    request_id=request_id,
                    action="deny",
                    user_id=reason,
                    timestamp=time.time(),
                )
                pending.future.set_result(result)

    # --- JSON-RPC helpers ---

    async def _send_result(self, websocket: Any, result: Any, msg_id: Any) -> None:
        """Send a JSON-RPC success response."""
        response = {"jsonrpc": "2.0", "result": result, "id": msg_id}
        await websocket.send(json.dumps(response))

    async def _send_error(self, websocket: Any, code: int, message: str, msg_id: Any) -> None:
        """Send a JSON-RPC error response."""
        response = {
            "jsonrpc": "2.0",
            "error": {"code": code, "message": message},
            "id": msg_id,
        }
        await websocket.send(json.dumps(response))
