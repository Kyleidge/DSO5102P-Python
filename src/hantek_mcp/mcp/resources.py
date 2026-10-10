"""MCP resources (read-only views of instrument and capture state)."""

from __future__ import annotations

import functools
from collections.abc import Callable
from typing import Any

import anyio
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ResourceError, ResourceNotFoundError

from hantek_mcp.errors import CaptureNotFoundError, HantekError, ParameterError
from hantek_mcp.service import ScopeService

MEASUREMENT_GUIDE = """\
# Measuring reliably with the Hantek DSO5102P MCP server

1. `scope_status` / `scope_get_capabilities` first: note `simulated`, `scaling.voltage_verified` and
   `scaling.time_verified`.
2. Configure with the set_* tools. Every change is read back from the instrument; report the
   `confirmed_value`, not what you asked for.
3. Prefer `scope_capture(mode="single")`: freshness `new_trigger_confirmed` (or
   `new_record_content_changed`) proves the record is new.
   `stopped_snapshot` / `running_snapshot` may be old data; `identical_to_previous` means stale.
4. Check `quality` before measuring: `clipped` invalidates amplitudes, `adc_utilisation` < 0.25 means poor
   resolution, `samples_per_cycle` < 10 limits timing accuracy, fewer than 2 cycles prevents frequency.
5. Measurement results carry `valid`, `warnings`, `voltage_scaling_status`, `time_scaling_status`.
   `nominal_unverified` values must be reported as such. Never present simulated data as real.
6. Two-channel timing (phase, delay) is only meaningful for captures sharing a `group_id`.
7. If a trigger times out, say so - do not substitute an untriggered or old record silently.
"""


def register_resources(app: MCPServer, get_service: Callable[[], ScopeService]) -> list[str]:
    uris: list[str] = []

    async def run(fn: Callable[..., Any], *args: Any) -> Any:
        try:
            return await anyio.to_thread.run_sync(functools.partial(fn, *args))
        except (CaptureNotFoundError, ParameterError) as e:
            raise ResourceNotFoundError(str(e)) from e
        except HantekError as e:
            raise ResourceError(str(e)) from e

    def res(uri: str, name: str, description: str, mime: str = "application/json") -> Callable[[Any], Any]:
        uris.append(uri)
        return app.resource(uri, name=name, description=description, mime_type=mime)

    @res(
        "scope://device/status",
        "device-status",
        "Connection, backend (hardware/simulator), acquisition state",
    )
    async def device_status() -> dict[str, Any]:
        return await run(get_service().status)  # type: ignore[no-any-return]

    @res(
        "scope://device/capabilities",
        "device-capabilities",
        "Allowed settings, limits, feature status, safety",
    )
    async def device_capabilities() -> dict[str, Any]:
        return await run(get_service().capabilities)  # type: ignore[no-any-return]

    @res(
        "scope://device/settings",
        "device-settings",
        "Settings read back from the instrument (requires connection)",
    )
    async def device_settings() -> dict[str, Any]:
        return await run(get_service().settings)  # type: ignore[no-any-return]

    @res("scope://device/identity", "device-identity", "Model, firmware, USB endpoints")
    async def device_identity() -> dict[str, Any]:
        return await run(get_service().identify)  # type: ignore[no-any-return]

    @res("scope://captures", "captures", "Stored captures, newest first")
    async def captures() -> dict[str, Any]:
        return await run(get_service().list_captures, 100)  # type: ignore[no-any-return]

    @res("scope://captures/{capture_id}/metadata", "capture-metadata", "Full metadata of one capture")
    async def capture_metadata(capture_id: str) -> dict[str, Any]:
        return await run(get_service().capture_metadata, capture_id)  # type: ignore[no-any-return]

    @res(
        "scope://captures/{capture_id}/samples",
        "capture-samples",
        "Min/max-decimated view (<=1000 points) of one capture",
    )
    async def capture_samples(capture_id: str) -> dict[str, Any]:
        return await run(get_service().get_samples, capture_id, 1000, "minmax")  # type: ignore[no-any-return]

    @res("scope://diagnostics/errors", "recent-errors", "Most recent structured errors")
    async def errors() -> dict[str, Any]:
        return await run(get_service().recent_errors, 50)  # type: ignore[no-any-return]

    @res(
        "scope://docs/measurement-guide",
        "measurement-guide",
        "How to obtain trustworthy measurements",
        mime="text/markdown",
    )
    def guide() -> str:
        return MEASUREMENT_GUIDE

    return uris
