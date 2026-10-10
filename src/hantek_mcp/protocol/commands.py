"""Frame encoding/decoding for the Hantek DSO5000-series USB protocol.

Frame layout (identical for requests and responses)::

    +--------+---------+---------+---------+---------------+----------+
    | marker | len LSB | len MSB | command | payload ...   | checksum |
    +--------+---------+---------+---------+---------------+----------+

* ``marker`` is ``0x53`` (normal) or ``0x43`` (debug).
* ``len`` counts the command byte, the payload and the checksum (i.e. it
  excludes the marker and the length field itself).
* responses echo the command with bit 7 set (``cmd | 0x80``).
* ``checksum`` is the low byte of the sum of every preceding byte.

References: elinux.org "Das Oszi Protocol" and the upstream rcr/dso5102p
implementation (``_SendCommand``/``_ReadAnswer``), both of which agree on
this layout. See ``docs/usb-protocol.md`` for the confirmed/inferred split.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum

from hantek_mcp.errors import ChecksumError, ForbiddenCommandError, FramingError

RESPONSE_FLAG = 0x80
#: Upper bound on a single frame's length field; the protocol uses a 16-bit length.
MAX_FRAME_LENGTH = 0xFFFF


class Marker(IntEnum):
    NORMAL = 0x53
    DEBUG = 0x43


class Command(IntEnum):
    """Normal (0x53) commands used by this project."""

    ECHO = 0x00
    READ_SETTINGS = 0x01
    READ_SAMPLES = 0x02
    READ_FILE = 0x10
    WRITE_SETTINGS = 0x11
    PANEL = 0x12
    KEY_PRESS = 0x13
    SCREENSHOT = 0x20
    READ_SYSTEM_TIME = 0x21


class DebugCommand(IntEnum):
    """Debug (0x43) commands this project is willing to send."""

    BEEP = 0x44


class PanelSubcommand(IntEnum):
    ACQUISITION = 0x00  # data byte 0x00 = start (run), 0x01 = stop
    LOCK = 0x01  # data byte 0x01 = lock, 0x00 = unlock


class BulkSubcommand(IntEnum):
    """Sub-command byte found at the start of multi-frame responses."""

    LENGTH = 0x00  # sample transfers only: 3-byte little-endian total length
    DATA = 0x01
    END = 0x02
    ERROR = 0x03  # sample transfers only: no data available / transfer error


#: Normal-marker commands that may be transmitted. Anything else is refused at the
#: encoder level so that no code path (MCP, GUI, scripts) can inject e.g. firmware or
#: file writes. Debug commands such as the remote shell (0x43/0x11), FPGA register
#: writes (0x43/0x42), firmware variable/calibration writes (0x43/0x40, 0x41), file
#: write/update (0x43/0x50, 0x60) and init (0x43/0x7F) are deliberately absent.
ALLOWED_NORMAL_COMMANDS: frozenset[int] = frozenset(int(c) for c in Command)
ALLOWED_DEBUG_COMMANDS: frozenset[int] = frozenset(int(c) for c in DebugCommand)


def checksum(data: bytes | bytearray | memoryview) -> int:
    return sum(data) & 0xFF


def encode_frame(command: int, payload: bytes = b"", marker: Marker = Marker.NORMAL) -> bytes:
    """Encode a request frame, enforcing the command allow-list."""
    allowed = ALLOWED_NORMAL_COMMANDS if marker == Marker.NORMAL else ALLOWED_DEBUG_COMMANDS
    if command not in allowed:
        raise ForbiddenCommandError(
            f"command 0x{command:02X} with marker 0x{int(marker):02X} is not on the allow-list",
            details={"command": command, "marker": int(marker)},
        )
    return _encode_unchecked(int(marker), command, payload)


def encode_response(command: int, payload: bytes = b"", marker: int = Marker.NORMAL) -> bytes:
    """Encode a *response* frame (used by the simulator and test fixtures)."""
    return _encode_unchecked(int(marker), command | RESPONSE_FLAG, payload)


def _encode_unchecked(marker: int, command: int, payload: bytes) -> bytes:
    length = 1 + len(payload) + 1
    if length > MAX_FRAME_LENGTH:
        raise FramingError(f"payload too long for one frame ({len(payload)} bytes)")
    body = bytes([marker, length & 0xFF, (length >> 8) & 0xFF, command & 0xFF]) + bytes(payload)
    return body + bytes([checksum(body)])


@dataclass(frozen=True, slots=True)
class Frame:
    marker: int
    command: int
    payload: bytes

    @property
    def is_response(self) -> bool:
        return bool(self.command & RESPONSE_FLAG)

    @property
    def request_command(self) -> int:
        return self.command & ~RESPONSE_FLAG & 0xFF

    @property
    def subcommand(self) -> int | None:
        return self.payload[0] if self.payload else None

    def encode(self) -> bytes:
        return _encode_unchecked(self.marker, self.command, self.payload)


class FrameDecoder:
    """Incremental frame re-assembler.

    USB bulk reads do not necessarily align with protocol frames: one read may
    contain several frames or only part of one (the DSO5102P reports
    ``wMaxPacketSize`` 512 on endpoint 0x81, older units 64). Bytes are buffered
    until a complete frame is available.
    """

    def __init__(self) -> None:
        self._buf = bytearray()
        self._ready: list[Frame] = []
        self.checksum_errors = 0
        self.resyncs = 0
        self.discarded_bytes = 0

    @property
    def pending(self) -> int:
        return len(self._buf)

    def clear(self) -> None:
        self._buf.clear()
        self._ready.clear()

    def feed(self, data: bytes | bytearray) -> list[Frame]:
        """Add bytes and return every complete, valid frame.

        Garbage between frames is skipped and counted (``resyncs``/``discarded_bytes``).
        A frame with a bad checksum raises :class:`ChecksumError`; frames decoded before it
        are kept and returned by the next call, so a valid reply is never lost.
        """
        self._buf.extend(data)
        frames, self._ready = self._ready, []
        while True:
            try:
                frame = self._next()
            except ChecksumError:
                self._ready = frames
                raise
            if frame is None:
                return frames
            frames.append(frame)

    def _skip(self, n: int) -> None:
        del self._buf[:n]
        self.discarded_bytes += n
        self.resyncs += 1

    def _next(self) -> Frame | None:
        buf = self._buf
        while True:
            # Discard garbage until a plausible marker is at the head of the buffer.
            skipped = 0
            while skipped < len(buf) and buf[skipped] not in (Marker.NORMAL, Marker.DEBUG):
                skipped += 1
            if skipped:
                self._skip(skipped)
            if len(buf) < 3:
                return None
            length = buf[1] | (buf[2] << 8)
            if length < 2:
                self._skip(1)  # not a real frame start
                continue
            break
        total = 3 + length
        if len(buf) < total:
            return None
        raw = bytes(buf[:total])
        del buf[:total]
        if checksum(raw[:-1]) != raw[-1]:
            self.checksum_errors += 1
            raise ChecksumError(
                f"frame checksum mismatch for command 0x{raw[3]:02X}",
                details={"expected": checksum(raw[:-1]), "received": raw[-1], "length": length},
            )
        return Frame(marker=raw[0], command=raw[3], payload=raw[4:-1])


def decode_frame(raw: bytes) -> Frame:
    """Decode exactly one complete frame (raises if incomplete or trailing bytes remain)."""
    dec = FrameDecoder()
    frames = dec.feed(raw)
    if len(frames) != 1 or dec.pending or dec.discarded_bytes:
        raise FramingError(
            f"expected exactly one frame, got {len(frames)} with {dec.pending} trailing byte(s)"
        )
    return frames[0]
