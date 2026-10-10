"""Protocol-level simulated DSO5102P.

``SimulatedTransport`` behaves like the USB transport: it accepts encoded request
frames and returns encoded response frames, so the complete stack above it
(framing, decoders, protocol session, controller, MCP, GUI) runs unchanged.

What the simulator models:

* the settings record (bundled DSO5102P ``protocol.inf`` layout),
* front-panel key emulation including the channel/trigger soft-menu cycling
  behaviour that dsoc-extended documented on a DSO5202B,
* run/stop, single-sequence arming, auto/normal trigger modes, force trigger,
* edge-triggered acquisition of configurable signals on CH1/CH2 with 8-bit
  quantisation and clipping,
* screenshots (RGB565), files, system time, echo,
* fault injection: disconnection, dropped responses, corrupted checksums,
  response fragmentation and latency.

Every capture produced through it is labelled as simulated by the controller.
The simulator uses the same nominal scaling model as the decoder, so it validates
the software plumbing; it cannot validate the scaling model itself.
"""

from __future__ import annotations

import collections
import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import numpy as np
import numpy.typing as npt

from hantek_mcp.errors import DeviceDisconnectedError, NotConnectedError, ScopeTimeoutError
from hantek_mcp.protocol.commands import (
    BulkSubcommand,
    Command,
    DebugCommand,
    FrameDecoder,
    Marker,
    PanelSubcommand,
    encode_response,
)
from hantek_mcp.protocol.keys import Key
from hantek_mcp.protocol.scaling import ADC_MAX, ADC_MIN
from hantek_mcp.protocol.settings import (
    COUPLINGS,
    DEFAULT_KEY_LAYOUT_RESOURCE,
    DEFAULT_LAYOUT_RESOURCE,
    DSO5102P_TB_RANGE,
    DSO5102P_VB_RANGE,
    MEMORY_DEPTHS,
    NOMINAL_COUNTS_PER_DIV,
    PROBE_FACTORS,
    TIMEBASE_S_PER_DIV,
    TRIGGER_COUPLINGS,
    TRIGGER_MODES,
    TRIGGER_SLOPES,
    TRIGGER_SOURCES,
    TRIGGER_STATES,
    TRIGGER_TYPES,
    VOLTS_PER_DIV,
    _read_resource,
    decode_settings,
    default_layout,
    encode_settings,
    nearest_index,
)
from hantek_mcp.transport.base import Transport, TransportInfo
from hantek_mcp.transport.sim_signals import SignalSpec

log = logging.getLogger(__name__)

SAMPLE_CHUNK = 10000
FILE_CHUNK = 10208
SIM_SYS_INF = "[DST type]dst1102b-simulator\r\n[soft version]sim-0.1.0\r\n[fpga version]0x00000000\r\n"

STATE = {name: i for i, name in enumerate(TRIGGER_STATES)}


def default_raw_settings() -> dict[str, int]:
    """Settings after a simulated "Default Setup"."""
    raw = dict.fromkeys(default_layout().names(), 0)
    raw.update(
        {
            "VERT-CH1-DISP": 1,
            "VERT-CH1-VB": VOLTS_PER_DIV.index(1.0),
            "VERT-CH2-DISP": 1,
            "VERT-CH2-VB": VOLTS_PER_DIV.index(1.0),
            "TRIG-STATE": STATE["triggered"],
            "HORIZ-TB": nearest_index(TIMEBASE_S_PER_DIV, 200e-6),
            "HORIZ-WIN-TB": nearest_index(TIMEBASE_S_PER_DIV, 200e-6),
            "ACQURIE-STORE-DEPTH": 0,
            "TRIG-HOLDTIME-MIN": 100_000,
            "TRIG-HOLDTIME-MAX": 10_000_000_000_000,
            "TRIG-HOLDTIME": 100_000,
            "DISPLAY-CONTRAST": 8,
            "DISPLAY-MAXCONTRAST": 15,
            "DISPLAY-GRID-BRIGHT": 8,
            "DISPLAY-MAXGRID-BRIGHT": 15,
        }
    )
    return raw


@dataclass
class SimFaults:
    disconnected: bool = False
    drop_responses: int = 0
    corrupt_checksums: int = 0
    fragment_size: int | None = None
    latency_s: float = 0.0
    sample_error_channels: set[int] = field(default_factory=set)


class SimulatedScope:
    """State machine of the simulated instrument (independent of byte framing)."""

    def __init__(self, seed: int | None = None) -> None:
        self.layout = default_layout()
        self.raw = default_raw_settings()
        self.raw["TRIG-VPOS"] = 62  # ~2.5 V at 1 V/div: mid-level of the default probe-comp-like square
        self.signals: dict[int, SignalSpec] = {
            1: SignalSpec("square", 1000.0, 2.5, 2.5, noise_rms_v=0.01),  # probe-comp-like 5 Vpp @ 1 kHz
            2: SignalSpec("sine", 1000.0, 1.0, 0.0, phase_deg=-90.0, noise_rms_v=0.01),
        }
        self.rng = np.random.default_rng(seed)
        self.running = True
        self.single_armed = False
        self.single_polls = 0
        self.panel_locked = False
        self.menu: str | None = None
        self.buffers: dict[int, npt.NDArray[np.int8]] = {}
        self.acquisitions = 0
        self.key_log: list[int] = []
        self.settings_writes = 0
        self._acquire(force=True)

    # -- helpers ---------------------------------------------------------------------------------

    def vpd(self, ch: int) -> float:
        return VOLTS_PER_DIV[self.raw[f"VERT-CH{ch}-VB"]] * PROBE_FACTORS[self.raw[f"VERT-CH{ch}-PROBE"]]

    def record_length(self) -> int:
        depth = MEMORY_DEPTHS[self.raw["ACQURIE-STORE-DEPTH"]]
        return depth

    def sample_interval(self) -> float:
        depth = MEMORY_DEPTHS[self.raw["ACQURIE-STORE-DEPTH"]]
        return TIMEBASE_S_PER_DIV[self.raw["HORIZ-TB"]] / (depth / 20.0)

    def trigger_source(self) -> int | None:
        src = self.raw["TRIG-SRC"]
        return src + 1 if src in (0, 1) else None

    def trigger_level_v(self) -> float | None:
        ch = self.trigger_source()
        if ch is None:
            return None
        return self.raw["TRIG-VPOS"] * self.vpd(ch) / NOMINAL_COUNTS_PER_DIV

    def can_trigger(self) -> float | None:
        """Time shift for a triggered acquisition, or None if the trigger cannot fire."""
        ch = self.trigger_source()
        if ch is None or TRIGGER_TYPES[self.raw["TRIG-TYPE"]] != "edge":
            return None
        level = self.trigger_level_v()
        assert level is not None
        sig = self.signals[ch]
        coup = COUPLINGS[self.raw[f"VERT-CH{ch}-COUP"]]
        if coup == "GND":
            return None
        if coup == "AC":
            sig = SignalSpec(**{**sig.as_dict(), "offset_v": 0.0})
        rising = TRIGGER_SLOPES[self.raw["TRIG-EDGE-SLOPE"]] == "rising"
        return sig.trigger_shift(level, rising)

    def _acquire(self, force: bool = False) -> bool:
        """Acquire a new record into the buffers. Returns True if it was a triggered record."""
        shift = self.can_trigger()
        triggered = shift is not None
        auto = TRIGGER_MODES[self.raw["TRIG-MODE"]] == "auto"
        if not triggered and not (auto or force):
            return False
        if shift is None:
            shift = float(self.rng.uniform(0, 1e-3))
        n = self.record_length()
        dt = self.sample_interval()
        t = (np.arange(n) - n / 2.0) * dt + shift
        for ch in (1, 2):
            sig = self.signals[ch]
            v = sig.evaluate(t)
            if sig.waveform == "noise":
                v = v + self.rng.normal(0.0, sig.amplitude_v, n)
            if sig.noise_rms_v > 0:
                v = v + self.rng.normal(0.0, sig.noise_rms_v, n)
            coup = COUPLINGS[self.raw[f"VERT-CH{ch}-COUP"]]
            if coup == "AC":
                v = v - sig.offset_v
            elif coup == "GND":
                v = np.zeros(n)
            if self.raw[f"VERT-CH{ch}-RPHASE"]:
                v = -v
            counts = v * NOMINAL_COUNTS_PER_DIV / self.vpd(ch) + self.raw[f"VERT-CH{ch}-POS"]
            self.buffers[ch] = np.clip(np.round(counts), ADC_MIN, ADC_MAX).astype(np.int8)
        self.acquisitions += 1
        return triggered

    def poll(self) -> None:
        """Advance the acquisition state machine (called on every settings read/sample read)."""
        mode_auto = TRIGGER_MODES[self.raw["TRIG-MODE"]] == "auto"
        if self.single_armed:
            if self.single_polls == 0:
                # Report the armed state at least once, like a real instrument filling its pre-trigger buffer.
                self.single_polls += 1
                self.raw["TRIG-STATE"] = STATE["ready"]
                return
            self.single_polls += 1
            if self._acquire(force=False):
                self.single_armed = False
                self.running = False
                self.raw["TRIG-STATE"] = STATE["stop"]
            else:
                self.raw["TRIG-STATE"] = STATE["ready"]
            return
        if not self.running:
            self.raw["TRIG-STATE"] = STATE["stop"]
            return
        if self._acquire(force=False):
            self.raw["TRIG-STATE"] = STATE["triggered"]
        elif mode_auto:
            self._acquire(force=True)
            self.raw["TRIG-STATE"] = STATE["auto"]
        else:
            self.raw["TRIG-STATE"] = STATE["ready"]

    def settings_blob(self) -> bytes:
        self.poll()
        self._update_counter()
        return encode_settings(self.raw, self.layout)

    def _update_counter(self) -> None:
        ch = self.trigger_source()
        sig = self.signals.get(ch) if ch else None
        if sig is not None and sig.periodic() and self.can_trigger() is not None:
            self.raw["TRIG-FREQUENCY"] = int(round(sig.frequency_hz * 1000))
        else:
            self.raw["TRIG-FREQUENCY"] = 0

    # -- key handling ----------------------------------------------------------------------------

    def _cycle(self, name: str, n: int) -> None:
        self.raw[name] = (self.raw[name] + 1) % n

    def _clamp(self, name: str, delta: int, lo: int, hi: int) -> None:
        self.raw[name] = max(lo, min(hi, self.raw[name] + delta))

    def press(self, code: int) -> int:
        self.key_log.append(code)
        if self.panel_locked:
            log.debug("sim: key 0x%02X ignored: panel locked", code)
        try:
            key = Key(code)
        except ValueError:
            return 0
        menu_ids = {"CH1": 1, "CH2": 2, "TRIG": 3, "HORIZ": 4}
        for ch in (1, 2):
            p = f"VERT-CH{ch}-"
            if key == getattr(Key, f"CH{ch}_MENU"):
                if not self.raw[p + "DISP"]:
                    self.raw[p + "DISP"] = 1
                    self.menu = f"CH{ch}"
                elif self.menu == f"CH{ch}":
                    self.raw[p + "DISP"] = 0
                    self.menu = None
                else:
                    self.menu = f"CH{ch}"
            elif key == getattr(Key, f"CH{ch}_VDIV_UP"):
                self._clamp(p + "VB", +1, *DSO5102P_VB_RANGE)
            elif key == getattr(Key, f"CH{ch}_VDIV_DOWN"):
                self._clamp(p + "VB", -1, *DSO5102P_VB_RANGE)
            elif key == getattr(Key, f"CH{ch}_POS_UP"):
                self._clamp(p + "POS", +1, -250, 250)
            elif key == getattr(Key, f"CH{ch}_POS_DOWN"):
                self._clamp(p + "POS", -1, -250, 250)
            elif key == getattr(Key, f"CH{ch}_POS_ZERO"):
                self.raw[p + "POS"] = 0
        if key in (Key.F1, Key.F4) and self.menu in ("CH1", "CH2"):
            p = f"VERT-{self.menu}-"
            if key == Key.F1:
                self._cycle(p + "COUP", len(COUPLINGS))
            else:
                self._cycle(p + "PROBE", len(PROBE_FACTORS))
        elif key in (Key.F1, Key.F2, Key.F3, Key.F4, Key.F5) and self.menu == "TRIG":
            name, n = {
                Key.F1: ("TRIG-TYPE", len(TRIGGER_TYPES)),
                Key.F2: ("TRIG-SRC", len(TRIGGER_SOURCES)),
                Key.F3: ("TRIG-EDGE-SLOPE", len(TRIGGER_SLOPES)),
                Key.F4: ("TRIG-MODE", len(TRIGGER_MODES)),
                Key.F5: ("TRIG-COUP", len(TRIGGER_COUPLINGS)),
            }[key]
            self._cycle(name, n)
        elif key == Key.TRIG_MENU:
            self.menu = "TRIG"
        elif key == Key.HORIZ_MENU:
            self.menu = "HORIZ"
        elif key in (Key.MEASURE, Key.ACQUIRE, Key.UTILITY, Key.CURSOR, Key.DISPLAY, Key.SAVE_RECALL,
                     Key.MATH_MENU, Key.HELP):  # fmt: skip
            self.menu = key.name
        elif key == Key.TIMEBASE_UP:
            self._clamp("HORIZ-TB", +1, *DSO5102P_TB_RANGE)
        elif key == Key.TIMEBASE_DOWN:
            self._clamp("HORIZ-TB", -1, *DSO5102P_TB_RANGE)
        elif key == Key.TRIG_LEVEL_UP:
            self._clamp("TRIG-VPOS", +1, -200, 200)
        elif key == Key.TRIG_LEVEL_DOWN:
            self._clamp("TRIG-VPOS", -1, -200, 200)
        elif key == Key.TRIG_LEVEL_ZERO:
            self.raw["TRIG-VPOS"] = 0
        elif key == Key.TRIG_LEVEL_50:
            ch = self.trigger_source()
            if ch is not None:
                lo, hi = self.signals[ch].extremes()
                self.raw["TRIG-VPOS"] = int(round((lo + hi) / 2 * NOMINAL_COUNTS_PER_DIV / self.vpd(ch)))
        elif key == Key.FORCE_TRIG:
            if self.single_armed:
                self._acquire(force=True)
                self.single_armed = False
                self.running = False
                self.raw["TRIG-STATE"] = STATE["stop"]
            elif self.running:
                self._acquire(force=True)
        elif key == Key.RUN_STOP:
            self.running = not self.running
            self.single_armed = False
            self.raw["TRIG-STATE"] = STATE["auto"] if self.running else STATE["stop"]
        elif key == Key.SINGLE_SEQ:
            self.single_armed = True
            self.single_polls = 0
            self.running = False
            self.raw["TRIG-STATE"] = STATE["ready"]
        elif key == Key.DEFAULT_SETUP:
            self.raw = default_raw_settings()
            self.running, self.single_armed, self.menu = True, False, None
        elif key == Key.AUTOSET:
            self._autoset()
        return menu_ids.get(self.menu or "", 0)

    def _autoset(self) -> None:
        """Simplified autoset: fit the largest signal to ~5 div, show ~2.5 periods, trigger at 50%."""
        for ch in (1, 2):
            sig = self.signals[ch]
            lo, hi = sig.extremes()
            span = max(hi - lo, 1e-3)
            probe = PROBE_FACTORS[self.raw[f"VERT-CH{ch}-PROBE"]]
            self.raw[f"VERT-CH{ch}-VB"] = nearest_index(
                VOLTS_PER_DIV, span / 5.0 / probe, *DSO5102P_VB_RANGE
            )
            self.raw[f"VERT-CH{ch}-POS"] = int(round(-(lo + hi) / 2 * NOMINAL_COUNTS_PER_DIV / self.vpd(ch)))
            self.raw[f"VERT-CH{ch}-DISP"] = int(sig.waveform != "off")
        src = 1 if self.signals[1].waveform != "off" else 2
        sig = self.signals[src]
        self.raw.update({"TRIG-TYPE": 0, "TRIG-SRC": src - 1, "TRIG-MODE": 0, "TRIG-EDGE-SLOPE": 0})
        if sig.periodic():
            self.raw["HORIZ-TB"] = nearest_index(
                TIMEBASE_S_PER_DIV, 2.5 / sig.frequency_hz / 16, *DSO5102P_TB_RANGE
            )
        lo, hi = sig.extremes()
        self.raw["TRIG-VPOS"] = int(round((lo + hi) / 2 * NOMINAL_COUNTS_PER_DIV / self.vpd(src)))
        self.running, self.single_armed = True, False

    # -- screenshot ------------------------------------------------------------------------------

    def screenshot_rgb565(self) -> bytes:
        h, w = 480, 800
        img = np.zeros((h, w), dtype=np.uint16)
        grid = 0x4208
        img[40:440:50, 0:800:4] = grid
        img[40:440:2, 0:800:50] = grid
        colours = {1: 0xFFE0, 2: 0x07FF}  # yellow, cyan
        for ch, buf in self.buffers.items():
            if not self.raw[f"VERT-CH{ch}-DISP"]:
                continue
            xs = np.linspace(0, len(buf) - 1, w).astype(int)
            ys = np.clip(240 - (buf[xs].astype(int) * 2), 0, h - 1)
            img[ys, np.arange(w)] = colours[ch]
        return img.astype("<u2").tobytes()


class SimulatedTransport(Transport):
    """Byte-level transport wrapping :class:`SimulatedScope`."""

    def __init__(self, scope: SimulatedScope | None = None, seed: int | None = None) -> None:
        self.scope = scope or SimulatedScope(seed=seed)
        self.faults = SimFaults()
        self._open = False
        self._decoder = FrameDecoder()
        self._out: collections.deque[bytes] = collections.deque()
        self._lock = threading.Lock()
        self.requests: list[tuple[int, int, bytes]] = []

    # -- Transport API ---------------------------------------------------------------------------

    def open(self) -> None:
        if self.faults.disconnected:
            raise DeviceDisconnectedError("simulated device is disconnected")
        self._open = True

    def close(self) -> None:
        self._open = False
        self._out.clear()
        self._decoder.clear()

    @property
    def is_open(self) -> bool:
        return self._open

    def _check(self) -> None:
        if not self._open:
            raise NotConnectedError("simulated transport is not open")
        if self.faults.disconnected:
            raise DeviceDisconnectedError("simulated device disconnected")

    def write(self, data: bytes, timeout_s: float) -> int:
        self._check()
        with self._lock:
            for frame in self._decoder.feed(data):
                self.requests.append((frame.marker, frame.command, frame.payload))
                for resp in self._handle(frame.marker, frame.command, frame.payload):
                    if self.faults.drop_responses > 0:
                        self.faults.drop_responses -= 1
                        continue
                    if self.faults.corrupt_checksums > 0:
                        self.faults.corrupt_checksums -= 1
                        resp = resp[:-1] + bytes([(resp[-1] + 1) & 0xFF])
                    self._queue(resp)
        return len(data)

    def _queue(self, resp: bytes) -> None:
        frag = self.faults.fragment_size
        if frag:
            for i in range(0, len(resp), frag):
                self._out.append(resp[i : i + frag])
        else:
            self._out.append(resp)

    def read(self, max_bytes: int, timeout_s: float) -> bytes:
        self._check()
        if self.faults.latency_s:
            time.sleep(self.faults.latency_s)
        with self._lock:
            if self._out:
                chunk = self._out.popleft()
                if len(chunk) > max_bytes:
                    self._out.appendleft(chunk[max_bytes:])
                    chunk = chunk[:max_bytes]
                return chunk
        time.sleep(min(timeout_s, 0.005))
        raise ScopeTimeoutError(f"simulated read timed out after {timeout_s:.3f}s")

    def info(self) -> TransportInfo:
        return TransportInfo(
            backend="simulator",
            simulated=True,
            description="Protocol-level DSO5102P simulator (no hardware)",
            vendor_id=0x049F,
            product_id=0x505A,
            endpoint_out=0x02,
            endpoint_in=0x81,
            max_packet_size=512,
            extra={"signals": {str(k): v.as_dict() for k, v in self.scope.signals.items()}},
        )

    # -- convenience for tests / GUI -------------------------------------------------------------

    def set_signal(self, channel: int, spec: SignalSpec) -> None:
        spec.validate()
        with self._lock:
            self.scope.signals[channel] = spec

    def disconnect(self) -> None:
        self.faults.disconnected = True

    def reconnect(self) -> None:
        self.faults.disconnected = False

    # -- request handling ------------------------------------------------------------------------

    def _handle(self, marker: int, cmd: int, payload: bytes) -> list[bytes]:
        sc = self.scope
        if marker == Marker.DEBUG:
            if cmd == DebugCommand.BEEP:
                return [encode_response(cmd, b"", marker=Marker.DEBUG)]
            return []
        if cmd == Command.ECHO:
            return [encode_response(cmd, payload)]
        if cmd == Command.READ_SETTINGS:
            return [encode_response(cmd, sc.settings_blob())]
        if cmd == Command.READ_SAMPLES:
            return self._samples(payload)
        if cmd == Command.READ_FILE:
            return self._file(payload[1:].decode("ascii", "replace"))
        if cmd == Command.WRITE_SETTINGS:
            try:
                values, _ = decode_settings(payload, sc.layout)
            except Exception:  # noqa: BLE001 - mirror the instrument's status byte
                return [encode_response(cmd, b"\x01")]
            sc.raw.update(values)
            sc.settings_writes += 1
            return [encode_response(cmd, b"\x00")]
        if cmd == Command.PANEL:
            sub, val = payload[0], payload[1]
            if sub == PanelSubcommand.ACQUISITION:
                sc.running = val == 0
                sc.single_armed = False
                sc.raw["TRIG-STATE"] = STATE["auto"] if sc.running else STATE["stop"]
            elif sub == PanelSubcommand.LOCK:
                sc.panel_locked = bool(val)
            return [encode_response(cmd, payload[:2])]
        if cmd == Command.KEY_PRESS:
            menu = sc.press(payload[0])
            return [encode_response(cmd, bytes([menu]))]
        if cmd == Command.SCREENSHOT:
            return self._bulk(cmd, sc.screenshot_rgb565())
        if cmd == Command.READ_SYSTEM_TIME:
            now = datetime.now()
            p = bytes([now.year & 0xFF, now.year >> 8, now.month, now.day, now.hour, now.minute, now.second])
            return [encode_response(cmd, p)]
        return []

    def _samples(self, payload: bytes) -> list[bytes]:
        sc = self.scope
        ch_code = payload[1] if len(payload) > 1 else 0
        ch = ch_code + 1
        if sc.running and not sc.single_armed:
            sc.poll()
        if ch not in (1, 2) or ch in self.faults.sample_error_channels or not sc.raw[f"VERT-CH{ch}-DISP"]:
            return [encode_response(Command.READ_SAMPLES, bytes([BulkSubcommand.ERROR, ch_code & 0xFF]))]
        data = sc.buffers[ch].tobytes()
        n = len(data)
        frames = [encode_response(Command.READ_SAMPLES, bytes([0, n & 0xFF, (n >> 8) & 0xFF, n >> 16]))]
        for i in range(0, n, SAMPLE_CHUNK):
            frames.append(
                encode_response(Command.READ_SAMPLES, bytes([1, ch_code]) + data[i : i + SAMPLE_CHUNK])
            )
        frames.append(encode_response(Command.READ_SAMPLES, bytes([2, ch_code])))
        return frames

    def _file(self, name: str) -> list[bytes]:
        files = {
            "/protocol.inf": _read_resource(DEFAULT_LAYOUT_RESOURCE).encode("ascii"),
            "/keyprotocol.inf": _read_resource(DEFAULT_KEY_LAYOUT_RESOURCE).encode("ascii"),
            "/sys.inf": SIM_SYS_INF.encode("ascii"),
            "/logotype.dis": b"DSO5102P\x00",
        }
        return self._bulk(Command.READ_FILE, files.get(name, b""))

    @staticmethod
    def _bulk(cmd: int, data: bytes) -> list[bytes]:
        frames = [
            encode_response(cmd, bytes([1]) + data[i : i + FILE_CHUNK]) for i in range(0, len(data), FILE_CHUNK)
        ]
        frames.append(encode_response(cmd, bytes([2, sum(data) & 0xFF])))
        return frames

    def describe_state(self) -> dict[str, Any]:
        sc = self.scope
        return {
            "running": sc.running,
            "single_armed": sc.single_armed,
            "panel_locked": sc.panel_locked,
            "menu": sc.menu,
            "acquisitions": sc.acquisitions,
            "signals": {str(k): v.as_dict() for k, v in sc.signals.items()},
        }
