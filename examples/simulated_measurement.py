"""Complete MCP client example: characterise a (simulated) signal through the MCP server.

Runs the server as a subprocess over stdio exactly like a desktop MCP client would,
then performs a verification-first measurement loop.

    python examples/simulated_measurement.py            # simulator
    python examples/simulated_measurement.py --hardware  # a real DSO5102P (USB)
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

import anyio
from mcp import Client
from mcp.client.stdio import StdioServerParameters


async def call(c: Client, tool: str, **args: Any) -> dict[str, Any]:
    r = await c.call_tool(tool, args)
    if r.is_error:
        raise RuntimeError(f"{tool} failed: {r.content[0].text}")  # type: ignore[union-attr]
    return r.structured_content or {}


async def main(hardware: bool) -> None:
    server_args = ["-m", "hantek_mcp", "serve"] + ([] if hardware else ["--simulate"])
    async with Client(StdioServerParameters(command=sys.executable, args=server_args)) as c:
        tools = [t.name for t in (await c.list_tools()).tools]
        print(f"{len(tools)} tools available")

        status = await call(c, "scope_status")
        print(f"backend={status['backend']} simulated={status['simulated']} connected={status['connected']}")

        # 1. bring the CH1 signal on screen (software-assisted, every change read back)
        auto = await call(c, "scope_autoset", channel=1)
        for ch in auto["changes"]:
            print(f"  set {ch['setting']} -> {ch['confirmed_value']} ({ch['verification']})")

        # 2. acquire a record that is proven to be new
        cap = (await call(c, "scope_capture", channel=1, mode="single"))["capture"]
        print(
            f"capture {cap['capture_id']}: {cap['sample_count']} samples @ {cap['sample_rate_hz']:.4g} S/s, "
            f"freshness={cap['freshness']}, clipped={cap['quality']['clipped']}"
        )

        # 3. measure on the stored record
        report: dict[str, Any] = {}
        for kind in ("frequency", "voltage", "rms", "duty_cycle"):
            m = await call(c, f"scope_measure_{kind}", capture_id=cap["capture_id"])
            report[kind] = {"valid": m["valid"], "results": m["results"], "warnings": m["warnings"]}
        f = report["frequency"]["results"]
        v = report["voltage"]["results"]
        print(f"frequency = {f['frequency_hz']:.3f} Hz (±{f['relative_uncertainty_est'] * 100:.3f} %)")
        print(f"Vpp = {v['peak_to_peak']:.3f} {v['unit']}, top-base = {v['amplitude']:.3f} {v['unit']}")
        print(
            f"RMS = {report['rms']['results']['rms']:.3f} V, duty = "
            f"{report['duty_cycle']['results']['duty_cycle_percent']:.2f} %"
        )
        print(f"voltage scaling: {cap['voltage_scaling_status']}, time scaling: {cap['time_scaling_status']}")

        # 4. export the complete record
        exp = await call(c, "scope_export_csv", capture_id=cap["capture_id"], name="example", overwrite=True)
        print(f"exported {exp['path']}")
        print(json.dumps({"capture_id": cap["capture_id"], "simulated": cap["simulated"]}, indent=2))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--hardware", action="store_true", help="use the real instrument instead of the simulator"
    )
    anyio.run(main, ap.parse_args().hardware)
