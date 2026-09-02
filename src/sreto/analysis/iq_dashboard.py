"""
iq_dashboard.py — dual-channel IQ dashboard + raw interferometric observables.

Given one dual-channel capture (ch1 = "RE" direct/reference, ch2 = "GR"
ground-reflected) this draws the IQ / waterfall / cross-spectrum dashboard AND
computes the raw interferometric observables that the physics retrieval is built
on. It deliberately stops at the *raw* observables — the full soil-moisture /
altimetry inversion (which needs the transmitter elevation angle) lives in
physics.py; this module is elevation-agnostic and stays honest about that.

THE OBSERVABLES (all standard InSAR / GNSS-R quantities)
--------------------------------------------------------
Over the analysis band (see WINDOW below) the two channels give, per short
coherent block k,

    γ_k = Σ z1·z2* / sqrt(Σ|z1|²·Σ|z2|²)          (complex coherence)

  |γ|   ∈ [0,1]  interferometric quality      (Zebker & Villasenor 1992)
  arg γ = φ1 − φ2   wrapped phase difference   (the altimetry observable)
  σ_φ   = sqrt((1−|γ|²)/(2·N·|γ|²))   phase CRLB (Rodríguez & Martin 1992)
  Γ     = P_r / P_d   reflected/direct power ratio  = |r|²·|g_r/g_d|²
          → surface reflectivity (bistatic radar eq.; inverted in physics.py)

WHY BLOCK COHERENCE, NOT ONE WINDOW.  A single |γ| over the whole (up to 30 s)
window is the ML estimate ONLY if arg γ is constant across it. This receiver has
a residual inter-chain frequency offset (arg γ drifts ~tens of rad over a run),
so a single-window |γ| sums rotating phasors incoherently and is biased LOW.
Short blocks (block_sec) keep arg γ ~constant within each, so the MEDIAN block
|γ| is the drift-robust quality; the drift itself is reported as a residual Hz.

THE WINDOW (bandwidth + offset).  The band the observables are computed on is
set by exactly two knobs, applied in this order:
    1. digital mix by −offset  → the tone at (HW center + offset) lands at DC
    2. low-pass at bandwidth/2  → keep ±bw/2 around it
so the observables — and the reported centre-frequency POWER — describe the band
[fc+offset − bw/2 , fc+offset + bw/2]. This is the identical convention used by
physics.py (sdr_core.digital_mix / bandlimit), so the two always agree.

Run standalone:
    python iq_dashboard.py <capture.json> --offset -0.45 --bw 0.05
    python iq_dashboard.py <capture.json> --offset -0.45 --bw 0.05 --power   # power only
    python iq_dashboard.py --selftest                                        # known-truth check

VENDORED COPY — see sreto.analysis package docstring (__init__.py) for what
that means and how this file is kept in sync with 01_CODE/iq_dashboard.py in
the sdr_r science repo. Only the `sdr_core` / `console` imports below differ
(package-relative here vs bare in sdr_r).
"""

import argparse
import json
import os
import sys

import numpy as np
import matplotlib
matplotlib.use("Agg")     # before pyplot: this tool only ever savefig()s, and
                          # the macosx backend builds a Cocoa canvas per figure
                          # and registers the process with the WindowServer.
import matplotlib.pyplot as plt
import matplotlib.gridspec as gridspec
from scipy.signal import spectrogram, resample_poly

from . import sdr_core                  # sdr_r: `import sdr_core`
from . import console as con            # sdr_r: `import console as con`

matplotlib.rcParams.update({"font.family": "monospace", "axes.unicode_minus": False})

# ═══════════════════════════════════════════════════════════════
#  HARMONIZED PALETTE
# ═══════════════════════════════════════════════════════════════
BG, PANEL, BORDER, TEXT, MUTED = "#f8f9fa", "#ffffff", "#dee2e6", "#212529", "#6c757d"
C1, C2, C_DIFF, C_PH, C_XC, C_PEAK = "#1f77b4", "#ff7f0e", "#d62728", "#17becf", "#9467bd", "#e377c2"
CMAP_WF, CMAP_DIF, CMAP_TIME = "viridis", "coolwarm", "plasma"
# Cyclic map for the interferometric-phase panel: ΔΦ = −π and +π are the SAME
# angle, so the colour must wrap too (a non-cyclic map fakes a seam at ±π).
CMAP_PHASE = "twilight_shifted"

# ═══════════════════════════════════════════════════════════════
#  HARD SPEED LIMITS (FOR VISUAL DASHBOARD)
# ═══════════════════════════════════════════════════════════════
MAX_IQ_SCATTER_POINTS = 5000  # Never plot more than 5k dots on the IQ scatter plot
PLOT_DECIMATION = 50          # Slices the 1D time arrays (Envelope, Phase) before plotting
MAX_WATERFALL_COLUMNS = 2000  # Time-axis column cap for the spectrogram imshow panels.
# scipy.signal.spectrogram at 0 overlap still makes far more columns than a PNG
# has pixels; matplotlib's imshow runs its full colormap/RGBA pipeline over the
# source array before downsampling for display. Column-AVERAGING (linear power /
# complex cross-spectrum) down to a display count keeps all the data — a stride
# would throw rows away and keep single-look noise.

DEFAULT_BLOCK_SEC = 0.1  # coherent block for the interferometric observables


# ═══════════════════════════════════════════════════════════════
#  IO / METADATA
# ═══════════════════════════════════════════════════════════════
def resolve_input_path(input_file):
    if not isinstance(input_file, str) or not input_file.strip():
        raise ValueError("Invalid input path.")
    if os.path.splitext(input_file)[1].lower() == ".json":
        bin_path = os.path.splitext(input_file)[0] + ".bin"
        if os.path.exists(bin_path):
            return bin_path
    if os.path.exists(input_file):
        return input_file
    raise FileNotFoundError(f"Cannot find input file: {input_file}")


def load_metadata(bin_path):
    json_path = os.path.splitext(bin_path)[0] + ".json"
    if os.path.exists(json_path):
        with open(json_path) as f:
            return json.load(f)
    return {}


def extract_params(meta):
    return {
        "freq_mhz": sdr_core.get_meta(meta, ["frequency_MHz", "freq_mhz", "center_freq_mhz"], 433.0),
        "samplerate_mhz": sdr_core.get_meta(meta, ["samplerate_mhz", "fs_mhz"], 2.0),
        "bandwidth_mhz": sdr_core.get_meta(meta, ["bandwidth_MHz", "bw_mhz"], 2.0),
    }


def load_iq(bin_path, max_samples):
    """Bitmode-aware dual-channel load via sdr_core (old version hard-coded int16)."""
    ch1, ch2, _, _, _, _, is_dual = sdr_core.load_dual_iq(bin_path, max_samples=max_samples)
    if not is_dual:
        raise ValueError("The IQ dashboard requires a dual-channel (ch1_2) capture.")
    return ch1, ch2, len(ch1)


# ═══════════════════════════════════════════════════════════════
#  DSP: the analysis window (mix → decimate → band-limit)
#
#  Shared by the file-driven run AND the self-test, so what the test proves is
#  literally what the dashboard runs. Same primitives as physics.py.
# ═══════════════════════════════════════════════════════════════
def prepare_channels(c1_raw, c2_raw, raw_fs_hz, offset_hz, decimation, bw_hz):
    """
    Return (c1, c2, c1_nb, c2_nb, fs_hz):
      c1/c2      decimated wide-band channels (for the IQ scatter / waterfalls)
      c1_nb/c2_nb  band-limited to ±bw/2 around (center+offset) — every
                   interferometric estimator and the centre power run on these
      fs_hz      sample rate after decimation
    """
    if offset_hz != 0.0:
        # digital_mix shifts by −offset (float64 phase accumulation, chunked) so
        # the tone at +offset lands at DC. Identical to physics.py.
        c1_raw = sdr_core.digital_mix(c1_raw, raw_fs_hz, offset_hz)
        c2_raw = sdr_core.digital_mix(c2_raw, raw_fs_hz, offset_hz)

    fs_hz = raw_fs_hz
    if decimation > 1:
        c1 = resample_poly(c1_raw, 1, decimation).astype(np.complex64)
        c2 = resample_poly(c2_raw, 1, decimation).astype(np.complex64)
        fs_hz = raw_fs_hz / decimation
    else:
        c1, c2 = c1_raw, c2_raw

    cutoff_hz = min(bw_hz / 2.0, 0.4 * fs_hz)
    c1_nb = sdr_core.lowpass_iq(c1, fs_hz, cutoff_hz)
    c2_nb = sdr_core.lowpass_iq(c2, fs_hz, cutoff_hz)
    return c1, c2, c1_nb, c2_nb, fs_hz


def center_power_db(ch_nb):
    """In-band mean power (after the offset+band-limit) in dB. Uncalibrated:
    the reference is the ADC LSB, so only DIFFERENCES/RATIOS are meaningful."""
    return float(10.0 * np.log10(np.mean(np.abs(ch_nb) ** 2) + 1e-30))


def extract_interferometry(c1_nb, c2_nb, fs_hz, fc_hz, bw_hz, block_sec=DEFAULT_BLOCK_SEC):
    """
    The physics extractor. Block complex coherence over the band-limited window.

    Returns a dict of the raw observables (see module docstring). Falls back to a
    single-window estimate when the window is shorter than one block.
    """
    n = min(len(c1_nb), len(c2_nb))
    dur = n / fs_hz
    n_eff = max(1.0, min(bw_hz * block_sec, block_sec * fs_hz))  # indep. samples/block

    if dur < 2.0 * block_sec:  # too short to block — single-window ML estimate
        coh, dphi = sdr_core.coherence_and_phase(c1_nb[:n], c2_nb[:n])
        return {
            "t_k": np.array([0.0]), "gamma_k": np.array([coh * np.exp(1j * dphi)]),
            "coh_k": np.array([coh]), "coh_med": coh, "coh_naive": coh,
            "dphi_rep": dphi, "dphi_naive": dphi, "drift_hz": np.nan,
            "P_d": center_power_db(c1_nb), "P_r": center_power_db(c2_nb),
            "n_eff": n_eff, "sig_phi": float(sdr_core.phase_sigma_crlb(coh, n_eff)),
            "n_blocks": 1, "block_sec": block_sec,
        }

    t_k, gamma_k, p_d_k, p_r_k = sdr_core.block_coherence(c1_nb, c2_nb, fs_hz, block_sec)
    coh_k = np.abs(gamma_k)
    coh_med = float(np.median(coh_k))

    # Single-window value kept ONLY for contrast — it is the biased one.
    coh_naive, dphi_naive = sdr_core.coherence_and_phase(c1_nb, c2_nb)

    # Representative ΔΦ: circular mean of the UNIT block phasors, so one loud
    # block cannot dominate and the ±π seam is handled correctly.
    unit = gamma_k / (np.abs(gamma_k) + 1e-30)
    dphi_rep = float(np.angle(np.mean(unit)))

    # Residual inter-chain carrier offset: unwrap the block phases over the
    # coherent blocks and read the slope. This is the drift the notes flag —
    # surfacing it explains any gap between the median and single-window |γ|.
    valid = coh_k >= 0.2
    drift_hz = np.nan
    if int(valid.sum()) >= 3:
        ph = sdr_core.unwrap_masked(np.angle(gamma_k), valid)
        slope = np.polyfit(t_k[valid], ph[valid], 1)[0]  # rad/s
        drift_hz = float(slope / (2.0 * np.pi))

    return {
        "t_k": t_k, "gamma_k": gamma_k, "coh_k": coh_k, "coh_med": coh_med,
        "coh_naive": float(coh_naive), "dphi_rep": dphi_rep, "dphi_naive": float(dphi_naive),
        "drift_hz": drift_hz,
        "P_d": float(10.0 * np.log10(np.mean(p_d_k) + 1e-30)),
        "P_r": float(10.0 * np.log10(np.mean(p_r_k) + 1e-30)),
        "n_eff": n_eff, "sig_phi": float(sdr_core.phase_sigma_crlb(coh_med, n_eff)),
        "n_blocks": len(t_k), "block_sec": block_sec,
    }


# ═══════════════════════════════════════════════════════════════
#  DSP: spectrograms (power of each channel + interferometric phase)
# ═══════════════════════════════════════════════════════════════
def compute_spectrograms(c1, c2, fs_hz, nperseg=2048, max_cols=MAX_WATERFALL_COLUMNS):
    """
    Return (f_hz, t_sec, p1_db, p2_db, phase_wf):
      p1_db/p2_db  per-channel power waterfalls (dB), fftshifted (freq, time)
      phase_wf     interferometric phase arg⟨S1·S2*⟩ per (freq, time) bin, wrapped

    Power is averaged LINEARLY over the pooled columns; the cross-spectrum is
    averaged as a COMPLEX quantity before its angle is taken (circular mean —
    the only wrap-safe way to multi-look a phase).
    """
    n = min(len(c1), len(c2))
    if n < 2:
        z = np.zeros((1, 1), dtype=np.float32)
        return np.array([0.0]), np.array([0.0]), z, z.copy(), z.copy()
    seg = min(n, nperseg)
    kw = dict(fs=fs_hz, window="hann", nperseg=seg, noverlap=0,
              return_onesided=False, mode="complex")
    f, t, S1 = spectrogram(c1[:n], **kw)
    _, _, S2 = spectrogram(c2[:n], **kw)
    S1 = np.fft.fftshift(S1, axes=0)
    S2 = np.fft.fftshift(S2, axes=0)

    P1 = np.abs(S1) ** 2
    P2 = np.abs(S2) ** 2
    X = S1 * np.conj(S2)  # cross-spectrum (complex), averaged before angle()

    n_cols = S1.shape[1]
    if n_cols > max_cols:
        pool = n_cols // max_cols
        keep = (n_cols // pool) * pool
        P1 = P1[:, :keep].reshape(P1.shape[0], -1, pool).mean(axis=2)
        P2 = P2[:, :keep].reshape(P2.shape[0], -1, pool).mean(axis=2)
        X = X[:, :keep].reshape(X.shape[0], -1, pool).mean(axis=2)
        t = t[:keep].reshape(-1, pool).mean(axis=1)

    f = np.fft.fftshift(f)
    p1_db = 10.0 * np.log10(P1 + 1e-10)
    p2_db = 10.0 * np.log10(P2 + 1e-10)
    return f, t, p1_db, p2_db, np.angle(X)


def phase_diff_1d(c1_nb, c2_nb):
    """Raw wrapped ΔΦ(t) = arg(z1·z2*) between two band-limited channels.
    NO smoothing (deliberately): the band-limit alone sets the noise, and any
    boxcar here would hide the very drift/steps the trace is meant to show."""
    n = min(len(c1_nb), len(c2_nb))
    if n < 1:
        return np.zeros(n, dtype=np.float32)
    return np.angle(c1_nb[:n] * np.conj(c2_nb[:n]))


def resolve_range(start_sec, end_sec, num_samples, fs_hz, n_total):
    i0 = int(round(start_sec * fs_hz))
    i1 = int(round(end_sec * fs_hz)) if end_sec is not None else i0 + int(num_samples)
    return max(0, min(i0, n_total - 1)), max(i0 + 1, min(i1, n_total))


def timedelay2distance(t):
    return t * 1e-6 * sdr_core.C_LIGHT


# ═══════════════════════════════════════════════════════════════
#  PLOTTING HELPERS
# ═══════════════════════════════════════════════════════════════
def style(ax, title=""):
    ax.set_facecolor(PANEL)
    if title:
        ax.set_title(title, color=TEXT, fontsize=9, pad=5, fontweight="bold")
    ax.tick_params(colors=MUTED, labelsize=7.5, which="both")
    ax.xaxis.label.set_color(MUTED)
    ax.yaxis.label.set_color(MUTED)
    for sp in ax.spines.values():
        sp.set_edgecolor(BORDER)
    ax.grid(True, color=BORDER, linewidth=0.6, linestyle="--")


def save_figure(fig, name, save_plots, save_dir):
    if not save_plots:
        return
    os.makedirs(save_dir, exist_ok=True)
    path = os.path.join(save_dir, f"{name}.png")
    fig.savefig(path, facecolor=fig.get_facecolor(), dpi=150, bbox_inches="tight")
    con.saved(path)


def standalone_figure(name, figsize, render_fn, save_plots, save_dir):
    fig = plt.figure(figsize=figsize, facecolor=BG)
    render_fn(fig, gridspec.GridSpec(1, 1, figure=fig, top=0.90, bottom=0.12,
                                     left=0.10, right=0.95)[0, 0])
    save_figure(fig, name, save_plots, save_dir)
    plt.close(fig)


# ═══════════════════════════════════════════════════════════════
#  RENDERERS
# ═══════════════════════════════════════════════════════════════
def render_iq(fig, spec, ch, title, color_time=True, lim=None):
    ax = fig.add_subplot(spec)
    stride = max(1, len(ch) // MAX_IQ_SCATTER_POINTS)
    ch_plot = ch[::stride][:MAX_IQ_SCATTER_POINTS]
    if color_time:
        sc = ax.scatter(np.real(ch_plot), np.imag(ch_plot), c=np.arange(len(ch_plot)),
                        cmap=CMAP_TIME, s=1, alpha=0.5)
        cbar = fig.colorbar(sc, ax=ax, fraction=0.046, pad=0.04)
        cbar.set_label('Sample Index (Time)', size=7, color=MUTED)
        cbar.ax.tick_params(labelsize=7, colors=MUTED)
    else:
        ax.scatter(np.real(ch_plot), np.imag(ch_plot), color=C1, s=1, alpha=0.5)
    ax.set_aspect('equal')
    if lim:
        ax.set_xlim(-lim, lim)
        ax.set_ylim(-lim, lim)
    style(ax, f"{title} (strided, {len(ch_plot)} pts)")
    ax.set_xlabel("In-Phase (I)")
    ax.set_ylabel("Quadrature (Q)")


def render_envelope(fig, spec, ch1, ch2, t_line, title="Complex Baseband Envelope |I+jQ|"):
    ax = fig.add_subplot(spec)
    ax.plot(t_line[::PLOT_DECIMATION], np.abs(ch1)[::PLOT_DECIMATION], color=C1,
            label="Ch1 Env (direct)", linewidth=0.8, alpha=0.8)
    ax.plot(t_line[::PLOT_DECIMATION], np.abs(ch2)[::PLOT_DECIMATION], color=C2,
            label="Ch2 Env (reflected)", linewidth=0.8, alpha=0.8)
    style(ax, title)
    ax.set_xlabel("Time (s)")
    ax.set_ylabel("Magnitude")
    ax.legend(fontsize=7, loc="upper right")


def render_xcorr(fig, spec, lags, mag, peak_tau, title="X-Corr"):
    ax = fig.add_subplot(spec)
    ax.plot(lags, mag, color=C_XC, linewidth=1.5, label="Correlation Mag")
    ax.axvline(peak_tau, color=C_PEAK, linestyle="--", linewidth=1.5,
               label=f"Peak: {peak_tau:.4f} µs")
    style(ax, title)
    ax.set_xlabel("Lag (µs)")
    ax.set_ylabel("Magnitude")
    ax.legend(fontsize=7)
    ax.set_xlim(peak_tau - (lags.max() - lags.min()) * 0.1,
                peak_tau + (lags.max() - lags.min()) * 0.1)


def render_meta_and_delay_text(fig, spec, obs, tau_xcorr, xcorr_peak, params,
                               dec_factor, n_samples, offset_mhz, target_bw_khz):
    ax = fig.add_subplot(spec)
    ax.axis("off")
    actual_fc = params['freq_mhz'] + offset_mhz
    fc_hz = actual_fc * 1e6
    dphi = obs["dphi_rep"]
    # group delay: xcorr envelope peak, resolution ~1/bandwidth (coarse)
    # carrier phase: ΔΦ wrapped to ±π  ->  ±1/(2·fc) of physical delay (fine)
    tau_phase_ns = dphi / (2 * np.pi * fc_hz) * 1e9
    delay_res_us = 1.0 / (target_bw_khz * 1e3) * 1e6
    gamma_db = obs["P_r"] - obs["P_d"]
    drift = ("n/a" if not np.isfinite(obs["drift_hz"]) else f"{obs['drift_hz']:+.2f} Hz")
    text = (
        f"CAPTURE METADATA\n"
        f"{'─' * 37}\n"
        f"HW Center Freq  : {params['freq_mhz']:.3f} MHz\n"
        f"Freq Offset     : {offset_mhz:+.3f} MHz\n"
        f"Actual Carrier  : {actual_fc:.3f} MHz\n"
        f"Base Sample Rate: {params['samplerate_mhz']:.2f} MS/s\n"
        f"Analysis BW     : {target_bw_khz:.1f} kHz  (±{target_bw_khz/2:.1f} kHz)\n"
        f"Global Decim    : {dec_factor}x\n"
        f"Samples Analysed: {n_samples:,}\n"
        f"Coh. Blocks     : {obs['n_blocks']} x {obs['block_sec']:.3f}s "
        f"(N_eff≈{obs['n_eff']:.0f})\n\n"
        f"INTERFEROMETRIC OBSERVABLES\n"
        f"{'─' * 37}\n"
        f"Coherence |γ|   : {obs['coh_med']:.3f}  (median block, drift-robust)\n"
        f"    naive 1-win : {obs['coh_naive']:.3f}  (biased low by drift)\n"
        f"Residual drift  : {drift}  (inter-chain carrier offset)\n"
        f"Carrier ΔΦ      : {dphi:+.4f} ± {obs['sig_phi']:.4f} rad (CRLB)\n"
        f" -> phase delay : {tau_phase_ns:+.4f} ns "
        f"({tau_phase_ns * 1e-9 * sdr_core.C_LIGHT * 100:+.2f} cm)\n"
        f"    ambiguity   : ±{1 / (2 * fc_hz) * 1e9:.4f} ns (±λ/2 path)\n\n"
        f"CENTRE-FREQ POWER (in-band, uncal.)\n"
        f"{'─' * 37}\n"
        f"P direct  (ch1) : {obs['P_d']:+.2f} dB\n"
        f"P reflected(ch2): {obs['P_r']:+.2f} dB\n"
        f"Γ = P_r/P_d     : {gamma_db:+.2f} dB  (reflectivity ratio)\n\n"
        f"Group delay (xc): {tau_xcorr:+.4f} µs "
        f"({timedelay2distance(tau_xcorr):.1f} m), peak {xcorr_peak:.2f}\n"
        f"    resolution  : ~{delay_res_us:.2f} µs (≈1/BW — coarse anchor)\n\n"
        f"NOTE: raw observables only. Group delay\n"
        f"(µs, envelope) and carrier phase (sub-ns)\n"
        f"differ by ~10^3x; do not subtract them.\n"
        f"Full soil-moisture/altimetry: physics.py.\n"
    )
    ax.text(0.03, 0.5, text, color=TEXT, fontsize=8.5, va="center", ha="left",
            family="monospace")


def render_aligned_views(fig, specs, p2_db, phase_wf, s_diff, s_diff_1d,
                         phase_diff, amp_diff, extent, t_line):
    # 1) interferometric-phase waterfall REPLACES the Ch1 power waterfall — Ch1
    #    and Ch2 look nearly identical, whereas arg⟨S1·S2*⟩ is the actual signal.
    #    Cyclic map + nearest interpolation (antialiasing averages wrapped angles
    #    to ~0 and would erase the phase structure).
    ax1 = fig.add_subplot(specs[0])
    im1 = ax1.imshow(phase_wf.T, aspect='auto', origin='lower', extent=extent,
                     cmap=CMAP_PHASE, vmin=-np.pi, vmax=np.pi, interpolation='nearest')
    style(ax1, "ΔΦ Waterfall  arg⟨S1·S2*⟩")
    ax1.set_ylabel("Time (s)")
    ax1.set_xlabel("Frequency (kHz)")
    cb1 = fig.colorbar(im1, ax=ax1, fraction=0.046, pad=0.04, ticks=[-np.pi, 0, np.pi])
    cb1.ax.set_yticklabels(['-π', '0', 'π'])
    cb1.set_label("ΔΦ (rad)", size=7, color=MUTED)

    ax2 = fig.add_subplot(specs[1], sharey=ax1, sharex=ax1)
    im2 = ax2.imshow(p2_db.T, aspect='auto', origin='lower', extent=extent,
                     cmap=CMAP_WF, interpolation='nearest')
    style(ax2, "Waterfall Ch2 (reflected)")
    ax2.set_xlabel("Frequency (kHz)")
    plt.setp(ax2.get_yticklabels(), visible=False)
    fig.colorbar(im2, ax=ax2, fraction=0.046, pad=0.04).set_label("dB", size=7, color=MUTED)

    diff_max = max(np.percentile(np.abs(s_diff), 99), 1.0)
    ax3 = fig.add_subplot(specs[2], sharey=ax1, sharex=ax1)
    im3 = ax3.imshow(s_diff.T, aspect='auto', origin='lower', extent=extent,
                     cmap=CMAP_DIF, vmin=-diff_max, vmax=diff_max, interpolation='nearest')
    style(ax3, "Δ-Power (Ch1-Ch2)")
    ax3.set_xlabel("Frequency (kHz)")
    plt.setp(ax3.get_yticklabels(), visible=False)
    fig.colorbar(im3, ax=ax3, fraction=0.046, pad=0.04).set_label("Δ dB", size=7, color=MUTED)

    ax4 = fig.add_subplot(specs[3], sharey=ax1)
    ax4.plot(amp_diff[::PLOT_DECIMATION], t_line[::PLOT_DECIMATION], color=C_DIFF,
             linewidth=0.5, label="|C1| - |C2|")
    style(ax4, "Amplitude Diff")
    ax4.set_xlabel("Δ Magnitude")
    ax4.legend(loc="upper right", fontsize=7)
    ax4.axvline(0, color=MUTED, linestyle="--", linewidth=1)
    plt.setp(ax4.get_yticklabels(), visible=False)

    ax5 = fig.add_subplot(specs[4], sharey=ax1)
    ax5.plot(phase_diff[::PLOT_DECIMATION], t_line[::PLOT_DECIMATION], color=C_PH,
             linewidth=0.5, label="ΔΦ (raw)")
    style(ax5, "Phase Diff (raw)")
    ax5.set_xlabel("Radians")
    ax5.legend(loc="upper right", fontsize=7)
    ax5.set_xticks([-np.pi, 0, np.pi])
    ax5.set_xticklabels(['-π', '0', 'π'])
    ax5.axvline(0, color=MUTED, linestyle="--", linewidth=1)
    plt.setp(ax5.get_yticklabels(), visible=False)

    ax6 = fig.add_subplot(specs[5], sharey=ax1)
    t_spec = np.linspace(extent[2], extent[3], len(s_diff_1d))
    ax6.plot(s_diff_1d, t_spec, color=TEXT, linewidth=1.5, label="Avg Δ dB")
    style(ax6, "BW-Avg Δ-Power")
    ax6.set_xlabel("Δ dB")
    ax6.legend(loc="upper right", fontsize=7)
    ax6.axvline(0, color=MUTED, linestyle="--", linewidth=1)
    plt.setp(ax6.get_yticklabels(), visible=False)
    ax1.set_ylim(extent[2], extent[3])


def plot_reconstructed_rf(ch1, ch2, fs_hz, fc_hz, save_plots, save_dir, num_samples, info_str=""):
    """
    Illustrative carrier reconstruction s(t) = Re{ z(t)·e^{j2π·f·t} }.

    A GHz carrier cannot be evaluated on MS/s sample times, so if fc is not
    representable at this fs the baseband is re-modulated onto a low DISPLAY IF
    (~12 cycles across the window); the label states the carrier is illustrative.
    """
    n = min(len(ch1), num_samples)
    t = np.arange(n) / fs_hz
    if fc_hz < 0.4 * fs_hz:
        f_disp = fc_hz
        carrier_note = f"true carrier {fc_hz / 1e6:.3f} MHz"
    else:
        f_disp = 12.0 * fs_hz / max(n, 1)
        carrier_note = (f"display IF {f_disp / 1e3:.1f} kHz — ILLUSTRATIVE "
                        f"(true fc {fc_hz / 1e6:.1f} MHz not representable at fs={fs_hz / 1e6:.2f} MS/s)")

    carrier = np.exp(2j * np.pi * f_disp * t)
    s1 = np.real(ch1[:n] * carrier)
    s2 = np.real(ch2[:n] * carrier)

    fig, ax = plt.subplots(figsize=(10, 4), facecolor=BG)
    ax.plot(t * 1e6, s1, label="Ch1 Reconstructed", color=C1, alpha=0.8, linewidth=1.2)
    ax.plot(t * 1e6, s2, label="Ch2 Reconstructed", color=C2, alpha=0.8, linestyle="--", linewidth=1.2)
    style(ax, f"Reconstructed Carrier ({carrier_note}) | {info_str}")
    ax.set_xlabel("Time (µs)")
    ax.set_ylabel("Amplitude")
    ax.legend(loc="upper right")
    save_figure(fig, "05_08_Reconstructed_RF_Zoom", save_plots, save_dir)
    plt.close(fig)


# ═══════════════════════════════════════════════════════════════
#  MAIN ENTRY POINT
# ═══════════════════════════════════════════════════════════════
def run_iq_dashboard(
    input_file, center_freq_offset_mhz=0.0, analysis_bandwidth_mhz=0.025, max_samples=10_000_000,
    global_decimation_factor=4, window_start_sec=0.0, window_end_sec=60.0, window_num_samples=50_000,
    xcorr_start_sec=0.0, xcorr_end_sec=10.0, xcorr_num_samples=None, run_auto_best_window_xcorr=True,
    auto_xcorr_window_sec=0.05, auto_xcorr_step_sec=1.0, block_sec=DEFAULT_BLOCK_SEC, save_plots=True,
    # "." (not an absolute /Users/... path) so a direct import behaves the same
    # on any machine/repo location; MAIN.py and the CLI always pass save_dir
    # explicitly and are unaffected. See --save-dir's identical default below.
    save_dir=".",
    plot_reconstructed_zoom=True, rf_zoom_samples=250, verbose=True,
):
    if verbose:
        con.banner("IQ DASHBOARD — interferometric observables")

    resolved_input_file = resolve_input_path(input_file)
    meta = load_metadata(resolved_input_file)
    params = extract_params(meta)

    target_bw_khz = analysis_bandwidth_mhz * 1000.0
    offset_hz = center_freq_offset_mhz * 1e6
    bw_hz = target_bw_khz * 1e3
    fc_hz = (params["freq_mhz"] * 1e6) + offset_hz
    raw_fs_hz = params["samplerate_mhz"] * 1e6
    info_str = (f"Fc {fc_hz / 1e6:.3f} MHz (HW {params['freq_mhz']:.3f} {center_freq_offset_mhz:+.3f}) "
                f"| BW {target_bw_khz:.1f} kHz")

    if verbose:
        con.say(f"   loading {os.path.basename(resolved_input_file)}")
    ch1_raw, ch2_raw, _ = load_iq(resolved_input_file, max_samples)

    # mix → decimate → band-limit (identical convention to physics.py)
    ch1, ch2, ch1_nb, ch2_nb, fs_hz = prepare_channels(
        ch1_raw, ch2_raw, raw_fs_hz, offset_hz, global_decimation_factor, bw_hz)

    i0, i1 = resolve_range(window_start_sec, window_end_sec, window_num_samples, fs_hz, len(ch1))
    c1_z, c2_z = ch1[i0:i1], ch2[i0:i1]
    c1_nb, c2_nb = ch1_nb[i0:i1], ch2_nb[i0:i1]
    global_iq_lim = max(np.max(np.abs(c1_z)), np.max(np.abs(c2_z))) * 1.05

    # ── the physics extractor ──────────────────────────────────────────
    obs = extract_interferometry(c1_nb, c2_nb, fs_hz, fc_hz, bw_hz, block_sec)

    # Group delay over the xcorr_* window (band-limited channels only).
    xi0, xi1 = resolve_range(xcorr_start_sec, xcorr_end_sec, xcorr_num_samples, fs_hz, len(ch1_nb))
    tau_xcorr, peak_mag, lags, xmag = sdr_core.xcorr_delay(ch1_nb[xi0:xi1], ch2_nb[xi0:xi1], fs_hz)

    if verbose:
        con.say()
        con.say("   interferometric observables (analysis band)", "bold")
        drift = ("n/a" if not np.isfinite(obs["drift_hz"]) else f"{obs['drift_hz']:+.2f} Hz")
        con.kv("Coherence |γ| (median)", f"{obs['coh_med']:.3f}   (naive 1-window {obs['coh_naive']:.3f})")
        con.kv("Carrier ΔΦ", f"{obs['dphi_rep']:+.4f} ± {obs['sig_phi']:.4f} rad (CRLB)")
        con.kv("Residual drift", f"{drift}")
        con.kv("P direct  (ch1)", f"{obs['P_d']:+.2f} dB")
        con.kv("P reflected (ch2)", f"{obs['P_r']:+.2f} dB")
        con.kv("Γ = P_r/P_d", f"{obs['P_r'] - obs['P_d']:+.2f} dB", val_style="cyan")
        con.kv("Group delay (xcorr)", f"{tau_xcorr:+.4f} µs (peak {peak_mag:.2f})")

    # Waterfalls (power of each channel + cross-phase) on the wide-band window.
    f1, t1, p1_db, p2_db, phase_wf = compute_spectrograms(c1_z, c2_z, fs_hz)
    s_diff = p1_db - p2_db
    s_diff_1d = np.mean(s_diff, axis=0)
    phase_diff = phase_diff_1d(c1_nb, c2_nb)
    amp_diff = np.abs(c1_z) - np.abs(c2_z)
    extent = [f1[0] / 1e3, f1[-1] / 1e3, t1[0], t1[-1]]
    t_line = np.linspace(t1[0], t1[-1], len(c1_z))

    if verbose:
        con.say()
        con.say("   rendering figures", "bold")
    standalone_figure("05_01_IQ_Ch1", (5, 5),
                      lambda f, s: render_iq(f, s, c1_z, f"IQ Ch1 | {info_str}", lim=global_iq_lim),
                      save_plots, save_dir)
    standalone_figure("05_02_IQ_Ch2", (5, 5),
                      lambda f, s: render_iq(f, s, c2_z, f"IQ Ch2 | {info_str}", lim=global_iq_lim),
                      save_plots, save_dir)

    def plot_phase_diff_horizontal(f, s):
        ax = f.add_subplot(s)
        ax.plot(t_line[::PLOT_DECIMATION], phase_diff[::PLOT_DECIMATION], color=C_PH,
                linewidth=1.0, label="ΔΦ (raw, unsmoothed)")
        style(ax, f"Phase Difference (ΔΦ) | {info_str}")
        ax.set_xlabel("Time (s)")
        ax.set_ylabel("Radians")
        ax.set_yticks([-np.pi, 0, np.pi])
        ax.set_yticklabels(['-π', '0', 'π'])
        ax.legend()

    standalone_figure("05_03_Phase_Diff", (8, 4), plot_phase_diff_horizontal, save_plots, save_dir)
    standalone_figure("05_04_Envelope", (8, 4),
                      lambda f, s: render_envelope(f, s, c1_z, c2_z, t_line,
                                                   f"Complex Baseband Envelope |I+jQ| | {info_str}"),
                      save_plots, save_dir)
    standalone_figure("05_05_XCorr", (6, 4),
                      lambda f, s: render_xcorr(f, s, lags, xmag, tau_xcorr,
                                                f"X-Corr ({xi1 - xi0:,} smp, band-limited) | {info_str}"),
                      save_plots, save_dir)

    if plot_reconstructed_zoom:
        plot_reconstructed_rf(c1_z, c2_z, fs_hz, fc_hz, save_plots, save_dir, rf_zoom_samples, info_str)

    fig = plt.figure(figsize=(24, 12), facecolor=BG)
    fig.suptitle(f"Data Stream Analyzer — Time-Aligned Dashboard\n{info_str}",
                 color=TEXT, fontsize=16, fontweight="bold")
    gs = gridspec.GridSpec(1, 7, figure=fig, wspace=0.7, hspace=0.4)
    render_aligned_views(fig, [gs[0, i] for i in range(6)], p2_db, phase_wf,
                         s_diff, s_diff_1d, phase_diff, amp_diff, extent, t_line)
    render_meta_and_delay_text(fig, gs[0, 6], obs, tau_xcorr, peak_mag, params,
                               global_decimation_factor, len(c1_z), center_freq_offset_mhz, target_bw_khz)
    save_figure(fig, "05_00_Main_Dashboard", save_plots, save_dir)
    plt.close(fig)

    if run_auto_best_window_xcorr:
        _sliding_window_figure(ch1_nb, ch2_nb, fs_hz, xcorr_start_sec, xcorr_end_sec,
                               xcorr_num_samples, auto_xcorr_window_sec, auto_xcorr_step_sec,
                               info_str, save_plots, save_dir)

    if verbose:
        con.say()
        con.say("   dashboard complete", "green")
    return {"obs": obs, "tau_xcorr_us": tau_xcorr, "xcorr_peak": peak_mag,
            "fc_hz": fc_hz, "fs_hz": fs_hz, "info": info_str}


def _sliding_window_figure(ch1_nb, ch2_nb, fs_hz, xcorr_start_sec, xcorr_end_sec,
                           xcorr_num_samples, win_sec, step_sec, info_str, save_plots, save_dir):
    t_start, t_end = resolve_range(xcorr_start_sec, xcorr_end_sec, xcorr_num_samples, fs_hz, len(ch1_nb))
    step_samp = max(1, int(step_sec * fs_hz))
    win_samp = max(2, int(win_sec * fs_hz))
    times, x_delays, p_phases, cohs = [], [], [], []
    for i in range(t_start, max(t_start + 1, t_end - win_samp), step_samp):
        c1_w, c2_w = ch1_nb[i:i + win_samp], ch2_nb[i:i + win_samp]
        tx, _, _, _ = sdr_core.xcorr_delay(c1_w, c2_w, fs_hz)
        coh_w, dphi_w = sdr_core.coherence_and_phase(c1_w, c2_w)
        times.append(i / fs_hz)
        x_delays.append(tx)
        p_phases.append(dphi_w)
        cohs.append(coh_w)
    if not times:
        return
    fig3, (ax, ax2, ax3) = plt.subplots(3, 1, sharex=True, figsize=(10, 8), facecolor=BG)
    fig3.suptitle(f"Sliding Window Interferometry | {info_str}", color=TEXT, fontsize=12, fontweight="bold")
    ax.plot(times, x_delays, 'o-', color=C_XC, label="XCorr Group Delay", markersize=4)
    style(ax, "Group Delay (coarse, resolution ≈ 1/BW)")
    ax.set_ylabel("Delay (µs)")
    ax.legend(fontsize=7)
    ax2.plot(times, p_phases, 's-', color=C_PH, label="Carrier ΔΦ (wrapped)", markersize=4)
    ax2.set_ylim(-np.pi, np.pi)
    ax2.set_yticks([-np.pi, 0, np.pi])
    ax2.set_yticklabels(['-π', '0', 'π'])
    style(ax2, "Carrier Phase (wrapped ±π)")
    ax2.set_ylabel("ΔΦ (rad)")
    ax2.legend(fontsize=7)
    ax3.plot(times, cohs, '^-', color=C_XC, label="|γ| Coherence", markersize=4)
    ax3.set_ylim(0, 1.05)
    style(ax3, "Interferometric Coherence")
    ax3.set_xlabel("Capture Time (s)")
    ax3.set_ylabel("|γ|")
    ax3.legend(fontsize=7)
    fig3.subplots_adjust(hspace=0.35)
    save_figure(fig3, "05_07_Sliding_Delay", save_plots, save_dir)
    plt.close(fig3)


# ═══════════════════════════════════════════════════════════════
#  LIGHTWEIGHT: just the centre-frequency power (no plots)
# ═══════════════════════════════════════════════════════════════
def report_center_power(input_file, center_freq_offset_mhz=0.0, analysis_bandwidth_mhz=0.025,
                        global_decimation_factor=1, max_samples=10_000_000, block_sec=DEFAULT_BLOCK_SEC):
    """Load a capture and print ONLY the in-band signal power at (center+offset),
    for both channels plus the reflectivity ratio. No figures. Returns the obs dict."""
    con.banner("CENTRE-FREQUENCY SIGNAL POWER")
    resolved = resolve_input_path(input_file)
    params = extract_params(load_metadata(resolved))
    offset_hz = center_freq_offset_mhz * 1e6
    bw_hz = analysis_bandwidth_mhz * 1e6
    fc_hz = params["freq_mhz"] * 1e6 + offset_hz
    raw_fs_hz = params["samplerate_mhz"] * 1e6

    con.kv("Capture", os.path.basename(resolved))
    con.kv("Centre freq", f"{fc_hz / 1e6:.4f} MHz (HW {params['freq_mhz']:.3f} {center_freq_offset_mhz:+.3f})")
    con.kv("Analysis band", f"±{analysis_bandwidth_mhz * 1000 / 2:.1f} kHz")

    ch1_raw, ch2_raw, _ = load_iq(resolved, max_samples)
    _, _, c1_nb, c2_nb, fs_hz = prepare_channels(
        ch1_raw, ch2_raw, raw_fs_hz, offset_hz, global_decimation_factor, bw_hz)
    obs = extract_interferometry(c1_nb, c2_nb, fs_hz, fc_hz, bw_hz, block_sec)

    con.say()
    con.kv("P direct   (ch1)", f"{obs['P_d']:+.2f} dB", val_style="bold")
    con.kv("P reflected (ch2)", f"{obs['P_r']:+.2f} dB", val_style="bold")
    con.kv("Γ = P_r/P_d", f"{obs['P_r'] - obs['P_d']:+.2f} dB", val_style="cyan")
    con.kv("Coherence |γ|", f"{obs['coh_med']:.3f}")
    con.note("power is uncalibrated (ADC LSB reference) — the Γ ratio is the physical quantity")
    return obs


# ═══════════════════════════════════════════════════════════════
#  SELF-TEST — known-truth synthetic validation
# ═══════════════════════════════════════════════════════════════
def _selftest():
    con.banner("iq_dashboard SELF-TEST (known-truth synthetic)")
    rng = np.random.default_rng(0)
    ok = True

    def check(name, cond, detail=""):
        nonlocal ok
        con.say(f" [{'PASS' if cond else 'FAIL'}] {name}  {detail}",
                *("green",) if cond else ("red",))
        ok &= bool(cond)

    fs = 2e6
    dur = 4.0
    n = int(fs * dur)
    t = np.arange(n) / fs
    f_off = 300e3          # beacon 300 kHz above HW centre
    dphi_true = 1.0        # ch2 lags ch1 by 1.0 rad
    amp_r = 0.5            # reflected amplitude = 0.5·direct  -> Γ = -6.02 dB
    a = np.exp(2j * np.pi * f_off * t)
    sigma = 0.02           # high SNR -> |γ| ~ 1
    c1 = (a + sigma * (rng.standard_normal(n) + 1j * rng.standard_normal(n))).astype(np.complex64)
    c2 = (amp_r * a * np.exp(-1j * dphi_true)
          + sigma * (rng.standard_normal(n) + 1j * rng.standard_normal(n))).astype(np.complex64)
    bw_hz = 50e3

    # --- correct offset: beacon lands in the band ---
    _, _, c1_nb, c2_nb, fso = prepare_channels(c1, c2, fs, f_off, 1, bw_hz)
    obs = extract_interferometry(c1_nb, c2_nb, fso, 2.4e9, bw_hz, block_sec=0.1)
    check("high-SNR coherence |γ| > 0.98", obs["coh_med"] > 0.98, f"(got {obs['coh_med']:.4f})")
    dphi_err = abs(np.angle(np.exp(1j * (obs["dphi_rep"] - dphi_true))))
    check("recovered ΔΦ ≈ +1.0 rad", dphi_err < 0.02, f"(got {obs['dphi_rep']:+.4f}, err {dphi_err:.4f})")
    gamma_db = obs["P_r"] - obs["P_d"]
    check("power ratio Γ ≈ -6.02 dB", abs(gamma_db - (-6.02)) < 0.3, f"(got {gamma_db:+.3f} dB)")

    # --- wrong offset (0): beacon at 300 kHz is OUTSIDE the ±25 kHz band ---
    _, _, c1_w, c2_w, _ = prepare_channels(c1, c2, fs, 0.0, 1, bw_hz)
    p_out = center_power_db(c1_w)
    check("window follows the offset (beacon rejected when offset=0)",
          obs["P_d"] - p_out > 20.0,
          f"(in-band {obs['P_d']:+.1f} dB vs off-band {p_out:+.1f} dB)")

    # --- drift recovery: inject a realistic inter-chain frequency offset ---
    # (~0.1-0.3 Hz is what a shared-LO bladeRF shows; must be sub-radian per
    #  block or the block coherence itself would degrade — that is physics, not
    #  a bug, so the test uses a realistic value.)
    df = 0.3  # Hz  ->  0.19 rad/block, ~7.5 rad across the 4 s window
    c2_drift = (amp_r * a * np.exp(-1j * dphi_true) * np.exp(-1j * 2 * np.pi * df * t)
                + sigma * (rng.standard_normal(n) + 1j * rng.standard_normal(n))).astype(np.complex64)
    _, _, c1d, c2d, fsd = prepare_channels(c1, c2_drift, fs, f_off, 1, bw_hz)
    obs_d = extract_interferometry(c1d, c2d, fsd, 2.4e9, bw_hz, block_sec=0.1)
    check("residual drift recovered (~+0.3 Hz)", abs(obs_d["drift_hz"] - df) < 0.05,
          f"(got {obs_d['drift_hz']:+.3f} Hz)")
    check("median |γ| stays high under drift (block-robust)", obs_d["coh_med"] > 0.95,
          f"(median {obs_d['coh_med']:.3f} vs naive {obs_d['coh_naive']:.3f})")

    con.rule("─", "cyan")
    con.say(" OVERALL: " + ("PASS ✅" if ok else "FAIL ❌"), *("green",) if ok else ("red",))
    return 0 if ok else 1


def _cli():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    p.add_argument("json_path", nargs="?", help="capture .json (with matching .bin)")
    p.add_argument("--selftest", action="store_true", help="run known-truth validation")
    p.add_argument("--power", action="store_true", help="print centre-freq power only (no plots)")
    p.add_argument("--offset", type=float, default=0.0, help="center freq offset [MHz]")
    p.add_argument("--bw", type=float, default=0.05, help="analysis bandwidth [MHz]")
    p.add_argument("--decim", type=int, default=1, help="global decimation factor")
    p.add_argument("--save-dir", default=".")
    args = p.parse_args()

    if args.selftest:
        sys.exit(_selftest())
    if not args.json_path:
        p.error("provide a capture .json, --selftest, or --power <json>")
    if args.power:
        report_center_power(args.json_path, center_freq_offset_mhz=args.offset,
                            analysis_bandwidth_mhz=args.bw, global_decimation_factor=args.decim)
    else:
        run_iq_dashboard(args.json_path, center_freq_offset_mhz=args.offset,
                         analysis_bandwidth_mhz=args.bw, global_decimation_factor=args.decim,
                         save_dir=args.save_dir)


if __name__ == "__main__":
    _cli()
