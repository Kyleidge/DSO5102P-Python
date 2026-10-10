# Contributing

Thanks for helping make the DSO5102P a reliable AI-accessible instrument.

## Most valuable contribution: hardware validation

If you own a DSO5102P (or another DSO5000-family scope), run
[docs/acquisition-validation.md](docs/acquisition-validation.md) and open an
issue with the report and artefacts. Recorded protocol data becomes regression
fixtures in `tests/fixtures/`.

## Development set-up

```bash
uv sync --locked --extra dev --extra gui    # or: pip install -e ".[dev,gui]"
uv run pytest                               # hardware tests are excluded by default
uv run ruff check src tests
uv run ruff format src tests
uv run mypy                                 # strict
```

## Rules of the road

* **Never fabricate data.** Anything that is not measured must be labelled
  (simulated, nominal, inferred, unavailable). Do not substitute simulated
  values for hardware data.
* **Verify settings by read-back.** New setters must confirm the instrument's
  reported value and raise `SettingNotConfirmedError` otherwise.
* **Keep the safety boundary.** No raw command, shell, firmware/file-write or
  arbitrary-path functionality in MCP, the GUI or the CLI. New protocol commands
  must be added to the allow-list in `protocol/commands.py` with a justification.
* **Bound everything.** Loops, repetitions, response sizes and disk use need
  limits from `LimitsConfig`; long operations must call `check_abort()`.
* **Document provenance.** Protocol facts go into `docs/usb-protocol.md` with
  their source and status (confirmed / inferred / unresolved).
* **Licences.** This project is MIT. Do not copy code from GPL projects such as
  dsoc-extended; describing protocol facts is fine.
* Tests: add unit tests for pure logic, simulator tests for instrument
  behaviour, and keep `tests/hardware` free of assumptions that the simulator
  makes. Regenerate `docs/tool-reference.md` when tools change.
* Commits: small, reviewable, imperative subject lines.
