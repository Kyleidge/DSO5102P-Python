"""Capture store: persisted waveform records, bounded by count and disk usage.

Layout under the configured data directory::

    captures/<capture_id>.npz   complete record (raw counts, time, volts, metadata JSON)
    exports/<name>.csv|.npz     user/agent-requested exports

Only names - never caller-supplied paths - are accepted from the MCP layer, and
every resolved path is checked to stay inside its directory.
"""

from __future__ import annotations

import csv
import io
import json
import logging
import re
import threading
from collections import OrderedDict
from pathlib import Path
from typing import Any

import numpy as np

from hantek_mcp.errors import CaptureNotFoundError, ParameterError, PathNotAllowedError, StorageError
from hantek_mcp.models import WaveformCapture

log = logging.getLogger(__name__)

_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_. -]{0,99}$")
CSV_MAGIC = "# hantek-mcp waveform capture v1"
_CACHE_SIZE = 32


def validate_capture_id(capture_id: str) -> str:
    if not isinstance(capture_id, str) or not _ID_RE.match(capture_id) or ".." in capture_id:
        raise ParameterError(f"invalid capture id {capture_id!r}")
    return capture_id


def safe_filename(name: str, suffix: str) -> str:
    """Validate an export file *name* (no directories) and force the extension."""
    base = name.strip()
    if base.lower().endswith(suffix):
        base = base[: -len(suffix)]
    if not base or not _NAME_RE.match(base) or ".." in base or base.upper().split(".")[0] in _WIN_RESERVED:
        raise PathNotAllowedError(
            f"invalid export name {name!r}: use letters, digits, space, '_', '-', '.' (no directories)"
        )
    return base + suffix


_WIN_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}


def _inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


class CaptureStore:
    def __init__(
        self, data_dir: Path, max_captures: int = 500, max_disk_mb: float = 1024.0, persist: bool = True
    ) -> None:
        self.root = Path(data_dir)
        self.capture_dir = self.root / "captures"
        self.export_dir = self.root / "exports"
        self.max_captures = max_captures
        self.max_disk_bytes = int(max_disk_mb * 1024 * 1024)
        self.persist = persist
        self._index: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self._sizes: dict[str, int] = {}
        self._cache: OrderedDict[str, WaveformCapture] = OrderedDict()
        self._memory_only: dict[str, WaveformCapture] = {}
        self._lock = threading.RLock()
        self.evicted: list[str] = []
        if persist:
            self.capture_dir.mkdir(parents=True, exist_ok=True)
            self.export_dir.mkdir(parents=True, exist_ok=True)
            self._load_index()

    # -- index -----------------------------------------------------------------------------------

    def _load_index(self) -> None:
        entries = []
        for p in self.capture_dir.glob("*.npz"):
            try:
                with np.load(p, allow_pickle=False) as npz:
                    meta = json.loads(str(npz["metadata_json"]))
                entries.append((meta["timestamp_utc"], meta, p.stat().st_size))
            except Exception as e:  # noqa: BLE001 - skip unreadable files but say so
                log.warning("ignoring unreadable capture file %s: %s", p.name, e)
        for _, meta, size in sorted(entries, key=lambda e: e[0]):
            cid = meta["capture_id"]
            self._index[cid] = _summary_from_meta(meta)
            self._sizes[cid] = size

    @property
    def disk_bytes(self) -> int:
        return sum(self._sizes.values())

    def __len__(self) -> int:
        return len(self._index)

    def __contains__(self, capture_id: object) -> bool:
        return capture_id in self._index

    # -- CRUD ------------------------------------------------------------------------------------

    def add(self, cap: WaveformCapture) -> None:
        validate_capture_id(cap.capture_id)
        with self._lock:
            if self.persist:
                path = self.capture_dir / f"{cap.capture_id}.npz"
                tmp = path.with_suffix(".tmp.npz")
                try:
                    np.savez_compressed(tmp, **cap.to_npz_dict())
                    tmp.replace(path)
                except OSError as e:
                    raise StorageError(f"could not save capture: {e}") from e
                self._sizes[cap.capture_id] = path.stat().st_size
            else:
                self._memory_only[cap.capture_id] = cap
                self._sizes[cap.capture_id] = cap.raw_samples.nbytes + cap.time_s.nbytes + (
                    0 if cap.voltage_v is None else cap.voltage_v.nbytes
                )
            self._index[cap.capture_id] = cap.summary()
            self._remember(cap)
            self._enforce_limits(keep=cap.capture_id)

    def _remember(self, cap: WaveformCapture) -> None:
        self._cache[cap.capture_id] = cap
        self._cache.move_to_end(cap.capture_id)
        while len(self._cache) > _CACHE_SIZE:
            self._cache.popitem(last=False)

    def _enforce_limits(self, keep: str) -> None:
        while len(self._index) > self.max_captures or self.disk_bytes > self.max_disk_bytes:
            oldest = next((k for k in self._index if k != keep), None)
            if oldest is None:
                break
            log.info("capture store limit reached; evicting %s", oldest)
            self._delete(oldest)
            self.evicted.append(oldest)
            del self.evicted[:-100]

    def get(self, capture_id: str) -> WaveformCapture:
        validate_capture_id(capture_id)
        with self._lock:
            if capture_id not in self._index:
                raise CaptureNotFoundError(f"no capture with id {capture_id!r}")
            if capture_id in self._cache:
                self._cache.move_to_end(capture_id)
                return self._cache[capture_id]
            if not self.persist:
                return self._memory_only[capture_id]
            path = self.capture_dir / f"{capture_id}.npz"
            try:
                with np.load(path, allow_pickle=False) as npz:
                    cap = WaveformCapture.from_npz(npz)
            except (OSError, KeyError, ValueError) as e:
                raise StorageError(f"could not load capture {capture_id}: {e}") from e
            self._remember(cap)
            return cap

    def summary(self, capture_id: str) -> dict[str, Any]:
        validate_capture_id(capture_id)
        try:
            return dict(self._index[capture_id])
        except KeyError:
            raise CaptureNotFoundError(f"no capture with id {capture_id!r}") from None

    def list(self, limit: int = 50, channel: int | None = None, label: str | None = None) -> list[dict[str, Any]]:
        with self._lock:
            items = list(reversed(self._index.values()))
        if channel is not None:
            items = [i for i in items if i["channel"] == channel]
        if label is not None:
            items = [i for i in items if i.get("label") == label]
        return items[: max(0, limit)]

    def delete(self, capture_id: str) -> None:
        validate_capture_id(capture_id)
        with self._lock:
            if capture_id not in self._index:
                raise CaptureNotFoundError(f"no capture with id {capture_id!r}")
            self._delete(capture_id)

    def _delete(self, capture_id: str) -> None:
        self._index.pop(capture_id, None)
        self._sizes.pop(capture_id, None)
        self._cache.pop(capture_id, None)
        self._memory_only.pop(capture_id, None)
        if self.persist:
            p = self.capture_dir / f"{capture_id}.npz"
            try:
                p.unlink(missing_ok=True)
            except OSError as e:
                log.warning("could not delete %s: %s", p, e)

    # -- export / import -------------------------------------------------------------------------

    def _export_path(self, name: str | None, default: str, suffix: str, overwrite: bool) -> Path:
        self.export_dir.mkdir(parents=True, exist_ok=True)
        fname = safe_filename(name or default, suffix)
        path = self.export_dir / fname
        if not _inside(path, self.export_dir):
            raise PathNotAllowedError("export path escapes the export directory")
        if path.exists() and not overwrite:
            raise PathNotAllowedError(f"{fname} already exists; pass overwrite=true or choose another name")
        return path

    def export_csv(self, capture_id: str, name: str | None = None, overwrite: bool = False) -> Path:
        cap = self.get(capture_id)
        path = self._export_path(name, capture_id, ".csv", overwrite)
        path.write_text(capture_to_csv(cap), encoding="utf-8", newline="")
        return path

    def export_npz(self, capture_id: str, name: str | None = None, overwrite: bool = False) -> Path:
        cap = self.get(capture_id)
        path = self._export_path(name, capture_id, ".npz", overwrite)
        np.savez_compressed(path, **cap.to_npz_dict())
        return path

    def list_exports(self) -> list[dict[str, Any]]:
        if not self.export_dir.is_dir():
            return []
        return [
            {"name": p.name, "bytes": p.stat().st_size}
            for p in sorted(self.export_dir.iterdir())
            if p.suffix in (".csv", ".npz")
        ]

    def import_export(self, name: str) -> WaveformCapture:
        """Reload a file from the export directory (by name) into the store."""
        suffix = Path(name).suffix.lower()
        if suffix not in (".csv", ".npz"):
            raise PathNotAllowedError("only .csv and .npz exports can be imported")
        path = self.export_dir / safe_filename(name, suffix)
        if not _inside(path, self.export_dir) or not path.is_file():
            raise CaptureNotFoundError(f"export {name!r} not found")
        cap = load_capture_file(path)
        if cap.capture_id in self._index:
            cap.capture_id = f"{cap.capture_id}-reloaded"
        cap.warnings.append(f"reloaded from export file {path.name}")
        self.add(cap)
        return cap


def _summary_from_meta(meta: dict[str, Any]) -> dict[str, Any]:
    acq = meta.get("acquisition", {})
    return {
        "capture_id": meta["capture_id"],
        "label": meta.get("label"),
        "channel": meta["channel"],
        "timestamp_utc": meta["timestamp_utc"],
        "simulated": meta.get("simulated", False),
        "sample_count": meta.get("sample_count"),
        "sample_rate_hz": meta.get("sample_rate_hz"),
        "voltage_status": meta.get("voltage_scaling", {}).get("status"),
        "time_status": meta.get("time_scaling", {}).get("status"),
        "freshness": acq.get("freshness"),
        "group_id": acq.get("group_id"),
        "clipped": bool(meta.get("quality", {}).get("clipped", False)),
    }


def capture_to_csv(cap: WaveformCapture) -> str:
    buf = io.StringIO()
    buf.write(CSV_MAGIC + "\n")
    buf.write("# metadata: " + json.dumps(cap.metadata(), separators=(",", ":")) + "\n")
    if cap.simulated:
        buf.write("# SIMULATED DATA - not from a physical instrument\n")
    buf.write(f"# voltage scaling status: {cap.voltage_status}; time scaling status: {cap.time_status}\n")
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(["index", "time_s", "raw_counts", "voltage_v"])
    volts = cap.voltage_v
    for i in range(cap.sample_count):
        w.writerow([i, repr(float(cap.time_s[i])), int(cap.raw_samples[i]),
                    "" if volts is None else repr(float(volts[i]))])  # fmt: skip
    return buf.getvalue()


def load_capture_file(path: Path) -> WaveformCapture:
    """Load a capture previously written by :func:`capture_to_csv` or ``export_npz``."""
    if path.suffix.lower() == ".npz":
        try:
            with np.load(path, allow_pickle=False) as npz:
                return WaveformCapture.from_npz(npz)
        except (OSError, KeyError, ValueError) as e:
            raise StorageError(f"{path.name} is not a hantek-mcp NPZ capture: {e}") from e
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    if not lines or lines[0] != CSV_MAGIC or not lines[1].startswith("# metadata: "):
        raise StorageError(f"{path.name} is not a hantek-mcp CSV capture")
    meta = json.loads(lines[1][len("# metadata: ") :])
    rows = [ln for ln in lines if ln and not ln.startswith("#")]
    reader = csv.reader(rows[1:])
    t, raw, v = [], [], []
    for r in reader:
        t.append(float(r[1]))
        raw.append(int(r[2]))
        v.append(float(r[3]) if r[3] else np.nan)
    arrays: dict[str, Any] = {
        "raw_samples": np.array(raw, dtype=np.int8),
        "time_s": np.array(t),
        "metadata_json": np.array(json.dumps(meta)),
    }
    if meta.get("voltage_available"):
        arrays["voltage_v"] = np.array(v)
    return WaveformCapture.from_npz(arrays)
