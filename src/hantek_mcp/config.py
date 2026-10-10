"""Configuration: defaults < TOML file < environment variables < CLI flags.

Environment variables use the ``HANTEK_MCP_`` prefix and ``__`` for nesting,
e.g. ``HANTEK_MCP_BACKEND=simulator`` or ``HANTEK_MCP_STORAGE__MAX_CAPTURES=50``.
"""

from __future__ import annotations

import os
import sys
import tomllib
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

ENV_PREFIX = "HANTEK_MCP_"


def default_data_dir() -> Path:
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    else:
        base = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    return base / "hantek-mcp"


def default_config_path() -> Path:
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return base / "hantek-mcp" / "config.toml"


class _Section(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)


class UsbConfig(_Section):
    vendor_id: int = Field(0x049F, ge=0, le=0xFFFF)
    product_id: int = Field(0x505A, ge=0, le=0xFFFF)
    bus: int | None = None
    address: int | None = None
    index: int = Field(0, ge=0, description="Which matching device to use when several are attached")
    interface: int = Field(0, ge=0)
    endpoint_out: int | None = Field(None, description="Override bulk OUT endpoint (default: discover, expect 0x02)")
    endpoint_in: int | None = Field(None, description="Override bulk IN endpoint (default: discover, expect 0x81)")
    command_delay_s: float = Field(0.02, ge=0, le=1)
    key_press_delay_s: float = Field(0.2, ge=0, le=2, description="Settle time after each emulated key press")
    timeout_s: float = Field(2.0, gt=0, le=60)
    sample_timeout_s: float = Field(10.0, gt=0, le=120)
    screenshot_timeout_s: float = Field(20.0, gt=0, le=120)
    max_retries: int = Field(2, ge=0, le=5)


class ScalingConfig(_Section):
    counts_per_div: float = Field(25.0, gt=0, le=200)
    samples_per_div_override: float | None = Field(None, gt=0)
    voltage_verified: bool = Field(
        False, description="Set only after the hardware validation checklist confirmed voltage scaling"
    )
    time_verified: bool = Field(
        False, description="Set only after the hardware validation checklist confirmed time scaling"
    )


class StorageConfig(_Section):
    data_dir: Path = Field(default_factory=default_data_dir)
    max_captures: int = Field(500, ge=1, le=100_000)
    max_disk_mb: float = Field(1024.0, gt=0)
    persist_captures: bool = True


class LimitsConfig(_Section):
    max_response_samples: int = Field(5000, ge=10, le=200_000)
    max_repeat_count: int = Field(100, ge=1, le=10_000)
    max_workflow_duration_s: float = Field(600.0, gt=0, le=24 * 3600)
    max_plan_steps: int = Field(50, ge=1, le=1000)
    max_list_items: int = Field(500, ge=1, le=100_000)
    max_trigger_wait_s: float = Field(60.0, gt=0, le=3600)
    max_key_presses_per_operation: int = Field(120, ge=1, le=2000)


class ServerConfig(_Section):
    http_host: str = "127.0.0.1"
    http_port: int = Field(8765, ge=1, le=65535)
    http_path: str = "/mcp"
    allow_remote: bool = Field(False, description="Required (with auth_token) to bind a non-loopback address")
    auth_token: str | None = Field(None, description="Bearer token required on every HTTP request when set")
    allowed_origins: list[str] = Field(default_factory=list)


class ControlConfig(_Section):
    settings_write_mode: Literal["keypress", "direct_write_experimental"] = "keypress"
    lock_panel_during_capture: bool = True
    allow_disruptive_operations: bool = Field(
        False, description="Permit factory 'Default Setup' via MCP (still requires explicit confirmation)"
    )


class SimulatorConfig(_Section):
    seed: int | None = None


class AppConfig(_Section):
    backend: Literal["usb", "simulator"] = "usb"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    log_file: Path | None = None
    usb: UsbConfig = Field(default_factory=UsbConfig)
    scaling: ScalingConfig = Field(default_factory=ScalingConfig)
    storage: StorageConfig = Field(default_factory=StorageConfig)
    limits: LimitsConfig = Field(default_factory=LimitsConfig)
    server: ServerConfig = Field(default_factory=ServerConfig)
    control: ControlConfig = Field(default_factory=ControlConfig)
    simulator: SimulatorConfig = Field(default_factory=SimulatorConfig)

    @field_validator("backend", mode="before")
    @classmethod
    def _norm_backend(cls, v: Any) -> Any:
        if isinstance(v, str) and v.lower() in ("sim", "simulate", "simulation"):
            return "simulator"
        return v


def _env_overrides(environ: dict[str, str]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in environ.items():
        if not key.startswith(ENV_PREFIX) or key == ENV_PREFIX + "CONFIG":
            continue
        path = key[len(ENV_PREFIX) :].lower().split("__")
        node = out
        for part in path[:-1]:
            node = node.setdefault(part, {})
        node[path[-1]] = value
    return out


def _merge(base: dict[str, Any], over: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config(
    path: Path | str | None = None,
    overrides: dict[str, Any] | None = None,
    environ: dict[str, str] | None = None,
) -> AppConfig:
    env = dict(os.environ if environ is None else environ)
    data: dict[str, Any] = {}
    cfg_path = Path(path) if path else (Path(env[ENV_PREFIX + "CONFIG"]) if env.get(ENV_PREFIX + "CONFIG") else None)
    if cfg_path is None and default_config_path().is_file():
        cfg_path = default_config_path()
    if cfg_path is not None:
        with cfg_path.open("rb") as fh:
            data = tomllib.load(fh)
    data = _merge(data, _env_overrides(env))
    if overrides:
        data = _merge(data, overrides)
    return AppConfig.model_validate(data)
