"""SYSData settings record: layout parsing, binary decode/encode and interpretation.

The scope describes its own settings record in ``/protocol.inf`` (readable with
command 0x10). The DSO5102P file captured by the upstream project (firmware
3.40.0(200217.0), ``DST type dst1102b``) is bundled as the default layout; at
connect time the controller re-reads the file from the instrument and uses
that instead whenever it is available.

Interpretation tables carry a ``source`` so callers can tell confirmed facts
from inferences. None of the enumeration tables below has been verified on a
DSO5102P by this project yet (see docs/usb-protocol.md).
"""

from __future__ import annotations

import re
import struct
from dataclasses import dataclass, field
from importlib import resources
from typing import Any

from hantek_mcp.errors import ParameterError, ProtocolError

DEFAULT_LAYOUT_RESOURCE = "dso5102p_fw3.40.0/protocol.inf"
DEFAULT_KEY_LAYOUT_RESOURCE = "dso5102p_fw3.40.0/keyprotocol.inf"

_LINE_RE = re.compile(r"^\s*\[([^\]]+)\]\s*(\d+)?\s*$")


@dataclass(frozen=True, slots=True)
class FieldSpec:
    name: str
    size: int
    offset: int


@dataclass(frozen=True)
class SettingsLayout:
    fields: tuple[FieldSpec, ...]
    total: int
    source: str = "unknown"

    @property
    def size(self) -> int:
        return sum(f.size for f in self.fields)

    def names(self) -> list[str]:
        return [f.name for f in self.fields]

    def get(self, name: str) -> FieldSpec:
        for f in self.fields:
            if f.name == name:
                return f
        raise KeyError(name)

    def __contains__(self, name: object) -> bool:
        return any(f.name == name for f in self.fields)


def parse_inf(text: str) -> tuple[int | None, list[tuple[str, int]]]:
    """Parse the ``[NAME] size`` format shared by protocol.inf and keyprotocol.inf."""
    total: int | None = None
    entries: list[tuple[str, int]] = []
    started = False
    for raw_line in text.replace("\r", "").split("\n"):
        line = raw_line.strip()
        if not line:
            continue
        m = _LINE_RE.match(line)
        if not m:
            raise ProtocolError(f"unparseable .inf line: {line!r}")
        name, size = m.group(1), m.group(2)
        if name == "TOTAL":
            total = int(size) if size is not None else None
        elif name == "START":
            started = True
        elif name == "END":
            break
        else:
            if not started:
                raise ProtocolError(f".inf entry {name!r} appears before [START]")
            if size is None:
                raise ProtocolError(f".inf entry {name!r} has no size")
            entries.append((name, int(size)))
    return total, entries


def parse_layout(text: str, source: str = "instrument") -> SettingsLayout:
    total, entries = parse_inf(text)
    offset = 0
    fields: list[FieldSpec] = []
    for name, size in entries:
        if size not in (1, 2, 4, 8):
            raise ProtocolError(f"unsupported field size {size} for {name}")
        fields.append(FieldSpec(name, size, offset))
        offset += size
    if total is not None and total != offset:
        raise ProtocolError(f"protocol.inf TOTAL={total} but fields sum to {offset}")
    return SettingsLayout(tuple(fields), offset, source)


def _read_resource(name: str) -> str:
    return resources.files("hantek_mcp.protocol").joinpath("data", name).read_text(encoding="ascii")


def default_layout() -> SettingsLayout:
    return parse_layout(_read_resource(DEFAULT_LAYOUT_RESOURCE), source="bundled:" + DEFAULT_LAYOUT_RESOURCE)


def default_key_names() -> list[str]:
    _, entries = parse_inf(_read_resource(DEFAULT_KEY_LAYOUT_RESOURCE))
    return [name for name, _ in entries]


def parse_key_names(text: str) -> list[str]:
    """Key code == index of the entry after ``[START]`` (elinux, upstream Test1.py)."""
    _, entries = parse_inf(text)
    return [name for name, _ in entries]


_FORMATS = {1: "<B", 2: "<h", 4: "<i", 8: "<q"}


def decode_settings(blob: bytes, layout: SettingsLayout) -> tuple[dict[str, int], bytes]:
    """Decode a SYSData blob into raw integers.

    1-byte fields are unsigned, 2-, 4- and 8-byte fields signed little-endian (the
    vertical/trigger positions are signed; 8-byte fields hold times/frequencies that
    are positive in practice so signedness only matters for HORIZ-TRIGTIME).
    Returns ``(values, trailing_bytes)``.
    """
    if len(blob) < layout.size:
        raise ProtocolError(
            f"settings record too short: {len(blob)} bytes, layout needs {layout.size}",
            details={"received": len(blob), "expected": layout.size},
        )
    values: dict[str, int] = {}
    for f in layout.fields:
        values[f.name] = struct.unpack_from(_FORMATS[f.size], blob, f.offset)[0]
    return values, bytes(blob[layout.size :])


def encode_settings(values: dict[str, int], layout: SettingsLayout, trailing: bytes = b"") -> bytes:
    out = bytearray(layout.size)
    for f in layout.fields:
        if f.name not in values:
            raise ParameterError(f"missing settings field {f.name}")
        v = int(values[f.name])
        fmt = _FORMATS[f.size]
        try:
            struct.pack_into(fmt, out, f.offset, v)
        except struct.error as e:
            raise ParameterError(f"value {v} out of range for {f.name} ({f.size} bytes)") from e
    return bytes(out) + trailing


# ---------------------------------------------------------------------------------------------
# Interpretation tables
# ---------------------------------------------------------------------------------------------

#: V/div at the BNC for a 1x probe, indexed by VERT-CHx-VB. Source: dsoc-extended (DSO5202B)
#: settings decoder, consistent with the 1-2-5 sequence in the DSO5000P manual. The DSO5102P
#: manual specifies 2 mV/div .. 5 V/div, i.e. indices 0..10.
VOLTS_PER_DIV: tuple[float, ...] = (0.002, 0.005, 0.01, 0.02, 0.05, 0.1, 0.2, 0.5, 1.0, 2.0, 5.0, 10.0)
DSO5102P_VB_RANGE = (0, 10)

#: s/div indexed by HORIZ-TB. 2-4-8 sequence starting at 2 ns (dsoc-extended; elinux notes TBID<3
#: are 2/4/8 ns). DSO5102P manual: 4 ns/div .. 40 s/div => indices 1..31.
TIMEBASE_S_PER_DIV: tuple[float, ...] = tuple(
    round(m * 10.0**e, 15) for e in range(-9, 2) for m in (2.0, 4.0, 8.0)
)
DSO5102P_TB_RANGE = (1, 31)

COUPLINGS = ("DC", "AC", "GND")
PROBE_FACTORS = (1, 10, 100, 1000)
TRIGGER_STATES = ("stop", "ready", "auto", "triggered", "scan", "astop", "armed")
TRIGGER_TYPES = ("edge", "video", "pulse", "slope", "overtime", "swap")
TRIGGER_SOURCES = ("CH1", "CH2", "EXT", "EXT/5", "AC50")
TRIGGER_MODES = ("auto", "normal")
TRIGGER_COUPLINGS = ("DC", "AC", "NOISE_REJECT", "HF_REJECT", "LF_REJECT")
TRIGGER_SLOPES = ("rising", "falling")
# The following orderings are inferred from the front-panel menu order in the DSO5000P manual.
ACQUIRE_MODES = ("normal", "peak_detect", "average")
ACQUIRE_TYPES = ("real_time", "equivalent")
ACQUIRE_AVERAGES = (4, 16, 64, 128)
MEMORY_DEPTHS = (4000, 20000, 40000)

#: Counts of the 8-bit sample stream per vertical division. elinux: samples span ~10.2 div over
#: -127..127 (=> ~25/div); dsoc-extended trigger level formula uses VPOS*V/div*probe/25.
NOMINAL_COUNTS_PER_DIV = 25.0

SOURCE_DSOC = "dsoc-extended decoder (verified by its author on DSO5202B)"
SOURCE_MANUAL = "inferred from DSO5000P manual menu order"
SOURCE_ELINUX = "elinux.org Das_Oszi_Protocol"


@dataclass(frozen=True)
class Decoded:
    """A decoded value plus provenance so callers can judge its trustworthiness."""

    raw: int
    value: Any
    source: str
    valid: bool = True

    def as_dict(self) -> dict[str, Any]:
        return {"raw": self.raw, "value": self.value, "source": self.source, "valid": self.valid}


def lookup(table: tuple[Any, ...], raw: int, source: str) -> Decoded:
    if 0 <= raw < len(table):
        return Decoded(raw, table[raw], source)
    return Decoded(raw, None, source, valid=False)


@dataclass
class ChannelState:
    channel: int
    enabled: bool
    volts_per_div: float | None
    vb_index: int
    coupling: str | None
    bandwidth_limit_20mhz: bool
    fine_vdiv: bool
    probe: int | None
    inverted: bool
    position_counts: int
    notes: list[str] = field(default_factory=list)

    @property
    def effective_volts_per_div(self) -> float | None:
        """V/div at the probe tip (BNC V/div times probe attenuation)."""
        if self.volts_per_div is None or self.probe is None:
            return None
        return self.volts_per_div * self.probe

    def as_dict(self) -> dict[str, Any]:
        return {
            "channel": self.channel,
            "enabled": self.enabled,
            "volts_per_div": self.volts_per_div,
            "volts_per_div_at_probe_tip": self.effective_volts_per_div,
            "vb_index": self.vb_index,
            "coupling": self.coupling,
            "bandwidth_limit_20mhz": self.bandwidth_limit_20mhz,
            "fine_vdiv": self.fine_vdiv,
            "probe_attenuation": self.probe,
            "inverted": self.inverted,
            "position_counts": self.position_counts,
            "position_divisions": self.position_counts / NOMINAL_COUNTS_PER_DIV,
            "notes": list(self.notes),
        }


@dataclass
class TriggerState:
    state: str | None
    type: str | None
    source: str | None
    mode: str | None
    coupling: str | None
    slope: str | None
    level_counts: int
    counter_frequency_hz: float | None
    holdoff_s: float | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "type": self.type,
            "source": self.source,
            "mode": self.mode,
            "coupling": self.coupling,
            "slope": self.slope,
            "level_counts": self.level_counts,
            "level_divisions": self.level_counts / NOMINAL_COUNTS_PER_DIV,
            "counter_frequency_hz": self.counter_frequency_hz,
            "holdoff_s": self.holdoff_s,
        }


@dataclass
class HorizontalState:
    seconds_per_div: float | None
    tb_index: int
    window_enabled: bool
    window_seconds_per_div: float | None
    trigger_time_raw: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "seconds_per_div": self.seconds_per_div,
            "tb_index": self.tb_index,
            "window_zoom_enabled": self.window_enabled,
            "window_seconds_per_div": self.window_seconds_per_div,
            "trigger_time_raw": self.trigger_time_raw,
        }


@dataclass
class AcquireState:
    mode: str | None
    averages: int | None
    type: str | None
    memory_depth: int | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "averages": self.averages,
            "type": self.type,
            "memory_depth": self.memory_depth,
            "memory_depth_source": SOURCE_MANUAL,
        }


@dataclass
class ScopeSettings:
    """Typed view of a decoded SYSData record. ``raw`` always holds every field."""

    raw: dict[str, int]
    channels: dict[int, ChannelState]
    trigger: TriggerState
    horizontal: HorizontalState
    acquire: AcquireState
    layout_source: str
    trailing_bytes: int = 0

    def as_dict(self, include_raw: bool = False) -> dict[str, Any]:
        d: dict[str, Any] = {
            "channels": {str(k): v.as_dict() for k, v in self.channels.items()},
            "trigger": self.trigger.as_dict(),
            "horizontal": self.horizontal.as_dict(),
            "acquire": self.acquire.as_dict(),
            "layout_source": self.layout_source,
            "decoding_sources": {
                "volts_per_div": SOURCE_DSOC,
                "timebase": SOURCE_DSOC,
                "coupling/probe/trigger enums": SOURCE_DSOC,
                "acquire enums/memory depth": SOURCE_MANUAL,
                "counts_per_division": SOURCE_ELINUX,
            },
        }
        if include_raw:
            d["raw"] = dict(self.raw)
        return d


def _g(raw: dict[str, int], name: str, default: int = 0) -> int:
    return int(raw.get(name, default))


def interpret_settings(raw: dict[str, int], layout_source: str = "unknown", trailing: int = 0) -> ScopeSettings:
    channels: dict[int, ChannelState] = {}
    for ch in (1, 2):
        p = f"VERT-CH{ch}-"
        vb = _g(raw, p + "VB")
        vdiv = lookup(VOLTS_PER_DIV, vb, SOURCE_DSOC)
        coup = lookup(COUPLINGS, _g(raw, p + "COUP"), SOURCE_DSOC)
        probe = lookup(PROBE_FACTORS, _g(raw, p + "PROBE"), SOURCE_DSOC)
        notes: list[str] = []
        fine = bool(_g(raw, p + "FINE"))
        if fine:
            notes.append("fine V/div adjustment active: V/div is not one of the coarse table values")
        if not vdiv.valid:
            notes.append(f"unknown VB index {vb}")
        channels[ch] = ChannelState(
            channel=ch,
            enabled=bool(_g(raw, p + "DISP")),
            volts_per_div=vdiv.value,
            vb_index=vb,
            coupling=coup.value,
            bandwidth_limit_20mhz=bool(_g(raw, p + "20MHZ")),
            fine_vdiv=fine,
            probe=probe.value,
            inverted=bool(_g(raw, p + "RPHASE")),
            position_counts=_g(raw, p + "POS"),
            notes=notes,
        )
    freq_raw = raw.get("TRIG-FREQUENCY")
    hold_raw = raw.get("TRIG-HOLDTIME")
    trigger = TriggerState(
        state=lookup(TRIGGER_STATES, _g(raw, "TRIG-STATE"), SOURCE_DSOC).value,
        type=lookup(TRIGGER_TYPES, _g(raw, "TRIG-TYPE"), SOURCE_DSOC).value,
        source=lookup(TRIGGER_SOURCES, _g(raw, "TRIG-SRC"), SOURCE_DSOC).value,
        mode=lookup(TRIGGER_MODES, _g(raw, "TRIG-MODE"), SOURCE_DSOC).value,
        coupling=lookup(TRIGGER_COUPLINGS, _g(raw, "TRIG-COUP"), SOURCE_DSOC).value,
        slope=lookup(TRIGGER_SLOPES, _g(raw, "TRIG-EDGE-SLOPE"), SOURCE_DSOC).value,
        level_counts=_g(raw, "TRIG-VPOS"),
        counter_frequency_hz=None if freq_raw is None else freq_raw * 1e-3,
        holdoff_s=None if hold_raw is None else hold_raw * 1e-12,
    )
    tb = _g(raw, "HORIZ-TB")
    wtb = _g(raw, "HORIZ-WIN-TB")
    horizontal = HorizontalState(
        seconds_per_div=lookup(TIMEBASE_S_PER_DIV, tb, SOURCE_DSOC).value,
        tb_index=tb,
        window_enabled=bool(_g(raw, "HORIZ-WIN-STATE")),
        window_seconds_per_div=lookup(TIMEBASE_S_PER_DIV, wtb, SOURCE_DSOC).value,
        trigger_time_raw=_g(raw, "HORIZ-TRIGTIME"),
    )
    acquire = AcquireState(
        mode=lookup(ACQUIRE_MODES, _g(raw, "ACQURIE-MODE"), SOURCE_MANUAL).value,
        averages=lookup(ACQUIRE_AVERAGES, _g(raw, "ACQURIE-AVG-CNT"), SOURCE_MANUAL).value,
        type=lookup(ACQUIRE_TYPES, _g(raw, "ACQURIE-TYPE"), SOURCE_MANUAL).value,
        memory_depth=lookup(MEMORY_DEPTHS, _g(raw, "ACQURIE-STORE-DEPTH"), SOURCE_MANUAL).value,
    )
    return ScopeSettings(raw, channels, trigger, horizontal, acquire, layout_source, trailing)


def nearest_index(table: tuple[float, ...], value: float, lo: int = 0, hi: int | None = None) -> int:
    """Index of the table entry closest to ``value`` in ratio terms, restricted to [lo, hi]."""
    if value <= 0:
        raise ParameterError("value must be positive")
    hi = len(table) - 1 if hi is None else hi
    best = lo
    for i in range(lo, hi + 1):
        if abs(table[i] / value - 1) < abs(table[best] / value - 1):
            best = i
    return best


def exact_index(table: tuple[float, ...], value: float, rel_tol: float = 1e-6) -> int | None:
    for i, v in enumerate(table):
        if abs(v - value) <= rel_tol * max(abs(v), abs(value)):
            return i
    return None
