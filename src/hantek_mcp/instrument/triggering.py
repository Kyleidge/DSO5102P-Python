"""Trigger controls (edge trigger only).

Trigger settings are changed through the trigger soft menu (F1 type, F2 source,
F3 slope, F4 mode, F5 coupling per dsoc-extended on a DSO5202B) and verified by
read-back. Other trigger types re-arrange that menu, so changes are refused
unless the instrument reports the edge trigger type.
"""

from __future__ import annotations

from typing import Any

from hantek_mcp.errors import ParameterError, UnsupportedOperationError
from hantek_mcp.instrument.channels import ChannelControlMixin
from hantek_mcp.instrument.core import SettingResult
from hantek_mcp.protocol.keys import Key
from hantek_mcp.protocol.settings import (
    TRIGGER_COUPLINGS,
    TRIGGER_MODES,
    TRIGGER_SLOPES,
    TRIGGER_SOURCES,
    ScopeSettings,
)

MAX_TRIGGER_LEVEL_DIV = 8.0  # DSO5000P manual: CH1/CH2 trigger level range ±8 divisions

_MENU_KEYS = {
    "source": (Key.F2, TRIGGER_SOURCES, "TRIG-SRC"),
    "slope": (Key.F3, TRIGGER_SLOPES, "TRIG-EDGE-SLOPE"),
    "mode": (Key.F4, TRIGGER_MODES, "TRIG-MODE"),
    "coupling": (Key.F5, TRIGGER_COUPLINGS, "TRIG-COUP"),
}


def normalise_source(source: str) -> str:
    s = source.strip().upper().replace("_", "/")
    aliases = {"EXT5": "EXT/5", "EXT/5": "EXT/5", "AC": "AC50", "LINE": "AC50", "AC/50": "AC50"}
    s = aliases.get(s, s)
    if s not in TRIGGER_SOURCES:
        raise ParameterError(f"trigger source must be one of {list(TRIGGER_SOURCES)}")
    return s


def normalise_trigger_coupling(c: str) -> str:
    key = c.strip().upper().replace(" ", "_").replace("-", "_")
    aliases = {
        "NOISE": "NOISE_REJECT",
        "NOISEREJ": "NOISE_REJECT",
        "HF": "HF_REJECT",
        "HFREJ": "HF_REJECT",
        "LF": "LF_REJECT",
        "LFREJ": "LF_REJECT",
    }
    key = aliases.get(key, key)
    if key not in TRIGGER_COUPLINGS:
        raise ParameterError(f"trigger coupling must be one of {list(TRIGGER_COUPLINGS)}")
    return key


class TriggerControlMixin(ChannelControlMixin):
    def get_trigger(self) -> dict[str, Any]:
        st = self.read_settings()
        d = st.trigger.as_dict()
        d["level_v"] = self._level_volts(st)
        d["level_v_note"] = (
            "level_v = level_counts * V/div(probe tip) / counts_per_div of the source channel "
            "(dsoc-extended formula; whether it is relative to the channel offset is unverified)"
        )
        d["counter_note"] = (
            "counter_frequency_hz is the instrument's own trigger frequency counter (TRIG-FREQUENCY*1e-3, "
            "scale from dsoc-extended); 0 typically means no trigger"
        )
        return d

    def _level_volts(self, st: ScopeSettings) -> float | None:
        src = st.trigger.source
        if src not in ("CH1", "CH2"):
            return None
        ch = st.channels[1 if src == "CH1" else 2]
        vpd = ch.effective_volts_per_div
        if vpd is None:
            return None
        return st.trigger.level_counts * vpd / self.calibration.counts_per_div

    def _require_edge(self, st: ScopeSettings) -> None:
        if st.trigger.type != "edge":
            raise UnsupportedOperationError(
                f"trigger type is {st.trigger.type!r}; only the edge trigger menu layout is supported. "
                "Select the Edge trigger type on the instrument first.",
                details={"trigger_type": st.trigger.type},
            )

    def _set_trigger_menu(self, what: str, value: str) -> SettingResult:
        key, table, field = _MENU_KEYS[what]
        with self.operation(f"set_trigger_{what}") as s:
            st = self._refresh_settings(s)
            self._require_edge(st)
            prev = getattr(st.trigger, what)
            presses = 0
            if prev != value:
                if self.write_method.startswith("direct"):
                    self.write_raw_fields(s, {field: table.index(value)})
                else:
                    cur = table.index(prev) if prev in table else 0
                    n = (table.index(value) - cur) % len(table)
                    presses += self.press(s, Key.TRIG_MENU)
                    presses += self.press(s, key, n)
            return self.verify(
                s,
                f"trigger.{what}",
                value,
                prev,
                lambda x: getattr(x.trigger, what),
                self.write_method,
                presses,
            )

    def set_trigger_source(self, source: str) -> SettingResult:
        return self._set_trigger_menu("source", normalise_source(source))

    def set_trigger_mode(self, mode: str) -> SettingResult:
        m = mode.strip().lower()
        if m not in TRIGGER_MODES:
            raise ParameterError(f"trigger mode must be one of {list(TRIGGER_MODES)}")
        return self._set_trigger_menu("mode", m)

    def set_trigger_slope(self, slope: str) -> SettingResult:
        sl = slope.strip().lower()
        sl = {"rise": "rising", "positive": "rising", "fall": "falling", "negative": "falling"}.get(sl, sl)
        if sl not in TRIGGER_SLOPES:
            raise ParameterError(f"trigger slope must be one of {list(TRIGGER_SLOPES)}")
        return self._set_trigger_menu("slope", sl)

    def set_trigger_coupling(self, coupling: str) -> SettingResult:
        return self._set_trigger_menu("coupling", normalise_trigger_coupling(coupling))

    def set_trigger_level(self, volts: float) -> SettingResult:
        with self.operation("set_trigger_level") as s:
            st = self._refresh_settings(s)
            self._require_edge(st)
            src = st.trigger.source
            if src not in ("CH1", "CH2"):
                raise UnsupportedOperationError(
                    f"absolute trigger level is only supported for CH1/CH2 (source {src})"
                )
            ch = st.channels[1 if src == "CH1" else 2]
            vpd = ch.effective_volts_per_div
            if vpd is None:
                raise UnsupportedOperationError("source channel V/div unknown (fine mode?)")
            cpd = self.calibration.counts_per_div
            target = round(volts * cpd / vpd)
            if abs(target) > MAX_TRIGGER_LEVEL_DIV * cpd:
                raise ParameterError(
                    f"{volts} V is outside the ±{MAX_TRIGGER_LEVEL_DIV} div trigger range at {vpd} V/div",
                    details={"min_v": -MAX_TRIGGER_LEVEL_DIV * vpd, "max_v": MAX_TRIGGER_LEVEL_DIV * vpd},
                )
            prev = st.trigger.level_counts
            presses = 0
            if self.write_method.startswith("direct"):
                self.write_raw_fields(s, {"TRIG-VPOS": target})
            elif target == 0:
                presses += self.press(s, Key.TRIG_LEVEL_ZERO)
            else:
                presses += self._converge(
                    s,
                    lambda x: x.trigger.level_counts,
                    target,
                    0,
                    Key.TRIG_LEVEL_UP,
                    Key.TRIG_LEVEL_DOWN,
                    "trigger level",
                )
            res = self.verify(
                s,
                "trigger.level_counts",
                target,
                prev,
                lambda x: x.trigger.level_counts,
                self.write_method,
                presses,
            )
            res.notes = [
                f"level quantised to {vpd / cpd:.4g} V steps; requested {volts} V -> {target * vpd / cpd:.4g} V",
                "volts conversion uses the nominal counts/div (unverified on DSO5102P)",
            ]
            return res

    def set_trigger_level_50(self) -> SettingResult:
        with self.operation("set_trigger_50") as s:
            st = self._refresh_settings(s)
            prev = st.trigger.level_counts
            presses = self.press(s, Key.TRIG_LEVEL_50)
            new = self._refresh_settings(s).trigger.level_counts
            return SettingResult(
                "trigger.level_counts",
                "50%",
                prev,
                new,
                "front_panel_key_emulation",
                "readback_reported",
                presses,
                ["instrument's 'set to 50%' (manual: works for signals >= 50 Hz)"],
            )

    def force_trigger(self) -> dict[str, Any]:
        with self.operation("force_trigger") as s:
            self.press(s, Key.FORCE_TRIG)
            st = self._refresh_settings(s)
            return {"forced": True, "trigger_state": st.trigger.state, "method": "front_panel_key_emulation"}
