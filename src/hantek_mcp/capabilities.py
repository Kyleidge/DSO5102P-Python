"""Static capability and feature-status description.

``FEATURES`` is the single source of truth for what is implemented and how far
it has been validated. Status vocabulary:

* ``simulation_tested``   implemented and covered by simulator tests
* ``fixture_validated``   implemented and checked against recorded protocol data
* ``awaiting_hardware``   implemented; behaviour on a real DSO5102P not yet confirmed
* ``hardware_confirmed``  confirmed on a DSO5102P (by upstream or a recorded validation run)
* ``unsupported``         deliberately not implemented
"""

from __future__ import annotations

from typing import Any

from hantek_mcp.instrument.acquisition import CAPTURE_MODES, allowed_timebases
from hantek_mcp.instrument.channels import allowed_volts_per_div
from hantek_mcp.protocol.settings import (
    COUPLINGS,
    MEMORY_DEPTHS,
    PROBE_FACTORS,
    TRIGGER_COUPLINGS,
    TRIGGER_MODES,
    TRIGGER_SLOPES,
    TRIGGER_SOURCES,
)

FEATURES: dict[str, dict[str, str]] = {
    "usb_frame_protocol": {"status": "hardware_confirmed", "note": "upstream Echo/ReadSystemTime/KeyTrigger/Screenshot worked on a DSO5102P"},
    "settings_readback": {"status": "awaiting_hardware", "note": "upstream marked ReadSettings 'implemented, not tested'; layout from the DSO5102P's own protocol.inf"},
    "waveform_download": {"status": "awaiting_hardware", "note": "framing agrees in upstream, dsoc-extended and elinux; untested on DSO5102P by upstream"},
    "voltage_scaling": {"status": "awaiting_hardware", "note": "nominal 25 counts/div model; flagged nominal_unverified in every capture"},
    "time_scaling": {"status": "awaiting_hardware", "note": "memory_depth/20 samples/div model; flagged nominal_unverified"},
    "single_sequence_freshness": {"status": "awaiting_hardware", "note": "relies on TRIG-STATE enumeration from dsoc-extended"},
    "channel_controls_key_emulation": {"status": "awaiting_hardware", "note": "keycodes match the DSO5102P keyprotocol.inf; soft-menu F-key mapping from DSO5202B"},
    "trigger_controls_key_emulation": {"status": "awaiting_hardware", "note": "edge trigger menu only"},
    "direct_settings_write": {"status": "awaiting_hardware", "note": "command 0x11 documented on elinux; disabled unless control.settings_write_mode=direct_write_experimental"},
    "screenshot": {"status": "hardware_confirmed", "note": "upstream decoded 800x480x16-bit on a DSO5102P; RGB565 colour decode is new"},
    "run_stop": {"status": "hardware_confirmed", "note": "upstream Test1.py Start/StopAcquisition"},
    "panel_lock": {"status": "hardware_confirmed", "note": "upstream Test1.py Lock/UnLockControlPanel"},
    "measurements_fft_timing": {"status": "simulation_tested", "note": "computed in software from downloaded samples"},
    "simulator": {"status": "simulation_tested", "note": "protocol-level simulator"},
    "remote_shell": {"status": "unsupported", "note": "removed for safety (could brick the instrument)"},
    "file_write_firmware_calibration": {"status": "unsupported", "note": "never exposed"},
    "math_ref_fft_channels_download": {"status": "unsupported", "note": "protocol only exposes CH1/CH2 (elinux)"},
    "non_edge_trigger_types": {"status": "unsupported", "note": "menu layout differs; settings are reported read-only"},
    "continuous_streaming": {"status": "unsupported", "note": "the protocol only downloads stored records; repeated capture is offered instead"},
}  # fmt: skip

SAFETY = {
    "input_impedance": "1 MΩ ±2% || 20 pF ±3 pF",
    "maximum_input": "300 V RMS CAT II (10x probe), 150 V RMS CAT III (1x) per the DSO5000P manual; derate above 100 kHz",
    "note": "Software cannot make a connection safe. Follow the manufacturer's safety guidance; the scope ground is "
    "earth-referenced - never clip the probe ground to a non-earth-referenced potential.",
}


def capabilities(simulated: bool) -> dict[str, Any]:
    return {
        "model": "Hantek DSO5102P",
        "simulated_backend": simulated,
        "channels": [1, 2],
        "adc_bits": 8,
        "bandwidth_hz": 100e6,
        "max_realtime_sample_rate_sps": 1e9,
        "memory_depths_samples": list(MEMORY_DEPTHS),
        "volts_per_div_by_probe": {str(p): allowed_volts_per_div(p) for p in PROBE_FACTORS},
        "seconds_per_div": allowed_timebases(),
        "couplings": list(COUPLINGS),
        "probe_attenuations": list(PROBE_FACTORS),
        "trigger": {
            "types_supported_for_control": ["edge"],
            "sources": list(TRIGGER_SOURCES),
            "modes": list(TRIGGER_MODES),
            "slopes": list(TRIGGER_SLOPES),
            "couplings": list(TRIGGER_COUPLINGS),
            "level_range_divisions": 8,
        },
        "capture_modes": list(CAPTURE_MODES),
        "settings_change_method": "front-panel key emulation with mandatory read-back verification",
        "features": FEATURES,
        "safety": SAFETY,
    }
