"""Frame codec, settings record, decoders and key table."""

from __future__ import annotations

import json
import struct
from datetime import datetime

import numpy as np
import pytest

from hantek_mcp.errors import (
    ChecksumError,
    ForbiddenCommandError,
    FramingError,
    NoSampleDataError,
    ProtocolError,
    UnexpectedResponseError,
)
from hantek_mcp.protocol import keys
from hantek_mcp.protocol.commands import (
    Command,
    Frame,
    FrameDecoder,
    Marker,
    checksum,
    decode_frame,
    encode_frame,
    encode_response,
)
from hantek_mcp.protocol.decoder import (
    BulkTransfer,
    SampleTransfer,
    decode_screenshot,
    decode_system_time,
    encode_png,
    parse_sys_inf,
)
from hantek_mcp.protocol.settings import (
    TIMEBASE_S_PER_DIV,
    VOLTS_PER_DIV,
    _read_resource,
    decode_settings,
    default_key_names,
    default_layout,
    encode_settings,
    interpret_settings,
    parse_layout,
)
from tests.conftest import FIXTURES

# --------------------------------------------------------------------------------- frames


@pytest.mark.parametrize("frame", json.loads((FIXTURES / "elinux_frames.json").read_text())["frames"])
def test_encoder_matches_documented_frames(frame: dict[str, object]) -> None:
    raw = encode_frame(int(frame["command"]), bytes(frame["payload"]), Marker(frame["marker"]))  # type: ignore[arg-type,call-overload]
    assert raw.hex() == frame["hex"]


@pytest.mark.parametrize("resp", json.loads((FIXTURES / "elinux_frames.json").read_text())["responses"])
def test_decoder_accepts_documented_responses(resp: dict[str, str]) -> None:
    f = decode_frame(bytes.fromhex(resp["hex"]))
    assert f.is_response


def test_length_field_counts_command_payload_and_checksum() -> None:
    raw = encode_frame(Command.ECHO, b"abc")
    assert raw[1] | raw[2] << 8 == 1 + 3 + 1
    assert raw[-1] == checksum(raw[:-1])


@pytest.mark.parametrize(
    ("marker", "cmd"),
    [
        (Marker.DEBUG, 0x11),  # remote shell
        (Marker.DEBUG, 0x40),  # write firmware variables
        (Marker.DEBUG, 0x41),  # write calibration
        (Marker.DEBUG, 0x42),  # write FPGA register
        (Marker.DEBUG, 0x50),  # write file
        (Marker.DEBUG, 0x60),  # update file
        (Marker.DEBUG, 0x7F),  # init
        (Marker.NORMAL, 0x14),  # set system time (not needed)
        (Marker.NORMAL, 0x7F),
    ],
)
def test_dangerous_commands_are_refused(marker: Marker, cmd: int) -> None:
    with pytest.raises(ForbiddenCommandError):
        encode_frame(cmd, b"", marker)


def test_frame_decoder_reassembles_fragments_and_batches() -> None:
    frames = [encode_response(Command.ECHO, bytes([i]) * i) for i in range(1, 40)]
    stream = b"".join(frames)
    dec = FrameDecoder()
    out: list[Frame] = []
    for i in range(0, len(stream), 7):  # awkward fragment size
        out += dec.feed(stream[i : i + 7])
    assert [f.payload for f in out] == [bytes([i]) * i for i in range(1, 40)]
    assert dec.pending == 0


def test_frame_decoder_detects_checksum_and_resyncs() -> None:
    good = encode_response(Command.ECHO, b"ok")
    bad = bytearray(encode_response(Command.ECHO, b"xx"))
    bad[-1] ^= 0xFF
    dec = FrameDecoder()
    with pytest.raises(ChecksumError):
        dec.feed(bytes(bad) + good)
    assert dec.feed(b"")[0].payload == b"ok"
    # garbage between frames is skipped without losing either frame (review finding #7)
    frames = dec.feed(good + b"\x00\xff\x01" + good)
    assert [f.payload for f in frames] == [b"ok", b"ok"]
    assert dec.resyncs == 1 and dec.discarded_bytes == 3


def test_frame_decoder_keeps_frames_decoded_before_a_checksum_error() -> None:
    good = encode_response(Command.ECHO, b"first")
    bad = bytearray(encode_response(Command.ECHO, b"xx"))
    bad[-1] ^= 0xFF
    dec = FrameDecoder()
    with pytest.raises(ChecksumError):
        dec.feed(good + bytes(bad))
    assert [f.payload for f in dec.feed(b"")] == [b"first"]


def test_decode_frame_rejects_trailing_bytes() -> None:
    with pytest.raises(FramingError):
        decode_frame(encode_response(0, b"a") + b"\x53")


# --------------------------------------------------------------------------------- settings


def test_bundled_dso5102p_layout_is_consistent() -> None:
    lay = default_layout()
    assert lay.size == 208
    assert len(lay.fields) == 119
    assert lay.get("VERT-CH1-POS").size == 2
    assert lay.get("TRIG-SWAP-CH1-PULSE-TIME").size == 1  # firmware quirk preserved
    assert lay.get("TRIG-SWAP-CH2-PULSE-TIME").size == 8


def test_layout_total_mismatch_is_an_error() -> None:
    with pytest.raises(ProtocolError):
        parse_layout("[TOTAL] 3\n[START]\n[A] 1\n[B] 1\n[END]\n")


def test_settings_roundtrip_and_signedness() -> None:
    lay = default_layout()
    raw = dict.fromkeys(lay.names(), 0)
    raw.update(
        {"VERT-CH1-POS": -37, "TRIG-VPOS": -200, "HORIZ-TRIGTIME": -123456789, "TRIG-FREQUENCY": 10**12}
    )
    blob = encode_settings(raw, lay) + b"\x01\x02"
    dec, trailing = decode_settings(blob, lay)
    assert dec == raw
    assert trailing == b"\x01\x02"
    off = lay.get("VERT-CH1-POS").offset
    assert struct.unpack_from("<h", blob, off)[0] == -37


def test_settings_too_short() -> None:
    with pytest.raises(ProtocolError):
        decode_settings(b"\x00" * 10, default_layout())


def test_interpretation_tables() -> None:
    lay = default_layout()
    raw = dict.fromkeys(lay.names(), 0)
    raw.update(
        {
            "VERT-CH1-DISP": 1,
            "VERT-CH1-VB": 8,
            "VERT-CH1-PROBE": 1,
            "VERT-CH1-COUP": 1,
            "HORIZ-TB": 15,
            "TRIG-SRC": 1,
            "TRIG-STATE": 3,
            "TRIG-FREQUENCY": 1_000_000,
            "VERT-CH2-FINE": 1,
            "VERT-CH2-VB": 99,
        }
    )
    st = interpret_settings(raw)
    ch1 = st.channels[1]
    assert ch1.volts_per_div == 1.0 and ch1.probe == 10 and ch1.effective_volts_per_div == 10.0
    assert ch1.coupling == "AC"
    assert st.horizontal.seconds_per_div == pytest.approx(200e-6)
    assert st.trigger.source == "CH2" and st.trigger.state == "triggered"
    assert st.trigger.counter_frequency_hz == pytest.approx(1000.0)
    assert st.channels[2].volts_per_div is None and st.channels[2].notes


def test_tables_cover_dso5102p_ranges() -> None:
    assert VOLTS_PER_DIV[0] == 0.002 and VOLTS_PER_DIV[10] == 5.0
    assert TIMEBASE_S_PER_DIV[1] == pytest.approx(4e-9) and TIMEBASE_S_PER_DIV[31] == pytest.approx(40.0)


def test_key_table_matches_instrument_file() -> None:
    assert tuple(default_key_names()) == keys.INF_NAMES
    for i, name in enumerate(keys.INF_NAMES):
        assert keys.Key(i).value == i, name
    assert keys.Key.MEASURE == 0x0C and keys.Key.ACQUIRE == 0x0D  # confirmed by upstream Test1.py


def test_sys_inf_parse() -> None:
    info = parse_sys_inf(_read_resource("dso5102p_fw3.40.0/sys.inf"))
    assert info["soft version"] == "3.40.0(200217.0)"
    assert info["DST type"] == "dst1102b"


# --------------------------------------------------------------------------------- transfers


def _sample_frames(ch_code: int, data: bytes, announced: int | None = None) -> list[Frame]:
    n = len(data) if announced is None else announced
    frames = [encode_response(Command.READ_SAMPLES, bytes([0, n & 0xFF, n >> 8 & 0xFF, n >> 16]))]
    frames += [
        encode_response(Command.READ_SAMPLES, bytes([1, ch_code]) + data[i : i + 10000])
        for i in range(0, len(data), 10000)
    ]
    frames.append(encode_response(Command.READ_SAMPLES, bytes([2, ch_code])))
    return [decode_frame(f) for f in frames]


def test_sample_transfer_is_signed_int8_and_multi_frame() -> None:
    data = bytes([0x00, 0x7F, 0x80, 0xFF]) * 6250  # 25000 bytes -> 3 data frames
    tr = SampleTransfer(channel_code=1)
    done = [tr.feed(f) for f in _sample_frames(1, data)]
    assert done[-1] and not any(done[:-1])
    s = tr.samples()
    assert s.dtype == np.int8 and s.size == 25000
    assert list(s[:4]) == [0, 127, -128, -1]


def test_sample_transfer_validations() -> None:
    tr = SampleTransfer(channel_code=0)
    with pytest.raises(UnexpectedResponseError):
        for f in _sample_frames(1, b"\x00" * 10):
            tr.feed(f)
    tr = SampleTransfer(channel_code=0)
    for f in _sample_frames(0, b"\x00" * 10, announced=11):
        tr.feed(f)
    with pytest.raises(ProtocolError):
        tr.samples()
    tr = SampleTransfer(channel_code=0)
    with pytest.raises(NoSampleDataError):
        tr.feed(decode_frame(encode_response(Command.READ_SAMPLES, bytes([3, 0]))))


def test_bulk_transfer_checksum() -> None:
    data = bytes(range(256)) * 50
    frames = [
        encode_response(Command.READ_FILE, b"\x01" + data[i : i + 10208]) for i in range(0, len(data), 10208)
    ]
    tr = BulkTransfer(response_command=0x90)
    for f in frames:
        tr.feed(decode_frame(f))
    assert tr.feed(decode_frame(encode_response(Command.READ_FILE, bytes([2, sum(data) & 0xFF]))))
    assert tr.data() == data
    tr2 = BulkTransfer(response_command=0x90)
    tr2.feed(decode_frame(frames[0]))
    with pytest.raises(ChecksumError):
        tr2.feed(decode_frame(encode_response(Command.READ_FILE, bytes([2, 0x00]))))


def test_screenshot_rgb565_decoding() -> None:
    px = np.zeros((480, 800), dtype="<u2")
    px[0, 0] = 0xF800  # red
    px[0, 1] = 0x07E0  # green
    px[0, 2] = 0x001F  # blue
    px[479, 799] = 0xFFFF
    rgb, fmt = decode_screenshot(px.tobytes())
    assert fmt == "rgb565" and rgb.shape == (480, 800, 3)
    assert (
        tuple(rgb[0, 0]) == (255, 0, 0)
        and tuple(rgb[0, 1]) == (0, 255, 0)
        and tuple(rgb[0, 2]) == (0, 0, 255)
    )
    assert tuple(rgb[479, 799]) == (255, 255, 255)
    png = encode_png(rgb)
    assert png.startswith(b"\x89PNG")
    with pytest.raises(ProtocolError):
        decode_screenshot(b"\x00" * 1000)


def test_system_time_decoding_matches_legacy_for_2019() -> None:
    p = bytes([0xE3, 0x07, 5, 5, 11, 58, 48])  # 2019-05-05 11:58:48 (upstream screenshot name)
    assert decode_system_time(p) == datetime(2019, 5, 5, 11, 58, 48)
    legacy_year = p[1] * 0xFF + p[0] + 7
    assert legacy_year == 2019
