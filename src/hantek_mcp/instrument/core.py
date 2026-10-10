"""Controller core: connection lifecycle, serialisation, settings cache, events and errors."""

from __future__ import annotations

import contextlib
import logging
import threading
import time
from collections import deque
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from hantek_mcp import __version__
from hantek_mcp.config import AppConfig
from hantek_mcp.errors import (
    AbortedError,
    DeviceBusyError,
    DeviceDisconnectedError,
    HantekError,
    LimitExceededError,
    NotConnectedError,
    SettingNotConfirmedError,
    UnsupportedOperationError,
)
from hantek_mcp.instrument.ownership import OwnershipLock
from hantek_mcp.models import utcnow
from hantek_mcp.protocol import keys as keymod
from hantek_mcp.protocol.decoder import parse_sys_inf
from hantek_mcp.protocol.keys import Key
from hantek_mcp.protocol.scaling import ScalingCalibration
from hantek_mcp.protocol.session import ProtocolSession, SessionTimeouts
from hantek_mcp.protocol.settings import (
    ScopeSettings,
    SettingsLayout,
    decode_settings,
    default_layout,
    encode_settings,
    interpret_settings,
    parse_key_names,
    parse_layout,
)
from hantek_mcp.transport.base import Transport

log = logging.getLogger(__name__)

TransportFactory = Callable[[AppConfig], Transport]
Listener = Callable[[dict[str, Any]], None]


class ConnectionState(StrEnum):
    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    LOST = "lost"  # device vanished; explicit reconnect required


@dataclass
class SettingResult:
    setting: str
    requested: Any
    previous: Any
    confirmed: Any
    method: str
    verification: str
    key_presses: int = 0
    notes: list[str] | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "setting": self.setting,
            "requested": self.requested,
            "previous": self.previous,
            "confirmed_value": self.confirmed,
            "method": self.method,
            "verification": self.verification,
            "key_presses": self.key_presses,
            "notes": list(self.notes or []),
        }


def default_transport_factory(config: AppConfig) -> Transport:
    if config.backend == "simulator":
        from hantek_mcp.transport.simulator import SimulatedTransport

        return SimulatedTransport(seed=config.simulator.seed)
    from hantek_mcp.transport.usb import UsbSelector, UsbTransport

    u = config.usb
    return UsbTransport(
        UsbSelector(
            vendor_id=u.vendor_id,
            product_id=u.product_id,
            bus=u.bus,
            address=u.address,
            index=u.index,
            interface=u.interface,
            endpoint_out=u.endpoint_out,
            endpoint_in=u.endpoint_in,
        )
    )


class ControllerCore:
    """State and primitives shared by the channel, trigger and acquisition mixins."""

    def __init__(self, config: AppConfig, transport_factory: TransportFactory | None = None) -> None:
        self.config = config
        self._factory = transport_factory or default_transport_factory
        self.lock = threading.RLock()
        self.abort_event = threading.Event()
        self.transport: Transport | None = None
        self.session: ProtocolSession | None = None
        self.state = ConnectionState.DISCONNECTED
        self.layout: SettingsLayout = default_layout()
        self.layout_from_instrument = False
        self.key_names_match: bool | None = None
        self.sys_info: dict[str, str] = {}
        self.model_name: str | None = None
        self.connected_at: str | None = None
        self._settings: ScopeSettings | None = None
        self._settings_time = 0.0
        self.recent_errors: deque[dict[str, Any]] = deque(maxlen=100)
        self.activity: deque[dict[str, Any]] = deque(maxlen=500)
        self._listeners: list[Listener] = []
        self._busy_op: str | None = None
        # Abort handling: request_abort() bumps a generation counter; the outermost running operation
        # (or workflow) captured the generation when it started and aborts when it changes. Later
        # operations start with the new generation, so an abort never "sticks".
        self._abort_gen = 0
        self._token: int | None = None
        self._depth = 0
        self._workflow: str | None = None
        self._ownership = OwnershipLock(
            config.storage.data_dir, f"usb-{config.usb.vendor_id:04x}-{config.usb.product_id:04x}"
        )
        self.calibration = ScalingCalibration(
            counts_per_div=config.scaling.counts_per_div,
            samples_per_div_override=config.scaling.samples_per_div_override,
            voltage_verified=config.scaling.voltage_verified,
            time_verified=config.scaling.time_verified,
        )

    # -- events ----------------------------------------------------------------------------------

    def add_listener(self, fn: Listener) -> None:
        self._listeners.append(fn)

    def remove_listener(self, fn: Listener) -> None:
        with contextlib.suppress(ValueError):
            self._listeners.remove(fn)

    def emit(self, kind: str, **data: Any) -> None:
        event = {"time_utc": utcnow().isoformat(), "kind": kind, **data}
        self.activity.append(event)
        for fn in list(self._listeners):
            try:
                fn(event)
            except Exception:  # listeners must never break instrument control
                log.exception("event listener failed")

    def record_error(self, op: str, err: BaseException) -> None:
        entry = {"time_utc": utcnow().isoformat(), "operation": op}
        if isinstance(err, HantekError):
            entry.update(err.to_dict())
        else:
            entry.update({"code": "INTERNAL_ERROR", "message": repr(err)})
        self.recent_errors.append(entry)
        self.emit("error", operation=op, code=entry["code"], message=entry["message"])

    # -- properties ------------------------------------------------------------------------------

    @property
    def simulated(self) -> bool:
        return self.config.backend == "simulator"

    @property
    def is_connected(self) -> bool:
        return self.state == ConnectionState.CONNECTED and self.session is not None

    @property
    def busy_operation(self) -> str | None:
        return self._busy_op

    def require_session(self) -> ProtocolSession:
        if self.state == ConnectionState.LOST:
            raise NotConnectedError(
                "the instrument was disconnected; call connect() to re-establish the link"
            )
        if self.session is None or self.state != ConnectionState.CONNECTED:
            raise NotConnectedError("not connected to the instrument; call connect() first")
        return self.session

    @contextlib.contextmanager
    def operation(self, name: str, wait_s: float | None = None) -> Iterator[ProtocolSession]:
        """Serialise an instrument operation and translate failures consistently.

        ``wait_s`` bounds how long to wait for another operation to finish (default:
        ``limits.max_lock_wait_s``); status-type callers use a short wait so they never
        block behind a long capture.
        """
        timeout = self.config.limits.max_lock_wait_s if wait_s is None else wait_s
        if not self.lock.acquire(timeout=timeout):
            raise DeviceBusyError(
                f"instrument busy with {self._busy_op or self._workflow or 'another operation'!r}",
                details={"busy_operation": self._busy_op, "workflow": self._workflow, "waited_s": timeout},
            )
        try:
            session = self.require_session()
            outer = self._depth == 0
            if outer:
                self._busy_op = name
                if self._workflow is None:
                    self._begin_token()
            self._depth += 1
            try:
                yield session
            except DeviceDisconnectedError as e:
                self.record_error(name, e)
                self._mark_lost()
                raise
            except HantekError as e:
                if outer:
                    self.record_error(name, e)
                raise
            finally:
                self._depth -= 1
                if outer:
                    self._busy_op = None
                    if self._workflow is None:
                        self._token = None
        finally:
            self.lock.release()

    def _begin_token(self) -> None:
        self._token = self._abort_gen
        self.abort_event.clear()

    def request_abort(self) -> None:
        """Abort the running operation/workflow at its next check point (emergency stop for software
        activity; it cannot stop the instrument's own acquisition in progress)."""
        self._abort_gen += 1
        self.abort_event.set()
        self.emit("abort_requested", busy_operation=self._busy_op, workflow=self._workflow)

    def begin_workflow(self, name: str) -> None:
        if self._workflow is not None:
            raise UnsupportedOperationError(f"workflow {self._workflow!r} is already running")
        self._workflow = name
        self._begin_token()

    def end_workflow(self) -> None:
        self._workflow = None
        if self._depth == 0:
            self._token = None

    @property
    def active_workflow(self) -> str | None:
        return self._workflow

    def check_abort(self) -> None:
        if self._token is not None and self._token != self._abort_gen:
            raise AbortedError("operation aborted by request")

    # -- connection ------------------------------------------------------------------------------

    def connect(self) -> dict[str, Any]:
        with self.lock:
            if self.is_connected:
                return self.identify()
            self.state = ConnectionState.CONNECTING
            self.emit("connecting", backend=self.config.backend)
            try:
                if not self.simulated:
                    self._ownership.acquire(f"hantek-mcp {__version__}")
                transport = self._factory(self.config)
                transport.open()
                u = self.config.usb
                session = ProtocolSession(
                    transport,
                    SessionTimeouts(
                        default_s=u.timeout_s,
                        sample_s=u.sample_timeout_s,
                        screenshot_s=u.screenshot_timeout_s,
                    ),
                    command_delay_s=0.0 if self.simulated else u.command_delay_s,
                    max_retries=u.max_retries,
                )
                self.transport, self.session = transport, session
                session.flush()
                session.echo(b"hantek-mcp-sync")
                self._load_instrument_descriptors(session)
                self.state = ConnectionState.CONNECTED
                self.connected_at = utcnow().isoformat()
                self._refresh_settings(session)
            except BaseException as e:
                self.record_error("connect", e)
                self._teardown()
                self.state = ConnectionState.DISCONNECTED
                raise
            self.emit("connected", backend=self.config.backend, simulated=self.simulated)
            return self.identify()

    def _load_instrument_descriptors(self, session: ProtocolSession) -> None:
        try:
            text = session.read_file("/protocol.inf").decode("ascii", "replace")
            self.layout = (
                parse_layout(text, source="instrument:/protocol.inf") if text.strip() else default_layout()
            )
            self.layout_from_instrument = bool(text.strip())
        except HantekError as e:
            log.warning("could not read /protocol.inf (%s); using bundled DSO5102P layout", e)
            self.layout, self.layout_from_instrument = default_layout(), False
        try:
            names = parse_key_names(session.read_file("/keyprotocol.inf").decode("ascii", "replace"))
            self.key_names_match = tuple(names) == keymod.INF_NAMES
            if not self.key_names_match:
                log.warning("instrument key table differs from the DSO5102P table; key emulation disabled")
        except HantekError as e:
            log.warning("could not read /keyprotocol.inf (%s)", e)
            self.key_names_match = None
        try:
            self.sys_info = parse_sys_inf(session.read_file("/sys.inf").decode("ascii", "replace"))
        except HantekError as e:
            log.warning("could not read /sys.inf (%s)", e)
            self.sys_info = {}
        try:
            self.model_name = (
                session.read_file("/logotype.dis").split(b"\x00")[0].decode("ascii", "replace").strip()
            )
        except HantekError:
            self.model_name = None

    def disconnect(self) -> None:
        with self.lock:
            self._teardown()
            self.state = ConnectionState.DISCONNECTED
            self.emit("disconnected")

    def _teardown(self) -> None:
        if self.transport is not None:
            try:
                self.transport.close()
            except Exception:
                log.debug("transport close failed", exc_info=True)
        self.transport = None
        self.session = None
        self._settings = None
        self._ownership.release()

    def _mark_lost(self) -> None:
        self._teardown()
        self.state = ConnectionState.LOST
        self.emit("connection_lost")

    # -- settings --------------------------------------------------------------------------------

    def _refresh_settings(self, session: ProtocolSession) -> ScopeSettings:
        blob = session.read_settings_blob()
        raw, trailing = decode_settings(blob, self.layout)
        st = interpret_settings(raw, self.layout.source, len(trailing))
        self._settings, self._settings_time = st, time.monotonic()
        return st

    def read_settings(self, max_age_s: float = 0.0, wait_s: float | None = None) -> ScopeSettings:
        with self.operation("read_settings", wait_s) as s:
            if self._settings is not None and time.monotonic() - self._settings_time <= max_age_s:
                return self._settings
            return self._refresh_settings(s)

    @property
    def cached_settings(self) -> ScopeSettings | None:
        return self._settings

    # -- identity --------------------------------------------------------------------------------

    def identify(self) -> dict[str, Any]:
        info = self.transport.info().as_dict() if self.transport else {}
        return {
            "connected": self.is_connected,
            "backend": self.config.backend,
            "simulated": self.simulated,
            "model": self.model_name,
            "firmware": self.sys_info.get("soft version"),
            "fpga_version": self.sys_info.get("fpga version"),
            "dst_type": self.sys_info.get("DST type"),
            "settings_layout_source": self.layout.source,
            "settings_layout_fields": len(self.layout.fields),
            "key_table_matches_dso5102p": self.key_names_match,
            "transport": info,
            "serial_number": None,
            "serial_number_note": "the DSO5000 series reports iSerial=0; units cannot be told apart by serial",
        }

    # -- key emulation primitives ----------------------------------------------------------------

    def _ensure_keys_usable(self) -> None:
        if self.key_names_match is False:
            raise UnsupportedOperationError(
                "the instrument's /keyprotocol.inf does not match the DSO5102P key table; front-panel "
                "emulation is disabled to avoid pressing the wrong keys"
            )

    def press(
        self, session: ProtocolSession, key: Key, count: int = 1, allow_disruptive: bool = False
    ) -> int:
        """Press ``key`` ``count`` times, flushing each press with an echo (dsoc-extended found the
        instrument processes a key when the next command arrives) and waiting for it to settle."""
        self._ensure_keys_usable()
        if key in keymod.DISRUPTIVE_KEYS and not allow_disruptive:
            raise UnsupportedOperationError(
                f"key {key.name} is disruptive and requires explicit confirmation"
            )
        limit = self.config.limits.max_key_presses_per_operation
        if count > limit:
            raise LimitExceededError(
                f"operation would need {count} key presses (limit {limit})", details={"limit": limit}
            )
        delay = 0.0 if self.simulated else self.config.usb.key_press_delay_s
        for _ in range(count):
            self.check_abort()
            session.press_key(int(key))
            session.echo(b"flush")
            if delay:
                time.sleep(delay)
        self.emit("key_press", key=key.name, count=count)
        return count

    def write_raw_fields(self, session: ProtocolSession, changes: dict[str, int]) -> None:
        """EXPERIMENTAL direct settings write (command 0x11). Only used when configured."""
        current = self._refresh_settings(session)
        raw = dict(current.raw)
        raw.update(changes)
        status = session.write_settings_blob(encode_settings(raw, self.layout))
        if status != 0:
            raise SettingNotConfirmedError(f"instrument rejected settings write (status {status})")

    def verify(
        self,
        session: ProtocolSession,
        setting: str,
        requested: Any,
        previous: Any,
        observe: Callable[[ScopeSettings], Any],
        method: str,
        presses: int,
        notes: list[str] | None = None,
    ) -> SettingResult:
        st = self._refresh_settings(session)
        observed = observe(st)
        if observed != requested:
            raise SettingNotConfirmedError(
                f"{setting}: requested {requested!r} but the instrument reports {observed!r}",
                details={
                    "setting": setting,
                    "requested": requested,
                    "observed": observed,
                    "previous": previous,
                    "method": method,
                    "key_presses": presses,
                },
            )
        res = SettingResult(
            setting, requested, previous, observed, method, "readback_confirmed", presses, notes
        )
        self.emit("setting_changed", **res.as_dict())
        return res

    @property
    def write_method(self) -> str:
        if self.config.control.settings_write_mode == "direct_write_experimental":
            return "direct_settings_write_experimental"
        return "front_panel_key_emulation"
