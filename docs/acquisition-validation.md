# Acquisition and hardware validation

**Status: no physical DSO5102P has been tested by this project yet.** Milestone E
(hardware compatibility) stays open until a run of this checklist is returned.
Everything else was implemented and tested against the protocol-level simulator
and documented protocol fixtures.

## Safety first

* Read the DSO5000P manual's safety section (`docs/manuals/`). Maximum input:
  300 V RMS CAT II with a 10× probe, 150 V RMS CAT III with 1×; derate above
  100 kHz.
* The scope's ground is earth-referenced. Never connect the probe ground clip to
  a point that is not at earth potential (mains circuits, non-isolated supplies).
* Software cannot make a connection safe. Automation can change V/div and
  coupling but cannot protect the input.

For this checklist only the scope's own **PROBE COMP** output is needed.

## Set-up

1. Install as in `windows-setup.md` (or Linux: `pip install -e ".[gui,dev]"` and
   `scripts/99-hantek-dso.rules`).
2. Connect the CH1 probe tip to PROBE COMP (≈5 V, 1 kHz square) and its ground
   clip to the PROBE COMP ground. If you have a second probe, connect CH2 too.
3. Set the probe switch and the scope's CH1 (and CH2) probe menu to the same
   factor (10× recommended).
4. On the scope: *Default Setup*, then trigger Edge / CH1 / Auto. Close menus.
5. `hantek-mcp diagnose` must list one device with endpoints `0x02` and `0x81`.

## Automated acceptance run

```bash
HANTEK_MCP_HARDWARE=1 pytest -m hardware tests/hardware -v -s
# Windows PowerShell:
$env:HANTEK_MCP_HARDWARE = "1"; .venv\Scripts\pytest -m hardware tests\hardware -v -s
```

| Test | Acceptance criterion | Milestone E item |
|---|---|---|
| `test_01_device_detected` | ≥1 device 049f:505a with bulk endpoints | DSO5102P detected under Windows |
| `test_02_identity` | firmware read, `/protocol.inf` read from the scope, key table matches | USB transport established |
| `test_03_settings_readback` | V/div and s/div decode to table values | Settings read-back confirmed |
| `test_04_waveform_download[1/2]` | non-empty int8 records for CH1 and CH2 | CH1/CH2 waveform downloaded |
| `test_05_probe_comp_scaling` | frequency within ±2 % of 1 kHz, top−base within ±10 % of 5 V | Voltage and time scaling verified |
| `test_06_trigger_and_freshness` | three single captures all `new_trigger_confirmed` | Trigger operation checked |
| `test_07_repeated_captures_not_stale` | running captures are not all byte-identical | Stale-buffer check |
| `test_08_settings_change_roundtrip` | V/div changed and restored, both confirmed by read-back | Key emulation works |
| `test_09_screenshot` | 800×480 image decoded | Screenshot |
| `test_10_mcp_stdio_end_to_end_on_hardware` | MCP client over stdio measures 1 kHz ±2 % with `simulated=false` | MCP client measures a physical signal |

Tolerances: the probe-comp output's accuracy is not specified by Hantek ("~5 V
@ 1 kHz"); ±2 % frequency and ±10 % amplitude separate a correct scaling model
from a wrong one (a wrong samples-per-division or counts-per-division would be
off by ≥20 %). For calibration-grade validation repeat with a calibrated
function generator (e.g. 1.000 kHz, 2.000 Vpp sine into 1 MΩ) and set
`HANTEK_MCP_PROBE_COMP_HZ` / `HANTEK_MCP_PROBE_COMP_VPP` accordingly.

## Manual checks (add notes to the report)

1. **Menu-dependent record length**: with the CH1 menu open and closed, run
   `hantek-mcp capture --mode stop_and_read` and note `sample_count` and
   `time_scaling.samples_per_div` (question 2 in `usb-protocol.md`).
2. **Memory depth**: set 4K/20K/40K in *Acquire → Memory depth*, capture, note the
   sample counts and whether `acquire_settings.memory_depth` matches.
3. **Offset sign**: `scope_set_vertical_offset(1, 2)`; the measured mean voltage
   must be unchanged (offset is subtracted correctly).
4. **Trigger level reference**: with CH1 offset at +2 div, call
   `scope_set_trigger_level(volts=2.5)`; the trigger marker on the scope must sit
   on the middle of the square.
5. **Soft-menu mapping**: `scope_set_coupling(1, "AC")`, `scope_set_probe_attenuation(1, 1)`,
   `scope_set_trigger_mode("normal")` all confirmed by read-back.
6. **Disconnect**: unplug USB during `hantek-mcp gui` live refresh: the status
   badge must show `LOST`; reconnect with *Connect*.

## What to send back

* `hardware-validation-report.json` and the `hardware-artifacts/` folder
  (settings blob, `protocol.inf`, raw CH1/CH2 records, screenshot).
* The scope's firmware string (*Utility → System info*) if `identity.firmware`
  is empty.
* Notes from the manual checks.

The developer will add the artefacts to `tests/fixtures/` as regression data,
correct the scaling model if needed and, once the criteria pass, set
`scaling.voltage_verified` / `time_verified` defaults and mark the features
`hardware_confirmed` in `capabilities.py`.

## Applying a correction locally

`scope_run_self_test` reports `calibration_suggestions` (it never applies them):

```toml
[scaling]
counts_per_div = 24.6          # from counts_per_div_if_amplitude_error_is_scaling
samples_per_div_override = 200 # from samples_per_div_if_frequency_error_is_scaling
```

Only set `voltage_verified = true` / `time_verified = true` after a passing run
against a reference you trust; until then every result says
`nominal_unverified`.
