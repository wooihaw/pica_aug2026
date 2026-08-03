#!/usr/bin/env python3
"""
virtual_scope.py - a complete oscilloscope teaching rig in one file.

This is scope_sim.py + scope_driver.py + scope_gui_psg.py merged.  The three
layers are still three layers; they are just no longer three files:

    PART 1  SIMULATOR   a virtual 2-channel oscilloscope that speaks SCPI over TCP
    PART 2  DRIVER      a small PyVISA client that knows nothing about part 1
    PART 3  FRONT PANEL a PySimpleGUI display that knows nothing about part 1
    PART 4  ENTRY POINT wires them together

The layers still talk over a real TCP socket on 127.0.0.1:5025, not by calling
each other's Python functions.  That is deliberate.  It means the driver code
below is byte-for-byte the code you would use against real hardware on the
bench - only the resource string changes:

    scope = Scope("TCPIP0::127.0.0.1::5025::SOCKET")          # simulator
    scope = Scope("USB0::0x2A8D::0x0396::CN12345678::INSTR")  # real hardware

Install:

    pip install "pysimplegui>=6.0" pyvisa pyvisa-py numpy

Usage:

    python virtual_scope.py                  simulator + front panel, one window
    python virtual_scope.py --sim-only       just the simulator, no GUI
                                             (the old two-terminal workflow)
    python virtual_scope.py --no-sim         front panel only; connect to a
                                             simulator someone else is running
    python virtual_scope.py --resource USB0::0x2A8D::0x0396::CN12345678::INSTR
                                             front panel against real hardware
    python virtual_scope.py -v               log every SCPI exchange

Implemented SCPI subset (long and short forms, case-insensitive):

    *IDN?  *RST  *CLS  *OPC?  *ESR?  :SYSTem:ERRor?
    :CHANnel<n>:SCALe|OFFSet|COUPling|DISPlay|PROBe   (set and query)
    :TIMebase:SCALe|POSition                          (set and query)
    :TRIGger:SWEep
    :TRIGger:EDGE:SOURce|LEVel|SLOPe                  (set and query)
    :RUN  :STOP  :SINGle  :AUToscale  :DIGitize
    :MEASure:VPP?|VMAX?|VMIN?|VAMPlitude?|VAVerage?|VRMS?|FREQuency?|PERiod?
    :WAVeform:SOURce|FORMat|POINts|BYTeorder          (set and query)
    :WAVeform:PREamble?  :WAVeform:DATA?
    :SIMulate:...                                     (non-standard, see below)

The :SIMulate: subsystem is NOT part of any real instrument.  It changes the
"device under test" hanging off the probes so participants see the display
react:

    :SIMulate:CHANnel1:SHAPe SQUare
    :SIMulate:CHANnel1:FREQuency 2500
    :SIMulate:CHANnel1:AMPLitude 1.5      (amplitude, i.e. Vpp/2)
    :SIMulate:CHANnel1:DCOFfset 0
    :SIMulate:CHANnel1:NOISe 0.01
"""

import argparse
import csv
import datetime
import math
import queue
import random
import socket
import socketserver
import sys
import threading
import time

import numpy as np

PROGRAM = "virtual_scope.py"

# PySimpleGUI is imported lazily in main() so that --sim-only works on a
# machine with no GUI toolkit installed (a headless lab PC, a CI runner).
sg = None


# ==========================================================================
# PART 1 - THE SIMULATOR
#
# Everything in this section is prefixed Sim/sim_/SIM_ so that it can never be
# confused with the driver or the front panel further down.  In the original
# three-file layout the module name did that job.
# ==========================================================================

SIM_IDN = "KEYSIGHT TECHNOLOGIES,DSOX1204G-SIM,SIM0000001,02.11.2020"
SIM_V_DIVISIONS = 8          # vertical divisions on screen
SIM_H_DIVISIONS = 10         # horizontal divisions on screen
SIM_N_CHANNELS = 2
SIM_NOT_AVAILABLE = 9.9e37   # the "measurement could not be made" value

# Allowed 1-2-5 sequences, same as the real front panel knobs
SIM_V_SCALES = [1e-3, 2e-3, 5e-3, 1e-2, 2e-2, 5e-2, 1e-1, 2e-1, 5e-1,
                1.0, 2.0, 5.0, 10.0]
SIM_T_SCALES = [1e-8, 2e-8, 5e-8, 1e-7, 2e-7, 5e-7, 1e-6, 2e-6, 5e-6,
                1e-5, 2e-5, 5e-5, 1e-4, 2e-4, 5e-4, 1e-3, 2e-3, 5e-3,
                1e-2, 2e-2, 5e-2, 1e-1, 2e-1, 5e-1, 1.0]


def sim_snap(value, allowed):
    """Round a requested setting to the nearest available knob position.
    Real instruments do exactly this - ask for 0.3 V/div and you get 0.5."""
    value = abs(value)
    if value <= 0:
        return allowed[0]
    return min(allowed, key=lambda a: abs(math.log10(a) - math.log10(value)))


# --------------------------------------------------------------------------
# The "device under test" - what is physically connected to the probe
# --------------------------------------------------------------------------
class SimSignalSource:
    """An ideal function generator feeding one channel."""

    def __init__(self, shape="SIN", freq=1000.0, amplitude=1.0,
                 dc=0.0, noise=0.005):
        self.shape = shape          # SIN | SQU | RAMP | TRI | DC | NOIS
        self.freq = freq            # Hz
        self.amplitude = amplitude  # volts peak (so Vpp = 2 * amplitude)
        self.dc = dc                # volts
        self.noise = noise          # volts RMS

    def sample(self, t):
        """Return the source voltage at absolute times `t` (numpy array)."""
        phase = 2.0 * np.pi * self.freq * t
        if self.shape == "SIN":
            v = np.sin(phase)
        elif self.shape == "SQU":
            v = np.sign(np.sin(phase))
            v[v == 0] = 1.0
        elif self.shape == "RAMP":
            frac = np.mod(phase / (2 * np.pi), 1.0)
            v = 2.0 * frac - 1.0
        elif self.shape == "TRI":
            frac = np.mod(phase / (2 * np.pi), 1.0)
            v = 4.0 * np.abs(frac - 0.5) - 1.0
        elif self.shape == "NOIS":
            v = np.zeros_like(t)
        else:  # DC
            v = np.zeros_like(t)
        v = self.amplitude * v + self.dc
        if self.noise > 0:
            v = v + np.random.normal(0.0, self.noise, size=t.shape)
        return v


# --------------------------------------------------------------------------
# Channel front-end settings
# --------------------------------------------------------------------------
class SimChannel:
    def __init__(self, number, source):
        self.number = number
        self.source = source
        self.reset()

    def reset(self):
        self.scale = 1.0        # volts/division
        self.offset = 0.0       # volts at the centre of the screen
        self.coupling = "DC"
        self.display = True
        self.probe = 1.0        # attenuation ratio

    @property
    def full_scale(self):
        return SIM_V_DIVISIONS * self.scale

    def acquire(self, t):
        """Voltage seen by the ADC, after probe and coupling."""
        v = self.source.sample(t) / self.probe
        if self.coupling == "AC":
            v = v - np.mean(v)
        return v


# --------------------------------------------------------------------------
# The instrument itself
# --------------------------------------------------------------------------
class SimOscilloscope:
    def __init__(self):
        # Two different signals so participants have something to compare
        self.channels = {
            1: SimChannel(1, SimSignalSource("SIN", 1000.0, 1.0, 0.0, 0.005)),
            2: SimChannel(2, SimSignalSource("SQU", 500.0, 0.75, 0.0, 0.004)),
        }
        self.lock = threading.Lock()
        self.reset()

    def reset(self):
        for ch in self.channels.values():
            ch.reset()
        self.time_scale = 500e-6      # seconds/division
        self.time_position = 0.0      # delay of trigger from screen centre
        self.trig_source = 1
        self.trig_level = 0.0
        self.trig_slope = "POS"
        self.trig_sweep = "AUTO"      # AUTO | NORMal
        self.running = True
        self.wav_source = 1
        self.wav_format = "BYTE"      # BYTE | WORD | ASCii
        self.wav_points = 1000
        self.byte_order = "LSBF"
        self.error_queue = []
        self._frozen = None

    # ---------------- acquisition ----------------
    @property
    def time_span(self):
        return SIM_H_DIVISIONS * self.time_scale

    def _find_trigger_time(self):
        """Locate an absolute time at which the trigger condition is met.

        This is what gives a real scope a *stable* display: every sweep starts
        at the same point on the waveform.  If no crossing exists (level set
        outside the signal) we fall back to a random start time, which is
        exactly why an untriggered trace 'runs' across the screen.
        """
        ch = self.channels[self.trig_source]
        src = ch.source
        if src.freq <= 0 or src.shape in ("DC", "NOIS"):
            return random.uniform(0.0, 1.0)

        period = 1.0 / src.freq
        # Search one period at fine resolution, without the noise term
        clean = SimSignalSource(src.shape, src.freq, src.amplitude, src.dc, 0.0)
        t = np.linspace(0.0, period, 4096, endpoint=False)
        v = clean.sample(t) / ch.probe
        if ch.coupling == "AC":
            v = v - np.mean(v)

        d = v - self.trig_level
        if self.trig_slope == "POS":
            idx = np.where((d[:-1] <= 0) & (d[1:] > 0))[0]
        elif self.trig_slope == "NEG":
            idx = np.where((d[:-1] >= 0) & (d[1:] < 0))[0]
        else:  # EITHer
            idx = np.where(np.sign(d[:-1]) != np.sign(d[1:]))[0]

        if len(idx) == 0:
            # No trigger.  AUTO sweep free-runs; NORMal would hold the last
            # trace, but for teaching purposes free-running is more visible.
            return random.uniform(0.0, period)

        i = idx[0]
        # Linear interpolation between the two straddling samples
        frac = -d[i] / (d[i + 1] - d[i]) if d[i + 1] != d[i] else 0.0
        t_trig = t[i] + frac * (t[1] - t[0])
        # Advance by a whole number of periods so the trace looks "live"
        return t_trig + period * random.randint(0, 50)

    def invalidate(self):
        """Horizontal settings changed - the frozen record is no longer valid."""
        self._frozen = None

    def _sweep(self):
        """Perform one real acquisition of every channel.

        `t_disp` is time relative to the trigger event.  :TIMebase:POSition
        shifts the window: positive delay shows what happened *after* the
        trigger, exactly as on the real instrument.
        """
        n = self.wav_points
        span = self.time_span
        pos = self.time_position
        t_disp = np.linspace(pos - span / 2.0, pos + span / 2.0, n, endpoint=False)
        t_abs = self._find_trigger_time() + t_disp
        volts = {c: ch.acquire(t_abs) for c, ch in self.channels.items()}
        return t_disp, volts

    def capture(self, channel):
        """Return (time_axis, volts) for one screen of data on one channel.

        While RUNning every call is a fresh sweep - just like a real scope,
        where :WAVeform:PREamble? and :WAVeform:DATA? can land on different
        acquisitions.  Use :DIGitize or :STOP first if you need a coherent
        record.  Once STOPped the record is frozen, so changing V/div
        rescales the *same* captured samples instead of re-measuring.
        """
        if self.running or self._frozen is None:
            record = self._sweep()
            if not self.running:
                self._frozen = record
        else:
            record = self._frozen
        t_disp, volts = record
        return t_disp, volts[channel]

    def freeze(self):
        """Take one acquisition and hold it (:STOP, :SINGle, :DIGitize)."""
        self.running = False
        self._frozen = self._sweep()

    # ---------------- waveform transfer ----------------
    def preamble_and_codes(self):
        """Digitise one screen exactly the way the real instrument does."""
        ch = self.channels[self.wav_source]
        t_disp, v = self.capture(self.wav_source)

        levels = 65536 if self.wav_format == "WORD" else 256
        y_ref = levels // 2
        y_inc = ch.full_scale / levels
        y_org = ch.offset                       # volts at the reference level

        codes = np.round((v - y_org) / y_inc) + y_ref
        codes = np.clip(codes, 0, levels - 1).astype(
            np.uint16 if levels == 65536 else np.uint8)

        x_inc = self.time_span / self.wav_points
        x_org = float(t_disp[0])
        fmt_code = {"BYTE": 0, "WORD": 1, "ASC": 4}[self.wav_format]
        preamble = (fmt_code, 2, self.wav_points, 1,
                    x_inc, x_org, 0, y_inc, y_org, y_ref)
        return preamble, codes

    def waveform_block(self):
        """IEEE 488.2 definite-length arbitrary block, as :WAV:DATA? returns."""
        pre, codes = self.preamble_and_codes()
        if self.wav_format == "ASC":
            y_inc, y_org, y_ref = pre[7], pre[8], pre[9]
            volts = (codes.astype(float) - y_ref) * y_inc + y_org
            return ",".join(f"{x:.6E}" for x in volts).encode() + b"\n"

        if self.wav_format == "WORD":
            raw = codes.astype("<u2" if self.byte_order == "LSBF" else ">u2").tobytes()
        else:
            raw = codes.tobytes()
        header = f"#{len(str(len(raw)))}{len(raw)}".encode()
        return header + raw + b"\n"

    # ---------------- measurements ----------------
    def measure(self, kind, channel):
        _, v = self.capture(channel)
        ch = self.channels[channel]
        top, bottom = ch.offset + ch.full_scale / 2, ch.offset - ch.full_scale / 2
        if np.max(v) > top or np.min(v) < bottom:
            return SIM_NOT_AVAILABLE      # clipped - real scopes refuse too

        if kind == "VPP":
            return float(np.max(v) - np.min(v))
        if kind == "VMAX":
            return float(np.max(v))
        if kind == "VMIN":
            return float(np.min(v))
        if kind == "VAMP":
            return float(np.max(v) - np.min(v))
        if kind == "VAV":
            return float(np.mean(v))
        if kind == "VRMS":
            return float(np.sqrt(np.mean(v ** 2)))
        if kind in ("FREQ", "PER"):
            mid = (np.max(v) + np.min(v)) / 2.0
            d = v - mid
            idx = np.where((d[:-1] <= 0) & (d[1:] > 0))[0]
            if len(idx) < 2:
                return SIM_NOT_AVAILABLE   # fewer than 2 cycles on screen
            dt = self.time_span / self.wav_points
            period = float(np.mean(np.diff(idx))) * dt
            return 1.0 / period if kind == "FREQ" else period
        return SIM_NOT_AVAILABLE


# --------------------------------------------------------------------------
# SCPI parsing
#
# In SCPI every keyword has a long form and a short form; the short form is
# the upper-case part of the long form (CHANnel -> CHAN).  Instruments accept
# either, in any case.  We normalise everything to the short form.
# --------------------------------------------------------------------------
SIM_KEYWORDS = {
    "CHANNEL": "CHAN", "CHAN": "CHAN",
    "TIMEBASE": "TIM", "TIM": "TIM",
    "TRIGGER": "TRIG", "TRIG": "TRIG",
    "MEASURE": "MEAS", "MEAS": "MEAS",
    "WAVEFORM": "WAV", "WAV": "WAV",
    "SYSTEM": "SYST", "SYST": "SYST",
    "SIMULATE": "SIM", "SIM": "SIM",
    "SCALE": "SCAL", "SCAL": "SCAL",
    "OFFSET": "OFFS", "OFFS": "OFFS",
    "POSITION": "POS", "POS": "POS",
    "COUPLING": "COUP", "COUP": "COUP",
    "DISPLAY": "DISP", "DISP": "DISP",
    "PROBE": "PROB", "PROB": "PROB",
    "SOURCE": "SOUR", "SOUR": "SOUR",
    "LEVEL": "LEV", "LEV": "LEV",
    "SLOPE": "SLOP", "SLOP": "SLOP",
    "SWEEP": "SWE", "SWE": "SWE",
    "EDGE": "EDGE",
    "FORMAT": "FORM", "FORM": "FORM",
    "POINTS": "POIN", "POIN": "POIN",
    "PREAMBLE": "PRE", "PRE": "PRE",
    "BYTEORDER": "BYT", "BYT": "BYT",
    "DATA": "DATA",
    "ERROR": "ERR", "ERR": "ERR",
    "VPP": "VPP", "VMAX": "VMAX", "VMIN": "VMIN",
    "VAMPLITUDE": "VAMP", "VAMP": "VAMP",
    "VAVERAGE": "VAV", "VAV": "VAV",
    "VRMS": "VRMS",
    "FREQUENCY": "FREQ", "FREQ": "FREQ",
    "PERIOD": "PER", "PER": "PER",
    "AMPLITUDE": "AMPL", "AMPL": "AMPL",
    "SHAPE": "SHAP", "SHAP": "SHAP",
    "DCOFFSET": "DCOF", "DCOF": "DCOF",
    "NOISE": "NOIS", "NOIS": "NOIS",
    "RUN": "RUN", "STOP": "STOP",
    "SINGLE": "SING", "SING": "SING",
    "DIGITIZE": "DIG", "DIG": "DIG",
    "AUTOSCALE": "AUT", "AUT": "AUT",
}


def sim_normalise(header):
    """':chan1:SCALe' -> (':CHAN:SCAL', [1])   -- returns path and suffixes."""
    header = header.strip().lstrip(":")
    path, suffixes = [], []
    for token in header.split(":"):
        digits = ""
        while token and token[-1].isdigit():
            digits = token[-1] + digits
            token = token[:-1]
        key = SIM_KEYWORDS.get(token.upper())
        if key is None:
            raise KeyError(token)
        path.append(key)
        if digits:
            suffixes.append(int(digits))
    return ":" + ":".join(path), suffixes


def sim_on_off(text):
    return text.strip().upper() in ("1", "ON", "TRUE")


class ScpiError(Exception):
    pass


# --------------------------------------------------------------------------
# Command dispatch
# --------------------------------------------------------------------------
def sim_execute(scope, line):
    """Run one SCPI message.  Returns bytes to send back, or None."""
    line = line.strip()
    if not line:
        return None

    # ---- IEEE 488.2 common commands ----
    upper = line.upper()
    if upper == "*IDN?":
        return SIM_IDN.encode() + b"\n"
    if upper == "*RST":
        scope.reset()
        return None
    if upper == "*CLS":
        scope.error_queue.clear()
        return None
    if upper in ("*OPC?", "*ESR?"):
        return b"1\n" if upper == "*OPC?" else b"0\n"
    if upper.startswith("*"):
        return None

    # The '?' marks the *header* as a query and may be followed by parameters,
    # e.g. ":MEASure:VPP? CHANnel1".  So split the header off first.
    header, _, arg = line.partition(" ")
    arg = arg.strip()
    query = header.endswith("?")
    if query:
        header = header[:-1]

    try:
        path, sfx = sim_normalise(header)
    except KeyError as exc:
        raise ScpiError(f'-113,"Undefined header: {exc.args[0]}"')

    ch_no = sfx[0] if sfx else None
    chan = scope.channels.get(ch_no) if ch_no else None
    if path.startswith(":CHAN") and chan is None:
        raise ScpiError('-222,"Data out of range: channel"')

    # ---- :CHANnel<n>: ----
    if path == ":CHAN:SCAL":
        if query:
            return f"{chan.scale:.4E}\n".encode()
        chan.scale = sim_snap(float(arg), SIM_V_SCALES)
        return None
    if path == ":CHAN:OFFS":
        if query:
            return f"{chan.offset:.4E}\n".encode()
        chan.offset = float(arg)
        return None
    if path == ":CHAN:COUP":
        if query:
            return chan.coupling.encode() + b"\n"
        chan.coupling = "AC" if arg.upper().startswith("AC") else "DC"
        return None
    if path == ":CHAN:DISP":
        if query:
            return (b"1\n" if chan.display else b"0\n")
        chan.display = sim_on_off(arg)
        return None
    if path == ":CHAN:PROB":
        if query:
            return f"{chan.probe:.4E}\n".encode()
        chan.probe = float(arg)
        return None

    # ---- :TIMebase: ----
    if path == ":TIM:SCAL":
        if query:
            return f"{scope.time_scale:.4E}\n".encode()
        scope.time_scale = sim_snap(float(arg), SIM_T_SCALES)
        scope.invalidate()
        return None
    if path == ":TIM:POS":
        if query:
            return f"{scope.time_position:.4E}\n".encode()
        scope.time_position = float(arg)
        scope.invalidate()
        return None

    # ---- :TRIGger: ----
    if path == ":TRIG:EDGE:SOUR":
        if query:
            return f"CHAN{scope.trig_source}\n".encode()
        digits = "".join(c for c in arg if c.isdigit())
        scope.trig_source = int(digits) if digits else 1
        return None
    if path == ":TRIG:EDGE:LEV":
        if query:
            return f"{scope.trig_level:.4E}\n".encode()
        scope.trig_level = float(arg.split(",")[0])
        return None
    if path == ":TRIG:EDGE:SLOP":
        if query:
            return scope.trig_slope.encode() + b"\n"
        scope.trig_slope = arg.upper()[:3].replace("EIT", "EITH")
        return None
    if path == ":TRIG:SWE":
        if query:
            return scope.trig_sweep.encode() + b"\n"
        scope.trig_sweep = "NORM" if arg.upper().startswith("NORM") else "AUTO"
        return None

    # ---- acquisition control ----
    if path == ":RUN":
        scope.running = True
        scope.invalidate()
        return None
    if path in (":STOP", ":SING", ":DIG"):
        # :SINGle and :DIGitize each take exactly one acquisition and hold it.
        # This is the documented way to fetch a coherent PREamble + DATA pair.
        scope.freeze()
        return None
    if path == ":AUT":
        # Autoscale: pick sensible knob settings for channel 1's signal
        src = scope.channels[1].source
        scope.channels[1].scale = sim_snap(max(src.amplitude, 1e-3) / 2.5,
                                           SIM_V_SCALES)
        scope.channels[1].offset = src.dc
        if src.freq > 0:
            scope.time_scale = sim_snap(2.0 / (src.freq * SIM_H_DIVISIONS),
                                        SIM_T_SCALES)
        scope.trig_level = src.dc
        return None

    # ---- :MEASure: ----
    if path.startswith(":MEAS:"):
        kind = path.split(":")[2]
        target = 1
        if arg:
            digits = "".join(c for c in arg if c.isdigit())
            target = int(digits) if digits else 1
        elif ch_no:
            target = ch_no
        if target not in scope.channels:
            raise ScpiError('-222,"Data out of range: channel"')
        return f"{scope.measure(kind, target):.6E}\n".encode()

    # ---- :WAVeform: ----
    if path == ":WAV:SOUR":
        if query:
            return f"CHAN{scope.wav_source}\n".encode()
        digits = "".join(c for c in arg if c.isdigit())
        scope.wav_source = int(digits) if digits else 1
        return None
    if path == ":WAV:FORM":
        if query:
            return scope.wav_format.encode() + b"\n"
        a = arg.upper()
        scope.wav_format = "WORD" if a.startswith("WORD") else \
                           "ASC" if a.startswith("ASC") else "BYTE"
        return None
    if path == ":WAV:POIN":
        if query:
            return f"{scope.wav_points}\n".encode()
        scope.wav_points = max(100, min(int(float(arg)), 62500))
        scope.invalidate()
        return None
    if path == ":WAV:BYT":
        if query:
            return scope.byte_order.encode() + b"\n"
        scope.byte_order = "MSBF" if arg.upper().startswith("MSB") else "LSBF"
        return None
    if path == ":WAV:PRE":
        p = scope.preamble_and_codes()[0]
        text = ",".join(str(x) if isinstance(x, int) else f"{x:.6E}" for x in p)
        return text.encode() + b"\n"
    if path == ":WAV:DATA":
        return scope.waveform_block()

    # ---- :SYSTem:ERRor? ----
    if path == ":SYST:ERR":
        if scope.error_queue:
            return scope.error_queue.pop(0).encode() + b"\n"
        return b'+0,"No error"\n'

    # ---- :SIMulate: (not a real instrument command) ----
    if path.startswith(":SIM:CHAN:"):
        src = scope.channels[ch_no].source if ch_no in scope.channels else None
        if src is None:
            raise ScpiError('-222,"Data out of range: channel"')
        leaf = path.split(":")[3]
        if leaf == "SHAP":
            if query:
                return src.shape.encode() + b"\n"
            a = arg.upper()[:4]
            src.shape = {"SIN": "SIN", "SINE": "SIN", "SQU": "SQU",
                         "SQUA": "SQU", "RAMP": "RAMP", "TRI": "TRI",
                         "TRIA": "TRI", "DC": "DC", "NOIS": "NOIS"}.get(a, "SIN")
            return None
        attr = {"FREQ": "freq", "AMPL": "amplitude",
                "DCOF": "dc", "NOIS": "noise"}.get(leaf)
        if attr:
            if query:
                return f"{getattr(src, attr):.4E}\n".encode()
            setattr(src, attr, float(arg))
            return None

    raise ScpiError(f'-113,"Undefined header: {header}"')


# --------------------------------------------------------------------------
# TCP server
# --------------------------------------------------------------------------
class SimHandler(socketserver.StreamRequestHandler):
    def handle(self):
        peer = self.client_address
        print(f"[sim] connected from {peer[0]}:{peer[1]}")
        try:
            for raw in self.rfile:
                message = raw.decode(errors="replace").strip()
                if not message:
                    continue
                if self.server.verbose:
                    print(f"[sim] <- {message}")
                # A single message may chain several commands with ';'
                for part in message.split(";"):
                    part = part.strip()
                    if not part:
                        continue
                    try:
                        with self.server.scope.lock:
                            reply = sim_execute(self.server.scope, part)
                    except ScpiError as exc:
                        self.server.scope.error_queue.append(str(exc))
                        print(f"[sim] !! {exc}")
                        reply = None
                    except Exception as exc:                      # noqa: BLE001
                        self.server.scope.error_queue.append(
                            f'-100,"Command error: {exc}"')
                        print(f"[sim] !! {exc}")
                        reply = None
                    if reply is not None:
                        if self.server.verbose:
                            preview = reply[:60]
                            print(f"[sim] -> {preview!r}"
                                  f"{' ...' if len(reply) > 60 else ''}")
                        self.wfile.write(reply)
                        self.wfile.flush()
        except (ConnectionResetError, BrokenPipeError):
            pass
        finally:
            print(f"[sim] disconnected {peer[0]}:{peer[1]}")


class SimServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def sim_port_in_use(host, port, timeout=0.5):
    """Is something already listening there?

    Worth checking before we bind.  allow_reuse_address is set above, and on
    Windows SO_REUSEADDR will happily let a second process bind a port that is
    already being listened on - after which connections are delivered to one
    socket or the other more or less at random.  Better to detect the clash and
    just use the simulator that is already running.
    """
    with socket.socket() as probe:
        probe.settimeout(timeout)
        return probe.connect_ex((host, port)) == 0


def start_simulator(host, port, verbose=False):
    """Bind the listening socket on *this* thread, then serve on another.

    The bind has to happen synchronously.  If it were left to the background
    thread, the front panel would try to connect a few milliseconds later and
    an unbound port refuses the connection rather than queueing it - the
    classic start-up race in this kind of merged script.
    """
    server = SimServer((host, port), SimHandler)   # binds and listens here
    server.scope = SimOscilloscope()
    server.verbose = verbose
    threading.Thread(target=server.serve_forever, name="scope-sim",
                     daemon=True).start()
    return server


def stop_simulator(server):
    """Daemon threads die on exit anyway, but the listening socket does not
    always get released promptly - which makes a quick restart fail."""
    if server is None:
        return
    print("[sim] shutting down")
    server.shutdown()
    server.server_close()


# ==========================================================================
# PART 2 - THE DRIVER
#
# Nothing in this section knows whether it is talking to the simulator above
# or to real hardware.  That is the whole point: participants write this once
# and switch instruments by changing one string.
# ==========================================================================

class Scope:
    # Front-panel knob positions, used to populate the GUI dropdowns
    V_DIV_CHOICES = [1e-3, 2e-3, 5e-3, 1e-2, 2e-2, 5e-2, 1e-1, 2e-1, 5e-1,
                     1.0, 2.0, 5.0]
    T_DIV_CHOICES = [1e-7, 2e-7, 5e-7, 1e-6, 2e-6, 5e-6, 1e-5, 2e-5, 5e-5,
                     1e-4, 2e-4, 5e-4, 1e-3, 2e-3, 5e-3, 1e-2, 5e-2, 1e-1]
    NOT_AVAILABLE = 9.9e37

    def __init__(self, resource, timeout=10000):
        # Imported here rather than at the top of the file so that --sim-only
        # runs on a machine with no VISA stack installed.
        import pyvisa

        is_socket = resource.upper().endswith("SOCKET")

        # For a SOCKET resource, check the plain TCP connection first.  PyVISA
        # reports a failed open as VI_ERROR_TMO (-1073807339), which tells you
        # nothing useful; a direct socket test tells you exactly what is wrong.
        if is_socket:
            self._preflight(resource)

        self.rm = pyvisa.ResourceManager()
        try:
            self.inst = self.rm.open_resource(resource, open_timeout=5000)
        except Exception as exc:                                  # noqa: BLE001
            self.rm.close()
            raise ConnectionError(
                f"Could not open {resource}\n"
                f"  {type(exc).__name__}: {exc}"
            ) from exc

        self.inst.timeout = timeout

        # A raw-socket resource has no message-based framing of its own, so we
        # must tell PyVISA where a message ends.  USB INSTR resources do this
        # for us, and setting it anyway is harmless.
        if is_socket:
            self.inst.read_termination = "\n"
            self.inst.write_termination = "\n"

        try:
            self.idn = self.inst.query("*IDN?").strip()
        except Exception as exc:                                  # noqa: BLE001
            self.close()
            raise ConnectionError(
                f"Opened {resource} but *IDN? timed out.\n"
                f"  {type(exc).__name__}: {exc}\n"
                f"  For a SOCKET resource this almost always means the\n"
                f"  termination characters are wrong, or another VISA backend\n"
                f"  was selected ahead of pyvisa-py."
            ) from exc

    @staticmethod
    def _preflight(resource):
        """Turn a VISA timeout into a sentence a human can act on."""
        parts = resource.split("::")
        try:
            host, port = parts[1], int(parts[2])
        except (IndexError, ValueError):
            return                      # unusual resource string; let VISA try
        try:
            socket.create_connection((host, port), timeout=3).close()
        except ConnectionRefusedError:
            raise ConnectionError(
                f"Nothing is listening on {host}:{port}.\n"
                f"  No simulator is running on that port.  Either drop the\n"
                f"  --no-sim flag so this program starts its own, or start one\n"
                f"  in another terminal with:\n"
                f"      python {PROGRAM} --sim-only --port {port}"
            ) from None
        except socket.timeout:
            raise ConnectionError(
                f"Timed out connecting to {host}:{port}.\n"
                f"  A refusal would be instant, so packets are being dropped -\n"
                f"  usually a firewall or endpoint security product filtering\n"
                f"  loopback traffic."
            ) from None

    # ---------------- housekeeping ----------------
    def close(self):
        try:
            self.inst.close()
        finally:
            self.rm.close()

    def reset(self):
        self.inst.write("*RST")

    def error(self):
        return self.inst.query(":SYSTem:ERRor?").strip()

    # ---------------- vertical ----------------
    def set_volts_per_div(self, channel, volts):
        self.inst.write(f":CHANnel{channel}:SCALe {volts:G}")

    def get_volts_per_div(self, channel):
        return float(self.inst.query(f":CHANnel{channel}:SCALe?"))

    def set_offset(self, channel, volts):
        self.inst.write(f":CHANnel{channel}:OFFSet {volts:G}")

    def get_offset(self, channel):
        return float(self.inst.query(f":CHANnel{channel}:OFFSet?"))

    def set_coupling(self, channel, coupling):
        self.inst.write(f":CHANnel{channel}:COUPling {coupling}")

    def get_coupling(self, channel):
        return self.inst.query(f":CHANnel{channel}:COUPling?").strip().upper()

    def set_display(self, channel, on):
        self.inst.write(f":CHANnel{channel}:DISPlay {1 if on else 0}")

    # ---------------- horizontal ----------------
    def set_time_per_div(self, seconds):
        self.inst.write(f":TIMebase:SCALe {seconds:G}")

    def get_time_per_div(self):
        return float(self.inst.query(":TIMebase:SCALe?"))

    def set_time_position(self, seconds):
        """Time at the centre of the screen, relative to the trigger event.

        Positive values show what happened *after* the trigger.  Bench
        instruments call this the delay or horizontal position.
        """
        self.inst.write(f":TIMebase:POSition {seconds:G}")

    def get_time_position(self):
        return float(self.inst.query(":TIMebase:POSition?"))

    # ---------------- trigger and run control ----------------
    # The three edge parameters are settable one at a time as well as
    # together.  The front panel needs them separate: with only the combined
    # call below, nudging the trigger *level* would also rewrite the source
    # and slope, so the two new dropdowns would snap back on every drag.
    def set_trigger_source(self, channel):
        self.inst.write(f":TRIGger:EDGE:SOURce CHANnel{channel}")

    def get_trigger_source(self):
        """The instrument answers 'CHAN2'; give the caller the number."""
        reply = self.inst.query(":TRIGger:EDGE:SOURce?").strip()
        digits = "".join(c for c in reply if c.isdigit())
        return int(digits) if digits else 1

    def set_trigger_level(self, level):
        self.inst.write(f":TRIGger:EDGE:LEVel {level:G}")

    def get_trigger_level(self):
        return float(self.inst.query(":TRIGger:EDGE:LEVel?"))

    def set_trigger_slope(self, slope):
        """slope: POSitive | NEGative | EITHer (short forms POS/NEG/EITH)."""
        self.inst.write(f":TRIGger:EDGE:SLOPe {slope}")

    def get_trigger_slope(self):
        return self.inst.query(":TRIGger:EDGE:SLOPe?").strip().upper()

    def set_trigger(self, source, level, slope="POSitive"):
        """All three edge parameters at once - the convenient form for a
        script or a notebook.  Kept because the teaching material uses it."""
        self.set_trigger_source(source)
        self.set_trigger_level(level)
        self.set_trigger_slope(slope)

    def get_state(self, channel):
        """Read back everything needed to render the display.

        A front panel should show what the *instrument* is set to, not what
        this program last asked for.  On a real bench someone may be turning
        the knobs while your script runs; here it lets a notebook and the GUI
        drive the same instrument coherently.
        """
        return {
            "v_div": self.get_volts_per_div(channel),
            "t_div": self.get_time_per_div(),
            "offset": self.get_offset(channel),
            "position": self.get_time_position(),
            "trigger": self.get_trigger_level(),
            "coupling": self.get_coupling(channel),
            "trig_source": self.get_trigger_source(),
            "trig_slope": self.get_trigger_slope(),
        }

    def run(self):
        self.inst.write(":RUN")

    def stop(self):
        self.inst.write(":STOP")

    def autoscale(self):
        self.inst.write(":AUToscale")

    # ---------------- measurements ----------------
    def measure(self, kind, channel):
        """kind: VPP, VMAX, VMIN, VAVerage, VRMS, FREQuency, PERiod"""
        value = float(self.inst.query(f":MEASure:{kind}? CHANnel{channel}"))
        return None if value >= 9.9e37 else value

    # ---------------- waveform download ----------------
    def capture(self, channel, points=1000):
        """Return (times in seconds, volts) for one screen of data.

        This is the sequence every instrument programming guide gives you:
          1. choose the source, format and record length
          2. :DIGitize to take one coherent acquisition and hold it
          3. read the PREamble, which carries the scaling constants
          4. read the raw DATA block
          5. convert codes to volts and seconds using the preamble
        """
        self.inst.write(f":WAVeform:SOURce CHANnel{channel}")
        self.inst.write(":WAVeform:FORMat BYTE")
        self.inst.write(f":WAVeform:POINts {points}")
        self.inst.write(f":DIGitize CHANnel{channel}")

        pre = [float(x) for x in self.inst.query(":WAVeform:PREamble?").split(",")]
        n = int(pre[2])
        x_inc, x_org, x_ref = pre[4], pre[5], pre[6]
        y_inc, y_org, y_ref = pre[7], pre[8], pre[9]

        raw = self.inst.query_binary_values(
            ":WAVeform:DATA?", datatype="B", container=list)

        times = [(i - x_ref) * x_inc + x_org for i in range(len(raw))]
        volts = [(code - y_ref) * y_inc + y_org for code in raw]
        return times, volts

    # ---------------- simulator-only helpers ----------------
    def set_test_signal(self, channel, shape=None, freq=None,
                        amplitude=None, dc=None, noise=None):
        """Change the signal the simulator's virtual probe is clipped onto.

        This is NOT a real instrument command - on real hardware you would
        turn the knobs on a function generator instead.
        """
        if shape is not None:
            self.inst.write(f":SIMulate:CHANnel{channel}:SHAPe {shape}")
        if freq is not None:
            self.inst.write(f":SIMulate:CHANnel{channel}:FREQuency {freq:G}")
        if amplitude is not None:
            self.inst.write(f":SIMulate:CHANnel{channel}:AMPLitude {amplitude:G}")
        if dc is not None:
            self.inst.write(f":SIMulate:CHANnel{channel}:DCOFfset {dc:G}")
        if noise is not None:
            self.inst.write(f":SIMulate:CHANnel{channel}:NOISe {noise:G}")


def eng(value, unit=""):
    """Format a number the way a scope front panel does: 500 us, 20 mV."""
    if value is None:
        return "----"
    prefixes = [(1e9, "G"), (1e6, "M"), (1e3, "k"), (1.0, ""),
                (1e-3, "m"), (1e-6, "u"), (1e-9, "n"), (1e-12, "p")]
    a = abs(value)
    for scale, prefix in prefixes:
        if a >= scale or scale == 1e-12:
            return f"{value / scale:.4g} {prefix}{unit}".strip()
    return f"{value:g} {unit}".strip()


# ==========================================================================
# PART 3 - THE FRONT PANEL
#
# Two structural points worth making to participants, because both are forced
# by tkinter rather than by taste:
#
# 1. Tkinter is not thread-safe.  The acquisition thread may not touch a
#    widget.  It posts results back with Window.write_event_value(), and every
#    draw call happens in the main event loop.  The Flet version updates the
#    page straight from its polling thread; tkinter would crash or silently
#    corrupt the canvas.
#
# 2. The graticule is drawn once and left alone.  Only the trace and the two
#    trigger cursors are deleted and redrawn each frame.  Flet rebuilds the
#    whole shape list every update; on a tkinter Canvas that would be wasteful.
# ==========================================================================

SCREEN_W, SCREEN_H = 720, 432          # 10 x 8 divisions, 72 px each
H_DIV, V_DIV = 10, 8
BG = "#0d1117"
PANEL = "#161b22"
GRID = "#2a3038"
AXIS = "#4a545e"
TRACE = ["#ffd400", "#00d4ff"]         # channel 1 yellow, channel 2 cyan
TRIG = "#f78166"
MUTED = "#8b949e"
GOOD = "#3fb950"
BAD = "#f85149"
FG = "#c9d1d9"

POLL_PERIOD = 0.25                     # seconds between acquisitions
SYNC_HOLD = 1.0                        # ignore instrument read-back this long
                                       # after the user moves a control
PENDING_HOLD = 2.0                     # give the instrument this long to
                                       # confirm a setting before believing the
                                       # read-back over the user

# Combo entries: display text -> value, e.g. "500 mV" -> 0.5
VDIV_ITEMS = {eng(v, "V"): v for v in Scope.V_DIV_CHOICES}
TDIV_ITEMS = {eng(t, "s"): t for t in Scope.T_DIV_CHOICES}

# These three combos show the SCPI short forms the instrument itself returns,
# so what is on screen matches what :TRIGger:EDGE:SLOPe? and
# :CHANnel<n>:COUPling? answer in the notebook, character for character.
COUPLING_CHOICES = ["DC", "AC"]
SLOPE_CHOICES = ["POS", "NEG", "EITH"]
SOURCE_CHOICES = ["1", "2"]


def label(text):
    return sg.Text(text, size=(18, 1), font=("Helvetica", 9), text_color=MUTED)


def heading(text):
    return sg.Text(text, font=("Helvetica", 9, "bold"), text_color=MUTED)


class ScopeApp:
    def __init__(self, resource, autoconnect=False):
        self.resource = resource
        self.autoconnect = autoconnect
        self.scope = None
        self.polling = False
        self.channel = 1
        self.last_capture = None
        self.graticule_ids = []
        self.trace_ids = []
        # Per-control timestamp of the last user edit.  While a control is
        # "warm" the instrument read-back is not allowed to snap it back, which
        # would otherwise make the sliders fight the user mid-drag.
        self.touched = {}
        # Last value actually written to the instrument, so that a slider
        # dragging across 40 pixels does not emit 40 identical SCPI writes.
        self.sent = {}
        # key -> (value we asked for, when we asked).  While an entry is here
        # the read-back is not allowed to write to that widget.  See
        # _accepts_readback() for why this matters for the Combos.
        self.pending = {}
        # Last value written into each widget, so a widget is only touched when
        # something actually changed rather than four times a second.
        self.shown = {}
        self.cmd_q = queue.Queue()
        self.window = self.build()

    # ==================================================================
    # UI construction
    # ==================================================================
    def build(self):
        sg.theme("DarkGrey13")

        top = [
            sg.Text("VISA resource", text_color=MUTED),
            sg.Input(self.resource, key="-RESOURCE-", size=(46, 1),
                     font=("Courier", 10)),
            sg.Button("Connect", key="-CONNECT-", size=(11, 1)),
            sg.Text("not connected", key="-STATUS-", size=(40, 1),
                    text_color=MUTED),
        ]

        screen = sg.Graph(
            canvas_size=(SCREEN_W, SCREEN_H),
            # y grows downward, exactly as on the Flet canvas, so the pixel
            # mapping in draw() is identical in both versions.
            graph_bottom_left=(0, SCREEN_H),
            graph_top_right=(SCREEN_W, 0),
            background_color=BG,
            key="-SCREEN-",
            pad=(0, 0),
        )

        readout = sg.Text("", key="-READOUT-", size=(96, 2),
                          font=("Courier", 10), text_color=FG)

        slider = dict(orientation="h", size=(26, 14), enable_events=True,
                      trough_color="#30363d")

        controls = [
            [heading("ACQUISITION")],
            [sg.Text("Channel", size=(10, 1), text_color=MUTED),
             sg.Combo(["1", "2"], default_value="1", key="-CH-",
                      size=(6, 1), readonly=True, enable_events=True)],
            [sg.Text("Volts / div", size=(10, 1), text_color=MUTED),
             sg.Combo(list(VDIV_ITEMS), default_value=eng(0.5, "V"),
                      key="-VDIV-", size=(12, 1), readonly=True,
                      enable_events=True)],
            [sg.Text("Coupling", size=(10, 1), text_color=MUTED),
             sg.Combo(COUPLING_CHOICES, default_value="DC", key="-COUP-",
                      size=(12, 1), readonly=True, enable_events=True)],
            [sg.Text("Time / div", size=(10, 1), text_color=MUTED),
             sg.Combo(list(TDIV_ITEMS), default_value=eng(5e-4, "s"),
                      key="-TDIV-", size=(12, 1), readonly=True,
                      enable_events=True)],

            [label("Vertical offset (V)")],
            [sg.Slider(range=(-5, 5), default_value=0, resolution=0.1,
                       key="-OFFSET-", **slider)],
            [label("Horizontal position (div)")],
            [sg.Slider(range=(-5, 5), default_value=0, resolution=0.1,
                       key="-POS-", **slider)],

            [sg.HorizontalSeparator()],
            [heading("TRIGGER  (edge)")],
            [sg.Text("Source", size=(10, 1), text_color=MUTED),
             sg.Combo(SOURCE_CHOICES, default_value="1", key="-TSRC-",
                      size=(12, 1), readonly=True, enable_events=True)],
            [sg.Text("Slope", size=(10, 1), text_color=MUTED),
             sg.Combo(SLOPE_CHOICES, default_value="POS", key="-TSLOPE-",
                      size=(12, 1), readonly=True, enable_events=True)],
            [label("Trigger level (V)")],
            [sg.Slider(range=(-4, 4), default_value=0, resolution=0.1,
                       key="-TRIG-", **slider)],

            [sg.HorizontalSeparator()],
            [sg.Button("Run", key="-RUN-", size=(8, 1), button_color=("white", "#238636")),
             sg.Button("Stop", key="-STOP-", size=(8, 1)),
             sg.Button("Autoscale", key="-AUTO-", size=(10, 1))],
            [sg.Button("Save CSV", key="-SAVE-", size=(28, 1),
                       button_color=("white", "#1f6feb"))],

            [sg.HorizontalSeparator()],
            [heading("DEVICE UNDER TEST  (simulator only)")],
            [sg.Text("Test signal", size=(10, 1), text_color=MUTED),
             sg.Combo(["SIN", "SQU", "TRI", "RAMP", "DC", "NOIS"],
                      default_value="SIN", key="-SHAPE-", size=(10, 1),
                      readonly=True, enable_events=True)],
            [sg.Text("Freq (Hz)", size=(10, 1), text_color=MUTED),
             sg.Input("1000", key="-FREQ-", size=(12, 1))],
            [sg.Text("Ampl (V)", size=(10, 1), text_color=MUTED),
             sg.Input("1.0", key="-AMPL-", size=(12, 1))],
            [sg.Button("Apply signal", key="-SIGNAL-", size=(28, 1))],
        ]

        layout = [
            top,
            [sg.Column([[screen], [readout]], pad=(0, 10)),
             sg.Column(controls, vertical_alignment="top", pad=(16, 10))],
        ]

        window = sg.Window("Virtual Oscilloscope - PyVISA + PySimpleGUI",
                           layout, background_color=PANEL, finalize=True)
        self.draw_graticule(window["-SCREEN-"])
        return window

    def draw_graticule(self, graph):
        """Dotted 10 x 8 division grid with brighter centre axes.

        Drawn once.  The figure ids are kept only so the grid can be found
        again if the screen is ever cleared wholesale.
        """
        self.graticule_ids = []
        for i in range(H_DIV + 1):
            x = i * SCREEN_W / H_DIV
            self.graticule_ids.append(graph.draw_line(
                (x, 0), (x, SCREEN_H),
                color=AXIS if i == H_DIV // 2 else GRID, width=1))
        for i in range(V_DIV + 1):
            y = i * SCREEN_H / V_DIV
            self.graticule_ids.append(graph.draw_line(
                (0, y), (SCREEN_W, y),
                color=AXIS if i == V_DIV // 2 else GRID, width=1))

    # ==================================================================
    # connection
    # ==================================================================
    def on_connect(self, values):
        if self.scope:
            self.polling = False
            time.sleep(POLL_PERIOD + 0.1)
            self.scope.close()
            self.scope = None
            self.window["-CONNECT-"].update("Connect")
            self.set_status("not connected", MUTED)
            return
        try:
            self.scope = Scope(values["-RESOURCE-"].strip())
        except Exception as exc:                                  # noqa: BLE001
            self.set_status(f"failed: {exc}".splitlines()[0], BAD)
            return
        self.set_status(self.scope.idn, GOOD)
        self.window["-CONNECT-"].update("Disconnect")
        self.push_all(values)
        self.polling = True
        threading.Thread(target=self.poll_loop, daemon=True).start()

    def set_status(self, text, colour):
        self.window["-STATUS-"].update(text, text_color=colour)

    def push_all(self, values):
        """Send the current front-panel state to the instrument."""
        s = self.scope
        s.set_volts_per_div(self.channel, VDIV_ITEMS[values["-VDIV-"]])
        s.set_coupling(self.channel, values["-COUP-"])
        s.set_time_per_div(TDIV_ITEMS[values["-TDIV-"]])
        s.set_offset(self.channel, float(values["-OFFSET-"]))
        s.set_time_position(float(values["-POS-"]) * TDIV_ITEMS[values["-TDIV-"]])
        s.set_trigger_source(int(values["-TSRC-"]))
        s.set_trigger_slope(values["-TSLOPE-"])
        s.set_trigger_level(float(values["-TRIG-"]))
        s.run()

    # ==================================================================
    # acquisition thread
    # ==================================================================
    def poll_loop(self):
        """Runs off the main thread.  Touches no widget - it only posts events.

        Front-panel commands are funnelled through cmd_q and executed here too,
        so that exactly one thread ever holds the VISA session.  Sharing a
        pyvisa resource across threads is the classic way to get interleaved
        query responses.
        """
        while self.polling and self.scope:
            try:
                while True:
                    try:
                        self.cmd_q.get_nowait()(self.scope)
                    except queue.Empty:
                        break

                state = self.scope.get_state(self.channel)
                times, volts = self.scope.capture(self.channel, points=1000)
                readings = {
                    "vpp": self.scope.measure("VPP", self.channel),
                    "vrms": self.scope.measure("VRMS", self.channel),
                    "freq": self.scope.measure("FREQuency", self.channel),
                }
                self.window.write_event_value(
                    "-DATA-", (state, times, volts, readings))
            except Exception as exc:                              # noqa: BLE001
                self.window.write_event_value("-ACQ-ERROR-", str(exc))
                return
            time.sleep(POLL_PERIOD)

    def send(self, fn):
        """Queue a one-line SCPI action for the acquisition thread."""
        if self.scope:
            self.cmd_q.put(fn)

    # ==================================================================
    # control callbacks - each one is a single SCPI write
    # ==================================================================
    @staticmethod
    def _same(a, b, tol):
        """Compare two control values, which may be numbers or SCPI words.

        Coupling and slope come back as text ('DC', 'POS'), so the numeric
        tolerance used for the knobs does not apply to them.
        """
        if isinstance(a, str) or isinstance(b, str):
            return a == b
        return abs(a - b) <= tol

    def changed(self, key, value, tol=1e-9):
        """True if this control's value differs from what was last sent.

        A tkinter Slider fires an event for every pixel of drag, so without
        this guard one sweep of the offset knob would emit dozens of identical
        :CHANnel1:OFFSet writes.
        """
        previous = self.sent.get(key)
        if previous is not None and self._same(previous, value, tol):
            return False
        self.sent[key] = value
        self.touched[key] = time.monotonic()
        self.pending[key] = (value, time.monotonic())
        return True

    def handle_control(self, event, values):
        if event == "-CH-":
            self.channel = int(values["-CH-"])
            self.sent.clear()               # per-channel settings differ
            self.pending.clear()
            self.shown.clear()
        elif event == "-VDIV-":
            v = VDIV_ITEMS[values["-VDIV-"]]
            if self.changed("-VDIV-", v):
                self.send(lambda s: s.set_volts_per_div(self.channel, v))
        elif event == "-COUP-":
            c = values["-COUP-"]
            if self.changed("-COUP-", c):
                self.send(lambda s: s.set_coupling(self.channel, c))
        elif event == "-TDIV-":
            t = TDIV_ITEMS[values["-TDIV-"]]
            if self.changed("-TDIV-", t):
                self.send(lambda s: s.set_time_per_div(t))
        elif event == "-TSRC-":
            n = int(values["-TSRC-"])
            if self.changed("-TSRC-", n):
                self.send(lambda s: s.set_trigger_source(n))
        elif event == "-TSLOPE-":
            sl = values["-TSLOPE-"]
            if self.changed("-TSLOPE-", sl):
                self.send(lambda s: s.set_trigger_slope(sl))
        elif event == "-OFFSET-":
            v = float(values["-OFFSET-"])
            if self.changed("-OFFSET-", v, tol=1e-4):
                self.send(lambda s: s.set_offset(self.channel, v))
        elif event == "-POS-":
            d = float(values["-POS-"])
            if self.changed("-POS-", d, tol=1e-4):
                # The slider is in divisions; the instrument wants seconds.
                self.send(lambda s: s.set_time_position(d * s.get_time_per_div()))
        elif event == "-TRIG-":
            v = float(values["-TRIG-"])
            if self.changed("-TRIG-", v, tol=1e-4):
                # Level only.  set_trigger() would also rewrite the source and
                # slope, undoing the two dropdowns above on every drag.
                self.send(lambda s: s.set_trigger_level(v))
        elif event == "-RUN-":
            self.send(lambda s: s.run())
        elif event == "-STOP-":
            self.send(lambda s: s.stop())
        elif event == "-AUTO-":
            self.sent.clear()
            self.touched.clear()            # let the read-back drive the panel
            self.pending.clear()
            self.shown.clear()
            self.send(lambda s: s.autoscale())
        elif event == "-SIGNAL-" or event == "-SHAPE-":
            try:
                shape = values["-SHAPE-"]
                freq = float(values["-FREQ-"])
                ampl = float(values["-AMPL-"])
            except ValueError:
                self.set_status("frequency and amplitude must be numbers", BAD)
                return
            self.send(lambda s: s.set_test_signal(
                self.channel, shape=shape, freq=freq, amplitude=ampl))

    def on_save(self):
        if not self.last_capture:
            self.set_status("nothing captured yet", MUTED)
            return
        times, volts = self.last_capture
        stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        name = f"capture_ch{self.channel}_{stamp}.csv"
        with open(name, "w", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["time_s", "voltage_V"])
            w.writerows(zip(times, volts))
        self.set_status(f"saved {len(times)} points to {name}", GOOD)

    # ==================================================================
    # display refresh - all of this runs on the main thread
    # ==================================================================
    def warm(self, key):
        """Has the user touched this control recently enough that the
        instrument read-back should leave it alone?"""
        return time.monotonic() - self.touched.get(key, 0.0) < SYNC_HOLD

    def _accepts_readback(self, key, actual, tol):
        """May the instrument's value be written into this widget right now?

        Not while a setting we asked for is still in flight.  Without this the
        4 Hz read-back overwrites a Combo selection before the event loop has
        read it: the user picks 200 mV, a -DATA- event arrives first, the
        widget is set back to 500 mV, and by the time the -VDIV- event is
        handled the widget reports 500 mV again - so nothing is ever sent.

        A Slider survives that race because tkinter fires an event for every
        pixel of drag, so one of the many events always gets through.  A Combo
        fires exactly once.  That is why the two dropdowns appeared dead while
        the sliders worked.
        """
        want, asked_at = self.pending.get(key, (None, 0.0))
        if want is not None:
            if self._same(actual, want, tol):
                self.pending.pop(key, None)     # confirmed; widget is correct
                return False
            if time.monotonic() - asked_at < PENDING_HOLD:
                return False                   # still in flight - hands off
            # The instrument refused or snapped the value to a knob position.
            # Stop waiting and let reality win, or the control sticks forever.
            self.pending.pop(key, None)
        return not self.warm(key)

    def _show(self, key, value):
        """Write to a widget only when the value actually changes.

        Re-setting a ttk Combobox four times a second is what opens the race
        above; it is also simply wasted work.
        """
        if self.shown.get(key) != value:
            self.shown[key] = value
            self.window[key].update(value=value)

    def sync_widgets(self, state):
        """Make the front panel reflect the instrument, not the other way.

        Anything else driving this scope - a notebook, a colleague at the front
        panel - shows up here, and the display follows it.  But a control the
        user has just changed is left alone until the instrument confirms it.
        """
        v_div, t_div = state["v_div"], state["t_div"]

        if self._accepts_readback("-VDIV-", v_div, abs(v_div) * 1e-6 + 1e-12):
            text = eng(v_div, "V")
            if text in VDIV_ITEMS:
                self._show("-VDIV-", text)
                self.sent["-VDIV-"] = v_div

        if self._accepts_readback("-TDIV-", t_div, abs(t_div) * 1e-6 + 1e-15):
            text = eng(t_div, "s")
            if text in TDIV_ITEMS:
                self._show("-TDIV-", text)
                self.sent["-TDIV-"] = t_div

        if self._accepts_readback("-OFFSET-", state["offset"], 1e-4):
            v = max(-5.0, min(5.0, state["offset"]))
            self._show("-OFFSET-", v)
            self.sent["-OFFSET-"] = v

        divs = state["position"] / t_div if t_div else 0.0
        if self._accepts_readback("-POS-", divs, 1e-4):
            v = max(-5.0, min(5.0, divs))
            self._show("-POS-", v)
            self.sent["-POS-"] = v

        if self._accepts_readback("-TRIG-", state["trigger"], 1e-4):
            v = max(-4.0, min(4.0, state["trigger"]))
            self._show("-TRIG-", v)
            self.sent["-TRIG-"] = v

        # The three text-valued controls.  Tolerance is irrelevant for these -
        # _same() falls back to string equality.  Each is guarded by its list
        # of choices so an unexpected reply can never put a readonly Combo
        # into a state the user cannot get out of.
        coupling = state["coupling"]
        if coupling in COUPLING_CHOICES and \
                self._accepts_readback("-COUP-", coupling, 0):
            self._show("-COUP-", coupling)
            self.sent["-COUP-"] = coupling

        slope = state["trig_slope"]
        if slope in SLOPE_CHOICES and \
                self._accepts_readback("-TSLOPE-", slope, 0):
            self._show("-TSLOPE-", slope)
            self.sent["-TSLOPE-"] = slope

        source = state["trig_source"]
        if str(source) in SOURCE_CHOICES and \
                self._accepts_readback("-TSRC-", source, 0):
            self._show("-TSRC-", str(source))
            self.sent["-TSRC-"] = source

    def draw(self, times, volts, state):
        """Map (seconds, volts) onto screen pixels using the instrument's
        current scaling - not this program's idea of it."""
        graph = self.window["-SCREEN-"]
        for fid in self.trace_ids:
            graph.delete_figure(fid)
        self.trace_ids = []

        span_t = H_DIV * state["t_div"]
        span_v = V_DIV * state["v_div"]
        offset = state["offset"]
        position = state["position"]           # time at the centre of the screen
        cx, cy = SCREEN_W / 2, SCREEN_H / 2

        step = max(1, len(times) // 360)       # decimate for smooth redraw
        pts = []
        for i in range(0, len(times), step):
            # Subtracting the position keeps the captured window filling the
            # screen; it is the trigger point that moves, not the trace.
            x = cx + (times[i] - position) / span_t * SCREEN_W
            y = cy - (volts[i] - offset) / span_v * SCREEN_H
            pts.append((x, max(-20.0, min(SCREEN_H + 20.0, y))))

        trig_y = cy - (state["trigger"] - offset) / span_v * SCREEN_H
        # Where the trigger event itself (t = 0) now sits on screen.  With no
        # position it is dead centre; winding the position moves it sideways.
        trig_x = cx - position / span_t * SCREEN_W

        # The level cursor belongs to the trigger *source* channel, and is
        # drawn in that channel's volts scale.  When you are looking at CH1
        # but triggering off CH2 there is nothing meaningful to draw, so the
        # cursor disappears - which is also how you can tell at a glance that
        # the trigger is armed on the other channel.
        if state["trig_source"] == self.channel:
            self.trace_ids.append(
                graph.draw_line((0, trig_y), (SCREEN_W, trig_y),
                                color=TRIG, width=1))
        self.trace_ids.append(
            graph.draw_line((trig_x, 0), (trig_x, SCREEN_H), color=TRIG, width=1))
        if pts:
            # One canvas item for the whole trace, not 360 separate lines.
            self.trace_ids.append(
                graph.draw_lines(pts, color=TRACE[self.channel - 1], width=2))

    def update_readout(self, state, r):
        self.window["-READOUT-"].update(
            f"CH{self.channel} {state['coupling']}   "
            f"{eng(state['v_div'], 'V')}/div   "
            f"{eng(state['t_div'], 's')}/div   "
            f"pos {eng(state['position'], 's')}\n"
            f"Trig CH{state['trig_source']} {state['trig_slope']} "
            f"{eng(state['trigger'], 'V')}   "
            f"Vpp {eng(r['vpp'], 'V')}   Vrms {eng(r['vrms'], 'V')}   "
            f"f {eng(r['freq'], 'Hz')}"
        )

    # ==================================================================
    # main event loop
    # ==================================================================
    def run(self):
        if self.autoconnect:
            # The window is finalized, so the widgets exist and read() will
            # deliver a full values dict alongside this injected event.
            self.window.write_event_value("-CONNECT-", None)

        while True:
            event, values = self.window.read(timeout=100)

            if event in (sg.WIN_CLOSED, None):
                break
            if event == sg.TIMEOUT_KEY:
                continue

            if event == "-DATA-":
                state, times, volts, readings = values["-DATA-"]
                self.last_capture = (times, volts)
                self.sync_widgets(state)
                self.draw(times, volts, state)
                self.update_readout(state, readings)
            elif event == "-ACQ-ERROR-":
                self.polling = False
                self.set_status(f"acquisition error: {values['-ACQ-ERROR-']}", BAD)
            elif event == "-CONNECT-":
                self.on_connect(values)
            elif event == "-SAVE-":
                self.on_save()
            else:
                self.handle_control(event, values)

        self.polling = False
        if self.scope:
            time.sleep(POLL_PERIOD + 0.1)
            self.scope.close()
        self.window.close()


# ==========================================================================
# PART 4 - ENTRY POINT
# ==========================================================================

EPILOG = """\
examples:
  python virtual_scope.py
      Start the simulator in a background thread and open the front panel
      against it.  One command, one window - the usual way to run this.

  python virtual_scope.py --sim-only
      Simulator only, no GUI.  Use this to keep the original two-terminal
      workflow, or to drive the instrument from a Jupyter notebook:
          scope = Scope("TCPIP0::127.0.0.1::5025::SOCKET")

  python virtual_scope.py --no-sim
      Front panel only.  Connects to a simulator someone else is running,
      possibly on another PC via --host.

  python virtual_scope.py --resource USB0::0x2A8D::0x0396::CN12345678::INSTR
      Front panel against the real scope on the bench.  Same driver code,
      same SCPI traffic - only this string changes.
"""


def build_parser():
    p = argparse.ArgumentParser(
        prog=PROGRAM,
        description="Virtual oscilloscope: SCPI simulator, PyVISA "
                    "driver and front panel in a single file.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=EPILOG)
    p.add_argument("--host", default="127.0.0.1",
                   help="simulator bind/connect address "
                        "(use 0.0.0.0 to let other PCs in; default 127.0.0.1)")
    p.add_argument("--port", type=int, default=5025,
                   help="simulator TCP port (default 5025)")
    p.add_argument("--resource", default=None,
                   help="full VISA resource string; overrides --host/--port "
                        "and turns the built-in simulator off")
    p.add_argument("--sim", dest="sim", action="store_true", default=None,
                   help="start the built-in simulator (default unless "
                        "--resource is given)")
    p.add_argument("--no-sim", dest="sim", action="store_false",
                   help="do not start the built-in simulator")
    p.add_argument("--sim-only", action="store_true",
                   help="run the simulator without the front panel")
    p.add_argument("-v", "--verbose", action="store_true",
                   help="log every SCPI exchange - useful in training, but "
                        "the front panel polls at 4 Hz, so expect a flood")
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)

    # Driving real hardware means there is nothing to simulate, so the
    # simulator defaults off in that case - but an explicit --sim still wins.
    if args.sim is None:
        args.sim = args.resource is None

    resource = args.resource or f"TCPIP0::{args.host}::{args.port}::SOCKET"

    server = None
    if args.sim or args.sim_only:
        if sim_port_in_use(args.host, args.port):
            print(f"[sim] {args.host}:{args.port} is already in use - "
                  f"not starting a second simulator.")
            if args.sim_only:
                return 1
        else:
            server = start_simulator(args.host, args.port, args.verbose)
            print("=" * 62)
            print(f"  {SIM_IDN}")
            print(f"  listening on {args.host}:{args.port}")
            print(f"  PyVISA resource: TCPIP0::{args.host}::{args.port}::SOCKET")
            print("=" * 62)

    # ---- simulator only: no GUI, just hold the port open ----
    if args.sim_only:
        print("  Ctrl-C to stop")
        try:
            while True:
                time.sleep(0.5)
        except KeyboardInterrupt:
            print()
        finally:
            stop_simulator(server)
        return 0

    # ---- front panel ----
    global sg
    try:
        import PySimpleGUI as sg           # noqa: F401  (bound to the global)
    except ImportError:
        stop_simulator(server)
        print('PySimpleGUI is not installed.  Either install it with\n'
              '    pip install "pysimplegui>=6.0"\n'
              f'or run the simulator alone with\n'
              f'    python {PROGRAM} --sim-only', file=sys.stderr)
        return 1

    # Come up already connected whenever there is a simulator to connect to -
    # whether this process started it or found one already running.  The test
    # is deliberately "is something listening", not "did I start it": a
    # simulator someone left running in another terminal is just as safe to
    # attach to, and the alternative is a front panel that opens dead for no
    # reason the student can see.
    #
    # Anything not started by this program - real hardware, or a simulator
    # somewhere else - is left to the Connect button on purpose, because
    # on_connect() calls push_all(), which writes this panel's settings to the
    # instrument.  Doing that unasked to a scope on the bench would wipe
    # whatever the last person set up on it.
    autoconnect = args.sim and (server is not None
                                or sim_port_in_use(args.host, args.port))
    try:
        ScopeApp(resource, autoconnect=autoconnect).run()
    finally:
        stop_simulator(server)
    return 0


if __name__ == "__main__":
    sys.exit(main())
