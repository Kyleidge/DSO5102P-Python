"""Timebase, run control and waveform acquisition.

Acquisition record download (command 0x02) is distinct from acquiring: the
instrument keeps one record per channel in memory and returns whatever is
there. To give a capture a trustworthy identity this module offers three modes:

``single``         arm Single-Sequence, observe the trigger state machine complete,
                   then download. The only mode that proves the record is new.
``stop_and_read``  stop acquisition, download the frozen record, optionally resume.
``running``        download while running (fastest; freshness not guaranteed).

Independently of mode, each record's SHA-1 is compared with the previous record of
the same channel: identical raw data is flagged as probably stale.
"""

from __future__ import annotations

import hashlib
import logging
import secrets
import time
from typing import Any, Literal

import numpy as np
import numpy.typing as npt

from hantek_mcp.analysis.quality import record_quality
from hantek_mcp.errors import (
    AcquisitionError,
    HantekError,
    NoSampleDataError,
    ParameterError,
    TriggerTimeoutError,
    UnsupportedOperationError,
)
from hantek_mcp.instrument.core import SettingResult
from hantek_mcp.instrument.triggering import TriggerControlMixin
from hantek_mcp.models import AcquisitionInfo, DataSource, Freshness, WaveformCapture, utcnow
from hantek_mcp.protocol.decoder import decode_screenshot
from hantek_mcp.protocol.keys import Key
from hantek_mcp.protocol.scaling import horizontal_scaling, vertical_scaling
from hantek_mcp.protocol.session import ProtocolSession
from hantek_mcp.protocol.settings import DSO5102P_TB_RANGE, TIMEBASE_S_PER_DIV, ScopeSettings

log = logging.getLogger(__name__)

CaptureMode = Literal["single", "stop_and_read", "running"]
CAPTURE_MODES: tuple[str, ...] = ("single", "stop_and_read", "running")
_ARMED_STATES = {"ready", "armed", "triggered"}


def allowed_timebases() -> list[float]:
    lo, hi = DSO5102P_TB_RANGE
    return list(TIMEBASE_S_PER_DIV[lo : hi + 1])


def _scaling_fields(st: ScopeSettings, ch: int) -> tuple[Any, ...]:
    c = st.channels[ch]
    return (
        c.vb_index,
        c.probe,
        c.position_counts,
        c.fine_vdiv,
        c.coupling,
        c.inverted,
        st.horizontal.tb_index,
        st.acquire.memory_depth,
        st.horizontal.window_enabled,
    )


class AcquisitionMixin(TriggerControlMixin):
    _last_hash: dict[int, str]
    _capture_seq: int

    def _init_acquisition(self) -> None:
        self._last_hash = {}
        self._capture_seq = 0

    # -- timebase --------------------------------------------------------------------------------

    def set_timebase(self, seconds_per_div: float, snap: bool = False) -> SettingResult:
        if seconds_per_div <= 0:
            raise ParameterError("seconds_per_div must be positive")
        lo, hi = DSO5102P_TB_RANGE
        exact = [i for i in range(lo, hi + 1) if abs(TIMEBASE_S_PER_DIV[i] / seconds_per_div - 1) < 1e-6]
        if exact:
            idx = exact[0]
        elif snap:
            idx = min(range(lo, hi + 1), key=lambda i: abs(TIMEBASE_S_PER_DIV[i] / seconds_per_div - 1))
        else:
            raise ParameterError(
                f"{seconds_per_div} s/div is not an available timebase (2-4-8 sequence, 4 ns .. 40 s)",
                details={"allowed_seconds_per_div": allowed_timebases()},
            )
        with self.operation("set_timebase") as s:
            st = self._refresh_settings(s)
            prev_idx = st.horizontal.tb_index
            presses = 0
            if idx != prev_idx:
                if self.write_method.startswith("direct"):
                    self.write_raw_fields(s, {"HORIZ-TB": idx})
                else:
                    key = Key.TIMEBASE_UP if idx > prev_idx else Key.TIMEBASE_DOWN
                    presses = self.press(s, key, abs(idx - prev_idx))
            notes = [] if exact else [f"snapped {seconds_per_div} s/div to {TIMEBASE_S_PER_DIV[idx]} s/div"]
            return self.verify(
                s,
                "timebase.seconds_per_div",
                TIMEBASE_S_PER_DIV[idx],
                st.horizontal.seconds_per_div,
                lambda x: x.horizontal.seconds_per_div,
                self.write_method,
                presses,
                notes,
            )

    # -- run control -----------------------------------------------------------------------------

    def run(self) -> dict[str, Any]:
        with self.operation("run") as s:
            s.set_acquisition(True)
            st = self._refresh_settings(s)
            self.emit("acquisition", action="run", trigger_state=st.trigger.state)
            return {
                "running_requested": True,
                "trigger_state": st.trigger.state,
                "method": "protocol command 0x12/00 (documented as 'somewhat buggy' on elinux)",
            }

    def stop(self) -> dict[str, Any]:
        with self.operation("stop") as s:
            s.set_acquisition(False)
            st = self._refresh_settings(s)
            self.emit("acquisition", action="stop", trigger_state=st.trigger.state)
            return {
                "stopped": st.trigger.state == "stop",
                "trigger_state": st.trigger.state,
                "method": "protocol command 0x12/00",
            }

    def arm_single(self) -> dict[str, Any]:
        with self.operation("arm_single") as s:
            self.press(s, Key.SINGLE_SEQ)
            st = self._refresh_settings(s)
            self.emit("acquisition", action="single_armed", trigger_state=st.trigger.state)
            return {"armed": True, "trigger_state": st.trigger.state, "method": "front_panel_key_emulation"}

    def acquisition_state(self, wait_s: float | None = None) -> dict[str, Any]:
        st = self.read_settings(wait_s=wait_s)
        state = st.trigger.state
        return {
            "trigger_state": state,
            "running": state not in ("stop", None),
            "waiting_for_trigger": state in ("ready", "armed"),
            "trigger_mode": st.trigger.mode,
            "trigger_counter_hz": st.trigger.counter_frequency_hz,
            "busy_operation": self.busy_operation,
            "state_source": "TRIG-STATE field of the settings record (enumeration from dsoc-extended)",
        }

    def wait_for_trigger(self, timeout_s: float) -> dict[str, Any]:
        """Wait until the instrument reports a triggered or completed acquisition (does not download)."""
        self._check_wait(timeout_s)
        with self.operation("wait_for_trigger") as s:
            t0 = time.monotonic()
            seen: list[str] = []
            while True:
                self.check_abort()
                state = self._refresh_settings(s).trigger.state or "unknown"
                if not seen or seen[-1] != state:
                    seen.append(state)
                if state == "triggered" or (state == "stop" and len(seen) > 1):
                    return {
                        "triggered": True,
                        "trigger_state": state,
                        "states_seen": seen,
                        "waited_s": round(time.monotonic() - t0, 4),
                    }
                if state == "stop" and len(seen) == 1:
                    return {
                        "triggered": False,
                        "trigger_state": state,
                        "states_seen": seen,
                        "waited_s": 0.0,
                        "note": "acquisition is stopped; arm single or run first",
                    }
                if time.monotonic() - t0 > timeout_s:
                    raise TriggerTimeoutError(
                        f"no trigger within {timeout_s} s",
                        details={"states_seen": seen, "timeout_s": timeout_s},
                    )
                self._poll_sleep()

    def _check_wait(self, timeout_s: float) -> None:
        limit = self.config.limits.max_trigger_wait_s
        if not 0 < timeout_s <= limit:
            raise ParameterError(f"timeout_s must be in (0, {limit}]")

    def _poll_sleep(self) -> None:
        time.sleep(0.002 if self.simulated else 0.05)

    def _arm_and_wait(
        self, s: ProtocolSession, timeout_s: float, force_on_timeout: bool, probe_channel: int
    ) -> tuple[Freshness, str | None, str | None, bool, float, list[str], str | None]:
        """Arm Single-Sequence and wait for completion.

        Returns ``(freshness, state_before, state_after, trigger_observed, wait_s, warnings,
        pre_hash)``. When the scope was already stopped, the current record of ``probe_channel``
        is hashed first (``pre_hash``) so that a completion without an observed armed state can
        still be checked for a content change by the caller.
        """
        st0 = self._refresh_settings(s)
        before = st0.trigger.state
        pre_hash: str | None = None
        if before == "stop":
            try:
                pre = s.read_samples(probe_channel)
                pre_hash = hashlib.sha1(pre.tobytes(), usedforsecurity=False).hexdigest()
            except NoSampleDataError:
                pre_hash = None
        self.press(s, Key.SINGLE_SEQ)
        t_start = time.monotonic()
        deadline = t_start + timeout_s
        observed_armed = False
        forced = False
        warnings: list[str] = []
        while True:
            self.check_abort()
            st = self._refresh_settings(s)
            state = st.trigger.state
            if state in _ARMED_STATES:
                observed_armed = True
            elif state == "stop":
                if forced:
                    fresh = Freshness.FORCED_NEW
                    warnings.append("no trigger occurred; the record was forced and is not trigger-aligned")
                elif observed_armed:
                    fresh = Freshness.NEW_TRIGGER_CONFIRMED
                else:
                    fresh = Freshness.STOPPED_SNAPSHOT
                    warnings.append(
                        "single-sequence completed before the armed state was observed; freshness is "
                        "established by comparing the record with the one in memory before arming"
                        if pre_hash
                        else "single-sequence completed before the armed state was observed; the record "
                        "could not be confirmed as new"
                    )
                wait_s = time.monotonic() - t_start
                return fresh, before, state, observed_armed and not forced, wait_s, warnings, pre_hash
            if time.monotonic() > deadline:
                if force_on_timeout and not forced:
                    self.press(s, Key.FORCE_TRIG)
                    forced = True
                    deadline = time.monotonic() + min(2.0, timeout_s)  # time for the forced record
                    continue
                raise TriggerTimeoutError(
                    f"no trigger within {timeout_s} s (trigger state {state!r}); the instrument is left armed. "
                    "Check trigger source/level/mode, use force_on_timeout, or force the trigger.",
                    details={
                        "trigger_state": state,
                        "timeout_s": timeout_s,
                        "trigger_level_counts": st.trigger.level_counts,
                        "trigger_source": st.trigger.source,
                    },
                )
            self._poll_sleep()

    # -- capture ---------------------------------------------------------------------------------

    def capture(
        self,
        channel: int,
        mode: str = "single",
        timeout_s: float = 5.0,
        force_on_timeout: bool = False,
        resume: bool = True,
        label: str | None = None,
    ) -> WaveformCapture:
        return self.capture_channels([channel], mode, timeout_s, force_on_timeout, resume, label)[0]

    def capture_channels(
        self,
        channels: list[int],
        mode: str = "single",
        timeout_s: float = 5.0,
        force_on_timeout: bool = False,
        resume: bool = True,
        label: str | None = None,
    ) -> list[WaveformCapture]:
        for ch in channels:
            if ch not in (1, 2):
                raise ParameterError(f"channel must be 1 or 2 (got {ch})")
        if not channels or len(set(channels)) != len(channels):
            raise ParameterError("channels must be a non-empty list without duplicates")
        if mode not in CAPTURE_MODES:
            raise ParameterError(f"mode must be one of {list(CAPTURE_MODES)}")
        self._check_wait(timeout_s)
        with self.operation(f"capture_{mode}") as s:
            warnings: list[str] = []
            restart = False
            observed = False
            wait_s: float | None = None
            pre_hash: str | None = None
            if mode == "single":
                fresh, before, after, observed, wait_s, w, pre_hash = self._arm_and_wait(
                    s, timeout_s, force_on_timeout, channels[0]
                )
                warnings += w
            elif mode == "stop_and_read":
                before = self._refresh_settings(s).trigger.state
                s.set_acquisition(False)
                if not self.simulated:
                    time.sleep(0.1)
                after = self._refresh_settings(s).trigger.state
                restart = resume and before != "stop"
                if after == "stop":
                    fresh = Freshness.STOPPED_SNAPSHOT
                else:
                    fresh = Freshness.RUNNING_SNAPSHOT
                    warnings.append(f"stop command did not stop acquisition (state {after!r})")
            else:
                before = after = self._refresh_settings(s).trigger.state
                fresh = Freshness.STOPPED_SNAPSHOT if before == "stop" else Freshness.RUNNING_SNAPSHOT
                if fresh == Freshness.RUNNING_SNAPSHOT:
                    warnings.append(
                        "downloaded while running: the record may change during download or be stale"
                    )
            try:
                caps = self._download(
                    s, channels, mode, fresh, before, after, observed, wait_s, warnings, label
                )
                if mode == "single" and fresh == Freshness.STOPPED_SNAPSHOT and pre_hash is not None:
                    self._check_single_changed(caps, pre_hash)
            finally:
                if restart:
                    try:
                        s.set_acquisition(True)
                    except HantekError as e:
                        log.warning("could not resume acquisition: %s", e)
            return caps

    def _download(
        self,
        s: ProtocolSession,
        channels: list[int],
        mode: str,
        fresh: Freshness,
        before: str | None,
        after: str | None,
        observed: bool,
        wait_s: float | None,
        warnings: list[str],
        label: str | None,
    ) -> list[WaveformCapture]:
        locked = False
        if self.config.control.lock_panel_during_capture:
            try:
                s.set_panel_lock(True)
                locked = True
            except HantekError as e:
                warnings.append(f"could not lock the front panel during settings read: {e.code}")
        try:
            st = self._refresh_settings(s)
        finally:
            if locked:
                s.set_panel_lock(False)
        t0 = time.monotonic()
        raws: dict[int, npt.NDArray[np.int8]] = {}
        for ch in channels:
            self.check_abort()
            raws[ch] = s.read_samples(ch)
        download_s = time.monotonic() - t0
        st_after = self._refresh_settings(s)
        group = f"grp-{secrets.token_hex(4)}" if len(channels) > 1 else None
        if len(channels) > 1:
            simult = (
                "same_stopped_acquisition: the instrument samples both channels simultaneously; the stopped "
                "record was downloaded channel by channel"
                if fresh != Freshness.RUNNING_SNAPSHOT
                else "not_guaranteed: downloaded while running, channels may come from different acquisitions"
            )
        else:
            simult = None
        out = []
        for ch in channels:
            changed = _scaling_fields(st, ch) != _scaling_fields(st_after, ch)
            info = AcquisitionInfo(
                mode=mode,
                freshness=fresh,
                trigger_state_before=before,
                trigger_state_after=after,
                trigger_observed=observed,
                settings_changed_during_capture=changed,
                group_id=group,
                simultaneity=simult,
                wait_s=None if wait_s is None else round(wait_s, 4),
                download_s=round(download_s, 4),
            )
            cap = self._build_capture(ch, raws[ch], st, info, list(warnings), label)
            if changed:
                cap.warnings.append(
                    "scaling-relevant settings changed during the download; scaling may be wrong"
                )
                cap.quality["valid"] = False
            out.append(cap)
        return out

    def _build_capture(
        self,
        ch: int,
        raw: npt.NDArray[np.int8],
        st: ScopeSettings,
        info: AcquisitionInfo,
        warnings: list[str],
        label: str | None,
    ) -> WaveformCapture:
        c = st.channels[ch]
        vert = vertical_scaling(
            c.volts_per_div,
            c.probe,
            c.position_counts,
            c.fine_vdiv,
            c.coupling,
            c.inverted,
            self.calibration,
            self.simulated,
        )
        horiz = horizontal_scaling(
            raw.size,
            st.horizontal.seconds_per_div,
            st.acquire.memory_depth,
            self.calibration,
            self.simulated,
            st.horizontal.trigger_time_raw,
        )
        if st.horizontal.window_enabled:
            warnings.append(
                "window (zoom) timebase is active: elinux reports only the visible part is transferred; "
                "time scaling is unreliable"
            )
        if st.acquire.mode not in ("normal", None):
            warnings.append(
                f"acquisition mode {st.acquire.mode!r}: record semantics unverified (normal mode tested)"
            )
        digest = hashlib.sha1(raw.tobytes(), usedforsecurity=False).hexdigest()
        prev = self._last_hash.get(ch)
        identical = None if prev is None else prev == digest
        self._last_hash[ch] = digest
        quality = record_quality(raw, identical)
        if info.freshness in (Freshness.RUNNING_SNAPSHOT,) and identical:
            quality["valid"] = False
        warnings = warnings + list(quality.get("warnings", []))
        if self.simulated:
            warnings.insert(
                0, "SIMULATED DATA: produced by the built-in simulator, not a physical instrument"
            )
        self._capture_seq += 1
        now = utcnow()
        cid = f"{'sim' if self.simulated else 'hw'}-{now:%Y%m%d-%H%M%S}-{self._capture_seq:04d}-{secrets.token_hex(2)}-ch{ch}"
        cap = WaveformCapture(
            capture_id=cid,
            channel=ch,
            timestamp_utc=now,
            raw_samples=raw,
            time_s=horiz.time_axis(raw.size),
            voltage_v=vert.to_volts(raw),
            source=DataSource.SIMULATOR if self.simulated else DataSource.HARDWARE,
            horizontal=horiz.as_dict(),
            vertical=vert.as_dict(),
            channel_settings=c.as_dict(),
            trigger_settings=st.trigger.as_dict(),
            timebase_settings=st.horizontal.as_dict(),
            acquire_settings=st.acquire.as_dict(),
            acquisition=info,
            quality=quality,
            instrument={
                "model": self.model_name,
                "firmware": self.sys_info.get("soft version"),
                "backend": self.config.backend,
            },
            label=label,
            warnings=warnings,
        )
        self.emit(
            "capture",
            capture_id=cid,
            channel=ch,
            samples=raw.size,
            freshness=str(info.freshness),
            simulated=self.simulated,
        )
        return cap

    def _check_single_changed(self, caps: list[WaveformCapture], pre_hash: str) -> None:
        """Decide freshness of a single sequence whose armed state was never observed."""
        first = caps[0]
        if first.raw_sha1 == pre_hash:
            raise AcquisitionError(
                "Single-Sequence did not produce a new record: the record is byte-identical to the one in "
                "memory before arming (the key press may not have been applied). Retry the capture.",
                details={
                    "channel": first.channel,
                    "trigger_state_after": first.acquisition.trigger_state_after,
                },
            )
        for c in caps:
            c.acquisition.freshness = Freshness.NEW_RECORD_CONTENT_CHANGED
            c.warnings = [w for w in c.warnings if "armed state was observed" not in w]
            c.warnings.append(
                "trigger armed state not observed (fast completion); the record differs from the pre-arm record"
            )

    # -- misc ------------------------------------------------------------------------------------

    def screenshot(self) -> tuple[npt.NDArray[np.uint8], str]:
        with self.operation("screenshot") as s:
            return decode_screenshot(s.screenshot_raw())

    def front_panel_autoset(self) -> dict[str, Any]:
        """Press the instrument's own AUTOSET key and report the resulting settings (read back)."""
        with self.operation("autoset") as s:
            before = self._refresh_settings(s).as_dict()
            self.press(s, Key.AUTOSET)
            time.sleep(0.0 if self.simulated else 3.0)  # instrument autoset takes a few seconds
            after = self._refresh_settings(s).as_dict()
            return {
                "method": "instrument_autoset_key",
                "settings_before": before,
                "settings_after": after,
                "note": "settings_after is read back from the instrument",
            }

    def default_setup(self, confirm: bool) -> dict[str, Any]:
        if not self.config.control.allow_disruptive_operations:
            raise UnsupportedOperationError(
                "factory Default Setup is disabled; enable control.allow_disruptive_operations in the config"
            )
        if not confirm:
            raise ParameterError(
                "Default Setup resets every instrument setting; pass confirm=true to proceed"
            )
        with self.operation("default_setup") as s:
            self.press(s, Key.DEFAULT_SETUP, allow_disruptive=True)
            time.sleep(0.0 if self.simulated else 3.0)
            return {"default_setup": True, "settings": self._refresh_settings(s).as_dict()}

    def beep(self, duration_ms: int = 100) -> dict[str, Any]:
        with self.operation("beep") as s:
            s.beep(duration_ms)
            return {"beeped_ms": max(100, duration_ms // 100 * 100)}
