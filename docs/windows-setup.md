# Windows 10/11 setup

## 1. Install Python and hantek-mcp

1. Install **Python 3.11 or newer** (64-bit) from <https://www.python.org/downloads/>
   or `winget install Python.Python.3.12`. Tick *Add python.exe to PATH*.
2. Get the code: download the repository ZIP from GitHub and extract it, or
   `git clone https://github.com/Kyleidge/DSO5102P-Python`. (Git is not needed
   once you have a wheel: `pip install hantek_mcp-<version>-py3-none-any.whl[gui]`.)
3. In PowerShell, from the repository folder:

   ```powershell
   powershell -ExecutionPolicy Bypass -File scripts\install_windows.ps1
   ```

   This creates `.venv`, installs the package with the GUI extra, runs a
   simulator self-check and prints read-only USB diagnostics. It **does not touch
   USB drivers**. Options: `-Dev` (test tools), `-NoGui`.

Manual alternative:

```powershell
py -3.12 -m venv .venv
.venv\Scripts\python -m pip install -e ".[gui]"
.venv\Scripts\hantek-mcp capture --simulate --measure frequency
```

`libusb-package` (installed automatically on Windows) provides
`libusb-1.0.dll`, so no separate DLL installation is required.

## 2. Bind WinUSB to the oscilloscope (Zadig)

PyUSB talks to the scope through libusb, which needs a generic driver such as
**WinUSB** bound to the device. Hantek's own driver (used by TTScope) is not
compatible; replacing it means TTScope will not see the scope until you roll
back (§3).

1. Connect the DSO5102P by USB and switch it on.
2. Download Zadig from <https://zadig.akeo.ie/> (official site only).
3. *Options → List All Devices*.
4. Select the device whose USB ID is **049F 505A** (it may be listed as
   "USB device", "DSO" or with a Hantek name). **Check the ID** — binding WinUSB
   to the wrong device (e.g. your keyboard or a USB hub) disables it.
5. Note the *current* driver shown on the left (for rollback).
6. Choose **WinUSB** on the right and click *Replace Driver* / *Install Driver*.
7. Verify:

   ```powershell
   .venv\Scripts\hantek-mcp diagnose     # device_count should be 1, backend libusb-1.0
   .venv\Scripts\hantek-mcp info         # connects and prints firmware + settings
   ```

libusbK or libusb-win32 also work with PyUSB; WinUSB is the default
recommendation.

## 3. Rollback to the original driver

1. *Device Manager* → find the scope (under *Universal Serial Bus devices* or
   *libusb/WinUSB devices*).
2. Right-click → *Properties → Driver → Roll Back Driver* (if available), **or**
   *Uninstall device*, tick *Delete the driver software for this device* (this
   removes only the WinUSB binding for this device), then unplug/replug.
3. Reinstall Hantek's driver from the TTScope / DSO5000P software package if
   Windows does not pick it up automatically.

## 4. Running

| Task | Command |
|---|---|
| GUI (hardware) | `scripts\run_gui.bat` |
| GUI (simulator) | `scripts\run_gui.bat --simulate` |
| GUI + MCP endpoint | `scripts\run_gui.bat --mcp-http` |
| MCP server for a client (stdio) | `<repo>\.venv\Scripts\hantek-mcp.exe serve --transport stdio` |
| Self-test with PROBE COMP | `.venv\Scripts\hantek-mcp selftest` |

Data (captures, exports, screenshots, lock files) lives in
`%LOCALAPPDATA%\hantek-mcp`; the optional config file is
`%APPDATA%\hantek-mcp\config.toml` (see `hantek-mcp config`).

## 5. Optional single-folder executable

`.github/workflows/windows-exe.yml` (manual trigger) or locally:

```powershell
.venv\Scripts\python -m pip install -e ".[gui,exe]"
.venv\Scripts\pyinstaller packaging\hantek-mcp.spec --noconfirm
dist\hantek-mcp\hantek-mcp.exe gui --simulate
```

The pip-installable package remains the primary distribution.

## 6. Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `USB_BACKEND_UNAVAILABLE` | `libusb-package` missing: reinstall with `pip install -e .`; check `hantek-mcp diagnose` |
| `DEVICE_NOT_FOUND` | Scope off/unplugged, cable is charge-only, or still on Hantek's driver (Zadig shows it but libusb cannot open it) |
| `TRANSPORT_ERROR ... Access denied` / `[Errno 13]` | Another program (TTScope, a second hantek-mcp) has the device, or WinUSB not bound |
| `DEVICE_BUSY` | Another hantek-mcp process owns the scope; the message names its PID. Use the GUI's MCP endpoint + `hantek-mcp bridge` to share one connection |
| `TIMEOUT` on every command | Reconnect USB; power-cycle the scope; check `hantek-mcp diagnose` endpoints are `0x02`/`0x81` |
| `SETTING_NOT_CONFIRMED` | A front-panel menu was open or the firmware's soft-menu layout differs; close menus and retry, then report via the validation checklist |
| GUI does not start | `pip install -e ".[gui]"`; on Windows N editions install the Media Feature Pack (Qt multimedia dependencies) |
