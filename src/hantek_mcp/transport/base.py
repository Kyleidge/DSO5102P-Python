"""Transport abstraction: a byte pipe to the instrument's bulk endpoints."""

from __future__ import annotations

import abc
from dataclasses import dataclass, field
from typing import Any


@dataclass
class TransportInfo:
    backend: str  # "usb" | "simulator" | "replay"
    simulated: bool
    description: str
    vendor_id: int | None = None
    product_id: int | None = None
    bus: int | None = None
    address: int | None = None
    endpoint_out: int | None = None
    endpoint_in: int | None = None
    max_packet_size: int | None = None
    interface: int | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        d = {k: v for k, v in self.__dict__.items() if k != "extra"}
        for key in ("vendor_id", "product_id", "endpoint_out", "endpoint_in"):
            if d.get(key) is not None:
                d[key + "_hex"] = f"0x{d[key]:04X}" if "id" in key else f"0x{d[key]:02X}"
        d.update(self.extra)
        return d


class Transport(abc.ABC):
    """Raw byte transport. Implementations must be used from one thread at a time;
    :class:`hantek_mcp.protocol.session.ProtocolSession` provides the serialisation."""

    @abc.abstractmethod
    def open(self) -> None: ...

    @abc.abstractmethod
    def close(self) -> None: ...

    @property
    @abc.abstractmethod
    def is_open(self) -> bool: ...

    @abc.abstractmethod
    def write(self, data: bytes, timeout_s: float) -> int:
        """Write all of ``data``; returns the number of bytes written."""

    @abc.abstractmethod
    def read(self, max_bytes: int, timeout_s: float) -> bytes:
        """Read up to ``max_bytes``. Raises ScopeTimeoutError if nothing arrives in time."""

    @abc.abstractmethod
    def info(self) -> TransportInfo: ...

    def __enter__(self) -> Transport:
        self.open()
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
