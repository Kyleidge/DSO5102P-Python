"""MCP server assembly and transports (stdio default, Streamable HTTP optional).

Security model
--------------
* stdio: the MCP client launched this process; stdout carries only protocol
  messages, all logging goes to stderr (and optionally a log file).
* HTTP: binds 127.0.0.1 by default with DNS-rebinding protection (Host/Origin
  checks). A non-loopback bind is refused unless ``allow_remote`` is set *and*
  an auth token is configured; every request must then carry
  ``Authorization: Bearer <token>``.
* No tool can execute shell commands, write firmware/files on the instrument or
  touch arbitrary host paths (exports go to one configured directory by name).
"""

from __future__ import annotations

import hmac
import ipaddress
import logging
import sys
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings

from hantek_mcp import __version__
from hantek_mcp.config import AppConfig
from hantek_mcp.errors import HantekError, ParameterError
from hantek_mcp.mcp.prompts import register_prompts
from hantek_mcp.mcp.resources import register_resources
from hantek_mcp.mcp.tools import register_tools
from hantek_mcp.service import ScopeService

log = logging.getLogger(__name__)

INSTRUCTIONS = """\
Controls a Hantek DSO5102P 2-channel 100 MHz oscilloscope (or its simulator) and returns real numerical
waveform records. Start with scope_status / scope_get_capabilities. Captures return a capture_id plus metadata;
analyse them with the scope_measure_* / scope_fft tools instead of pulling bulk samples. Every result states
whether data is simulated and whether voltage/time scaling is hardware-verified; report that honestly. Settings
changes are verified by read-back. Prefer scope_capture(mode="single") for records proven to be new. Use
scope_abort to stop long workflows. Read scope://docs/measurement-guide for the full checklist.
"""


def configure_logging(config: AppConfig) -> None:
    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    handler = logging.StreamHandler(sys.stderr)  # never stdout: it carries MCP protocol messages on stdio
    handler.setFormatter(fmt)
    root.addHandler(handler)
    if config.log_file:
        config.log_file.parent.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(config.log_file, encoding="utf-8")
        fh.setFormatter(fmt)
        root.addHandler(fh)
    root.setLevel(config.log_level)


def build_server(
    service: ScopeService,
    auto_connect: bool = False,
    owns_service: bool = True,
) -> MCPServer[Any]:
    """Create the MCP server around an existing service (the GUI passes its own shared service)."""

    @asynccontextmanager
    async def lifespan(_: MCPServer[Any]) -> AsyncIterator[dict[str, Any]]:
        if auto_connect and not service.controller.is_connected:
            import anyio

            try:
                await anyio.to_thread.run_sync(service.connect)
                log.info("connected to the instrument (%s backend)", service.config.backend)
            except HantekError as e:
                log.warning(
                    "auto-connect failed: %s (tools will report NOT_CONNECTED until scope_connect)", e
                )
        try:
            yield {"service": service}
        finally:
            if owns_service and service.controller.is_connected:
                service.controller.disconnect()

    app: MCPServer[Any] = MCPServer(
        "hantek-dso5102p",
        title="Hantek DSO5102P oscilloscope",
        description="Numerical waveform acquisition, instrument control and signal analysis for the Hantek DSO5102P",
        instructions=INSTRUCTIONS,
        version=__version__,
        lifespan=lifespan,
        log_level=service.config.log_level,
    )
    get: Callable[[], ScopeService] = lambda: service  # noqa: E731
    register_tools(app, get)
    register_resources(app, get)
    register_prompts(app)
    return app


def _is_loopback(host: str) -> bool:
    if host in ("localhost", "::1"):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def check_http_security(config: AppConfig) -> None:
    sc = config.server
    if not _is_loopback(sc.http_host):
        if not sc.allow_remote:
            raise ParameterError(
                f"refusing to bind {sc.http_host}: non-loopback HTTP requires server.allow_remote=true and an "
                "auth token (the instrument may be connected to live electronics)"
            )
        if not sc.auth_token or len(sc.auth_token) < 16:
            raise ParameterError("remote HTTP access requires server.auth_token with at least 16 characters")


def transport_security(config: AppConfig) -> TransportSecuritySettings:
    sc = config.server
    hosts = [f"127.0.0.1:{sc.http_port}", f"localhost:{sc.http_port}", f"[::1]:{sc.http_port}"]
    origins = [f"http://127.0.0.1:{sc.http_port}", f"http://localhost:{sc.http_port}"]
    if not _is_loopback(sc.http_host):
        hosts.append(f"{sc.http_host}:{sc.http_port}")
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=hosts,
        allowed_origins=origins + list(sc.allowed_origins),
    )


class BearerAuthMiddleware:
    """Pure-ASGI bearer-token check (constant-time comparison)."""

    def __init__(self, app: Any, token: str) -> None:
        self.app = app
        self.token = token.encode()

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] == "http":
            headers = dict(scope.get("headers") or [])
            auth = headers.get(b"authorization", b"")
            ok = auth.startswith(b"Bearer ") and hmac.compare_digest(auth[7:].strip(), self.token)
            if not ok:
                await send(
                    {
                        "type": "http.response.start",
                        "status": 401,
                        "headers": [(b"content-type", b"application/json"), (b"www-authenticate", b"Bearer")],
                    }
                )
                await send({"type": "http.response.body", "body": b'{"error":"unauthorized"}'})
                return
        await self.app(scope, receive, send)


def http_app(app: MCPServer[Any], config: AppConfig) -> Any:
    check_http_security(config)
    asgi = app.streamable_http_app(
        streamable_http_path=config.server.http_path,
        transport_security=transport_security(config),
        host=config.server.http_host,
    )
    if config.server.auth_token:
        return BearerAuthMiddleware(asgi, config.server.auth_token)
    return asgi


def run_stdio(service: ScopeService, auto_connect: bool = True) -> None:
    app = build_server(service, auto_connect=auto_connect)
    app.run("stdio")


def run_http(service: ScopeService, auto_connect: bool = True) -> None:
    import uvicorn

    cfg = service.config
    app = build_server(service, auto_connect=auto_connect)
    asgi = http_app(app, cfg)
    log.info(
        "serving MCP over Streamable HTTP at http://%s:%d%s",
        cfg.server.http_host,
        cfg.server.http_port,
        cfg.server.http_path,
    )
    uvicorn.run(
        asgi,
        host=cfg.server.http_host,
        port=cfg.server.http_port,
        log_level=cfg.log_level.lower(),
        log_config=None,
    )
