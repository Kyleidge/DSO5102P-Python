"""ScopeService: measurement envelopes and bounded workflows on the simulator."""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

from hantek_mcp.errors import LimitExceededError, ParameterError, ScalingUnavailableError
from hantek_mcp.service import ScopeService
from hantek_mcp.transport.sim_signals import SignalSpec
from hantek_mcp.transport.simulator import SimulatedTransport
from tests.conftest import sim_config


def sim_of(svc: ScopeService) -> SimulatedTransport:
    t = svc.controller.transport
    assert isinstance(t, SimulatedTransport)
    return t


def test_capture_returns_compact_metadata(service: ScopeService) -> None:
    r = service.capture(1)
    cap = r["capture"]
    assert r["simulated"] and cap["simulated"]
    assert "raw_samples" not in cap and "voltage_v" not in cap
    assert cap["freshness"] == "new_trigger_confirmed"
    assert cap["capture_id"] in service.store


@pytest.mark.parametrize(
    ("kind", "key", "expected", "tol"),
    [
        ("frequency", "frequency_hz", 1000.0, 0.5),
        ("period", "period_s", 1e-3, 1e-6),
        ("duty_cycle", "duty_cycle_percent", 50.0, 0.5),
        ("peak_to_peak", "peak_to_peak", 5.0, 0.15),
        ("rms", "rms", 5.0 / 2**0.5, 0.05),
    ],
)
def test_measurements_on_probe_comp_like_signal(
    service: ScopeService, kind: str, key: str, expected: float, tol: float
) -> None:
    r = service.measure(kind, channel=1)
    assert r["valid"] and r["simulated"] and r["warnings"][0] == "SIMULATED DATA"
    assert r["results"][key] == pytest.approx(expected, abs=tol)
    assert r["voltage_scaling_status"] == "simulated"


def test_measurement_on_clipped_record_is_invalid(service: ScopeService) -> None:
    service.set_vertical_scale(1, 0.5)
    r = service.measure("voltage", channel=1)
    assert not r["valid"] and any("clipped" in w for w in r["warnings"])


def test_measurement_without_cycles_reports_error(service: ScopeService) -> None:
    sim_of(service).set_signal(1, SignalSpec("sine", 50, 2.0, 0.0))
    service.set_trigger_level(0.0)
    r = service.measure("frequency", channel=1)
    assert not r["valid"] and "error" in r["results"]


def test_require_verified_scaling(tmp_path: Path) -> None:
    svc = ScopeService(sim_config(tmp_path))
    svc.connect()
    cid = svc.capture(1)["capture"]["capture_id"]
    assert svc.measure("voltage", cid, require_verified_scaling=True)["valid"]  # simulated scaling is exact
    cap = svc.store.get(cid)
    cap.vertical["status"] = "nominal_unverified"
    with pytest.raises(ScalingUnavailableError):
        svc.measure("voltage", cid, require_verified_scaling=True)
    svc.disconnect()


def test_phase_and_delay_between_channels(service: ScopeService) -> None:
    sim_of(service).set_signal(2, SignalSpec("sine", 1000, 1.0, phase_deg=-45.0))
    r = service.phase()
    assert r["valid"] and r["results"]["phase_deg"] == pytest.approx(-45.0, abs=1.0)
    d = service.channel_delay()
    # CH2's rising zero crossing lags CH1's rising edge by 45/360 of a period; 1 LSB of the sine at its
    # zero crossing corresponds to ~6 us, which bounds the timing resolution here
    assert d["valid"] and d["results"]["delay_s"] == pytest.approx(0.125e-3, abs=1e-5)
    # captures from different acquisitions are flagged
    a = service.capture(1)["capture"]["capture_id"]
    b = service.capture(2)["capture"]["capture_id"]
    r2 = service.phase(a, b)
    assert not r2["valid"] and any("not from the same" in w for w in r2["warnings"])


def test_fft_and_signal_processing(service: ScopeService) -> None:
    f = service.fft(channel=2, max_points=128)
    res = f["results"]
    assert res["peaks"][0]["frequency_hz"] == pytest.approx(1000, abs=5)
    assert len(res["freqs_hz"]) <= 128 and res["spectrum_decimated"]
    p = service.spectrum_peaks(channel=1, n_peaks=3)["results"]["peaks"]
    assert [round(x["frequency_hz"] / 1000) for x in p[:2]] == [1, 3]
    q = service.measure("noise", channel=2)  # 10 mV noise at 1 V/div is below 1 LSB (40 mV)
    assert any("quantisation-limited" in w for w in q["warnings"])
    sim_of(service).set_signal(2, SignalSpec("sine", 1000, 1.0, noise_rms_v=0.1))
    n = service.measure("noise", channel=2)["results"]
    assert n["noise_rms"] == pytest.approx(0.1, rel=0.15)
    e = service.edges(channel=1, max_edges=3)["results"]
    assert len(e["edges"]) == 3 and e["truncated"]
    pl = service.pulses(channel=1)["results"]
    assert pl["width_stats_s"]["mean"] == pytest.approx(0.5e-3, rel=0.01)
    x = service.cross_correlation()["results"]
    assert x["peak_correlation"] > 0.5


def test_sample_access_is_bounded(service: ScopeService) -> None:
    cid = service.capture(1)["capture"]["capture_id"]
    s = service.get_samples(cid, 100)
    assert s["returned_points"] <= 100 and s["decimated"]
    w = service.get_sample_window(cid, start_index=10, count=50)
    assert w["end_index"] - w["start_index"] == 50 and len(w["voltage_v"]) == 50
    w2 = service.get_sample_window(cid, start_time_s=0.0, count=5)
    assert abs(w2["time_s"][0]) < 1e-5
    with pytest.raises(LimitExceededError):
        service.get_samples(cid, 10**6)
    with pytest.raises(ParameterError):
        service.get_sample_window(cid, start_index=10**6)


def test_export_and_screenshot(service: ScopeService) -> None:
    cid = service.capture(1)["capture"]["capture_id"]
    e = service.export(cid, "csv", "probe comp")
    assert Path(e["path"]).is_file() and Path(e["path"]).parent == service.store.export_dir
    png, meta = service.screenshot()
    assert png.startswith(b"\x89PNG") and Path(meta["path"]).is_file() and "not waveform" in meta["note"]


def test_software_autoset_recovers_from_bad_settings(service: ScopeService) -> None:
    service.set_vertical_scale(1, 0.05)  # badly clipped
    service.set_timebase(4e-6)  # far less than one cycle
    r = service.autoset(1)
    assert r["method"] == "software_assisted_autoset"
    assert all(c["verification"].startswith("readback") for c in r["changes"])
    final = r["final_capture"]
    assert not final["quality"]["clipped"]
    m = service.measure("frequency", final["capture_id"])
    assert m["valid"] and m["results"]["cycles_measured"] >= 2


def test_instrument_autoset(service: ScopeService) -> None:
    r = service.autoset(method="instrument")
    assert r["method"] == "instrument_autoset_key" and "settings_after" in r


def test_find_signal(service: ScopeService) -> None:
    sim_of(service).set_signal(2, SignalSpec("off"))
    r = service.find_signal()
    by = {c["channel"]: c for c in r["channels"]}
    assert by[1]["activity"] == "signal present" and by[2]["activity"] == "none/flat"


def test_measure_repeatedly(service: ScopeService) -> None:
    r = service.measure_repeatedly(1, ["frequency", "vpp"], count=5)
    assert r["iterations"] == 5 and r["statistics"]["frequency"]["mean"] == pytest.approx(1000, abs=1)
    assert len(r["stored_capture_ids"]) == 1 and r["stale_records_detected"] == 0
    with pytest.raises(LimitExceededError):
        service.measure_repeatedly(1, ["vpp"], count=10**6)
    with pytest.raises(ParameterError):
        service.measure_repeatedly(1, ["bogus"], count=2)


def test_acquire_until(service: ScopeService) -> None:
    r = service.acquire_until(1, "vpp", ">", 4.0, max_attempts=3)
    assert r["condition_met"] and r["capture"]["capture_id"] in service.store
    r2 = service.acquire_until(1, "vpp", ">", 100.0, max_attempts=3)
    assert not r2["condition_met"] and r2["attempts"] == 3


def test_workflow_abort(service: ScopeService) -> None:
    threading.Timer(0.3, service.abort).start()
    r = service.measure_repeatedly(1, ["vpp"], count=100, interval_s=0.05)
    assert r["aborted"] and r["aborted"]["code"] == "ABORTED" and r["iterations"] < 100
    assert service.capture(1)["capture"]["sample_count"] == 4000  # abort did not stick


def test_measurement_plan(service: ScopeService) -> None:
    plan = [
        {"action": "set_timebase", "args": {"seconds_per_div": 0.0004}},
        {"action": "capture", "args": {"channel": 1}},
        {"action": "measure", "args": {"kind": "frequency", "capture_id": "$last"}},
        {"action": "capture_both_channels", "args": {}},
        {"action": "phase", "args": {}},
        {"action": "wait", "args": {"seconds": 0.01}},
    ]
    r = service.run_plan(plan)
    assert r["all_ok"], r
    assert r["results"][2]["result"]["results"]["frequency_hz"] == pytest.approx(1000, abs=1)
    bad = service.run_plan([{"action": "nope"}, {"action": "capture", "args": {"channel": 1}}])
    assert not bad["all_ok"] and bad["steps_executed"] == 1
    cont = service.run_plan(
        [
            {"action": "set_timebase", "args": {"seconds_per_div": 1}},
            {"action": "capture", "args": {"channel": 1}},
        ],
        stop_on_error=False,
    )
    assert cont["steps_executed"] == 2 and cont["results"][1]["ok"]
    with pytest.raises(LimitExceededError):
        service.run_plan([{"action": "wait", "args": {"seconds": 0}}] * 1000)


def test_self_test_on_simulator(service: ScopeService) -> None:
    r = service.self_test(1)
    assert r["pass"], r["checks"]
    assert r["simulated"]
    sim_of(service).scope.physical_probe[1] = 1  # a 1x probe is attached ...
    service.set_probe(1, 10)  # ... but the scope is told 10x -> amplitude check must fail
    r2 = service.self_test(1)
    assert not r2["pass"]
    amp = next(c for c in r2["checks"] if c["check"].startswith("amplitude"))
    assert not amp["pass"]


def test_status_health_capabilities(service: ScopeService) -> None:
    st = service.status()
    assert st["connected"] and st["simulated"] and st["acquisition"]["trigger_state"]
    h = service.health()
    assert h["echo_ok"] and h["protocol_errors"]["checksum_errors"] == 0
    caps = service.capabilities()
    assert caps["features"]["remote_shell"]["status"] == "unsupported"
    assert 5.0 in caps["volts_per_div_by_probe"]["1"] and 10.0 not in caps["volts_per_div_by_probe"]["1"]
    assert service.usb_diagnostics()["backend_in_use"] == "simulator"
