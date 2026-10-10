"""PyUSB/libusb transport for the DSO5102P (VID:PID 049f:505a).

* Linux: libusb-1.0 from the system; the kernel's ``cdc_subset`` driver may bind to
  the device and is detached on open (upstream behaviour) and re-attached on close.
* Windows: PyUSB needs a libusb-compatible driver bound to the device (WinUSB via
  Zadig is the documented route, see docs/windows-setup.md). ``libusb-package``
  ships ``libusb-1.0.dll`` so no manual DLL install is needed. This module never
  installs or replaces drivers.
"""

from __future__ import annotations

import errno
import logging
import sys
from dataclasses import dataclass
from typing import Any

from hantek_mcp.errors import (
    BackendUnavailableError,
    DeviceDisconnectedError,
    DeviceNotFoundError,
    NotConnectedError,
    ScopeTimeoutError,
    TransportError,
)
from hantek_mcp.transport.base import Transport, TransportInfo

log = logging.getLogger(__name__)

DEFAULT_VID = 0x049F
DEFAULT_PID = 0x505A
#: Endpoints observed on a DSO5102P by the upstream author (0x02 OUT, 0x81 IN, wMaxPacketSize 512).
EXPECTED_EP_OUT = 0x02
EXPECTED_EP_IN = 0x81
READ_CHUNK = 0x10000

_DISCONNECT_ERRNOS = {errno.ENODEV, errno.ENXIO, errno.ESHUTDOWN, errno.EPIPE}


def _import_usb() -> Any:
    try:
        import usb.core
        import usb.util

        return usb
    except ImportError as e:  # pragma: no cover - pyusb is a hard dependency
        raise BackendUnavailableError("PyUSB is not installed") from e


def load_backend() -> tuple[Any, str]:
    """Return ``(backend, description)`` choosing libusb-1.0, preferring the bundled DLL on Windows."""
    usb = _import_usb()
    errors: list[str] = []
    if sys.platform == "win32":
        try:
            import libusb_package

            be = libusb_package.get_libusb1_backend()
            if be is not None:
                return be, "libusb-1.0 (libusb-package bundled DLL)"
            errors.append("libusb-package returned no backend")
        except Exception as e:
            errors.append(f"libusb-package: {e}")
    try:
        import usb.backend.libusb1 as l1

        be = l1.get_backend()
        if be is not None:
            return be, "libusb-1.0 (system)"
        errors.append("system libusb-1.0 not found")
    except Exception as e:
        errors.append(f"libusb1: {e}")
    try:
        import usb.backend.libusb0 as l0

        be = l0.get_backend()
        if be is not None:
            return be, "libusb-0.1 / libusb-win32"
        errors.append("libusb-0.1 not found")
    except Exception as e:
        errors.append(f"libusb0: {e}")
    del usb
    raise BackendUnavailableError(
        "no usable libusb backend found. On Windows install the package with its dependencies "
        "(libusb-package) and bind WinUSB with Zadig; on Linux install libusb-1.0.",
        details={"attempts": errors},
    )


def _is_disconnect(exc: BaseException) -> bool:
    err = getattr(exc, "errno", None)
    backend_code = getattr(exc, "backend_error_code", None)
    text = str(exc).lower()
    return (
        err in _DISCONNECT_ERRNOS
        or backend_code == -4  # LIBUSB_ERROR_NO_DEVICE
        or "no such device" in text
        or "device not found" in text
        or "no device" in text
    )


@dataclass
class UsbSelector:
    vendor_id: int = DEFAULT_VID
    product_id: int = DEFAULT_PID
    bus: int | None = None
    address: int | None = None
    index: int = 0
    interface: int = 0
    endpoint_out: int | None = None
    endpoint_in: int | None = None


class UsbTransport(Transport):
    def __init__(self, selector: UsbSelector | None = None) -> None:
        self.selector = selector or UsbSelector()
        self._dev: Any = None
        self._ep_out: Any = None
        self._ep_in: Any = None
        self._detached_kernel_driver = False
        self._backend_desc = ""
        self._claimed = False

    # -- lifecycle -------------------------------------------------------------------------------

    def open(self) -> None:
        if self._dev is not None:
            return
        usb = _import_usb()
        backend, self._backend_desc = load_backend()
        sel = self.selector
        try:
            devices = list(
                usb.core.find(
                    find_all=True, idVendor=sel.vendor_id, idProduct=sel.product_id, backend=backend
                )
            )
        except usb.core.USBError as e:
            raise TransportError(f"USB enumeration failed: {e}") from e
        if sel.bus is not None:
            devices = [d for d in devices if d.bus == sel.bus]
        if sel.address is not None:
            devices = [d for d in devices if d.address == sel.address]
        if len(devices) <= sel.index:
            raise DeviceNotFoundError(
                f"no USB device {sel.vendor_id:04x}:{sel.product_id:04x} found"
                + (f" at bus={sel.bus} address={sel.address}" if sel.bus or sel.address else ""),
                details={"matches": len(devices), "index": sel.index, "backend": self._backend_desc},
            )
        dev = devices[sel.index]
        try:
            try:
                cfg = dev.get_active_configuration()
            except usb.core.USBError:
                dev.set_configuration()
                cfg = dev.get_active_configuration()
            intf = cfg[(sel.interface, 0)]
            if sys.platform.startswith("linux"):
                try:
                    if dev.is_kernel_driver_active(sel.interface):
                        dev.detach_kernel_driver(sel.interface)
                        self._detached_kernel_driver = True
                        log.info("detached kernel driver from interface %d", sel.interface)
                except (NotImplementedError, usb.core.USBError) as e:
                    log.debug("kernel driver check failed: %s", e)
            self._dev = dev  # from here on close() releases whatever was acquired
            usb.util.claim_interface(dev, sel.interface)
            self._claimed = True
            ep_out, ep_in = self._find_endpoints(usb, intf)
        except usb.core.USBError as e:
            self._dev = dev
            self.close()
            if getattr(e, "errno", None) in (errno.EACCES, errno.EPERM):
                raise TransportError(
                    "permission denied opening the USB device. On Linux install the udev rule from "
                    "scripts/99-hantek-dso.rules; on Windows bind WinUSB with Zadig.",
                    details={"error": str(e)},
                ) from e
            if getattr(e, "errno", None) == errno.EBUSY:
                raise TransportError("USB interface is busy (claimed by another program?)") from e
            raise TransportError(f"failed to open USB device: {e}") from e
        except BaseException:
            self._dev = dev
            self.close()  # release the interface and re-attach the kernel driver
            raise
        self._dev = dev
        self._ep_out = ep_out
        self._ep_in = ep_in
        log.info(
            "opened DSO on bus %s address %s (OUT 0x%02X, IN 0x%02X, wMaxPacketSize %s) via %s",
            dev.bus,
            dev.address,
            ep_out.bEndpointAddress,
            ep_in.bEndpointAddress,
            ep_in.wMaxPacketSize,
            self._backend_desc,
        )

    def _find_endpoints(self, usb: Any, intf: Any) -> tuple[Any, Any]:
        bulk = usb.util.ENDPOINT_TYPE_BULK
        eps = [e for e in intf if usb.util.endpoint_type(e.bmAttributes) == bulk]
        outs = [e for e in eps if usb.util.endpoint_direction(e.bEndpointAddress) == usb.util.ENDPOINT_OUT]
        ins = [e for e in eps if usb.util.endpoint_direction(e.bEndpointAddress) == usb.util.ENDPOINT_IN]

        def pick(cands: list[Any], wanted: int | None, expected: int, label: str) -> Any:
            if wanted is not None:
                for e in cands:
                    if e.bEndpointAddress == wanted:
                        return e
                raise TransportError(f"configured {label} endpoint 0x{wanted:02X} not present on interface")
            for e in cands:
                if e.bEndpointAddress == expected:
                    return e
            if cands:
                log.warning(
                    "expected %s endpoint 0x%02X not found; using 0x%02X",
                    label,
                    expected,
                    cands[0].bEndpointAddress,
                )
                return cands[0]
            raise TransportError(f"no bulk {label} endpoint on interface")

        return (
            pick(outs, self.selector.endpoint_out, EXPECTED_EP_OUT, "OUT"),
            pick(ins, self.selector.endpoint_in, EXPECTED_EP_IN, "IN"),
        )

    def close(self) -> None:
        dev = self._dev
        if dev is None:
            return
        usb = _import_usb()
        try:
            if self._claimed:
                usb.util.release_interface(dev, self.selector.interface)
        except Exception as e:
            log.debug("release_interface failed: %s", e)
        if self._detached_kernel_driver:
            try:
                dev.attach_kernel_driver(self.selector.interface)
            except Exception as e:
                log.debug("re-attaching kernel driver failed: %s", e)
        try:
            usb.util.dispose_resources(dev)
        except Exception as e:
            log.debug("dispose_resources failed: %s", e)
        self._dev = None
        self._ep_in = self._ep_out = None
        self._claimed = False
        self._detached_kernel_driver = False

    @property
    def is_open(self) -> bool:
        return self._dev is not None

    # -- I/O -------------------------------------------------------------------------------------

    def write(self, data: bytes, timeout_s: float) -> int:
        if self._dev is None:
            raise NotConnectedError("USB transport is not open")
        usb = _import_usb()
        try:
            n = int(self._dev.write(self._ep_out, data, int(timeout_s * 1000)))
        except usb.core.USBTimeoutError as e:
            raise ScopeTimeoutError(f"USB write timed out after {timeout_s:.2f}s") from e
        except usb.core.USBError as e:
            if _is_disconnect(e):
                raise DeviceDisconnectedError(f"device disconnected during write: {e}") from e
            raise TransportError(f"USB write failed: {e}") from e
        if n != len(data):
            raise TransportError(f"short USB write: {n} of {len(data)} bytes")
        return n

    def read(self, max_bytes: int, timeout_s: float) -> bytes:
        if self._dev is None:
            raise NotConnectedError("USB transport is not open")
        usb = _import_usb()
        mps = int(self._ep_in.wMaxPacketSize) or 64
        size = max(mps, (max(max_bytes, READ_CHUNK) // mps) * mps)
        try:
            data = self._dev.read(self._ep_in, size, int(max(timeout_s, 0.001) * 1000))
        except usb.core.USBTimeoutError as e:
            raise ScopeTimeoutError(f"USB read timed out after {timeout_s:.2f}s") from e
        except usb.core.USBError as e:
            if _is_disconnect(e):
                raise DeviceDisconnectedError(f"device disconnected during read: {e}") from e
            raise TransportError(f"USB read failed: {e}") from e
        return bytes(data)

    def info(self) -> TransportInfo:
        dev = self._dev
        return TransportInfo(
            backend="usb",
            simulated=False,
            description=f"PyUSB via {self._backend_desc}" if self._backend_desc else "PyUSB",
            vendor_id=self.selector.vendor_id,
            product_id=self.selector.product_id,
            bus=getattr(dev, "bus", None),
            address=getattr(dev, "address", None),
            endpoint_out=getattr(self._ep_out, "bEndpointAddress", None),
            endpoint_in=getattr(self._ep_in, "bEndpointAddress", None),
            max_packet_size=getattr(self._ep_in, "wMaxPacketSize", None),
            interface=self.selector.interface,
            extra={"kernel_driver_detached": self._detached_kernel_driver},
        )


def usb_diagnostics(vendor_id: int = DEFAULT_VID, product_id: int = DEFAULT_PID) -> dict[str, Any]:
    """Read-only USB diagnostics: backend availability and descriptors of matching devices.

    Never claims interfaces or sends protocol traffic.
    """
    out: dict[str, Any] = {
        "platform": sys.platform,
        "vendor_id": f"0x{vendor_id:04X}",
        "product_id": f"0x{product_id:04X}",
    }
    try:
        usb = _import_usb()
        out["pyusb_version"] = getattr(usb, "__version__", "unknown")
    except BackendUnavailableError as e:
        out["error"] = str(e)
        return out
    try:
        backend, desc = load_backend()
        out["backend"] = desc
    except BackendUnavailableError as e:
        out["backend"] = None
        out["backend_error"] = e.to_dict()
        return out
    devices: list[dict[str, Any]] = []
    try:
        found = list(usb.core.find(find_all=True, idVendor=vendor_id, idProduct=product_id, backend=backend))
    except Exception as e:
        out["enumeration_error"] = str(e)
        found = []
    for d in found:
        info: dict[str, Any] = {
            "bus": d.bus,
            "address": d.address,
            "bcdUSB": f"0x{d.bcdUSB:04X}",
            "bcdDevice": f"0x{d.bcdDevice:04X}",
            "bDeviceClass": d.bDeviceClass,
            "iSerialNumber": d.iSerialNumber,
            "speed": getattr(d, "speed", None),
            "configurations": [],
        }
        for cfg in d:
            c: dict[str, Any] = {"bConfigurationValue": cfg.bConfigurationValue, "interfaces": []}
            for intf in cfg:
                c["interfaces"].append(
                    {
                        "bInterfaceNumber": intf.bInterfaceNumber,
                        "bAlternateSetting": intf.bAlternateSetting,
                        "bInterfaceClass": intf.bInterfaceClass,
                        "endpoints": [
                            {
                                "address": f"0x{e.bEndpointAddress:02X}",
                                "type": ["control", "isochronous", "bulk", "interrupt"][e.bmAttributes & 3],
                                "wMaxPacketSize": e.wMaxPacketSize,
                            }
                            for e in intf
                        ],
                    }
                )
            info["configurations"].append(c)
        devices.append(info)
    out["devices"] = devices
    out["device_count"] = len(devices)
    if not devices:
        out["hint"] = (
            "No DSO found. Check the cable, that the scope is powered on, and (Windows) that WinUSB is "
            "bound with Zadig; (Linux) that the udev rule is installed."
        )
    return out
