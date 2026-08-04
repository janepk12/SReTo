"""
radios.py — which SDRs SReTo drives today, which are coming, and what changes.

WHY THIS FILE EXISTS
--------------------
"Does it work with my HackRF?" was being answered in three different places
with three different levels of optimism: the README said bladeRF, the
pre-checks said `bladeRF-cli not on PATH`, and nothing said whether another
radio was *possible*. This module is the single answer, and it is deliberately
data rather than prose so `sreto --radios`, the pre-check hints and the README
table cannot drift apart.

THE CONSTRAINT THAT DECIDES EVERYTHING
--------------------------------------
SReTo measures a **carrier phase difference between two antennas**. That is only
meaningful if both channels are sampled by ONE clock and one PLL — two
receivers, however well synchronised over PPS, drift in carrier phase and the
difference stops being a property of the ground. So a radio is either

    COHERENT PAIR   two RX channels from one reference  → the full measurement
    SINGLE CHANNEL  one RX chain                        → planning, direct-only
                                                          recording, replay and
                                                          analysis of existing
                                                          captures, but never
                                                          Level 1b onwards

A single-channel radio is therefore not "partial support pending work". It
cannot do the measurement at all, and this module says so rather than listing
it as a nearly-ready backend.

STATUS VALUES
-------------
    supported   drives real hardware today
    in-progress being built, on the roadmap with an owner
    planned     designed for, no code yet — "coming soon"
    limited     usable, but cannot make the phase measurement (single channel)

Nothing here talks to hardware. It is a catalogue; ``prechecks.check_bladerf``
is what actually probes a device.
"""

from dataclasses import dataclass, field

SUPPORTED = "supported"
IN_PROGRESS = "in-progress"
PLANNED = "planned"
LIMITED = "limited"

_STATUS_LABEL = {
    SUPPORTED: "supported",
    IN_PROGRESS: "in progress",
    PLANNED: "coming soon",
    LIMITED: "coming soon (single channel)",
}


@dataclass(frozen=True)
class Radio:
    """One SDR, and everything that changes about the command line for it."""

    key: str                    # what `--radio KEY` would take
    label: str                  # human name
    status: str
    rx_channels: int            # coherent RX chains from ONE clock
    host_tool: str              # the CLI binary SReTo would shell out to
    probe: str                  # command that answers "is it plugged in?"
    capture: str                # a representative capture invocation
    notes: str = ""
    requires: tuple = field(default_factory=tuple)   # host packages to install

    @property
    def coherent(self):
        """Can it make the two-antenna phase measurement at all?"""
        return self.rx_channels >= 2

    @property
    def status_label(self):
        return _STATUS_LABEL.get(self.status, self.status)

    def __str__(self):                                    # pragma: no cover
        return f"{self.label} ({self.status_label})"


# ── the catalogue ──────────────────────────────────────────────────────────
# `capture` strings are the real syntax of each host tool, written for
# 1575.42 MHz (GPS L1) at 2 Msps so they can be compared line for line. They
# are documentation of what the backend WOULD run, not something this module
# executes.
CATALOG = (
    Radio(
        key="bladerf",
        label="Nuand bladeRF (x40/x115/2.0 micro)",
        status=SUPPORTED,
        rx_channels=2,
        host_tool="bladeRF-cli",
        probe="bladeRF-cli -p",
        capture=(
            'bladeRF-cli -e "set frequency rx 1575420000" '
            '-e "set samplerate rx 2000000" -e "set bandwidth rx 1000000" '
            '-e "rx config file=capture.bin format=bin n=20M channel=1,2" '
            '-e "rx start" -e "rx wait"'
        ),
        notes=("The reference platform. Both RX chains share one PLL, which is "
               "the whole reason it was chosen. Driven through the science "
               "repository's capture.sh, not directly."),
        requires=("bladeRF host tools (libbladeRF)",),
    ),
    Radio(
        key="usrp",
        label="Ettus USRP B210 / N210 (UHD)",
        status=IN_PROGRESS,
        rx_channels=2,
        host_tool="rx_samples_to_file (UHD)",
        probe="uhd_find_devices",
        capture=(
            'rx_samples_to_file --args "type=b200" --freq 1575.42e6 '
            "--rate 2e6 --bw 1e6 --gain 40 --channels 0,1 "
            "--file capture.dat --duration 10"
        ),
        notes=("The most direct substitute: the B210's two RX channels are "
               "coherent by design, and UHD exposes the same "
               "frequency/rate/bandwidth/gain knobs capture.sh already asks "
               "for. Sample format is sc16 rather than bladeRF's interleaved "
               "bin, so the reader needs a format shim."),
        requires=("UHD host driver", "uhd_images_downloader"),
    ),
    Radio(
        key="rspduo",
        label="SDRplay RSPduo",
        status=PLANNED,
        rx_channels=2,
        host_tool="rx_sdr (SoapySDR)",
        probe='SoapySDRUtil --probe="driver=sdrplay"',
        capture=(
            "rx_sdr -d driver=sdrplay,mode=Dual -f 1575420000 -s 2000000 "
            "-b 1000000 -g 40 -n 20000000 capture.bin"
        ),
        notes=("Dual-tuner and genuinely coherent in its dual-tuner mode, and "
               "much cheaper than a USRP. Needs the proprietary sdrplay_api "
               "service running, which is the reason it is behind UHD in the "
               "queue."),
        requires=("SDRplay API v3", "SoapySDRPlay3", "rx_tools"),
    ),
    Radio(
        key="pluto",
        label="Analog Devices ADALM-Pluto (AD9361)",
        status=PLANNED,
        rx_channels=2,
        host_tool="iio_readdev (libiio)",
        probe="iio_info -u ip:192.168.2.1",
        capture=(
            "iio_attr -u ip:192.168.2.1 -c ad9361-phy RX_LO frequency 1575420000 && "
            "iio_readdev -u ip:192.168.2.1 -b 65536 -s 20000000 "
            "cf-ad9361-lpc > capture.bin"
        ),
        notes=("The AD9361 underneath is a 2x2 transceiver and the second RX "
               "chain is coherent, but a stock Pluto exposes only one until it "
               "is re-flashed to the 2r2t firmware. Support means documenting "
               "that step, not working around it."),
        requires=("libiio", "2r2t firmware for the second RX chain"),
    ),
    Radio(
        key="limesdr",
        label="Lime Microsystems LimeSDR (USB)",
        status=PLANNED,
        rx_channels=2,
        host_tool="rx_sdr (SoapySDR)",
        probe="LimeUtil --find",
        capture=(
            "rx_sdr -d driver=lime -f 1575420000 -s 2000000 -b 1000000 "
            "-g 40 -n 20000000 capture.bin"
        ),
        notes=("Two coherent RX channels on the full-size LimeSDR (the Mini "
               "has one, so the Mini is single-channel and cannot make the "
               "measurement). Would arrive together with the RSPduo through "
               "the same SoapySDR path."),
        requires=("LimeSuite", "SoapySDR", "rx_tools"),
    ),
    Radio(
        key="krakensdr",
        label="KrakenSDR (5x coherent RTL-SDR)",
        status=PLANNED,
        rx_channels=5,
        host_tool="Heimdall DAQ",
        probe="python3 -m krakensdr.daq --list",
        capture=(
            "heimdall_daq -c daq_chain_config.ini      # centre 1575420000, fs 2000000"
        ),
        notes=("Five phase-coherent channels behind one noise-source "
               "calibration. Overkill for a two-antenna experiment, but it is "
               "the cheapest coherent hardware that exists and would make the "
               "measurement reproducible on a hobby budget."),
        requires=("Heimdall DAQ firmware", "librtlsdr (Kraken fork)"),
    ),
    Radio(
        key="rtlsdr",
        label="RTL-SDR (RTL2832U)",
        status=LIMITED,
        rx_channels=1,
        host_tool="rtl_sdr",
        probe="rtl_test -t",
        capture="rtl_sdr -f 1575420000 -s 2048000 -g 40 -n 20480000 capture.bin",
        notes=("One tuner, one clock, 8-bit samples. Fine for pass planning "
               "and for recording the DIRECT signal to check that a satellite "
               "was where the planner said it was — but two dongles are not "
               "coherent, so there is no phase difference to measure. See "
               "KrakenSDR for the coherent version of the same silicon."),
        requires=("librtlsdr",),
    ),
    Radio(
        key="hackrf",
        label="HackRF One",
        status=LIMITED,
        rx_channels=1,
        host_tool="hackrf_transfer",
        probe="hackrf_info",
        capture=("hackrf_transfer -r capture.bin -f 1575420000 -s 8000000 "
                 "-a 1 -l 32 -g 16"),
        notes=("Single half-duplex chain and 8-bit samples. Wide tuning range "
               "makes it a good survey radio for finding an illuminator, and "
               "nothing more than that for this experiment."),
        requires=("hackrf host tools",),
    ),
    Radio(
        key="airspy",
        label="Airspy R2 / Mini",
        status=LIMITED,
        rx_channels=1,
        host_tool="airspy_rx",
        probe="airspy_info",
        capture="airspy_rx -r capture.bin -f 1575.42 -a 10000000 -t 0 -g 17",
        notes=("Single channel, and its tuning range stops below 1.8 GHz, so "
               "L-band GNSS is in reach but the S-band SoOp targets are not."),
        requires=("airspy host tools",),
    ),
    Radio(
        key="soapy",
        label="Any SoapySDR device (generic backend)",
        status=PLANNED,
        rx_channels=2,
        host_tool="rx_sdr (SoapySDR)",
        probe="SoapySDRUtil --find",
        capture=("rx_sdr -d driver=<name> -f 1575420000 -s 2000000 "
                 "-b 1000000 -g 40 -n 20000000 capture.bin"),
        notes=("The escape hatch. One backend that speaks SoapySDR covers "
               "Lime, SDRplay, Airspy and several others at once, at the cost "
               "of only reaching the settings SoapySDR exposes — which does "
               "not include the bias-tee and per-chain AGC that capture.sh "
               "asks about today."),
        requires=("SoapySDR", "the device's Soapy module", "rx_tools"),
    ),
)

DEFAULT = "bladerf"

_BY_KEY = {r.key: r for r in CATALOG}


def get(key):
    """The Radio with this key, or None."""
    return _BY_KEY.get(str(key or "").strip().lower())


def keys():
    return [r.key for r in CATALOG]


def by_status(status):
    return [r for r in CATALOG if r.status == status]


def supported():
    """Radios that can be used for a real capture right now."""
    return by_status(SUPPORTED)


def coming_soon():
    """Everything not yet usable, most-complete first."""
    order = {IN_PROGRESS: 0, PLANNED: 1, LIMITED: 2}
    return sorted((r for r in CATALOG if r.status != SUPPORTED),
                  key=lambda r: (order.get(r.status, 9), r.label))


def coherent_only():
    """Radios that could ever make the two-antenna phase measurement."""
    return [r for r in CATALOG if r.coherent]


def format_table(width=78):
    """The `sreto --radios` output: the matrix, then the per-radio commands."""
    lines = [
        "SReTo drives one radio today and is designed to drive more.",
        "",
        "The measurement needs TWO RX channels sampled by ONE clock. A "
        "single-channel",
        "radio can plan passes and record the direct signal, but it cannot "
        "produce a",
        "carrier phase difference, so it is listed as limited rather than "
        "as pending.",
        "",
    ]

    key_w = max(len(r.key) for r in CATALOG)
    label_w = max(len(r.label) for r in CATALOG)
    status_w = max(len(r.status_label) for r in CATALOG)
    header = (f"  {'radio'.ljust(key_w)}  {'device'.ljust(label_w)}  "
              f"{'status'.ljust(status_w)}  coherent RX")
    lines.append(header)
    lines.append("  " + "-" * (len(header) - 2))
    for r in CATALOG:
        chans = f"{r.rx_channels} ch" if r.coherent else "1 ch — no phase Δ"
        lines.append(f"  {r.key.ljust(key_w)}  {r.label.ljust(label_w)}  "
                     f"{r.status_label.ljust(status_w)}  {chans}")

    lines += ["", "", "What changes per radio — the host tool SReTo would "
                    "drive, and how it is called:", ""]
    for r in CATALOG:
        lines.append(f"  {r.label}  [{r.status_label}]")
        lines.append(f"    tool    : {r.host_tool}")
        lines.append(f"    probe   : {r.probe}")
        for i, chunk in enumerate(_wrap(r.capture, width - 14)):
            lines.append(f"    {'capture :' if i == 0 else '         '} {chunk}")
        if r.requires:
            lines.append(f"    needs   : {', '.join(r.requires)}")
        for chunk in _wrap(r.notes, width - 14):
            lines.append(f"              {chunk}")
        lines.append("")

    lines += [
        "Only the supported radios above are wired up. The rest are the "
        "roadmap, and",
        "`--radio` is not a flag yet — see the README's 'Supported radios' "
        "section for",
        "the interface these commands will be reached through.",
    ]
    return "\n".join(lines)


def _wrap(text, width):
    """Minimal greedy wrap. textwrap would do, but it re-flows the shell
    command examples across their quotes and makes them uncopyable."""
    words, line, out = str(text or "").split(), "", []
    for word in words:
        if line and len(line) + 1 + len(word) > width:
            out.append(line)
            line = word
        else:
            line = f"{line} {word}" if line else word
    if line:
        out.append(line)
    return out
