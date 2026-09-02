"""
band_correlator.py — global dual-channel cross-correlation of the BW-reduced sliver.

EXTRA / SIDE-CHANNEL TOOL. Nothing in the main pipeline imports it, it takes no
arguments away from anything else and it writes its own 08_xx figures, so
flipping RUN_SIGNAL_XCORR on or off cannot change a single number produced by
the waterfalls, the IQ dashboard or physics.py.

WHAT IT DOES — four stages
══════════════════════════

1. REBUILD THE SLIVER (no changes to waterfalls.py)
   Exactly the chain the 1D waterfall stage runs on the analysis band:
       digital_mix(-offset)  ->  bandlimit_and_decimate(analysis_bw)
   Same sdr_core primitives, same two knobs, so the samples correlated here are
   bit-identical to the ones behind the 1D phase/delay traces. sdr_core's IQ
   cache normally makes the re-derivation free (no second read off disk).

2. RECONSTRUCT THE WAVEFORM
   The sliver is a COMPLEX baseband z(t) = I + jQ. The real, scope-like waveform
   it stands for is
       s(t) = Re{ z(t) · exp(j·2π·f_IF·t) }
   The true RF carrier (GHz) cannot be evaluated at a few MS/s, so f_IF is set to
   bw/2 — the MINIMUM intermediate frequency at which the baseband [-bw/2,+bw/2]
   becomes a real bandpass signal occupying [0, bw] with no aliasing.
   bandlimit_and_decimate always leaves fs >= 2.5·bw > 2·bw, so this is exact and
   lossless: the same waveform shifted in frequency, not an illustration.
   (iq_dashboard's 05_08 zoom uses an ARBITRARY display IF because it runs on the
   wide-band stream; here the band is known, so the IF is the canonical one.)

   The DC term is removed first. After the mix, hardware LO leakage sits at
   -offset, i.e. exactly at DC when CENTER_FREQ_OFFSET_MHZ = 0 — a large constant
   that would dominate both the envelope and the correlation at lag 0.

3. AMPLITUDE THRESHOLD ("only the peaks above 50% of the data")
   The gate is applied to the ENVELOPE |z(t)|, not to s(t): |z| is independent of
   carrier phase, whereas s(t) crosses zero twice per RF cycle, so thresholding
   s(t) would chop every burst into carrier half-cycles instead of selecting it.

       keep sample n  <=>  |z(n)| >= percentile(|z|, threshold_pct)

   Each channel gets its OWN percentile by default: rx1 (RE, direct) and rx2
   (GR, reflected) sit at very different absolute levels, and one shared absolute
   threshold would gate the reflected chain down to nothing.

   Gating is a NONLINEAR operation, so it can bias a delay estimate. That is why
   the ungated correlation is computed alongside by default (compare_ungated) —
   if the threshold helped, the gated peak is sharper and its peak-to-sidelobe
   ratio is higher; if it hurt, you can see that instead of assuming.

4. GLOBAL CROSS-CORRELATION — two accumulations, one pass
   Global = every sample of the record contributes, not a 1000-sample window like
   the dashboard's 05_05. The record is cut into coherent blocks of length L and
   the exact linear correlation of each block,

       R_b(τ) = Σ_{n ∈ block b}  z1(n+τ) · conj(z2(n)),        |τ| <= M

   is evaluated by FFT (each block zero-padded to N = L + 2M, so the circular
   correlation equals the linear one over the whole lag range — no wraparound).
   The blocks are then combined in the two standard ways:

     COHERENT    C(τ) = |Σ_b R_b(τ)| / sqrt(E1·E2)
                 One correlation over the whole stream, phase and all. Maximum
                 processing gain — but ONLY if arg R is stable across the record.
     INCOHERENT  I(τ) = (1/B) Σ_b |R̂_b(τ)|²      (R̂ = per-block normalised)
                 Magnitudes added after each block is correlated coherently.
                 Immune to slow phase drift; the standard GNSS non-coherent
                 integration.

   WHY BOTH. This receiver has a residual inter-chain carrier offset of order
   0.1-0.3 Hz (iq_dashboard measures it and reports it as `drift_hz`). Over a 60 s
   record that is ~10-20 full rotations of the phasor, so the COHERENT sum very
   nearly cancels itself and reads far below the true correlation — that is
   physics, not a bug. The INCOHERENT sum is therefore the one to read for the
   peak; the coherent/incoherent ratio tells you how much coherence the drift is
   costing. Both are normalised to [0,1] by Cauchy-Schwarz.

   SIGN CONVENTION (inherited from sdr_core.complex_xcorr):
       positive lag = ch1 delayed relative to ch2.
   ch1 = rx1 = RE (direct) and ch2 = rx2 = GR (ground-reflected), and the
   reflected path is the LONGER one, so a real specular return is expected at
   NEGATIVE lag. Path difference = c·τ.

Run standalone:
    python band_correlator.py <capture.json> --bw 1.0 --offset 0.0
    python band_correlator.py --selftest        # known-truth injected delay

VENDORED COPY — see sreto.analysis package docstring (__init__.py) for what
that means and how this file is kept in sync with 01_CODE/band_correlator.py
in the sdr_r science repo. Only the `sdr_core` / `console` imports below
differ (package-relative here vs bare in sdr_r).
"""

import argparse
import os
import sys

import numpy as np
import matplotlib
matplotlib.use("Agg")     # before pyplot: this tool only ever savefig()s, and
                          # the macosx backend builds a Cocoa canvas per figure
                          # and registers the process with the WindowServer.
import matplotlib.pyplot as plt
import scipy.fft
from scipy.signal import hilbert, resample_poly

from . import sdr_core                  # sdr_r: `import sdr_core`
from . import console as con            # sdr_r: `import console as con`

matplotlib.rcParams.update({"font.family": "monospace", "axes.unicode_minus": False})

# ═══════════════════════════════════════════════════════════════
#  PALETTE — the pipeline's shared look (see waterfalls.py / iq_dashboard.py)
# ═══════════════════════════════════════════════════════════════
BG, PANEL, BORDER, TEXT, MUTED = "#f8f9fa", "#ffffff", "#dee2e6", "#212529", "#6c757d"
C1 = "#1f77b4"        # ch1 / rx1 / RE  (direct)      — cool
C2 = "#ff7f0e"        # ch2 / rx2 / GR  (reflected)   — warm
C_XC = "#9467bd"      # correlation trace
C_PEAK = "#e377c2"    # peak marker
C_REF = "#adb5bd"     # ungated reference / de-emphasised
C_THR = "#d62728"     # threshold marker (same red the waterfalls use for the
                      # extraction window — both mean "this is the selection")

_FFT_WORKERS = -1

# Display-only limits.
MAX_TRACE_POINTS = 4000     # peak-hold decimation target for the full-record traces
ZOOM_UPSAMPLE = 16          # waveform zoom is interpolated so it reads as a curve


# ═══════════════════════════════════════════════════════════════
#  STAGE 1 — rebuild the analysis sliver
# ═══════════════════════════════════════════════════════════════
def extract_sliver(json_filepath, center_freq_offset_mhz=0.0, analysis_bandwidth_mhz=1.0,
                   percentage=1.0, max_samples=None):
    """
    Re-derive the BW-reduced sliver the 1D waterfall chain analyses.

    Returns (z1, z2, fs_b, fc_rf, bw_hz, meta) with z1/z2 complex64 baseband at
    fs_b, and fc_rf = hardware centre + offset (the RF carrier the band sits on).
    """
    ch1, ch2, fs, fc, _hw_bw, meta, is_dual = sdr_core.load_dual_iq(
        json_filepath, max_samples=max_samples, percentage=percentage)
    if not is_dual:
        raise ValueError("[band-xcorr] requires a dual-channel (ch1_2) capture.")

    offset_hz = float(center_freq_offset_mhz) * 1e6
    bw_hz = float(analysis_bandwidth_mhz) * 1e6

    # Identical to waterfalls.generate_phase_waterfalls_and_1d's 1D chain.
    z1, fs_b = sdr_core.bandlimit_and_decimate(
        sdr_core.digital_mix(ch1, fs, offset_hz), fs, bw_hz)
    z2, _ = sdr_core.bandlimit_and_decimate(
        sdr_core.digital_mix(ch2, fs, offset_hz), fs, bw_hz)

    n = min(len(z1), len(z2))
    return z1[:n], z2[:n], fs_b, fc + offset_hz, bw_hz, meta


def remove_dc(z):
    """Subtract the mean and report how big it was, in dB relative to the AC power.

    After digital_mix the hardware LO leak sits at -offset — exactly at DC when
    the offset is 0. Left in, it is a constant that swamps the envelope (so the
    threshold selects nothing meaningful) and puts a huge triangular pedestal
    under the correlation.
    """
    dc = complex(np.mean(z))
    z = (z - dc).astype(np.complex64)
    ac = float(np.mean(np.abs(z) ** 2))
    dc_db = 10.0 * np.log10((abs(dc) ** 2 + 1e-30) / (ac + 1e-30))
    return z, dc, dc_db


def notch_dc(z, fs_b, notch_hz):
    """
    High-pass the sliver by `notch_hz` — subtract its own low-passed copy.

    WHY THIS IS NOT OPTIONAL IN PRACTICE. Removing the MEAN (above) is a
    zero-width notch, and it is not enough: the bladeRF's DC/LO offset is not a
    constant, it WANDERS, so it survives as a narrow smear a few hundred Hz wide
    around DC. Measured on a 1621.25 MHz Iridium capture with a 1 MHz analysis
    band: 66% of the band's total energy sat inside ±1 kHz of DC at |γ| = 0.986,
    against a band-wide mean |γ| of 0.064.

    Both receive chains share one LO, so that leakage is ~99% COMMON-MODE and it
    dominates the cross-correlation completely — a component confined to ±1 kHz
    has a ~1 ms correlation time, which shows up as a broad triangular pedestal
    that buries any real signal peak. It is an artefact of the hardware, not sky.

    The cost is negligible: a few kHz out of a ~1 MHz band is <1% of the
    bandwidth, so the ~1/BW delay resolution is unchanged. lowpass_iq is
    zero-phase (filtfilt), so no group delay is introduced into either channel.

    Set notch_hz = 0 to disable and see the raw behaviour.
    """
    if not notch_hz or notch_hz <= 0:
        return z, 0.0
    e0 = float(np.mean(np.abs(z) ** 2)) + 1e-30
    z = (z - sdr_core.lowpass_iq(z, fs_b, float(notch_hz))).astype(np.complex64)
    e1 = float(np.mean(np.abs(z) ** 2))
    return z, float(100.0 * (1.0 - e1 / e0))


# ═══════════════════════════════════════════════════════════════
#  STAGE 2 — waveform reconstruction
# ═══════════════════════════════════════════════════════════════
def display_if_hz(bw_hz, fs_b):
    """Highest alias-free IF (capped at bw) for a real rendition of the baseband.

    A real signal at IF occupies ±[IF-bw/2, IF+bw/2], so IF must satisfy
    IF + bw/2 < fs/2. The obvious choice IF = bw/2 puts the band at [0, bw] —
    the minimum fs, but it TOUCHES DC, which both smears the waveform with a
    near-zero-frequency component and breaks the Bedrosian condition that makes
    the rendition analytically invertible. Pushing the IF up to `bw` (band
    [bw/2, 3bw/2]) clears DC entirely; when fs cannot afford that, the largest
    IF that still fits under 0.9·Nyquist is used.

    bandlimit_and_decimate guarantees fs >= 2.5·bw, which yields IF = 0.625·bw
    and a band of [0.125·bw, 1.125·bw] — clear of both DC and Nyquist.
    """
    hi = 0.45 * fs_b - bw_hz / 2.0      # highest IF keeping the band under Nyquist
    if hi <= 0.0:                       # band already fills Nyquist: nothing clean
        return float(0.25 * fs_b)
    return float(min(bw_hz, hi))


def reconstruct_waveform(z, fs_b, f_if_hz, upsample=1):
    """
    Real waveform s(t) = Re{z(t)·exp(j2π·f_IF·t)}, optionally interpolated.

    `upsample` only makes the curve readable on screen — z is band-limited, so
    polyphase interpolation adds no information and removes none.
    Returns (t_sec, s).
    """
    if upsample > 1:
        z = resample_poly(z, int(upsample), 1)
        fs_b = fs_b * int(upsample)
    t = np.arange(len(z), dtype=np.float64) / fs_b
    return t, np.real(z * np.exp(2j * np.pi * float(f_if_hz) * t)).astype(np.float32)


# ═══════════════════════════════════════════════════════════════
#  STAGE 3 — amplitude threshold
# ═══════════════════════════════════════════════════════════════
def envelope_threshold(z, pct, max_probe=4_000_000):
    """
    Percentile of |z| ("keep the peaks above pct% of the data").

    The percentile is estimated from a strided probe of at most `max_probe`
    samples: np.percentile partitions a full float copy, which is ~600 MB on a
    150 M-sample sliver, and a few million samples pin a percentile far tighter
    than anything downstream cares about.
    """
    stride = max(1, len(z) // int(max_probe))
    return float(np.percentile(np.abs(z[::stride]), float(pct)))


def gate_stats(z, thr):
    """(kept fraction of samples, kept fraction of ENERGY) for a |z| >= thr gate."""
    stride = max(1, len(z) // 4_000_000)
    a = np.abs(z[::stride]).astype(np.float64)
    keep = a >= thr
    e_tot = float(np.sum(a ** 2)) + 1e-30
    return float(np.mean(keep)), float(np.sum(a[keep] ** 2) / e_tot)


def _apply_gate(seg, mask_src, thr):
    """Zero every sample whose gating envelope is below thr. Returns a new array."""
    out = seg.copy()
    out[np.abs(mask_src) < thr] = 0
    return out


# ═══════════════════════════════════════════════════════════════
#  STAGE 4 — the global cross-correlation
# ═══════════════════════════════════════════════════════════════
def global_xcorr(z1, z2, fs_b, max_lag_us=1000.0, block_sec=0.1,
                 thr1=None, thr2=None, gate_mode="each", verbose=True):
    """
    Block-sectioned exact global cross-correlation, accumulated coherently AND
    incoherently in one pass. See the module docstring for the maths.

    gate_mode: "each" gates each channel on its own envelope, "ref" gates BOTH on
               ch1's envelope (keeps the two masks identical — useful when the
               reflected chain is too weak to threshold sensibly), "none" skips
               the gate entirely.

    Returns dict with lags_us, incoherent, coherent, and the block bookkeeping.
    """
    n = min(len(z1), len(z2))
    M = max(1, int(round(float(max_lag_us) * 1e-6 * fs_b)))       # lag half-width [samples]
    L = max(1, int(round(float(block_sec) * fs_b)))               # coherent block [samples]

    # A block shorter than the lag range spends most of its FFT on zero padding
    # and correlates only a sliver of overlap at the extreme lags.
    if L < 4 * M:
        L = 4 * M
        if verbose:
            con.note(f"coherent block raised to {L / fs_b * 1e3:.1f} ms so it covers "
                     f"4x the +/-{max_lag_us:g} us lag range")
    if L > n:
        L = n
        M = min(M, max(1, n // 4))

    N = scipy.fft.next_fast_len(L + 2 * M)
    n_blocks = max(1, -(-n // L))     # ceil

    acc_coh = np.zeros(N, dtype=np.complex128)    # Σ_b R_b  (frequency domain)
    acc_inc = np.zeros(N, dtype=np.float64)       # Σ_b |R̂_b|²
    e1_tot = e2_tot = 0.0
    used = 0

    gate = gate_mode != "none" and thr1 is not None

    for b0 in range(0, n, L):
        b1 = min(b0 + L, n)
        blk = b1 - b0

        # y: ch2's block, parked at offset M inside the zero-padded buffer.
        y = np.zeros(N, dtype=np.complex64)
        seg2 = z2[b0:b1]
        if gate:
            seg2 = _apply_gate(seg2, z1[b0:b1] if gate_mode == "ref" else seg2,
                               thr1 if gate_mode == "ref" else thr2)
        y[M:M + blk] = seg2

        # x: ch1 over [b0-M, b0-M+N), clipped to the record and zero-filled.
        x = np.zeros(N, dtype=np.complex64)
        a0 = b0 - M
        src0, src1 = max(a0, 0), min(a0 + N, n)
        seg1 = z1[src0:src1]
        if gate:
            seg1 = _apply_gate(seg1, seg1, thr1)
        x[src0 - a0: src0 - a0 + (src1 - src0)] = seg1

        # Block-local energies for the per-block Cauchy-Schwarz normalisation.
        # (The exact bound's ch1 energy depends on τ; the block-local value is
        # its statistical equal and keeps |R̂| <= 1 to within edge effects.)
        e1 = float(np.sum(np.abs(x[M:M + blk]) ** 2))
        e2 = float(np.sum(np.abs(y[M:M + blk]) ** 2))
        if e1 <= 0.0 or e2 <= 0.0:
            continue                                   # block fully gated out

        X = scipy.fft.fft(x, workers=_FFT_WORKERS)
        Y = scipy.fft.fft(y, workers=_FFT_WORKERS)
        P = X * np.conj(Y)

        acc_coh += P
        R_b = scipy.fft.ifft(P, workers=_FFT_WORKERS)
        acc_inc += (np.abs(R_b) ** 2) / (e1 * e2)      # |R̂_b|², already in [0,1]

        e1_tot += e1
        e2_tot += e2
        used += 1

    if used == 0:
        raise ValueError("[band-xcorr] every block was empty after gating — "
                         "lower threshold_pct.")

    # Unwrap the circular lag axis to -M..+M.
    def _slice(arr):
        return np.concatenate([arr[N - M:], arr[:M + 1]])

    lags = np.concatenate([np.arange(-M, 0), np.arange(0, M + 1)])
    lags_us = lags / fs_b * 1e6

    coh_full = scipy.fft.ifft(acc_coh, workers=_FFT_WORKERS)
    coherent = np.abs(_slice(coh_full)) / (np.sqrt(e1_tot * e2_tot) + 1e-30)
    coh_phase = np.angle(_slice(coh_full))
    incoherent = _slice(acc_inc) / used

    return {
        "lags_us": lags_us, "incoherent": incoherent, "coherent": coherent,
        "coh_phase": coh_phase, "n_blocks": used, "n_blocks_total": n_blocks,
        "block_len": L, "block_sec": L / fs_b, "max_lag_samples": M,
        "nfft": N, "fs_b": fs_b, "n_samples": n,
    }


def peak_metrics(lags_us, mag, fs_b, exclude_us=None):
    """
    Sub-sample peak location + peak-to-sidelobe ratio.

    Parabolic interpolation on the three samples around the maximum (identical to
    sdr_core.xcorr_delay). PSR = peak / median(|R| outside +/-exclude_us of it) —
    a scale-free "is this a real peak" number; > ~3 is a peak you can believe.
    """
    idx = int(np.argmax(mag))
    tau = float(lags_us[idx])
    pk = float(mag[idx])
    if 0 < idx < len(mag) - 1:
        y0, y1, y2 = float(mag[idx - 1]), float(mag[idx]), float(mag[idx + 1])
        den = y0 - 2 * y1 + y2
        if abs(den) > 1e-30:
            d = float(np.clip(0.5 * (y0 - y2) / den, -1.0, 1.0))
            tau = float(lags_us[idx] + d * (lags_us[idx + 1] - lags_us[idx]))
            pk = float(max(y1 - 0.25 * (y0 - y2) * d, 0.0))

    if exclude_us is None:
        exclude_us = max(4.0 / fs_b * 1e6, (lags_us[-1] - lags_us[0]) * 0.002)
    off = np.abs(lags_us - lags_us[idx]) > exclude_us
    floor = float(np.median(mag[off])) if off.any() else float(np.median(mag))
    return {"tau_us": tau, "peak": pk, "psr": pk / (floor + 1e-30),
            "floor": floor, "path_m": tau * 1e-6 * sdr_core.C_LIGHT}


# ═══════════════════════════════════════════════════════════════
#  PLOTTING
# ═══════════════════════════════════════════════════════════════
def _sci_scale(vmax):
    """(divisor, label suffix) so tiny values print as O(1) ticks.

    Correlation levels here run to 1e-5, and matplotlib's automatic answer is a
    detached '1e-5' offset text pinned to the top-left of the axes — exactly
    where the title and the secondary (path-difference) axis label already are,
    so all three overprint. Folding the exponent into the axis LABEL removes the
    offset text entirely.
    """
    if not np.isfinite(vmax) or vmax <= 0:
        return 1.0, ""
    e = int(np.floor(np.log10(vmax)))
    return (1.0, "") if -2 <= e <= 3 else (10.0 ** e, f"  (x 1e{e})")


def _style(ax, title="", ylabel=None, xlabel=None):
    """Recessive chrome, solid hairline grid — the pipeline's panel look."""
    ax.set_facecolor(PANEL)
    if title:
        ax.set_title(title, color=TEXT, fontsize=9.5, pad=6, fontweight="bold")
    ax.tick_params(colors=MUTED, labelsize=8, which="both")
    ax.xaxis.label.set_color(MUTED)
    ax.yaxis.label.set_color(MUTED)
    if ylabel:
        ax.set_ylabel(ylabel)
    if xlabel:
        ax.set_xlabel(xlabel)
    for sp in ax.spines.values():
        sp.set_edgecolor(BORDER)
    ax.grid(True, color=BORDER, linewidth=0.6, alpha=0.9)
    ax.set_axisbelow(True)


def _save(fig, name, save_plots, save_dir):
    if not save_plots:
        plt.close(fig)
        return None
    os.makedirs(save_dir, exist_ok=True)
    path = os.path.join(save_dir, f"{name}.png")
    fig.savefig(path, facecolor=fig.get_facecolor(), dpi=150, bbox_inches="tight")
    con.saved(path)
    plt.close(fig)
    return path


def _peak_hold(x, n_out=MAX_TRACE_POINTS):
    """Block-MAX decimation. A stride would drop short bursts out of the picture
    entirely; peak-hold guarantees every burst survives to the PNG."""
    n = len(x)
    if n <= n_out:
        return np.arange(n, dtype=np.float64), x
    pool = max(1, n // n_out)
    keep = (n // pool) * pool
    return (np.arange(n // pool, dtype=np.float64) + 0.5) * pool, \
        x[:keep].reshape(-1, pool).max(axis=1)


def _block_stats(x, n_out=MAX_TRACE_POINTS):
    """(index, block median, block max) — the pair the envelope panel needs.

    The MEDIAN is what the p50 threshold is comparable to (the gate line lands
    right on it); the MAX is what keeps a short burst from being averaged out of
    existence. Plotting only the max leaves a ~10 dB dead gap down to the
    threshold, which reads as a broken axis rather than as data.
    """
    n = len(x)
    if n <= n_out:
        i = np.arange(n, dtype=np.float64)
        return i, x, x
    pool = max(1, n // n_out)
    keep = (n // pool) * pool
    b = x[:keep].reshape(-1, pool)
    return ((np.arange(len(b), dtype=np.float64) + 0.5) * pool,
            np.median(b, axis=1), b.max(axis=1))


def _block_fraction(a, thr, n_out=MAX_TRACE_POINTS):
    """Fraction of samples above thr, per display block (where the gate keeps data)."""
    n = len(a)
    pool = max(1, n // n_out)
    keep = (n // pool) * pool
    m = (a[:keep] >= thr).reshape(-1, pool)
    return (np.arange(len(m), dtype=np.float64) + 0.5) * pool, m.mean(axis=1)


def plot_waveform_and_threshold(z1, z2, fs_b, thr1, thr2, f_if_hz, info_str,
                                zoom_start_sec=None, zoom_span_us=40.0,
                                save_plots=True, save_dir=".", threshold_pct=50.0):
    """08_00 — full-record envelope + gate occupancy, and a zoomed scope view of
    the reconstructed real waveform with the threshold drawn on it."""
    a1, a2 = np.abs(z1), np.abs(z2)

    fig, (ax_e, ax_f, ax_w) = plt.subplots(
        3, 1, figsize=(14, 10), facecolor=BG,
        gridspec_kw={"height_ratios": [3, 1.5, 3]})
    fig.suptitle(f"Analysis-band waveform & {threshold_pct:g}% amplitude gate\n{info_str}",
                 color=TEXT, fontsize=12, fontweight="bold")

    # ── (a) FULL record: envelope in dB, peak-held ────────────────────────
    lo_db, hi_db = np.inf, -np.inf
    for a, thr, col, lab in ((a1, thr1, C1, "ch1 rx1 RE (direct)"),
                             (a2, thr2, C2, "ch2 rx2 GR (reflected)")):
        i, med, mx = _block_stats(a)
        med_db, mx_db = 20 * np.log10(med + 1e-12), 20 * np.log10(mx + 1e-12)
        ax_e.plot(i / fs_b, mx_db, color=col, lw=0.7, alpha=0.40,
                  label=f"{lab.split()[0]} block max (peak-hold)")
        ax_e.plot(i / fs_b, med_db, color=col, lw=1.0, alpha=0.95,
                  label=f"{lab} — block median |z|")
        ax_e.axhline(20 * np.log10(thr + 1e-12), color=col, lw=1.3, ls=":",
                     alpha=0.95, label=f"{lab.split()[0]} p{threshold_pct:g} threshold")
        lo_db = min(lo_db, float(np.percentile(med_db, 0.5)),
                    20 * np.log10(thr + 1e-12))
        hi_db = max(hi_db, float(np.percentile(mx_db, 99.5)))
    # Robust y-range: the receiver's start/stop transients run tens of dB above
    # the record and would otherwise squash everything into a few pixels. They
    # clip off the top instead.
    ax_e.set_ylim(lo_db - 2.0, hi_db + 5.0)
    _style(ax_e, "FULL RECORD — envelope |z(t)| and the amplitude gate "
                 "(start/stop transients clip off the top)",
           ylabel="|z|  (dB, uncal.)")
    ax_e.legend(fontsize=7, loc="upper right", ncol=3,
                facecolor=PANEL, edgecolor=BORDER)

    # ── (b) where the gate actually keeps data ────────────────────────────
    for a, thr, col, lab in ((a1, thr1, C1, "ch1"), (a2, thr2, C2, "ch2")):
        i, frac = _block_fraction(a, thr)
        ax_f.plot(i / fs_b, frac, color=col, lw=0.9, alpha=0.9, label=lab)
    ax_f.set_ylim(0, 1.02)
    _style(ax_f, "Gate occupancy — fraction of samples kept per display block",
           ylabel="kept fraction", xlabel="Time (s)")
    ax_f.legend(fontsize=7.5, loc="upper right", facecolor=PANEL, edgecolor=BORDER)

    # ── (c) ZOOM: the reconstructed real waveform ─────────────────────────
    # Default location = the strongest burst in the direct channel, so the zoom
    # lands on signal rather than on whatever happens to sit at t=0.
    n_span = max(8, int(round(zoom_span_us * 1e-6 * fs_b)))
    if zoom_start_sec is None:
        # Strongest burst — but the outer 2% is excluded first. The receiver's
        # start/stop transient is by far the largest thing in the record, so a
        # plain argmax parks the zoom on the switch-on glitch every single time
        # instead of on signal.
        i_pk, env = _peak_hold(a1, 2000)
        guard = max(1, int(0.02 * len(env)))
        inner = env[guard:len(env) - guard]
        c = int(i_pk[guard + int(np.argmax(inner))]) if len(inner) else len(z1) // 2
        i0 = int(np.clip(c - n_span // 2, 0, max(0, len(z1) - n_span)))
    else:
        i0 = int(np.clip(round(zoom_start_sec * fs_b), 0, max(0, len(z1) - n_span)))
    i1 = min(i0 + n_span, len(z1))

    t_w, s1 = reconstruct_waveform(z1[i0:i1], fs_b, f_if_hz, ZOOM_UPSAMPLE)
    _, s2 = reconstruct_waveform(z2[i0:i1], fs_b, f_if_hz, ZOOM_UPSAMPLE)
    env1 = np.abs(resample_poly(z1[i0:i1], ZOOM_UPSAMPLE, 1))
    # Time RELATIVE to the zoom start, with the absolute offset in the title.
    # Absolute µs runs into the millions here, and matplotlib then renders the
    # axis as "30 ... 70   +1.3717e6", which is unreadable.
    t_us = t_w * 1e6
    t0_s = i0 / fs_b

    ax_w.plot(t_us, s1, color=C1, lw=1.1, label="ch1 s(t) = Re{z·e^{j2πf_IF t}}")
    ax_w.plot(t_us, s2, color=C2, lw=1.1, alpha=0.85, label="ch2 s(t)")
    ax_w.plot(t_us, env1, color=C1, lw=0.8, ls="--", alpha=0.45, label="ch1 envelope |z|")
    ax_w.plot(t_us, -env1, color=C1, lw=0.8, ls="--", alpha=0.45)
    for sgn in (+1, -1):
        ax_w.axhline(sgn * thr1, color=C_THR, lw=1.0, ls=":", alpha=0.9)
    ax_w.axhline(np.nan, color=C_THR, lw=1.0, ls=":", label="ch1 gate threshold ±")

    # Headroom so the legend never sits on the trace.
    amp = float(max(np.max(np.abs(s1)), np.max(np.abs(s2)), thr1)) * 1.05
    ax_w.set_ylim(-amp, amp * 1.55)

    # Shade the spans the gate REJECTS, so "which peaks survive" is literal.
    # Heavy fragmentation here is itself the result: it means the band is
    # noise-like rather than bursty, and a 50% gate is then just selecting the
    # upper half of a Rayleigh envelope rather than isolating pulses.
    rej = env1 < thr1
    if rej.any():
        ax_w.fill_between(t_us, -amp, amp * 1.55, where=rej, color=MUTED,
                          alpha=0.10, step="mid", lw=0, label="rejected by gate (ch1)")
    _style(ax_w, f"ZOOM at t = {t0_s:.4f} s — reconstructed real waveform at "
                 f"f_IF = {f_if_hz/1e3:.1f} kHz (exact, alias-free rendition of the "
                 f"band; true RF carrier not representable at {fs_b/1e6:.2f} MS/s)",
           ylabel="Amplitude (uncal.)",
           xlabel=f"Time (µs from t = {t0_s:.4f} s)")
    ax_w.legend(fontsize=7.5, loc="upper right", ncol=3,
                facecolor=PANEL, edgecolor=BORDER)

    fig.subplots_adjust(hspace=0.42, top=0.90)
    return _save(fig, "08_00_Band_Waveform_Threshold", save_plots, save_dir), (i0, i1)


def plot_global_xcorr(res, res_ref, m_inc, m_coh, info_str, gate_txt,
                      f_if_hz, save_plots=True, save_dir="."):
    """08_01 — the global correlation: full lag span and a zoom on the peak, for
    both the incoherent (drift-robust) and coherent (phase-preserving) sums."""
    lags = res["lags_us"]
    span = float(lags[-1] - lags[0])
    zoom_us = max(20.0 / res["fs_b"] * 1e6, span * 0.004)

    # Generous top margin and row gap: every panel carries a bold title AND a
    # secondary path-difference axis mounted above it, and the titles are placed
    # manually (see _panel), so the clearance is budgeted here rather than left
    # to matplotlib to discover at draw time.
    fig = plt.figure(figsize=(17, 9.6), facecolor=BG)
    gs = fig.add_gridspec(2, 3, width_ratios=[3, 3, 1.15], hspace=0.62,
                          wspace=0.26, top=0.845, bottom=0.075,
                          left=0.055, right=0.985)
    axes = np.array([[fig.add_subplot(gs[r, c]) for c in range(2)] for r in range(2)])
    ax_txt = fig.add_subplot(gs[:, 2])
    fig.suptitle(f"Global dual-channel cross-correlation — {res['n_blocks']} x "
                 f"{res['block_sec']*1e3:.0f} ms blocks, {res['n_samples']:,} samples/ch\n"
                 f"{info_str}   |   {gate_txt}",
                 color=TEXT, fontsize=12, fontweight="bold")

    def _panel(ax, mag, met, title, ylab, color, zoom, ref=None):
        sc, sfx = _sci_scale(float(np.max(mag)))
        mag, ylab = mag / sc, ylab + sfx
        if ref is not None:
            ax.plot(lags, ref / sc, color=C_REF, lw=0.9, alpha=0.95, zorder=2,
                    label="ungated (reference)")
        ax.plot(lags, mag, color=color, lw=1.2, zorder=3, label="gated")
        ax.axvline(met["tau_us"], color=C_PEAK, lw=1.2, ls="--", zorder=4,
                   label=f"peak  τ = {met['tau_us']:+.4f} µs")
        if zoom:
            lo, hi = met["tau_us"] - zoom_us, met["tau_us"] + zoom_us
            ax.set_xlim(lo, hi)
            sel = (lags >= lo) & (lags <= hi)
            if sel.any():
                # Zoom on the LOCAL range, not forced through 0 — anchoring to zero
                # flattens the very peak shape the zoom exists to show.
                y0, y1 = float(np.min(mag[sel])), float(np.max(mag[sel]))
                pad = max((y1 - y0) * 0.18, y1 * 1e-3, 1e-12)
                ax.set_ylim(y0 - pad, y1 + pad)
        else:
            ax.set_xlim(lags[0], lags[-1])
            ax.set_ylim(0, max(float(np.max(mag)), 1e-12) * 1.10)
        ax.ticklabel_format(axis="y", style="plain", useOffset=False)
        _style(ax, ylabel=ylab,
               xlabel="Lag τ (µs)   —   ch1 delayed w.r.t. ch2 →")
        # Title placed MANUALLY in axes coordinates. set_title() is auto-
        # positioned against the axes' top decorations, and with a
        # secondary_xaxis mounted above, that placement comes out different for
        # each panel (the left column floated ~40 px higher than the right).
        # A fixed axes-fraction offset is identical for every panel by
        # construction, which is what a 2x2 grid needs.
        ax.text(0.5, 1.17, title, transform=ax.transAxes, ha="center",
                va="bottom", color=TEXT, fontsize=9.5, fontweight="bold")
        # Same axis, second unit: path difference. NOT a second measure.
        sec = ax.secondary_xaxis("top", functions=(
            lambda u: u * 1e-6 * sdr_core.C_LIGHT,
            lambda d: d / sdr_core.C_LIGHT * 1e6))
        sec.set_xlabel("Path difference c·τ (m)", color=MUTED, fontsize=8)
        sec.tick_params(colors=MUTED, labelsize=7.5)
        ax.legend(fontsize=7.5, loc="upper right", facecolor=PANEL, edgecolor=BORDER)

    ref_inc = None if res_ref is None else res_ref["incoherent"]
    ref_coh = None if res_ref is None else res_ref["coherent"]
    # Keep the y-labels SHORT. Rotated 90 degrees, a long label overruns the
    # panel height and drags matplotlib's title placement with it (the left
    # column's titles end up floating far above the right column's). The panel
    # title already names the accumulation, so the label only needs the quantity.
    _panel(axes[0, 0], res["incoherent"], m_inc,
           "INCOHERENT Σ|R̂_b|²  —  FULL lag span",
           "mean |γ_b|²", C_XC, zoom=False, ref=ref_inc)
    _panel(axes[0, 1], res["incoherent"], m_inc,
           "INCOHERENT  —  ZOOM on peak", "mean |γ_b|²", C_XC,
           zoom=True, ref=ref_inc)
    _panel(axes[1, 0], res["coherent"], m_coh,
           "COHERENT |ΣR_b|  —  FULL lag span (drift-limited)",
           "|R| / √(E1·E2)", C1, zoom=False, ref=ref_coh)
    # No ungated overlay in the coherent zoom: it shares this panel with the
    # real-waveform fringe trace, and two de-emphasised grey lines read as one.
    _panel(axes[1, 1], res["coherent"], m_coh,
           "COHERENT  —  ZOOM on peak", "|R| / √(E1·E2)", C1, zoom=True)

    # The one place carrier structure belongs: the coherent zoom. The real-waveform
    # correlation is exactly Re{R(τ)·e^{j2πf_IF·τ}} — same envelope, fringed at the
    # IF — so it is derived, not re-computed.
    sel = np.abs(lags - m_coh["tau_us"]) <= zoom_us
    if sel.sum() > 3:
        # Must reuse the panel's own display scale, or the overlay lands on a
        # different axis than the trace it is derived from.
        coh_sc, coh_sfx = _sci_scale(float(np.max(res["coherent"])))
        fr = (res["coherent"][sel] / coh_sc) * np.cos(
            res["coh_phase"][sel] + 2 * np.pi * f_if_hz * lags[sel] * 1e-6)
        # Same quantity, same hue, de-emphasised: this IS the coherent trace,
        # rendered as the real waveform's correlation rather than recomputed.
        axes[1, 1].plot(lags[sel], fr, color=C1, lw=0.8, alpha=0.45, zorder=1,
                        label="same, as real waveform (fringed at f_IF)")
        lim = max(float(np.max(np.abs(fr))), m_coh["peak"] / coh_sc)
        axes[1, 1].set_ylim(-lim * 1.15, lim * 1.55)   # headroom for the legend
        # This panel carries the signed real-waveform trace too, so the axis is
        # no longer a magnitude.
        axes[1, 1].set_ylabel("R / √(E1·E2)" + coh_sfx)
        axes[1, 1].legend(fontsize=7.5, loc="upper right",
                          facecolor=PANEL, edgecolor=BORDER)

    # Metrics get their own column — inside a panel they always land on the data.
    ax_txt.axis("off")
    ratio = (m_coh["peak"] / (np.sqrt(max(m_inc["peak"], 1e-30)) + 1e-30))
    txt = (f"GLOBAL X-CORR\n{'─' * 26}\n"
           f"INCOHERENT  Σ|R̂_b|²\n"
           f"(drift-robust — read this)\n"
           f"  τ     {m_inc['tau_us']:+.4f} µs\n"
           f"  Δd    {m_inc['path_m']:+.1f} m\n"
           f"  peak  {m_inc['peak']:.3e}\n"
           f"  PSR   {m_inc['psr']:.2f}\n\n"
           f"COHERENT  |ΣR_b|\n"
           f"  τ     {m_coh['tau_us']:+.4f} µs\n"
           f"  Δd    {m_coh['path_m']:+.1f} m\n"
           f"  peak  {m_coh['peak']:.3e}\n"
           f"  PSR   {m_coh['psr']:.2f}\n"
           f"  coh/√inc {ratio:.2f}\n\n"
           f"BLOCKS\n"
           f"  {res['n_blocks']} x {res['block_sec']*1e3:.0f} ms\n"
           f"  NFFT {res['nfft']:,}\n"
           f"  lag ±{lags[-1]:.0f} µs\n\n"
           f"SIGN\n"
           f"  τ<0 : ch2 (GR,\n"
           f"        reflected) later\n"
           f"        — expected for a\n"
           f"        ground return\n\n"
           f"RESOLUTION\n"
           f"  ~1/BW; a peak\n"
           f"  narrower than that\n"
           f"  is not resolvable\n\n"
           f"PSR < 3 => no peak;\n"
           f"treat τ as noise.")
    ax_txt.text(0.0, 1.0, txt, transform=ax_txt.transAxes, va="top", ha="left",
                fontsize=7.8, color=TEXT, family="monospace",
                bbox=dict(facecolor=PANEL, edgecolor=BORDER, alpha=0.95,
                          boxstyle="round,pad=0.5"))

    # NOTE: no subplots_adjust / bbox_inches='tight' here — the gridspec above
    # already budgets the title + secondary-axis clearance, and re-solving the
    # layout would undo it.
    return _save(fig, "08_01_Band_Global_XCorr", save_plots, save_dir)


# ═══════════════════════════════════════════════════════════════
#  ENTRY POINT
# ═══════════════════════════════════════════════════════════════
def run_band_correlation(
    json_filepath, center_freq_offset_mhz=0.0, analysis_bandwidth_mhz=1.0,
    percentage=1.0, max_samples=None, threshold_pct=50.0, gate_mode="each",
    dc_notch_khz=5.0, max_lag_us=1000.0, coherent_block_sec=0.1, compare_ungated=True,
    zoom_start_sec=None, zoom_span_us=40.0, save_plots=True,
    # "." (not an absolute /Users/... path) so a direct import behaves the same
    # on any machine/repo location; MAIN.py and the CLI always pass save_dir
    # explicitly and are unaffected. See --save-dir's identical default below.
    save_dir=".",
    verbose=True,
):
    """Stages 1-4 end to end. Returns the results dict; writes 08_00 and 08_01."""
    if verbose:
        con.banner("SIGNAL X-CORR — global correlation of the analysis band")

    z1, z2, fs_b, fc_rf, bw_hz, _meta = extract_sliver(
        json_filepath, center_freq_offset_mhz, analysis_bandwidth_mhz,
        percentage, max_samples)
    z1, _dc1, dc1_db = remove_dc(z1)
    z2, _dc2, dc2_db = remove_dc(z2)
    z1, notch1_pct = notch_dc(z1, fs_b, float(dc_notch_khz) * 1e3)
    z2, notch2_pct = notch_dc(z2, fs_b, float(dc_notch_khz) * 1e3)

    dur = len(z1) / fs_b
    f_if = display_if_hz(bw_hz, fs_b)
    info_str = (f"fc {fc_rf/1e6:.4f} MHz | band {bw_hz/1e3:g} kHz | "
                f"fs_band {fs_b/1e6:.3f} MS/s | {dur:.2f} s")

    if verbose:
        con.kv("Capture", os.path.basename(json_filepath))
        con.kv("Analysis band", f"{fc_rf/1e6:.4f} MHz +/- {bw_hz/2e3:.1f} kHz")
        con.kv("Sliver", f"{len(z1):,} samples/ch at {fs_b/1e6:.3f} MS/s ({dur:.2f} s)")
        con.kv("DC removed", f"ch1 {dc1_db:+.1f} dB, ch2 {dc2_db:+.1f} dB (rel. AC power)")
        if dc_notch_khz and dc_notch_khz > 0:
            con.kv(f"DC notch +/-{dc_notch_khz:g} kHz",
                   f"took {notch1_pct:.1f}% of ch1 / {notch2_pct:.1f}% of ch2 energy "
                   f"({dc_notch_khz*2e3/bw_hz*100:.2f}% of the band)")
            if max(notch1_pct, notch2_pct) > 30.0:
                con.note("that is a LOT of energy for a few kHz — this capture's band is "
                         "dominated by common-mode LO/DC leakage, not by sky signal")
        else:
            con.warn("DC notch disabled — a wandering LO/DC offset is ~99% common-mode "
                     "between the two chains and will dominate the correlation")
        con.kv("Reconstruction IF", f"{f_if/1e3:.1f} kHz (exact, alias-free)")

    # ── threshold ─────────────────────────────────────────────────────────
    thr1 = envelope_threshold(z1, threshold_pct)
    thr2 = envelope_threshold(z2, threshold_pct)
    if gate_mode == "ref":
        thr2 = thr1
    k1, ee1 = gate_stats(z1, thr1)
    k2, ee2 = gate_stats(z2, thr2 if gate_mode != "ref" else thr1)
    if verbose:
        con.say()
        con.say(f"   amplitude gate — keep |z| >= p{threshold_pct:g} "
                f"(mode '{gate_mode}')", "bold")
        con.kv("ch1 threshold", f"{20*np.log10(thr1+1e-12):+.2f} dB -> "
                                f"{k1*100:.1f}% of samples, {ee1*100:.1f}% of energy")
        con.kv("ch2 threshold", f"{20*np.log10(thr2+1e-12):+.2f} dB -> "
                                f"{k2*100:.1f}% of samples, {ee2*100:.1f}% of energy")

    # ── figures: waveform + gate ──────────────────────────────────────────
    if verbose:
        con.say()
        con.say("   rendering figures", "bold")
    _, zoom_idx = plot_waveform_and_threshold(
        z1, z2, fs_b, thr1, thr2, f_if, info_str, zoom_start_sec, zoom_span_us,
        save_plots, save_dir, threshold_pct)

    # ── the correlation ───────────────────────────────────────────────────
    if verbose:
        con.say()
        con.say(f"   correlating (+/-{max_lag_us:g} us = "
                f"+/-{max_lag_us*1e-6*sdr_core.C_LIGHT/1e3:.1f} km of path)", "bold")
    res = global_xcorr(z1, z2, fs_b, max_lag_us, coherent_block_sec,
                       thr1, thr2, gate_mode, verbose)
    res_ref = None
    if compare_ungated and gate_mode != "none":
        res_ref = global_xcorr(z1, z2, fs_b, max_lag_us, coherent_block_sec,
                               None, None, "none", verbose=False)

    m_inc = peak_metrics(res["lags_us"], res["incoherent"], fs_b)
    m_coh = peak_metrics(res["lags_us"], res["coherent"], fs_b)

    gate_txt = (f"gate p{threshold_pct:g} '{gate_mode}' — "
                f"ch1 {k1*100:.0f}%/{ee1*100:.0f}%E, ch2 {k2*100:.0f}%/{ee2*100:.0f}%E")
    plot_global_xcorr(res, res_ref, m_inc, m_coh, info_str, gate_txt, f_if,
                      save_plots, save_dir)

    if verbose:
        con.say()
        con.say("   global correlation peak", "bold")
        con.kv("Blocks combined", f"{res['n_blocks']} x {res['block_sec']*1e3:.1f} ms "
                                  f"(NFFT {res['nfft']:,})")
        con.kv("INCOHERENT  τ", f"{m_inc['tau_us']:+.4f} µs  "
                                f"({m_inc['path_m']:+.1f} m path)", val_style="cyan")
        con.kv("   peak / PSR", f"{m_inc['peak']:.3e} / {m_inc['psr']:.2f}")
        con.kv("COHERENT    τ", f"{m_coh['tau_us']:+.4f} µs  "
                                f"({m_coh['path_m']:+.1f} m path)")
        con.kv("   peak / PSR", f"{m_coh['peak']:.3e} / {m_coh['psr']:.2f}")
        if res_ref is not None:
            r_inc = peak_metrics(res_ref["lags_us"], res_ref["incoherent"], fs_b)
            con.kv("ungated (ref) τ/PSR", f"{r_inc['tau_us']:+.4f} µs / {r_inc['psr']:.2f}")
            better = "sharpened" if m_inc["psr"] > r_inc["psr"] else "did NOT sharpen"
            con.note(f"the {threshold_pct:g}% gate {better} the peak "
                     f"(PSR {r_inc['psr']:.2f} -> {m_inc['psr']:.2f})")
        if m_inc["psr"] < 3.0:
            con.warn(f"PSR {m_inc['psr']:.2f} — no correlation peak stands out of the "
                     f"floor; treat τ as noise, not a delay.")
        elif m_inc["tau_us"] > 0:
            con.note("τ > 0 puts ch1 (RE, direct) LATER than ch2 (GR, reflected); a "
                     "specular ground return should land at τ < 0. Check the cabling "
                     "or read this as a chain-delay offset, not a path.")
        con.note("delay resolution is ~1/BW = "
                 f"{1.0/bw_hz*1e6:.3f} µs ({1.0/bw_hz*sdr_core.C_LIGHT:.0f} m) — this is "
                 "a coarse group-delay anchor, not the carrier-phase observable")

    return {"xcorr": res, "xcorr_ungated": res_ref, "peak_incoherent": m_inc,
            "peak_coherent": m_coh, "fs_band_hz": fs_b, "fc_rf_hz": fc_rf,
            "bw_hz": bw_hz, "f_if_hz": f_if, "thr": (thr1, thr2),
            "kept_frac": (k1, k2), "kept_energy": (ee1, ee2),
            "zoom_idx": zoom_idx, "info": info_str}


# ═══════════════════════════════════════════════════════════════
#  SELF-TEST — known injected delay
# ═══════════════════════════════════════════════════════════════
def _selftest():
    con.banner("band_correlator SELF-TEST (known-truth injected delay)")
    rng = np.random.default_rng(7)
    ok = True

    def check(name, cond, detail=""):
        nonlocal ok
        con.say(f" [{'PASS' if cond else 'FAIL'}] {name}  {detail}",
                *("green",) if cond else ("red",))
        ok &= bool(cond)

    fs = 1e6
    bw = 4e5                                 # fs = 2.5*bw, the ratio bandlimit_and_decimate leaves
    n = int(fs * 6.0)
    d = 37                                   # ch2 (reflected) arrives 37 samples LATER
    pad = 256
    # Bursty band-limited noise: bursts are what an amplitude gate is FOR.
    src = (rng.standard_normal(n + 2 * pad) + 1j * rng.standard_normal(n + 2 * pad)).astype(np.complex64)
    src = sdr_core.lowpass_iq(src, fs, bw / 2.0).astype(np.complex64)
    burst = (np.sin(2 * np.pi * 3.0 * np.arange(n + 2 * pad) / fs) > 0.6).astype(np.float32)
    src *= burst
    nse = lambda: (0.30 * (rng.standard_normal(n) + 1j * rng.standard_normal(n))).astype(np.complex64)
    # s2(k) = 0.5*s1(k-d): the reflected chain sees the source d samples later, so
    # ch1 is ADVANCED w.r.t. ch2 and the peak must land at tau = -d/fs (negative
    # lag — the sign a real specular ground return has, and the branch of the
    # circular lag unwrap most likely to be wrong).
    s1 = (src[pad:pad + n] + nse()).astype(np.complex64)
    s2 = (0.5 * src[pad - d:pad - d + n] + nse()).astype(np.complex64)

    thr1 = envelope_threshold(s1, 50.0)
    thr2 = envelope_threshold(s2, 50.0)
    r = global_xcorr(s1, s2, fs, max_lag_us=200.0, block_sec=0.5,
                     thr1=thr1, thr2=thr2, gate_mode="each", verbose=False)
    m_i = peak_metrics(r["lags_us"], r["incoherent"], fs)
    m_c = peak_metrics(r["lags_us"], r["coherent"], fs)
    truth = -float(d) / fs * 1e6             # ch1 advanced w.r.t. ch2 -> negative lag

    check("incoherent peak finds the injected lag",
          abs(m_i["tau_us"] - truth) < 1.5, f"(got {m_i['tau_us']:+.3f} µs, truth {truth:+.3f})")
    check("coherent peak finds the injected lag",
          abs(m_c["tau_us"] - truth) < 1.5, f"(got {m_c['tau_us']:+.3f} µs, truth {truth:+.3f})")
    check("peak stands clear of the floor (PSR > 5)", m_i["psr"] > 5.0,
          f"(PSR {m_i['psr']:.1f})")
    check("normalisation keeps both accumulations in [0,1]",
          m_i["peak"] <= 1.0 and m_c["peak"] <= 1.0,
          f"(inc {m_i['peak']:.3f}, coh {m_c['peak']:.3f})")

    # Drift: the coherent sum must collapse while the incoherent one survives.
    t = np.arange(n) / fs
    s2d = (s2 * np.exp(2j * np.pi * 0.5 * t)).astype(np.complex64)
    rd = global_xcorr(s1, s2d, fs, max_lag_us=200.0, block_sec=0.5,
                      thr1=None, thr2=None, gate_mode="none", verbose=False)
    di = peak_metrics(rd["lags_us"], rd["incoherent"], fs)
    dc = peak_metrics(rd["lags_us"], rd["coherent"], fs)
    check("incoherent survives a 0.5 Hz inter-chain drift",
          abs(di["tau_us"] - truth) < 1.5 and di["psr"] > 5.0,
          f"(τ {di['tau_us']:+.3f} µs, PSR {di['psr']:.1f})")
    check("coherent is crushed by that drift (the reason both are shown)",
          dc["peak"] < 0.35 * m_c["peak"],
          f"(coh peak {dc['peak']:.3e} vs {m_c['peak']:.3e} undrifted)")

    # The reconstruction must be an EXACT rendition of the band, not a cartoon:
    # the analytic signal of s(t)=Re{z·e^{jwt}} is z·e^{jwt}, so de-mixing the
    # Hilbert transform must return z itself. (This is the property the IF choice
    # in display_if_hz exists to protect — an IF of bw/2 puts the band on DC and
    # breaks it.)
    f_if = display_if_hz(bw, fs)
    m = 8192
    # Band-limited input, as bandlimit_and_decimate always delivers — the whole
    # premise of a real IF rendition is that z occupies only +/-bw/2. (s1 above
    # carries full-Nyquist white noise on purpose, and would alias here.)
    zb = sdr_core.lowpass_iq(s1[:m], fs, bw / 2.0).astype(np.complex64)
    _, w = reconstruct_waveform(zb, fs, f_if)
    z_rec = (hilbert(np.asarray(w, dtype=np.float64))
             * np.exp(-2j * np.pi * f_if * np.arange(m) / fs))
    g = slice(600, m - 600)                        # drop the Hilbert FFT edge wrap
    err = float(np.mean(np.abs(z_rec[g] - zb[g]) ** 2)
                / (np.mean(np.abs(zb[g]) ** 2) + 1e-30))
    check("waveform reconstruction is invertible (analytic re-mix < -20 dB)",
          err < 1e-2, f"({10*np.log10(err+1e-30):.1f} dB, IF {f_if/1e3:.0f} kHz)")

    # The DC notch must kill a shared LO-leak smear WITHOUT moving the delay —
    # this is the failure mode seen on the real 1621 MHz capture, where the leak
    # carried |gamma| ~ 0.99 and buried the peak under a broad pedestal.
    leak = (2.0 * np.exp(2j * np.pi * 300.0 * np.arange(n) / fs)).astype(np.complex64)
    l1 = (s1 + leak).astype(np.complex64)
    l2 = (s2 + leak).astype(np.complex64)     # common-mode: identical in both chains
    rl = global_xcorr(l1, l2, fs, max_lag_us=200.0, block_sec=0.5,
                      gate_mode="none", verbose=False)
    ml = peak_metrics(rl["lags_us"], rl["incoherent"], fs)
    # The observed failure mode is a BURIED peak, not a displaced one: the leak's
    # long correlation time raises a broad pedestal that swamps the sidelobe floor.
    check("LO-leak reproduces the artefact (peak buried under the pedestal)",
          ml["psr"] < 0.2 * m_i["psr"],
          f"(PSR {ml['psr']:.1f} vs {m_i['psr']:.1f} clean)")

    l1n, rem1 = notch_dc(remove_dc(l1)[0], fs, 5e3)
    l2n, _ = notch_dc(remove_dc(l2)[0], fs, 5e3)
    rn = global_xcorr(l1n, l2n, fs, max_lag_us=200.0, block_sec=0.5,
                      gate_mode="none", verbose=False)
    mn = peak_metrics(rn["lags_us"], rn["incoherent"], fs)
    check("DC notch digs the peak back out of the leak",
          abs(mn["tau_us"] - truth) < 1.5 and mn["psr"] > 0.5 * m_i["psr"],
          f"(τ {mn['tau_us']:+.3f} µs, PSR {ml['psr']:.1f} -> {mn['psr']:.1f}, "
          f"notch took {rem1:.0f}% energy)")

    con.rule("─", "cyan")
    con.say(" OVERALL: " + ("PASS ✅" if ok else "FAIL ❌"), *("green",) if ok else ("red",))
    return 0 if ok else 1


def _cli():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    p.add_argument("json_path", nargs="?", help="capture .json (with matching .bin)")
    p.add_argument("--selftest", action="store_true")
    p.add_argument("--offset", type=float, default=0.0, help="centre freq offset [MHz]")
    p.add_argument("--bw", type=float, default=1.0, help="analysis bandwidth [MHz]")
    p.add_argument("--pct", type=float, default=100.0, help="percentage of capture")
    p.add_argument("--threshold", type=float, default=50.0, help="amplitude percentile")
    p.add_argument("--max-lag-us", type=float, default=1000.0)
    p.add_argument("--block-sec", type=float, default=0.1)
    p.add_argument("--save-dir", default=".")
    a = p.parse_args()

    if a.selftest:
        sys.exit(_selftest())
    if not a.json_path:
        p.error("provide a capture .json or --selftest")
    run_band_correlation(a.json_path, center_freq_offset_mhz=a.offset,
                         analysis_bandwidth_mhz=a.bw, percentage=a.pct / 100.0,
                         threshold_pct=a.threshold, max_lag_us=a.max_lag_us,
                         coherent_block_sec=a.block_sec, save_dir=a.save_dir)


if __name__ == "__main__":
    _cli()
