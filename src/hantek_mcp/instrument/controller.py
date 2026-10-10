"""The instrument controller: single coordinated owner of one DSO5102P connection.

All hardware access in a process goes through one :class:`ScopeController`. Each
operation runs under the controller's re-entrant lock, so concurrent callers (GUI
worker threads, MCP tool calls) are serialised at operation granularity and can
never interleave frames of two transactions. A cross-process lock file prevents a
second process from opening the same USB device.
"""

from __future__ import annotations

from typing import Any

from hantek_mcp.config import AppConfig
from hantek_mcp.instrument.acquisition import AcquisitionMixin
from hantek_mcp.instrument.core import TransportFactory


class ScopeController(AcquisitionMixin):
    def __init__(self, config: AppConfig, transport_factory: TransportFactory | None = None) -> None:
        super().__init__(config, transport_factory)
        self._init_acquisition()

    def status(self) -> dict[str, Any]:
        st = self.cached_settings
        return {
            "connection_state": str(self.state),
            "connected": self.is_connected,
            "backend": self.config.backend,
            "simulated": self.simulated,
            "model": self.model_name,
            "firmware": self.sys_info.get("soft version"),
            "connected_at_utc": self.connected_at,
            "busy_operation": self.busy_operation,
            "abort_requested": self.abort_event.is_set(),
            "trigger_state": st.trigger.state if st else None,
            "settings_write_method": self.write_method,
            "scaling": {
                "voltage_verified": self.calibration.voltage_verified,
                "time_verified": self.calibration.time_verified,
                "counts_per_div": self.calibration.counts_per_div,
            },
            "recent_error_count": len(self.recent_errors),
        }

    def protocol_diagnostics(self) -> dict[str, Any]:
        s = self.session
        return {
            "connected": self.is_connected,
            "session_stats": s.stats.as_dict() if s else None,
            "settings_layout": {
                "source": self.layout.source,
                "fields": len(self.layout.fields),
                "size_bytes": self.layout.size,
                "from_instrument": self.layout_from_instrument,
            },
            "key_table_matches_dso5102p": self.key_names_match,
            "sys_inf": dict(self.sys_info),
            "settings_write_method": self.write_method,
        }
