# Hantek DSO5102P USB protocol — audit and implementation notes

This is the Phase-1 audit deliverable. It separates **confirmed facts**,
**inferred behaviour** and **unresolved questions**, and records how each one is
handled in `hantek_mcp`. Nothing in this document has been verified on a
physical DSO5102P *by this project*; "confirmed" below means confirmed by a
cited source that exercised real hardware.

## 1. Sources reviewed

| Source | What it is | Use |
|---|---|---|
| [titos-carrasco/DSO5102P-Python](https://github.com/titos-carrasco/DSO5102P-Python) @ `181f290` (MIT) | Original Python library tested by its author on a DSO5102P; includes `/protocol.inf`, `/keyprotocol.inf`, `/sys.inf` read from that scope (firmware `3.40.0(200217.0)`, `DST type dst1102b`) | Base of this fork; the `.inf` files are bundled as package data and test fixtures |
| [uberdaff/dsoc-extended](https://github.com/uberdaff/dsoc-extended) @ `94fe963` (GPLv3) | Python 3 port of Onno Kortmann's `dsoc`, tested by its author on a **DSO5202B** | Protocol *facts* only. No code was copied: GPLv3 code cannot be relicensed into this MIT project |
| [elinux.org Das_Oszi_Protocol](https://elinux.org/Das_Oszi_Protocol) (retrieved 2026-10-10) | Community protocol documentation for the DSO5xxxB family (from mikrocontroller.net) | Frame format, command list, sample-transfer structure, scaling notes |
| DSO5000P User Manual V1.1 (`docs/manuals/`) | Manufacturer manual | Ranges (V/div, s/div, memory depth), input ratings, probe-comp signal |
| mikrocontroller.net "Hantek Protokoll", `SysDATA_v1.0.zip`, `Sampledaten.pdf` | Original German documentation | **Not reviewed**: the site returned a bot-protection page. Enumerations that would come from SysDATA are therefore marked inferred |

## 2. Device identification and endpoints

| Item | Value | Status |
|---|---|---|
| VID:PID | `049f:505a` | **Confirmed** on DSO5102P (upstream README) and DSO5x02B (dsoc-extended, elinux) |
| Serial number | `iSerial = 0` | **Confirmed** (elinux). Several scopes can only be told apart by bus/address → `usb.bus`, `usb.address`, `usb.index` config |
| Interface | 0 | **Confirmed** (upstream, dsoc-extended) |
| Bulk OUT / IN | `0x02` / `0x81`, `wMaxPacketSize` 512 | **Confirmed** on DSO5102P (upstream source comment). elinux describes 64-byte packets on endpoints 1/2 for older units; dsoc-extended had to change its endpoints to `0x02/0x81` for a DSO5202B. The transport discovers bulk endpoints from the descriptors, prefers `0x02/0x81` and can be overridden |
| Linux kernel driver | `cdc_subset` may bind to interface 0 | **Confirmed** (upstream comment). Detached on open and re-attached on close |
| Windows driver | Hantek ships its own driver for TTScope; PyUSB needs a libusb-compatible driver (WinUSB/libusbK/libusb-win32) | Documented install/rollback with Zadig in `windows-setup.md`. The software never installs drivers |

## 3. Frame format (confirmed)

```
+--------+---------+---------+---------+---------------+----------+
| marker | len LSB | len MSB | command | payload ...   | checksum |
+--------+---------+---------+---------+---------------+----------+
```

* `marker` `0x53` normal, `0x43` debug.
* `len` = 1 (command) + payload length + 1 (checksum), little-endian.
* Responses echo the command with bit 7 set.
* `checksum` = low byte of the sum of all preceding bytes.

All three sources agree, and every example frame on elinux checks out
(`tests/fixtures/elinux_frames.json`). Long responses are split across USB
transfers; `FrameDecoder` re-assembles frames from arbitrary fragments and
resynchronises after garbage or checksum errors (the upstream code assumed one
USB read = one frame and looped forever on unexpected replies).

## 4. Commands

| Cmd | Marker | Purpose | Status on DSO5102P | In `hantek_mcp` |
|---|---|---|---|---|
| `0x00` | 0x53 | Echo | Confirmed (upstream "OK") | Sync, health check, key-press flush |
| `0x01` | 0x53 | Read settings (SYSData) | Upstream: "implemented, not tested". dsoc-extended decodes it on a DSO5202B | Decoded with the instrument's own `/protocol.inf` (re-read at connect; bundled copy as fallback) |
| `0x02` | 0x53 | Read sample data CH1/CH2 | Upstream: "implemented, not tested". Works on DSO5202B (dsoc-extended) | See §5 |
| `0x10` | 0x53 | Read file | Confirmed (upstream used it to fetch the `.inf` files) | Used only for `/protocol.inf`, `/keyprotocol.inf`, `/sys.inf`, `/logotype.dis`; not exposed via MCP |
| `0x11` | 0x53 | Write settings | elinux only | **Experimental, off by default** (`control.settings_write_mode = "direct_write_experimental"`), always verified by read-back |
| `0x12` | 0x53 | `00 xx`: run (00) / stop (01); `01 xx`: unlock (00) / lock (01) panel | Confirmed (upstream "OK"); elinux: run/stop "somewhat buggy" | Run/stop, panel lock while reading settings |
| `0x13` | 0x53 | Key press `<code> 01` | Confirmed (upstream "OK"); keycodes = line order of `/keyprotocol.inf` | All setting changes; codes cross-checked against the scope's own `/keyprotocol.inf` at connect, key emulation is disabled if they differ |
| `0x14` | 0x53 | Set system time | elinux | Not implemented (not needed) |
| `0x20` | 0x53 | Screenshot | Confirmed (upstream "OK"): 768 000 bytes = 800×480×16 bit | RGB565 decode (§8) |
| `0x21` | 0x53 | Read system time | Confirmed (upstream "OK") | Implemented; fixes upstream year arithmetic (§9) |
| `0x44` | 0x43 | Beep | dsoc-extended (DSO5202B) | Library only |
| `0x11` | 0x43 | Remote shell | Upstream "OK" | **Removed.** Arbitrary shell commands on the scope can brick it; output >10 kB crashes `dso.exe` (elinux) |
| `0x40 0x41 0x42 0x50 0x60 0x7F …` | 0x43 | Firmware variables, calibration, FPGA registers, file write/update, init | elinux | **Refused by the encoder allow-list** (`ForbiddenCommandError`); never reachable from MCP, GUI or CLI |

## 5. Sample data (`0x02`) — the original `ReadSampleData`

Request: `53 04 00 02 01 <ch> <sum>` where `<ch>` is `0x00` for CH1 and `0x01` for CH2.

Response sequence (elinux; consistent with both implementations):

```
82 00 <n:3 bytes LE>        announced number of samples
82 01 <ch> <samples ...>    data, ≤ 10 000 samples per frame
...
82 02 <ch>                  end of transfer
82 03 <ch>                  error / no data (e.g. channel off, memory empty)
```

### What the upstream implementation returned

`ReadSampleData(channel)` (upstream `rcr/dso5102p/DSO5102P.py`):

* sends `[0x01, channel & 0x01]` — the argument is the **wire code**, so
  `ReadSampleData(1)` reads **CH2**, an easy off-by-one for callers;
* parses the length announcement but never compares it with the data received;
* appends `d[6:-1]` (skipping the channel byte) without checking the channel;
* treats sub-command `0x03` (error) like the end of transfer and silently returns
  whatever was received;
* returns an `array('B')`: **raw, unsigned bytes** — neither volts nor
  sign-interpreted ADC counts. No scaling information is attached.

### dsoc-extended

Same request; validates the channel byte on every frame and the total length
against the announcement; interprets the bytes as **signed `int8`**; treats
`0x03` as an error. Its CLI then converts with heuristic constants
(`yscale_div = 10.3`, `xscale_div = 19.2`, and a `/1.2` correction for record
lengths that suggest the on-screen menu was open), noting the vertical scale
"seems to be a bit off".

### This implementation

`SampleTransfer` validates channel and length, raises `NoSampleDataError` for
`0x03`, re-assembles frames split across USB transfers and returns `int8`
counts. Every capture keeps the raw counts; volts are derived separately with an
explicit status (§6). Endianness only matters for the 24-bit length field
(little-endian, confirmed); samples are single bytes.

## 6. Scaling model (inferred, flagged on every capture)

| Quantity | Model | Evidence | Status |
|---|---|---|---|
| Volts | `(raw − VERT-CHx-POS) × V/div × probe / 25` | elinux: samples span ~10.2 div over −127…127 (≈25 counts/div); dsoc-extended's trigger formula `VPOS × V/div × probe / 25`; dsoc subtracts `POS` | `nominal_unverified` |
| V/div table | index 0…11 = 2 mV … 10 V (1-2-5) | dsoc-extended decoder (DSO5202B); DSO5102P manual: 2 mV–5 V → index 0…10 | inferred |
| Probe | index 0…3 = 1×, 10×, 100×, 1000× | dsoc-extended; manual lists these factors | inferred |
| Time | `dt = s/div ÷ (memory_depth / 20)` | elinux: "20 DIV horizontal"; manual: "sample interval = s/div ÷ 200" at the default 4K depth | `nominal_unverified` |
| s/div table | index 0…32 = 2 ns … 80 s (2-4-8) | dsoc-extended; elinux: TBID < 3 are 2/4/8 ns; DSO5102P: 4 ns–40 s → index 1…31 | inferred |
| Memory depth | index 0…2 = 4K, 20K, 40K | manual menu order only | inferred |
| Trigger position | record centre = trigger (`t = 0`) | screen layout; `HORIZ-TRIGTIME` units unknown (not applied, reported in notes) | inferred |

Where the depth field is unavailable the samples-per-division is inferred from
the record length (e.g. 3200 = 16 div × 200 → menu open, as observed by
dsoc-extended), falling back to a 20-division span. Implied rates above 1 GS/s
are flagged as interpolated. Fine V/div (`VERT-CHx-FINE`) makes volts
**unavailable** rather than guessed. `scaling.counts_per_div` and
`scaling.samples_per_div_override` can be corrected from a validation run, and
`scaling.voltage_verified` / `time_verified` may only be set after one
(`acquisition-validation.md`).

## 7. Acquisition freshness

The protocol has **no acquisition counter or timestamp**: `0x02` returns
whatever is in memory. A record is only proven new when the client armed a
single sequence (key `SINGLE SEQ`) and then observed the trigger state machine
(`TRIG-STATE`: `stop, ready, auto, trig'd, scan, astop, armed`, enumeration from
dsoc-extended) leave an armed state and reach `stop`. Captures therefore carry
`freshness` (`new_trigger_confirmed`, `forced_new`, `stopped_snapshot`,
`running_snapshot`) plus a SHA-1 comparison with the previous record of the same
channel (`identical_to_previous`), and both channels of
`capture_both_channels` come from one stopped acquisition (the manual states
both channels are sampled simultaneously).

## 8. Screenshots

* 768 000 bytes (DSO5102P per upstream; DSO5202B per dsoc-extended; DSO5072P
  per elinux after the 2013 hardware revision): 800 × 480 RGB565,
  little-endian, top row first. The upstream `Screenshot()` cast each 16-bit
  pixel to `uint8`, keeping only the low byte; `Color-Sreenshot.py` then
  guessed colours from that byte. `decode_screenshot()` decodes the real colours;
  the legacy facade keeps the old 8-bit behaviour for compatibility.
* 384 000 bytes (older DSO5xxxB): 8-bit palette indices, rows bottom-up
  (elinux); the palette lives in the firmware and is not available, so it is
  rendered as greyscale.

Screenshots are never used as waveform data.

## 9. Other findings in the upstream code

* `ReadSystemTime` computed the year as `b1*0xFF + b0 + 7`, which equals the
  little-endian value only while the high byte is 7 (years 1792–2047).
* `_ReadAnswer` read up to 1 MiB with a 500 ms timeout and retried forever on
  unexpected replies; checksum mismatches were only logged.
* The constructor called `exit()` when the scope was missing.
* Screenshots were saved with `:` in the file name (invalid on Windows), and
  the repository contained such files (renamed in this fork).

## 10. Model differences (DSO5102P vs DSO5102B vs DSO5202B)

| | DSO5102P | DSO5102B | DSO5202B |
|---|---|---|---|
| Bandwidth / max s/div | 100 MHz, 4 ns/div | 100 MHz | 200 MHz, 2 ns/div |
| V/div range | 2 mV–5 V (manual) | similar | dsoc lists up to 10 V |
| Screenshot | 16-bit RGB565 (upstream) | 8-bit on pre-2013 units, 16-bit later (elinux) | 16-bit (dsoc-extended) |
| `/keyprotocol.inf` | 49 keys, identical order to elinux's DSO5202B listing | — | 49 keys (elinux) |
| `/protocol.inf` | 208 bytes, 119 fields (bundled) | firmware dependent | firmware dependent; dsoc reads it at run time |
| Verified by | upstream author (subset of commands) | — | dsoc-extended author (full CLI) |

Because the settings layout differs by firmware, the layout is always taken
from the connected instrument. The soft-menu F-key mapping used for coupling,
probe and trigger settings (CH menu F1/F4; trigger menu F1…F5) was verified by
dsoc-extended on a DSO5202B only — every change is therefore read back, and a
mismatch is reported as `SETTING_NOT_CONFIRMED` instead of being assumed.

## 11. Unresolved questions (to be answered by `tests/hardware`)

1. Does `0x02` on a DSO5102P return int8 samples with ≈25 counts/div, and is
   `POS` subtracted correctly (sign, units)?
2. What record lengths does the DSO5102P return for 4K/20K/40K, with and
   without the menu shown? Is `memory_depth/20` samples/div correct?
3. Is the `ACQURIE-STORE-DEPTH` / `ACQURIE-MODE` enumeration as inferred?
4. Is `TRIG-STATE` enumerated as on the DSO5202B, and does it pass through an
   armed state after `SINGLE SEQ`?
5. Does `TRIG-VPOS` measure the level relative to the channel ground or to the
   screen centre (dsoc assumes ground; only identical when `POS = 0`)?
6. Units of `HORIZ-TRIGTIME` (horizontal trigger offset).
7. Do the soft-menu F-key positions match the DSO5202B's on DSO5102P firmware
   3.40?
8. Does the `VERT-CHx-VB` field read back correctly after a front-panel change
   (dsoc-extended saw it stuck at 0 after the debug "init" command, which this
   project never sends)?
9. Is `0x11` (write settings) accepted, and does it take effect immediately?
10. Is the 8-bit screenshot ever produced by a DSO5102P?
