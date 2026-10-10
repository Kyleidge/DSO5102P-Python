"""Shared fixtures. Every fixture uses the simulator and a temporary data directory."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

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
