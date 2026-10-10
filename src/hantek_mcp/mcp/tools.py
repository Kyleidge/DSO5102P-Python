"""MCP tool definitions.

Every tool is a thin, typed adapter over :class:`ScopeService`. Blocking
instrument work runs in a worker thread so the MCP event loop stays responsive
(``scope_abort`` and status calls are served while a capture is in progress).
Errors are raised as ``ToolError`` carrying a JSON object with a stable
``code``, a message, ``retryable`` and ``details``.
"""

from __future__ import annotations

import functools
import inspect
import json
import logging
import time
from collections.abc import Callable
from typing import Annotated, Any, Literal, TypeVar

import anyio
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp_types import CallToolResult, ImageContent, TextContent, ToolAnnotations
from pydantic import BaseModel, Field

from hantek_mcp.errors import HantekError, UnsupportedOperationError
from hantek_mcp.service import CONDITION_METRICS, ScopeService

log = logging.getLogger(__name__)
T = TypeVar("T")

ServiceGetter = Callable[[], ScopeService]

RO = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False)
#: Analysis tools: read-only on an existing capture_id, but they ACQUIRE (arm Single, stop the scope)
#: when called with a channel or without capture ids, so they must not be advertised as read-only.
ANALYSE = ToolAnnotations(
    read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=False
)
ACQ = ToolAnnotations(
    read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=False
)
SET = ToolAnnotations(
    read_only_hint=False, destructive_hint=False, idempotent_hint=True, open_world_hint=False
)
DEL = ToolAnnotations(
    read_only_hint=False, destructive_hint=True, idempotent_hint=True, open_world_hint=False
)

Channel = Annotated[int, Field(ge=1, le=2, description="Input channel: 1 (CH1) or 2 (CH2)")]
OptChannel = Annotated[
    int | None,
    Field(ge=1, le=2, description="Channel to acquire a NEW record from when capture_id is omitted"),
]
CaptureId = Annotated[
    str, Field(min_length=1, max_length=128, description="Capture id returned by a capture tool")
]
OptCaptureId = Annotated[
    str | None,
    Field(
        max_length=128, description="Existing capture to analyse; omit to acquire a new record on `channel`"
    ),
]
Mode = Annotated[
    Literal["single", "stop_and_read", "running"],
    Field(
        description="single: arm Single-Sequence and wait for a confirmed new trigger (default, most trustworthy); "
        "stop_and_read: stop and download the frozen record; running: download while running (fastest, freshness "
        "not guaranteed)"
    ),
]
Timeout = Annotated[float, Field(gt=0, le=3600, description="Seconds to wait for the trigger")]
RequireVerified = Annotated[
    bool,
    Field(description="Fail instead of returning values whose voltage scaling is not hardware-verified"),
]
ExportName = Annotated[
    str | None,
    Field(
        max_length=100,
        description="File name only (no directories); saved in the configured export directory. Default: capture id",
    ),
]
Metric = Literal[CONDITION_METRICS]  # type: ignore[valid-type]


class PlanStep(BaseModel):
    action: Literal[
        "enable_channel",
        "set_vertical_scale",
        "set_vertical_offset",
        "set_coupling",
        "set_probe_attenuation",
        "set_timebase",
        "set_trigger_source",
        "set_trigger_mode",
        "set_trigger_slope",
        "set_trigger_level",
        "capture",
        "capture_both_channels",
        "measure",
        "fft",
        "phase",
        "channel_delay",
        "wait",
        "start_acquisition",
        "stop_acquisition",
    ]
    args: dict[str, Any] = Field(
        default_factory=dict,
        description="Keyword arguments of the matching service call, e.g. {'channel': 1, 'volts_per_div': 0.5}; "
        "for measure: {'kind': 'frequency', 'capture_id': '$last'}; wait: {'seconds': 1}",
    )


def register_tools(app: MCPServer, get_service: ServiceGetter) -> list[str]:
    names: list[str] = []

    async def call(tool: str, fn: Callable[..., T], *args: Any, **kwargs: Any) -> T:
        svc = get_service()
        t0 = time.perf_counter()
        try:
            result = await anyio.to_thread.run_sync(functools.partial(fn, *args, **kwargs))
        except HantekError as e:
            svc.record_tool_call(tool, False, (time.perf_counter() - t0) * 1000, e.code)
            raise ToolError(json.dumps({"error": e.to_dict()}, default=str)) from e
        except Exception as e:
            log.exception("tool %s failed unexpectedly", tool)
            svc.record_tool_call(tool, False, (time.perf_counter() - t0) * 1000, "INTERNAL_ERROR")
            raise ToolError(
                json.dumps(
                    {
                        "error": {
                            "code": "INTERNAL_ERROR",
                            "message": repr(e),
                            "retryable": False,
                            "details": {},
                        }
                    }
                )
            ) from e
        svc.record_tool_call(tool, True, (time.perf_counter() - t0) * 1000)
        return result

    def tool(annotations: ToolAnnotations, title: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        def deco(fn: Callable[..., Any]) -> Callable[..., Any]:
            names.append(fn.__name__)
            app.add_tool(
                fn,
                name=fn.__name__,
                title=title,
                annotations=annotations,
                description=inspect.cleandoc(fn.__doc__ or ""),
            )
            return fn

        return deco

    S = get_service  # noqa: N806

    # ---------------------------------------------------------------- instrument & connection
    @tool(ACQ, "Connect to the oscilloscope")
    async def scope_connect() -> dict[str, Any]:
        """Open the configured backend (USB hardware or the explicitly configured simulator), synchronise the
        protocol and read the instrument's identity and settings layout. The backend is fixed by server
        configuration; this tool never silently falls back to simulation."""
        return await call("scope_connect", S().connect)

    @tool(ACQ, "Disconnect from the oscilloscope")
    async def scope_disconnect() -> dict[str, Any]:
        """Release the USB device so other programs (or the GUI) can use it."""
        return await call("scope_disconnect", S().disconnect)

    @tool(RO, "Instrument status")
    async def scope_status() -> dict[str, Any]:
        """Connection state, backend (hardware/simulator), firmware, acquisition/trigger state, busy operation,
        active workflow and scaling-verification status."""
        return await call("scope_status", S().status)

    @tool(RO, "Identify instrument")
    async def scope_identify() -> dict[str, Any]:
        """Model, firmware and FPGA version (from /sys.inf), USB endpoints and settings-layout source."""
        return await call("scope_identify", S().identify)

    @tool(RO, "Instrument capabilities")
    async def scope_get_capabilities() -> dict[str, Any]:
        """Allowed V/div (per probe), timebases, trigger options, capture modes, limits, per-feature verification
        status and input safety ratings. Call this before changing settings."""
        return await call("scope_get_capabilities", S().capabilities)

    @tool(RO, "Read all settings")
    async def scope_get_settings(
        include_raw: Annotated[
            bool, Field(description="Also return every raw field of the settings record")
        ] = False,
    ) -> dict[str, Any]:
        """Read the complete settings record back from the instrument and decode channels, trigger, timebase and
        acquisition. Each decoded table carries its provenance."""
        return await call("scope_get_settings", S().settings, include_raw)

    @tool(RO, "Health check")
    async def scope_get_health() -> dict[str, Any]:
        """Echo round-trip test, protocol error counters, capture-store usage and free disk space."""
        return await call("scope_get_health", S().health)

    # ---------------------------------------------------------------- channels
    @tool(RO, "Read channel settings")
    async def scope_get_channel(channel: Channel) -> dict[str, Any]:
        """Read back one channel's settings: enabled, V/div (at probe tip), coupling, probe, offset, BW limit."""
        return await call("scope_get_channel", S().get_channel, channel)

    @tool(SET, "Enable/disable a channel")
    async def scope_enable_channel(channel: Channel, enabled: bool) -> dict[str, Any]:
        """Turn a channel's display/acquisition on or off. Verified by settings read-back."""
        return await call("scope_enable_channel", S().enable_channel, channel, enabled)

    @tool(SET, "Set vertical scale")
    async def scope_set_vertical_scale(
        channel: Channel,
        volts_per_div: Annotated[
            float,
            Field(
                gt=0,
                le=10000,
                description="V/div as shown on screen, i.e. at the "
                "probe tip including probe attenuation (1-2-5 sequence)",
            ),
        ],
        snap: Annotated[
            bool, Field(description="Snap to the nearest available value instead of rejecting")
        ] = False,
    ) -> dict[str, Any]:
        """Set V/div. Unsupported values are rejected with the allowed list. Implemented with front-panel knob
        emulation and confirmed by read-back (see result.verification)."""
        return await call("scope_set_vertical_scale", S().set_vertical_scale, channel, volts_per_div, snap)

    @tool(SET, "Set vertical offset")
    async def scope_set_vertical_offset(
        channel: Channel,
        divisions: Annotated[
            float,
            Field(
                ge=-10,
                le=10,
                description="Ground-marker position in divisions from screen centre (positive = up)",
            ),
        ],
    ) -> dict[str, Any]:
        """Move the channel's vertical position by knob emulation, re-reading the settings until it converges."""
        return await call("scope_set_vertical_offset", S().set_vertical_offset, channel, divisions)

    @tool(SET, "Set input coupling")
    async def scope_set_coupling(channel: Channel, coupling: Literal["DC", "AC", "GND"]) -> dict[str, Any]:
        """Set DC/AC/GND input coupling (soft-menu emulation, verified by read-back)."""
        return await call("scope_set_coupling", S().set_coupling, channel, coupling)

    @tool(SET, "Set probe attenuation")
    async def scope_set_probe_attenuation(
        channel: Channel, attenuation: Literal[1, 10, 100, 1000]
    ) -> dict[str, Any]:
        """Set the probe attenuation factor. It must match the physical probe; it only rescales readings."""
        return await call("scope_set_probe_attenuation", S().set_probe, channel, attenuation)

    # ---------------------------------------------------------------- timebase & acquisition
    @tool(SET, "Set timebase")
    async def scope_set_timebase(
        seconds_per_div: Annotated[
            float,
            Field(
                gt=0,
                le=40,
                description="s/div from the 2-4-8 sequence (4 ns .. 40 s), e.g. 0.0002 for 200 us/div",
            ),
        ],
        snap: Annotated[
            bool, Field(description="Snap to the nearest available value instead of rejecting")
        ] = False,
    ) -> dict[str, Any]:
        """Set the horizontal scale (knob emulation, verified by read-back)."""
        return await call("scope_set_timebase", S().set_timebase, seconds_per_div, snap)

    @tool(ACQ, "Run acquisition")
    async def scope_start_acquisition() -> dict[str, Any]:
        """Put the instrument in RUN (continuous acquisition)."""
        return await call("scope_start_acquisition", S().start)

    @tool(ACQ, "Stop acquisition")
    async def scope_stop_acquisition() -> dict[str, Any]:
        """STOP acquisition, freezing the current record."""
        return await call("scope_stop_acquisition", S().stop)

    @tool(ACQ, "Arm single acquisition")
    async def scope_single_acquisition() -> dict[str, Any]:
        """Arm Single-Sequence without waiting or downloading (use scope_wait_for_trigger, then scope_capture with
        mode=stop_and_read; or simply scope_capture with mode=single)."""
        return await call("scope_single_acquisition", S().single)

    @tool(RO, "Wait for trigger")
    async def scope_wait_for_trigger(timeout_s: Timeout = 5.0) -> dict[str, Any]:
        """Poll the trigger state until a trigger occurs or the acquisition completes (no download)."""
        return await call("scope_wait_for_trigger", S().wait_for_trigger, timeout_s)

    @tool(ACQ, "Capture a waveform")
    async def scope_capture(
        channel: Channel,
        mode: Mode = "single",
        timeout_s: Timeout = 5.0,
        force_on_timeout: Annotated[
            bool,
            Field(
                description="If no trigger occurs, force one (record is then "
                "not trigger-aligned; freshness=forced_new)"
            ),
        ] = False,
        label: Annotated[
            str | None, Field(max_length=64, description="Optional label stored with the capture")
        ] = None,
    ) -> dict[str, Any]:
        """Acquire and download a complete numerical record (raw 8-bit samples + time + volts) for one channel.
        Returns compact metadata and a capture_id (no bulk arrays): freshness, scaling status, quality (clipping,
        ADC utilisation, stale detection) and warnings. Use scope_get_samples for data."""
        return await call("scope_capture", S().capture, channel, mode, timeout_s, force_on_timeout, label)

    @tool(ACQ, "Capture both channels")
    async def scope_capture_both_channels(
        mode: Mode = "single",
        timeout_s: Timeout = 5.0,
        force_on_timeout: bool = False,
        label: Annotated[str | None, Field(max_length=64)] = None,
    ) -> dict[str, Any]:
        """Capture CH1 and CH2 from the same acquisition (one trigger, sequential download) sharing a group_id.
        `simultaneity` states whether the records are guaranteed to come from the same acquisition."""
        return await call(
            "scope_capture_both_channels", S().capture_both, mode, timeout_s, force_on_timeout, label
        )

    @tool(RO, "Acquisition state")
    async def scope_get_acquisition_state() -> dict[str, Any]:
        """Trigger state (stop/ready/auto/triggered/...), running flag and the instrument's trigger counter."""
        return await call("scope_get_acquisition_state", S().acquisition_state)

    # ---------------------------------------------------------------- trigger
    @tool(RO, "Read trigger settings")
    async def scope_get_trigger() -> dict[str, Any]:
        """Trigger type, source, mode, slope, coupling, level (counts and volts) and frequency counter."""
        return await call("scope_get_trigger", S().get_trigger)

    @tool(SET, "Set trigger source")
    async def scope_set_trigger_source(
        source: Literal["CH1", "CH2", "EXT", "EXT/5", "AC50"],
    ) -> dict[str, Any]:
        """Set the edge-trigger source (requires trigger type Edge on the instrument)."""
        return await call("scope_set_trigger_source", S().set_trigger_source, source)

    @tool(SET, "Set trigger mode")
    async def scope_set_trigger_mode(mode: Literal["auto", "normal"]) -> dict[str, Any]:
        """auto: free-runs without a trigger; normal: acquires only on a trigger."""
        return await call("scope_set_trigger_mode", S().set_trigger_mode, mode)

    @tool(SET, "Set trigger slope")
    async def scope_set_trigger_slope(slope: Literal["rising", "falling"]) -> dict[str, Any]:
        """Set the edge-trigger slope."""
        return await call("scope_set_trigger_slope", S().set_trigger_slope, slope)

    @tool(SET, "Set trigger coupling")
    async def scope_set_trigger_coupling(
        coupling: Literal["DC", "AC", "NOISE_REJECT", "HF_REJECT", "LF_REJECT"],
    ) -> dict[str, Any]:
        """Set the trigger coupling/filter."""
        return await call("scope_set_trigger_coupling", S().set_trigger_coupling, coupling)

    @tool(SET, "Set trigger level")
    async def scope_set_trigger_level(
        volts: Annotated[
            float | None, Field(description="Level in volts on the source channel (CH1/CH2 only)")
        ] = None,
        fifty_percent: Annotated[
            bool, Field(description="Use the instrument's 'set to 50%' instead")
        ] = False,
    ) -> dict[str, Any]:
        """Set the trigger level. Quantised to 1/25 div; the volts conversion uses the nominal scaling model."""
        return await call("scope_set_trigger_level", S().set_trigger_level, volts, fifty_percent)

    @tool(ACQ, "Force trigger")
    async def scope_force_trigger() -> dict[str, Any]:
        """Force a trigger (completes an armed single acquisition without a trigger event)."""
        return await call("scope_force_trigger", S().force_trigger)

    # ---------------------------------------------------------------- measurements
    def _measure_tool(kind: str, title: str, doc: str) -> None:
        async def fn(
            capture_id: OptCaptureId = None,
            channel: OptChannel = None,
            acquire_mode: Mode = "single",
            require_verified_scaling: RequireVerified = False,
        ) -> dict[str, Any]:
            return await call(
                f"scope_measure_{kind}",
                S().measure,
                kind,
                capture_id,
                channel,
                acquire_mode,
                require_verified_scaling,
            )

        fn.__name__ = f"scope_measure_{kind}"
        fn.__doc__ = (
            doc
            + " Computed in software from the full downloaded record. Pass capture_id to analyse an existing "
            "capture or channel to acquire a new one. Check `valid`, `warnings`, `voltage_scaling_status` and "
            "`time_scaling_status` before reporting the value."
        )
        tool(ANALYSE, title)(fn)

    _measure_tool(
        "voltage", "Measure voltages", "Max, min, peak-to-peak, mean, RMS, AC RMS, top, base, amplitude."
    )
    _measure_tool(
        "frequency",
        "Measure frequency",
        "Frequency from 50%-level crossings over all complete cycles, "
        "with an uncertainty estimate and the instrument's own trigger counter when available.",
    )
    _measure_tool("period", "Measure period", "Period from 50%-level crossings over all complete cycles.")
    _measure_tool("duty_cycle", "Measure duty cycle", "Positive duty cycle at the 50% level.")
    _measure_tool("rise_time", "Measure rise time", "10%-90% rise time (median over complete edges).")
    _measure_tool("fall_time", "Measure fall time", "90%-10% fall time (median over complete edges).")
    _measure_tool("rms", "Measure RMS", "True RMS (DC+AC) and AC RMS over the record.")
    _measure_tool("peak_to_peak", "Measure peak-to-peak", "Max - min over the record (includes noise).")
    _measure_tool("noise", "Measure noise", "Robust white-noise RMS estimate, ADC step and SNR estimate.")

    @tool(ANALYSE, "Measure phase between channels")
    async def scope_measure_phase(
        capture_id_a: Annotated[
            str | None,
            Field(description="Reference record (e.g. CH1); omit both ids to capture CH1+CH2 together"),
        ] = None,
        capture_id_b: Annotated[str | None, Field(description="Second record (e.g. CH2)")] = None,
        acquire_mode: Mode = "single",
    ) -> dict[str, Any]:
        """Phase of B relative to A at A's fundamental (positive: B leads). Only meaningful for records from the
        same two-channel acquisition (same group_id); otherwise the result is marked invalid."""
        return await call("scope_measure_phase", S().phase, capture_id_a, capture_id_b, acquire_mode)

    @tool(ANALYSE, "Measure channel delay")
    async def scope_measure_channel_delay(
        capture_id_a: str | None = None,
        capture_id_b: str | None = None,
        slope: Literal["rising", "falling"] = "rising",
        acquire_mode: Mode = "single",
    ) -> dict[str, Any]:
        """Delay from A's edges to B's next same-slope edges at each record's 50% level (median), with the sample
        interval as resolution. Requires records from the same acquisition."""
        return await call(
            "scope_measure_channel_delay", S().channel_delay, capture_id_a, capture_id_b, slope, acquire_mode
        )

    # ---------------------------------------------------------------- signal processing
    @tool(ANALYSE, "FFT spectrum")
    async def scope_fft(
        capture_id: OptCaptureId = None,
        channel: OptChannel = None,
        window: Literal["hann", "hamming", "blackman", "blackmanharris", "flattop", "rectangular"] = "hann",
        detrend: Literal["none", "mean", "linear"] = "mean",
        scaling: Literal["peak", "rms"] = "peak",
        max_points: Annotated[
            int,
            Field(
                ge=16,
                le=5000,
                description="Maximum spectrum points returned (block-maximum decimation keeps peaks)",
            ),
        ] = 512,
        n_peaks: Annotated[int, Field(ge=1, le=50)] = 8,
    ) -> dict[str, Any]:
        """Windowed, detrended single-sided amplitude spectrum with bin width, ENBW, interpolated peaks and
        harmonic/THD analysis of the strongest tone."""
        return await call(
            "scope_fft", S().fft, capture_id, channel, window, detrend, scaling, max_points, n_peaks
        )

    @tool(ANALYSE, "Spectrum peaks")
    async def scope_spectrum_peaks(
        capture_id: OptCaptureId = None,
        channel: OptChannel = None,
        n_peaks: Annotated[int, Field(ge=1, le=50)] = 10,
        window: Literal["hann", "hamming", "blackman", "blackmanharris", "flattop", "rectangular"] = "hann",
        min_relative_db: Annotated[float, Field(ge=-200, le=0)] = -60.0,
    ) -> dict[str, Any]:
        """The strongest spectral components (frequency, amplitude, dB relative to the largest)."""
        return await call(
            "scope_spectrum_peaks", S().spectrum_peaks, capture_id, channel, n_peaks, window, min_relative_db
        )

    @tool(ANALYSE, "Cross-correlation")
    async def scope_cross_correlation(
        capture_id_a: str | None = None,
        capture_id_b: str | None = None,
        max_lag_s: Annotated[
            float | None, Field(gt=0, description="Only search lags within ±max_lag_s")
        ] = None,
    ) -> dict[str, Any]:
        """Lag of B relative to A from the normalised cross-correlation peak (omit ids to capture both channels)."""
        return await call(
            "scope_cross_correlation", S().cross_correlation, capture_id_a, capture_id_b, max_lag_s
        )

    @tool(RO, "Compare two captures")
    async def scope_compare_captures(capture_id_a: CaptureId, capture_id_b: CaptureId) -> dict[str, Any]:
        """Statistics, frequency, RMS difference, correlation and settings differences between two records."""
        return await call("scope_compare_captures", S().compare, capture_id_a, capture_id_b)

    @tool(ANALYSE, "Detect edges")
    async def scope_detect_edges(
        capture_id: OptCaptureId = None,
        channel: OptChannel = None,
        level: Annotated[float | None, Field(description="Reference level (default 50% of base-top)")] = None,
        max_edges: Annotated[int, Field(ge=1, le=2000)] = 200,
    ) -> dict[str, Any]:
        """Rising/falling edge times (hysteresis-filtered, interpolated)."""
        return await call("scope_detect_edges", S().edges, capture_id, channel, level, max_edges)

    @tool(ANALYSE, "Detect pulses")
    async def scope_detect_pulses(
        capture_id: OptCaptureId = None,
        channel: OptChannel = None,
        polarity: Literal["positive", "negative"] = "positive",
        level: float | None = None,
        min_width_s: Annotated[float | None, Field(gt=0)] = None,
        max_width_s: Annotated[float | None, Field(gt=0)] = None,
        max_pulses: Annotated[int, Field(ge=1, le=2000)] = 200,
    ) -> dict[str, Any]:
        """Complete pulses with start time and width, optionally filtered by width, plus width statistics."""
        return await call(
            "scope_detect_pulses",
            S().pulses,
            capture_id,
            channel,
            polarity,
            level,
            min_width_s,
            max_width_s,
            max_pulses,
        )

    # ---------------------------------------------------------------- data & export
    @tool(RO, "List captures")
    async def scope_list_captures(
        limit: Annotated[int, Field(ge=1, le=500)] = 50,
        channel: Annotated[int | None, Field(ge=1, le=2)] = None,
        label: str | None = None,
    ) -> dict[str, Any]:
        """Stored captures, newest first (compact summaries)."""
        return await call("scope_list_captures", S().list_captures, limit, channel, label)

    @tool(RO, "Capture metadata")
    async def scope_get_capture_metadata(capture_id: CaptureId) -> dict[str, Any]:
        """Full metadata of a capture: settings snapshot, scaling model and status, acquisition/freshness, quality,
        instrument identity and the SHA-1 of the raw samples."""
        return await call("scope_get_capture_metadata", S().capture_metadata, capture_id)

    @tool(RO, "Get (decimated) samples")
    async def scope_get_samples(
        capture_id: CaptureId,
        max_points: Annotated[int, Field(ge=2, le=5000, description="Upper bound on returned points")] = 1000,
        method: Annotated[
            Literal["minmax", "stride", "mean"],
            Field(description="minmax keeps per-block extremes (envelope)"),
        ] = "minmax",
    ) -> dict[str, Any]:
        """Bounded, explicitly decimated view of a record (time, raw counts, volts). Not for timing analysis."""
        return await call("scope_get_samples", S().get_samples, capture_id, max_points, method)

    @tool(RO, "Get a sample window")
    async def scope_get_sample_window(
        capture_id: CaptureId,
        start_index: Annotated[int | None, Field(ge=0)] = None,
        count: Annotated[int, Field(ge=1, le=5000)] = 500,
        start_time_s: Annotated[float | None, Field(description="Alternative to start_index")] = None,
    ) -> dict[str, Any]:
        """Undecimated contiguous samples from a record (bounded count)."""
        return await call(
            "scope_get_sample_window", S().get_sample_window, capture_id, start_index, count, start_time_s
        )

    @tool(SET, "Export capture as CSV")
    async def scope_export_csv(
        capture_id: CaptureId, name: ExportName = None, overwrite: bool = False
    ) -> dict[str, Any]:
        """Write the complete record (index, time_s, raw_counts, voltage_v + metadata header) as CSV."""
        return await call("scope_export_csv", S().export, capture_id, "csv", name, overwrite)

    @tool(SET, "Export capture as NPZ")
    async def scope_export_npz(
        capture_id: CaptureId, name: ExportName = None, overwrite: bool = False
    ) -> dict[str, Any]:
        """Write the complete record as a NumPy .npz (raw_samples, time_s, voltage_v, metadata_json)."""
        return await call("scope_export_npz", S().export, capture_id, "npz", name, overwrite)

    @tool(RO, "List export files")
    async def scope_list_exports() -> dict[str, Any]:
        """Files in the export directory."""
        return await call(
            "scope_list_exports",
            lambda: S()._env(
                {"exports": S().store.list_exports(), "export_directory": str(S().store.export_dir)}
            ),
        )

    @tool(ACQ, "Reload an exported capture")
    async def scope_import_export(
        name: Annotated[
            str, Field(max_length=100, description="File name in the export directory (.csv or .npz)")
        ],
    ) -> dict[str, Any]:
        """Reload a previously exported CSV/NPZ file into the capture store (returns a capture id)."""

        def run() -> dict[str, Any]:
            svc = S()
            return svc._env({"capture": svc.capture_brief(svc.store.import_export(name))})

        return await call("scope_import_export", run)

    @tool(DEL, "Delete a capture")
    async def scope_delete_capture(capture_id: CaptureId) -> dict[str, Any]:
        """Delete a stored capture (does not touch exports)."""
        return await call("scope_delete_capture", S().delete_capture, capture_id)

    names.append("scope_capture_screenshot")

    async def scope_capture_screenshot() -> CallToolResult:
        """Grab the instrument's 800x480 screen as PNG (also saved under the data directory). A screenshot is a
        picture of the display, NOT waveform data - never derive measurements from it."""
        png, meta = await call("scope_capture_screenshot", S().screenshot)
        import base64

        return CallToolResult(
            content=[
                ImageContent(type="image", data=base64.b64encode(png).decode(), mime_type="image/png"),
                TextContent(type="text", text=json.dumps(meta)),
            ],
            structured_content=meta,
        )

    app.add_tool(
        scope_capture_screenshot,
        description=inspect.cleandoc(scope_capture_screenshot.__doc__ or ""),
        name="scope_capture_screenshot",
        title="Screenshot (display image)",
        annotations=ACQ,
        structured_output=False,
    )

    # ---------------------------------------------------------------- automation
    @tool(ACQ, "Autoset")
    async def scope_autoset(
        channel: Channel = 1,
        method: Annotated[
            Literal["software", "instrument"],
            Field(
                description="software: iterative, measurement-"
                "driven adjustment of V/div, offset, timebase and trigger with read-back of every change; "
                "instrument: press the scope's own AUTOSET key"
            ),
        ] = "software",
        max_iterations: Annotated[int, Field(ge=1, le=12)] = 6,
        target_cycles: Annotated[float, Field(ge=1, le=50, description="Cycles to fit in the record")] = 4.0,
    ) -> dict[str, Any]:
        """Bring a signal on screen. The software method is clearly labelled software-assisted; every reported
        setting is read back from the instrument."""
        return await call("scope_autoset", S().autoset, channel, method, max_iterations, target_cycles)

    @tool(ACQ, "Find signals")
    async def scope_find_signal(mode: Mode = "stop_and_read") -> dict[str, Any]:
        """Capture each enabled channel and report whether a signal is present, clipped or flat (no settings
        are changed)."""
        return await call("scope_find_signal", S().find_signal, mode)

    @tool(ACQ, "Measure repeatedly")
    async def scope_measure_repeatedly(
        channel: Channel,
        metrics: Annotated[list[Metric], Field(min_length=1, max_length=10)],
        count: Annotated[
            int, Field(ge=1, le=10000, description="Repetitions (bounded by limits.max_repeat_count)")
        ] = 10,
        interval_s: Annotated[float, Field(ge=0, le=60)] = 0.0,
        mode: Mode = "single",
        timeout_s: Timeout = 5.0,
        keep_captures: Annotated[
            bool, Field(description="Store every capture (default: only the last)")
        ] = False,
    ) -> dict[str, Any]:
        """Repeat capture+measure and return per-metric statistics, stale-record count and values. Bounded and
        abortable with scope_abort."""
        return await call(
            "scope_measure_repeatedly",
            S().measure_repeatedly,
            channel,
            list(metrics),
            count,
            interval_s,
            mode,
            timeout_s,
            keep_captures,
        )

    @tool(ACQ, "Acquire until condition")
    async def scope_acquire_until(
        channel: Channel,
        metric: Metric,
        operator: Literal[">", ">=", "<", "<="],
        threshold: float,
        max_attempts: Annotated[int, Field(ge=1, le=10000)] = 20,
        timeout_s: Annotated[float, Field(gt=0, le=86400, description="Overall time limit")] = 60.0,
        mode: Mode = "single",
        capture_timeout_s: Timeout = 5.0,
    ) -> dict[str, Any]:
        """Keep acquiring until `metric operator threshold` holds (e.g. vpp > 3.3) and store the matching record.
        Useful for intermittent events. Bounded by attempts and time; abortable."""
        return await call(
            "scope_acquire_until",
            S().acquire_until,
            channel,
            metric,
            operator,
            threshold,
            max_attempts,
            timeout_s,
            mode,
            capture_timeout_s,
        )

    @tool(ACQ, "Run a measurement plan")
    async def scope_run_measurement_plan(
        steps: Annotated[list[PlanStep], Field(min_length=1, max_length=1000)],
        stop_on_error: bool = True,
    ) -> dict[str, Any]:
        """Execute a bounded list of typed steps (settings, captures, measurements, waits). 'capture_id': '$last'
        refers to the latest capture; phase/channel_delay default to the latest capture_both_channels pair."""
        return await call(
            "scope_run_measurement_plan", S().run_plan, [s.model_dump() for s in steps], stop_on_error
        )

    @tool(ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=True), "Abort")
    async def scope_abort() -> dict[str, Any]:
        """Emergency stop for software activity: aborts the running capture wait / workflow at its next check
        point. Answered immediately even while other tools are busy."""
        svc = S()
        res = svc.abort()
        svc.record_tool_call("scope_abort", True, 0.0)
        return res

    # ---------------------------------------------------------------- diagnostics
    @tool(RO, "USB diagnostics")
    async def scope_get_usb_diagnostics() -> dict[str, Any]:
        """Read-only: libusb backend availability, matching devices and their descriptors/endpoints."""
        return await call("scope_get_usb_diagnostics", S().usb_diagnostics)

    @tool(RO, "Protocol diagnostics")
    async def scope_get_protocol_diagnostics() -> dict[str, Any]:
        """Frame/byte counters, timeouts, checksum/framing errors, retries, latency, settings-layout source."""
        return await call("scope_get_protocol_diagnostics", S().protocol_diagnostics)

    @tool(ACQ, "Probe-compensation self-test")
    async def scope_run_self_test(
        channel: Channel = 1,
        expected_frequency_hz: Annotated[float, Field(gt=0)] = 1000.0,
        expected_vpp: Annotated[float, Field(gt=0, description="Expected amplitude at the probe tip")] = 5.0,
        frequency_tolerance_pct: Annotated[float, Field(gt=0, le=50)] = 2.0,
        vpp_tolerance_pct: Annotated[float, Field(gt=0, le=50)] = 10.0,
        mode: Mode = "single",
    ) -> dict[str, Any]:
        """Acquire the PROBE COMP signal (~5 V, 1 kHz square; connect the probe first and set the matching
        attenuation) and check frequency and amplitude against tolerances. Suggests, never applies, scaling
        corrections."""
        return await call(
            "scope_run_self_test",
            S().self_test,
            channel,
            expected_frequency_hz,
            expected_vpp,
            frequency_tolerance_pct,
            vpp_tolerance_pct,
            mode,
        )

    @tool(RO, "Recent errors")
    async def scope_get_recent_errors(limit: Annotated[int, Field(ge=1, le=100)] = 20) -> dict[str, Any]:
        """The most recent structured errors (code, message, operation, details)."""
        return await call("scope_get_recent_errors", S().recent_errors, limit)

    @tool(
        ToolAnnotations(read_only_hint=False, destructive_hint=True, idempotent_hint=True),
        "Factory default setup",
    )
    async def scope_default_setup(
        confirm: Annotated[bool, Field(description="Must be true: resets EVERY instrument setting")] = False,
    ) -> dict[str, Any]:
        """Press the instrument's Default Setup key. Disabled unless the server config sets
        control.allow_disruptive_operations=true, and requires confirm=true."""
        return await call("scope_default_setup", S().controller.default_setup, confirm)

    @tool(SET, "Configure simulated signal (simulator only)")
    async def scope_set_simulated_signal(
        channel: Channel,
        waveform: Literal["sine", "square", "pwm", "triangle", "sawtooth", "noise", "dc", "off"],
        frequency_hz: Annotated[float, Field(gt=0, le=1e9)] = 1000.0,
        amplitude_v: Annotated[
            float, Field(ge=0, le=500, description="Peak amplitude (RMS for noise)")
        ] = 1.0,
        offset_v: Annotated[float, Field(ge=-500, le=500)] = 0.0,
        duty_cycle: Annotated[float, Field(gt=0, lt=1)] = 0.5,
        phase_deg: Annotated[float, Field(ge=-360, le=360)] = 0.0,
        noise_rms_v: Annotated[float, Field(ge=0, le=100)] = 0.0,
    ) -> dict[str, Any]:
        """SIMULATOR ONLY: change the signal applied to a simulated input. Fails on real hardware."""

        def run() -> dict[str, Any]:
            from hantek_mcp.transport.sim_signals import SignalSpec
            from hantek_mcp.transport.simulator import SimulatedTransport

            svc = S()
            t = svc.controller.transport
            if not isinstance(t, SimulatedTransport):
                raise UnsupportedOperationError(
                    "scope_set_simulated_signal is only available with the simulator"
                )
            spec = SignalSpec(
                waveform, frequency_hz, amplitude_v, offset_v, duty_cycle, phase_deg, noise_rms_v
            )
            try:
                t.set_signal(channel, spec)
            except ValueError as e:
                raise UnsupportedOperationError(str(e)) from e
            return svc._env({"channel": channel, "signal": spec.as_dict()})

        return await call("scope_set_simulated_signal", run)

    return names
