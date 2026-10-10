"""Agent-oriented MCP prompts. Each one encodes a verification-first workflow."""

from __future__ import annotations

from collections.abc import Callable

from mcp.server.mcpserver import MCPServer

RULES = """
Ground rules (always):
- Only report numbers returned by tools, with units, capture_id and the tool's `valid`/`warnings`.
- If `simulated` is true, say clearly that the data is simulated.
- If voltage_scaling_status or time_scaling_status is `nominal_unverified`, report values as nominal/unverified.
- Never claim a measurement succeeded when the trigger timed out, the record is stale
  (`identical_to_previous`, `running_snapshot`), clipped, or flat. Fix the setup and re-acquire instead.
- Report settings using the read-back `confirmed_value`, not the value you requested.
- A screenshot is a picture of the display, never a source of measurements.
"""


def register_prompts(app: MCPServer) -> list[str]:
    names: list[str] = []

    def prompt(title: str, description: str) -> Callable[[Callable[..., str]], Callable[..., str]]:
        def deco(fn: Callable[..., str]) -> Callable[..., str]:
            names.append(fn.__name__)
            return app.prompt(name=fn.__name__, title=title, description=description)(fn)

        return deco

    @prompt("Characterise a signal", "Frequency, amplitude, RMS and duty cycle with sanity checks")
    def characterize_signal(channel: str = "1") -> str:
        return f"""Characterise the signal on CH{channel} of the oscilloscope.

1. scope_status and scope_get_capabilities: note backend, simulated flag and scaling verification.
2. scope_get_channel(channel={channel}) and scope_get_trigger: record current settings.
3. scope_capture(channel={channel}, mode="single"). If the trigger times out, use scope_find_signal or
   scope_autoset(channel={channel}) and set the trigger to the source channel at mid-level; retry.
4. Inspect quality: if clipped -> increase V/div; if adc_utilisation < 0.25 -> decrease V/div; if fewer than
   2 cycles or < 10 samples/cycle -> adjust the timebase. Re-capture after each change.
5. On the final capture_id: scope_measure_frequency, scope_measure_voltage, scope_measure_rms,
   scope_measure_duty_cycle (for pulse-like signals), scope_measure_rise_time.
6. Report a table (quantity, value, unit, method, valid, capture_id) plus the settings used.
{RULES}"""

    @prompt("Clock analysis", "Frequency accuracy, duty cycle, edges and jitter of a clock")
    def clock_analysis(channel: str = "1", expected_frequency_hz: str = "") -> str:
        exp = f" Expected frequency: {expected_frequency_hz} Hz." if expected_frequency_hz else ""
        return f"""Analyse the clock signal on CH{channel}.{exp}

1. Capture with mode="single" so the record is confirmed fresh; aim for >= 10 cycles and >= 20 samples/cycle
   (adjust scope_set_timebase; check `samples_per_cycle` in the frequency result).
2. scope_measure_frequency (note relative_uncertainty_est), scope_measure_duty_cycle, scope_measure_rise_time,
   scope_measure_fall_time, scope_detect_edges.
3. Jitter: use `period_std` from the frequency result and scope_measure_repeatedly(metrics=["frequency"],
   count=20) for capture-to-capture variation. Explain that period_std is limited by the sample interval.
4. If an expected frequency was given, compute the error in ppm and compare it with the uncertainty.
5. Use a faster timebase for rise/fall time if the result warns about sampling resolution.
{RULES}"""

    @prompt("PWM analysis", "Duty cycle, frequency and pulse-width statistics of a PWM signal")
    def pwm_analysis(channel: str = "1") -> str:
        return f"""Analyse the PWM signal on CH{channel}.

1. Capture (mode="single") with several PWM periods in the record; ensure no clipping.
2. scope_measure_frequency, scope_measure_duty_cycle, scope_detect_pulses (positive and negative), and
   scope_measure_voltage for high/low levels (top/base).
3. If the duty cycle varies (e.g. a modulated PWM), use scope_measure_repeatedly(metrics=["duty_cycle",
   "frequency"]) and report min/mean/max.
4. Note duty_cycle_resolution_percent and whether edges are adequately sampled.
{RULES}"""

    @prompt("Noise investigation", "Noise level, spectrum and SNR")
    def noise_investigation(channel: str = "1") -> str:
        return f"""Investigate noise on CH{channel}.

1. Use the most sensitive V/div that does not clip (quality.clipped false, adc_utilisation high); consider
   AC coupling to remove a DC offset (scope_set_coupling).
2. scope_measure_noise (robust RMS estimate, LSB, SNR). If noise_rms < 0.5 LSB the result is
   quantisation-limited: decrease V/div.
3. scope_fft with window="hann" and then window="flattop" for amplitude accuracy; list scope_spectrum_peaks
   (e.g. mains 50/60 Hz harmonics, switching frequencies). State the bin width.
4. Repeat at a second timebase to cover both low- and high-frequency content.
{RULES}"""

    @prompt("Trigger configuration", "Set up a stable edge trigger for a described signal")
    def trigger_setup(channel: str = "1", signal_description: str = "") -> str:
        desc = f" Signal: {signal_description}." if signal_description else ""
        return f"""Configure a stable edge trigger on CH{channel}.{desc}

1. scope_get_trigger: the trigger type must be Edge (other types cannot be configured remotely).
2. scope_capture(channel={channel}, mode="stop_and_read") to see the signal's levels.
3. scope_set_trigger_source("CH{channel}"), scope_set_trigger_slope, then scope_set_trigger_level at the
   midpoint between top and base (or fifty_percent=true). Use mode "normal" to avoid untriggered records,
   "auto" if the signal may disappear.
4. Verify: scope_capture(mode="single") must return freshness new_trigger_confirmed and
   scope_get_acquisition_state should show a non-zero trigger counter.
{RULES}"""

    @prompt("Two-channel comparison", "Compare CH1 and CH2: amplitude, phase and delay")
    def two_channel_comparison() -> str:
        return f"""Compare CH1 and CH2.

1. Make sure both channels are enabled and not clipped; trigger on the reference channel.
2. scope_capture_both_channels(mode="single") - both records share a group_id and come from one acquisition.
3. scope_measure_voltage and scope_measure_frequency on each capture id.
4. scope_measure_phase(capture_id_a=CH1, capture_id_b=CH2) and scope_measure_channel_delay; cross-check with
   scope_cross_correlation. Report the sign convention and resolution.
5. scope_compare_captures for differences in shape/level.
{RULES}"""

    @prompt("Intermittent signal investigation", "Catch a rare event (glitch, dropout, overshoot)")
    def intermittent_signal(channel: str = "1", condition: str = "vpp > 1.0") -> str:
        return f"""Catch an intermittent event on CH{channel}. Condition of interest: {condition}.

1. Configure the trigger to fire on the event where possible (normal mode, level beyond the normal range).
2. Use scope_acquire_until(channel={channel}, metric, operator, threshold, max_attempts, timeout_s) to keep
   acquiring until the condition holds; it stores only the matching record. Bound attempts and time.
3. If the condition is never met, report that explicitly with the attempt count and the value history.
4. When caught: measure the event (scope_detect_pulses, scope_measure_voltage) and export it with
   scope_export_npz for later analysis.
5. Use scope_abort if the investigation must be stopped.
{RULES}"""

    return names
