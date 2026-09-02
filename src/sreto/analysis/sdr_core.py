"""
sdr_core.py — shared DSP primitives for the dual-channel bistatic reflectometry pipeline.

Every mathematically sensitive operation lives here exactly once, so a fix in this
file propagates to the waterfall generators, the IQ dashboard, and the physics
extraction identically.

Conventions used throughout the pipeline
----------------------------------------
* Dual-channel bladeRF capture, interleaved int16 (SC16Q11) or int8 (SC8):
  one frame = [ch1_I, ch1_Q, ch2_I, ch2_Q].
* ch1 = rx1 = "RE" (reference / direct antenna), ch2 = rx2 = "GR" (ground-reflected),
  unless overridden by the caller.
* Inter-channel product is always  z = s1 * conj(s2),  so
      angle(z) = phase(ch1) - phase(ch2).
* WRAPPED PHASE RULE: never average or smooth raw angles (a boxcar over the
  ±π seam averages +3.1 rad and -3.1 rad to 0 instead of π). Always average
  the COMPLEX product and take the angle afterwards (circular mean):
  use smooth_complex() / block_coherence() below.

VENDORED COPY — byte-identical to 01_CODE/sdr_core.py in the sdr_r science
repo (this module has no local imports, so nothing needed to change). See
sreto.analysis's package docstring (__init__.py) for the sync story.
"""

import json
import os

import numpy as np
import scipy.fft
from scipy import signal
from scipy.ndimage import uniform_filter1d

C_LIGHT = 299_792_458.0  # speed of light [m/s]

# scipy.fft's multi-threaded FFT is ~25x faster than np.fft at this pipeline's
# batch sizes (measured: 20000x2048 complex64 FFT, 0.617s np.fft vs 0.024s
# scipy.fft workers=-1, on an 8-core M2) -- GPU/MPS was also benchmarked and
# lost to even single-threaded CPU here (dispatch overhead dominates at these
# per-FFT sizes), so this stays a CPU-only optimization.
_FFT_WORKERS = -1


# ═══════════════════════════════════════════════════════════════
#  METADATA + LOADING
# ═══════════════════════════════════════════════════════════════

def get_meta(meta, keys, default, cast=float):
    """Case-insensitive metadata lookup with fallback default."""
    lower_map = {str(k).lower(): k for k in meta.keys()}
    for k in keys:
        if k.lower() in lower_map:
            try:
                return cast(meta[lower_map[k.lower()]])
            except (TypeError, ValueError):
                pass
    return default


def resolve_paths(input_path):
    """Accept either the .json or the .bin path; return (json_path, bin_path)."""
    root, ext = os.path.splitext(input_path)
    json_path, bin_path = root + ".json", root + ".bin"
    if not os.path.exists(bin_path):
        raise FileNotFoundError(f"Missing binary capture: {bin_path}")
    if not os.path.exists(json_path):
        raise FileNotFoundError(f"Missing metadata JSON: {json_path}")
    return json_path, bin_path


# ── in-process IQ cache ─────────────────────────────────────────────
# MAIN.py runs THREE stages over the SAME capture (power waterfalls, phase
# waterfalls, IQ dashboard) and each used to re-read and re-decode the whole
# file from disk. One entry is kept here; a request for a SUBSET of what is
# already loaded is served as a VIEW — zero disk I/O, zero copy.
#
# Safe because nothing downstream mutates the loaded arrays in place: every
# DSP primitive (digital_mix, bandlimit_and_decimate, spectral_waterfall, ...)
# allocates its own output. Do NOT start writing into ch1/ch2 without calling
# clear_iq_cache() or copying first.
_IQ_CACHE = {"key": None, "n": 0, "ch1": None, "ch2": None}

# Raw int16/int8 values read per chunk (~128 MB at int16) — keeps the decode
# buffer small without making the read syscall-bound.
_LOAD_CHUNK_VALUES = 64_000_000


def total_ram_bytes(default=8 * 1024 ** 3):
    """Physical RAM, for sizing the caches. Falls back to `default`."""
    try:
        return os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
    except (ValueError, OSError, AttributeError):
        return default


# The IQ cache saves re-reading the capture for every pipeline stage, but the
# arrays are huge (8 B/sample/channel) and pinning them for the whole run is
# exactly how the process gets OOM-killed while the waterfall stage allocates.
# Above this budget the cache is SKIPPED, so each stage frees its copy as it
# used to: a slower run always beats a killed one.
_IQ_CACHE_MAX_BYTES = int(0.25 * total_ram_bytes())

# Hard ceiling on waterfall rows. A PNG is ~1-2k pixels tall, so tens of
# thousands of rows cost gigabytes to produce something the renderer just
# downsamples anyway. avg_frames is raised automatically to respect this —
# still AVERAGING (every sample used, noise down ~sqrt(N)), never striding.
MAX_WATERFALL_ROWS = 4000


def clear_iq_cache():
    """Release the cached capture (~8 bytes per sample per channel)."""
    _IQ_CACHE.update(key=None, n=0, ch1=None, ch2=None)


def _read_iq_chunked(bin_path, dtype, vals_per_frame, n_target, is_dual):
    """
    Decode `n_target` interleaved frames into complex64 channel arrays, reading
    the file in CHUNKS so the raw int block is never materialised whole.

    Reading it in one go costs an extra bytes_per_frame*n_target of peak RAM on
    top of the complex64 outputs — for a 2.9 GB dual capture that is ~8.6 GB
    peak instead of ~5.8 GB, i.e. the difference between fitting in memory and
    swapping. Swapping is what made large files feel "extremely slow" (measured:
    284 s to load a full 2.9 GB capture that way, vs 11 s chunked).
    """
    ch1 = np.empty(n_target, dtype=np.complex64)
    ch2 = np.empty(n_target, dtype=np.complex64) if is_dual else None

    chunk_frames = max(1, int(_LOAD_CHUNK_VALUES // vals_per_frame))
    done = 0
    with open(bin_path, "rb") as fh:
        while done < n_target:
            want = min(chunk_frames, n_target - done)
            buf = np.fromfile(fh, dtype=dtype, count=want * vals_per_frame)
            got = buf.size // vals_per_frame
            if got == 0:
                break                      # file shorter than its size implied
            fr = buf[: got * vals_per_frame].reshape(-1, vals_per_frame)
            sl = slice(done, done + got)
            ch1.real[sl], ch1.imag[sl] = fr[:, 0], fr[:, 1]
            if is_dual:
                ch2.real[sl], ch2.imag[sl] = fr[:, 2], fr[:, 3]
            done += got

    if done < n_target:                    # truncate to what actually existed
        ch1 = ch1[:done]
        ch2 = None if ch2 is None else ch2[:done]
    return ch1, ch2


def load_dual_iq(input_path, max_samples=None, percentage=1.0, use_cache=True):
    """
    Load an interleaved dual-channel capture.

    Returns (ch1, ch2, fs_hz, fc_hz, bw_hz, meta, is_dual). ch2 is None for
    single-channel captures. Arrays are complex64.

    Respects the JSON `bitmode` field (16bit -> int16 SC16Q11, 8bit -> int8 SC8);
    the previous loaders hard-coded int16 and would have parsed 8-bit captures
    as garbage.

    PERFORMANCE: only the frames actually requested are read off disk
    (np.fromfile(count=...)). This used to slurp the ENTIRE file and throw most
    of it away afterwards, which made `percentage`/`max_samples` cost full price
    — on a 2.9 GB capture, a 10% request took 18 s instead of 1 s. Repeat loads
    of the same file are served from the in-process cache (see _IQ_CACHE).
    """
    json_path, bin_path = resolve_paths(input_path)
    with open(json_path) as f:
        meta = json.load(f)

    fs_hz = get_meta(meta, ["samplerate_MHz", "fs_mhz"], 2.0) * 1e6
    fc_hz = get_meta(meta, ["frequency_MHz", "center_freq_mhz", "freq_mhz"], 433.0) * 1e6
    bw_hz = get_meta(meta, ["bandwidth_MHz", "bw_mhz"], 2.0) * 1e6
    channels = str(get_meta(meta, ["channels"], "1", cast=str))
    is_dual = "2" in channels
    bitmode = str(get_meta(meta, ["bitmode"], "16bit", cast=str)).lower()
    dtype = np.int8 if "8" in bitmode else np.int16

    vals_per_frame = 4 if is_dual else 2
    itemsize = np.dtype(dtype).itemsize

    # Frame count comes from the file SIZE, so we never read to find out.
    total_vals = os.path.getsize(bin_path) // itemsize
    n_frames = total_vals // vals_per_frame
    if total_vals % vals_per_frame:
        print(f"[load] WARNING: {total_vals % vals_per_frame} trailing values "
              f"dropped (incomplete frame — capture may have ended early).")

    n_target = int(n_frames * percentage)
    if max_samples is not None and n_target > max_samples:
        print(f"[load] NOTE: truncating {n_target:,} -> {max_samples:,} frames "
              f"(max_samples cap).")
        n_target = int(max_samples)
    n_target = max(0, min(n_target, n_frames))

    key = (os.path.abspath(bin_path), os.path.getmtime(bin_path),
           np.dtype(dtype).str, vals_per_frame)
    if use_cache and _IQ_CACHE["key"] == key and _IQ_CACHE["n"] >= n_target:
        c1, c2 = _IQ_CACHE["ch1"], _IQ_CACHE["ch2"]
        print(f"[load] cache hit: {n_target:,} of {_IQ_CACHE['n']:,} cached "
              f"frames (no disk read)")
        return (c1[:n_target], None if c2 is None else c2[:n_target],
                fs_hz, fc_hz, bw_hz, meta, is_dual)

    ch1, ch2 = _read_iq_chunked(bin_path, dtype, vals_per_frame, n_target, is_dual)

    nbytes = ch1.nbytes + (0 if ch2 is None else ch2.nbytes)
    if use_cache and nbytes <= _IQ_CACHE_MAX_BYTES:
        _IQ_CACHE.update(key=key, n=len(ch1), ch1=ch1, ch2=ch2)
    elif use_cache:
        clear_iq_cache()   # too big to pin: let each stage free its own copy
        print(f"[load] NOTE: {nbytes/1e9:.2f} GB of IQ exceeds the "
              f"{_IQ_CACHE_MAX_BYTES/1e9:.2f} GB cache budget — not caching "
              f"(stages will re-read; this protects against OOM).")

    return ch1, ch2, fs_hz, fc_hz, bw_hz, meta, is_dual


# ═══════════════════════════════════════════════════════════════
#  SPECTRAL WATERFALL (Welch-style, look-averaged)
# ═══════════════════════════════════════════════════════════════

def spectral_waterfall(ch1, fs_hz, nperseg=2048, avg_frames=1, ch2=None,
                       chunk_frames=4096, max_rows=MAX_WATERFALL_ROWS):
    """
    Spectral waterfall with non-coherent look averaging (Welch in time).

    The signal is cut into consecutive non-overlapping `nperseg`-sample frames
    (Hann window). EVERY frame is FFT'd, and `avg_frames` consecutive frames
    are averaged into one output row:

        P(row,f) = <|S|²>_avg / (fs·Σw²)      one-channel PSD (density, V²/Hz)
        C(row,f) = <S1·S2*>_avg / (fs·Σw²)    complex cross-spectrum (dual ch.)

    `avg_frames` is the correct way to shrink a waterfall: it reduces the row
    count by N while KEEPING all samples, cutting the per-pixel noise std by
    ~sqrt(N) (radiometric looks). A frame STRIDE (the old dec_factor) discards
    (N-1)/N of the data and keeps single-look noise — the trend gets NOISIER
    relative to what averaging gives, never cleaner. No spectral aliasing is
    possible either way: samples inside each FFT stay at the full rate.

    The complex product is averaged BEFORE any magnitude is taken, so
    angle(C) is the multi-look interferometric phase and
    |C|/sqrt(P1·P2) is the multi-look coherence (circular statistics).

    Processed in chunks of `chunk_frames` frames so a 60 s @ 2 MS/s dual
    capture never materializes the full STFT (~chunk_frames·nperseg·8 B peak
    per channel).

    Returns dict:
      f_hz  (nperseg,)        fftshifted baseband frequency axis [Hz]
      t_sec (n_rows,)         center time of each averaged row [s]
      P1    (n_rows,nperseg)  ch1 PSD, fftshifted, float32
      phi1  (n_rows,nperseg)  SINGLE-look phase of the first frame in each row
      P2, phi2, C             same for ch2 / cross-spectrum (only when ch2 given)
    """
    avg = max(1, int(avg_frames))
    nperseg = int(nperseg)
    dual = ch2 is not None
    n = min(len(ch1), len(ch2)) if dual else len(ch1)
    n_frames = n // nperseg
    n_rows = n_frames // avg

    # MEMORY GUARD: the output arrays are n_rows x nperseg and there are five of
    # them when dual (P1, phi1, P2, phi2 float32 + C complex64) = 24 B per cell.
    # A long capture with a small avg_frames therefore asks for gigabytes to
    # draw an image only ~1-2k pixels tall — that is what got the process
    # OOM-killed. Raise avg instead: still AVERAGING (every sample contributes,
    # noise down ~sqrt(avg)), never striding, so nothing is discarded.
    if max_rows and n_rows > int(max_rows):
        per_row = nperseg * (24 if dual else 8)
        avg = -(-n_frames // int(max_rows))          # ceil division
        new_rows = n_frames // avg
        print(f"[waterfall] avg_frames {int(avg_frames)} -> {avg}: "
              f"{n_rows:,} rows would need "
              f"{n_rows * per_row / 1e9:.2f} GB; capped at {new_rows:,} rows "
              f"({new_rows * per_row / 1e9:.2f} GB). All samples still averaged "
              f"in — raise sdr_core.MAX_WATERFALL_ROWS if you truly need more.")
        n_rows = new_rows

    if n_rows == 0:
        raise ValueError(f"Not enough data for one averaged row "
                         f"({n} samples < nperseg*avg = {nperseg * avg}).")

    win = np.hanning(nperseg).astype(np.float32)
    psd_scale = np.float32(fs_hz * np.sum(win.astype(np.float64) ** 2))

    P1 = np.empty((n_rows, nperseg), dtype=np.float32)
    phi1 = np.empty((n_rows, nperseg), dtype=np.float32)
    if dual:
        P2 = np.empty_like(P1)
        phi2 = np.empty_like(P1)
        C = np.empty((n_rows, nperseg), dtype=np.complex64)

    rows_per_chunk = max(1, int(chunk_frames) // avg)
    for r0 in range(0, n_rows, rows_per_chunk):
        r1 = min(r0 + rows_per_chunk, n_rows)
        a, b = r0 * avg * nperseg, r1 * avg * nperseg

        S1 = scipy.fft.fft(ch1[a:b].reshape(-1, nperseg) * win,
                           axis=1, workers=_FFT_WORKERS)
        S1 = np.fft.fftshift(S1, axes=1)
        P1[r0:r1] = (np.abs(S1) ** 2).reshape(r1 - r0, avg, nperseg).mean(axis=1) / psd_scale
        phi1[r0:r1] = np.angle(S1[::avg])

        if dual:
            S2 = scipy.fft.fft(ch2[a:b].reshape(-1, nperseg) * win,
                               axis=1, workers=_FFT_WORKERS)
            S2 = np.fft.fftshift(S2, axes=1)
            P2[r0:r1] = (np.abs(S2) ** 2).reshape(r1 - r0, avg, nperseg).mean(axis=1) / psd_scale
            phi2[r0:r1] = np.angle(S2[::avg])
            C[r0:r1] = (S1 * np.conj(S2)).reshape(r1 - r0, avg, nperseg).mean(axis=1) / psd_scale

    out = {
        "f_hz": np.fft.fftshift(np.fft.fftfreq(nperseg, 1.0 / fs_hz)),
        "t_sec": (np.arange(n_rows) + 0.5) * avg * nperseg / fs_hz,
        "P1": P1, "phi1": phi1,
    }
    if dual:
        out.update(P2=P2, phi2=phi2, C=C)
    return out


# ═══════════════════════════════════════════════════════════════
#  MIXING / FILTERING / DECIMATION
# ═══════════════════════════════════════════════════════════════

def digital_mix(ch, fs_hz, offset_hz, chunk=4_000_000):
    """
    Frequency-shift `ch` by -offset_hz (moves a tone at +offset_hz down to DC).

    The mixer phase 2π·f_off·t reaches ~1e7 rad over a 10 s capture, so it must
    be accumulated in float64; float32 phase would carry radian-level error.
    Processed in chunks to keep peak memory at complex64 levels.
    """
    if offset_hz == 0.0:
        return ch
    out = np.empty(len(ch), dtype=np.complex64)
    w = -2j * np.pi * (offset_hz / fs_hz)
    for i0 in range(0, len(ch), chunk):
        i1 = min(i0 + chunk, len(ch))
        n = np.arange(i0, i1, dtype=np.float64)
        out[i0:i1] = ch[i0:i1] * np.exp(w * n)
    return out


def lowpass_iq(ch, fs_hz, cutoff_hz, order=4):
    """Zero-phase Butterworth low-pass on complex baseband."""
    nyq = fs_hz / 2.0
    Wn = min(max(cutoff_hz / nyq, 1e-5), 0.99)
    b, a = signal.butter(order, Wn, btype="low")
    if len(ch) <= 3 * max(len(a), len(b)):
        return signal.lfilter(b, a, ch)
    return signal.filtfilt(b, a, ch)


def bandlimit_and_decimate(ch, fs_hz, bw_hz):
    """
    Isolate a band of width bw_hz centred at DC and decimate.

    Two stages: anti-aliased polyphase decimation to ~5x the band, then a
    Butterworth at bw/2 with the cutoff kept at <= 0.8x the final Nyquist
    (the old chain put the cutoff exactly AT the post-decimation Nyquist,
    leaving the transition band to alias).

    Returns (ch_out, fs_out).
    """
    d1 = max(1, int(fs_hz // (5.0 * bw_hz)))
    if d1 > 1:
        ch = signal.resample_poly(ch, 1, d1).astype(np.complex64)
    fs1 = fs_hz / d1

    ch = lowpass_iq(ch, fs1, bw_hz / 2.0)

    d2 = max(1, int(fs1 // (2.5 * bw_hz)))  # final fs >= 2.5*bw -> cutoff <= 0.8*Nyquist
    if d2 > 1:
        ch = ch[::d2]  # spectrum already confined to ±bw/2, plain stride is alias-free
    return ch.astype(np.complex64), fs1 / d2


# ═══════════════════════════════════════════════════════════════
#  PHASE / COHERENCE (the core interferometric estimators)
# ═══════════════════════════════════════════════════════════════

def smooth_complex(z, n):
    """
    Boxcar-smooth a complex series (circular mean when applied to a phasor).

    This is THE correct way to smooth phase: smooth the complex product,
    then take angle(). O(N) via uniform_filter1d, replacing the old
    O(N·kernel) np.convolve on wrapped angles.
    """
    if n <= 1 or len(z) == 0:
        return z
    n = min(int(n), len(z))
    out = np.empty(len(z), dtype=np.complex64)
    out.real = uniform_filter1d(z.real.astype(np.float32), n, mode="nearest")
    out.imag = uniform_filter1d(z.imag.astype(np.float32), n, mode="nearest")
    return out


def coherence_and_phase(s1, s2):
    """
    Single-window complex coherence  γ = Σ s1·s2* / sqrt(Σ|s1|²·Σ|s2|²).

    Returns (|γ|, angle(γ)):  |γ| ∈ [0,1] is the interferometric quality,
    angle(γ) the maximum-likelihood wrapped phase difference ch1-ch2.
    (Replaces estimate_phase_delay's sum(prod)/sum(|prod|), whose denominator
    was real-positive and therefore never changed the angle.)
    """
    num = np.sum(s1 * np.conj(s2))
    den = np.sqrt(np.sum(np.abs(s1) ** 2) * np.sum(np.abs(s2) ** 2)) + 1e-30
    gamma = num / den
    return float(np.abs(gamma)), float(np.angle(gamma))


def block_coherence(s1, s2, fs_hz, block_sec):
    """
    Block-wise complex coherence time series.

    Returns (t_k, gamma_k, P1_k, P2_k):
      t_k      block-centre times [s]
      gamma_k  complex coherence per block; |γ| = quality, angle = wrapped Δφ
      P1_k/P2_k mean power per block (for the reflectivity ratio)
    """
    n_blk = max(1, int(round(block_sec * fs_hz)))
    n_blocks = min(len(s1), len(s2)) // n_blk
    if n_blocks == 0:
        raise ValueError(f"Capture shorter than one {block_sec}s block.")
    a = s1[: n_blocks * n_blk].reshape(n_blocks, n_blk)
    b = s2[: n_blocks * n_blk].reshape(n_blocks, n_blk)

    num = np.sum(a * np.conj(b), axis=1)
    e1 = np.sum(np.abs(a) ** 2, axis=1)
    e2 = np.sum(np.abs(b) ** 2, axis=1)
    gamma = num / (np.sqrt(e1 * e2) + 1e-30)

    t_k = (np.arange(n_blocks) + 0.5) * n_blk / fs_hz
    return t_k, gamma.astype(np.complex128), e1 / n_blk, e2 / n_blk


def unwrap_masked(phase_wrapped, valid_mask):
    """
    np.unwrap over the valid samples only; invalid entries become NaN.

    Low-coherence blocks carry uniformly random phase — feeding them to
    np.unwrap would inject spurious 2π jumps into the good data on both sides.
    Unwrapping across a masked gap assumes the true phase moved < π during the
    gap (documented limitation).
    """
    out = np.full(len(phase_wrapped), np.nan)
    if np.any(valid_mask):
        out[valid_mask] = np.unwrap(phase_wrapped[valid_mask])
    return out


def phase_sigma_crlb(coherence, n_eff):
    """
    Cramér–Rao bound of the interferometric phase estimate [rad]:
        σ_φ = sqrt( (1-γ²) / (2·N_eff·γ²) )
    with N_eff independent samples (≈ analysis_bandwidth × block_length).
    Standard InSAR result (Rodríguez & Martin 1992).
    """
    g = np.clip(np.asarray(coherence, dtype=float), 1e-6, 1.0)
    n = max(float(n_eff), 1.0)
    return np.sqrt((1.0 - g ** 2) / (2.0 * n * g ** 2))


# ═══════════════════════════════════════════════════════════════
#  CROSS-CORRELATION (group delay)
# ═══════════════════════════════════════════════════════════════

def complex_xcorr(s1, s2, fs_hz):
    """
    Normalized linear cross-correlation via zero-padded FFT.

    Positive lag = ch1 delayed relative to ch2. Peak magnitude is bounded by 1
    for identical aligned signals. Means are removed first (kills DC, not
    off-DC common-mode tones — the caller must band-limit; see xcorr_delay).
    Returns (lags_us, |xcorr| normalized).
    """
    s1 = np.asarray(s1) - np.mean(s1)
    s2 = np.asarray(s2) - np.mean(s2)
    n = len(s1) + len(s2) - 1
    nfft = int(2 ** np.ceil(np.log2(max(n, 2))))
    xc = scipy.fft.ifft(
        scipy.fft.fft(s1, n=nfft, workers=_FFT_WORKERS)
        * np.conj(scipy.fft.fft(s2, n=nfft, workers=_FFT_WORKERS)),
        workers=_FFT_WORKERS)[:n]
    xc = np.concatenate([xc[len(s1) - 1:], xc[: len(s1) - 1]])
    lags_us = (np.arange(len(xc)) - (len(s1) - 1)) / fs_hz * 1e6
    norm = np.sqrt(np.sum(np.abs(s1) ** 2) * np.sum(np.abs(s2) ** 2)) + 1e-30
    return lags_us, np.abs(xc) / norm


def xcorr_delay(s1, s2, fs_hz, max_samples=2_000_000):
    """
    Group-delay estimate: xcorr peak + parabolic sub-sample interpolation.

    Honors the full requested window (the old code silently truncated every
    request to 1000 samples). `max_samples` is only a memory guard and warns
    when it engages. Delay resolution is fundamentally ~1/bandwidth — at 2 MHz
    that is ~0.5 µs (150 m of path), so treat this as a coarse anchor next to
    the carrier-phase observable.

    Returns (tau_us, peak_mag, lags_us, mag).
    """
    n = min(len(s1), len(s2))
    if max_samples is not None and n > max_samples:
        print(f"[xcorr] NOTE: window capped {n:,} -> {max_samples:,} samples "
              f"(memory guard; raise max_samples to override).")
        n = int(max_samples)
    lags, mag = complex_xcorr(s1[:n], s2[:n], fs_hz)

    idx = int(np.argmax(mag))
    if idx <= 0 or idx >= len(mag) - 1:
        return lags[idx], float(mag[idx]), lags, mag
    y0, y1, y2 = mag[idx - 1], mag[idx], mag[idx + 1]
    denom = y0 - 2 * y1 + y2
    if abs(denom) < 1e-30:
        return lags[idx], float(mag[idx]), lags, mag
    delta = np.clip(0.5 * (y0 - y2) / denom, -1.0, 1.0)
    tau = lags[idx] + delta * (lags[idx + 1] - lags[idx])
    peak = float(np.clip(y1 - 0.25 * (y0 - y2) * delta, 0.0, 1.0))
    return tau, peak, lags, mag
