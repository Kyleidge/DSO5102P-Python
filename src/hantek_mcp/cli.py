"""Command line interface: ``hantek-mcp <command>``.

stdout is reserved for MCP protocol traffic in ``serve --transport stdio`` and
``bridge``; every other command prints JSON results to stdout and logs to stderr.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Any

from hantek_mcp import __version__
from hantek_mcp.config import AppConfig, default_config_path, load_config
from hantek_mcp.errors import HantekError

log = logging.getLogger("hantek_mcp.cli")


def _config(args: argparse.Namespace) -> AppConfig:
    overrides: dict[str, Any] = {}
    if getattr(args, "simulate", False):
        overrides["backend"] = "simulator"
    elif getattr(args, "backend", None):
        overrides["backend"] = args.backend
    server: dict[str, Any] = {}
    for key in ("host", "port", "auth_token"):
        v = getattr(args, key, None)
        if v is not None:
            server[{"host": "http_host", "port": "http_port"}.get(key, key)] = v
    if getattr(args, "allow_remote", False):
        server["allow_remote"] = True
    if server:
        overrides["server"] = server
    if getattr(args, "data_dir", None):
        overrides["storage"] = {"data_dir": args.data_dir}
    if getattr(args, "log_level", None):
        overrides["log_level"] = args.log_level
    return load_config(args.config, overrides)


def _print(obj: Any) -> None:
    print(json.dumps(obj, indent=2, default=str))


def cmd_serve(args: argparse.Namespace) -> int:
    from hantek_mcp.mcp.server import configure_logging, run_http, run_stdio
    from hantek_mcp.service import ScopeService

    cfg = _config(args)
    configure_logging(cfg)
    svc = ScopeService(cfg)
    log.info("hantek-mcp %s: backend=%s transport=%s", __version__, cfg.backend, args.transport)
    if args.transport == "stdio":
        run_stdio(svc, auto_connect=not args.no_connect)
    else:
        run_http(svc, auto_connect=not args.no_connect)
    return 0


def cmd_bridge(args: argparse.Namespace) -> int:
    import anyio

    from hantek_mcp.mcp.bridge import run_bridge

    logging.basicConfig(stream=sys.stderr, level=logging.INFO)
    anyio.run(run_bridge, args.url, args.auth_token)
    return 0


def cmd_gui(args: argparse.Namespace) -> int:
    from hantek_mcp.gui.app import run_gui

    cfg = _config(args)
    return run_gui(cfg, mcp_http=args.mcp_http, connect=not args.no_connect)


def _with_service(args: argparse.Namespace) -> Any:
    from hantek_mcp.mcp.server import configure_logging
    from hantek_mcp.service import ScopeService

    cfg = _config(args)
    configure_logging(cfg)
    svc = ScopeService(cfg)
    svc.connect()
    return svc


def cmd_info(args: argparse.Namespace) -> int:
    svc = _with_service(args)
    try:
        _print({"identity": svc.identify(), "settings": svc.settings()})
    finally:
        svc.disconnect()
    return 0


def cmd_capture(args: argparse.Namespace) -> int:
    svc = _with_service(args)
    try:
        res = svc.capture(args.channel, args.mode, args.timeout, args.force_on_timeout, args.label)
        cid = res["capture"]["capture_id"]
        out: dict[str, Any] = {"capture": res["capture"]}
        if args.csv:
            out["csv"] = svc.export(cid, "csv", args.csv, overwrite=True)["path"]
        if args.npz:
            out["npz"] = svc.export(cid, "npz", args.npz, overwrite=True)["path"]
        for kind in args.measure or []:
            out.setdefault("measurements", {})[kind] = svc.measure(kind, cid)
        _print(out)
    finally:
        svc.disconnect()
    return 0


def cmd_selftest(args: argparse.Namespace) -> int:
    svc = _with_service(args)
    try:
        res = svc.self_test(args.channel, args.expected_frequency, args.expected_vpp)
        _print(res)
        return 0 if res.get("pass") else 2
    finally:
        svc.disconnect()


def cmd_diagnose(args: argparse.Namespace) -> int:
    from hantek_mcp.transport.usb import usb_diagnostics

    cfg = _config(args)
    _print(
        {
            "version": __version__,
            "python": sys.version,
            "config_file_default": str(default_config_path()),
            "data_dir": str(cfg.storage.data_dir),
            "usb": usb_diagnostics(cfg.usb.vendor_id, cfg.usb.product_id),
        }
    )
    return 0


def cmd_config(args: argparse.Namespace) -> int:
    cfg = _config(args)
    d = cfg.model_dump(mode="json")
    if d["server"].get("auth_token"):
        d["server"]["auth_token"] = "***"
    _print({"config_file_default": str(default_config_path()), "effective": d})
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="hantek-mcp", description="Hantek DSO5102P instrument control and MCP server"
    )
    p.add_argument("--version", action="version", version=f"hantek-mcp {__version__}")
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--config", type=Path, help="TOML config file (default: %(default)s)", default=None)
    g = common.add_mutually_exclusive_group()
    g.add_argument(
        "--simulate", action="store_true", help="use the built-in simulator instead of USB hardware"
    )
    g.add_argument(
        "--backend", choices=["usb", "simulator"], help="instrument backend (default from config: usb)"
    )
    common.add_argument("--data-dir", type=Path, help="capture/export directory")
    common.add_argument("--log-level", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("serve", parents=[common], help="run the MCP server")
    s.add_argument("--transport", choices=["stdio", "http"], default="stdio")
    s.add_argument("--host", help="HTTP bind address (default 127.0.0.1)")
    s.add_argument("--port", type=int, help="HTTP port (default 8765)")
    s.add_argument(
        "--allow-remote", action="store_true", help="permit a non-loopback bind (requires --auth-token)"
    )
    s.add_argument("--auth-token", help="bearer token required on every HTTP request")
    s.add_argument("--no-connect", action="store_true", help="do not connect to the instrument at start-up")
    s.set_defaults(func=cmd_serve)

    b = sub.add_parser("bridge", help="stdio MCP server forwarding to an HTTP MCP endpoint (e.g. the GUI's)")
    b.add_argument("--url", default="http://127.0.0.1:8765/mcp")
    b.add_argument("--auth-token")
    b.set_defaults(func=cmd_bridge)

    gu = sub.add_parser("gui", parents=[common], help="launch the desktop GUI")
    gu.add_argument("--mcp-http", action="store_true", help="also host the MCP server over HTTP on localhost")
    gu.add_argument("--port", type=int, help="MCP HTTP port when --mcp-http is used")
    gu.add_argument("--auth-token", help="bearer token for the GUI-hosted MCP endpoint")
    gu.add_argument("--no-connect", action="store_true")
    gu.set_defaults(func=cmd_gui)

    i = sub.add_parser("info", parents=[common], help="connect and print identity and settings")
    i.set_defaults(func=cmd_info)

    c = sub.add_parser("capture", parents=[common], help="capture one channel and optionally export/measure")
    c.add_argument("--channel", type=int, choices=[1, 2], default=1)
    c.add_argument("--mode", choices=["single", "stop_and_read", "running"], default="single")
    c.add_argument("--timeout", type=float, default=5.0)
    c.add_argument("--force-on-timeout", action="store_true")
    c.add_argument("--label")
    c.add_argument("--csv", metavar="NAME", help="export as CSV into the export directory")
    c.add_argument("--npz", metavar="NAME", help="export as NPZ into the export directory")
    c.add_argument(
        "--measure",
        nargs="*",
        choices=[
            "voltage",
            "frequency",
            "period",
            "duty_cycle",
            "rise_time",
            "fall_time",
            "rms",
            "peak_to_peak",
            "noise",
        ],
    )
    c.set_defaults(func=cmd_capture)

    t = sub.add_parser("selftest", parents=[common], help="probe-compensation acquisition self-test")
    t.add_argument("--channel", type=int, choices=[1, 2], default=1)
    t.add_argument("--expected-frequency", type=float, default=1000.0)
    t.add_argument("--expected-vpp", type=float, default=5.0)
    t.set_defaults(func=cmd_selftest)

    d = sub.add_parser(
        "diagnose", parents=[common], help="read-only USB/backend diagnostics (no device traffic)"
    )
    d.set_defaults(func=cmd_diagnose)

    cf = sub.add_parser("config", parents=[common], help="print the effective configuration")
    cf.set_defaults(func=cmd_config)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args))
    except HantekError as e:
        print(json.dumps({"error": e.to_dict()}, indent=2), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
