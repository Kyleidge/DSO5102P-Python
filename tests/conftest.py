"""Shared fixtures. Every fixture uses the simulator and a temporary data directory."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from hantek_mcp.config import AppConfig, load_config
from hantek_mcp.instrument.controller import ScopeController
from hantek_mcp.service import ScopeService
from hantek_mcp.transport.simulator import SimulatedTransport

FIXTURES = Path(__file__).parent / "fixtures"


def sim_config(tmp_path: Path, **overrides: object) -> AppConfig:
    base: dict[str, object] = {
        "backend": "simulator",
        "storage": {"data_dir": str(tmp_path / "data")},
        "simulator": {"seed": 1234},
    }
    base.update(overrides)
    return load_config(overrides=base, environ={})


@pytest.fixture
def config(tmp_path: Path) -> AppConfig:
    return sim_config(tmp_path)


@pytest.fixture
def controller(config: AppConfig) -> Iterator[ScopeController]:
    c = ScopeController(config)
    c.connect()
    yield c
    c.disconnect()


@pytest.fixture
def sim(controller: ScopeController) -> SimulatedTransport:
    assert isinstance(controller.transport, SimulatedTransport)
    return controller.transport


@pytest.fixture
def service(config: AppConfig) -> Iterator[ScopeService]:
    s = ScopeService(config)
    s.connect()
    yield s
    s.disconnect()


# ----------------------------------------------------------------------------- mocked PyUSB


class FakeEndpoint:
    def __init__(self, addr: int, mps: int = 512, attrs: int = 2) -> None:
        self.bEndpointAddress = addr
        self.wMaxPacketSize = mps
        self.bmAttributes = attrs


class FakeInterface(list[FakeEndpoint]):
    bInterfaceNumber = 0
    bAlternateSetting = 0
    bInterfaceClass = 0xFF


class FakeConfig(dict[tuple[int, int], FakeInterface]):
    bConfigurationValue = 1

    def __iter__(self):  # type: ignore[override]
        return iter(self.values())


class FakeDevice:
    bus, address, bcdUSB, bcdDevice, bDeviceClass, iSerialNumber, speed = 1, 7, 0x200, 0x100, 0, 0, 3

    def __init__(self, eps: list[FakeEndpoint]) -> None:
        intf = FakeInterface(eps)
        self.cfg = FakeConfig({(0, 0): intf})
        self.kernel_active = True
        self.detached = False
        self.read_error: Exception | None = None
        self.writes: list[bytes] = []

    def __iter__(self):  # type: ignore[no-untyped-def]
        return iter([self.cfg])

    def get_active_configuration(self) -> FakeConfig:
        return self.cfg

    def is_kernel_driver_active(self, i: int) -> bool:
        return self.kernel_active

    def detach_kernel_driver(self, i: int) -> None:
        self.detached = True

    def attach_kernel_driver(self, i: int) -> None:
        self.detached = False

    def write(self, ep: Any, data: bytes, timeout: int) -> int:
        self.writes.append(bytes(data))
        return len(data)

    def read(self, ep: Any, size: int, timeout: int) -> bytes:
        if self.read_error:
            raise self.read_error
        return b"\x53\x02\x00\x80\xd5"


@pytest.fixture
def fake_usb(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    import usb.core
    import usb.util

    from hantek_mcp.transport import usb as usbmod

    state: dict[str, Any] = {"devices": [FakeDevice([FakeEndpoint(0x02), FakeEndpoint(0x81)])]}
    monkeypatch.setattr(usbmod, "load_backend", lambda: (object(), "fake backend"))
    monkeypatch.setattr(usb.core, "find", lambda **kw: iter(state["devices"]))
    monkeypatch.setattr(usb.util, "claim_interface", lambda d, i: None)
    monkeypatch.setattr(usb.util, "release_interface", lambda d, i: None)
    monkeypatch.setattr(usb.util, "dispose_resources", lambda d: None)
    monkeypatch.setattr(usbmod.sys, "platform", "linux")
    return state
