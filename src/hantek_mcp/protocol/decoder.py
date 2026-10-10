"""Decoders for multi-frame responses (sample data, files, screenshots) and small replies."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

import numpy as np
import numpy.typing as npt

from hantek_mcp.errors import ChecksumError, NoSampleDataError, ProtocolError, UnexpectedResponseError
from hantek_mcp.protocol.commands import BulkSubcommand, Frame

SCREEN_WIDTH = 800
SCREEN_HEIGHT = 480


@dataclass
class SampleTransfer:
    """Accumulates the response frames of command 0x02 (read sample data).

    Response sequence (elinux, confirmed by both upstream implementations)::

        0x82 00 <len:3 LE>            announced sample count
        0x82 01 <ch> <samples...>     one or more data frames (<=10000 samples each)
        0x82 02 <ch>                  end of transfer
        0x82 03 <ch>                  error / no data (e.g. scope in STOP with empty memory)

    Samples are signed 8-bit values (dsoc-extended decodes them as int8; the upstream
    rcr implementation returned the unsigned bytes unchanged, which is the same byte
    stream interpreted differently).
    """

    channel_code: int
    announced_length: int | None = None
    chunks: list[bytes] = field(default_factory=list)
    frames: int = 0
    complete: bool = False

    def feed(self, frame: Frame) -> bool:
        """Consume one frame; returns True when the transfer is complete."""
        if frame.command != 0x82:
            raise UnexpectedResponseError(f"expected 0x82 sample frame, got 0x{frame.command:02X}")
        if not frame.payload:
            raise ProtocolError("empty sample data frame")
        self.frames += 1
        sub = frame.payload[0]
        if sub == BulkSubcommand.LENGTH:
            if len(frame.payload) < 4:
                raise ProtocolError("sample length frame too short")
            p = frame.payload
            self.announced_length = p[1] | (p[2] << 8) | (p[3] << 16)
            return False
        if sub in (BulkSubcommand.DATA, BulkSubcommand.END, BulkSubcommand.ERROR):
            if len(frame.payload) < 2:
                raise ProtocolError(f"sample frame (sub 0x{sub:02X}) missing channel byte")
            ch = frame.payload[1]
            if ch != self.channel_code:
                raise UnexpectedResponseError(
                    f"sample frame for channel code {ch}, requested {self.channel_code}",
                    details={"received_channel_code": ch, "requested_channel_code": self.channel_code},
                )
            if sub == BulkSubcommand.DATA:
                self.chunks.append(bytes(frame.payload[2:]))
                return False
            if sub == BulkSubcommand.ERROR:
                raise NoSampleDataError(
                    "instrument reported no sample data for this channel (sub-command 0x03); "
                    "the channel may be disabled or the acquisition memory empty",
                    details={"channel_code": ch},
                )
            self.complete = True
            return True
        raise UnexpectedResponseError(f"unknown sample sub-command 0x{sub:02X}")

    def samples(self) -> npt.NDArray[np.int8]:
        if not self.complete:
            raise ProtocolError("sample transfer incomplete")
        data = b"".join(self.chunks)
        if self.announced_length is None:
            raise ProtocolError("sample transfer missing length announcement")
        if len(data) != self.announced_length:
            raise ProtocolError(
                f"received {len(data)} samples but {self.announced_length} were announced",
                details={"received": len(data), "announced": self.announced_length},
            )
        return np.frombuffer(data, dtype=np.int8).copy()


@dataclass
class BulkTransfer:
    """Accumulates file/screenshot transfers: ``01 <data>`` frames then ``02 <sum8>``."""

    response_command: int
    chunks: list[bytes] = field(default_factory=list)
    running_sum: int = 0
    frames: int = 0
    complete: bool = False
    received_checksum: int | None = None
    verify_checksum: bool = True

    def feed(self, frame: Frame) -> bool:
        if frame.command != self.response_command:
            raise UnexpectedResponseError(
                f"expected 0x{self.response_command:02X}, got 0x{frame.command:02X}"
            )
        if not frame.payload:
            raise ProtocolError("empty bulk frame")
        self.frames += 1
        sub = frame.payload[0]
        if sub == BulkSubcommand.DATA:
            chunk = bytes(frame.payload[1:])
            self.chunks.append(chunk)
            self.running_sum = (self.running_sum + sum(chunk)) & 0xFF
            return False
        if sub == BulkSubcommand.END:
            self.received_checksum = frame.payload[1] if len(frame.payload) > 1 else None
            self.complete = True
            if (
                self.verify_checksum
                and self.received_checksum is not None
                and self.received_checksum != self.running_sum
            ):
                raise ChecksumError(
                    "bulk transfer content checksum mismatch",
                    details={"expected": self.running_sum, "received": self.received_checksum},
                )
            return True
        raise UnexpectedResponseError(f"unknown bulk sub-command 0x{sub:02X}")

    def data(self) -> bytes:
        return b"".join(self.chunks)


def decode_screenshot(data: bytes) -> tuple[npt.NDArray[np.uint8], str]:
    """Decode a screenshot payload into an RGB image ``(480, 800, 3)``.

    * 768000 bytes: RGB565 little-endian, top row first (DSO5102P per the upstream
      project's 16-bit reshape and the dsoc-extended DSO5202B decoder).
    * 384000 bytes: 8-bit palette indices (older DSO5xxxB, palette not available here,
      rendered as greyscale; elinux states rows are sent bottom-up).
    """
    n = SCREEN_WIDTH * SCREEN_HEIGHT
    if len(data) == 2 * n:
        px = np.frombuffer(data, dtype="<u2").reshape(SCREEN_HEIGHT, SCREEN_WIDTH)
        r = ((px >> 11) & 0x1F).astype(np.uint16)
        g = ((px >> 5) & 0x3F).astype(np.uint16)
        b = (px & 0x1F).astype(np.uint16)
        rgb = np.stack([(r * 255 + 15) // 31, (g * 255 + 31) // 63, (b * 255 + 15) // 31], axis=-1)
        return rgb.astype(np.uint8), "rgb565"
    if len(data) == n:
        idx = np.frombuffer(data, dtype=np.uint8).reshape(SCREEN_HEIGHT, SCREEN_WIDTH)[::-1]
        return np.repeat(idx[:, :, None], 3, axis=2).copy(), "palette8_greyscale"
    raise ProtocolError(
        f"unexpected screenshot size {len(data)} bytes",
        details={"received": len(data), "expected_one_of": [2 * n, n]},
    )


def encode_png(rgb: npt.NDArray[np.uint8]) -> bytes:
    """Minimal dependency-free PNG encoder for RGB uint8 images."""
    import struct
    import zlib

    h, w, c = rgb.shape
    if c != 3:
        raise ValueError("expected RGB image")
    raw = b"".join(b"\x00" + rgb[y].tobytes() for y in range(h))

    def chunk(tag: bytes, body: bytes) -> bytes:
        return (
            struct.pack(">I", len(body)) + tag + body + struct.pack(">I", zlib.crc32(tag + body) & 0xFFFFFFFF)
        )

    ihdr = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(raw, 6))
        + chunk(b"IEND", b"")
    )


def decode_system_time(payload: bytes) -> datetime:
    """Decode the 7-byte reply of command 0x21 (year LE u16, month, day, h, m, s).

    The upstream code computed the year as ``b1*0xFF + b0 + 7``, which only equals the
    little-endian value while the high byte is 7 (years 1792-2047); this is the general form.
    """
    if len(payload) < 7:
        raise ProtocolError(f"system time reply too short ({len(payload)} bytes)")
    year = payload[0] | (payload[1] << 8)
    try:
        return datetime(year, payload[2], payload[3], payload[4], payload[5], payload[6])
    except ValueError as e:
        raise ProtocolError(f"invalid system time from instrument: {list(payload[:7])}") from e


def parse_sys_inf(text: str) -> dict[str, str]:
    """Parse ``/sys.inf`` (``[key]value`` lines) e.g. ``[soft version]3.40.0(200217.0)``."""
    out: dict[str, str] = {}
    for raw_line in text.replace("\r", "").split("\n"):
        line = raw_line.strip()
        if line.startswith("[") and "]" in line:
            k, v = line[1:].split("]", 1)
            out[k.strip()] = v.strip()
    return out
