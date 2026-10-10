"""Vertical (channel) controls.

The DSO5102P protocol offers no documented per-setting command, so settings are
changed by emulating front-panel keys (command 0x13) and then *verified by
reading the settings record back*. Knob-style settings (V/div, position) are
stepped; soft-menu settings (coupling, probe) are cycled with F-keys inside the
channel menu - the F-key mapping (F1 coupling, F4 probe) comes from
dsoc-extended on a DSO5202B and is unverified on a DSO5102P, which is exactly
why every change is read back before success is reported.
"""

from __future__ import annotations

from typing import Any

from hantek_mcp.errors import ParameterError, SettingNotConfirmedError
from hantek_mcp.instrument.core import ControllerCore, SettingResult
from hantek_mcp.protocol.keys import Key, channel_keys
from hantek_mcp.protocol.session import ProtocolSession
from hantek_mcp.protocol.settings import (
    COUPLINGS,
    DSO5102P_VB_RANGE,
    PROBE_FACTORS,
    VOLTS_PER_DIV,
    ScopeSettings,
)

MAX_POSITION_DIV = 10.0


def validate_channel(channel: int) -> int:
    if channel not in (1, 2):
        raise ParameterError(f"channel must be 1 or 2 (got {channel})", details={"allowed": [1, 2]})
    return channel


def allowed_volts_per_div(probe: int) -> list[float]:
    lo, hi = DSO5102P_VB_RANGE
    return [round(VOLTS_PER_DIV[i] * probe, 9) for i in range(lo, hi + 1)]


class ChannelControlMixin(ControllerCore):
    def get_channel(self, channel: int) -> dict[str, Any]:
        validate_channel(channel)
        st = self.read_settings()
        return st.channels[channel].as_dict()

    def _open_channel_menu(self, s: ProtocolSession, channel: int) -> int:
        """Show the channel menu without leaving the channel disabled.

        Pressing CHx MENU while that menu is already shown turns the channel off, so the
        enabled flag is checked afterwards and restored with a second press if needed.
        """
        keys = channel_keys(channel)
        presses = self.press(s, keys["menu"])
        st = self._refresh_settings(s)
        if not st.channels[channel].enabled:
            presses += self.press(s, keys["menu"])
            st = self._refresh_settings(s)
            if not st.channels[channel].enabled:
                raise SettingNotConfirmedError(f"could not open the CH{channel} menu with the channel enabled")
        return presses

    def set_channel_enabled(self, channel: int, enabled: bool) -> SettingResult:
        validate_channel(channel)
        with self.operation("set_channel_enabled") as s:
            st = self._refresh_settings(s)
            prev = st.channels[channel].enabled
            presses = 0
            if prev != enabled:
                if self.write_method.startswith("direct"):
                    self.write_raw_fields(s, {f"VERT-CH{channel}-DISP": int(enabled)})
                else:
                    menu = channel_keys(channel)["menu"]
                    presses += self.press(s, menu)
                    if not enabled:
                        st = self._refresh_settings(s)
                        if st.channels[channel].enabled:
                            presses += self.press(s, menu)
            return self.verify(s, f"CH{channel}.enabled", enabled, prev,
                               lambda x: x.channels[channel].enabled, self.write_method, presses)  # fmt: skip

    def set_volts_per_div(self, channel: int, volts_per_div: float, snap: bool = False) -> SettingResult:
        """Set the vertical scale as shown on screen (i.e. at the probe tip, including attenuation)."""
        validate_channel(channel)
        if volts_per_div <= 0:
            raise ParameterError("volts_per_div must be positive")
        with self.operation("set_volts_per_div") as s:
            st = self._refresh_settings(s)
            c = st.channels[channel]
            probe = c.probe or 1
            if c.fine_vdiv:
                raise SettingNotConfirmedError(
                    f"CH{channel} is in fine V/div mode; switch it to coarse on the instrument first"
                )
            allowed = allowed_volts_per_div(probe)
            target_bnc = volts_per_div / probe
            lo, hi = DSO5102P_VB_RANGE
            exact = [i for i in range(lo, hi + 1) if abs(VOLTS_PER_DIV[i] - target_bnc) <= 1e-9 * target_bnc + 1e-12]
            if exact:
                idx = exact[0]
            elif snap:
                idx = min(range(lo, hi + 1), key=lambda i: abs(VOLTS_PER_DIV[i] / target_bnc - 1))
            else:
                raise ParameterError(
                    f"{volts_per_div} V/div is not available with a {probe}x probe",
                    details={"allowed_volts_per_div": allowed, "probe_attenuation": probe},
                )
            prev_idx = c.vb_index
            presses = 0
            if idx != prev_idx:
                if self.write_method.startswith("direct"):
                    self.write_raw_fields(s, {f"VERT-CH{channel}-VB": idx})
                else:
                    keys = channel_keys(channel)
                    key = keys["vdiv_up"] if idx > prev_idx else keys["vdiv_down"]
                    presses = self.press(s, key, abs(idx - prev_idx))
            requested = round(VOLTS_PER_DIV[idx] * probe, 9)

            def observe(x: ScopeSettings) -> float | None:
                ch = x.channels[channel]
                return None if ch.volts_per_div is None else round(ch.volts_per_div * (ch.probe or 1), 9)

            prev_val = None if c.volts_per_div is None else round(c.volts_per_div * probe, 9)
            notes = [] if not snap or exact else [f"snapped {volts_per_div} V/div to nearest {requested} V/div"]
            return self.verify(s, f"CH{channel}.volts_per_div", requested, prev_val, observe,
                               self.write_method, presses, notes)  # fmt: skip

    def set_coupling(self, channel: int, coupling: str) -> SettingResult:
        validate_channel(channel)
        coupling = coupling.upper()
        if coupling not in COUPLINGS:
            raise ParameterError(f"coupling must be one of {list(COUPLINGS)}")
        with self.operation("set_coupling") as s:
            st = self._refresh_settings(s)
            prev = st.channels[channel].coupling
            presses = 0
            if prev != coupling:
                if self.write_method.startswith("direct"):
                    self.write_raw_fields(s, {f"VERT-CH{channel}-COUP": COUPLINGS.index(coupling)})
                else:
                    cur = COUPLINGS.index(prev) if prev in COUPLINGS else 0
                    n = (COUPLINGS.index(coupling) - cur) % len(COUPLINGS)
                    presses += self._open_channel_menu(s, channel)
                    presses += self.press(s, Key.F1, n)
            return self.verify(s, f"CH{channel}.coupling", coupling, prev,
                               lambda x: x.channels[channel].coupling, self.write_method, presses)  # fmt: skip

    def set_probe(self, channel: int, attenuation: int) -> SettingResult:
        validate_channel(channel)
        if attenuation not in PROBE_FACTORS:
            raise ParameterError(f"probe attenuation must be one of {list(PROBE_FACTORS)}")
        with self.operation("set_probe") as s:
            st = self._refresh_settings(s)
            prev = st.channels[channel].probe
            presses = 0
            if prev != attenuation:
                if self.write_method.startswith("direct"):
                    self.write_raw_fields(s, {f"VERT-CH{channel}-PROBE": PROBE_FACTORS.index(attenuation)})
                else:
                    cur = PROBE_FACTORS.index(prev) if prev in PROBE_FACTORS else 0
                    n = (PROBE_FACTORS.index(attenuation) - cur) % len(PROBE_FACTORS)
                    presses += self._open_channel_menu(s, channel)
                    presses += self.press(s, Key.F4, n)
            return self.verify(s, f"CH{channel}.probe_attenuation", attenuation, prev,
                               lambda x: x.channels[channel].probe, self.write_method, presses,
                               ["probe attenuation only rescales readings; it must match the physical probe"])  # fmt: skip

    def set_position(self, channel: int, divisions: float, tolerance_div: float = 0.04) -> SettingResult:
        """Move the channel's vertical offset (ground marker) to ``divisions`` from centre."""
        validate_channel(channel)
        if abs(divisions) > MAX_POSITION_DIV:
            raise ParameterError(f"position must be within ±{MAX_POSITION_DIV} divisions")
        cpd = self.calibration.counts_per_div
        target = round(divisions * cpd)
        tol = max(1, round(tolerance_div * cpd))
        with self.operation("set_position") as s:
            st = self._refresh_settings(s)
            prev = st.channels[channel].position_counts
            presses = 0
            if self.write_method.startswith("direct"):
                self.write_raw_fields(s, {f"VERT-CH{channel}-POS": target})
            elif target == 0:
                presses += self.press(s, channel_keys(channel)["pos_zero"])
            else:
                presses += self._converge(
                    s,
                    lambda x: x.channels[channel].position_counts,
                    target,
                    tol,
                    channel_keys(channel)["pos_up"],
                    channel_keys(channel)["pos_down"],
                    f"CH{channel} position",
                )
            st = self._refresh_settings(s)
            got = st.channels[channel].position_counts
            if abs(got - target) > tol:
                raise SettingNotConfirmedError(
                    f"CH{channel} position: requested {target} counts, instrument reports {got}",
                    details={"requested_counts": target, "observed_counts": got},
                )
            res = SettingResult(f"CH{channel}.position_divisions", divisions, prev / cpd, got / cpd,
                                self.write_method, "readback_confirmed_within_tolerance", presses,
                                [f"tolerance ±{tol / cpd:.3f} div; counts/div={cpd:g} (nominal)"])  # fmt: skip
            self.emit("setting_changed", **res.as_dict())
            return res

    def _converge(
        self,
        s: ProtocolSession,
        observe: Any,
        target: int,
        tol: int,
        up: Key,
        down: Key,
        what: str,
        chunk: int = 25,
    ) -> int:
        """Step a knob towards ``target`` re-reading the settings between batches."""
        presses = 0
        last_delta: int | None = None
        stalls = 0
        limit = self.config.limits.max_key_presses_per_operation
        while True:
            self.check_abort()
            cur = observe(self._refresh_settings(s))
            delta = target - cur
            if abs(delta) <= tol:
                return presses
            if last_delta is not None and abs(delta) >= abs(last_delta):
                stalls += 1
                if stalls >= 2:
                    raise SettingNotConfirmedError(
                        f"{what}: knob emulation made no progress (at {cur}, target {target})",
                        details={"observed": cur, "target": target},
                    )
            last_delta = delta
            n = min(abs(delta), chunk, limit - presses)
            if n <= 0:
                raise SettingNotConfirmedError(f"{what}: key press limit reached before converging")
            presses += self.press(s, up if delta > 0 else down, n)
