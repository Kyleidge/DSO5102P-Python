"""Request/response protocol client over a :class:`Transport`.

The session owns the frame re-assembly buffer, serialises transactions with a
lock, applies timeouts, retries *idempotent* reads only, and keeps protocol
statistics for diagnostics.
"""

from __future__ import annotations

import contextlib
import logging
import os
import threading
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, TypeVar

import numpy as np
import numpy.typing as npt

from hantek_mcp.errors import (
    ChecksumError,
    DeviceDisconnectedError,
    HantekError,
    NotConnectedError,
    ProtocolError,
    ScopeTimeoutError,
    UnexpectedResponseError,
)
from hantek_mcp.protocol.commands import (
    RESPONSE_FLAG,
    Command,
    DebugCommand,
    Frame,
    FrameDecoder,
    Marker,
    PanelSubcommand,
    encode_frame,
)
from hantek_mcp.protocol.decoder import BulkTransfer, SampleTransfer, decode_system_time
from hantek_mcp.transport.base import Transport

log = logging.getLogger(__name__)
T = TypeVar("T")

#: Frames with an unexpected command are discarded (upstream "BADANSWER" behaviour) up to this limit.
MAX_STRAY_FRAMES = 64


@dataclass
class SessionStats:
    frames_sent: int = 0
    frames_received: int = 0
    bytes_sent: int = 0
    bytes_received: int = 0
    timeouts: int = 0
    checksum_errors: int = 0
    framing_errors: int = 0
    stray_frames: int = 0
    retries: int = 0
    flushed_bytes: int = 0
    last_command: str | None = None
    last_latency_ms: float | None = None
    commands: Counter[str] = field(default_factory=Counter)

    def as_dict(self) -> dict[str, Any]:
        d = {k: v for k, v in self.__dict__.items() if k != "commands"}
        d["commands"] = dict(self.commands)
        return d


@dataclass
class SessionTimeouts:
    default_s: float = 2.0
    sample_s: float = 10.0
    screenshot_s: float = 15.0
    flush_s: float = 0.05


class ProtocolSession:
    def __init__(
        self,
        transport: Transport,
        timeouts: SessionTimeouts | None = None,
        command_delay_s: float = 0.0,
        max_retries: int = 2,
    ) -> None:
        self.transport = transport
        self.timeouts = timeouts or SessionTimeouts()
        self.command_delay_s = command_delay_s
        self.max_retries = max_retries
        self.stats = SessionStats()
        self._decoder = FrameDecoder()
        self._frames: list[Frame] = []
        self._lock = threading.RLock()

    @property
    def lock(self) -> threading.RLock:
        return self._lock

    # -- low level -------------------------------------------------------------------------------

    def _send(self, command: int, payload: bytes, marker: Marker) -> None:
        if not self.transport.is_open:
            raise NotConnectedError("transport is not open")
        frame = encode_frame(command, payload, marker)
        if self.command_delay_s:
            time.sleep(self.command_delay_s)
        self.transport.write(frame, self.timeouts.default_s)
        self.stats.frames_sent += 1
        self.stats.bytes_sent += len(frame)
        name = _command_name(command, marker)
        self.stats.commands[name] += 1
        self.stats.last_command = name
        log.debug("-> %s %s", name, frame[:16].hex(" "))

    def _read_frame(self, deadline: float) -> Frame:
        while not self._frames:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self.stats.timeouts += 1
                raise ScopeTimeoutError("timed out waiting for a response frame")
            try:
                chunk = self.transport.read(0x10000, min(remaining, 1.0))
            except ScopeTimeoutError:
                continue
            self.stats.bytes_received += len(chunk)
            resyncs = self._decoder.resyncs
            try:
                self._frames.extend(self._decoder.feed(chunk))
            except ChecksumError:
                self.stats.checksum_errors += 1
                raise
            finally:
                self.stats.framing_errors += self._decoder.resyncs - resyncs
        frame = self._frames.pop(0)
        self.stats.frames_received += 1
        return frame

    def _expect(self, deadline: float, response_command: int) -> Frame:
        for _ in range(MAX_STRAY_FRAMES):
            frame = self._read_frame(deadline)
            if frame.command == response_command:
                return frame
            self.stats.stray_frames += 1
            log.warning(
                "discarding unexpected frame 0x%02X while waiting for 0x%02X", frame.command, response_command
            )
        raise UnexpectedResponseError(f"too many unexpected frames waiting for 0x{response_command:02X}")

    def flush(self) -> int:
        """Discard pending input (stale responses from an interrupted transaction)."""
        with self._lock:
            n = self._decoder.pending + sum(len(f.payload) for f in self._frames)
            self._decoder.clear()
            self._frames.clear()
            for _ in range(256):
                try:
                    chunk = self.transport.read(0x10000, self.timeouts.flush_s)
                except ScopeTimeoutError:
                    break
                n += len(chunk)
            self.stats.flushed_bytes += n
            if n:
                log.info("flushed %d stale byte(s)", n)
            return n

    def transact(
        self,
        command: int,
        payload: bytes = b"",
        marker: Marker = Marker.NORMAL,
        timeout_s: float | None = None,
    ) -> Frame:
        with self._lock:
            t0 = time.monotonic()
            self._send(command, payload, marker)
            frame = self._expect(t0 + (timeout_s or self.timeouts.default_s), command | RESPONSE_FLAG)
            self.stats.last_latency_ms = (time.monotonic() - t0) * 1000
            return frame

    def transact_multi(
        self, command: int, payload: bytes, consumer: Callable[[Frame], bool], timeout_s: float
    ) -> None:
        """Send a request and feed response frames to ``consumer`` until it returns True.

        The deadline is an inactivity timeout: it is extended whenever a frame arrives,
        so large transfers on slow links do not fail while data is still flowing.
        """
        with self._lock:
            t0 = time.monotonic()
            self._send(command, payload, Marker.NORMAL)
            inactivity = min(timeout_s, max(self.timeouts.default_s, 2.0))
            hard_deadline = t0 + timeout_s
            deadline = min(t0 + inactivity, hard_deadline)
            while True:
                frame = self._expect(deadline, command | RESPONSE_FLAG)
                if consumer(frame):
                    break
                deadline = min(time.monotonic() + inactivity, hard_deadline)
            self.stats.last_latency_ms = (time.monotonic() - t0) * 1000

    def with_retry(self, fn: Callable[[], T], idempotent: bool, what: str) -> T:
        """Run ``fn``; on timeout/protocol errors flush and retry if the operation is idempotent."""
        attempts = 1 + (self.max_retries if idempotent else 0)
        last: HantekError | None = None
        for i in range(attempts):
            try:
                return fn()
            except DeviceDisconnectedError:
                raise
            except (ScopeTimeoutError, ProtocolError) as e:
                last = e
                if i + 1 >= attempts:
                    break
                self.stats.retries += 1
                log.warning(
                    "%s failed (%s); resynchronising and retrying (%d/%d)", what, e, i + 1, attempts - 1
                )
                self.resync()
        assert last is not None
        if not idempotent:
            # The instrument may or may not have executed the command; resynchronise and report.
            with contextlib.suppress(HantekError):
                self.resync()
        raise last

    def resync(self) -> None:
        """Bring the response stream back in step with our requests.

        Draining for a short while is not enough: a reply delayed beyond the flush window
        would later be taken as the answer to the *next* request. The instrument answers in
        order, so an echo with a unique nonce is sent and every frame before its reply is
        discarded.
        """
        with self._lock:
            self.flush()
            nonce = b"resync-" + os.urandom(6)
            try:
                self._send(Command.ECHO, nonce, Marker.NORMAL)
                deadline = time.monotonic() + max(self.timeouts.default_s, 1.0)
                for _ in range(MAX_STRAY_FRAMES):
                    f = self._read_frame(deadline)
                    if f.command == Command.ECHO | RESPONSE_FLAG and f.payload == nonce:
                        return
                    self.stats.stray_frames += 1
            except DeviceDisconnectedError:
                raise
            except HantekError as e:
                log.warning("resynchronisation failed: %s", e)
                self.flush()

    # -- typed requests --------------------------------------------------------------------------

    def echo(self, data: bytes = b"PING") -> bytes:
        def op() -> bytes:
            f = self.transact(Command.ECHO, data)
            if f.payload != data:
                raise UnexpectedResponseError("echo reply does not match request")
            return f.payload

        return self.with_retry(op, idempotent=True, what="echo")

    def read_settings_blob(self) -> bytes:
        return self.with_retry(
            lambda: self.transact(Command.READ_SETTINGS).payload, idempotent=True, what="read settings"
        )

    def read_samples(self, channel: int) -> npt.NDArray[np.int8]:
        """Read the sample record for channel 1 or 2 (wire channel code 0 or 1)."""
        if channel not in (1, 2):
            raise ValueError("channel must be 1 or 2")
        code = channel - 1

        def op() -> npt.NDArray[np.int8]:
            tr = SampleTransfer(channel_code=code)
            self.transact_multi(Command.READ_SAMPLES, bytes([0x01, code]), tr.feed, self.timeouts.sample_s)
            return tr.samples()

        return self.with_retry(op, idempotent=True, what=f"read CH{channel} samples")

    def read_file(self, path: str) -> bytes:
        if not path.startswith("/") or "\x00" in path:
            raise ValueError("instrument file paths must be absolute")

        def op() -> bytes:
            tr = BulkTransfer(response_command=Command.READ_FILE | RESPONSE_FLAG)
            self.transact_multi(
                Command.READ_FILE, b"\x00" + path.encode("ascii"), tr.feed, self.timeouts.default_s * 3
            )
            return tr.data()

        return self.with_retry(op, idempotent=True, what=f"read file {path}")

    def screenshot_raw(self) -> bytes:
        def op() -> bytes:
            tr = BulkTransfer(response_command=Command.SCREENSHOT | RESPONSE_FLAG)
            self.transact_multi(Command.SCREENSHOT, b"", tr.feed, self.timeouts.screenshot_s)
            return tr.data()

        return self.with_retry(op, idempotent=True, what="screenshot")

    def read_system_time(self) -> datetime:
        f = self.with_retry(
            lambda: self.transact(Command.READ_SYSTEM_TIME), idempotent=True, what="read time"
        )
        return decode_system_time(f.payload)

    def set_acquisition(self, run: bool) -> None:
        payload = bytes([PanelSubcommand.ACQUISITION, 0x00 if run else 0x01])
        f = self.with_retry(lambda: self.transact(Command.PANEL, payload), idempotent=True, what="run/stop")
        if f.payload[:2] != payload:
            raise UnexpectedResponseError("run/stop reply does not echo the request")

    def set_panel_lock(self, locked: bool) -> None:
        payload = bytes([PanelSubcommand.LOCK, 0x01 if locked else 0x00])
        f = self.with_retry(lambda: self.transact(Command.PANEL, payload), idempotent=True, what="panel lock")
        if f.payload[:2] != payload:
            raise UnexpectedResponseError("panel lock reply does not echo the request")

    def press_key(self, code: int) -> int:
        """Emulate one key press. NOT idempotent: never retried automatically."""
        if not 0 <= code <= 0x30:
            raise ValueError(f"key code 0x{code:02X} out of range")
        f = self.with_retry(
            lambda: self.transact(Command.KEY_PRESS, bytes([code, 0x01])), idempotent=False, what="key press"
        )
        return f.payload[0] if f.payload else 0

    def write_settings_blob(self, blob: bytes) -> int:
        """EXPERIMENTAL command 0x11 (normal marker) - documented on elinux, unverified on DSO5102P."""
        f = self.with_retry(
            lambda: self.transact(Command.WRITE_SETTINGS, blob), idempotent=False, what="write settings"
        )
        return f.payload[0] if f.payload else -1

    def beep(self, duration_ms: int) -> None:
        if not 0 < duration_ms <= 5000:
            raise ValueError("beep duration must be 1..5000 ms")
        units = max(1, duration_ms // 100)
        self.with_retry(
            lambda: self.transact(DebugCommand.BEEP, bytes([units]), marker=Marker.DEBUG),
            idempotent=True,
            what="beep",
        )


def _command_name(command: int, marker: Marker) -> str:
    try:
        name = Command(command).name if marker == Marker.NORMAL else DebugCommand(command).name
    except ValueError:
        name = f"0x{command:02X}"
    return name
