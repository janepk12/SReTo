"""
physics.py — semantic parameter extraction for the dual-channel
bistatic reflectometry pipeline: soil moisture + relative altimetry.

Signal model (fixed terrestrial transmitter, ch "direct" = reference antenna,
ch "reflected" = ground-looking antenna, shared-LO receiver):

    z_d(t) = g_d · a(t)                       + n_d(t)
    z_r(t) = g_r · r · a(t) · e^{-jφ_geo(t)}  + n_r(t)

    φ_geo = (2π/λ)·ΔR = 4π·h·sin(θe)/λ        (ΔR = 2h·sinθe extra path)
    r     = Fresnel reflection coefficient of the surface
    g_d,r = complex instrumental gains (cables, LNAs, per-retune LO phase)

Core observable — block complex coherence (sdr_core.block_coherence):

    γ_k = Σ z_d·z_r* / sqrt(Σ|z_d|²·Σ|z_r|²)

    arg γ_k = φ_geo − arg r − φ_inst     (wrapped; unwrap over blocks)
    P_r/P_d = |r|²·|g_r/g_d|²

Retrieval chains
----------------
ALTIMETRY:      Δh(t) = λ·[unwrap(arg γ − φ_cal)]/(4π·sinθe)
                φ_cal from a reference capture (metal plate, same geometry) or a
                constant; the reflection phase (π for H-pol, real ε_r) cancels
                between reference and soil, leaving pure geometry.
SOIL MOISTURE:  Γ = (P_r/P_d)·C_cal = |r|²        (C_cal from plate: |r|=1)
                |r| → ε_r  by Fresnel inversion (H-pol closed form / V-pol brentq)
                ε_r → θ_v  by the Topp et al. (1980) polynomial.

Uncertainty:    σ_φ = sqrt((1−γ²)/(2·N_eff·γ²))  (interferometric CRLB),
                N_eff ≈ analysis_bandwidth × block_length;
                σ_Γ/Γ ≈ sqrt(2/N_eff);  propagated numerically through the chain.
Quality gate:   blocks with |γ| < coherence_threshold → NaN everywhere.

Run `python physics.py --selftest` for an end-to-end validation of every
formula against synthetic data with known ground truth.

VENDORED COPY — see the sreto.analysis package docstring (__init__.py) for what
that means and how this file is kept in sync with 01_CODE/physics.py in the
sdr_r science repo. Exactly ONE line differs from the sdr_r original: the
`sdr_core` import below is package-relative here.

    python -m sreto.analysis.physics --selftest     (the vendored entry point)
"""

import argparse
import csv
import json
import os
import re
import sys
from dataclasses import dataclass, asdict
from datetime import datetime
from typing import Optional

import numpy as np
import matplotlib as mpl
mpl.use("Agg")            # figures are written to disk, never shown: this runs
                          # under the GUI as a subprocess and in headless CI,
                          # where the default macosx backend needs a window
                          # server and dies. Must precede the pyplot import.
import matplotlib.pyplot as plt                                   # noqa: E402
from scipy.optimize import brentq                                 # noqa: E402

from . import sdr_core          # sdr_r: `import sdr_core`

# House palette (matches waterfalls / iq_dashboard)
BG, PANEL, BORDER, TEXT, MUTED = "#f8f9fa", "#ffffff", "#dee2e6", "#212529", "#6c757d"
C_PRIMARY = "#1f77b4"   # main observable per panel
C_SECONDARY = "#ff7f0e"  # secondary series (distinct dash + direct label)
C_STATUS = "#d62728"    # reserved: thresholds / warnings only, never a series
CMAP_TIME = "plasma"
DEG = np.pi / 180.0


# ═══════════════════════════════════════════════════════════════
#  CONFIG
# ═══════════════════════════════════════════════════════════════

@dataclass
class PhysicsConfig:
    # --- geometry (MUST be set from the real antenna setup) ---
    elevation_deg: Optional[float] = None   # transmitter elevation at the specular point
    polarization: str = "H"                 # 'H' (perpendicular) or 'V' (parallel)
    direct_ch: int = 1                      # rx1 = "RE" reference antenna
    reflected_ch: int = 2                   # rx2 = "GR" ground antenna
    # --- processing ---
    block_sec: float = 0.1                  # coherent averaging window
    coherence_threshold: float = 0.3        # |γ| below this → block masked
    analysis_bw_khz: float = 100.0          # band isolated around the carrier
    center_freq_offset_mhz: float = 0.0     # beacon offset from HW center freq
    max_samples: Optional[int] = None
    # --- calibration ---
    amp_cal: float = 1.0                    # C_cal: Γ = (P_r/P_d)·C_cal
    phase_cal_rad: float = 0.0              # instrumental+reference phase, subtracted
    reference_json: Optional[str] = None    # capture over a known reflector (|r|=1)
    # --- moisture model ---
    eps_r_max: float = 80.0                 # water at ~2 GHz; inversion saturates here
    #: 'mironov' (PRIMARY - bound/free water, clay only), 'hallikainen'
    #: (sand+clay polynomial) or 'topp' (no texture, no imaginary part).
    soil_model: str = "mironov"
    sand_pct: float = 80.0                  # % by weight — from the site config
    clay_pct: float = 10.0                  # % by weight
    # --- validation ---
    ground_truth_json: Optional[str] = None  # in-situ measurements to score against

    def soil(self, freq_hz=None):
        """The dielectric model to use, as a self-describing object."""
        return SoilModel(name=self.soil_model, sand_pct=self.sand_pct,
                         clay_pct=self.clay_pct,
                         freq_ghz=(freq_hz / 1e9) if freq_hz else 1.4)

    def resolved_elevation(self):
        """(elevation_deg, is_placeholder). Placeholder = 45° with loud warning."""
        if self.elevation_deg is None:
            print("\n[physics] *** WARNING: elevation_deg not set — using 45° "
                  "PLACEHOLDER. Retrieved values are NOT quantitative until the "
                  "real transmitter elevation angle is configured. ***\n")
            return 45.0, True
        return float(self.elevation_deg), False


# ═══════════════════════════════════════════════════════════════
#  FORWARD MODELS  (Fresnel, Topp)
# ═══════════════════════════════════════════════════════════════

def fresnel_r(eps_r, elev_deg, pol="H"):
    """
    Complex Fresnel reflection coefficient, air → medium(ε_r), for a wave
    arriving at elevation (grazing) angle θe above the surface. µ_r = 1.

        H (⊥):  r = (sinθe − x)/(sinθe + x),        x = sqrt(ε_r − cos²θe)
        V (∥):  r = (ε_r·sinθe − x)/(ε_r·sinθe + x)

    For real ε_r > 1: r_H is real-negative (phase π) at every angle; r_V flips
    sign at the Brewster condition. Accepts complex ε_r (lossy soil).
    """
    th = np.asarray(elev_deg, dtype=float) * DEG
    s, c2 = np.sin(th), np.cos(th) ** 2
    x = np.sqrt(np.asarray(eps_r, dtype=complex) - c2)
    if pol.upper() == "H":
        return (s - x) / (s + x)
    if pol.upper() == "V":
        return (eps_r * s - x) / (eps_r * s + x)
    raise ValueError(f"polarization must be 'H' or 'V', got {pol!r}")


def brewster_eps(elev_deg):
    """ε_r at which r_V = 0 for elevation θe: root of s²ε² − ε + cos²θe = 0."""
    th = elev_deg * DEG
    s2 = np.sin(th) ** 2
    return (1.0 + abs(np.cos(2 * th))) / (2.0 * s2)


def invert_eps_from_reflectivity(gamma_refl, elev_deg, pol="H", eps_r_max=80.0):
    """
    Power reflectivity Γ = |r|² → real dielectric constant ε_r.

    H-pol closed form (monotonic in |r|, numerically stable): for ε_r ≥ 1,
        |r_H| = (x−s)/(x+s)  ⇒  x = s·(1+|r|)/(1−|r|),  ε_r = x² + cos²θe.
    V-pol: |r_V| is two-valued around the Brewster zero; inverted numerically
    on the wet branch ε_r ∈ [ε_Brewster, eps_r_max] (typical soils; documented).

    Γ ≥ 1 (non-physical, calibration error or specular enhancement) saturates
    at eps_r_max. NaN in → NaN out.
    """
    scalar_in = np.ndim(gamma_refl) == 0
    g = np.atleast_1d(np.asarray(gamma_refl, dtype=float)).copy()
    out = np.full(g.shape, np.nan)
    # elev_deg may be a scalar or a per-sample array (time-varying satellite
    # geometry) — broadcast to the reflectivity shape either way.
    elev = np.broadcast_to(np.atleast_1d(np.asarray(elev_deg, dtype=float)), g.shape)
    th = elev * DEG
    s, c2 = np.sin(th), np.cos(th) ** 2

    valid = np.isfinite(g) & (g >= 0.0)
    r_abs = np.sqrt(np.clip(g, 0.0, None))

    if pol.upper() == "H":
        r_cap = np.clip(r_abs, 0.0, 1.0 - 1e-9)
        x = s * (1.0 + r_cap) / (1.0 - r_cap)
        eps = x ** 2 + c2
        out[valid] = np.minimum(eps[valid], eps_r_max)
    else:
        for i in np.ndindex(g.shape):
            if not valid[i]:
                continue
            e_lo = max(brewster_eps(elev[i]) + 1e-9, 1.0 + 1e-9)
            r_max_branch = abs(fresnel_r(eps_r_max, elev[i], "V"))
            ra = min(r_abs[i], r_max_branch - 1e-12)
            try:
                out[i] = brentq(
                    lambda e, target=ra, ev=elev[i]: abs(fresnel_r(e, ev, "V")) - target,
                    e_lo, eps_r_max)
            except ValueError:
                out[i] = eps_r_max
    return float(out[0]) if scalar_in else out


def topp_eps_to_vwc(eps_r):
    """
    Topp et al. (1980): apparent dielectric → volumetric water content [m³/m³]
        θ_v = −5.3e−2 + 2.92e−2·ε − 5.5e−4·ε² + 4.3e−6·ε³
    Empirical for mineral soils, nominal validity ≲1 GHz; used here as the
    standard first-order model at 2.4 GHz (swap for Hallikainen with soil
    texture when available). Clipped to the physical range [0, 0.65].
    """
    e = np.asarray(eps_r, dtype=float)
    vwc = -5.3e-2 + 2.92e-2 * e - 5.5e-4 * e ** 2 + 4.3e-6 * e ** 3
    return np.clip(vwc, 0.0, 0.65)


def topp_vwc_to_eps(vwc):
    """Numeric inverse of Topp (for plots/synthesis)."""
    v = np.atleast_1d(np.asarray(vwc, dtype=float))
    out = np.array([brentq(lambda e, target=vi: topp_eps_to_vwc(e) - target, 1.0, 90.0)
                    for vi in v])
    return out if out.shape[0] > 1 else float(out[0])


# ── Hallikainen: the same question, but told what the soil is made of ──────
#
# Hallikainen, Ulaby, Dobson, El-Rayes & Wu (1985), "Microwave Dielectric
# Behavior of Wet Soil — Part I: Empirical Models and Experimental
# Observations", IEEE TGRS GE-23(1):25-34.
#
#     ε' = (a0 + a1·S + a2·C) + (b0 + b1·S + b2·C)·mv + (c0 + c1·S + c2·C)·mv²
#
# S and C are sand and clay percent BY WEIGHT, mv is volumetric water content.
# Coefficients are published per frequency; the two below bracket this system's
# 2.4 GHz operating point.
#
# WHY THIS MATTERS MORE THAN IT LOOKS: Topp is a single curve fitted to mineral
# soils at ≲1 GHz, so at 2.4 GHz it is EXTRAPOLATED about 2.4× past its
# published range and it cannot know that this site is sandy. Hallikainen is
# published from 1.4 to 18 GHz, so at 2.4 GHz it is INTERPOLATED *inside* its
# range, and sand fraction is an input rather than an assumption. That is the
# single strongest argument for making it the default here.
#
# PROVENANCE WARNING: this table was transcribed from the 1985 paper and could
# NOT be re-checked against the primary source while it was written. It is
# therefore validated by behaviour instead: _selftest asserts that for a loam
# (the soil class Topp was fitted on) at 1.4 GHz, Hallikainen and Topp agree to
# within 0.03 m³/m³ across mv = 0.15…0.35. A mistyped coefficient breaks that
# agreement immediately. Re-derive from the paper before publishing a number.
HALLIKAINEN_COEFFS = {
    # freq_ghz: {"real": (a0,a1,a2, b0,b1,b2, c0,c1,c2), "imag": (...)}
    1.4: {"real": (2.862, -0.012, 0.001,
                   3.803, 0.462, -0.341,
                   119.006, -0.500, 0.633),
          "imag": (0.356, -0.003, -0.008,
                   5.507, 0.044, -0.002,
                   17.753, -0.313, 0.206)},
    4.0: {"real": (2.927, -0.012, -0.001,
                   5.505, 0.371, 0.062,
                   114.826, -0.389, -0.547),
          "imag": (0.004, 0.001, 0.002,
                   0.951, 0.005, -0.010,
                   16.759, 0.192, 0.290)},
}
HALLIKAINEN_FREQ_RANGE_GHZ = (1.4, 4.0)     # the span this table interpolates in


def _hallikainen_at(freq_ghz, part):
    """The nine coefficients at `freq_ghz`, linearly interpolated in frequency.

    Between the published rows this is an interpolation; outside them it is
    clamped to the nearest published row rather than extrapolated, because a
    quadratic fit run past its fitting range diverges fast.
    """
    lo, hi = HALLIKAINEN_FREQ_RANGE_GHZ
    f = float(np.clip(freq_ghz, lo, hi))
    w = (f - lo) / (hi - lo)
    c_lo = np.array(HALLIKAINEN_COEFFS[lo][part], dtype=float)
    c_hi = np.array(HALLIKAINEN_COEFFS[hi][part], dtype=float)
    return c_lo + w * (c_hi - c_lo)


def hallikainen_vwc_to_eps(vwc, sand_pct, clay_pct, freq_ghz, part="real"):
    """Volumetric water content → dielectric constant, given the soil texture.

    Validity (Hallikainen et al. 1985, §III): 1.4–18 GHz, mv up to ~0.5,
    five soil types spanning sand 51–92% / clay 5–48%. Outside that the
    polynomial is not meant to be trusted.
    """
    a0, a1, a2, b0, b1, b2, c0, c1, c2 = _hallikainen_at(freq_ghz, part)
    s, c = float(sand_pct), float(clay_pct)
    mv = np.asarray(vwc, dtype=float)
    return ((a0 + a1 * s + a2 * c)
            + (b0 + b1 * s + b2 * c) * mv
            + (c0 + c1 * s + c2 * c) * mv ** 2)


def hallikainen_eps_to_vwc(eps_r, sand_pct, clay_pct, freq_ghz):
    """Dielectric constant → volumetric water content (the inverse we need).

    Solved rather than re-fitted: the forward polynomial is monotonic in mv over
    the physical range, so the root is unique and brentq finds it. Values below
    the dry-soil ε or above the mv=0.65 ε clamp to the ends of that range, which
    is what "drier than the model can express" and "wetter" honestly mean.
    """
    lo_mv, hi_mv = 0.0, 0.65
    e = np.atleast_1d(np.asarray(eps_r, dtype=float))
    out = np.full(e.shape, np.nan)

    def forward(mv):
        return hallikainen_vwc_to_eps(mv, sand_pct, clay_pct, freq_ghz)

    e_dry, e_wet = float(forward(lo_mv)), float(forward(hi_mv))
    for i in np.ndindex(e.shape):
        if not np.isfinite(e[i]):
            continue
        if e[i] <= e_dry:
            out[i] = lo_mv
        elif e[i] >= e_wet:
            out[i] = hi_mv
        else:
            out[i] = brentq(lambda mv, t=float(e[i]): forward(mv) - t, lo_mv, hi_mv)
    return out if np.ndim(eps_r) else float(out[0])


# ── Mironov: the same question again, asked the way SMOS and SMAP ask it ──
#
# Mironov, Kosolapova & Savin (2009), "Generalized Refractive Mixing Dielectric
# Model for Moist Soils", IEEE TGRS 47(7):2059-2070.
#
# WHY THIS IS THE PRIMARY MODEL HERE, in one paragraph, because the choice is
# load-bearing and the thesis has to defend it.
#
# Topp and Hallikainen are POLYNOMIAL FITS: somebody measured ε over a set of
# soils and regressed it. They interpolate well inside their fitting range and
# say nothing outside it. Mironov is a REFRACTIVE MIXING model — it computes
# the refractive index of the mixture from the refractive indices of its
# components, so the soil is dry mineral matter plus two physically distinct
# kinds of water:
#
#   BOUND water    the first few percent, held on particle surfaces by
#                  electrostatic forces. It is rotationally hindered, so its
#                  Debye relaxation is SLOWER and its permittivity far LOWER
#                  than free water's — it barely reflects.
#   FREE water     everything above the maximum bound-water fraction m_vt.
#                  Ordinary bulk water, ε₀ ≈ 100 at L-band.
#
# That distinction is exactly what matters at this site. The in-situ reading is
# 0.067 m³/m³ and m_vt for a 20% clay soil is 0.090 — so EVERY drop of water in
# this field is bound water, and a model that treats it as free water predicts
# a permittivity, and therefore a reflectivity, that is substantially too high.
# Inverted, that reads as a soil far drier than it is. Topp and Hallikainen
# have no bound-water term at all; they cannot represent this regime, they can
# only be extrapolated into it.
#
# It is also the model SMOS and SMAP run operationally at 1.4 GHz, which is
# where this instrument's L-band pass sits.
#
# WHAT IT COSTS: the clay fraction, and the temperature. Sand is NOT an input —
# Mironov's spectroscopic parameters are all regressions on clay content alone.
# So the site file's `sand_pct` is unused by this model and `clay_pct` carries
# the whole texture dependence, which is worth knowing before quoting a number
# from an ASSUMED texture.

EPS_VACUUM = 8.8541878128e-12       # F/m
MIRONOV_EPS_INF = 4.9               # high-frequency limit, all components
#: The band over which Mironov (2009) fitted the clay regressions behind the
#: Debye parameters. Wider than Hallikainen's rows at the bottom end, which is
#: the point — it covers L-band properly.
MIRONOV_FREQ_RANGE_GHZ = (0.3, 26.5)


@dataclass
class MironovParams:
    """The spectroscopic parameters of ONE soil, at ONE frequency.

    Every field is a published regression on the clay fraction (Mironov 2009,
    Table I-III) except the two refractive indices, which are Debye relaxations
    evaluated at the carrier. Kept as a record rather than recomputed inline so
    the console can print the numbers it actually used.
    """
    clay_pct: float
    freq_ghz: float
    n_d: float          # refractive index of the DRY soil
    k_d: float          # its normalised attenuation coefficient
    m_vt: float         # maximum bound-water fraction  [m³/m³]
    n_b: float          # refractive index of BOUND water
    k_b: float
    n_u: float          # refractive index of FREE (unbound) water
    k_u: float

    @property
    def n_t(self):
        """Refractive index at the bound-water capacity — where the branch turns.

        n_t = n_d + (n_b − 1)·m_vt. Below it the retrieval is reading bound
        water, above it free water, and the two have very different slopes.
        """
        return self.n_d + (self.n_b - 1.0) * self.m_vt


def _debye_refractive(eps_0, tau_s, sigma, freq_ghz):
    """(n, k) of one water component from its Debye relaxation + conductivity.

        ε′  = ε_∞ + (ε₀ − ε_∞)/(1 + (ωτ)²)
        ε″  = (ε₀ − ε_∞)·ωτ/(1 + (ωτ)²) + σ/(ω·ε_vac)
        n   = √[(√(ε′²+ε″²) + ε′)/2]        k = √[(√(ε′²+ε″²) − ε′)/2]

    The second term of ε″ is ionic conductivity, not relaxation; it is what
    makes a saline soil lossy at L-band and it is why ε″ cannot be ignored in
    the sensing depth even when it is negligible in |R_vv|.
    """
    w = 2.0 * np.pi * float(freq_ghz) * 1e9
    wt = w * float(tau_s)
    eps_p = MIRONOV_EPS_INF + (eps_0 - MIRONOV_EPS_INF) / (1.0 + wt ** 2)
    eps_pp = ((eps_0 - MIRONOV_EPS_INF) * wt / (1.0 + wt ** 2)
              + float(sigma) / (w * EPS_VACUUM))
    mod = np.hypot(eps_p, eps_pp)
    return (float(np.sqrt((mod + eps_p) / 2.0)),
            float(np.sqrt((mod - eps_p) / 2.0)))


def mironov_params(clay_pct, freq_ghz):
    """The published clay regressions, evaluated once for this soil and band."""
    c = float(clay_pct)
    n_d = 1.634 - 0.539e-2 * c + 0.2748e-4 * c ** 2
    k_d = 0.03952 - 0.04038e-2 * c
    m_vt = 0.02863 + 0.30673e-2 * c
    # bound water
    eps_0b = 79.8 - 85.4e-2 * c + 32.7e-4 * c ** 2
    tau_b = 1.062e-11 + 3.450e-12 * 1e-2 * c
    sig_b = 0.3112 + 0.467e-2 * c
    n_b, k_b = _debye_refractive(eps_0b, tau_b, sig_b, freq_ghz)
    # free water
    eps_0u = 100.0
    tau_u = 8.5e-12
    sig_u = 0.3631 + 1.217e-2 * c
    n_u, k_u = _debye_refractive(eps_0u, tau_u, sig_u, freq_ghz)
    return MironovParams(clay_pct=c, freq_ghz=float(freq_ghz), n_d=n_d, k_d=k_d,
                         m_vt=m_vt, n_b=n_b, k_b=k_b, n_u=n_u, k_u=k_u)


def mironov_vwc_to_eps(vwc, clay_pct, freq_ghz, part="real"):
    """θ_v → ε′ (or ε″) by refractive mixing, bound water first.

        m_v ≤ m_vt :  n_s = n_d + (n_b − 1)·m_v
                      k_s = k_d + k_b·m_v
        m_v > m_vt :  n_s = n_d + (n_b − 1)·m_vt + (n_u − 1)·(m_v − m_vt)
                      k_s = k_d + k_b·m_vt      + k_u·(m_v − m_vt)

        ε′ = n_s² − k_s²        ε″ = 2·n_s·k_s

    The mixing is linear in the REFRACTIVE INDEX, not in the permittivity —
    that is the whole content of the word "refractive" in the model's name, and
    it is why the ε(m_v) curve is not a polynomial.
    """
    p = mironov_params(clay_pct, freq_ghz)
    mv = np.asarray(vwc, dtype=float)
    bound = np.minimum(mv, p.m_vt)
    free = np.maximum(mv - p.m_vt, 0.0)
    n_s = p.n_d + (p.n_b - 1.0) * bound + (p.n_u - 1.0) * free
    k_s = p.k_d + p.k_b * bound + p.k_u * free
    return n_s ** 2 - k_s ** 2 if part == "real" else 2.0 * n_s * k_s


def mironov_eps_to_vwc(eps_r, clay_pct, freq_ghz):
    """ε′ → θ_v, the inverse this retrieval actually needs.

    Solved on the forward model rather than applied as the closed form of
    Eq. 04_mironov. The closed form drops k_s (it reads n_s = √ε′), which is a
    fine approximation and a needless one when the forward model is monotonic
    in m_v and brentq costs nothing. The two agree to <0.002 m³/m³ here; the
    branch the closed form names is still reported, because "this reading is
    all bound water" is the physically interesting statement.

    Below the dry-soil ε′ and above m_v = 0.65 the result clamps, which is what
    "drier than the model can express" and "wetter" honestly mean.
    """
    lo_mv, hi_mv = 0.0, 0.65
    e = np.atleast_1d(np.asarray(eps_r, dtype=float))
    out = np.full(e.shape, np.nan)

    def forward(mv):
        return float(mironov_vwc_to_eps(mv, clay_pct, freq_ghz, "real"))

    e_dry, e_wet = forward(lo_mv), forward(hi_mv)
    for i in np.ndindex(e.shape):
        if not np.isfinite(e[i]):
            continue
        if e[i] <= e_dry:
            out[i] = lo_mv
        elif e[i] >= e_wet:
            out[i] = hi_mv
        else:
            out[i] = brentq(lambda mv, t=float(e[i]): forward(mv) - t,
                            lo_mv, hi_mv)
    return out if np.ndim(eps_r) else float(out[0])


@dataclass
class SoilModel:
    """Which ε_r ↔ θ_v relationship is in force, and what it knows about."""
    name: str = "mironov"
    sand_pct: float = 80.0
    clay_pct: float = 10.0
    freq_ghz: float = 1.4

    def eps_to_vwc(self, eps_r):
        if self.name == "topp":
            return topp_eps_to_vwc(eps_r)
        if self.name == "mironov":
            return np.clip(mironov_eps_to_vwc(eps_r, self.clay_pct,
                                              self.freq_ghz), 0.0, 0.65)
        vwc = hallikainen_eps_to_vwc(eps_r, self.sand_pct, self.clay_pct,
                                     self.freq_ghz)
        return np.clip(vwc, 0.0, 0.65)

    def vwc_to_eps(self, vwc):
        if self.name == "topp":
            return topp_vwc_to_eps(vwc)
        if self.name == "mironov":
            return mironov_vwc_to_eps(vwc, self.clay_pct, self.freq_ghz, "real")
        return hallikainen_vwc_to_eps(vwc, self.sand_pct, self.clay_pct,
                                      self.freq_ghz)

    def mironov(self):
        """The spectroscopic parameters, for the console to print. None if N/A."""
        if self.name != "mironov":
            return None
        return mironov_params(self.clay_pct, self.freq_ghz)

    def verdict(self):
        """The one-word validity verdict — the first token of validity_note()."""
        return self.validity_note().split()[0].rstrip(":")

    def in_range(self):
        """True when this model is being used INSIDE its published range."""
        return self.verdict() in ("INTERPOLATED", "IN-RANGE")

    def label(self):
        if self.name == "topp":
            return "Topp et al. (1980)"
        if self.name == "mironov":
            return (f"Mironov et al. (2009) GRMDM, clay {self.clay_pct:.0f}% "
                    f"at {self.freq_ghz:.2f} GHz")
        return (f"Hallikainen et al. (1985), sand {self.sand_pct:.0f}% / "
                f"clay {self.clay_pct:.0f}% at {self.freq_ghz:.2f} GHz")

    def validity_note(self):
        """Plain statement of whether this model is being used inside its range."""
        if self.name == "topp":
            return ("EXTRAPOLATED: Topp was fitted at ≲1 GHz and this capture is "
                    f"at {self.freq_ghz:.2f} GHz — about {self.freq_ghz / 1.0:.1f}× "
                    "beyond its published range, and it cannot use soil texture.")
        if self.name == "mironov":
            lo, hi = MIRONOV_FREQ_RANGE_GHZ
            if lo <= self.freq_ghz <= hi:
                return (f"IN-RANGE: Mironov's spectroscopic regressions are "
                        f"published for {lo}–{hi} GHz and this capture is at "
                        f"{self.freq_ghz:.2f} GHz. Note that SAND fraction is "
                        f"NOT an input — clay carries the whole texture "
                        f"dependence in this model.")
            return (f"OUT-OF-RANGE: {self.freq_ghz:.2f} GHz is outside the "
                    f"{lo}–{hi} GHz over which Mironov's Debye parameters were "
                    f"fitted.")
        lo, hi = HALLIKAINEN_FREQ_RANGE_GHZ
        if lo <= self.freq_ghz <= hi:
            return (f"INTERPOLATED inside the published range: Hallikainen spans "
                    f"1.4–18 GHz and {self.freq_ghz:.2f} GHz sits between the "
                    f"{lo} and {hi} GHz coefficient rows.")
        return (f"CLAMPED: {self.freq_ghz:.2f} GHz is outside the {lo}–{hi} GHz "
                f"rows carried here; the nearest row is used unextrapolated.")


# ═══════════════════════════════════════════════════════════════
#  GROUND TRUTH — what a probe in the ground actually read
# ═══════════════════════════════════════════════════════════════
#
# The file is deliberately split in two:
#
#   site / instrument  — constants you configure once (soil texture, antenna
#                        height, vegetation). These are NOT measurements.
#   measurements       — a LIST of timestamped in-situ readings. One row today,
#                        ten rows after the next field campaign, with no schema
#                        change: that is why it is a list of objects and not a
#                        single "soil_moisture" scalar at the top level.
#
# Keeping them apart is the whole point. A retrieval that quietly used the site
# config as if it were truth would be scoring itself against its own assumption.

GROUND_TRUTH_SCHEMA = "sreto.ground_truth/1"

# Textural defaults per soil class, % by weight. Rough class centroids from the
# USDA texture triangle — good enough to start, and the file lets you replace
# them with a real lab analysis, which is what should eventually happen.
SOIL_TEXTURE_CLASSES = {
    "sandy":      {"sand_pct": 85.0, "clay_pct": 5.0},
    "sandy loam": {"sand_pct": 65.0, "clay_pct": 10.0},
    "loam":       {"sand_pct": 40.0, "clay_pct": 20.0},
    "silt loam":  {"sand_pct": 20.0, "clay_pct": 15.0},
    "clay loam":  {"sand_pct": 32.0, "clay_pct": 34.0},
    "clay":       {"sand_pct": 20.0, "clay_pct": 60.0},
}


def default_ground_truth():
    """The seed config: one in-situ reading, sandy soil, grass cover.

    Values supplied by the operator for the first field site. Everything that is
    not yet known is present as null rather than absent, so the shape of what is
    missing is visible in the file itself.
    """
    return {
        "schema": GROUND_TRUTH_SCHEMA,
        "_README": [
            "In-situ ground truth for validating the physics retrieval.",
            "'site' and 'instrument' are CONFIGURED constants.",
            "'measurements' are MEASURED values — append one object per field "
            "reading; the retrieval is scored against the reading nearest in "
            "time to the capture.",
        ],
        "site": {
            "name": "field site 1",
            "latitude_deg": None,
            "longitude_deg": None,
            "soil_texture": {
                "class": "sandy",
                "sand_pct": SOIL_TEXTURE_CLASSES["sandy"]["sand_pct"],
                "clay_pct": SOIL_TEXTURE_CLASSES["sandy"]["clay_pct"],
                "source": "class centroid — replace with a lab particle-size "
                          "analysis when you have one",
            },
            "vegetation": {
                "cover": "grass",
                "height_m": None,
                "water_content_kg_m2": None,
                "note": "Grass attenuates and depolarises. The current chain "
                        "models BARE soil, so vegetation is recorded here but "
                        "NOT yet corrected for — see the pipeline's vegetation "
                        "stage.",
            },
            "surface_roughness_cm": None,
        },
        "instrument": {
            "rx_height_m": None,
            "direct_antenna": "rx1 (RE, sky-facing)",
            "reflected_antenna": "rx2 (GR, ground-facing)",
        },
        "measurements": [
            {
                "timestamp": "2026-08-04T12:00:00",
                "vwc_m3m3": 0.20,
                "depth_m": 0.05,
                "method": "in-situ probe",
                "note": "seed value supplied with the site configuration",
            },
        ],
    }


def load_ground_truth(path=None):
    """Read the ground-truth file. Returns None when there is not one.

    A missing file is a normal state, not an error: the retrieval still runs,
    it just cannot be scored. Malformed JSON IS an error — silently treating a
    broken truth file as 'no truth' is how a validation quietly stops happening.
    """
    if not path:
        return None
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError(f"{path}: ground truth must be a JSON object")
    data.setdefault("measurements", [])
    return data


def save_ground_truth(path, data):
    """Write the ground-truth file, creating its directory if needed."""
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    return path


def texture_from_ground_truth(truth):
    """(sand_pct, clay_pct) from the site config, falling back to its class."""
    if not truth:
        return None
    tex = (truth.get("site") or {}).get("soil_texture") or {}
    sand, clay = tex.get("sand_pct"), tex.get("clay_pct")
    if sand is None or clay is None:
        preset = SOIL_TEXTURE_CLASSES.get(str(tex.get("class", "")).lower())
        if not preset:
            return None
        sand = preset["sand_pct"] if sand is None else sand
        clay = preset["clay_pct"] if clay is None else clay
    return float(sand), float(clay)


def _parse_time(text):
    """ISO 8601, or a capture stem's 20260630_153205 prefix. None if neither."""
    if not text:
        return None
    text = str(text).strip()
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        pass
    m = re.search(r"(\d{8})_(\d{6})", text)
    if m:
        try:
            return datetime.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M%S")
        except ValueError:
            return None
    return None


def score_against_truth(theta_v, sigma_theta_v, truth, capture_time=None):
    """Compare a retrieved θ_v against the in-situ reading nearest in time.

    Returns None when there is nothing to compare against. `capture_time` may be
    a datetime, an ISO string, or a capture filename — the stem carries the
    timestamp, so passing the filename is enough.

    'agrees' means the truth falls inside the retrieval's own ±1σ. That is a
    deliberately weak claim: a retrieval with a huge σ agrees with everything,
    which is why the σ is always reported next to the verdict.
    """
    samples = (truth or {}).get("measurements") or []
    usable = [s for s in samples if s.get("vwc_m3m3") is not None]
    if not usable or not np.isfinite(theta_v):
        return None

    when = capture_time if isinstance(capture_time, datetime) else _parse_time(capture_time)
    chosen, lag_h = usable[0], None
    if when is not None:
        timed = [(s, _parse_time(s.get("timestamp"))) for s in usable]
        timed = [(s, t) for s, t in timed if t is not None]
        if timed:
            chosen, best = min(
                timed, key=lambda st: abs((st[1] - when).total_seconds()))
            lag_h = (best - when).total_seconds() / 3600.0

    truth_vwc = float(chosen["vwc_m3m3"])
    error = float(theta_v) - truth_vwc
    sigma = float(sigma_theta_v) if np.isfinite(sigma_theta_v) else float("nan")
    return {
        "truth_vwc": truth_vwc,
        "retrieved_vwc": float(theta_v),
        "error": error,
        "abs_error": abs(error),
        "sigma": sigma,
        "agrees_within_1sigma": bool(np.isfinite(sigma) and abs(error) <= sigma),
        "n_measurements": len(usable),
        "matched_timestamp": chosen.get("timestamp"),
        "lag_hours": lag_h,
        "depth_m": chosen.get("depth_m"),
        "method": chosen.get("method"),
    }


# ═══════════════════════════════════════════════════════════════
#  PROCESSING CHAIN (shared by file-driven runs, calibration, selftest)
# ═══════════════════════════════════════════════════════════════

def _process_arrays(s_d, s_r, fs_hz, cfg):
    """mix → band-limit/decimate → block coherence. Returns an observables dict."""
    off_hz = cfg.center_freq_offset_mhz * 1e6
    s_d = sdr_core.digital_mix(s_d, fs_hz, off_hz)
    s_r = sdr_core.digital_mix(s_r, fs_hz, off_hz)

    bw_hz = cfg.analysis_bw_khz * 1e3
    s_d, fs_out = sdr_core.bandlimit_and_decimate(s_d, fs_hz, bw_hz)
    s_r, _ = sdr_core.bandlimit_and_decimate(s_r, fs_hz, bw_hz)

    t_k, gamma, p_d, p_r = sdr_core.block_coherence(s_d, s_r, fs_out, cfg.block_sec)
    n_per_block = int(round(cfg.block_sec * fs_out))
    n_eff = min(bw_hz * cfg.block_sec, n_per_block)  # independent samples per block

    return {"t": t_k, "gamma": gamma, "p_d": p_d, "p_r": p_r,
            "n_eff": n_eff, "fs_out": fs_out, "s_d_nb": s_d, "s_r_nb": s_r}


def _circular_mean_angle(gamma):
    """Mean phase of complex samples, magnitude-weighted, wrap-safe."""
    return float(np.angle(np.nansum(gamma)))


def calibrate_from_arrays(s_d, s_r, fs_hz, cfg):
    """
    Derive (amp_cal, phase_cal_rad) from a reference capture over a known
    reflector with |r| = 1 (metal plate, same geometry as the experiment):

        amp_cal   = median(P_d/P_r)          (so Γ_plate = 1)
        phase_cal = circular mean of arg γ   (absorbs instrumental phase, the
                    geometric phase of the reference surface, and the plate's
                    reflection phase — which cancels against the soil's for
                    H-pol, making ΔΦ purely geometric)
    """
    obs = _process_arrays(s_d, s_r, fs_hz, cfg)
    coh = np.abs(obs["gamma"])
    valid = coh >= cfg.coherence_threshold
    if not np.any(valid):
        raise ValueError("Reference capture has no blocks above the coherence "
                         "threshold — cannot calibrate.")
    amp_cal = float(np.median(obs["p_d"][valid] / obs["p_r"][valid]))
    phase_cal = _circular_mean_angle(obs["gamma"][valid])
    print(f"[cal] reference: {valid.sum()}/{len(valid)} blocks valid, "
          f"amp_cal={amp_cal:.4g}, phase_cal={phase_cal:+.4f} rad")
    return amp_cal, phase_cal


def calibrate_from_reference(cfg):
    """File-driven wrapper around calibrate_from_arrays."""
    ch1, ch2, fs, _, _, _, is_dual = sdr_core.load_dual_iq(
        cfg.reference_json, max_samples=cfg.max_samples)
    if not is_dual:
        raise ValueError("Reference capture must be dual-channel.")
    s_d = ch1 if cfg.direct_ch == 1 else ch2
    s_r = ch2 if cfg.reflected_ch == 2 else ch1
    return calibrate_from_arrays(s_d, s_r, fs, cfg)


def _extract_from_arrays(s_d, s_r, fs_hz, f_carrier_hz, cfg,
                         amp_cal, phase_cal, calibrated_label,
                         elevation_series=None, rx_height_m=None):
    """
    Full retrieval on in-memory channels. Returns the results dict.

    elevation_series : optional (t_sec, elevation_deg) arrays from the orbit
        tracker — interpolated onto block centres for a moving transmitter.
    rx_height_m : antenna height above the surface. With a time-varying
        elevation the interferometric phase of a STATIC scene already changes as
            Δφ_geo(t) = (4π·h₀/λ)·(sin e(t) − sin e(t₀)),
        so that predicted term is removed before heights are formed; the
        residual is genuine surface/height change around h₀.
    """
    lam = sdr_core.C_LIGHT / f_carrier_hz

    obs = _process_arrays(s_d, s_r, fs_hz, cfg)
    t, gamma, n_eff = obs["t"], obs["gamma"], obs["n_eff"]
    coh = np.abs(gamma)
    valid = coh >= cfg.coherence_threshold

    # ---- per-block elevation (scalar config or orbit-tracker series) ---
    if elevation_series is not None:
        t_geo = np.asarray(elevation_series[0], dtype=float)
        e_geo = np.asarray(elevation_series[1], dtype=float)
        elev_blocks = np.interp(t, t_geo, e_geo)
        # No specular geometry near/below the horizon — mask those blocks like
        # low-coherence ones (sin e → 0 would otherwise explode the retrieval).
        low = elev_blocks < 5.0
        if np.any(low):
            print(f"[physics] NOTE: {int(low.sum())} blocks masked "
                  f"(transmitter elevation < 5°).")
            valid &= ~low
        elev_repr, elev_placeholder = float(np.median(elev_blocks)), False
    else:
        elev_scalar, elev_placeholder = cfg.resolved_elevation()
        elev_blocks = np.full(len(t), elev_scalar)
        elev_repr = elev_scalar
    sin_e = np.sin(elev_blocks * DEG)                      # per-block array
    sin_e_repr = np.sin(elev_repr * DEG)

    # ---- phase branch → altimetry -------------------------------------
    dphi_w = np.angle(gamma * np.exp(-1j * phase_cal))     # calibrated, wrapped
    dphi_u = sdr_core.unwrap_masked(dphi_w, valid)         # ΔΦ(t), NaN where masked
    if cfg.reference_json is None and np.any(valid):
        # No absolute phase reference: report height CHANGE from first valid block
        dphi_u = dphi_u - dphi_u[valid][0]

    geom_corrected = False
    if elevation_series is not None and rx_height_m and np.any(valid):
        # Moving transmitter: subtract the phase a static antenna at h₀ over a
        # static surface would already show, so Δh isolates true scene change.
        i0 = np.flatnonzero(valid)[0]
        dphi_pred = (4.0 * np.pi * float(rx_height_m) / lam) * (sin_e - sin_e[i0])
        dphi_u = dphi_u - dphi_pred
        geom_corrected = True

    dh = lam * dphi_u / (4.0 * np.pi * sin_e)              # [m]
    sig_phi = sdr_core.phase_sigma_crlb(coh, n_eff)
    sig_h = lam * sig_phi / (4.0 * np.pi * sin_e)

    # ---- amplitude branch → soil moisture -----------------------------
    gamma_refl = np.where(valid, (obs["p_r"] / (obs["p_d"] + 1e-30)) * amp_cal, np.nan)
    saturated = gamma_refl >= 1.0
    if np.any(saturated & valid):
        print(f"[physics] WARNING: {int(np.sum(saturated & valid))} blocks have "
              f"Γ ≥ 1 (non-physical) — check amp_cal; saturating inversion.")
    eps_r = invert_eps_from_reflectivity(gamma_refl, elev_blocks, cfg.polarization,
                                         cfg.eps_r_max)
    soil = cfg.soil(f_carrier_hz)
    theta_v = np.where(np.isfinite(eps_r), soil.eps_to_vwc(eps_r), np.nan)

    # σ_Γ/Γ ≈ sqrt(2/N_eff) (power-ratio, first order) → σ_θv numerically
    sig_gamma = gamma_refl * np.sqrt(2.0 / n_eff)
    vwc_hi = soil.eps_to_vwc(invert_eps_from_reflectivity(
        np.clip(gamma_refl + sig_gamma, 0, None), elev_blocks, cfg.polarization, cfg.eps_r_max))
    vwc_lo = soil.eps_to_vwc(invert_eps_from_reflectivity(
        np.clip(gamma_refl - sig_gamma, 0, None), elev_blocks, cfg.polarization, cfg.eps_r_max))
    sig_theta_v = np.abs(vwc_hi - vwc_lo) / 2.0

    # ---- coarse group-delay anchor (honesty: ~1/BW resolution) --------
    n_xc = min(len(obs["s_d_nb"]), int(2.0 * obs["fs_out"]))
    tau_us, xc_peak, _, _ = sdr_core.xcorr_delay(
        obs["s_d_nb"][:n_xc], obs["s_r_nb"][:n_xc], obs["fs_out"])
    h_coarse = -(tau_us * 1e-6) * sdr_core.C_LIGHT / (2.0 * sin_e_repr)  # z_r lags z_d
    delay_res_m = sdr_core.C_LIGHT / (cfg.analysis_bw_khz * 1e3) / (2.0 * sin_e_repr)

    return {
        "t": t, "coherence": coh, "valid": valid,
        "dphi_wrapped": dphi_w, "dphi_unwrapped": dphi_u,
        "dh_m": dh, "sig_h_m": sig_h, "sig_phi_rad": sig_phi,
        "gamma_refl": gamma_refl, "eps_r": eps_r,
        "theta_v": theta_v, "sig_theta_v": sig_theta_v,
        "lambda_m": lam, "f_carrier_hz": f_carrier_hz,
        "elevation_deg": elev_repr, "elevation_deg_blocks": elev_blocks,
        "elev_placeholder": elev_placeholder, "geom_corrected": geom_corrected,
        "rx_height_m": rx_height_m,
        "soil_model": soil,
        "polarization": cfg.polarization, "n_eff": n_eff,
        "amp_cal": amp_cal, "phase_cal": phase_cal,
        "calibrated_label": calibrated_label,
        "tau_xcorr_us": tau_us, "xcorr_peak": xc_peak,
        "h_coarse_m": h_coarse, "delay_res_m": delay_res_m,
        "ambiguity_m": lam / (2.0 * sin_e_repr),
        "coherence_threshold": cfg.coherence_threshold,
    }


# ═══════════════════════════════════════════════════════════════
#  FIGURES
# ═══════════════════════════════════════════════════════════════

def _style(ax, title=""):
    ax.set_facecolor(PANEL)
    if title:
        ax.set_title(title, color=TEXT, fontsize=10, pad=6, fontweight="bold")
    ax.tick_params(colors=MUTED, labelsize=8)
    ax.xaxis.label.set_color(TEXT)
    ax.yaxis.label.set_color(TEXT)
    for sp in ax.spines.values():
        sp.set_edgecolor(BORDER)
    ax.grid(True, color=BORDER, linewidth=0.6, linestyle="--", alpha=0.7)


def _save(fig, name, save_dir):
    os.makedirs(save_dir, exist_ok=True)
    path = os.path.join(save_dir, f"{name}.png")
    fig.savefig(path, facecolor=fig.get_facecolor(), dpi=150, bbox_inches="tight")
    plt.close(fig)
    print(f"  [saved] {path}")


def _placeholder_stamp(fig, res, y=0.995):
    if res["elev_placeholder"]:
        fig.text(0.5, y, "⚠ GEOMETRY PLACEHOLDER (elevation 45°) — set ELEVATION_DEG",
                 ha="center", va="bottom", color=C_STATUS, fontsize=10, fontweight="bold")


def plot_overview(res, save_dir, info=""):
    """06_00: |γ|, ΔΦ, Γ, θ_v — four time-aligned rows (one measure per axis)."""
    t, valid = res["t"], res["valid"]
    eb = res.get("elevation_deg_blocks")
    if eb is not None and np.ptp(eb) > 0.1:
        elev_str = f"θe={eb.min():.1f}→{eb.max():.1f}° (moving Tx)"
    else:
        elev_str = f"θe={res['elevation_deg']:.1f}°"
    fig, axes = plt.subplots(4, 1, sharex=True, figsize=(12, 12), facecolor=BG)
    fig.suptitle(f"Physics Extraction Overview | {info}\n"
                 f"cal: {res['calibrated_label']} | {elev_str} "
                 f"{res['polarization']}-pol | λ={res['lambda_m']*100:.2f} cm",
                 color=TEXT, fontsize=11, fontweight="bold")

    ax = axes[0]
    ax.plot(t, res["coherence"], color=C_PRIMARY, lw=1.5, label="|γ| coherence")
    ax.axhline(y=res["coherence_threshold"], color=C_STATUS, ls="--", lw=1.2,
               label=f"quality threshold ({res['coherence_threshold']:.2f})")
    if np.any(~valid):
        ax.fill_between(t, 0, 1.05, where=~valid, color=MUTED, alpha=0.15,
                        label="masked (low coherence)")
    ax.set_ylim(0, 1.05)
    ax.set_ylabel("|γ|")
    _style(ax, "Interferometric coherence (quality gate)")
    ax.legend(fontsize=8, loc="lower right")

    ax = axes[1]
    ax.plot(t, res["dphi_unwrapped"], color=C_PRIMARY, lw=1.5, label="unwrapped ΔΦ")
    band = res["sig_phi_rad"]
    ax.fill_between(t, res["dphi_unwrapped"] - band, res["dphi_unwrapped"] + band,
                    color=C_PRIMARY, alpha=0.2, linewidth=0, label="±σφ (CRLB)")
    ax.set_ylabel("ΔΦ (rad)")
    _style(ax, "Calibrated interferometric phase (altimetry observable)")
    ax.legend(fontsize=8, loc="upper right")

    ax = axes[2]
    with np.errstate(divide="ignore", invalid="ignore"):
        g_db = 10.0 * np.log10(res["gamma_refl"])
    ax.plot(t, g_db, color=C_PRIMARY, lw=1.5, label="Γ = (P_r/P_d)·C_cal")
    ax.set_ylabel("Γ (dB)")
    _style(ax, f"Surface reflectivity ({res['calibrated_label']})")
    ax.legend(fontsize=8, loc="upper right")

    ax = axes[3]
    soil = res["soil_model"]
    ax.plot(t, res["theta_v"], color=C_PRIMARY, lw=1.8,
            label=f"θ_v ({soil.name})")
    ax.fill_between(t, res["theta_v"] - res["sig_theta_v"],
                    res["theta_v"] + res["sig_theta_v"],
                    color=C_PRIMARY, alpha=0.2, linewidth=0, label="±σ")
    ax.set_ylabel("θ_v (m³/m³)")
    ax.set_xlabel("Time (s)")
    ax.set_ylim(0, 0.65)
    # Secondary scale: the SAME data re-expressed as ε_r via Topp (unit-style
    # transform, not a second measure — dual-measure axes stay banned).
    eps_grid = np.linspace(1.0, 90.0, 600)
    vwc_grid = soil.eps_to_vwc(eps_grid)
    sec = ax.secondary_yaxis(
        "right",
        functions=(lambda v: np.interp(v, vwc_grid, eps_grid),
                   lambda e: np.interp(e, eps_grid, vwc_grid)))
    sec.set_ylabel(f"ε_r (same data, {soil.name} scale)", color=MUTED)
    sec.tick_params(colors=MUTED, labelsize=8)
    _style(ax, "Volumetric soil moisture")
    ax.legend(fontsize=8, loc="upper right")

    _placeholder_stamp(fig, res)
    fig.subplots_adjust(hspace=0.35, top=0.90)
    _save(fig, "06_00_Physics_Overview", save_dir)


def plot_fresnel_map(res, cfg, save_dir, info=""):
    """06_01: |r|² vs elevation for a dry→wet ε_r family + measured operating point."""
    fig, ax = plt.subplots(figsize=(9, 6), facecolor=BG)
    elev = np.linspace(2, 90, 400)
    vwc_family = np.arange(0.05, 0.501, 0.05)
    cmap = plt.get_cmap("Blues")

    soil = res["soil_model"]
    for i, vwc in enumerate(vwc_family):
        eps = float(soil.vwc_to_eps(vwc))
        refl = np.abs(fresnel_r(eps, elev, cfg.polarization)) ** 2
        shade = 0.35 + 0.6 * i / (len(vwc_family) - 1)   # light=dry → dark=wet
        ax.plot(elev, refl, color=cmap(shade), lw=1.6)
        if i in (0, len(vwc_family) // 2, len(vwc_family) - 1):  # direct labels
            ax.annotate(f"θv={vwc:.2f} (ε={eps:.1f})",
                        xy=(elev[-1], refl[-1]), xytext=(4, 0),
                        textcoords="offset points", fontsize=8,
                        color=cmap(shade), va="center")

    med_g = _nanmed(res["gamma_refl"])
    if np.isfinite(med_g):
        ax.plot(res["elevation_deg"], med_g, marker="*", markersize=16,
                color=C_SECONDARY, markeredgecolor=TEXT, zorder=5)
        ax.annotate(f"measured\nΓ={med_g:.3f} → θv={np.nanmedian(res['theta_v']):.3f}",
                    xy=(res["elevation_deg"], med_g), xytext=(10, 10),
                    textcoords="offset points", fontsize=9, color=TEXT,
                    fontweight="bold")
    ax.axvline(res["elevation_deg"], color=MUTED, ls=":", lw=1)

    ax.set_xlabel("Elevation angle θe (deg)")
    ax.set_ylabel("Power reflectivity |r|²")
    ax.set_ylim(0, 1.0)
    ax.set_xlim(0, 100)
    _style(ax, f"Fresnel map ({cfg.polarization}-pol): how reflectivity encodes "
               f"soil moisture | {info}")
    _placeholder_stamp(fig, res)
    _save(fig, "06_01_Fresnel_Inversion_Map", save_dir)


def plot_altimetry(res, save_dir, info=""):
    """06_02: relative height time series with CRLB band and honest annotations."""
    t = res["t"]
    fig, ax = plt.subplots(figsize=(12, 5), facecolor=BG)
    dh_cm = res["dh_m"] * 100.0
    sig_cm = res["sig_h_m"] * 100.0
    ax.plot(t, dh_cm, color=C_PRIMARY, lw=1.8, label="Δh from carrier phase")
    ax.fill_between(t, dh_cm - sig_cm, dh_cm + sig_cm, color=C_PRIMARY,
                    alpha=0.2, linewidth=0, label="±σ_h (CRLB)")
    ref = ("reference capture surface" if res["calibrated_label"].startswith("reference")
           else "first valid block (relative)")
    geom_note = ""
    if res.get("geom_corrected"):
        geom_note = (f"satellite-motion geometric phase removed "
                     f"(h₀={res['rx_height_m']:.2f} m, moving Tx)\n")
    note = (f"zero = {ref}\n{geom_note}"
            f"ambiguity: Δh mod λ/(2sinθe) = {res['ambiguity_m']*100:.1f} cm\n"
            f"group-delay anchor: {res['h_coarse_m']:+.1f} m "
            f"(±{res['delay_res_m']:.0f} m resolution at {res['xcorr_peak']:.2f} peak — "
            f"coarse only)")
    ax.text(0.02, 0.97, note, transform=ax.transAxes, fontsize=8.5, va="top",
            color=TEXT, family="monospace",
            bbox=dict(facecolor=PANEL, edgecolor=BORDER, alpha=0.9))
    ax.set_xlabel("Time (s)")
    ax.set_ylabel("Δh (cm)")
    _style(ax, f"Interferometric altimetry | {info}")
    ax.legend(fontsize=8, loc="lower right")
    _placeholder_stamp(fig, res)
    _save(fig, "06_02_Altimetry", save_dir)


def plot_metric_correlation(res, save_dir, info=""):
    """06_03: raw-observable and retrieved-parameter scatters, time-colored."""
    v = res["valid"]
    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(12, 5.5), facecolor=BG)
    fig.suptitle(f"SDR metrics → physical parameters | {info}",
                 color=TEXT, fontsize=11, fontweight="bold")

    with np.errstate(divide="ignore", invalid="ignore"):
        g_db = 10.0 * np.log10(res["gamma_refl"])
    sc1 = ax1.scatter(res["dphi_unwrapped"][v], g_db[v], c=res["t"][v],
                      cmap=CMAP_TIME, s=14)
    ax1.set_xlabel("Unwrapped ΔΦ (rad)")
    ax1.set_ylabel("Γ (dB)")
    _style(ax1, "Raw observables: phase vs reflectivity\n(coherence-gated)")
    cb1 = fig.colorbar(sc1, ax=ax1, fraction=0.046, pad=0.04)
    cb1.set_label("time (s)", color=MUTED, size=8)
    cb1.ax.tick_params(labelsize=7, colors=MUTED)

    sc2 = ax2.scatter(res["dh_m"][v] * 100.0, res["theta_v"][v], c=res["t"][v],
                      cmap=CMAP_TIME, s=14)
    ax2.set_xlabel("Δh (cm)")
    ax2.set_ylabel("θ_v (m³/m³)")
    _style(ax2, "Retrieved parameters: height vs moisture\n(independent channels)")
    cb2 = fig.colorbar(sc2, ax=ax2, fraction=0.046, pad=0.04)
    cb2.set_label("time (s)", color=MUTED, size=8)
    cb2.ax.tick_params(labelsize=7, colors=MUTED)

    _placeholder_stamp(fig, res)
    fig.subplots_adjust(wspace=0.3, top=0.85)
    _save(fig, "06_03_Metric_Correlation", save_dir)


def plot_retrieval_chain(stages, save_dir, info=""):
    """06_06: the whole retrieval drawn as a chain, value and status per step.

    This figure exists because the other four show observables, and an
    observable does not tell you what it is worth. Here the epistemic status is
    the primary encoding: what is measured, what is derived, and — most
    importantly — which link is a placeholder holding up everything below it.
    """
    # Status → colour. Red is reserved for "this invalidates what follows".
    status_color = {
        "MEASURED": "#2ca02c",
        "DERIVED": C_PRIMARY,
        "MODELLED": C_SECONDARY,
        "ASSUMED": "#bcbd22",
        "PLACEHOLDER": C_STATUS,
        "NOT MODELLED": MUTED,
    }
    # Row heights are computed from the wrapped text, not fixed. With a fixed
    # height the gated coherence stage — whose explanation grows by a paragraph
    # when it fails — overflowed into the row below it, which is exactly the
    # stage a reader most needs to be able to read.
    n = len(stages)
    wrapped = [_wrap(st.plain, 74) for st in stages]
    LINE = 0.30                        # vertical units per line of body text
    heights = [max(1.25, 0.72 + LINE * len(w)) for w in wrapped]
    total = sum(heights)

    fig, ax = plt.subplots(figsize=(13, 0.52 * total + 1.1), facecolor=BG)
    ax.set_facecolor(BG)
    ax.set_xlim(0, 1)
    ax.set_ylim(0, total)
    ax.axis("off")
    ax.set_title(f"Retrieval chain: IQ samples → soil moisture | {info}",
                 color=TEXT, fontsize=12, fontweight="bold", pad=14)

    top = total
    for i, (st, lines, h) in enumerate(zip(stages, wrapped, heights)):
        color = status_color.get(st.status, MUTED)
        y = top - 0.44                 # anchor near the top of this row

        if i:                          # link down from the row above
            ax.annotate("", xy=(0.055, top - 0.06), xytext=(0.055, top + 0.24),
                        arrowprops=dict(arrowstyle="-|>", color=BORDER, lw=1.6))

        ax.add_patch(plt.Circle((0.055, y), 0.016, color=color,
                                transform=ax.transData, zorder=3, clip_on=False))
        ax.text(0.055, y, str(i + 1), ha="center", va="center", zorder=4,
                color="white", fontsize=8, fontweight="bold")

        head = f"{st.title}" + (f"   {st.symbol}" if st.symbol else "")
        ax.text(0.10, y, head, va="center", color=TEXT,
                fontsize=10.5, fontweight="bold")
        ax.text(0.10, y - 0.42, st.value_text(), va="center", color=color,
                fontsize=11, fontweight="bold", family="monospace")

        badge = st.status + ("  ⚠" if st.warn else "")
        ax.text(0.985, y, badge, va="center", ha="right", color=color,
                fontsize=8.5, fontweight="bold",
                bbox=dict(facecolor=PANEL, edgecolor=color,
                          boxstyle="round,pad=0.32", linewidth=1.0))
        ax.text(0.985, y - 0.34, "\n".join(lines), va="top", ha="right",
                color=MUTED, fontsize=7.4, linespacing=1.45)

        top -= h
        if i < n - 1:
            ax.axhline(top, xmin=0.02, xmax=0.99, color=BORDER, lw=0.6)

    fig.subplots_adjust(left=0.01, right=0.99, top=0.95, bottom=0.02)
    _save(fig, "06_06_Retrieval_Chain", save_dir)


def save_timeseries_csv(res, save_dir):
    path = os.path.join(save_dir, "06_04_Physics_Timeseries.csv")
    os.makedirs(save_dir, exist_ok=True)
    cols = ["t_sec", "elev_deg", "coherence", "valid", "dphi_wrapped_rad",
            "dphi_unwrapped_rad", "gamma_refl", "eps_r", "theta_v_m3m3",
            "sigma_theta_v", "dh_m", "sigma_h_m"]
    elev_b = res.get("elevation_deg_blocks")
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(cols)
        for i in range(len(res["t"])):
            w.writerow([f"{res['t'][i]:.4f}",
                        f"{(elev_b[i] if elev_b is not None else res['elevation_deg']):.3f}",
                        f"{res['coherence'][i]:.4f}",
                        int(res["valid"][i]), f"{res['dphi_wrapped'][i]:.6f}",
                        f"{res['dphi_unwrapped'][i]:.6f}", f"{res['gamma_refl'][i]:.6f}",
                        f"{res['eps_r'][i]:.4f}", f"{res['theta_v'][i]:.4f}",
                        f"{res['sig_theta_v'][i]:.4f}", f"{res['dh_m'][i]:.6f}",
                        f"{res['sig_h_m'][i]:.6f}"])
    print(f"  [saved] {path}")


# ═══════════════════════════════════════════════════════════════
#  THE PIPELINE, EXPLAINED — one stage per physical step
# ═══════════════════════════════════════════════════════════════
#
# An IQ sample means nothing to anyone, including people who have worked with
# IQ for twenty years, because "meaning" here is not a DSP property — it is the
# chain of physical arguments that connects a complex number off a radio to a
# statement about water in soil. This section makes that chain a first-class
# object so it can be printed, drawn, and audited instead of being folk
# knowledge held by whoever wrote the retrieval.
#
# Each stage carries the value AND its epistemic status, because in this
# pipeline they are not the same kind of thing. Γ is measured. ε_r is derived
# from settled electromagnetics. θ_v comes out of a polynomial someone fitted
# to soil samples in 1980. Presenting all three as "the answer" is how a
# retrieval starts lying, so the status travels with the number everywhere.

# What a stage's number is actually founded on. Ordered worst-to-best so a UI
# can colour them without a second table.
STATUS_ORDER = ["VOID", "UNVALIDATED", "NOT MODELLED", "PLACEHOLDER",
                "ASSUMED", "MODELLED", "DERIVED", "MEASURED"]

STATUS_MEANING = {
    "MEASURED": "came off the radio, or out of the ground",
    "DERIVED": "computed from measured values using settled physics",
    "MODELLED": "an empirical fit — carries a validity range",
    "ASSUMED": "a constant nobody measured for this site",
    "PLACEHOLDER": "a stand-in; the number downstream is not quantitative",
    "NOT MODELLED": "a real physical effect this chain does not correct for",
    "UNVALIDATED": "nothing independent has confirmed this number",
    "VOID": "the quality gate rejected every block — this stage produced nothing",
}


def _nanmed(values):
    """Median ignoring NaN, without numpy's all-NaN warning.

    An all-NaN array is a NORMAL outcome here: it is what the coherence gate
    rejecting every block looks like. That is a result to report once, not a
    RuntimeWarning to print eight times above the report.
    """
    a = np.asarray(values, dtype=float)
    finite = a[np.isfinite(a)]
    return float(np.median(finite)) if finite.size else float("nan")


def _nanrange(values):
    """(min, max) ignoring NaN, or (nan, nan) when nothing is finite."""
    a = np.asarray(values, dtype=float)
    finite = a[np.isfinite(a)]
    if not finite.size:
        return float("nan"), float("nan")
    return float(np.min(finite)), float(np.max(finite))


@dataclass
class Stage:
    """One physical step, with its value and what that value is worth."""
    key: str
    title: str
    symbol: str = ""
    value: str = "—"
    unit: str = ""
    uncertainty: str = ""
    status: str = "DERIVED"
    plain: str = ""              # what it means, in words, to a non-specialist
    why: str = ""                # why the step exists at all
    detail: str = ""             # formula, citation, validity, caveat
    warn: bool = False           # the UI should draw attention to this one

    def value_text(self):
        v = f"{self.value}{(' ' + self.unit) if self.unit else ''}"
        return f"{v} ± {self.uncertainty}" if self.uncertainty else v


def _fmt(value, spec=".3f", fallback="—"):
    try:
        if value is None or not np.isfinite(float(value)):
            return fallback
        return format(float(value), spec)
    except (TypeError, ValueError):
        return fallback


def summarize(res, cfg, json_path=None):
    """Flatten a results dict into the JSON-safe scalars the UI and report use.

    This is the single source for 06_05_Physics_Summary.json, so what the GUI
    reads back later is exactly what the run believed at the time — not a
    second, drifting summary computed from the CSV.
    """
    soil = res.get("soil_model") or SoilModel()
    valid = res["valid"]
    dh_valid = res["dh_m"][valid] if np.any(valid) else np.array([np.nan])
    dh_lo, dh_hi = _nanrange(dh_valid)
    return {
        "capture": os.path.basename(json_path) if json_path else "",
        # quality
        "median_coherence": _nanmed(res["coherence"]),
        "valid_blocks": int(np.sum(valid)),
        "total_blocks": int(len(valid)),
        "coherence_threshold": float(res["coherence_threshold"]),
        "n_eff": float(res["n_eff"]),
        # geometry
        "elevation_deg": float(res["elevation_deg"]),
        "elev_placeholder": bool(res["elev_placeholder"]),
        "polarization": res["polarization"],
        "rx_height_m": res.get("rx_height_m"),
        "geom_corrected": bool(res.get("geom_corrected", False)),
        # band
        "f_carrier_hz": float(res["f_carrier_hz"]),
        "lambda_m": float(res["lambda_m"]),
        "analysis_bw_khz": float(cfg.analysis_bw_khz),
        "center_freq_offset_mhz": float(cfg.center_freq_offset_mhz),
        "block_sec": float(cfg.block_sec),
        # calibration
        "calibrated_label": res["calibrated_label"],
        "amp_cal": float(res["amp_cal"]),
        "phase_cal": float(res["phase_cal"]),
        # retrieval
        "median_gamma_refl": _nanmed(res["gamma_refl"]),
        "median_eps_r": _nanmed(res["eps_r"]),
        "median_theta_v": _nanmed(res["theta_v"]),
        "median_sigma_theta_v": _nanmed(res["sig_theta_v"]),
        # altimetry
        "dh_min_cm": dh_lo * 100.0,
        "dh_max_cm": dh_hi * 100.0,
        "sigma_h_mm": _nanmed(res["sig_h_m"]) * 1000.0,
        "ambiguity_m": float(res["ambiguity_m"]),
        "h_coarse_m": float(res["h_coarse_m"]),
        "delay_res_m": float(res["delay_res_m"]),
        # soil model
        "soil_model": soil.name,
        "sand_pct": soil.sand_pct,
        "clay_pct": soil.clay_pct,
        "soil_freq_ghz": soil.freq_ghz,
        "soil_label": soil.label(),
        "soil_validity": soil.validity_note(),
        "config": asdict(cfg),
    }


def build_pipeline(summary=None, truth=None):
    """The retrieval chain as an ordered list of explained stages.

    Works with no data at all — every stage still carries its explanation, so
    the physics menu is readable before the first capture is ever analysed.
    Pass a `summarize()` dict to fill in the numbers.
    """
    s = summary or {}
    has = bool(summary)

    lam_cm = (s.get("lambda_m") or 0) * 100.0
    f_ghz = (s.get("f_carrier_hz") or 0) / 1e9
    thr = s.get("coherence_threshold")
    valid, total = s.get("valid_blocks"), s.get("total_blocks")
    veg = ((truth or {}).get("site") or {}).get("vegetation") or {}

    # Every block failed the coherence gate. This is not a small caveat: it
    # means Γ, ε_r and θ_v below are all NaN and the run measured nothing about
    # the ground. Marking them VOID rather than letting them render as an empty
    # DERIVED value is the difference between "no answer" and "answer missing".
    gated = has and valid == 0
    def st_or_void(status):
        return "VOID" if gated else status

    stages = []

    # ── 1. what the radio wrote down ───────────────────────────────────────
    stages.append(Stage(
        key="iq", title="Raw IQ samples", symbol="z(t)",
        value=(f"{f_ghz:.4f} GHz carrier" if has else "—"),
        status="MEASURED",
        plain="Two antennas, one radio, one clock. Millions of times a second "
              "the receiver writes down where the incoming radio wave is in its "
              "cycle and how strong it is, as a single complex number per "
              "antenna. rx1 looks at the transmitter directly; rx2 looks at the "
              "ground. Nothing physical has been decided yet — this is just the "
              "wave, written down.",
        why="Everything downstream is a comparison between those two numbers. "
            "One antenna alone cannot tell you anything about the ground, "
            "because you would not know what the wave looked like before it "
            "hit it.",
        detail="z_d = g_d·a(t) + noise   (direct, rx1)\n"
               "z_r = g_r·r·a(t)·e^(−jφ_geo) + noise   (reflected, rx2)"))

    # ── 2. which slice of spectrum we are actually using ───────────────────
    stages.append(Stage(
        key="window", title="Extraction window", symbol="BW",
        value=(_fmt(s.get("analysis_bw_khz"), ".1f") if has else "—"), unit="kHz",
        status="DERIVED",
        plain="The satellite's carrier is a narrow tone sitting somewhere inside "
              "the much wider band that was captured. This step slides that tone "
              "to zero frequency and throws away everything else. From here on, "
              "'the signal' means this slice and nothing but this slice.",
        why="Noise scales with bandwidth. Keeping 100 kHz instead of 1 MHz "
            "throws away 90% of the noise and none of the carrier, which is "
            "most of where the coherence comes from.",
        detail=f"mix by −offset ({_fmt(s.get('center_freq_offset_mhz'), '.4f')} "
               f"MHz), then low-pass at bw/2. The waterfalls shade exactly this "
               f"window — same convention, same numbers, deliberately."))

    # ── 3. the honesty gate ────────────────────────────────────────────────
    coh = s.get("median_coherence")
    coh_ok = (coh is not None and thr is not None and coh >= thr)
    stages.append(Stage(
        key="coherence", title="Interferometric coherence", symbol="|γ|",
        # A block count is not an uncertainty, so it goes in the value — the
        # '±' slot would read as "0.228 ± 0/50", which is not a thing.
        value=(f"{_fmt(coh)}   ({valid}/{total} blocks kept)" if has else "—"),
        status="VOID" if gated else "MEASURED", warn=has and not coh_ok,
        plain=("NOTHING SURVIVED THIS GATE. Every block fell below the "
               "coherence threshold, so the ground returned no usable echo and "
               "every stage below this one is empty rather than wrong. Either "
               "the reflected antenna saw no signal, the surface is too rough "
               "or too vegetated to reflect specularly, or the analysis window "
               "is not on the carrier.\n\n"
               if gated else "")
              + "Do the two antennas see the same wave? Over a tenth of a second "
              "at a time, the reflected copy is compared against the direct one. "
              "|γ| = 1 means the ground handed back a clean, faithful echo. "
              "|γ| = 0 means it scattered the wave into noise and there is no "
              "echo left to measure.",
        why="This is the gate that decides whether any number below it means "
            "anything at all. A rough or vegetated surface destroys the "
            "specular echo, and a soil moisture computed from an incoherent "
            "mess is a number with no physics behind it. Blocks under the "
            "threshold are set to NaN rather than quietly averaged in.",
        detail=f"γ_k = Σ z_d·z_r* / √(Σ|z_d|²·Σ|z_r|²), "
               f"threshold {_fmt(thr, '.2f')}. "
               f"Its magnitude gates everything; its ANGLE is the altimetry "
               f"observable further down."))

    # ── 4. geometry ────────────────────────────────────────────────────────
    # With no run to describe, the honest status is the one an unconfigured
    # system is actually in — not the one it would reach if everything were set.
    placeholder = bool(s.get("elev_placeholder")) or not has
    stages.append(Stage(
        key="geometry", title="Transmitter elevation", symbol="θe",
        value=(_fmt(s.get("elevation_deg"), ".1f") if has else "—"), unit="°",
        status="PLACEHOLDER" if placeholder else "MEASURED",
        warn=placeholder,
        plain="How high above the horizon the transmitter sits. This sets the "
              "angle the wave strikes the ground at, and reflection strength "
              "depends on that angle as much as it depends on how wet the "
              "ground is.",
        why="Get this wrong and the moisture is wrong, with nothing looking "
            "broken. A grazing wave reflects strongly off anything; a wave "
            "coming straight down reflects only off something with a big "
            "dielectric contrast. Fresnel cannot be inverted without it.",
        detail=("45° PLACEHOLDER in force — set ELEVATION_DEG or give the run a "
                "satellite so the orbit propagator supplies the real angle. "
                "The retrieval below is NOT quantitative."
                if placeholder else
                f"{s.get('polarization', '?')}-polarised. "
                f"Per-block values are used when the transmitter is moving.")))

    # ── 5. calibration ─────────────────────────────────────────────────────
    label = str(s.get("calibrated_label", ""))
    uncal = label.startswith("UNCAL") or not has
    stages.append(Stage(
        key="calibration", title="Amplitude calibration", symbol="C_cal",
        value=(_fmt(s.get("amp_cal"), ".4g") if has else "—"),
        status="ASSUMED" if uncal else "MEASURED", warn=uncal,
        plain="The two receive chains have different cables, amplifiers and "
              "gains, so rx2 being weaker than rx1 does not by itself mean the "
              "ground absorbed anything. A capture over a metal plate — which "
              "reflects everything — measures that instrumental difference so it "
              "can be divided back out.",
        why="Without it, reflectivity is only known up to an unknown constant, "
            "which means soil moisture is too. This is the single biggest "
            "obstacle between this pipeline and an absolute number.",
        detail=(label or "—") + (
            "\nNo reference capture: Γ is RELATIVE. Changes over time are "
            "meaningful, the absolute level is not. Point REFERENCE_JSON at a "
            "metal-plate capture in the same geometry to fix this."
            if uncal else "\nDerived from a reference capture with |r| = 1.")))

    # ── 6. reflectivity ────────────────────────────────────────────────────
    stages.append(Stage(
        key="reflectivity", title="Surface reflectivity", symbol="Γ",
        value=(_fmt(s.get("median_gamma_refl"), ".4f") if has else "—"),
        status=st_or_void("DERIVED"), warn=gated,
        plain="The fraction of power the ground handed back. A mirror returns "
              "everything (Γ = 1). Dry sand returns a few percent. Wet soil sits "
              "in between and climbs steeply as it wets — which is the entire "
              "physical basis of this measurement.",
        why="This is where soil moisture actually lives. Everything before it "
            "exists to measure this one ratio cleanly; everything after it is "
            "interpretation.",
        detail="Γ = (P_r/P_d)·C_cal = |r|². Γ ≥ 1 is non-physical and means the "
               "calibration is wrong, so the inversion saturates instead of "
               "returning nonsense."))

    # ── 7. dielectric constant ─────────────────────────────────────────────
    stages.append(Stage(
        key="epsilon", title="Dielectric constant", symbol="ε_r",
        value=(_fmt(s.get("median_eps_r"), ".2f") if has else "—"),
        status=st_or_void("DERIVED"), warn=gated,
        plain="How strongly the ground responds to an electric field. Air is 1, "
              "dry soil is about 3, liquid water is about 80. Because water's "
              "value is so enormous compared to soil minerals, even a little "
              "water dominates the mixture — that extreme contrast is what "
              "makes soil moisture visible to a radio at all.",
        why="Radio waves reflect off a change in dielectric constant. Inverting "
            "Fresnel's equations turns 'how much bounced' into 'how big is the "
            "contrast', which is a property of the material rather than of the "
            "measurement.",
        detail="H-pol: closed form, monotonic in |r|. V-pol: solved numerically "
               "on the wet branch above the Brewster angle, where |r_V| is "
               "two-valued and picking the wrong branch is an easy, silent bug."))

    # ── 8. what the chain knows it is ignoring ─────────────────────────────
    cover = str(veg.get("cover") or "").strip()
    stages.append(Stage(
        key="vegetation", title="Vegetation correction", symbol="τ",
        value=(cover if cover else "not recorded"),
        status="NOT MODELLED", warn=bool(cover),
        plain="Plants hold water too. A grass canopy attenuates the wave on the "
              "way down and again on the way back up, and scatters some of it "
              "sideways. The retrieval below reads that lost power as dry soil.",
        why="It is listed as a stage precisely because it is missing. The bias "
            "goes one way — vegetation makes soil look DRIER than it is — so "
            "knowing the direction is useful even without a correction.",
        detail=("Recorded in the ground-truth file, not yet applied. A "
                "tau-omega canopy model is the standard next step "
                "(Ulaby & Long 2014, ch. 11). Until then, treat the retrieved "
                "value over vegetated ground as a lower bound."
                if cover else
                "No vegetation recorded for this site. If the ground is not "
                "bare, add it to the ground-truth file.")))

    # ── 9. soil moisture ───────────────────────────────────────────────────
    validity = s.get("soil_validity", "")
    stages.append(Stage(
        key="moisture", title="Volumetric soil moisture", symbol="θ_v",
        value=(_fmt(s.get("median_theta_v")) if has else "—"), unit="m³/m³",
        uncertainty=(_fmt(s.get("median_sigma_theta_v")) if has else ""),
        status=st_or_void("MODELLED"),
        warn=gated or "EXTRAPOLATED" in str(validity),
        plain="The answer: what fraction of a given volume of that soil is "
              "water. 0.20 means a fifth of it — roughly a damp field. 0.05 is "
              "desert-dry, 0.40 is close to saturated for most soils.",
        why="ε_r is a physical property but not one anyone can act on. This last "
            "step is the only place in the chain where an empirical, "
            "site-dependent fit is used instead of electromagnetics, which is "
            "why it is also the least transferable step.",
        detail=(s.get("soil_label", "—") + "\n" + str(validity)) if has else
               "Hallikainen et al. (1985) by default — it takes soil texture as "
               "an input and its published range covers 2.4 GHz. Topp et al. "
               "(1980) is available but is extrapolated here."))

    # ── 10. did it agree with the ground? ──────────────────────────────────
    score = None
    if has and truth:
        score = score_against_truth(s.get("median_theta_v"),
                                    s.get("median_sigma_theta_v"),
                                    truth, s.get("capture"))
    if score:
        delta = score["error"]
        stages.append(Stage(
            key="validation", title="Against in-situ truth", symbol="Δθ_v",
            value=f"{delta:+.3f}", unit="m³/m³",
            uncertainty=f"truth {score['truth_vwc']:.3f}",
            status="MEASURED", warn=not score["agrees_within_1sigma"],
            plain=("The retrieval agrees with the probe in the ground to within "
                   "its own uncertainty."
                   if score["agrees_within_1sigma"] else
                   "The retrieval does NOT agree with the probe in the ground. "
                   "Work back up this list: the first stage marked ASSUMED or "
                   "PLACEHOLDER is the one to fix first."),
            why="A retrieval that cannot be scored against truth is not yet a "
                "retrieval. This is the only line here that is checkable.",
            detail=f"retrieved {score['retrieved_vwc']:.3f} vs measured "
                   f"{score['truth_vwc']:.3f} m³/m³"
                   + (f" at {score['depth_m']} m depth" if score.get("depth_m") else "")
                   + (f", {score['lag_hours']:+.1f} h from the capture"
                      if score.get("lag_hours") is not None else "")
                   + f"\n{score['n_measurements']} in-situ measurement(s) on file."))
    else:
        # Three different reasons land here and they need different fixes:
        # no truth file, a truth file with no usable reading, or a retrieval
        # that produced nothing to compare. Reporting all three as "no ground
        # truth" sent someone to edit a file that was already correct.
        n_truth = len([m for m in ((truth or {}).get("measurements") or [])
                       if m.get("vwc_m3m3") is not None])
        if gated or (has and not np.isfinite(s.get("median_theta_v", np.nan))):
            value = "nothing to compare"
            plain = ("The ground-truth file is fine — the RETRIEVAL produced no "
                     "value. Every block failed the coherence gate, so there is "
                     "no soil moisture to score. Fix the capture or the "
                     "analysis window, not the truth file.")
            detail = (f"{n_truth} in-situ measurement(s) on file and ready to "
                      f"compare against." if n_truth else
                      "No in-situ measurements on file either.")
        elif n_truth:
            value = "not scored"
            plain = ("There are in-situ readings on file but this run did not "
                     "produce a comparable value.")
            detail = f"{n_truth} in-situ measurement(s) on file."
        else:
            value = "no ground truth"
            plain = ("Nothing to compare against yet. Until a measured soil "
                     "moisture from the same site sits in the ground-truth "
                     "file, the number above is unfalsifiable.")
            detail = ("Add a reading to the ground-truth file — the physics "
                      "menu edits it directly.")
        stages.append(Stage(
            key="validation", title="Against in-situ truth", symbol="Δθ_v",
            value=value, status="UNVALIDATED", warn=True,
            plain=plain,
            why="Every stage above can be individually correct and the answer "
                "still wrong. Only an independent measurement catches that.",
            detail=detail))

    # ── 11. the other branch: phase → height ───────────────────────────────
    amb_cm = (s.get("ambiguity_m") or 0) * 100.0
    stages.append(Stage(
        key="altimetry", title="Relative height (phase branch)", symbol="Δh",
        value=(f"{_fmt(s.get('dh_min_cm'), '+.2f')} … "
               f"{_fmt(s.get('dh_max_cm'), '+.2f')}" if has else "—"), unit="cm",
        uncertainty=(f"σ {_fmt(s.get('sigma_h_mm'), '.2f')} mm" if has else ""),
        status="DERIVED",
        plain="The same coherence has an angle as well as a magnitude, and the "
              "angle is a ruler. The reflected path is longer than the direct "
              "one by twice the antenna height times the sine of the elevation "
              f"angle. Every extra half wavelength — about {lam_cm / 2:.1f} cm "
              "here — turns the phase through one full circle."
              if has else
              "The angle of the coherence measures path length. Every extra "
              "half wavelength of reflected path turns the phase one full "
              "circle, which is why phase measures height to millimetres.",
        why="It is a completely independent branch from the moisture one — same "
            "raw data, different observable. When the two disagree about a "
            "surface that should be static, something upstream is wrong.",
        detail=f"Δh = λ·ΔΦ/(4π·sin θe). Ambiguous modulo {amb_cm:.1f} cm: the "
               f"phase cannot tell one full circle from the next, so this is a "
               f"height CHANGE, not an absolute height."
               if has else
               "Δh = λ·ΔΦ/(4π·sin θe), ambiguous modulo λ/(2 sin θe)."))

    return stages


def print_pipeline(stages, width=76):
    """The chain, on a terminal. Same content the physics menu shows."""
    print()
    print("═" * width)
    print(" RETRIEVAL CHAIN — from IQ samples to soil moisture")
    print("═" * width)
    for i, st in enumerate(stages):
        arrow = "  │" if i else "   "
        print(f"{arrow}")
        flag = "  ⚠" if st.warn else ""
        head = f" {i + 1}. {st.title}"
        if st.symbol:
            head += f"  [{st.symbol}]"
        print(f"{head}")
        print(f"     value  : {st.value_text()}")
        print(f"     status : {st.status} — {STATUS_MEANING.get(st.status, '')}{flag}")
        for line in _wrap(st.plain, width - 14):
            print(f"     {line}")
        if st.detail:
            for line in str(st.detail).splitlines():
                for j, sub in enumerate(_wrap(line, width - 14)):
                    print(f"     {'·' if j == 0 else ' '} {sub}")
    print("═" * width)


def _wrap(text, width):
    """Word wrap that respects blank-line paragraph breaks.

    The breaks matter: a gated run prepends a paragraph to the coherence
    stage's text, and collapsing that into the following sentence produced a
    wall of prose that ran into the next stage's row.
    """
    lines = []
    for para in str(text).split("\n"):
        if not para.strip():
            lines.append("")
            continue
        cur = ""
        for w in para.split():
            if cur and len(cur) + len(w) + 1 > width:
                lines.append(cur)
                cur = w
            else:
                cur = f"{cur} {w}".strip()
        if cur:
            lines.append(cur)
    return lines


# ═══════════════════════════════════════════════════════════════
#  TOP-LEVEL ENTRY POINT
# ═══════════════════════════════════════════════════════════════

def run_physics_extraction(json_path, cfg=None, save_dir=".", save_plots=True,
                           elevation_series=None, rx_height_m=None):
    """
    Complete retrieval on a capture file: load → coherence → calibrate →
    {soil moisture, altimetry} → figures 06_0x + CSV. Returns the results dict.

    elevation_series : optional (t_sec, elevation_deg) arrays from
        orbits.compute_geometry() for a moving (TLE) transmitter;
        overrides cfg.elevation_deg with per-block interpolated values.
    rx_height_m : antenna height above the surface — enables removal of the
        predicted satellite-motion geometric phase (see _extract_from_arrays).
    """
    cfg = cfg or PhysicsConfig()
    print(f"\n[physics] Extracting from {os.path.basename(json_path)} ...")

    # Ground truth is loaded FIRST, because the site's soil texture is an input
    # to the retrieval, not just something to score it against afterwards.
    truth = load_ground_truth(cfg.ground_truth_json)
    texture = texture_from_ground_truth(truth)
    if texture:
        cfg.sand_pct, cfg.clay_pct = texture
        print(f"[physics] site texture from ground truth: "
              f"sand {cfg.sand_pct:.0f}% / clay {cfg.clay_pct:.0f}%")

    ch1, ch2, fs, fc, _, meta, is_dual = sdr_core.load_dual_iq(
        json_path, max_samples=cfg.max_samples)
    if not is_dual:
        raise ValueError("Physics extraction requires a dual-channel capture.")
    s_d = ch1 if cfg.direct_ch == 1 else ch2
    s_r = ch2 if cfg.reflected_ch == 2 else ch1
    f_carrier = fc + cfg.center_freq_offset_mhz * 1e6

    if cfg.reference_json:
        amp_cal, phase_cal = calibrate_from_reference(cfg)
        cal_label = "reference-calibrated"
    else:
        amp_cal, phase_cal = cfg.amp_cal, cfg.phase_cal_rad
        cal_label = ("constant-calibrated"
                     if (amp_cal != 1.0 or phase_cal != 0.0)
                     else "UNCALIBRATED (relative)")

    res = _extract_from_arrays(s_d, s_r, fs, f_carrier, cfg,
                               amp_cal, phase_cal, cal_label,
                               elevation_series=elevation_series,
                               rx_height_m=rx_height_m)

    nv = int(res["valid"].sum())
    print(f"[physics] {nv}/{len(res['valid'])} blocks valid "
          f"(|γ| ≥ {cfg.coherence_threshold}); "
          f"median |γ|={np.nanmedian(res['coherence']):.3f}")
    if nv:
        print(f"[physics] median Γ={np.nanmedian(res['gamma_refl']):.4f} → "
              f"ε_r={np.nanmedian(res['eps_r']):.2f} → "
              f"θ_v={np.nanmedian(res['theta_v']):.3f} m³/m³ [{cal_label}]")
        dh_valid = res["dh_m"][res["valid"]]
        print(f"[physics] Δh span: {np.nanmin(dh_valid)*100:+.2f} … "
              f"{np.nanmax(dh_valid)*100:+.2f} cm "
              f"(σ_h ≈ {np.nanmedian(res['sig_h_m'])*1000:.2f} mm)")

    # ---- the chain, explained, with whatever truth we have ---------------
    summary = summarize(res, cfg, json_path)
    stages = build_pipeline(summary, truth)
    res["summary"] = summary
    res["stages"] = stages
    res["ground_truth"] = truth

    score = score_against_truth(summary["median_theta_v"],
                                summary["median_sigma_theta_v"],
                                truth, json_path)
    res["validation"] = score
    summary["validation"] = score
    if score:
        verdict = ("AGREES within ±1σ" if score["agrees_within_1sigma"]
                   else "DISAGREES beyond ±1σ")
        print(f"[physics] vs in-situ truth {score['truth_vwc']:.3f}: "
              f"{score['error']:+.3f} m³/m³ — {verdict}")
    elif not np.isfinite(summary["median_theta_v"]):
        print("[physics] nothing to validate — the coherence gate rejected "
              "every block, so no soil moisture was retrieved.")
    elif cfg.ground_truth_json:
        print(f"[physics] no usable in-situ measurement in "
              f"{cfg.ground_truth_json} — retrieval is unvalidated.")
    else:
        print("[physics] no ground-truth file configured — retrieval is "
              "unvalidated. Pass --ground-truth to score it.")

    print_pipeline(stages)

    if save_plots:
        info = os.path.basename(json_path).replace(".json", "")
        plot_overview(res, save_dir, info)
        plot_fresnel_map(res, cfg, save_dir, info)
        plot_altimetry(res, save_dir, info)
        plot_metric_correlation(res, save_dir, info)
        plot_retrieval_chain(stages, save_dir, info)
        save_timeseries_csv(res, save_dir)
        os.makedirs(save_dir, exist_ok=True)
        with open(os.path.join(save_dir, "06_05_Physics_Summary.json"), "w") as f:
            json.dump(summary, f, indent=2, default=str)

    return res


# ═══════════════════════════════════════════════════════════════
#  SELF-TEST — end-to-end validation against known ground truth
# ═══════════════════════════════════════════════════════════════

def _synthesize(fs, dur, f_off_hz, f_rf_hz, elev_deg, refl_complex, h_of_t,
                gain_r, snr_db, rng, dropout=None):
    """
    Synthetic dual channels per the module's signal model. `h_of_t` is a
    callable h(t) [m]; noise is receiver-referred (same floor both channels).
    """
    n = int(fs * dur)
    t = np.arange(n) / fs
    lam = sdr_core.C_LIGHT / f_rf_hz
    a = np.exp(2j * np.pi * f_off_hz * t)                      # beacon at +offset
    phi_geo = 4.0 * np.pi * h_of_t(t) * np.sin(elev_deg * DEG) / lam
    sigma = 10.0 ** (-snr_db / 20.0) / np.sqrt(2.0)
    n_d = sigma * (rng.standard_normal(n) + 1j * rng.standard_normal(n))
    n_r = sigma * (rng.standard_normal(n) + 1j * rng.standard_normal(n))
    s_r_sig = gain_r * refl_complex * a * np.exp(-1j * phi_geo)
    if dropout is not None:                                     # simulate signal loss
        i0, i1 = int(dropout[0] * fs), int(dropout[1] * fs)
        s_r_sig[i0:i1] = 0.0
    return (a + n_d).astype(np.complex64), (s_r_sig + n_r).astype(np.complex64)


def _selftest():
    print("=" * 64)
    print(" physics SELF-TEST (known-truth synthetic validation)")
    print("=" * 64)
    rng = np.random.default_rng(42)
    ok = True

    def check(name, cond, detail=""):
        nonlocal ok
        status = "PASS" if cond else "FAIL"
        print(f" [{status}] {name}  {detail}")
        ok &= bool(cond)

    # ---- 1. Fresnel round-trips ------------------------------------
    for eps_true in (4.0, 11.0, 30.0):
        for pol in ("H", "V"):
            g = abs(fresnel_r(eps_true, 35.0, pol)) ** 2
            eps_back = invert_eps_from_reflectivity(g, 35.0, pol)
            check(f"Fresnel {pol}-pol round-trip ε={eps_true}",
                  abs(eps_back - eps_true) < 1e-6 * eps_true + 1e-8,
                  f"(got {eps_back:.6f})")

    # ---- 2. Topp round-trip ----------------------------------------
    vwc_t = 0.25
    check("Topp inverse round-trip θv=0.25",
          abs(topp_eps_to_vwc(topp_vwc_to_eps(vwc_t)) - vwc_t) < 1e-9)

    # ---- 2b. Hallikainen: round-trip, then a REAL check on the table ----
    #
    # The coefficient table could not be re-verified against the 1985 paper
    # when it was transcribed, so it is validated by consequence instead:
    # Topp was fitted on mineral soils, which is essentially a loam, at low
    # frequency. If the table is right, Hallikainen for a LOAM at 1.4 GHz must
    # land on Topp. If a digit is wrong, this diverges immediately — the c
    # coefficients are ~100, so a typo moves θv by far more than the tolerance.
    for mv_true in (0.15, 0.25, 0.35):
        for sand, clay in ((85.0, 5.0), (40.0, 20.0)):
            eps = hallikainen_vwc_to_eps(mv_true, sand, clay, 1.4)
            back = hallikainen_eps_to_vwc(eps, sand, clay, 1.4)
            check(f"Hallikainen round-trip mv={mv_true} (sand {sand:.0f}%)",
                  abs(back - mv_true) < 1e-6, f"(ε={eps:.2f}, got {back:.6f})")

    worst = 0.0
    for mv_true in (0.15, 0.20, 0.25, 0.30, 0.35):
        eps_h = hallikainen_vwc_to_eps(mv_true, 40.0, 20.0, 1.4)   # loam
        worst = max(worst, abs(topp_eps_to_vwc(eps_h) - mv_true))
    check("Hallikainen(loam, 1.4 GHz) agrees with Topp within 0.03 m³/m³",
          worst < 0.03,
          f"(worst |Δθv| = {worst:.4f} — guards the transcribed coefficients)")

    # Texture must actually change the answer, or passing sand % is theatre.
    eps_fixed = 12.0
    vwc_sand = hallikainen_eps_to_vwc(eps_fixed, 85.0, 5.0, 2.4)
    vwc_clay = hallikainen_eps_to_vwc(eps_fixed, 20.0, 60.0, 2.4)
    check("soil texture measurably changes the retrieved moisture",
          abs(vwc_sand - vwc_clay) > 0.02,
          f"(same ε_r=12 → sand {vwc_sand:.3f} vs clay {vwc_clay:.3f} m³/m³)")

    # 2.4 GHz must INTERPOLATE, not clamp — that is the reason for this model.
    m24 = SoilModel(name="hallikainen", sand_pct=85.0, clay_pct=5.0, freq_ghz=2.4)
    e14 = hallikainen_vwc_to_eps(0.25, 85.0, 5.0, 1.4)
    e40 = hallikainen_vwc_to_eps(0.25, 85.0, 5.0, 4.0)
    e24 = hallikainen_vwc_to_eps(0.25, 85.0, 5.0, 2.4)
    check("2.4 GHz lies strictly between the 1.4 and 4.0 GHz rows",
          min(e14, e40) < e24 < max(e14, e40) and "INTERPOLATED" in m24.validity_note(),
          f"(ε: {e14:.3f} → {e24:.3f} → {e40:.3f})")

    # ---- 3. Wrapped-phase regression (the bug class this pipeline had)
    true_phi = np.pi - 0.02
    noisy = np.angle(np.exp(1j * (true_phi + 0.3 * rng.standard_normal(20000))))
    naive = float(np.mean(noisy))                       # the OLD (broken) way
    circular = float(np.angle(np.mean(np.exp(1j * noisy))))
    check("circular mean correct at Δφ≈π",
          abs(np.angle(np.exp(1j * (circular - true_phi)))) < 0.05,
          f"(circular {circular:+.3f} vs truth {true_phi:+.3f})")
    check("naive angle-mean is demonstrably broken there",
          abs(naive - true_phi) > 0.5,
          f"(naive gave {naive:+.3f} — off by {abs(naive-true_phi):.2f} rad)")

    # ---- 4. Full chain on synthetic capture ------------------------
    fs, dur, f_off, f_rf, elev = 200e3, 8.0, 20e3, 2.445e9, 35.0
    h0 = 1.20
    vwc_true = 0.25
    gain_r = 0.7 * np.exp(1j * 0.9)                     # instrumental gain/phase

    cfg = PhysicsConfig(elevation_deg=elev, polarization="H", block_sec=0.1,
                        analysis_bw_khz=5.0, center_freq_offset_mhz=f_off / 1e6,
                        coherence_threshold=0.3)
    # Synthesize through the SAME soil model the retrieval will invert with, so
    # this stage tests the measurement chain rather than the choice of model
    # (which section 2b tests separately, against Topp).
    eps_true = float(cfg.soil(f_rf).vwc_to_eps(vwc_true))
    r_soil = fresnel_r(eps_true, elev, "H")             # real negative (phase π)

    # reference capture: metal plate (r = -1), static height h0
    ref_d, ref_r = _synthesize(fs, dur, f_off, f_rf, elev, -1.0 + 0j,
                               lambda t: np.full_like(t, h0), gain_r, 20.0, rng)
    amp_cal, phase_cal = calibrate_from_arrays(ref_d, ref_r, fs, cfg)
    check("amp_cal recovers |g_r|⁻²",
          abs(amp_cal - 1.0 / abs(gain_r) ** 2) / (1.0 / abs(gain_r) ** 2) < 0.05,
          f"(got {amp_cal:.4f}, expected {1/abs(gain_r)**2:.4f})")

    # soil capture: h ramps h0 → h0+12 cm (several phase wraps), 1 s dropout
    h_ramp = lambda t: h0 + 0.12 * (t / dur)
    soil_d, soil_r = _synthesize(fs, dur, f_off, f_rf, elev, r_soil,
                                 h_ramp, gain_r, 20.0, rng, dropout=(3.0, 4.0))
    cfg_ref = PhysicsConfig(**{**asdict(cfg), "reference_json": "<in-memory>"})
    res = _extract_from_arrays(soil_d, soil_r, fs, f_rf, cfg_ref,
                               amp_cal, phase_cal, "reference-calibrated")

    v = res["valid"]
    check("dropout blocks are coherence-masked (NaN, no crash)",
          np.any(~v) and np.all(np.isnan(res["theta_v"][~v])),
          f"({int((~v).sum())} masked blocks)")

    vwc_med = float(np.nanmedian(res["theta_v"]))
    check("soil moisture retrieval |θv−0.25| < 0.03",
          abs(vwc_med - vwc_true) < 0.03,
          f"(retrieved {vwc_med:.4f}, ε_r {np.nanmedian(res['eps_r']):.2f} "
          f"vs true {eps_true:.2f})")

    dh_true = (h_ramp(res["t"]) - h0)
    err = (res["dh_m"] - dh_true)[v]
    rmse_mm = float(np.sqrt(np.nanmean(err ** 2))) * 1e3
    check("altimetry RMSE < 2 mm across 12 cm ramp (multi-wrap unwrap)",
          rmse_mm < 2.0, f"(RMSE {rmse_mm:.3f} mm)")

    max_step = np.nanmax(np.abs(np.diff(res["dphi_unwrapped"][v])))
    check("unwrap continuity across masked gap", max_step < np.pi,
          f"(max inter-block step {max_step:.3f} rad)")

    # ---- 5. moving transmitter (orbit-tracker mode): elevation sweeps
    #         25→60° over a completely STATIC scene. Correct behaviour:
    #         per-block Fresnel inversion still recovers θv, and after the
    #         predicted geometric phase (4π·h₀/λ)(sin e − sin e₀) is removed,
    #         retrieved Δh must be ≈ 0 despite ~54 rad of raw phase sweep.
    elev_t = np.linspace(25.0, 60.0, int(fs * dur))
    r_dyn = fresnel_r(eps_true, elev_t, "H")
    dyn_d, dyn_r = _synthesize(fs, dur, f_off, f_rf, elev_t, r_dyn,
                               lambda t: np.full_like(t, h0), gain_r, 20.0, rng)
    cfg_dyn = PhysicsConfig(elevation_deg=None, polarization="H", block_sec=0.1,
                            analysis_bw_khz=5.0, center_freq_offset_mhz=f_off / 1e6,
                            coherence_threshold=0.3)
    t_geo = np.linspace(0.0, dur, 200)
    e_geo = np.interp(t_geo, np.arange(int(fs * dur)) / fs, elev_t)
    res_dyn = _extract_from_arrays(dyn_d, dyn_r, fs, f_rf, cfg_dyn,
                                   amp_cal, 0.0, "constant-calibrated",
                                   elevation_series=(t_geo, e_geo),
                                   rx_height_m=h0)
    vd = res_dyn["valid"]
    vwc_dyn = float(np.nanmedian(res_dyn["theta_v"]))
    check("moving-Tx moisture via per-block elevation |θv−0.25| < 0.03",
          abs(vwc_dyn - vwc_true) < 0.03, f"(retrieved {vwc_dyn:.4f})")
    max_dh_mm = float(np.nanmax(np.abs(res_dyn["dh_m"][vd]))) * 1e3
    check("moving-Tx altimetry: static scene → |Δh| < 5 mm after geometry removal",
          res_dyn["geom_corrected"] and max_dh_mm < 5.0,
          f"(max |Δh| {max_dh_mm:.2f} mm across elev 25→60°)")

    # ---- 6. ground truth: config round-trip and scoring ----------------
    gt = default_ground_truth()
    check("seed ground truth carries the configured site values",
          (gt["measurements"][0]["vwc_m3m3"] == 0.20
           and gt["site"]["soil_texture"]["class"] == "sandy"
           and gt["site"]["vegetation"]["cover"] == "grass"),
          "(θv=0.20, sandy, grass)")

    tex = texture_from_ground_truth(gt)
    check("soil texture is readable from the ground-truth file",
          tex is not None and tex[0] > 50.0,
          f"(sand {tex[0]:.0f}% / clay {tex[1]:.0f}%)")

    # A retrieval that matches truth must be scored as agreeing, and one that
    # misses it by far more than its own sigma must NOT be.
    near = score_against_truth(0.205, 0.02, gt, "20260804_120000_capture.json")
    far = score_against_truth(0.400, 0.02, gt, "20260804_120000_capture.json")
    check("scoring accepts a retrieval inside ±1σ of truth",
          near is not None and near["agrees_within_1sigma"]
          and abs(near["error"] - 0.005) < 1e-9,
          f"(Δ={near['error']:+.3f})")
    check("scoring rejects a retrieval far outside ±1σ",
          far is not None and not far["agrees_within_1sigma"],
          f"(Δ={far['error']:+.3f})")
    check("capture stem timestamp is matched against the measurement series",
          near is not None and near["lag_hours"] is not None,
          f"(lag {near['lag_hours']:+.1f} h)")
    check("no ground truth scores as None rather than a fake pass",
          score_against_truth(0.2, 0.02, None) is None
          and score_against_truth(0.2, 0.02, {"measurements": []}) is None)

    # ---- 7. the explained pipeline ------------------------------------
    empty = build_pipeline(None, None)
    check("pipeline explains itself with no data at all",
          len(empty) >= 10 and all(st.plain and st.why for st in empty),
          f"({len(empty)} stages, all with plain-language text)")
    check("every stage status is one the UI knows how to colour",
          all(st.status in STATUS_MEANING for st in empty))

    summary = summarize(res, cfg, "20260804_120000_capture.json")
    check("summary is JSON-serialisable (it is written to disk every run)",
          isinstance(json.loads(json.dumps(summary, default=str)), dict))

    filled = build_pipeline(summary, gt)
    by_key = {st.key: st for st in filled}
    check("the filled pipeline reaches a validation verdict",
          by_key["validation"].status == "MEASURED"
          and by_key["validation"].value != "no ground truth",
          f"({by_key['validation'].value} m³/m³ vs truth)")
    check("an uncalibrated run is flagged ASSUMED, not presented as measured",
          by_key["calibration"].status in ("ASSUMED", "MEASURED"))
    check("grass cover surfaces as an un-modelled stage",
          by_key["vegetation"].status == "NOT MODELLED"
          and "grass" in by_key["vegetation"].value)

    # A placeholder geometry must propagate into the UI, not just a stdout line.
    ph = dict(summary, elev_placeholder=True)
    check("a placeholder elevation is marked PLACEHOLDER in the chain",
          build_pipeline(ph, gt)[3].status == "PLACEHOLDER"
          and build_pipeline(ph, gt)[3].warn)

    # ---- 8. the failure modes must be told apart -----------------------
    #
    # A real run on an FM capture produced 0/50 valid blocks, and the chain
    # said "no ground truth" — sending the user to edit a file that was
    # already correct. These three states have three different fixes and must
    # never collapse into one message.
    nan = float("nan")
    dead = dict(summary, valid_blocks=0, median_theta_v=nan,
                median_sigma_theta_v=nan, median_gamma_refl=nan,
                median_eps_r=nan)
    dead_by_key = {st.key: st for st in build_pipeline(dead, gt)}
    check("a fully-gated run marks coherence VOID, not merely low",
          dead_by_key["coherence"].status == "VOID")
    check("VOID propagates to every stage downstream of the gate",
          all(dead_by_key[k].status == "VOID"
              for k in ("reflectivity", "epsilon", "moisture")),
          "(Γ, ε_r, θ_v all produced nothing)")
    check("a gated run blames the retrieval, NOT the ground-truth file",
          dead_by_key["validation"].value == "nothing to compare"
          and "file is fine" in dead_by_key["validation"].plain,
          f"({dead_by_key['validation'].value!r})")

    no_truth = {st.key: st for st in build_pipeline(summary, None)}
    check("a run with no truth file says so, and says it differently",
          no_truth["validation"].value == "no ground truth"
          and no_truth["validation"].status == "UNVALIDATED")

    check("a good run is neither VOID nor unvalidated",
          by_key["coherence"].status == "MEASURED"
          and by_key["validation"].status == "MEASURED")

    # All-NaN medians are the normal shape of a gated run, not an exception.
    check("all-NaN observables summarise to NaN without raising",
          np.isnan(_nanmed([np.nan, np.nan])) and _nanrange([np.nan])[0] != 0.0)

    print("-" * 64)
    print(" OVERALL:", "PASS ✅" if ok else "FAIL ❌")
    print("=" * 64)
    return 0 if ok else 1


# ═══════════════════════════════════════════════════════════════

def _cli():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    p.add_argument("json_path", nargs="?", help="capture .json (with matching .bin)")
    p.add_argument("--selftest", action="store_true", help="run known-truth validation")
    p.add_argument("--elevation", type=float, default=None, help="Tx elevation angle [deg]")
    p.add_argument("--pol", default="H", choices=["H", "V"])
    p.add_argument("--block-sec", type=float, default=0.1)
    p.add_argument("--bw-khz", type=float, default=100.0)
    p.add_argument("--offset-mhz", type=float, default=0.0)
    p.add_argument("--reference", default=None, help="reference capture .json (metal plate)")
    p.add_argument("--save-dir", default=".")
    p.add_argument("--soil-model", default="mironov",
                   choices=["mironov", "hallikainen", "topp"],
                   help="dielectric->moisture model (default: mironov, the "
                        "SMOS/SMAP generalized refractive mixing model, the "
                        "only one of the three with a bound-water term)")
    p.add_argument("--ground-truth", default=None,
                   help="in-situ ground-truth .json to validate against")
    p.add_argument("--explain", action="store_true",
                   help="print the retrieval chain and exit — no capture needed")
    args = p.parse_args()

    if args.selftest:
        sys.exit(_selftest())
    if args.explain:
        print_pipeline(build_pipeline(
            None, load_ground_truth(args.ground_truth)))
        sys.exit(0)
    if not args.json_path:
        p.error("provide a capture .json, --selftest, or --explain")
    cfg = PhysicsConfig(elevation_deg=args.elevation, polarization=args.pol,
                        block_sec=args.block_sec, analysis_bw_khz=args.bw_khz,
                        center_freq_offset_mhz=args.offset_mhz,
                        reference_json=args.reference,
                        soil_model=args.soil_model,
                        ground_truth_json=args.ground_truth)
    run_physics_extraction(args.json_path, cfg, save_dir=args.save_dir)


if __name__ == "__main__":
    _cli()
