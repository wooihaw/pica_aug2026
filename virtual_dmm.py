#!/usr/bin/env python3
"""
virtual_dmm.py - a complete digital-multimeter teaching rig in one file.

Same shape as virtual_scope.py, but the wire protocol is *serial* rather than
VISA, and the instrument is a bench multimeter rather than a scope:

    PART 1  SIGNAL SOURCE  the physical quantity hanging off the test leads
    PART 2  INSTRUMENT     a virtual DMM: functions, ranges, NPLC, buffer
    PART 3  SCPI PARSER    text in, text out - the part students will extend
    PART 4  SERIAL SERVER  the TCP endpoint that PySerial's socket:// dials
    PART 5  FRONT PANEL    PySimpleGUI: 7-segment display, function keys,
                           signal source, traffic log
    PART 6  ENTRY POINT    wires them together

Why socket:// and not a mock object
-----------------------------------
PySerial can open more than a COM port.  `serial.serial_for_url()` accepts a
handful of URL schemes, and `socket://host:port` makes a TCP connection look
exactly like a serial port to everything above it:

    ser = serial.serial_for_url("socket://127.0.0.1:5025", timeout=5)
    ser.write(b"*IDN?\\n")
    print(ser.read_until(b"\\n").decode().strip())

Nothing in that snippet is stubbed.  It is a real `serial.Serial`-compatible
object, real bytes, real timeouts, real line termination.  Swap the URL for
`COM4` or `/dev/ttyUSB0` and the same code drives a real instrument through a
USB-to-serial adapter.  That is the whole point: the students' code is not
written against the simulator, it is written against PySerial.

    Simulator :  serial_for_url("socket://127.0.0.1:5025", timeout=5)
    Real DMM   : serial_for_url("COM4", baudrate=9600, timeout=5)

Baud rate, parity and flow control are ignored by the socket handler - there is
no UART to configure.  Worth saying out loud in class, because it is the one
thing the simulation cannot teach.

Install:

    pip install pyserial "pysimplegui>=6.0" numpy

Usage:

    python virtual_dmm.py                 simulator + front panel, one window
    python virtual_dmm.py --headless      simulator only, no GUI
    python virtual_dmm.py --port 5030     listen somewhere else
    python virtual_dmm.py --host 0.0.0.0  let the room connect to your machine
    python virtual_dmm.py -v              echo every exchange to stdout
    python virtual_dmm.py --strict        stop being forgiving (see below)

Forgiving by default
--------------------
Out of the box the simulator does not punish sloppy client code: autorange is
on, readings are always valid numbers, and a malformed command is answered with
an error in the queue but nothing worse.  `--strict` turns on the two
behaviours that bite people on real hardware - `9.9E+37` returned when the
input exceeds the selected range, and a genuinely enforced error queue.  Start
forgiving, switch to strict once the class has something working.

Implemented SCPI subset (long and short forms, case-insensitive):

    *IDN?  *RST  *CLS  *OPC?  *WAI  *TRG  *TST?  *ESR?  *STB?
    CONFigure:{VOLTage:DC|VOLTage:AC|CURRent:DC|CURRent:AC|RESistance|
               FRESistance|CAPacitance|FREQuency|TEMPerature|DIODe|CONTinuity}
    CONFigure?                       FUNCtion "VOLT:DC"      FUNCtion?
    MEASure:<function>? [range[,resolution]]
    READ?   INITiate   FETCh?   R? [n]   ABORt   DATA:POINts?
    [SENSe:]<function>:RANGe          [SENSe:]<function>:RANGe:AUTO
    [SENSe:]<function>:NPLCycles      [SENSe:]<function>:RESolution
    TRIGger:SOURce|COUNt|DELay        SAMPle:COUNt
    SYSTem:ERRor?  SYSTem:BEEPer  SYSTem:LFRequency?  SYSTem:VERSion?
    DISPlay:TEXT  DISPlay:TEXT:CLEar

Plus a non-standard SIMulate: subsystem that drives the signal source, so a
notebook can set up a repeatable device-under-test without anyone touching the
GUI.  This is the serial equivalent of the function generator you would wire to
the leads on a real bench:

    SIMulate:VOLTage:SHAPe STEP        CONStant|SINe|RAMP|STEP|NOISe
    SIMulate:VOLTage:LEVel 5.0         the base value, in the function's units
    SIMulate:VOLTage:AMPLitude 40      swing, as a percentage (see Source)
    SIMulate:VOLTage:PERiod 2.0        seconds
    SIMulate:VOLTage:NOISe 0.05        percentage
    SIMulate:VOLTage:TAU 0.3           settling time constant, seconds
    SIMulate:RESistance:LEVel 470      ... and the same for every function
"""

import argparse
import csv
import datetime
import math
import queue
import random
import socket
import socketserver
import threading
import time

# ==========================================================================
# PART 0  small shared helpers
# ==========================================================================

_PREFIXES = [(1e9, "G"), (1e6, "M"), (1e3, "k"), (1.0, ""),
             (1e-3, "m"), (1e-6, "µ"), (1e-9, "n"), (1e-12, "p")]


def prefix_for(x):
    """Engineering scale factor and SI prefix for a magnitude."""
    ax = abs(x)
    if ax == 0 or not math.isfinite(ax):
        return 1.0, ""
    for scale, name in _PREFIXES:
        if ax >= scale:
            return scale, name
    return 1e-12, "p"


def eng(value, unit="", digits=4):
    """1234.0 -> '1.234 k' - for logs and status lines, not the 7-seg display."""
    if value is None or not math.isfinite(value):
        return "----"
    scale, name = prefix_for(value)
    return f"{value / scale:.{digits}g} {name}{unit}".strip()


def scpi_num(value):
    """The instrument's number format: +5.00019000E+00.

    Nine significant figures, explicit sign, two-digit exponent.  Students who
    call float() on it never notice; students who try to parse it by hand
    always do.
    """
    return f"{value:+.8E}"


# ==========================================================================
# PART 1  SIGNAL SOURCE - the physical quantity under test
# ==========================================================================
#
# This is the part that has no counterpart on a real instrument.  It stands in
# for whatever is clipped to the test leads: a battery, a resistor, a thermo-
# couple, a signal generator.  Each measurement function gets its own source,
# because in real life you unplug one thing and plug in another.
#
# One design decision worth explaining in class: amplitude and noise are
# percentages, not absolute values.  A slider that means "40 %" behaves
# identically whether the quantity under test is 5 V, 470 ohms or 100 nF, so
# the same panel works for every function without rescaling.  The reference for
# the percentage is |level|, floored at 1 % of full scale so that a source
# sitting at exactly zero can still be made to move.

WAVEFORMS = ["CONSTANT", "SINE", "RAMP", "STEP", "NOISE"]


class Source:
    """A time-varying physical quantity.

    STEP is the interesting one.  It is a square wave passed through a
    first-order lag, so the value does not jump - it settles with time constant
    tau.  Point a continuous logging loop at it and the students capture a real
    settling curve, which is the cheapest possible introduction to why
    instruments have settling specifications at all.
    """

    def __init__(self, level, fullscale, waveform="CONSTANT", amp_pct=0.0,
                 period=2.0, noise_pct=0.02, tau=0.20):
        self.level = float(level)
        self.fullscale = float(fullscale)
        self.waveform = waveform
        self.amp_pct = float(amp_pct)
        self.period = float(period)
        self.noise_pct = float(noise_pct)
        self.tau = float(tau)
        # State for the STEP lag.  Advanced by wall clock, so it does not
        # matter whether the GUI or a client asks for the value next.
        self._lag = float(level)
        self._lag_t = time.monotonic()

    # -- the percentage reference -----------------------------------------
    def _span(self):
        return max(abs(self.level), self.fullscale * 0.01)

    def amplitude(self):
        return self.amp_pct / 100.0 * self._span()

    def noise_sigma(self):
        return self.noise_pct / 100.0 * self._span()

    # -- the value itself --------------------------------------------------
    def value(self, now=None):
        now = time.monotonic() if now is None else now
        amp = self.amplitude()
        period = max(self.period, 1e-3)

        if self.waveform == "CONSTANT":
            v = self.level
        elif self.waveform == "SINE":
            v = self.level + amp * math.sin(2 * math.pi * now / period)
        elif self.waveform == "RAMP":
            frac = (now % period) / period
            v = self.level + amp * (2.0 * frac - 1.0)
        elif self.waveform == "NOISE":
            v = self.level + amp * random.gauss(0.0, 1.0)
        elif self.waveform == "STEP":
            target = self.level + amp if (now % period) < period / 2 else self.level
            dt = max(0.0, now - self._lag_t)
            self._lag_t = now
            tau = max(self.tau, 1e-4)
            self._lag += (target - self._lag) * (1.0 - math.exp(-dt / tau))
            v = self._lag
        else:
            v = self.level

        sigma = self.noise_sigma()
        if sigma:
            v += random.gauss(0.0, sigma)
        return v

    def settled_target(self, now=None):
        """Where a STEP source is heading - used only by the front panel."""
        now = time.monotonic() if now is None else now
        if self.waveform != "STEP":
            return None
        period = max(self.period, 1e-3)
        return self.level + self.amplitude() if (now % period) < period / 2 \
            else self.level


# --------------------------------------------------------------------------
# Function table
# --------------------------------------------------------------------------
# key         : the canonical SCPI function name, as FUNCtion? reports it
# label       : what the front panel prints
# unit        : display unit
# annun       : the small annunciator, as it appears on a real meter
# ranges      : selectable ranges, smallest first
# fullscales  : what the source panel's "full scale" combo offers
# bipolar     : may the quantity be negative
# nplc        : does this function have an integration time
# digits      : display digits when NPLC does not apply (or its cap)

FUNCTIONS = {
    "VOLT":    dict(label="DC Volts",     unit="V",  annun="VDC",
                    ranges=[0.1, 1.0, 10.0, 100.0, 1000.0],
                    fullscales=[0.1, 1.0, 10.0, 100.0, 1000.0],
                    bipolar=True,  nplc=True,  digits=6.5),
    "VOLT:AC": dict(label="AC Volts",     unit="V",  annun="VAC",
                    ranges=[0.1, 1.0, 10.0, 100.0, 750.0],
                    fullscales=[0.1, 1.0, 10.0, 100.0, 750.0],
                    bipolar=False, nplc=False, digits=6.5),
    "CURR":    dict(label="DC Current",   unit="A",  annun="ADC",
                    ranges=[1e-4, 1e-3, 1e-2, 1e-1, 1.0, 3.0],
                    fullscales=[1e-3, 1e-2, 1e-1, 1.0, 3.0],
                    bipolar=True,  nplc=True,  digits=6.5),
    "CURR:AC": dict(label="AC Current",   unit="A",  annun="AAC",
                    ranges=[1e-4, 1e-3, 1e-2, 1e-1, 1.0, 3.0],
                    fullscales=[1e-3, 1e-2, 1e-1, 1.0, 3.0],
                    bipolar=False, nplc=False, digits=6.5),
    "RES":     dict(label="2-wire Res",   unit="Ω", annun="2WIRE",
                    ranges=[100.0, 1e3, 1e4, 1e5, 1e6, 1e7, 1e8],
                    fullscales=[100.0, 1e3, 1e4, 1e5, 1e6, 1e7, 1e8],
                    bipolar=False, nplc=True,  digits=6.5),
    "FRES":    dict(label="4-wire Res",   unit="Ω", annun="4WIRE",
                    ranges=[100.0, 1e3, 1e4, 1e5, 1e6, 1e7, 1e8],
                    fullscales=[100.0, 1e3, 1e4, 1e5, 1e6, 1e7, 1e8],
                    bipolar=False, nplc=True,  digits=6.5),
    "CAP":     dict(label="Capacitance",  unit="F",  annun="CAP",
                    ranges=[1e-9, 1e-8, 1e-7, 1e-6, 1e-5, 1e-4],
                    fullscales=[1e-9, 1e-8, 1e-7, 1e-6, 1e-5, 1e-4],
                    bipolar=False, nplc=False, digits=4.5),
    "FREQ":    dict(label="Frequency",    unit="Hz", annun="FREQ",
                    ranges=[1e6],
                    fullscales=[100.0, 1e3, 1e4, 1e5, 1e6],
                    bipolar=False, nplc=False, digits=6.5),
    "TEMP":    dict(label="Temperature",  unit="°C", annun="TEMP",
                    ranges=[100.0],
                    fullscales=[100.0, 200.0],
                    bipolar=True,  nplc=True,  digits=5.5),
    "DIOD":    dict(label="Diode",        unit="V",  annun="DIODE",
                    ranges=[1.2],
                    fullscales=[1.2, 2.0],
                    bipolar=False, nplc=False, digits=5.5),
    "CONT":    dict(label="Continuity",   unit="Ω", annun="CONT",
                    ranges=[1000.0],
                    fullscales=[1000.0],
                    bipolar=False, nplc=False, digits=4.5),
}

# Several functions probe the same physical thing.  A 4-wire measurement and a
# continuity test are both looking at the resistor you clipped on, so they share
# one source - change it once and all three functions follow.
SOURCE_ALIAS = {"FRES": "RES", "CONT": "RES"}

# Quantities the source panel offers, in panel order.
SOURCE_KEYS = ["VOLT", "VOLT:AC", "CURR", "CURR:AC", "RES", "CAP",
               "FREQ", "TEMP", "DIOD"]

DEFAULT_SOURCES = {
    "VOLT":    dict(level=5.0,    fullscale=10.0,  noise_pct=0.02),
    "VOLT:AC": dict(level=1.0,    fullscale=10.0,  noise_pct=0.05, tau=0.60),
    "CURR":    dict(level=0.010,  fullscale=0.1,   noise_pct=0.03),
    "CURR:AC": dict(level=0.005,  fullscale=0.1,   noise_pct=0.06, tau=0.60),
    "RES":     dict(level=470.0,  fullscale=1e3,   noise_pct=0.01),
    "CAP":     dict(level=1e-7,   fullscale=1e-6,  noise_pct=0.10, tau=0.50),
    "FREQ":    dict(level=1000.0, fullscale=1e4,   noise_pct=0.01),
    "TEMP":    dict(level=25.0,   fullscale=100.0, noise_pct=0.05, tau=2.00),
    "DIOD":    dict(level=0.65,   fullscale=1.2,   noise_pct=0.02),
}

# Test-lead resistance.  A 2-wire measurement includes it; a 4-wire one does
# not.  Two lines of code, and it makes the difference between RES and FRES
# something students can see rather than something they have to be told.
LEAD_RESISTANCE = 0.12

LINE_FREQ = 50.0                       # Hz, for NPLC -> seconds
NPLC_CHOICES = [0.02, 0.2, 1.0, 10.0, 100.0]
NPLC_DIGITS = {0.02: 4.5, 0.2: 5.5, 1.0: 6.5, 10.0: 6.5, 100.0: 6.5}
OVERLOAD = 9.9e37                      # the "not a valid reading" value


# ==========================================================================
# PART 2  THE INSTRUMENT
# ==========================================================================

class SimDMM:
    """State and measurement engine of a virtual DMM.

    Everything the SCPI parser and the front panel touch lives here, guarded by
    one lock.  The front panel runs on the tkinter main thread, the parser runs
    on a socket thread, and the buffered acquisition runs on a third - so a
    single coarse lock is the honest choice.  Nothing in here is fast enough to
    make lock contention interesting.
    """

    IDN = ("Keysight Technologies,34461A,MY57200042,"
           "A.02.17-02.40-02.17-00.52-03-01")

    def __init__(self, strict=False):
        self.lock = threading.RLock()
        self.strict = strict
        self.sources = {k: Source(**DEFAULT_SOURCES[k]) for k in SOURCE_KEYS}
        self.error_queue = []
        self.display_text = ""
        self.buffer = []
        self.acq_thread = None
        self.acq_stop = threading.Event()
        self.armed = False               # INITiated, waiting for a BUS trigger
        self.reset()

    # ------------------------------------------------------------------
    # state
    # ------------------------------------------------------------------
    def reset(self):
        self.function = "VOLT"
        self.ranges = {k: FUNCTIONS[k]["ranges"][-1] for k in FUNCTIONS}
        self.autorange = {k: True for k in FUNCTIONS}
        self.nplc = {k: 1.0 for k in FUNCTIONS}
        self.trig_source = "IMM"
        self.trig_count = 1
        self.trig_delay = 0.0
        self.sample_count = 1
        self.buffer = []
        self.armed = False
        self.display_text = ""
        self.stop_acquisition()

    def source_for(self, function):
        return self.sources[SOURCE_ALIAS.get(function, function)]

    # ------------------------------------------------------------------
    # the physical quantity, seen through this function's front end
    # ------------------------------------------------------------------
    def raw_value(self, function=None):
        """The true quantity, before ranging, noise or quantisation."""
        function = function or self.function
        v = self.source_for(function).value()
        if function in ("RES", "CONT"):
            v = max(0.0, v) + LEAD_RESISTANCE      # 2-wire sees the leads
        elif function == "FRES":
            v = max(0.0, v)                        # 4-wire does not
        elif function in ("CAP", "FREQ", "VOLT:AC", "CURR:AC", "DIOD"):
            v = max(0.0, v)                        # these cannot go negative
        return v

    def digits_for(self, function):
        spec = FUNCTIONS[function]
        if spec["nplc"]:
            return min(spec["digits"], NPLC_DIGITS[self.nplc[function]])
        return spec["digits"]

    def aperture(self, function):
        """How long one reading takes, in seconds."""
        spec = FUNCTIONS[function]
        if spec["nplc"]:
            return self.nplc[function] / LINE_FREQ + 0.004
        return 0.020 if function not in ("VOLT:AC", "CURR:AC") else 0.100

    def select_range(self, function, value):
        """Autorange: smallest range that holds the value at 120 % of full scale.

        This is the real rule, and it is why a 12 V input sits on the 100 V
        range while an 11 V input does not.
        """
        ranges = FUNCTIONS[function]["ranges"]
        if not self.autorange[function]:
            return self.ranges[function]
        for r in ranges:
            if abs(value) <= r * 1.2:
                return r
        return ranges[-1]

    def measure(self, function=None, apply_range=True):
        """One reading: source -> ranging -> noise -> quantisation."""
        function = function or self.function
        true_value = self.raw_value(function)

        rng = self.select_range(function, true_value)
        if apply_range and self.autorange[function]:
            self.ranges[function] = rng
        rng = self.ranges[function]

        if self.strict and abs(true_value) > rng * 1.2:
            return OVERLOAD

        digits = self.digits_for(function)
        # Meter noise, on top of whatever the source is doing.  Longer
        # integration averages it away - which is exactly the trade students
        # should discover by sweeping NPLC and watching the standard deviation.
        sigma = rng * 3e-6
        if FUNCTIONS[function]["nplc"]:
            sigma /= math.sqrt(self.nplc[function])
        reading = true_value + random.gauss(0.0, sigma)

        step = self.resolution(function)
        if step > 0:
            reading = round(reading / step) * step
        return reading

    def resolution(self, function):
        """Smallest displayable increment, from range and digits."""
        digits = self.digits_for(function)
        counts = 10 ** int(digits)
        if function == "FREQ":
            # A frequency counter's resolution follows the reading, not a range.
            v = abs(self.raw_value(function))
            return max(v, 1.0) / counts
        return self.ranges[function] / counts

    def display_range(self, function, value):
        """Magnitude the display scales itself to.

        The same as the selected range for every function except frequency,
        where the "range" is a placeholder - a counter has no range, so the
        reading has to scale itself.
        """
        if function == "FREQ":
            return max(abs(value), 1.0)
        return self.ranges[function]

    def overloaded(self, value, function=None):
        function = function or self.function
        return abs(value) > self.ranges[function] * 1.2

    # ------------------------------------------------------------------
    # configuration
    # ------------------------------------------------------------------
    def configure(self, function, rng=None, resolution=None):
        self.function = function
        if rng is None or (isinstance(rng, str) and rng in ("AUTO", "DEF")):
            self.autorange[function] = True
        elif isinstance(rng, str):
            choices = FUNCTIONS[function]["ranges"]
            self.autorange[function] = False
            self.ranges[function] = choices[0] if rng == "MIN" else choices[-1]
        else:
            self.autorange[function] = False
            self.ranges[function] = self.snap_range(function, float(rng))
        if resolution is not None and FUNCTIONS[function]["nplc"]:
            self.set_resolution(function, resolution)

    def snap_range(self, function, wanted):
        """Round up to a real range, the way the instrument does.

        RANGe 3 on DC volts selects the 10 V range and reports 10, not 3.  Ask
        students to set 3 and read it back; the surprise is the lesson.
        """
        for r in FUNCTIONS[function]["ranges"]:
            if wanted <= r * 1.0000001:
                return r
        return FUNCTIONS[function]["ranges"][-1]

    def set_resolution(self, function, resolution):
        """Pick the NPLC that gets closest to the requested resolution."""
        if isinstance(resolution, str) or resolution <= 0:
            self.nplc[function] = 1.0
            return
        rng = self.ranges[function]
        best, best_err = 1.0, float("inf")
        for nplc in NPLC_CHOICES:
            step = rng / 10 ** int(NPLC_DIGITS[nplc])
            err = abs(math.log10(max(step, 1e-15)) - math.log10(resolution))
            if err < best_err:
                best, best_err = nplc, err
        self.nplc[function] = best

    # ------------------------------------------------------------------
    # triggering and the reading buffer
    # ------------------------------------------------------------------
    def total_samples(self):
        return max(1, self.trig_count) * max(1, self.sample_count)

    def start_acquisition(self):
        """INITiate: clear memory and fill it in the background.

        Non-blocking, exactly like the real instrument.  That is what makes
        DATA:POINts? worth polling and what makes FETCh? a separate command.
        """
        self.stop_acquisition()
        self.buffer = []
        self.acq_stop.clear()
        if self.trig_source == "BUS":
            self.armed = True
            return
        self._launch()

    def _launch(self):
        self.armed = False
        n = self.total_samples()
        function = self.function
        interval = self.aperture(function)
        delay = self.trig_delay

        def worker():
            if delay:
                time.sleep(delay)
            # Absolute deadlines, not repeated sleep(interval).  Sleeping a
            # fixed amount each time accumulates the scheduler's overshoot -
            # a few milliseconds a sample, which over 200 samples is enough to
            # visibly skew a timebase a student derives from the elapsed time.
            deadline = time.monotonic()
            for _ in range(n):
                if self.acq_stop.is_set():
                    return
                deadline += interval
                remaining = deadline - time.monotonic()
                if remaining > 0:
                    time.sleep(remaining)
                with self.lock:
                    self.buffer.append(self.measure(function))

        self.acq_thread = threading.Thread(target=worker, name="dmm-acq",
                                           daemon=True)
        self.acq_thread.start()

    def bus_trigger(self):
        if self.armed:
            self._launch()

    def stop_acquisition(self):
        self.acq_stop.set()
        thread = getattr(self, "acq_thread", None)
        if thread and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=2.0)
        self.acq_thread = None
        self.armed = False

    def wait_for_acquisition(self, timeout=120.0):
        thread = self.acq_thread
        if thread and thread.is_alive():
            thread.join(timeout=timeout)

    def fetch(self):
        self.wait_for_acquisition()
        with self.lock:
            return list(self.buffer)

    def read_and_remove(self, n=None):
        with self.lock:
            n = len(self.buffer) if n is None else min(n, len(self.buffer))
            out, self.buffer = self.buffer[:n], self.buffer[n:]
            return out

    # ------------------------------------------------------------------
    # errors
    # ------------------------------------------------------------------
    def push_error(self, text):
        if len(self.error_queue) < 20:
            self.error_queue.append(text)

    def pop_error(self):
        return self.error_queue.pop(0) if self.error_queue else '+0,"No error"'


# ==========================================================================
# PART 3  THE SCPI PARSER
# ==========================================================================
#
# Deliberately a flat if/elif chain over a normalised command path.  It is not
# the fastest way to dispatch and it is not the most elegant, but a student can
# read it top to bottom and add a command in four lines - which is the point.

class ScpiError(Exception):
    """Raised with a SCPI-style '-113,"Undefined header"' payload."""


# Long form -> canonical short form.  SCPI lets a client send either, plus any
# prefix in between, so _canon() accepts all of them.
#
# Note RESistance and RESolution both shorten to RES.  That is genuinely
# ambiguous in SCPI and the real instrument resolves it by position, which is
# what _split_function() below does too.
_MNEMONICS = {
    "CONFIGURE": "CONF", "MEASURE": "MEAS", "FETCH": "FETC",
    "INITIATE": "INIT", "ABORT": "ABOR", "SENSE": "SENS",
    "VOLTAGE": "VOLT", "CURRENT": "CURR", "RESISTANCE": "RES",
    "FRESISTANCE": "FRES", "CAPACITANCE": "CAP", "FREQUENCY": "FREQ",
    "PERIOD": "PER", "TEMPERATURE": "TEMP", "DIODE": "DIOD",
    "CONTINUITY": "CONT", "RANGE": "RANG", "RESOLUTION": "RES",
    "FUNCTION": "FUNC", "TRIGGER": "TRIG", "SOURCE": "SOUR",
    "COUNT": "COUN", "DELAY": "DEL", "SAMPLE": "SAMP", "SYSTEM": "SYST",
    "ERROR": "ERR", "BEEPER": "BEEP", "VERSION": "VERS",
    "LFREQUENCY": "LFR", "DISPLAY": "DISP", "CLEAR": "CLE",
    "POINTS": "POIN", "AUTO": "AUTO", "NPLCYCLES": "NPLC",
    "BANDWIDTH": "BAND", "APERTURE": "APER", "STATE": "STAT",
    "SIMULATE": "SIM", "SHAPE": "SHAP", "LEVEL": "LEV",
    "AMPLITUDE": "AMPL", "NOISE": "NOIS", "TAU": "TAU",
    "CONSTANT": "CONS", "SINE": "SIN", "IMMEDIATE": "IMM",
    "EXTERNAL": "EXT", "INTERNAL": "INT", "TEXT": "TEXT", "DATA": "DATA",
    "READ": "READ", "IMPEDANCE": "IMP", "NULL": "NULL", "ZERO": "ZERO",
}


def _canon(word):
    w = word.upper().strip()
    for long, short in _MNEMONICS.items():
        if w == short or w == long or (long.startswith(w) and w.startswith(short)):
            return short
    return w


FUNC_ALIASES = {
    "VOLT": "VOLT", "VOLT:DC": "VOLT", "VOLT:AC": "VOLT:AC",
    "CURR": "CURR", "CURR:DC": "CURR", "CURR:AC": "CURR:AC",
    "RES": "RES", "FRES": "FRES", "CAP": "CAP", "FREQ": "FREQ",
    "TEMP": "TEMP", "DIOD": "DIOD", "CONT": "CONT",
}


def _split_function(parts):
    """('VOLT','DC','NPLC') -> ('VOLT', ('NPLC',))

    Two mnemonics are tried before one, so VOLT:AC wins over VOLT.
    """
    for n in (2, 1):
        key = ":".join(parts[:n])
        if key in FUNC_ALIASES:
            return FUNC_ALIASES[key], tuple(parts[n:])
    return None, tuple(parts)


def _number(token, default=None):
    token = token.strip().strip('"').strip("'")
    if not token:
        return default
    upper = token.upper()
    if upper in ("MIN", "MAX", "DEF", "AUTO", "ONCE"):
        return upper
    try:
        return float(token)
    except ValueError:
        raise ScpiError(f'-104,"Data type error: {token}"')


def _boolean(token):
    t = token.strip().upper()
    if t in ("1", "ON", "TRUE"):
        return True
    if t in ("0", "OFF", "FALSE"):
        return False
    if t == "ONCE":
        return "ONCE"
    raise ScpiError(f'-104,"Data type error: {token}"')


def execute(dmm, line):
    """Run one SCPI command.  Returns the response text, or None."""
    line = line.strip()
    if not line:
        return None

    # Split the header from its parameters at the first space.
    if " " in line:
        header, _, params = line.partition(" ")
    else:
        header, params = line, ""
    args = [a for a in params.split(",")] if params.strip() else []

    query = header.endswith("?")
    header = header[:-1] if query else header

    # -- IEEE 488.2 common commands ------------------------------------
    if header.startswith("*"):
        return _common(dmm, header.upper(), query, args)

    parts = [_canon(p) for p in header.lstrip(":").split(":") if p]
    if parts and parts[0] == "SENS":       # the SENSe: prefix is optional
        parts = parts[1:]
    path = ":".join(parts)

    # -- measurement -----------------------------------------------------
    if parts and parts[0] == "CONF":
        return _configure(dmm, parts[1:], query, args)
    if parts and parts[0] == "MEAS":
        return _measure(dmm, parts[1:], args)
    if path == "FUNC":
        return _function(dmm, query, args)
    if path == "READ" and query:
        dmm.start_acquisition()
        if dmm.trig_source == "BUS":
            dmm.bus_trigger()
        return ",".join(scpi_num(v) for v in dmm.fetch())
    if path == "INIT":
        dmm.start_acquisition()
        return None
    if path == "FETC" and query:
        values = dmm.fetch()
        if not values:
            dmm.push_error('-230,"Data corrupt or stale"')
            return scpi_num(dmm.measure())
        return ",".join(scpi_num(v) for v in values)
    if path == "R" and query:
        n = int(_number(args[0])) if args else None
        return ",".join(scpi_num(v) for v in dmm.read_and_remove(n))
    if path == "ABOR":
        dmm.stop_acquisition()
        return None
    if path in ("DATA:POIN", "DATA:POIN:EVEN:THR"):
        if query:
            return str(len(dmm.buffer))
        return None

    # -- SENSe: per-function settings -------------------------------------
    function, tail = _split_function(parts)
    if function is not None and tail:
        return _sense(dmm, function, tail, query, args)

    # -- trigger and sample ------------------------------------------------
    if path == "TRIG:SOUR":
        if query:
            return dmm.trig_source
        dmm.trig_source = _canon(args[0])[:3] if args else "IMM"
        if dmm.trig_source not in ("IMM", "EXT", "BUS"):
            dmm.trig_source = "IMM"
        return None
    if path == "TRIG:COUN":
        if query:
            return scpi_num(dmm.trig_count)
        dmm.trig_count = max(1, int(_number(args[0], 1)))
        return None
    if path in ("TRIG:DEL", "TRIG:DEL:AUTO"):
        if query:
            return scpi_num(dmm.trig_delay)
        value = _number(args[0], 0.0) if args else 0.0
        dmm.trig_delay = 0.0 if isinstance(value, str) else max(0.0, value)
        return None
    if path == "SAMP:COUN":
        if query:
            return scpi_num(dmm.sample_count)
        dmm.sample_count = max(1, int(_number(args[0], 1)))
        return None

    # -- system and display -------------------------------------------------
    if path == "SYST:ERR" and query:
        return dmm.pop_error()
    if path == "SYST:BEEP":
        return None
    if path == "SYST:LFR" and query:
        return scpi_num(LINE_FREQ)
    if path == "SYST:VERS" and query:
        return "1996.0"
    if path == "DISP" and not query:
        return None
    if path == "DISP:TEXT":
        if query:
            return f'"{dmm.display_text}"'
        dmm.display_text = params.strip().strip('"').strip("'")[:24]
        return None
    if path == "DISP:TEXT:CLE":
        dmm.display_text = ""
        return None

    # -- the non-standard signal source --------------------------------------
    if parts and parts[0] == "SIM":
        return _simulate(dmm, parts[1:], query, args)

    raise ScpiError(f'-113,"Undefined header: {line}"')


def _common(dmm, header, query, args):
    if header == "*IDN":
        return dmm.IDN
    if header == "*RST":
        dmm.reset()
        return None
    if header == "*CLS":
        dmm.error_queue.clear()
        return None
    if header == "*OPC":
        dmm.wait_for_acquisition()
        return "1" if query else None
    if header == "*WAI":
        dmm.wait_for_acquisition()
        return None
    if header == "*TRG":
        dmm.bus_trigger()
        return None
    if header in ("*TST", "*ESR", "*STB", "*SRE", "*ESE"):
        return "0" if query else None
    raise ScpiError(f'-113,"Undefined header: {header}"')


def _configure(dmm, parts, query, args):
    if query:
        f = dmm.function
        return (f"{f} {scpi_num(dmm.ranges[f])},"
                f"{scpi_num(dmm.resolution(f))}")
    function, tail = _split_function(parts)
    if function is None or tail:
        raise ScpiError(f'-113,"Undefined header: CONF:{":".join(parts)}"')
    rng = _number(args[0]) if len(args) >= 1 else None
    res = _number(args[1]) if len(args) >= 2 else None
    dmm.configure(function, rng, res)
    return None


def _measure(dmm, parts, args):
    """MEASure? is CONFigure followed by READ? - and it resets everything else.

    Convenient, and a trap: it wipes the NPLC and trigger settings a student
    carefully applied a moment ago.  Worth demonstrating deliberately.
    """
    function, tail = _split_function(parts)
    if function is None or tail:
        raise ScpiError(f'-113,"Undefined header: MEAS:{":".join(parts)}"')
    rng = _number(args[0]) if len(args) >= 1 else None
    res = _number(args[1]) if len(args) >= 2 else None
    dmm.trig_count = 1
    dmm.sample_count = 1
    dmm.configure(function, rng, res)
    time.sleep(dmm.aperture(function))
    return scpi_num(dmm.measure())


def _function(dmm, query, args):
    if query:
        return f'"{dmm.function}"'
    wanted = args[0].strip().strip('"').strip("'") if args else "VOLT"
    key = ":".join(_canon(p) for p in wanted.split(":"))
    if key not in FUNC_ALIASES:
        raise ScpiError(f'-224,"Illegal parameter value: {wanted}"')
    dmm.function = FUNC_ALIASES[key]
    return None


def _sense(dmm, function, tail, query, args):
    path = ":".join(tail)

    if path == "RANG":
        if query:
            return scpi_num(dmm.ranges[function])
        value = _number(args[0]) if args else "AUTO"
        if value == "AUTO":
            dmm.autorange[function] = True
        elif isinstance(value, str):
            choices = FUNCTIONS[function]["ranges"]
            dmm.autorange[function] = False
            dmm.ranges[function] = choices[0] if value == "MIN" else choices[-1]
        else:
            dmm.autorange[function] = False
            dmm.ranges[function] = dmm.snap_range(function, value)
        return None

    if path == "RANG:AUTO":
        if query:
            return "1" if dmm.autorange[function] else "0"
        state = _boolean(args[0]) if args else True
        if state == "ONCE":
            dmm.ranges[function] = dmm.select_range(
                function, dmm.raw_value(function))
            dmm.autorange[function] = False
        else:
            dmm.autorange[function] = bool(state)
        return None

    if path in ("NPLC", "APER"):
        if not FUNCTIONS[function]["nplc"]:
            raise ScpiError(f'-113,"Undefined header: {function}:{path}"')
        if query:
            return scpi_num(dmm.nplc[function])
        value = _number(args[0], 1.0)
        if value == "MIN":
            value = NPLC_CHOICES[0]
        elif value == "MAX":
            value = NPLC_CHOICES[-1]
        elif isinstance(value, str):
            value = 1.0
        # Snap to a real integration time, the way the instrument does.
        dmm.nplc[function] = min(NPLC_CHOICES, key=lambda n: abs(n - value))
        return None

    if path == "RES":
        if query:
            return scpi_num(dmm.resolution(function))
        dmm.set_resolution(function, _number(args[0], 0.0))
        return None

    if path in ("BAND", "IMP:AUTO", "NULL", "NULL:STAT", "ZERO:AUTO"):
        return "0" if query else None      # accepted and ignored

    raise ScpiError(f'-113,"Undefined header: {function}:{path}"')


def _simulate(dmm, parts, query, args):
    """SIMulate:<function>:<parameter> - not a real instrument command."""
    function, tail = _split_function(parts)
    if function is None:
        raise ScpiError(f'-113,"Undefined header: SIM:{":".join(parts)}"')
    source = dmm.source_for(function)
    param = ":".join(tail)

    if not param:                          # SIM:VOLT?  -> dump everything
        if query:
            return (f"{source.waveform},{scpi_num(source.level)},"
                    f"{scpi_num(source.amp_pct)},{scpi_num(source.period)},"
                    f"{scpi_num(source.noise_pct)},{scpi_num(source.tau)}")
        raise ScpiError('-113,"Undefined header: SIM"')

    if param == "SHAP":
        if query:
            return source.waveform
        wanted = args[0].strip().upper() if args else "CONSTANT"
        for w in WAVEFORMS:
            if w.startswith(wanted) or wanted.startswith(w[:3]):
                source.waveform = w
                return None
        raise ScpiError(f'-224,"Illegal parameter value: {wanted}"')

    fields = {"LEV": "level", "AMPL": "amp_pct", "PER": "period",
              "NOIS": "noise_pct", "TAU": "tau"}
    if param in fields:
        attr = fields[param]
        if query:
            return scpi_num(getattr(source, attr))
        setattr(source, attr, float(_number(args[0], 0.0)))
        return None

    if param == "VAL" and query:           # what is really on the leads
        return scpi_num(dmm.raw_value(function))

    raise ScpiError(f'-113,"Undefined header: SIM:{":".join(parts)}"')


# ==========================================================================
# PART 4  THE SERIAL SERVER
# ==========================================================================
#
# A plain TCP server.  PySerial's socket:// URL handler is a TCP *client*, so
# all this end has to do is speak line-terminated text - which is exactly what
# a real instrument's UART does.  Nothing here knows that PySerial exists.

TERMINATOR = b"\n"

# Traffic for the front panel.  The GUI drains this in its event loop; when
# there is no GUI the queue simply fills and drops, so the server never blocks
# on a listener that is not there.
TRAFFIC = queue.Queue(maxsize=4000)


def post_traffic(kind, text, extra=None):
    try:
        TRAFFIC.put_nowait((time.time(), kind, text, extra))
    except queue.Full:
        pass


class DmmHandler(socketserver.StreamRequestHandler):
    def handle(self):
        peer = f"{self.client_address[0]}:{self.client_address[1]}"
        self.server.clients.add(peer)
        post_traffic("CONN", peer)
        print(f"[dmm] connected {peer}")
        try:
            for raw in self.rfile:
                message = raw.decode(errors="replace").strip()
                if not message:
                    continue
                # One line may chain several commands with ';'.  Only the
                # first keeps its full path on a real instrument; here every
                # part is treated as a complete command, which is forgiving.
                for part in message.split(";"):
                    part = part.strip()
                    if not part:
                        continue
                    self.dispatch(part)
        except (ConnectionResetError, BrokenPipeError):
            pass
        finally:
            self.server.clients.discard(peer)
            post_traffic("DISC", peer)
            print(f"[dmm] disconnected {peer}")

    def dispatch(self, command):
        started = time.perf_counter()
        post_traffic("TX", command)
        if self.server.verbose:
            print(f"[dmm] <- {command}")
        try:
            reply = execute(self.server.dmm, command)
        except ScpiError as exc:
            self.server.dmm.push_error(str(exc))
            post_traffic("ERR", str(exc))
            print(f"[dmm] !! {exc}")
            return
        except Exception as exc:                                  # noqa: BLE001
            self.server.dmm.push_error(f'-100,"Command error: {exc}"')
            post_traffic("ERR", f"{type(exc).__name__}: {exc}")
            print(f"[dmm] !! {exc}")
            return

        if reply is None:
            return
        elapsed = (time.perf_counter() - started) * 1000.0
        post_traffic("RX", reply, elapsed)
        if self.server.verbose:
            preview = reply if len(reply) <= 70 else reply[:67] + "..."
            print(f"[dmm] -> {preview}")
        self.wfile.write(reply.encode() + TERMINATOR)
        self.wfile.flush()


class DmmServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def port_in_use(host, port, timeout=0.4):
    with socket.socket() as probe:
        probe.settimeout(timeout)
        return probe.connect_ex((host, port)) == 0


def start_server(dmm, host, port, verbose=False):
    """Bind on this thread, serve on another.

    The bind has to be synchronous.  If it happened on the background thread
    the front panel - or an over-eager notebook - could try to connect a few
    milliseconds later, and an unbound port refuses rather than queues.
    """
    server = DmmServer((host, port), DmmHandler)
    server.dmm = dmm
    server.verbose = verbose
    server.clients = set()
    threading.Thread(target=server.serve_forever, name="dmm-server",
                     daemon=True).start()
    print(f"[dmm] listening on socket://{host}:{port}")
    return server


# ==========================================================================
# PART 5  THE FRONT PANEL
# ==========================================================================
#
# Two structural constraints, both forced by tkinter rather than by taste:
#
# 1. Tkinter is not thread safe.  The socket threads never touch a widget -
#    they post to TRAFFIC and the main event loop drains it.
#
# 2. The seven-segment display is erased and redrawn wholesale at 6 Hz.  That
#    is about 60 canvas polygons per frame, which tkinter handles comfortably,
#    and 6 Hz is roughly the reading rate of a real meter at NPLC 1 anyway.

BG = "#0d1117"
PANEL = "#161b22"
LCD_BG = "#101a14"
SEG_ON = "#5ef2a0"
SEG_OFF = "#18251d"
FG = "#c9d1d9"
MUTED = "#8b949e"
GOOD = "#3fb950"
WARN = "#d29922"
BAD = "#f85149"
TX_COLOUR = "#7ee787"
RX_COLOUR = "#79c0ff"

DISP_W, DISP_H = 640, 190
REFRESH_HZ = 6
LEVEL_TICKS = 1000          # slider travel, in permille of 1.2 x full scale

# The function keys under the display, in the order a real meter has them.
FUNC_BUTTONS = [
    ("DCV", "VOLT"), ("ACV", "VOLT:AC"), ("DCI", "CURR"), ("ACI", "CURR:AC"),
    ("2WΩ", "RES"), ("4WΩ", "FRES"),
    ("FREQ", "FREQ"), ("CAP", "CAP"), ("TEMP", "TEMP"), ("DIODE", "DIOD"),
    ("CONT", "CONT"),
]
FUNC_BTN_ON = ("white", "#238636")
FUNC_BTN_OFF = ("#c9d1d9", "#30363d")

# Which segments each character lights.
SEVEN_SEG = {
    "0": "abcdef", "1": "bc", "2": "abdeg", "3": "abcdg", "4": "bcfg",
    "5": "acdfg", "6": "acdefg", "7": "abc", "8": "abcdefg", "9": "abcdfg",
    "-": "g", " ": "", "_": "d",
    "O": "abcdef", "L": "def", "V": "bcdef", "D": "bcdeg", "E": "adefg",
}


def _h_segment(x, y, w, t):
    """Bevelled horizontal bar centred on y, spanning x..x+w."""
    h = t / 2
    return [(x + h, y), (x + t, y - h), (x + w - t, y - h),
            (x + w - h, y), (x + w - t, y + h), (x + t, y + h)]


def _v_segment(x, y0, y1, t):
    """Bevelled vertical bar centred on x, spanning y0..y1."""
    h = t / 2
    return [(x, y0 + h), (x + h, y0 + t), (x + h, y1 - t),
            (x, y1 - h), (x - h, y1 - t), (x - h, y0 + t)]


def draw_seven_segment(graph, ch, x, y, w, h, t, dot=False):
    """Draw one character cell.  Off segments are drawn too - a real LCD's
    inactive segments are faintly visible, and it stops the display looking
    like it has holes in it."""
    lit = SEVEN_SEG.get(ch.upper(), "")
    mid = y + h / 2
    geometry = {
        "a": _h_segment(x, y + t / 2, w, t),
        "g": _h_segment(x, mid, w, t),
        "d": _h_segment(x, y + h - t / 2, w, t),
        "f": _v_segment(x + t / 2, y, mid, t),
        "b": _v_segment(x + w - t / 2, y, mid, t),
        "e": _v_segment(x + t / 2, mid, y + h, t),
        "c": _v_segment(x + w - t / 2, mid, y + h, t),
    }
    for name, points in geometry.items():
        colour = SEG_ON if name in lit else SEG_OFF
        graph.draw_polygon(points, fill_color=colour, line_color=colour)
    if dot:
        r = t * 0.55
        cx, cy = x + w + t * 0.45, y + h - r
        graph.draw_circle((cx, cy), r, fill_color=SEG_ON, line_color=SEG_ON)


def format_reading(value, rng, resolution, unit, overload=False):
    """Turn a float into (digit string, prefixed unit).

    Digits after the point come from the instrument's resolution, not from
    Python's idea of a nice number - which is why NPLC visibly changes how many
    figures the display shows.
    """
    if overload or not math.isfinite(value) or abs(value) >= 1e37:
        return "  OVLD  ", unit
    # Ranges from 1 to 1000 are shown unprefixed, the way the instrument does
    # it: the 1000 V range reads in volts, not kilovolts.
    if 1.0 <= rng <= 1000.0:
        scale, prefix = 1.0, ""
    else:
        scale, prefix = prefix_for(rng)
    v = value / scale
    step = max(resolution / scale, 1e-9)
    decimals = max(0, min(7, int(math.ceil(-math.log10(step)))))
    text = f"{v:.{decimals}f}"
    if len(text.replace("-", "").replace(".", "")) > 8:
        decimals = max(0, decimals - (len(text.replace("-", "").replace(".", "")) - 8))
        text = f"{v:.{decimals}f}"
    return text, prefix + unit


class DmmPanel:
    def __init__(self, dmm, server, host, port):
        self.dmm = dmm
        self.server = server
        self.host, self.port = host, port
        self.source_key = "VOLT"
        self.last_function = None
        self.log_paused = False
        self.log_lines = []
        self.last_event_t = None
        self.tx_count = 0
        self.rx_bytes = 0
        self.window = self.build()
        self.load_source_widgets()

    # ------------------------------------------------------------------
    # construction
    # ------------------------------------------------------------------
    def build(self):
        import PySimpleGUI as sg
        self.sg = sg
        sg.theme("DarkGrey13")

        display = sg.Graph(
            canvas_size=(DISP_W, DISP_H),
            graph_bottom_left=(0, DISP_H),
            graph_top_right=(DISP_W, 0),      # y grows downward
            background_color=LCD_BG,
            key="-LCD-", pad=(0, 0))

        ann = dict(font=("Courier", 11), background_color=PANEL)
        annun = [
            sg.Text("VDC", key="-A-FUNC-", size=(7, 1), text_color=SEG_ON,
                    **{**ann, "font": ("Courier", 11, "bold")}),
            sg.Text("AUTO", key="-A-AUTO-", size=(6, 1), text_color=WARN, **ann),
            sg.Text("RANGE 10 V", key="-A-RANGE-", size=(16, 1),
                    text_color=FG, **ann),
            sg.Text("NPLC 1", key="-A-NPLC-", size=(10, 1), text_color=FG, **ann),
            sg.Text("6½ DIGITS", key="-A-DIG-", size=(11, 1),
                    text_color=MUTED, **ann),
            sg.Text("TRIG IMM", key="-A-TRIG-", size=(11, 1),
                    text_color=MUTED, **ann),
            sg.Text("REM", key="-A-REM-", size=(5, 1), text_color=SEG_OFF,
                    **{**ann, "font": ("Courier", 11, "bold")}),
        ]

        # The function keys, the same ones that are under the display on a
        # real meter.  They set exactly the state CONFigure sets, so pressing DCV
        # here and sending CONF:VOLT:DC from a notebook are the same action -
        # which is the point worth making when a student asks what the front
        # panel is "really" doing.
        def fkey(label, function):
            return sg.Button(label, key=f"-F-{function}-", size=(6, 1),
                             font=("Helvetica", 9), pad=(2, 2))

        keypad = [
            [fkey(lab, fn) for lab, fn in FUNC_BUTTONS[:6]],
            [fkey(lab, fn) for lab, fn in FUNC_BUTTONS[6:]],
        ]

        truth = [
            sg.Text("on the leads:", size=(13, 1), text_color=MUTED,
                    font=("Helvetica", 9), background_color=PANEL),
            sg.Text("", key="-TRUE-", size=(24, 1), font=("Courier", 11),
                    text_color=WARN, background_color=PANEL),
            sg.Text("", key="-BUF-", size=(28, 1), font=("Courier", 10),
                    text_color=MUTED, background_color=PANEL),
        ]

        front = sg.Frame("INSTRUMENT FRONT PANEL",
                         [annun, [display]] + keypad + [truth],
                         font=("Helvetica", 9, "bold"), title_color=MUTED,
                         background_color=PANEL, pad=(8, 8))

        slider = dict(orientation="h", size=(34, 13), enable_events=True,
                      trough_color="#30363d", background_color=PANEL,
                      text_color=FG)

        waveform_row = [sg.Radio(w.title(), "WAVE", key=f"-W-{w}-",
                                 default=(w == "CONSTANT"), enable_events=True,
                                 background_color=PANEL, text_color=FG,
                                 font=("Helvetica", 9))
                        for w in WAVEFORMS]

        source = sg.Frame("SIGNAL SOURCE  —  the quantity under test", [
            [sg.Text("Quantity", size=(11, 1), text_color=MUTED,
                     background_color=PANEL),
             sg.Combo([FUNCTIONS[k]["label"] for k in SOURCE_KEYS],
                      default_value=FUNCTIONS["VOLT"]["label"],
                      key="-SRC-", size=(16, 1), readonly=True,
                      enable_events=True),
             sg.Text("full scale", text_color=MUTED, background_color=PANEL),
             sg.Combo([], key="-FS-", size=(12, 1), readonly=True,
                      enable_events=True)],

            [sg.Text("Waveform", size=(11, 1), text_color=MUTED,
                     background_color=PANEL)] + waveform_row,

            [sg.Text("Level", size=(11, 1), text_color=MUTED,
                     background_color=PANEL),
             sg.Input("", key="-LEVEL-IN-", size=(14, 1), font=("Courier", 10)),
             sg.Button("Set", key="-LEVEL-SET-", size=(5, 1),
                       bind_return_key=True),
             sg.Text("", key="-LEVEL-TXT-", size=(20, 1), font=("Courier", 10),
                     text_color=SEG_ON, background_color=PANEL)],
            # The level slider runs in permille of full scale rather than in
            # volts or ohms.  Slider.update() can change a slider's range but
            # not its resolution, so a fixed 0-1000 travel is the only way to
            # keep the feel identical whether the quantity under test is 100 mV
            # or 100 megohms.  The Text above it always shows the real value.
            [sg.Slider(range=(-LEVEL_TICKS, LEVEL_TICKS), default_value=0,
                       resolution=1, disable_number_display=True,
                       key="-LEVEL-", **slider)],

            [sg.Text("Amplitude (% of level)", text_color=MUTED,
                     background_color=PANEL, font=("Helvetica", 9)),
             sg.Text("", key="-AMPL-TXT-", size=(18, 1), font=("Courier", 9),
                     text_color=MUTED, background_color=PANEL)],
            [sg.Slider(range=(0, 200), default_value=0, resolution=1,
                       key="-AMPL-", **slider)],

            [sg.Text("Period (s)", text_color=MUTED, background_color=PANEL,
                     font=("Helvetica", 9))],
            [sg.Slider(range=(0.1, 20.0), default_value=2.0, resolution=0.1,
                       key="-PERIOD-", **slider)],

            [sg.Text("Noise (% of level)", text_color=MUTED,
                     background_color=PANEL, font=("Helvetica", 9))],
            [sg.Slider(range=(0.0, 5.0), default_value=0.02, resolution=0.01,
                       key="-NOISE-", **slider)],

            [sg.Text("Settling τ (ms)  —  STEP only", text_color=MUTED,
                     background_color=PANEL, font=("Helvetica", 9))],
            [sg.Slider(range=(1, 3000), default_value=200, resolution=1,
                       key="-TAU-", **slider)],
        ], font=("Helvetica", 9, "bold"), title_color=MUTED,
            background_color=PANEL, pad=(8, 8))

        log = sg.Frame("SERIAL TRAFFIC", [
            [sg.Multiline("", key="-LOG-", size=(118, 15), autoscroll=True,
                          disabled=True, font=("Courier", 9),
                          background_color=BG, text_color=FG,
                          expand_x=True, expand_y=True)],
            [sg.Text("client: none", key="-CLIENT-", size=(62, 1),
                     font=("Courier", 10), text_color=MUTED,
                     background_color=PANEL),
             sg.Text("", key="-COUNTS-", size=(34, 1), font=("Courier", 10),
                     text_color=MUTED, background_color=PANEL),
             sg.Button("Pause", key="-PAUSE-", size=(7, 1)),
             sg.Button("Clear", key="-CLEAR-", size=(7, 1)),
             sg.Button("Save log", key="-SAVELOG-", size=(9, 1))],
        ], font=("Helvetica", 9, "bold"), title_color=MUTED,
            background_color=PANEL, pad=(8, 8))

        layout = [[sg.Column([[front]], vertical_alignment="top",
                             background_color=BG),
                   sg.Column([[source]], vertical_alignment="top",
                             background_color=BG)],
                  [log]]
        self.window = sg.Window(
            f"Virtual DMM  —  socket://{self.host}:{self.port}",
            layout, background_color=BG, finalize=True, resizable=True)
        window = self.window

        self.append_log("READY", f"listening on socket://{self.host}:{self.port}",
                        MUTED)
        self.append_log("HINT",
                        'ser = serial.serial_for_url("socket://'
                        f'{self.host}:{self.port}", timeout=5)', MUTED)
        return window

    # ------------------------------------------------------------------
    # source panel <-> Source object
    # ------------------------------------------------------------------
    def source_obj(self):
        return self.dmm.sources[self.source_key]

    # The level slider carries permille of 1.2 x full scale, so these two
    # convert between slider ticks and the physical quantity.
    def ticks_to_level(self, ticks):
        return float(ticks) / LEVEL_TICKS * self.source_obj().fullscale * 1.2

    def level_to_ticks(self, level):
        span = self.source_obj().fullscale * 1.2
        ticks = round(level / span * LEVEL_TICKS) if span else 0
        return max(-LEVEL_TICKS, min(LEVEL_TICKS, ticks))

    def load_source_widgets(self):
        """Push the selected source's state into the widgets.

        One direction only.  Widgets write to the Source on their own events;
        this runs only when the selected quantity changes, so the two never
        fight - the race that makes read-back-driven panels so fiddly simply
        does not arise.
        """
        spec = FUNCTIONS[self.source_key]
        src = self.source_obj()
        w = self.window

        w["-SRC-"].update(value=spec["label"])
        choices = [eng(f, spec["unit"]) for f in spec["fullscales"]]
        current = eng(src.fullscale, spec["unit"])
        if current not in choices:
            choices.append(current)
        w["-FS-"].update(values=choices, value=current)

        for name in WAVEFORMS:
            w[f"-W-{name}-"].update(value=(name == src.waveform))

        lo = -LEVEL_TICKS if spec["bipolar"] else 0
        w["-LEVEL-"].update(range=(lo, LEVEL_TICKS),
                            value=max(lo, self.level_to_ticks(src.level)))
        w["-LEVEL-IN-"].update(f"{src.level:g}")
        w["-AMPL-"].update(value=src.amp_pct)
        w["-PERIOD-"].update(value=src.period)
        w["-NOISE-"].update(value=src.noise_pct)
        w["-TAU-"].update(value=src.tau * 1000.0)

    def handle_source_event(self, event, values):
        if not isinstance(event, str) or not values:
            return              # nothing else on this panel emits events
        spec = FUNCTIONS[self.source_key]
        src = self.source_obj()

        if event == "-SRC-":
            for key in SOURCE_KEYS:
                if FUNCTIONS[key]["label"] == values["-SRC-"]:
                    self.source_key = key
                    # Picking a different thing to test also puts the meter on
                    # the matching function.  Anything else is a trap: you
                    # clip a resistor to the leads, the panel says resistance,
                    # and the display carries on reading DC volts.  The 4WΩ and
                    # CONT keys reach the two functions that share this source.
                    self.select_function(key)
                    break
            self.load_source_widgets()
            return

        if event == "-FS-":
            for f in spec["fullscales"]:
                if eng(f, spec["unit"]) == values["-FS-"]:
                    with self.dmm.lock:
                        src.fullscale = f
                    self.load_source_widgets()
                    return
            return

        if event.startswith("-W-"):
            name = event[3:-1]
            with self.dmm.lock:
                src.waveform = name
            return

        if event == "-LEVEL-":
            with self.dmm.lock:
                src.level = self.ticks_to_level(values["-LEVEL-"])
            self.window["-LEVEL-IN-"].update(f"{src.level:.6g}")
            return

        if event in ("-LEVEL-SET-", "-LEVEL-IN-"):
            try:
                value = float(values["-LEVEL-IN-"])
            except ValueError:
                self.append_log("ERR", f"not a number: {values['-LEVEL-IN-']}", BAD)
                return
            with self.dmm.lock:
                src.level = value
                # A typed value outside the slider's span pulls the full-scale
                # setting up with it, rather than being silently clipped.
                while (abs(value) > src.fullscale * 1.2
                       and src.fullscale < spec["fullscales"][-1]):
                    src.fullscale = next(
                        f for f in spec["fullscales"] if f > src.fullscale)
            self.load_source_widgets()
            return

        with self.dmm.lock:
            if event == "-AMPL-":
                src.amp_pct = float(values["-AMPL-"])
            elif event == "-PERIOD-":
                src.period = float(values["-PERIOD-"])
            elif event == "-NOISE-":
                src.noise_pct = float(values["-NOISE-"])
            elif event == "-TAU-":
                src.tau = float(values["-TAU-"]) / 1000.0

    # ------------------------------------------------------------------
    # traffic log
    # ------------------------------------------------------------------
    def append_log(self, kind, text, colour, gap_ms=None, rt_ms=None):
        stamp = datetime.datetime.now().strftime("%H:%M:%S.%f")[:-3]
        gap = f"{gap_ms:8.1f}ms" if gap_ms is not None else " " * 10
        rt = f"  [{rt_ms:.1f} ms]" if rt_ms is not None else ""
        line = f"{stamp} {gap}  {kind:<4} {text}{rt}"
        self.log_lines.append(line)
        if not self.log_paused:
            self.window["-LOG-"].print(line, text_color=colour)

    def drain_traffic(self):
        drained = 0
        while drained < 200:
            try:
                when, kind, text, extra = TRAFFIC.get_nowait()
            except queue.Empty:
                break
            drained += 1
            gap = None
            if self.last_event_t is not None:
                gap = (when - self.last_event_t) * 1000.0
            self.last_event_t = when

            if kind == "TX":
                self.tx_count += 1
                self.append_log("TX", text, TX_COLOUR, gap)
            elif kind == "RX":
                self.rx_bytes += len(text)
                shown = text if len(text) <= 96 else \
                    text[:93] + f"... ({len(text)} bytes)"
                self.append_log("RX", shown, RX_COLOUR, gap, extra)
            elif kind == "ERR":
                self.append_log("ERR", text, BAD, gap)
            elif kind == "CONN":
                self.append_log("--", f"client connected from {text}", GOOD, gap)
            elif kind == "DISC":
                self.append_log("--", f"client disconnected {text}", WARN, gap)

    # ------------------------------------------------------------------
    # display refresh
    # ------------------------------------------------------------------
    def refresh(self):
        dmm = self.dmm
        with dmm.lock:
            function = dmm.function
            # A reading taken for the display only.  It does not touch the
            # buffer, so it can never steal a sample from the client.
            value = dmm.measure()
            rng = dmm.ranges[function]
            auto = dmm.autorange[function]
            digits = dmm.digits_for(function)
            resolution = dmm.resolution(function)
            disp_rng = dmm.display_range(function, value)
            nplc = dmm.nplc[function]
            trig = dmm.trig_source
            samples = dmm.total_samples()
            buffered = len(dmm.buffer)
            text = dmm.display_text
            true_value = dmm.raw_value(function)
            armed = dmm.armed

        spec = FUNCTIONS[function]
        over = dmm.overloaded(value, function) if not dmm.strict else value >= 1e37
        digit_text, unit = format_reading(value, disp_rng, resolution,
                                          spec["unit"], overload=over)
        self.draw_lcd(digit_text, unit, spec["annun"], text)

        w = self.window
        w["-A-FUNC-"].update(spec["annun"])
        w["-A-AUTO-"].update("AUTO" if auto else "MAN",
                             text_color=WARN if auto else FG)
        w["-A-RANGE-"].update(f"RANGE {eng(rng, spec['unit'])}")
        w["-A-NPLC-"].update(f"NPLC {nplc:g}" if spec["nplc"] else "NPLC  -")
        w["-A-DIG-"].update(f"{int(digits)}½ DIGITS")
        w["-A-TRIG-"].update(f"TRIG {trig}" + (" ARM" if armed else ""))
        connected = bool(self.server.clients)
        w["-A-REM-"].update("REM" if connected else "LCL",
                            text_color=SEG_ON if connected else SEG_OFF)

        w["-TRUE-"].update(eng(true_value, spec["unit"], digits=6))
        w["-BUF-"].update(f"buffer {buffered}/{samples} readings")

        src = self.source_obj()
        sspec = FUNCTIONS[self.source_key]
        w["-LEVEL-TXT-"].update(eng(src.level, sspec["unit"]))
        w["-AMPL-TXT-"].update("± " + eng(src.amplitude(), sspec["unit"]))

        if connected:
            peer = sorted(self.server.clients)[0]
            w["-CLIENT-"].update(
                f"client {peer}  on socket://{self.host}:{self.port}",
                text_color=GOOD)
        else:
            w["-CLIENT-"].update(
                f"no client  —  listening on socket://{self.host}:{self.port}",
                text_color=MUTED)
        w["-COUNTS-"].update(f"{self.tx_count} commands  {self.rx_bytes} B out")

    def draw_lcd(self, digit_text, unit, annunciator, message):
        graph = self.window["-LCD-"]
        graph.erase()

        cell_w, cell_h, thick = 44, 96, 10
        pitch = cell_w + 14
        x, y = 16, 34

        # The string is right-justified into nine cells: one for the sign plus
        # eight for digits, so the decimal point does not walk about as the
        # range changes.
        chars = []
        for ch in digit_text:
            if ch == "." and chars:
                chars[-1] = (chars[-1][0], True)
            else:
                chars.append((ch, False))
        chars = chars[-9:]
        start = x + (9 - len(chars)) * pitch

        for i, (ch, dot) in enumerate(chars):
            draw_seven_segment(graph, ch, start + i * pitch, y,
                               cell_w, cell_h, thick, dot=dot)

        graph.draw_text(unit, (DISP_W - 56, y + cell_h - 18), color=SEG_ON,
                        font=("Courier", 22, "bold"))
        graph.draw_text(annunciator, (48, 16), color=SEG_ON,
                        font=("Courier", 12, "bold"))
        if message:
            graph.draw_text(message, (DISP_W / 2, DISP_H - 18), color=WARN,
                            font=("Courier", 13))

    # ------------------------------------------------------------------
    # function selection
    # ------------------------------------------------------------------
    def select_function(self, function):
        """Press a function key.

        Sets exactly the state CONFigure sets and nothing else, so the panel
        and a notebook are genuinely doing the same thing rather than two
        similar things.  The source panel swings round to match on the next
        refresh, because the auto-follow in run() watches dmm.function.
        """
        if function not in FUNCTIONS:
            return
        with self.dmm.lock:
            self.dmm.function = function
        self.append_log("--", f"front panel: {FUNCTIONS[function]['label']} "
                              f"(same as CONF:{function})", MUTED)

    def sync_function_buttons(self, function):
        for _, key in FUNC_BUTTONS:
            self.window[f"-F-{key}-"].update(
                button_color=FUNC_BTN_ON if key == function else FUNC_BTN_OFF)

    # ------------------------------------------------------------------
    # main loop
    # ------------------------------------------------------------------
    def run(self):
        sg = self.sg
        period = 1.0 / REFRESH_HZ
        next_refresh = 0.0
        while True:
            event, values = self.window.read(timeout=40)
            if event in (sg.WIN_CLOSED, None):
                break

            if event == "-PAUSE-":
                self.log_paused = not self.log_paused
                self.window["-PAUSE-"].update("Resume" if self.log_paused
                                              else "Pause")
            elif event == "-CLEAR-":
                self.log_lines = []
                self.window["-LOG-"].update("")
            elif event == "-SAVELOG-":
                self.save_log()
            elif isinstance(event, str) and event.startswith("-F-"):
                self.select_function(event[3:-1])
            elif event != sg.TIMEOUT_KEY:
                self.handle_source_event(event, values)

            self.drain_traffic()

            now = time.monotonic()
            if now >= next_refresh:
                next_refresh = now + period
                # The source panel follows the instrument: send CONF:RES from a
                # notebook and the panel swings round to the resistor, which is
                # the thing you would have reached for on a real bench.
                if self.dmm.function != self.last_function:
                    self.last_function = self.dmm.function
                    self.sync_function_buttons(self.last_function)
                    key = SOURCE_ALIAS.get(self.last_function, self.last_function)
                    if key in SOURCE_KEYS and key != self.source_key:
                        self.source_key = key
                        self.load_source_widgets()
                self.refresh()

        self.window.close()

    def save_log(self):
        stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        name = f"dmm_traffic_{stamp}.csv"
        with open(name, "w", newline="", encoding="utf-8") as fh:
            writer = csv.writer(fh)
            writer.writerow(["line"])
            writer.writerows([[line] for line in self.log_lines])
        self.append_log("--", f"log saved to {name}", GOOD)


# ==========================================================================
# PART 6  ENTRY POINT
# ==========================================================================

def main():
    ap = argparse.ArgumentParser(
        description="Virtual bench multimeter on a PySerial socket:// endpoint")
    ap.add_argument("--host", default="127.0.0.1",
                    help="interface to listen on (default 127.0.0.1; use "
                         "0.0.0.0 to let the room connect)")
    ap.add_argument("--port", type=int, default=5025,
                    help="TCP port (default 5025)")
    ap.add_argument("--headless", action="store_true",
                    help="run the simulator with no front panel")
    ap.add_argument("--strict", action="store_true",
                    help="return 9.9E+37 on overload instead of the true value")
    ap.add_argument("-v", "--verbose", action="store_true",
                    help="echo every exchange to stdout")
    args = ap.parse_args()

    if port_in_use(args.host, args.port):
        raise SystemExit(
            f"[dmm] something is already listening on {args.host}:{args.port} - "
            f"use --port to pick another one")

    dmm = SimDMM(strict=args.strict)
    server = start_server(dmm, args.host, args.port, verbose=args.verbose)

    print(f'[dmm] connect with: serial.serial_for_url'
          f'("socket://{args.host}:{args.port}", timeout=5)')

    if args.headless:
        try:
            while True:
                time.sleep(0.5)
        except KeyboardInterrupt:
            print("\n[dmm] stopped")
        return

    DmmPanel(dmm, server, args.host, args.port).run()
    server.shutdown()


if __name__ == "__main__":
    main()
