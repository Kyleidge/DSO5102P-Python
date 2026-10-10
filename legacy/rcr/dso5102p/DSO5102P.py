# -*- coding: utf-8 -*-
#
# Backwards-compatible facade for the original rcr.dso5102p API (Roberto Carrasco, MIT).
#
# The original implementation lives in git history; this file now delegates to the
# hantek_mcp library so the legacy Test*.py scripts keep working with the robust
# transport (frame re-assembly, checksums, timeouts). Install the package first:
#
#     pip install -e .            (from the repository root)
#
# Differences from the original, documented in docs/usb-protocol.md:
#   * the constructor raises DeviceNotFoundError instead of printing and exit()-ing;
#   * ReadSampleData(channel) accepts 0/1 (as before, wire codes) and returns the raw
#     bytes exactly as before (array('B')); use hantek_mcp for signed/scaled data;
#   * Screenshot() keeps the legacy 8-bit result (low byte of each RGB565 pixel) so
#     Color-Sreenshot.py still works; ScreenshotRGB() returns the decoded colour image;
#   * RemoteShell() is removed for safety: arbitrary shell commands on the scope's
#     firmware can brick the instrument.
#
# Thanks to:
#   https://elinux.org/Das_Oszi_Protocol (protocolo)
#   https://randomprojects.org/wiki/Voltcraft_DSO-3062C (uso de usb)

import array

import numpy as np

from hantek_mcp.protocol.decoder import decode_screenshot
from hantek_mcp.protocol.session import ProtocolSession, SessionTimeouts
from hantek_mcp.transport.usb import UsbSelector, UsbTransport


class DSO5102P:
    def __init__(self, idVendor, idProduct, debug=False):
        self.debug = debug
        self._transport = UsbTransport(UsbSelector(vendor_id=idVendor, product_id=idProduct))
        self._transport.open()
        self._session = ProtocolSession(self._transport, SessionTimeouts(), command_delay_s=0.02)
        self._session.flush()

    def close(self):
        self._transport.close()

    def _dump(self, origin, data):
        if self.debug:
            print(origin, ":", ["0x%02X" % h for h in data])

    # OK
    def Echo(self, data):
        r = self._session.echo(bytes(data))
        self._dump("Echo", r)
        return list(r)

    def ReadSettings(self):
        return array.array("B", self._session.read_settings_blob())

    def ReadSampleData(self, channel):
        samples = self._session.read_samples((channel & 0x01) + 1)
        return array.array("B", samples.view(np.uint8).tobytes())

    # OK
    def ReadFile(self, fname):
        return self._session.read_file(fname).decode("latin-1")

    # OK
    def LockControlPanel(self):
        self._session.set_panel_lock(True)

    # OK
    def UnLockControlPanel(self):
        self._session.set_panel_lock(False)

    # OK
    def StartAcquisition(self):
        self._session.set_acquisition(True)

    # OK
    def StopAcquisition(self):
        self._session.set_acquisition(False)

    # OK
    def KeyTrigger(self, b1, b2):
        # b2 (press count) is ignored by the firmware beyond 1 (elinux); one press per call.
        self._session.press_key(b1)
        self._session.echo(b"flush")

    # OK - legacy 8-bit (low byte) image, kept for Color-Sreenshot.py
    def Screenshot(self):
        raw = self._session.screenshot_raw()
        img = np.frombuffer(raw, dtype=np.uint16).reshape(480, 800)
        return img.astype(np.uint8)

    def ScreenshotRGB(self):
        rgb, _fmt = decode_screenshot(self._session.screenshot_raw())
        return rgb

    # OK
    def ReadSystemTime(self):
        t = self._session.read_system_time()
        return t.strftime("%Y-%m-%d %H:%M:%S")

    def RemoteShell(self, cmdline):
        raise NotImplementedError(
            "RemoteShell was removed: running shell commands on the oscilloscope firmware can brick it"
        )
