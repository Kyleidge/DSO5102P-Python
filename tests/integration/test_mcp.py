"""MCP server end-to-end: in-process client, real stdio subprocess, HTTP and the stdio->HTTP bridge."""

from __future__ import annotations

import json
import socket
import sys
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import anyio
import httpx2
import pytest
from mcp import Client
from mcp.client.stdio import StdioServerParameters
from mcp.client.streamable_http import streamable_http_client
from mcp.shared._httpx_utils import create_mcp_http_client

from hantek_mcp.config import AppConfig
from hantek_mcp.errors import ParameterError
from hantek_mcp.mcp.server import build_server, check_http_security, http_app, transport_security
from hantek_mcp.service import ScopeService
from tests.conftest import sim_config

REQUIRED_TOOLS = {
    "scope_connect",
    "scope_disconnect",
    "scope_status",
    "scope_identify",
    "scope_get_capabilities",
    "scope_get_settings",
    "scope_get_health",
    "scope_get_channel",
    "scope_enable_channel",
    "scope_set_vertical_scale",
    "scope_set_vertical_offset",
    "scope_set_coupling",
    "scope_set_probe_attenuation",
    "scope_set_timebase",
    "scope_start_acquisition",
    "scope_stop_acquisition",
    "scope_single_acquisition",
    "scope_wait_for_trigger",
    "scope_capture",
    "scope_capture_both_channels",
    "scope_get_acquisition_state",
    "scope_get_trigger",
    "scope_set_trigger_source",
    "scope_set_trigger_mode",
    "scope_set_trigger_slope",
    "scope_set_trigger_level",
    "scope_force_trigger",
    "scope_measure_voltage",
    "scope_measure_frequency",
    "scope_measure_period",
    "scope_measure_duty_cycle",
    "scope_measure_rise_time",
    "scope_measure_fall_time",
    "scope_measure_rms",
    "scope_measure_peak_to_peak",
    "scope_measure_phase",
    "scope_measure_channel_delay",
    "scope_fft",
    "scope_spectrum_peaks",
    "scope_measure_noise",
    "scope_cross_correlation",
    "scope_compare_captures",
    "scope_detect_edges",
    "scope_detect_pulses",
    "scope_list_captures",
    "scope_get_capture_metadata",
    "scope_get_samples",
    "scope_get_sample_window",
    "scope_export_csv",
    "scope_export_npz",
    "scope_delete_capture",
    "scope_capture_screenshot",
    "scope_autoset",
    "scope_find_signal",
    "scope_measure_repeatedly",
    "scope_acquire_until",
    "scope_run_measurement_plan",
    "scope_abort",
    "scope_get_usb_diagnostics",
    "scope_get_protocol_diagnostics",
    "scope_run_self_test",
    "scope_get_recent_errors",
}
FORBIDDEN_WORDS = ("shell", "firmware", "flash", "bootloader", "raw_command", "write_file", "exec")


def _err(result: Any) -> dict[str, Any]:
    assert result.is_error
    text = result.content[0].text
    return json.loads(text[text.index("{") :])["error"]  # type: ignore[no-any-return]


# ------------------------------------------------------------------------------------------ in-process


@pytest.fixture
def svc(tmp_path: Path) -> Iterator[ScopeService]:
    s = ScopeService(sim_config(tmp_path))
    yield s
    if s.controller.is_connected:
        s.disconnect()


def run(coro_fn: Any) -> Any:
    return anyio.run(coro_fn)


def test_tool_discovery_and_schemas(svc: ScopeService) -> None:
    async def main() -> None:
        async with Client(build_server(svc, auto_connect=True)) as c:
            tools = {t.name: t for t in (await c.list_tools()).tools}
            assert set(tools) >= REQUIRED_TOOLS
            for name, t in tools.items():
                assert t.description and len(t.description) > 20, name
                assert t.input_schema["type"] == "object"
                assert not any(w in name for w in FORBIDDEN_WORDS), name
            props = tools["scope_capture"].input_schema["properties"]
            assert props["channel"]["minimum"] == 1 and props["channel"]["maximum"] == 2
            assert set(props["mode"]["enum"]) == {"single", "stop_and_read", "running"}
            assert tools[
                "scope_get_status" if "scope_get_status" in tools else "scope_status"
            ].annotations.read_only_hint

    run(main)


def test_tool_calls_results_and_structured_errors(svc: ScopeService) -> None:
    async def main() -> None:
        async with Client(build_server(svc, auto_connect=True)) as c:
            r = await c.call_tool("scope_capture", {"channel": 1})
            cap = r.structured_content["capture"]
            assert cap["simulated"] and cap["freshness"] == "new_trigger_confirmed"
            r = await c.call_tool("scope_measure_frequency", {"capture_id": cap["capture_id"]})
            assert r.structured_content["results"]["frequency_hz"] == pytest.approx(1000, abs=0.5)
            r = await c.call_tool("scope_set_vertical_scale", {"channel": 1, "volts_per_div": 3})
            e = _err(r)
            assert e["code"] == "INVALID_PARAMETER" and 2.0 in e["details"]["allowed_volts_per_div"]
            r = await c.call_tool("scope_capture", {"channel": 3})  # schema validation
            assert r.is_error
            r = await c.call_tool("scope_get_capture_metadata", {"capture_id": "../../etc/passwd"})
            assert _err(r)["code"] == "INVALID_PARAMETER"
            r = await c.call_tool("scope_export_csv", {"capture_id": cap["capture_id"], "name": "../x"})
            assert _err(r)["code"] == "PATH_NOT_ALLOWED"
            r = await c.call_tool("scope_get_samples", {"capture_id": cap["capture_id"], "max_points": 50})
            assert r.structured_content["returned_points"] <= 50
            r = await c.call_tool("scope_capture_screenshot", {})
            assert [x.type for x in r.content] == ["image", "text"]
            r = await c.call_tool("scope_default_setup", {"confirm": True})
            assert _err(r)["code"] == "UNSUPPORTED"
            r = await c.call_tool(
                "scope_run_measurement_plan",
                {
                    "steps": [
                        {"action": "capture", "args": {"channel": 2}},
                        {"action": "measure", "args": {"kind": "voltage", "capture_id": "$last"}},
                    ]
                },
            )
            assert r.structured_content["all_ok"]
            r = await c.call_tool(
                "scope_set_simulated_signal",
                {"channel": 1, "waveform": "pwm", "duty_cycle": 0.2, "amplitude_v": 1.5, "offset_v": 1.5},
            )
            assert not r.is_error
            r = await c.call_tool("scope_measure_duty_cycle", {"channel": 1})
            assert r.structured_content["results"]["duty_cycle_percent"] == pytest.approx(20, abs=0.5)

    run(main)


def test_not_connected_errors_are_explicit(svc: ScopeService) -> None:
    async def main() -> None:
        async with Client(build_server(svc, auto_connect=False)) as c:
            r = await c.call_tool("scope_capture", {"channel": 1})
            assert _err(r)["code"] == "NOT_CONNECTED"
            r = await c.call_tool("scope_status", {})
            assert r.structured_content["connected"] is False
            r = await c.call_tool("scope_connect", {})
            assert r.structured_content["identity"]["simulated"]

    run(main)


def test_resources_and_prompts(svc: ScopeService) -> None:
    async def main() -> None:
        async with Client(build_server(svc, auto_connect=True)) as c:
            uris = {str(r.uri) for r in (await c.list_resources()).resources}
            assert {
                "scope://device/status",
                "scope://device/capabilities",
                "scope://device/settings",
                "scope://captures",
                "scope://docs/measurement-guide",
            } <= uris
            templates = {t.uri_template for t in (await c.list_resource_templates()).resource_templates}
            assert "scope://captures/{capture_id}/metadata" in templates
            st = json.loads((await c.read_resource("scope://device/status")).contents[0].text)
            assert st["simulated"] is True
            cid = (await c.call_tool("scope_capture", {"channel": 1})).structured_content["capture"][
                "capture_id"
            ]
            meta = json.loads((await c.read_resource(f"scope://captures/{cid}/metadata")).contents[0].text)
            assert meta["capture_id"] == cid and meta["simulated"]
            lst = json.loads((await c.read_resource("scope://captures")).contents[0].text)
            assert lst["captures"][0]["capture_id"] == cid
            with pytest.raises(Exception):  # noqa: B017 - unknown capture -> protocol error
                await c.read_resource("scope://captures/does-not-exist/metadata")
            prompts = {p.name for p in (await c.list_prompts()).prompts}
            assert prompts == {
                "characterize_signal",
                "clock_analysis",
                "pwm_analysis",
                "noise_investigation",
                "trigger_setup",
                "two_channel_comparison",
                "intermittent_signal",
            }
            for name in prompts:
                text = (await c.get_prompt(name, {})).messages[0].content.text
                assert "Never claim" in text and "simulated" in text

    run(main)


def test_abort_is_served_while_a_capture_waits(svc: ScopeService) -> None:
    async def main() -> None:
        async with Client(build_server(svc, auto_connect=True)) as c:
            await c.call_tool("scope_set_trigger_mode", {"mode": "normal"})
            await c.call_tool("scope_set_simulated_signal", {"channel": 1, "waveform": "dc"})
            results: dict[str, Any] = {}

            async def long_capture() -> None:
                results["cap"] = await c.call_tool("scope_capture", {"channel": 1, "timeout_s": 20})

            t0 = time.monotonic()
            async with anyio.create_task_group() as tg:
                tg.start_soon(long_capture)
                await anyio.sleep(0.3)
                st = await c.call_tool("scope_status", {})  # served concurrently
                assert st.structured_content["busy_operation"] == "capture_single"
                ab = await c.call_tool("scope_abort", {})
                assert ab.structured_content["abort_requested"]
            assert _err(results["cap"])["code"] == "ABORTED"
            assert time.monotonic() - t0 < 5

    run(main)


# ------------------------------------------------------------------------------------------ stdio subprocess


def test_stdio_server_end_to_end(tmp_path: Path) -> None:
    async def main() -> None:
        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "hantek_mcp", "serve", "--simulate", "--data-dir", str(tmp_path / "d")],
        )
        async with Client(params) as c:
            names = {t.name for t in (await c.list_tools()).tools}
            assert names >= REQUIRED_TOOLS
            r = await c.call_tool("scope_capture_both_channels", {})
            caps = r.structured_content["captures"]
            assert len(caps) == 2 and caps[0]["group_id"] == caps[1]["group_id"]
            r = await c.call_tool(
                "scope_measure_phase",
                {"capture_id_a": caps[0]["capture_id"], "capture_id_b": caps[1]["capture_id"]},
            )
            assert r.structured_content["valid"]
            assert r.structured_content["results"]["phase_deg"] == pytest.approx(-90, abs=1.5)
            r = await c.call_tool("scope_export_npz", {"capture_id": caps[0]["capture_id"]})
            assert Path(r.structured_content["path"]).is_file()

    run(main)


# ------------------------------------------------------------------------------------------ HTTP + bridge


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


class HttpHost:
    def __init__(self, svc: ScopeService) -> None:
        import uvicorn

        self.svc = svc
        app = build_server(svc, auto_connect=False, owns_service=False)
        cfg = uvicorn.Config(
            http_app(app, svc.config),
            host="127.0.0.1",
            port=svc.config.server.http_port,
            log_config=None,
            log_level="warning",
        )
        self.server = uvicorn.Server(cfg)
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.svc.config.server.http_port}/mcp"

    def __enter__(self) -> HttpHost:
        self.thread.start()
        for _ in range(200):
            if self.server.started:
                return self
            time.sleep(0.02)
        raise RuntimeError("server did not start")

    def __exit__(self, *exc: object) -> None:
        self.server.should_exit = True
        self.thread.join(5)


def _http_service(tmp_path: Path, token: str | None = None) -> ScopeService:
    cfg = sim_config(tmp_path, server={"http_port": _free_port(), "auth_token": token})
    s = ScopeService(cfg)
    s.connect()
    return s


def test_http_requires_token_when_configured(tmp_path: Path) -> None:
    token = "t0ken-for-tests-0123456789"
    svc = _http_service(tmp_path, token)
    with HttpHost(svc) as host:
        r = httpx2.post(host.url, json={"jsonrpc": "2.0", "id": 1, "method": "ping"})
        assert r.status_code == 401
        r = httpx2.post(host.url, json={}, headers={"Authorization": "Bearer wrong"})
        assert r.status_code == 401

        async def main() -> None:
            http = create_mcp_http_client(headers={"Authorization": f"Bearer {token}"})
            async with http, Client(streamable_http_client(host.url, http_client=http)) as c:
                r = await c.call_tool("scope_capture", {"channel": 2})
                assert r.structured_content["capture"]["channel"] == 2

        anyio.run(main)
    svc.disconnect()


def test_http_rejects_foreign_host_header(tmp_path: Path) -> None:
    svc = _http_service(tmp_path)
    with HttpHost(svc) as host:
        r = httpx2.post(
            host.url,
            json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
            headers={"Host": "evil.example.com", "Accept": "application/json, text/event-stream"},
        )
        assert r.status_code in (400, 403, 421)
        r = httpx2.post(
            host.url,
            json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
            headers={"Origin": "http://evil.example.com", "Accept": "application/json, text/event-stream"},
        )
        assert r.status_code in (400, 403, 421)
    svc.disconnect()


def test_remote_bind_policy() -> None:
    cfg = AppConfig.model_validate({"server": {"http_host": "0.0.0.0"}})
    with pytest.raises(ParameterError):
        check_http_security(cfg)
    cfg = AppConfig.model_validate({"server": {"http_host": "0.0.0.0", "allow_remote": True}})
    with pytest.raises(ParameterError):
        check_http_security(cfg)
    cfg = AppConfig.model_validate(
        {"server": {"http_host": "0.0.0.0", "allow_remote": True, "auth_token": "x" * 32}}
    )
    with pytest.raises(ParameterError):  # wildcard binds need explicit Host allow-list entries
        check_http_security(cfg)
    cfg = AppConfig.model_validate(
        {"server": {"http_host": "0.0.0.0", "allow_remote": True, "auth_token": "x" * 32,
                    "allowed_hosts": ["192.168.1.5:8765"]}}
    )  # fmt: skip
    check_http_security(cfg)
    assert "192.168.1.5:8765" in transport_security(cfg).allowed_hosts
    v6 = AppConfig.model_validate(
        {"server": {"http_host": "fe80::1", "allow_remote": True, "auth_token": "x" * 32}}
    )
    check_http_security(v6)
    assert "[fe80::1]:8765" in transport_security(v6).allowed_hosts
    check_http_security(AppConfig())


def test_bridge_shares_one_instrument_owner(tmp_path: Path) -> None:
    """A stdio client reaches the GUI-hosted endpoint through the bridge; captures land in the host's store."""
    svc = _http_service(tmp_path)
    with HttpHost(svc) as host:

        async def main() -> None:
            params = StdioServerParameters(
                command=sys.executable, args=["-m", "hantek_mcp", "bridge", "--url", host.url]
            )
            async with Client(params) as c:
                names = {t.name for t in (await c.list_tools()).tools}
                assert names >= REQUIRED_TOOLS
                r = await c.call_tool("scope_capture", {"channel": 1})
                cid = r.structured_content["capture"]["capture_id"]
                assert cid in svc.store  # same service, same USB owner
                res = await c.read_resource("scope://device/status")
                assert json.loads(res.contents[0].text)["connected"]
                p = await c.get_prompt("pwm_analysis", {"channel": "2"})
                assert "CH2" in p.messages[0].content.text

        anyio.run(main)
    svc.disconnect()
