# Architecture

```
 AI client ──MCP (stdio | Streamable HTTP)──► mcp/server.py ──┐
 GUI (PySide6) ───────────── worker threads ─────────────────┼──► ScopeService ──► ScopeController ──► ProtocolSession ──► Transport ──► DSO5102P
 CLI / scripts ──────────────────────────────────────────────┘    (service.py)    (instrument/)       (protocol/)        usb.py | simulator.py
                                                                       │
                                                                       ├── CaptureStore (storage/)
                                                                       └── analysis/ (measurements, fft, timing, quality)
```

## Layers

| Package | Responsibility | Depends on |
|---|---|---|
| `protocol/commands.py` | Frame encode/decode, command allow-list, incremental frame re-assembly | — |
| `protocol/settings.py` | `protocol.inf` parser, SYSData decode/encode, typed interpretation with provenance | — |
| `protocol/decoder.py` | Multi-frame transfers (samples, files, screenshots), RGB565, PNG, time | commands |
| `protocol/scaling.py` | Time/voltage model with explicit status (`verified`, `nominal_unverified`, `simulated`, `unavailable`) | settings |
| `protocol/session.py` | Request/response over a transport: timeouts, retry of idempotent reads only, stray-frame handling, statistics | transport, decoder |
| `transport/usb.py` | PyUSB/libusb transport, endpoint discovery, disconnect detection, read-only diagnostics | pyusb |
| `transport/simulator.py` | Protocol-level simulated instrument + fault injection | protocol |
| `instrument/` | `ScopeController`: connection lifecycle, operation lock, ownership lock, key-emulation setters with read-back, acquisition modes and freshness | protocol, transport |
| `storage/captures.py` | Bounded persisted capture store, CSV/NPZ export/import inside one directory | models |
| `analysis/` | Pure functions on arrays (no I/O) | numpy, scipy |
| `service.py` | `ScopeService`: the single API used by MCP, GUI and CLI; measurement envelopes; bounded workflows | all of the above |
| `mcp/` | MCP v2 `MCPServer` tools/resources/prompts, HTTP security, stdio↔HTTP bridge | service |
| `gui/` | Desktop application | service |

The instrument library (`protocol`, `transport`, `instrument`, `analysis`,
`storage`) has no dependency on MCP or Qt and can be used directly:

```python
from hantek_mcp.config import load_config
from hantek_mcp.service import ScopeService

svc = ScopeService(load_config(overrides={"backend": "simulator"}))
svc.connect()
cap = svc.controller.capture(1, mode="single")       # WaveformCapture with raw_samples/time_s/voltage_v
print(cap.sample_rate_hz, cap.voltage_status, cap.acquisition.freshness)
```

## Single owner of the USB device

* **In-process:** every hardware operation runs inside
  `ScopeController.operation()`, which holds a re-entrant lock for the whole
  multi-frame transaction (e.g. *lock panel → read settings → unlock → read
  CH1 → read CH2 → read settings*). Frames of two operations can never
  interleave. Lock waits are bounded (`limits.max_lock_wait_s`); status and
  health calls use a 0.25–1 s wait and fall back to cached values, so they and
  `scope_abort` are always answered while a long capture is waiting for a
  trigger.
* **Cross-process:** connecting to USB hardware takes an exclusive OS lock
  (`<data_dir>/usb-049f-505a.lock`, `fcntl.flock` / `msvcrt.locking`). A second
  process gets `DEVICE_BUSY` naming the owner PID instead of corrupting the
  session.
* **GUI + AI together:** the GUI hosts the MCP Streamable HTTP endpoint on
  localhost in its own process (button or `hantek-mcp gui --mcp-http`), sharing
  its `ScopeService`. HTTP-capable MCP clients connect directly; stdio-only
  clients launch `hantek-mcp bridge`, a stdio MCP server that forwards every
  tool/resource/prompt to that endpoint. Both interfaces observe the same
  connection state, captures and activity log.

## Concurrency model

* MCP tools are `async` and run blocking service calls with
  `anyio.to_thread.run_sync`, so the MCP event loop keeps serving requests.
* The GUI runs all instrument/analysis work on a `QThreadPool`; results return
  through queued Qt signals. Live refresh is single-flight (a tick is skipped
  while the previous capture is still running).
* **Abort:** `request_abort()` increments a generation counter. The running
  operation or workflow captured the generation when it started and raises
  `AbortedError` at its next check point (trigger-wait polling, every key press,
  between workflow steps). Later operations start with the new generation, so
  an abort never sticks.
* Workflows (`autoset`, `measure_repeatedly`, `acquire_until`,
  `run_measurement_plan`) are bounded by count, duration and plan length limits
  and hold the instrument lock per step, not for the whole workflow, so the GUI
  stays usable.

## Data model

`WaveformCapture` (`models.py`) holds `raw_samples` (int8), `time_s`,
`voltage_v` (or `None` when scaling is unavailable), the full settings snapshot,
the horizontal/vertical scaling model with status and notes, `AcquisitionInfo`
(mode, freshness, trigger states, trigger observed, group id, simultaneity,
timing), `quality` (clipping, ADC utilisation, flat, stale) and warnings.
Captures are persisted as compressed NPZ with a JSON metadata entry; MCP
responses contain only compact metadata and a `capture_id`.

## Setting changes

The protocol offers no per-setting command, so the controller emulates
front-panel keys and **always** re-reads the settings record afterwards. Knob
settings step by the exact index difference; offset and trigger level converge
iteratively with a stall detector and a key-press budget; soft-menu settings
(coupling, probe, trigger source/mode/slope/coupling) open the menu and cycle
the option. Results report `confirmed_value` from the read-back and the method
used. An experimental direct-write mode (command `0x11`) exists behind a config
switch.

## Security boundaries

* Encoder allow-list: debug commands other than beep, file writes, firmware and
  calibration writes, FPGA access and the remote shell cannot be encoded.
* MCP exposes no raw-command, shell, firmware or arbitrary-path tool. Exports
  go to one directory, by validated file name, without silent overwrite.
* HTTP binds `127.0.0.1` by default with DNS-rebinding protection; non-loopback
  binds need `allow_remote` **and** a ≥16-character bearer token.
* stdio: stdout carries only MCP messages (the SDK additionally redirects fd 1
  to stderr while serving); logs go to stderr / an optional file.
* Disruptive "Default Setup" requires a config opt-in and `confirm=true`.

## Deviations from the original specification (and why)

| Specification | Implementation | Reason |
|---|---|---|
| "FastMCP API" | `mcp.server.mcpserver.MCPServer` | In MCP Python SDK 2.x FastMCP was renamed `MCPServer`; `mcp.server.fastmcp` no longer exists |
| `transport/simulator.py` simulates the instrument API | Simulates the **USB protocol** (bytes in/out) | The entire real stack (framing, decoders, controller) is exercised in simulation, not a parallel fake |
| `instrument/controller.py` single file | `core.py` + `channels.py` + `triggering.py` + `acquisition.py` mixins composed in `controller.py` | Keeps each concern reviewable |
| Native instrument measurements | Computed from samples; the instrument's trigger frequency counter is reported alongside | The protocol exposes no measurement readout other than `TRIG-FREQUENCY` |
| `scope_autoset` | `method="software"` (measurement-driven, labelled) or `"instrument"` (AUTOSET key + read-back) | Instrument autoset cannot be validated remotely; the software method reports every change with read-back |
| Continuous streaming | Repeated captures (`measure_repeatedly`, GUI live refresh) | The protocol only downloads stored records |
| `RemoteShell` backwards compatibility | Removed from the legacy facade | Can brick the instrument; cannot be made safe |
| dsoc-extended code reuse | Facts only | GPLv3 is incompatible with this MIT project |
