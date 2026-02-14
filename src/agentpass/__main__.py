"""CLI entrypoint and orchestration for agentpass."""

from __future__ import annotations

import argparse
import asyncio
import importlib
import logging
import os
import signal
import ssl
import sys
from pathlib import Path

import websockets
import websockets.asyncio.server
from aiohttp import web

from agentpass.config import ConfigError, ServiceConfig, load_config, load_permissions
from agentpass.dashboard import setup_dashboard
from agentpass.db import Database
from agentpass.engine import PermissionEngine
from agentpass.executor import Executor
from agentpass.messenger.telegram import TelegramAdapter
from agentpass.registry import build_registry
from agentpass.server import GatewayServer
from agentpass.services.base import ServiceHandler
from agentpass.services.http import GenericHTTPService

logger = logging.getLogger("agentpass")


def _load_plugin_service(config: ServiceConfig) -> ServiceHandler:
    """Load a Python plugin service handler from handler_class spec.

    The handler_class field must be in "module.path:ClassName" format.
    The class receives (config, tools) as constructor arguments.
    """
    handler_class = config.handler_class
    if not handler_class:
        raise ConfigError(
            f"Service '{config.name}' has handler=python but no handler_class specified"
        )

    # Parse "module.path:ClassName"
    if ":" not in handler_class:
        raise ConfigError(
            f"Invalid handler_class format for service '{config.name}': "
            f"expected 'module.path:ClassName', got '{handler_class}'"
        )

    module_path, class_name = handler_class.rsplit(":", 1)

    try:
        module = importlib.import_module(module_path)
    except ImportError as e:
        raise ConfigError(
            f"Cannot import module '{module_path}' for service '{config.name}': {e}"
        ) from e

    try:
        cls = getattr(module, class_name)
    except AttributeError:
        raise ConfigError(
            f"Class '{class_name}' not found in module '{module_path}' for service '{config.name}'"
        ) from None

    return cls(config, config.tools)


KNOWN_COMMANDS = {"serve", "request", "tools", "pending"}


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse CLI arguments with subcommand routing.

    Subcommands:
        serve     Start the gateway server (default if no subcommand given)
        request   Send a one-shot tool request
        tools     List available tools
        pending   Retrieve pending results

    Backward compat: if the first positional arg is not a known subcommand,
    'serve' is prepended automatically.
    """
    raw = list(argv) if argv is not None else sys.argv[1:]

    # Backward compat: if no subcommand given, default to "serve"
    first_positional = next((a for a in raw if not a.startswith("-")), None)
    if first_positional not in KNOWN_COMMANDS:
        raw = ["serve", *raw]

    parser = argparse.ArgumentParser(description="agentpass: execution gateway for AI agents")
    subparsers = parser.add_subparsers(dest="command")

    # serve subcommand
    serve_parser = subparsers.add_parser("serve", help="Start the gateway server")
    serve_parser.add_argument("--insecure", action="store_true", help="Allow plaintext WS (no TLS)")
    serve_parser.add_argument("--config", default="config.yaml", help="Config file path")
    serve_parser.add_argument(
        "--permissions", default="permissions.yaml", help="Permissions file path"
    )

    # request subcommand
    request_parser = subparsers.add_parser("request", help="Send a tool request")
    request_parser.add_argument("tool", help="Tool name")
    request_parser.add_argument("args", nargs="*", default=[], help="key=value arguments")
    request_parser.add_argument(
        "--url",
        default=os.environ.get("AGENTPASS_URL", ""),
        help="Gateway WebSocket URL",
    )
    request_parser.add_argument(
        "--token",
        default=os.environ.get("AGENT_TOKEN", ""),
        help="Agent token",
    )
    request_parser.add_argument("--timeout", type=float, default=900.0, help="Timeout in seconds")

    # tools subcommand
    tools_parser = subparsers.add_parser("tools", help="List available tools")
    tools_parser.add_argument(
        "--url",
        default=os.environ.get("AGENTPASS_URL", ""),
        help="Gateway WebSocket URL",
    )
    tools_parser.add_argument(
        "--token",
        default=os.environ.get("AGENT_TOKEN", ""),
        help="Agent token",
    )

    # pending subcommand
    pending_parser = subparsers.add_parser("pending", help="Retrieve pending results")
    pending_parser.add_argument(
        "--url",
        default=os.environ.get("AGENTPASS_URL", ""),
        help="Gateway WebSocket URL",
    )
    pending_parser.add_argument(
        "--token",
        default=os.environ.get("AGENT_TOKEN", ""),
        help="Agent token",
    )

    return parser.parse_args(raw)


async def run(args: argparse.Namespace) -> None:
    """Main async entrypoint -- orchestrates all components."""
    # 1. Load config
    config = load_config(args.config)
    permissions = load_permissions(args.permissions)

    # 2. TLS check
    if not args.insecure and config.gateway.tls is None:
        logger.error("TLS not configured. Use --insecure to allow plaintext WS.")
        sys.exit(1)

    # 3. Signal handling
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop_event.set)

    # 4. Initialize database
    db = Database(config.storage.path)
    await db.initialize()
    await db.cleanup_stale_requests()

    # 5. Build registry from all service tool definitions
    registry = build_registry(config.services)

    # 6. Initialize services + health checks
    services: dict[str, ServiceHandler] = {}
    for name, svc_config in config.services.items():
        if svc_config.handler == "python":
            service = _load_plugin_service(svc_config)
        else:
            service = GenericHTTPService(svc_config)
        if not await service.health_check():
            logger.warning("Service '%s' unreachable — continuing anyway", name)
        services[name] = service

    executor = Executor(services, registry)

    # 7. Initialize permission engine
    engine = PermissionEngine(permissions, registry=registry)

    # 8. Initialize Telegram adapter
    storage_dir = Path(config.storage.path).parent
    persistence_path = str(storage_dir / "callback_data.pickle")
    telegram = TelegramAdapter(config.messenger.telegram, persistence_path=persistence_path)

    # 9. Initialize gateway server
    gateway = GatewayServer(
        agent_token=config.agent.token,
        engine=engine,
        executor=executor,
        messenger=telegram,
        db=db,
        approval_timeout=config.approval_timeout,
        rate_limit_config=config.rate_limit,
        registry=registry,
        services=services,
    )

    # Wire approval callback
    await telegram.on_approval_callback(gateway.resolve_approval)

    # 10. PTB manual lifecycle -- NOT run_polling()
    ptb_app = telegram.application
    async with ptb_app:
        await ptb_app.start()
        await ptb_app.updater.start_polling()

        # 11. SSL context
        ssl_ctx = None
        if config.gateway.tls:
            ssl_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            ssl_ctx.load_cert_chain(config.gateway.tls.cert, config.gateway.tls.key)

        # 12. Start health HTTP server
        async def _health_handler(request: web.Request) -> web.Response:
            try:
                status = await gateway.health_status()
                code = 200 if status["status"] == "healthy" else 503
                return web.json_response(status, status=code)
            except Exception:
                logger.exception("Health check failed")
                return web.json_response(
                    {"status": "unhealthy", "error": "internal error"},
                    status=500,
                )

        health_app = web.Application()
        health_app.router.add_get("/healthz", _health_handler)

        # Wire dashboard routes
        setup_dashboard(health_app, db)

        health_runner = web.AppRunner(health_app)
        await health_runner.setup()
        health_host = config.gateway.health_host
        health_port = config.gateway.health_port
        health_site = web.TCPSite(health_runner, health_host, health_port)
        await health_site.start()
        logger.info("Health/dashboard on http://%s:%d", health_host, health_port)

        # 13. Start WebSocket server
        async with websockets.asyncio.server.serve(
            gateway.handle_connection,
            config.gateway.host,
            config.gateway.port,
            ssl=ssl_ctx,
        ):
            proto = "wss" if ssl_ctx else "ws"
            logger.info(
                "agentpass ready on %s://%s:%d",
                proto,
                config.gateway.host,
                config.gateway.port,
            )
            await stop_event.wait()

        # 14. Graceful shutdown
        logger.info("Shutting down...")
        await health_runner.cleanup()
        await gateway.resolve_all_pending("gateway_shutdown")
        await telegram.stop()
        await ptb_app.updater.stop()
        await ptb_app.stop()

    for svc in services.values():
        await svc.close()
    await db.close()
    logger.info("agentpass stopped")


def main(argv: list[str] | None = None) -> None:
    """Synchronous entrypoint for the CLI."""
    log_level = os.environ.get("LOG_LEVEL", "INFO").upper()
    logging.basicConfig(
        level=getattr(logging, log_level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s — %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    args = parse_args(argv)

    if args.command == "serve":
        try:
            asyncio.run(run(args))
        except ConfigError as e:
            logger.error("Configuration error: %s", e)
            sys.exit(1)
    elif args.command == "request":
        from agentpass.cli import run_request

        exit_code = asyncio.run(run_request(args))
        sys.exit(exit_code)
    elif args.command == "tools":
        from agentpass.cli import run_tools

        exit_code = asyncio.run(run_tools(args))
        sys.exit(exit_code)
    elif args.command == "pending":
        from agentpass.cli import run_pending

        exit_code = asyncio.run(run_pending(args))
        sys.exit(exit_code)


if __name__ == "__main__":
    main()
