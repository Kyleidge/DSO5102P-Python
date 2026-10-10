"""Storage, configuration, ownership lock, protocol session and USB transport (mocked PyUSB)."""

from __future__ import annotations

import errno
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from hantek_mcp.config import load_config
from hantek_mcp.errors import (
    CaptureNotFoundError,
    ChecksumError,
    DeviceBusyError,
    DeviceDisconnectedError,
    DeviceNotFoundError,
    ParameterError,
    PathNotAllowedError,
    ScopeTimeoutError,
    TransportError,
)
from hantek_mcp.instrument.ownership import OwnershipLock
from hantek_mcp.models import AcquisitionInfo, DataSource, Freshness, WaveformCapture, utcnow
from hantek_mcp.protocol.session import ProtocolSession, SessionTimeouts
from hantek_mcp.storage.captures import CaptureStore, load_capture_file
from hantek_mcp.transport.simulator import SimulatedTransport

# ----------------------------------------------------------------------------- storage


def make_capture(cid: str = "cap-1", n: int = 1000, channel: int = 1) -> WaveformCapture:
    raw = (np.arange(n) % 200 - 100).astype(np.int8)
    return WaveformCapture(
        capture_id=cid,
        channel=channel,
        timestamp_utc=utcnow(),
        raw_samples=raw,
        time_s=np.arange(n) * 1e-6,
        voltage_v=raw * 0.04,
        source=DataSource.SIMULATOR,
        horizontal={"sample_interval_s": 1e-6, "status": "simulated"},
        vertical={"volts_per_count": 0.04, "status": "simulated"},
        channel_settings={"volts_per_div_at_probe_tip": 1.0},
        trigger_settings={},
        timebase_settings={},
        acquire_settings={},
        acquisition=AcquisitionInfo("single", Freshness.NEW_TRIGGER_CONFIRMED, "ready", "stop", True),
        quality={"clipped": False},
    )


def test_store_add_get_list_delete_and_reload(tmp_path: Path) -> None:
    st = CaptureStore(tmp_path)
    for i in range(3):
        st.add(make_capture(f"cap-{i}", channel=1 + i % 2))
    assert [c["capture_id"] for c in st.list()] == ["cap-2", "cap-1", "cap-0"]
    assert [c["capture_id"] for c in st.list(channel=2)] == ["cap-1"]
    got = st.get("cap-1")
    np.testing.assert_array_equal(got.raw_samples, make_capture().raw_samples)
    st.delete("cap-0")
    with pytest.raises(CaptureNotFoundError):
        st.get("cap-0")
    st2 = CaptureStore(tmp_path)  # index rebuilt from disk
    assert len(st2) == 2 and st2.get("cap-2").acquisition.freshness == Freshness.NEW_TRIGGER_CONFIRMED


def test_store_limits_evict_oldest(tmp_path: Path) -> None:
    st = CaptureStore(tmp_path, max_captures=3)
    for i in range(5):
        st.add(make_capture(f"c{i}"))
    assert len(st) == 3 and "c0" not in st and "c1" not in st
    assert st.evicted == ["c0", "c1"]
    st_small = CaptureStore(tmp_path / "b", max_disk_mb=0.002)  # ~2 kB
    for i in range(4):
        st_small.add(make_capture(f"d{i}", n=4000))
    assert len(st_small) == 1 and "d3" in st_small


def test_memory_only_store(tmp_path: Path) -> None:
    st = CaptureStore(tmp_path / "mem", persist=False)
    st.add(make_capture("m1"))
    assert st.get("m1").sample_count == 1000
    assert not (tmp_path / "mem").exists()


@pytest.mark.parametrize(
    "bad", ["../evil", "a/b", "a\\b", "/abs", "C:\\x", "CON", "", " ", "x" * 200, "..csv"]
)
def test_export_names_are_validated(tmp_path: Path, bad: str) -> None:
    st = CaptureStore(tmp_path)
    st.add(make_capture("c"))
    with pytest.raises(PathNotAllowedError):
        st.export_csv("c", bad)


@pytest.mark.parametrize("bad", ["../x", "a/b", "", "x" * 300])
def test_capture_ids_are_validated(tmp_path: Path, bad: str) -> None:
    with pytest.raises(ParameterError):
        CaptureStore(tmp_path).get(bad)


@pytest.mark.parametrize("fmt", ["csv", "npz"])
def test_export_and_reload_roundtrip(tmp_path: Path, fmt: str) -> None:
    st = CaptureStore(tmp_path)
    cap = make_capture("orig")
    st.add(cap)
    path = st.export_csv("orig", "my export") if fmt == "csv" else st.export_npz("orig", "my export")
    assert path.parent == st.export_dir and path.name == f"my export.{fmt}"
    with pytest.raises(PathNotAllowedError):  # no silent overwrite
        st.export_csv("orig", "my export") if fmt == "csv" else st.export_npz("orig", "my export")
    back = load_capture_file(path)
    np.testing.assert_array_equal(back.raw_samples, cap.raw_samples)
    np.testing.assert_allclose(back.time_s, cap.time_s)
    assert back.voltage_v is not None
    np.testing.assert_allclose(back.voltage_v, cap.voltage_v)  # type: ignore[arg-type]
    assert back.simulated and back.acquisition.freshness == Freshness.NEW_TRIGGER_CONFIRMED
    reloaded = st.import_export(path.name)
    assert reloaded.capture_id == "orig-reloaded"
    if fmt == "csv":
        text = path.read_text()
        assert "SIMULATED DATA" in text and text.startswith("# hantek-mcp waveform capture v1")


# ----------------------------------------------------------------------------- config


def test_config_env_toml_and_overrides(tmp_path: Path) -> None:
    toml = tmp_path / "c.toml"
    toml.write_text('backend = "sim"\n[limits]\nmax_repeat_count = 7\n[server]\nhttp_port = 9000\n')
    env = {"HANTEK_MCP_LIMITS__MAX_REPEAT_COUNT": "9", "HANTEK_MCP_USB__TIMEOUT_S": "3.5"}
    cfg = load_config(toml, overrides={"server": {"http_port": 9100}}, environ=env)
    assert cfg.backend == "simulator"
    assert cfg.limits.max_repeat_count == 9  # env beats file
    assert cfg.usb.timeout_s == 3.5
    assert cfg.server.http_port == 9100  # overrides beat env
    assert cfg.server.http_host == "127.0.0.1" and not cfg.server.allow_remote


def test_config_rejects_unknown_keys(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        load_config(overrides={"usb": {"no_such_key": 1}}, environ={})


# ----------------------------------------------------------------------------- ownership


def test_ownership_lock_is_exclusive(tmp_path: Path) -> None:
    a, b = OwnershipLock(tmp_path, "dev"), OwnershipLock(tmp_path, "dev")
    a.acquire("first owner")
    with pytest.raises(DeviceBusyError) as ei:
        b.acquire("second owner")
    assert ei.value.details["owner"]["description"] == "first owner"
    a.release()
    b.acquire("second owner")
    b.release()


# ----------------------------------------------------------------------------- session


@pytest.fixture
def session() -> ProtocolSession:
    t = SimulatedTransport(seed=1)
    t.open()
    return ProtocolSession(t, SessionTimeouts(default_s=0.3, sample_s=1.0, flush_s=0.01))


def test_session_retries_idempotent_reads_after_checksum_error(session: ProtocolSession) -> None:
    t = session.transport
    assert isinstance(t, SimulatedTransport)
    t.faults.corrupt_checksums = 1
    assert len(session.read_settings_blob()) == 208
    assert session.stats.retries == 1 and session.stats.checksum_errors == 1


def test_session_times_out_and_does_not_retry_key_presses(session: ProtocolSession) -> None:
    t = session.transport
    assert isinstance(t, SimulatedTransport)
    t.faults.drop_responses = 1
    with pytest.raises(ScopeTimeoutError):
        session.press_key(0x13)
    assert session.stats.retries == 0
    assert len(t.scope.key_log) == 1  # pressed exactly once
    t.faults.drop_responses = 5
    with pytest.raises(ScopeTimeoutError):
        session.echo(b"x")
    assert session.stats.retries == 2


def test_session_survives_fragmented_responses(session: ProtocolSession) -> None:
    t = session.transport
    assert isinstance(t, SimulatedTransport)
    t.faults.fragment_size = 61
    assert session.read_samples(1).size == 4000
    assert session.read_file("/sys.inf").startswith(b"[DST type]")


def test_session_discards_stray_frames(session: ProtocolSession) -> None:
    from hantek_mcp.protocol.commands import encode_response

    t = session.transport
    assert isinstance(t, SimulatedTransport)
    t._out.append(encode_response(0x21, b"\x00" * 7))  # unsolicited stale reply
    assert session.echo(b"abc") == b"abc"
    assert session.stats.stray_frames == 1


def test_session_propagates_disconnect(session: ProtocolSession) -> None:
    t = session.transport
    assert isinstance(t, SimulatedTransport)
    t.disconnect()
    with pytest.raises(DeviceDisconnectedError):
        session.read_settings_blob()


def test_session_checksum_failures_exhaust_retries(session: ProtocolSession) -> None:
    t = session.transport
    assert isinstance(t, SimulatedTransport)
    t.faults.corrupt_checksums = 10
    with pytest.raises(ChecksumError):
        session.echo(b"z")


# ----------------------------------------------------------------------------- USB transport (mocked)


def test_usb_transport_discovers_endpoints_and_detaches_driver(fake_usb: dict[str, Any]) -> None:
    from hantek_mcp.transport.usb import UsbTransport

    t = UsbTransport()
    t.open()
    info = t.info()
    assert info.endpoint_out == 0x02 and info.endpoint_in == 0x81 and info.max_packet_size == 512
    assert fake_usb["devices"][0].detached
    t.write(b"\x53\x02\x00\x00\x55", 1.0)
    assert t.read(10, 1.0) == b"\x53\x02\x00\x80\xd5"
    t.close()
    assert not fake_usb["devices"][0].detached  # kernel driver restored


def test_usb_transport_errors(fake_usb: dict[str, Any]) -> None:
    import usb.core

    from hantek_mcp.transport.usb import UsbSelector, UsbTransport

    with pytest.raises(DeviceNotFoundError):
        UsbTransport(UsbSelector(bus=9)).open()
    with pytest.raises(TransportError):
        UsbTransport(UsbSelector(endpoint_in=0x83)).open()
    t = UsbTransport()
    t.open()
    dev = fake_usb["devices"][0]
    dev.read_error = usb.core.USBTimeoutError("timeout", errno=errno.ETIMEDOUT)
    with pytest.raises(ScopeTimeoutError):
        t.read(64, 0.1)
    dev.read_error = usb.core.USBError("No such device", errno=errno.ENODEV)
    with pytest.raises(DeviceDisconnectedError):
        t.read(64, 0.1)


def test_usb_diagnostics_are_read_only(fake_usb: dict[str, Any]) -> None:
    from hantek_mcp.transport.usb import usb_diagnostics

    d = usb_diagnostics()
    assert d["device_count"] == 1
    eps = d["devices"][0]["configurations"][0]["interfaces"][0]["endpoints"]
    assert {e["address"] for e in eps} == {"0x02", "0x81"}
    assert fake_usb["devices"][0].writes == []
    fake_usb["devices"].clear()
    assert "hint" in usb_diagnostics()
