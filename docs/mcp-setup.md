# MCP server setup

The server is built on the official MCP Python SDK 2.x (`mcp.server.mcpserver.MCPServer`,
the successor of FastMCP). It exposes 67 tools, 9 resources/templates and 7
prompts — see [tool-reference.md](tool-reference.md).

## Transports

| Mode | Command | Use |
|---|---|---|
| stdio (default) | `hantek-mcp serve` | The MCP client launches the server; one client owns the scope |
| Streamable HTTP | `hantek-mcp serve --transport http [--port 8765]` | Local HTTP endpoint `http://127.0.0.1:8765/mcp` |
| GUI-hosted HTTP | `hantek-mcp gui --mcp-http` (or the GUI's *Start MCP endpoint* button) | GUI and AI share one instrument connection |
| stdio bridge | `hantek-mcp bridge --url http://127.0.0.1:8765/mcp` | stdio-only clients reaching the GUI-hosted endpoint |

Common options: `--simulate` (built-in simulator; every result is labelled
`simulated: true`), `--config FILE`, `--data-dir DIR`, `--no-connect` (do not
connect at start-up; call `scope_connect`). Without `--simulate` the server uses
USB and **never** falls back to simulation: if the scope is absent, tools return
`NOT_CONNECTED` / `DEVICE_NOT_FOUND`.

On stdio, stdout carries only MCP messages; logs go to stderr (add
`log_file = "C:/path/hantek.log"` in the config for a file).

### HTTP security

* Binds `127.0.0.1` by default; Host/Origin headers are validated (DNS-rebinding
  protection).
* `--auth-token TOKEN` (or `server.auth_token`) requires
  `Authorization: Bearer TOKEN` on every request.
* A non-loopback `--host` is refused unless `--allow-remote` **and** a token of
  at least 16 characters are given. Wildcard binds (`0.0.0.0`, `::`) also need
  `server.allowed_hosts` listing the `host:port` values clients will send. Exposing an instrument that may be wired to
  live electronics on a network is your responsibility; prefer an SSH tunnel.

## Client configuration

Paths below assume the repository is at `C:\tools\DSO5102P-Python` and was
installed with `scripts\install_windows.ps1`; adjust to your location. Use the
absolute path to `hantek-mcp.exe` (or `hantek-mcp` on Linux).

### Verification status

| Client | Transport | Status |
|---|---|---|
| Official MCP Python SDK client (`mcp.Client`) | stdio, Streamable HTTP, bridge | **Verified** by the automated tests on Windows and Linux CI |
| Claude Code 2.1 (CLI) | stdio and Streamable HTTP | **Verified** on Linux: `claude mcp list` reports *Connected* for both (simulator) |
| Claude Desktop | stdio | Configuration below follows its documented format; not verified by this project |
| VS Code (Copilot agent mode) | stdio / HTTP | Documented format; not verified |
| Cursor | stdio | Documented format; not verified |
| ChatGPT | — | Not supported as documented here: ChatGPT connectors call *remote* HTTPS MCP servers and cannot launch a local stdio process. Making this server reachable from ChatGPT would require public exposure, which is not recommended for lab equipment |

### Claude Desktop (`%APPDATA%\Claude\claude_desktop_config.json`)

```json
{
  "mcpServers": {
    "hantek-dso5102p": {
      "command": "C:\\tools\\DSO5102P-Python\\.venv\\Scripts\\hantek-mcp.exe",
      "args": ["serve", "--transport", "stdio"]
    }
  }
}
```

Simulator: add `"--simulate"` to `args`. To share the scope with the GUI, start
the GUI with `--mcp-http` and use `"args": ["bridge", "--url", "http://127.0.0.1:8765/mcp"]`.

### Claude Code

```powershell
claude mcp add hantek-dso5102p -- C:\tools\DSO5102P-Python\.venv\Scripts\hantek-mcp.exe serve --transport stdio
# or, to the GUI-hosted endpoint:
claude mcp add --transport http hantek-dso5102p http://127.0.0.1:8765/mcp
claude mcp list
```

### VS Code (`.vscode/mcp.json`)

```json
{
  "servers": {
    "hantek-dso5102p": {
      "type": "stdio",
      "command": "C:\\tools\\DSO5102P-Python\\.venv\\Scripts\\hantek-mcp.exe",
      "args": ["serve", "--transport", "stdio"]
    }
  }
}
```

HTTP: `{"type": "http", "url": "http://127.0.0.1:8765/mcp"}`.

### Cursor (`%USERPROFILE%\.cursor\mcp.json`)

```json
{
  "mcpServers": {
    "hantek-dso5102p": {
      "command": "C:\\tools\\DSO5102P-Python\\.venv\\Scripts\\hantek-mcp.exe",
      "args": ["serve", "--transport", "stdio"]
    }
  }
}
```

## Complete example session (simulator)

`examples/simulated_measurement.py` launches the server over stdio and performs
a full measurement loop. Output (abridged, actual run):

```
67 tools available
backend=simulator simulated=True connected=True
  set CH1.position_divisions -> -2.48 (readback_confirmed_within_tolerance)
  set trigger.level_counts -> 62 (readback_confirmed)
capture sim-20261010-110810-0005-13c8-ch1: 4000 samples @ 1e+06 S/s, freshness=new_trigger_confirmed, clipped=False
frequency = 1000.000 Hz (±0.014 %)
Vpp = 5.080 V, top-base = 5.000 V
RMS = 3.536 V, duty = 50.00 %
voltage scaling: simulated, time scaling: simulated
exported %LOCALAPPDATA%\hantek-mcp\exports\example.csv
```

The equivalent agent conversation for *"Characterise the waveform on channel 1.
Determine its frequency, RMS voltage, peak-to-peak voltage and duty cycle.
Adjust the timebase if necessary."*:

1. `scope_status`, `scope_get_capabilities` → backend, `simulated`, scaling verification.
2. `scope_get_channel(1)`, `scope_get_trigger` → current settings.
3. `scope_capture(channel=1, mode="single")` → check `freshness`, `quality.clipped`,
   `quality.adc_utilisation`.
4. If clipped/too small or fewer than 2 cycles: `scope_set_vertical_scale`,
   `scope_set_timebase` (or `scope_autoset`) and capture again.
5. `scope_measure_frequency`, `scope_measure_rms`, `scope_measure_peak_to_peak`,
   `scope_measure_duty_cycle` with the `capture_id`.
6. Report values with units, `capture_id`, `valid`, warnings and the scaling
   status (`nominal_unverified` on real hardware until validated).

The `characterize_signal` prompt encodes this procedure.

## Configuration file

`hantek-mcp config` prints the effective configuration and the default file
location (`%APPDATA%\hantek-mcp\config.toml`, `~/.config/hantek-mcp/config.toml`).
Example:

```toml
backend = "usb"
log_level = "INFO"

[usb]
key_press_delay_s = 0.2      # settle time after each emulated key press
# bus = 1                   # select one of several scopes (iSerial is 0 on this family)
# address = 7

[server]
http_port = 8765
# auth_token = "a-long-random-token"

[limits]
max_response_samples = 5000
max_repeat_count = 100
max_workflow_duration_s = 600

[storage]
max_captures = 500
max_disk_mb = 1024

[scaling]
# Only after the hardware validation checklist confirmed them:
# voltage_verified = true
# time_verified = true
```

Environment variables override the file: `HANTEK_MCP_BACKEND=simulator`,
`HANTEK_MCP_SERVER__HTTP_PORT=9000`, …
