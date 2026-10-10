"""Cross-process single-owner lock for the USB instrument.

Only one process may talk to the scope at a time: interleaved request/response
traffic from two processes would corrupt both sessions. The GUI and the MCP
server therefore share one in-process controller (the GUI can host the MCP HTTP
endpoint), and this OS-level lock turns any second process into a clear
``DEVICE_BUSY`` error instead of silent corruption.
"""

from __future__ import annotations

import contextlib
import json
import os
import sys
from pathlib import Path
from typing import IO, Any

from hantek_mcp.errors import DeviceBusyError


class OwnershipLock:
    """Exclusive OS lock on ``<name>.lock``; owner details live in ``<name>.owner.json``.

    The owner description is kept in a separate file because Windows byte-range locks
    are mandatory: a second process could not even read a locked file to report who
    owns it.
    """

    def __init__(self, lock_dir: Path, name: str) -> None:
        self.path = Path(lock_dir) / f"{name}.lock"
        self.owner_path = Path(lock_dir) / f"{name}.owner.json"
        self._fh: IO[str] | None = None

    @property
    def held(self) -> bool:
        return self._fh is not None

    def acquire(self, description: str) -> None:
        if self._fh is not None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fh = self.path.open("a+", encoding="utf-8")
        try:
            _lock(fh)
        except OSError as e:
            fh.close()
            owner = self.owner()
            raise DeviceBusyError(
                "the instrument is already in use by another process"
                + (f" (pid {owner.get('pid')}: {owner.get('description')})" if owner else ""),
                details={"lock_file": str(self.path), "owner": owner},
            ) from e
        self._fh = fh
        with contextlib.suppress(OSError):  # owner details are informational only
            self.owner_path.write_text(json.dumps({"pid": os.getpid(), "description": description}), "utf-8")

    def owner(self) -> dict[str, Any]:
        try:
            return _read_owner(self.owner_path.read_text("utf-8"))
        except OSError:
            return {}

    def release(self) -> None:
        fh = self._fh
        if fh is None:
            return
        with contextlib.suppress(OSError):
            self.owner_path.unlink(missing_ok=True)
        try:
            _unlock(fh)
        finally:
            fh.close()
            self._fh = None


def _read_owner(text: str) -> dict[str, Any]:
    try:
        data = json.loads(text)
        return data if isinstance(data, dict) else {}
    except ValueError:
        return {}


if sys.platform == "win32":
    import msvcrt

    def _lock(fh: IO[str]) -> None:
        fh.seek(0)
        msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)

    def _unlock(fh: IO[str]) -> None:
        fh.seek(0)
        msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)

else:
    import fcntl

    def _lock(fh: IO[str]) -> None:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _unlock(fh: IO[str]) -> None:
        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
