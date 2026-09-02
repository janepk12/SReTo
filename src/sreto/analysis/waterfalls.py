"""
waterfalls.py — power / phase / coherence waterfalls + 1D phase-delay.

Layout schema (VSCode folds every '#region' block — only the DEFAULTS block
is meant to be touched by a user):

  #region USER-TUNABLE DEFAULTS   knobs with visible effect on the plots
  #region INTERNAL HELPERS        loading, axis styling, robust color limits
  #region POWER WATERFALLS        generate_power_waterfalls()
  #region PHASE WATERFALLS + 1D   generate_phase_waterfalls_and_1d()

What `decimation_factor` means here (both public functions):
  N consecutive FFT frames are non-coherently AVERAGED into one displayed row
  (sdr_core.spectral_waterfall). The plot gets N× fewer rows but uses ALL the
  data, and per-pixel noise drops by ~sqrt(N) — the visual trend is preserved
  (actually cleaned up). The old behavior kept 1 frame in N and threw the rest
  away, which kept full single-look noise while discarding 75%+ of the capture.

Why captures used to look "empty": color limits were autoscaled to the raw
min/max, and the DC/LO-leak spike (plus the t=0 startup transient) sits tens
of dB above everything else — one column of pixels consumed the whole
colormap. Limits are now robust percentiles, printed to the console.

VENDORED COPY — see sreto.analysis package docstring (__init__.py) for what
that means and how this file is kept in sync with 01_CODE/waterfalls.py in
the sdr_r science repo. Only two lines differ from the sdr_r original: the
`repo_paths` / `sdr_core` imports below are package-relative here.
"""

import gc

import numpy as np
import matplotlib as mpl
mpl.use("Agg")            # before pyplot: these figures are only ever saved.
                          # The macosx backend allocates a real Cocoa canvas
                          # per figure and registers the process with the
                          # WindowServer — pure cost for a savefig-only tool.
                          # soop_planner.py already does this; the others did not.
import matplotlib.pyplot as plt          # noqa: E402  (backend must precede it)
from scipy.ndimage import uniform_filter1d

from . import paths as _rp             # sdr_r: `import repo_paths as _rp`
from . import sdr_core                  # sdr_r: `import sdr_core`


def _figure_path(json_filepath, name, save_dir=None):
    """Absolute path for one waterfall figure, creating its directory.

    Derived products live under 03_FIGURES/10_ANALYSIS/<stem>/waterfalls/.
    This used to be json_filepath.replace('.json', '_100pct_power_waterfall
    .png'), which dropped PNGs into 02_DATA beside 18 GB of raw .bin — raw and
    derived interleaved in one flat directory, and no way to tell them apart
    but the extension.

    The capture stem is no longer repeated in the filename because the parent
    directory already carries it. `save_dir` overrides the location for
    callers that place figures themselves (MAIN.py's SAVE_DIR_WATERFALLS).
    """
    import os
    out = str(save_dir) if save_dir else str(
        _rp.analysis_dir(json_filepath, _rp.FIG_WATERFALLS))
    os.makedirs(out, exist_ok=True)
    return os.path.join(out, name)

#region ═══ USER-TUNABLE DEFAULTS ══════════════════════════════════════════

NPERSEG = 2048                     # FFT length per waterfall row
# Robust (vmin, vmax) percentiles for the power panels. vmin sits AT the median
# on purpose: a wideband capture is mostly noise floor, so anchoring the bottom
# of the colormap to p50 pushes the whole floor into one dark tone and spends
# the entire color range on what is actually above it. The old (2.0, 99.8)
# spent ~half the colormap resolving noise against noise, which is exactly why
# real signals looked washed out.
POWER_CLIP_PCT = (50.0, 99.9)
DIFF_CLIP_PCT = 99.5               # symmetric limit percentile, Ch1-Ch2 panel
TARGET_XTICKS = 7                  # approx. tick count for auto frequency axes

# Harmonized palette
BG, PANEL, BORDER, TEXT, MUTED = "#f8f9fa", "#ffffff", "#dee2e6", "#212529", "#6c757d"
C1 = "#1f77b4"
C2 = "#ff7f0e"
C3 = "#9467bd"
C_EXTRACT = "#d62728"              # extraction-window marker (red)
CMAP_WF = "viridis"
CMAP_DIF = "coolwarm"
CMAP_PHASE = "twilight"
CMAP_COH = "magma"

mpl.rcParams['agg.path.chunksize'] = 10000
mpl.rcParams['path.simplify'] = True
mpl.rcParams['path.simplify_threshold'] = 1.0

#endregion

#region ═══ INTERNAL HELPERS (no user knobs) ═══════════════════════════════

def _load_metadata_and_data(json_filepath, percentage):
    """Delegates to sdr_core.load_dual_iq (bitmode-aware), keeping this module's
    historical return order (ch1, ch2, fs, fc, is_dual, meta, bw)."""
    ch1, ch2, fs, fc, bw, meta, is_dual_channel = sdr_core.load_dual_iq(
        json_filepath, percentage=percentage)
    return ch1, ch2, fs, fc, is_dual_channel, meta, bw


def _auto_freq_ticks(f_lo, f_hi, target_ticks=TARGET_XTICKS):
    """'Nice' (1/2/2.5/5/10 x 10^k) tick positions + a format string, chosen
    from the axis SPAN rather than from an unrelated parameter.

    This exists because the tick step used to be handed in as
    `ANALYSIS_BANDWIDTH_MHZ/10` — a number describing the 1D EXTRACTION
    window — while the axis it was applied to spans the FULL captured
    bandwidth. At 50 kHz analysis bandwidth over a 2 MHz capture that is a
    5 kHz step across 2 MHz = 400 labels on one panel, i.e. a solid black bar.

    Decimals are grown until every label is unique, so a 2.5x step (e.g.
    0.25 MHz) never renders as two identical neighbours.
    """
    f_lo, f_hi = float(f_lo), float(f_hi)
    span = f_hi - f_lo
    if not np.isfinite(span) or span <= 0:
        return np.array([f_lo]), "%.3f"

    raw = span / max(int(target_ticks), 1)
    mag = 10.0 ** np.floor(np.log10(raw))
    step = 10.0 * mag
    for mult in (1.0, 2.0, 2.5, 5.0):
        if raw <= mult * mag:
            step = mult * mag
            break

    ticks = np.arange(np.ceil(f_lo / step) * step, f_hi + step * 0.5, step)
    tol = step * 1e-6
    ticks = ticks[(ticks >= f_lo - tol) & (ticks <= f_hi + tol)]
    if len(ticks) == 0:
        return np.array([f_lo, f_hi]), "%.3f"

    decimals = 7
    for d in range(0, 8):
        if len({"%.*f" % (d, t) for t in ticks}) == len(ticks):
            decimals = d
            break
    return ticks, "%%.%df" % decimals


def _format_waterfall_axes(ax, f_mhz, title, y_label=None, xtick_stepsize=None):
    """Style a full-bandwidth waterfall panel.

    xtick_stepsize=None (the default) auto-picks the step from the visible
    span; pass a number only to force a specific grid.
    """
    ax.set_facecolor(PANEL)
    ax.set_title(title, pad=10, fontweight='bold', color=TEXT)
    ax.set_xlabel("Frequency (MHz)", fontweight='bold', color=TEXT)

    f_lo, f_hi = float(f_mhz.min()), float(f_mhz.max())
    if xtick_stepsize:
        ticks = np.arange(f_lo, f_hi, float(xtick_stepsize))
        fmt = "%.3f"
    else:
        ticks, fmt = _auto_freq_ticks(f_lo, f_hi)
    ax.set_xticks(ticks)
    ax.set_xticklabels([fmt % t for t in ticks])
    # imshow already set xlim from `extent`; re-assert it so ticks rounded
    # outward cannot stretch the axis past the data.
    ax.set_xlim(f_lo, f_hi)

    ax.tick_params(axis='x', rotation=30, colors=MUTED)
    for lbl in ax.get_xticklabels():
        lbl.set_horizontalalignment('right')
    ax.tick_params(axis='y', colors=MUTED)
    if y_label:
        ax.set_ylabel(y_label, fontweight='bold', color=TEXT)
    ax.grid(color=BORDER, linestyle='--', alpha=0.5)
    for sp in ax.spines.values():
        sp.set_edgecolor(BORDER)


def _mark_extraction_window(ax, fc_hz, offset_mhz, bw_khz, label=True):
    """Shade the slice of the FULL-bandwidth axis that feeds the 1D analysis.

    The 2D panels always show the whole capture; the 1D phase/delay chain sees
    only [fc+offset -/+ bw/2]. Without this marker the two figures silently
    describe different frequency ranges.
    """
    if bw_khz is None or offset_mhz is None or bw_khz <= 0:
        return
    centre = (fc_hz / 1e6) + float(offset_mhz)
    half = (float(bw_khz) / 1e3) / 2.0
    lo, hi = centre - half, centre + half

    ax.axvspan(lo, hi, color=C_EXTRACT, alpha=0.13, zorder=3)
    for x in (lo, hi):
        ax.axvline(x, color=C_EXTRACT, linestyle='--', linewidth=1.3,
                   alpha=0.9, zorder=4)
    if label:
        ax.annotate(f"1D window\n{bw_khz:g} kHz", xy=(centre, 0.0),
                    xycoords=('data', 'axes fraction'), xytext=(0, 4),
                    textcoords='offset points', ha='center', va='bottom',
                    fontsize=7.5, color=C_EXTRACT, fontweight='bold', zorder=5)


def _render_power_strip(ax, ch, fs_hz, bw_hz, nperseg=256, max_cols=1500):
    """Horizontal power waterfall (x = time, y = frequency) of the EXTRACTED
    window only — the visual counterpart to the 1D phase traces above it.

    Fed the already-mixed+decimated channel, so it shows exactly the samples
    the 1D math consumed, not a re-derivation of them.
    """
    n_frames = len(ch) // nperseg
    if n_frames < 2:
        ax.text(0.5, 0.5, "capture too short for a power strip",
                transform=ax.transAxes, ha='center', va='center',
                color=MUTED, fontsize=9)
        return None

    avg = max(1, n_frames // max_cols)
    wf = sdr_core.spectral_waterfall(ch, fs_hz, nperseg=nperseg, avg_frames=avg)

    # bandlimit_and_decimate leaves fs >= 2.5*bw, so the array is wider than
    # the requested window — crop to the window the user actually asked for.
    keep = np.abs(wf["f_hz"]) <= (bw_hz / 2.0)
    if keep.sum() < 2:
        keep = np.ones(len(wf["f_hz"]), dtype=bool)

    f_khz = wf["f_hz"][keep] / 1e3
    P_dB = 10 * np.log10(wf["P1"][:, keep] + 1e-20)
    vmin, vmax = _robust_power_limits(P_dB)
    extent = [float(wf["t_sec"].min()), float(wf["t_sec"].max()),
              float(f_khz.min()), float(f_khz.max())]
    return ax.imshow(P_dB.T, aspect='auto', origin='lower', extent=extent,
                     cmap=CMAP_WF, vmin=vmin, vmax=vmax,
                     interpolation='antialiased')


def _robust_power_limits(*dB_arrays, clip_pct=POWER_CLIP_PCT):
    """Shared robust color range across panels: percentile-clipped so a DC/LO
    spike or a startup transient (single hot column/row) cannot stretch the
    scale and flatten the rest of the image."""
    lo = min(float(np.percentile(a, clip_pct[0])) for a in dB_arrays)
    hi = max(float(np.percentile(a, clip_pct[1])) for a in dB_arrays)
    if hi <= lo:
        hi = lo + 1.0
    return lo, hi


def _imshow_wf(ax, data, extent, interp='antialiased', **kw):
    """Waterfall imshow. data rows = time (top = t_min), cols = frequency.

    Default 'antialiased' matters for POWER/COHERENCE: with thousands of rows
    squeezed into ~1200 px, 'nearest' silently DROPS rows (display aliasing)
    and a short-lived signal can vanish from the PNG even though it is in the
    data. PHASE panels must pass interp='nearest' instead — antialiasing would
    linearly average WRAPPED angles across rows (the exact operation the
    pipeline's circular-statistics rule forbids), collapsing them toward 0."""
    return ax.imshow(data, aspect='auto', extent=extent,
                     interpolation=interp, **kw)

#endregion

#region ═══ POWER WATERFALLS ═══════════════════════════════════════════════

def generate_power_waterfalls(json_filepath, percentage=1.0, decimation_factor=4,
                              xtick_stepsize=None, add_differential_plot=True,
                              figsize_3_panel=(24, 8), nperseg=NPERSEG,
                              extract_offset_mhz=None, extract_bw_khz=None,
                              save_dir=None):
    """Per-channel PSD waterfalls (+ Ch1-Ch2 difference).

    ALWAYS spans the entire captured bandwidth (+/- fs/2 around fc) — the
    analysis bandwidth never narrows these panels, it only marks them.

    decimation_factor: FFT frames averaged per displayed row (see module doc).
    xtick_stepsize:    None = auto-fit the tick step to the span (recommended).
    extract_offset_mhz / extract_bw_khz: when given, shade the slice that the
        1D phase chain extracts, so the 2D and 1D figures stay comparable.
    """
    print(f"\n[POWER] Loading data from {json_filepath}...")
    ch1, ch2, fs, fc, is_dual_channel, _, _ = _load_metadata_and_data(json_filepath, percentage)

    wf = sdr_core.spectral_waterfall(ch1, fs, nperseg=nperseg,
                                     avg_frames=decimation_factor,
                                     ch2=ch2 if is_dual_channel else None)
    f_mhz = (wf["f_hz"] + fc) / 1e6
    t = wf["t_sec"]
    Sxx_dB_1 = 10 * np.log10(wf["P1"] + 1e-20)
    Sxx_dB_2 = 10 * np.log10(wf["P2"] + 1e-20) if is_dual_channel else None

    vmin, vmax = _robust_power_limits(
        *([Sxx_dB_1, Sxx_dB_2] if is_dual_channel else [Sxx_dB_1]))
    print(f"[POWER] {len(t)} rows x {nperseg} bins "
          f"({decimation_factor}-frame looks) | full band "
          f"{f_mhz.min():.4f}..{f_mhz.max():.4f} MHz | color range "
          f"{vmin:.1f} .. {vmax:.1f} dB (p{POWER_CLIP_PCT[0]:g}/p{POWER_CLIP_PCT[1]:g})")
    if extract_bw_khz:
        _c = (fc / 1e6) + float(extract_offset_mhz or 0.0)
        print(f"[POWER] 1D extraction window marked: {_c:.4f} MHz "
              f"+/- {float(extract_bw_khz)/2e3:.4f} MHz ({extract_bw_khz:g} kHz wide)")

    show_diff = is_dual_channel and add_differential_plot
    num_panels = 3 if show_diff else (2 if is_dual_channel else 1)

    fig, axes = plt.subplots(1, num_panels,
                             figsize=((figsize_3_panel[0] / 3) * num_panels, figsize_3_panel[1]),
                             sharey=True, constrained_layout=True)
    fig.patch.set_facecolor(BG)
    if num_panels == 1:
        axes = [axes]

    # rows = time, top = t_min: extent bottom/top are (t_max, t_min)
    extent = [f_mhz.min(), f_mhz.max(), t.max(), t.min()]

    mesh1 = _imshow_wf(axes[0], Sxx_dB_1, extent, cmap=CMAP_WF, vmin=vmin, vmax=vmax)
    _format_waterfall_axes(axes[0], f_mhz, f"Ch 1 Power (Center: {fc/1e6} MHz)",
                           "Time (Seconds)", xtick_stepsize=xtick_stepsize)
    _mark_extraction_window(axes[0], fc, extract_offset_mhz, extract_bw_khz)
    fig.colorbar(mesh1, ax=axes[0], pad=0.02).set_label('Power (dB)', rotation=270, labelpad=15)

    if is_dual_channel:
        mesh2 = _imshow_wf(axes[1], Sxx_dB_2, extent, cmap=CMAP_WF, vmin=vmin, vmax=vmax)
        _format_waterfall_axes(axes[1], f_mhz, f"Ch 2 Power (Center: {fc/1e6} MHz)",
                               xtick_stepsize=xtick_stepsize)
        _mark_extraction_window(axes[1], fc, extract_offset_mhz, extract_bw_khz)
        fig.colorbar(mesh2, ax=axes[1], pad=0.02).set_label('Power (dB)', rotation=270, labelpad=15)

        if show_diff:
            diff = Sxx_dB_1 - Sxx_dB_2
            diff_max = float(np.percentile(np.abs(diff), DIFF_CLIP_PCT))
            mesh3 = _imshow_wf(axes[2], diff, extent, cmap=CMAP_DIF,
                               vmin=-diff_max, vmax=diff_max)
            _format_waterfall_axes(axes[2], f_mhz, "Power Difference (Ch1 - Ch2)",
                                   xtick_stepsize=xtick_stepsize)
            _mark_extraction_window(axes[2], fc, extract_offset_mhz, extract_bw_khz)
            fig.colorbar(mesh3, ax=axes[2], pad=0.02).set_label('Δ Power (dB)', rotation=270, labelpad=15)

    out_path = _figure_path(json_filepath,
                            f"{int(percentage*100):03d}pct_power_waterfall.png", save_dir)
    fig.savefig(out_path, dpi=150, bbox_inches='tight', facecolor=fig.get_facecolor())
    plt.close(fig)
    del ch1, ch2, wf
    gc.collect()

#endregion

#region ═══ PHASE WATERFALLS + 1D PHASE/DELAY ══════════════════════════════

def generate_phase_waterfalls_and_1d(json_filepath, percentage=1.0, decimation_factor=4,
                                     waterfall_fft_size=NPERSEG, xtick_stepsize=None,
                                     plot_dec=1000, smoothing_window=10000,
                                     figsize_3_panel=(24, 8), target_bw_khz=500,
                                     center_freq_offset_mhz=0.0, coherence_looks=8,
                                     save_dir=None):
    """Phase panels (single-look per channel, multi-look interferometric),
    coherence panel, and the 1D smoothed phase-difference / delay figure.

    Two different frequency scopes on purpose:
      * the 2D panels ALWAYS cover the entire captured bandwidth, and merely
        SHADE the extraction window;
      * the 1D traces (+ the power strip beneath them) see ONLY
        [fc + center_freq_offset_mhz -/+ target_bw_khz/2].

    decimation_factor: FFT frames averaged per displayed row (see module doc).
    Coherence and interferometric phase get decimation_factor * coherence_looks
    total looks; the pure-noise coherence floor is ~1/sqrt(total looks).
    """
    ch1, ch2, fs, fc, is_dual_channel, meta, hardware_bw_hz = _load_metadata_and_data(json_filepath, percentage)

    if not is_dual_channel:
        raise ValueError("[PHASE] Requires dual-channel data.")

    # ── waterfalls: one chunked STFT pass over ALL frames ─────────────
    wf = sdr_core.spectral_waterfall(ch1, fs, nperseg=waterfall_fft_size,
                                     avg_frames=decimation_factor, ch2=ch2)
    f_mhz = (wf["f_hz"] + fc) / 1e6
    t_wf = wf["t_sec"]

    # Multi-look on top of the per-row averaging: boxcar over `looks` rows of
    # the complex cross-spectrum and the powers (complex product is averaged
    # BEFORE any magnitude/angle — circular statistics).
    looks = max(1, min(int(coherence_looks), wf["C"].shape[0]))
    C_ml = (uniform_filter1d(wf["C"].real, looks, axis=0, mode='nearest')
            + 1j * uniform_filter1d(wf["C"].imag, looks, axis=0, mode='nearest'))
    p1_ml = uniform_filter1d(wf["P1"], looks, axis=0, mode='nearest')
    p2_ml = uniform_filter1d(wf["P2"], looks, axis=0, mode='nearest')
    coherence_wf = np.abs(C_ml) / (np.sqrt(p1_ml * p2_ml) + 1e-30)

    total_looks = decimation_factor * looks
    print(f"[PHASE] {len(t_wf)} rows x {waterfall_fft_size} bins | "
          f"{total_looks} total looks -> pure-noise coherence floor "
          f"~{1.0/np.sqrt(total_looks):.2f}")

    panel_w = figsize_3_panel[0] / 3.0
    fig, axes = plt.subplots(1, 4, figsize=(panel_w * 4, figsize_3_panel[1]),
                             sharey=True, constrained_layout=True)
    fig.patch.set_facecolor(BG)
    cbar_ticks = [-np.pi, -np.pi/2, 0, np.pi/2, np.pi]
    cbar_labels = ['-π', '-π/2', '0', 'π/2', 'π']

    # rows = time, top = t_min (same orientation contract as _imshow_wf)
    extent = [f_mhz.min(), f_mhz.max(), t_wf.max(), t_wf.min()]

    for ax_i, (data, title) in enumerate([
            (wf["phi1"], "Ch 1 Phase (single-look)"),
            (wf["phi2"], "Ch 2 Phase (single-look)"),
            (np.angle(C_ml), f"Interferometric Phase Ch1-Ch2 ({total_looks}-look)")]):
        mesh = _imshow_wf(axes[ax_i], data, extent, interp='nearest',
                          cmap=CMAP_PHASE, vmin=-np.pi, vmax=np.pi)
        _format_waterfall_axes(axes[ax_i], f_mhz, title,
                               "Time (Seconds)" if ax_i == 0 else None,
                               xtick_stepsize=xtick_stepsize)
        _mark_extraction_window(axes[ax_i], fc, center_freq_offset_mhz, target_bw_khz)
        cbar = fig.colorbar(mesh, ax=axes[ax_i], pad=0.02, ticks=cbar_ticks)
        cbar.ax.set_yticklabels(cbar_labels)
        cbar.set_label('Phase (Rads)', rotation=270, labelpad=15)

    mesh4 = _imshow_wf(axes[3], coherence_wf, extent, cmap=CMAP_COH, vmin=0.0, vmax=1.0)
    _format_waterfall_axes(axes[3], f_mhz, f"Coherence |γ| ({total_looks}-look)",
                           xtick_stepsize=xtick_stepsize)
    _mark_extraction_window(axes[3], fc, center_freq_offset_mhz, target_bw_khz)
    cbar4 = fig.colorbar(mesh4, ax=axes[3], pad=0.02)
    cbar4.set_label('Coherence |γ|', rotation=270, labelpad=15)

    out_wf = _figure_path(json_filepath,
                          f"{int(percentage*100):03d}pct_phase_waterfall.png", save_dir)
    fig.savefig(out_wf, dpi=150, bbox_inches='tight', facecolor=fig.get_facecolor())
    plt.close(fig)
    del wf, C_ml, p1_ml, p2_ml, coherence_wf
    gc.collect()

    # ── 1D target-bandwidth phase / delay time series ──────────────────
    offset_hz = center_freq_offset_mhz * 1e6
    actual_fc = fc + offset_hz

    # Shift the target carrier down to DC so the low-pass isolates it, then
    # band-limit + decimate with anti-alias margin (cutoff <= 0.8x new Nyquist).
    ch1_mixed = sdr_core.digital_mix(ch1, fs, offset_hz)
    ch2_mixed = sdr_core.digital_mix(ch2, fs, offset_hz)
    ch1_dec, fs_1d = sdr_core.bandlimit_and_decimate(ch1_mixed, fs, target_bw_khz * 1000.0)
    ch2_dec, _ = sdr_core.bandlimit_and_decimate(ch2_mixed, fs, target_bw_khz * 1000.0)

    min_1d_len = min(len(ch1_dec), len(ch2_dec))
    ch1_dec, ch2_dec = ch1_dec[:min_1d_len], ch2_dec[:min_1d_len]

    # Interferometric phase: smooth the COMPLEX product, then take the angle
    # (circular mean). Averaging wrapped angles directly corrupts values near ±π.
    # The common mixer cancels in ch1·conj(ch2); Δφ is referenced to the RF carrier.
    z = ch1_dec * np.conj(ch2_dec)
    z_smooth = sdr_core.smooth_complex(z, smoothing_window)
    phase_diff_smooth = np.angle(z_smooth)

    # Unwrapped ΔΦ(t): the continuous observable used for physics extraction.
    phase_unwrapped = np.unwrap(phase_diff_smooth)

    # Wrapped carrier-phase delay τ = Δφ/(2π·f_RF), ambiguous modulo 1/f_RF.
    time_delay_smooth = (phase_diff_smooth / (2 * np.pi * actual_fc)) * 1e9

    t_1d = np.arange(min_1d_len) / fs_1d
    t_plot = t_1d[::plot_dec]
    ph_plot = phase_diff_smooth[::plot_dec]
    td_plot = time_delay_smooth[::plot_dec]
    ph_unwrap_plot = phase_unwrapped[::plot_dec]

    fig_1d, (ax1, ax_u, ax_s) = plt.subplots(
        3, 1, figsize=(12, 12), sharex=True,
        gridspec_kw={'height_ratios': [3, 3, 2]})
    fig_1d.patch.set_facecolor(BG)
    ax1.set_facecolor(PANEL)
    ax_u.set_facecolor(PANEL)
    ax_s.set_facecolor(PANEL)

    ax1.set_ylabel('Phase Difference (Radians)', color=C1, fontweight='bold')
    ax1.plot(t_plot, ph_plot, color=C1, lw=1.5, label='Smoothed Phase (wrapped)')
    ax1.tick_params(axis='y', labelcolor=C1)
    ax1.set_yticks([-np.pi, -np.pi/2, 0, np.pi/2, np.pi])
    ax1.set_yticklabels(['-π', '-π/2', '0', 'π/2', 'π'])
    ax1.grid(True, linestyle='--', alpha=0.6, color=BORDER)

    ax2 = ax1.twinx()
    ax2.set_ylabel(f'Time Delay at {actual_fc/1e6:.3f} MHz (Nanoseconds)', color=C2, fontweight='bold')
    ax2.plot(t_plot, td_plot, color=C2, lw=1.5, linestyle='--', alpha=0.8, label='Smoothed Delay (ns)')
    ax2.tick_params(axis='y', labelcolor=C2)

    max_delay_ns = (np.pi / (2 * np.pi * actual_fc)) * 1e9
    ax2.set_ylim(-max_delay_ns, max_delay_ns)

    ax3 = ax1.twinx()
    ax3.spines['right'].set_position(('outward', 60))
    ax3.set_frame_on(True)
    ax3.patch.set_visible(False)
    ax3.set_ylabel('Distance Delay (Meters)', color=C3, fontweight='bold')
    ax3.tick_params(axis='y', labelcolor=C3)
    ax3.spines['right'].set_color(C3)
    ax3.spines['right'].set_linewidth(1.5)

    max_dist_m = (max_delay_ns * 1e-9) * sdr_core.C_LIGHT
    ax3.set_ylim(-max_dist_m, max_dist_m)

    lines_1, labels_1 = ax1.get_legend_handles_labels()
    lines_2, labels_2 = ax2.get_legend_handles_labels()
    ax1.legend(lines_1 + lines_2, labels_1 + labels_2, loc='upper right',
               facecolor=PANEL, edgecolor=BORDER, fontsize=9)

    ax1.set_title(f"Inter-Channel Phase Difference & Time Delay\n"
                  f"Effective Carrier: {actual_fc/1e6:.3f} MHz | "
                  f"Target Analysis Bandwidth: {target_bw_khz} kHz", color=TEXT)

    # Bottom panel: unwrapped ΔΦ(t) with its one-way path-length equivalent
    # ΔL = λ·ΔΦ/(2π). Height retrieval additionally needs the geometry factor
    # 1/(2·sin(elevation)) — that lives in physics.py.
    lam_m = sdr_core.C_LIGHT / actual_fc
    ax_u.plot(t_plot, ph_unwrap_plot, color=C1, lw=1.5, label='Unwrapped ΔΦ')
    ax_u.set_ylabel('Unwrapped ΔΦ (Radians)', color=C1, fontweight='bold')
    ax_u.tick_params(axis='y', labelcolor=C1)
    ax_u.grid(True, linestyle='--', alpha=0.6, color=BORDER)

    ax_u2 = ax_u.twinx()
    ax_u2.set_ylabel('Equivalent Path Change (cm)', color=C2, fontweight='bold')
    ax_u2.tick_params(axis='y', labelcolor=C2)
    lo, hi = ax_u.get_ylim()
    ax_u2.set_ylim(lo * lam_m / (2 * np.pi) * 100.0, hi * lam_m / (2 * np.pi) * 100.0)
    ax_u.legend(loc='upper right', facecolor=PANEL, edgecolor=BORDER, fontsize=9)
    ax_u.set_title('Unwrapped Interferometric Phase (physics observable)', color=TEXT)

    # Bottom panel: the EXTRACTED band as a power waterfall on the same time
    # axis — a direct visual check of what the traces above were computed from
    # (e.g. a trace going wild while the strip shows no signal = noise, not physics).
    strip_mesh = _render_power_strip(ax_s, ch1_dec, fs_1d, target_bw_khz * 1000.0)
    ax_s.set_xlabel('Time (Seconds)', fontweight='bold', color=TEXT)
    ax_s.set_ylabel(f'Offset from\n{actual_fc/1e6:.4f} MHz (kHz)',
                    color=TEXT, fontweight='bold')
    ax_s.tick_params(colors=MUTED)
    ax_s.set_title(f'Extracted Band — Ch1 (direct/RE) Power | '
                   f'{target_bw_khz:g} kHz window driving the traces above',
                   color=TEXT, fontsize=10)

    fig_1d.subplots_adjust(right=0.82, hspace=0.28)

    # Colorbar in the free right margin rather than via fig.colorbar(ax=ax_s):
    # the latter would shrink ONLY the strip, breaking its x-alignment with the
    # shared-axis traces above (which is the whole point of the panel).
    if strip_mesh is not None:
        _p = ax_s.get_position()
        cax = fig_1d.add_axes([0.84, _p.y0, 0.014, _p.height])
        cb_s = fig_1d.colorbar(strip_mesh, cax=cax)
        cb_s.set_label('Power (dB)', rotation=270, labelpad=14, color=TEXT)
        cb_s.ax.tick_params(colors=MUTED, labelsize=8)

    out_1d = _figure_path(json_filepath,
                          f"{int(percentage*100):03d}pct_phase_delay_1D.png", save_dir)
    # NOTE: no bbox_inches='tight' here — it recomputes the layout and would
    # undo the manually placed colorbar's alignment with the strip.
    fig_1d.savefig(out_1d, dpi=150, facecolor=fig_1d.get_facecolor())
    plt.close(fig_1d)
    del ch1, ch2, ch1_mixed, ch2_mixed, z, z_smooth
    gc.collect()

#endregion
