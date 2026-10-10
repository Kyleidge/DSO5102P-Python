"""stdio <-> Streamable HTTP bridge.

Lets a stdio-only MCP client (e.g. a desktop app launching ``hantek-mcp bridge``)
talk to the MCP endpoint hosted by the GUI process, so the GUI and the AI share
one instrument owner instead of competing for the USB device. The bridge
forwards tools, resources and prompts verbatim; it adds no capabilities.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import mcp_types as types
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.shared._httpx_utils import create_mcp_http_client

from hantek_mcp import __version__

log = logging.getLogger(__name__)


def build_bridge(url: str, token: str | None = None) -> Server[Any]:
    headers = {"Authorization": f"Bearer {token}"} if token else None
    state: dict[str, Client] = {}

    @asynccontextmanager
    async def lifespan(_: Server[Any]) -> AsyncIterator[dict[str, Any]]:
        http = create_mcp_http_client(headers=headers)
        async with http, Client(streamable_http_client(url, http_client=http), mode="legacy") as client:
            state["c"] = client
            log.info("bridge connected to %s", url)
            yield {}

    def c() -> Client:
        return state["c"]

    async def list_tools(_ctx: Any, params: types.PaginatedRequestParams | None) -> types.ListToolsResult:
        return await c().list_tools(cursor=params.cursor if params else None)

    async def call_tool(_ctx: Any, params: types.CallToolRequestParams) -> types.CallToolResult:
        return await c().call_tool(params.name, params.arguments or {})

    async def list_resources(
        _ctx: Any, params: types.PaginatedRequestParams | None
    ) -> types.ListResourcesResult:
        return await c().list_resources(cursor=params.cursor if params else None)

    async def list_templates(
        _ctx: Any, params: types.PaginatedRequestParams | None
    ) -> types.ListResourceTemplatesResult:
        return await c().list_resource_templates(cursor=params.cursor if params else None)

    async def read_resource(_ctx: Any, params: types.ReadResourceRequestParams) -> types.ReadResourceResult:
        return await c().read_resource(str(params.uri))

    async def list_prompts(_ctx: Any, params: types.PaginatedRequestParams | None) -> types.ListPromptsResult:
        return await c().list_prompts(cursor=params.cursor if params else None)

    async def get_prompt(_ctx: Any, params: types.GetPromptRequestParams) -> types.GetPromptResult:
        return await c().get_prompt(params.name, params.arguments)

    return Server(
        "hantek-dso5102p-bridge",
        version=__version__,
        instructions=f"Bridge to the Hantek DSO5102P MCP endpoint at {url} (hosted by the GUI).",
        lifespan=lifespan,
        on_list_tools=list_tools,
        on_call_tool=call_tool,
        on_list_resources=list_resources,
        on_list_resource_templates=list_templates,
        on_read_resource=read_resource,
        on_list_prompts=list_prompts,
        on_get_prompt=get_prompt,
    )


async def run_bridge(url: str, token: str | None = None) -> None:
    server = build_bridge(url, token)
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())
