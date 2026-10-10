"""Core data model shared by the controller, storage, analysis, MCP server and GUI."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

import numpy as np
import numpy.typing as npt


class DataSource(StrEnum):
    HARDWARE = "hardware"
    SIMULATOR = "simulator"


class Freshness(StrEnum):
    """How confident we are that a record is a *new* acquisition.

    NEW_TRIGGER_CONFIRMED  single-sequence was armed by us and the trigger state was
                           observed to complete (ready/armed -> stop) before download.
    FORCED_NEW             armed by us and completed via Force Trigger (untriggered data).
    STOPPED_SNAPSHOT       acquisition stopped by us, then the frozen buffer was read; it is
                           the most recent record but may pre-date the request.
    RUNNING_SNAPSHOT       read while running; the buffer may be updated mid-download or
                           may be a stale record when the trigger is not firing.
    """

    NEW_TRIGGER_CONFIRMED = "new_trigger_confirmed"
    FORCED_NEW = "forced_new"
    STOPPED_SNAPSHOT = "stopped_snapshot"
    RUNNING_SNAPSHOT = "running_snapshot"


@dataclass
class AcquisitionInfo:
    mode: str  # "single" | "stop_and_read" | "running"
    freshness: Freshness
    trigger_state_before: str | None
    trigger_state_after: str | None
    trigger_observed: bool
    settings_changed_during_capture: bool = False
    group_id: str | None = None
    simultaneity: str | None = None
    wait_s: float | None = None
    download_s: float | None = None

    def as_dict(self) -> dict[str, Any]:
        d = dict(self.__dict__)
        d["freshness"] = str(self.freshness)
        return d


@dataclass
class WaveformCapture:
    capture_id: str
    channel: int
    timestamp_utc: datetime
    raw_samples: npt.NDArray[np.int8]
    time_s: npt.NDArray[np.float64]
    voltage_v: npt.NDArray[np.float64] | None
    source: DataSource
    horizontal: dict[str, Any]
    vertical: dict[str, Any]
    channel_settings: dict[str, Any]
    trigger_settings: dict[str, Any]
    timebase_settings: dict[str, Any]
    acquire_settings: dict[str, Any]
    acquisition: AcquisitionInfo
    quality: dict[str, Any] = field(default_factory=dict)
    instrument: dict[str, Any] = field(default_factory=dict)
    label: str | None = None
    warnings: list[str] = field(default_factory=list)

    @property
    def simulated(self) -> bool:
        return self.source == DataSource.SIMULATOR

    @property
    def sample_count(self) -> int:
        return int(self.raw_samples.size)

    @property
    def sample_interval_s(self) -> float | None:
        v = self.horizontal.get("sample_interval_s")
        return None if v is None else float(v)

    @property
    def sample_rate_hz(self) -> float | None:
        dt = self.sample_interval_s
        return None if not dt else 1.0 / dt

    @property
    def voltage_status(self) -> str:
        return str(self.vertical.get("status", "unavailable"))

    @property
    def time_status(self) -> str:
        return str(self.horizontal.get("status", "unavailable"))

    @property
    def raw_sha1(self) -> str:
        return hashlib.sha1(self.raw_samples.tobytes(), usedforsecurity=False).hexdigest()

    def metadata(self) -> dict[str, Any]:
        """Compact JSON-safe description (no sample arrays)."""
        return {
            "capture_id": self.capture_id,
            "label": self.label,
            "channel": self.channel,
            "timestamp_utc": self.timestamp_utc.isoformat(),
            "source": str(self.source),
            "simulated": self.simulated,
            "sample_count": self.sample_count,
            "sample_interval_s": self.sample_interval_s,
            "sample_rate_hz": self.sample_rate_hz,
            "record_duration_s": None
            if self.sample_interval_s is None
            else self.sample_interval_s * self.sample_count,
            "time_range_s": [float(self.time_s[0]), float(self.time_s[-1])] if self.sample_count else None,
            "voltage_scaling": self.vertical,
            "time_scaling": self.horizontal,
            "voltage_available": self.voltage_v is not None,
            "channel_settings": self.channel_settings,
            "trigger_settings": self.trigger_settings,
            "timebase_settings": self.timebase_settings,
            "acquire_settings": self.acquire_settings,
            "acquisition": self.acquisition.as_dict(),
            "quality": self.quality,
            "instrument": self.instrument,
            "raw_sha1": self.raw_sha1,
            "warnings": list(self.warnings),
        }

    def summary(self) -> dict[str, Any]:
        """Smaller description used in listings."""
        return {
            "capture_id": self.capture_id,
            "label": self.label,
            "channel": self.channel,
            "timestamp_utc": self.timestamp_utc.isoformat(),
            "simulated": self.simulated,
            "sample_count": self.sample_count,
            "sample_rate_hz": self.sample_rate_hz,
            "voltage_status": self.voltage_status,
            "time_status": self.time_status,
            "freshness": str(self.acquisition.freshness),
            "group_id": self.acquisition.group_id,
            "clipped": bool(self.quality.get("clipped", False)),
        }

    # -- (de)serialisation -----------------------------------------------------------------------

    def to_npz_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "raw_samples": self.raw_samples,
            "time_s": self.time_s,
            "metadata_json": np.array(json.dumps(self.metadata())),
        }
        if self.voltage_v is not None:
            d["voltage_v"] = self.voltage_v
        return d

    @classmethod
    def from_npz(cls, npz: Any) -> WaveformCapture:
        meta = json.loads(str(npz["metadata_json"]))
        acq = dict(meta["acquisition"])
        acq["freshness"] = Freshness(acq["freshness"])
        return cls(
            capture_id=meta["capture_id"],
            channel=int(meta["channel"]),
            timestamp_utc=datetime.fromisoformat(meta["timestamp_utc"]),
            raw_samples=np.asarray(npz["raw_samples"], dtype=np.int8),
            time_s=np.asarray(npz["time_s"], dtype=np.float64),
            voltage_v=np.asarray(npz["voltage_v"], dtype=np.float64) if "voltage_v" in npz else None,
            source=DataSource(meta["source"]),
            horizontal=meta["time_scaling"],
            vertical=meta["voltage_scaling"],
            channel_settings=meta["channel_settings"],
            trigger_settings=meta["trigger_settings"],
            timebase_settings=meta["timebase_settings"],
            acquire_settings=meta["acquire_settings"],
            acquisition=AcquisitionInfo(**acq),
            quality=meta.get("quality", {}),
            instrument=meta.get("instrument", {}),
            label=meta.get("label"),
            warnings=list(meta.get("warnings", [])),
        )


def utcnow() -> datetime:
    return datetime.now(UTC)
