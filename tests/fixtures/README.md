# Protocol fixtures

* `elinux_frames.json` - request frames with checksums exactly as published on
  elinux.org "Das Oszi Protocol" (used to validate the frame encoder).
* The DSO5102P's own `/protocol.inf`, `/keyprotocol.inf` and `/sys.inf`
  (firmware 3.40.0(200217.0)) captured by the upstream project live in
  `src/hantek_mcp/protocol/data/dso5102p_fw3.40.0/` and are used as fixtures too.

No raw sample/settings captures from a real DSO5102P exist yet. When a hardware
validation run is performed (`tests/hardware`), its recorded frames should be
added here so the decoders can be regression-tested against real data.
