"""Structured exception hierarchy.

Every error raised by the library carries a stable machine-readable ``code`` so
that the MCP layer, the GUI and scripts can react to failures without parsing
messages. ``details`` holds JSON-serialisable context (observed values, limits).
"""

from __future__ import annotations

from typing import Any


class HantekError(Exception):
    """Base class for all errors raised by :mod:`hantek_mcp`."""

    code: str = "HANTEK_ERROR"
    #: Whether retrying the *same* operation might succeed without user action.
    retryable: bool = False

    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details: dict[str, Any] = dict(details or {})

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "retryable": self.retryable,
            "details": self.details,
        }

    def __str__(self) -> str:
        return f"[{self.code}] {self.message}"


# --- connection / transport -------------------------------------------------------------------


class NotConnectedError(HantekError):
    code = "NOT_CONNECTED"


class DeviceNotFoundError(HantekError):
    code = "DEVICE_NOT_FOUND"


class DeviceBusyError(HantekError):
    """Another process (or another controller in this process) owns the instrument."""

    code = "DEVICE_BUSY"


class BackendUnavailableError(HantekError):
    """No usable USB backend (libusb) could be loaded."""

    code = "USB_BACKEND_UNAVAILABLE"


class TransportError(HantekError):
    code = "TRANSPORT_ERROR"
    retryable = True


class DeviceDisconnectedError(TransportError):
    code = "DEVICE_DISCONNECTED"
    retryable = False


class ScopeTimeoutError(TransportError):
    code = "TIMEOUT"


# --- protocol ---------------------------------------------------------------------------------


class ProtocolError(HantekError):
    code = "PROTOCOL_ERROR"
    retryable = True


class ChecksumError(ProtocolError):
    code = "CHECKSUM_ERROR"


class FramingError(ProtocolError):
    code = "FRAMING_ERROR"


class UnexpectedResponseError(ProtocolError):
    code = "UNEXPECTED_RESPONSE"


class ForbiddenCommandError(HantekError):
    """Raised when code attempts to send a command outside the safety allow-list."""

    code = "FORBIDDEN_COMMAND"


# --- validation / capability ------------------------------------------------------------------


class ParameterError(HantekError, ValueError):
    code = "INVALID_PARAMETER"


class UnsupportedOperationError(HantekError):
    code = "UNSUPPORTED"


class SettingNotConfirmedError(HantekError):
    """A setting command was sent but read-back did not confirm the requested value."""

    code = "SETTING_NOT_CONFIRMED"


# --- acquisition ------------------------------------------------------------------------------


class AcquisitionError(HantekError):
    code = "ACQUISITION_ERROR"


class NoSampleDataError(AcquisitionError):
    """The instrument reported that no sample data is available (protocol sub-command 0x03)."""

    code = "NO_SAMPLE_DATA"
    retryable = True


class TriggerTimeoutError(AcquisitionError):
    code = "TRIGGER_TIMEOUT"
    retryable = True


class ScalingUnavailableError(AcquisitionError):
    code = "SCALING_UNAVAILABLE"


class AbortedError(HantekError):
    code = "ABORTED"


class LimitExceededError(HantekError):
    code = "LIMIT_EXCEEDED"


# --- storage ----------------------------------------------------------------------------------


class StorageError(HantekError):
    code = "STORAGE_ERROR"


class CaptureNotFoundError(StorageError):
    code = "CAPTURE_NOT_FOUND"


class PathNotAllowedError(StorageError):
    code = "PATH_NOT_ALLOWED"
