#!/usr/bin/env python3
"""
check_setup.py - verify your environment before Day 1.

Run this after following PYTHON_SETUP.md:

    uv run python check_setup.py

It checks the Python version, the packages the training needs, and a few things
that quietly cause trouble later (a missing Tk, a OneDrive-synced folder, port
5025 already taken). Nothing is installed or changed - it only reports.

Every line is one check:

    [ OK ]   fine, move on
    [WARN]   works, but will bite you later - read the note
    [FAIL]   must be fixed before the training

If anything fails, the fix is almost always in PYTHON_SETUP.md under the step
of the same name.
"""

import importlib
import os
import platform
import socket
import sys
from importlib import metadata

# ---------------------------------------------------------------- reporting --

PASS, WARN, FAIL = "OK", "WARN", "FAIL"
_results = []


def report(status, label, detail=""):
    """Print one check and remember it for the summary."""
    _results.append(status)
    print(f"  [{status:^4}]  {label:<34}  {detail}")


def heading(text):
    print(f"\n{text}\n" + "-" * 72)


# ------------------------------------------------------------ version tools --

def as_tuple(version):
    """'2.1.0rc1' -> (2, 1, 0).  Good enough for a minimum-version test."""
    parts = []
    for chunk in version.split(".")[:3]:
        digits = ""
        for ch in chunk:
            if not ch.isdigit():
                break
            digits += ch
        parts.append(int(digits) if digits else 0)
    return tuple(parts)


# --------------------------------------------------------------- the checks --

# (distribution name on PyPI, module name to import, minimum version)
PACKAGES = [
    ("numpy",       "numpy",        (2, 0)),
    ("pandas",      "pandas",       (2, 2)),
    ("matplotlib",  "matplotlib",   (3, 9)),
    ("pyserial",    "serial",       (3, 5)),
    ("pyvisa",      "pyvisa",       (1, 14)),
    ("pyvisa-py",   "pyvisa_py",    (0, 7)),
    ("PySimpleGUI", "PySimpleGUI",  (6, 0)),   # v6 is LGPL - no licence key needed
    ("jupyterlab",  "jupyterlab",   (4, 2)),
    ("ipykernel",   "ipykernel",    (6, 29)),
]

MIN_PYTHON = (3, 10)
RECOMMENDED_PYTHON = (3, 13)


def check_python():
    heading("Python")

    v = sys.version_info
    running = f"{v.major}.{v.minor}.{v.micro}"
    if v[:2] < MIN_PYTHON:
        report(FAIL, "Python version", f"{running} - need {'.'.join(map(str, MIN_PYTHON))} or newer")
    elif v[:2] != RECOMMENDED_PYTHON:
        report(WARN, "Python version", f"{running} - the guide pins "
                                       f"{'.'.join(map(str, RECOMMENDED_PYTHON))}, yours may still work")
    else:
        report(PASS, "Python version", running)

    report(PASS, "Platform", f"{platform.system()} {platform.release()} ({platform.machine()})")

    exe = sys.executable
    in_venv = sys.prefix != sys.base_prefix
    if in_venv:
        report(PASS, "Virtual environment", exe)
    else:
        report(WARN, "Virtual environment", "not active - start commands with 'uv run'")

    if "onedrive" in os.getcwd().lower():
        report(WARN, "Project location", "inside OneDrive - syncing corrupts environments, "
                                         "move to C:\\Users\\<you>\\python_venv")
    else:
        report(PASS, "Project location", os.getcwd())


def check_packages():
    heading("Required packages")

    for dist, module, minimum in PACKAGES:
        try:
            installed = metadata.version(dist)
        except metadata.PackageNotFoundError:
            report(FAIL, dist, f"not installed - uv add \"{dist}>={'.'.join(map(str, minimum))}\"")
            continue

        if as_tuple(installed) < minimum:
            report(FAIL, dist, f"{installed} - need {'.'.join(map(str, minimum))} or newer")
            continue

        try:
            importlib.import_module(module)
        except Exception as exc:                 # installed but broken
            report(FAIL, dist, f"{installed} installed, but import failed: {exc}")
        else:
            report(PASS, dist, installed)


def check_gui():
    heading("Graphical interface")

    try:
        import tkinter
    except Exception as exc:
        report(FAIL, "tkinter", f"not available ({exc}) - PySimpleGUI cannot run")
        return

    report(PASS, "tkinter", f"Tk {tkinter.TkVersion}")

    if not (os.environ.get("DISPLAY") or os.name == "nt" or sys.platform == "darwin"):
        report(WARN, "Display", "no DISPLAY set - GUI scripts need a desktop session")
    else:
        report(PASS, "Display", "available")


def check_workspace():
    heading("Instrument simulators")

    # Both simulators listen here; if something else has it, they will not start.
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        probe.bind(("127.0.0.1", 5025))
    except OSError:
        report(WARN, "Port 5025", "already in use - a simulator may be running, "
                                  "or use --port 5030")
    else:
        report(PASS, "Port 5025", "free")
    finally:
        probe.close()

    here = os.path.dirname(os.path.abspath(__file__))
    for name in ("virtual_dmm.py", "virtual_scope.py", "data"):
        if os.path.exists(os.path.join(here, name)):
            report(PASS, f"Found {name}", "")
        else:
            report(FAIL, f"Found {name}", "missing - run this script from the training folder")


def smoke_test():
    heading("Smoke test")

    try:
        import matplotlib
        matplotlib.use("Agg")                    # no window, just exercise the stack
        import matplotlib.pyplot as plt
        import numpy as np
        import pandas as pd

        t = np.linspace(0, 0.04, 200)
        v = 2 * np.sin(2 * np.pi * 50 * t) + 5
        df = pd.DataFrame({"time": t, "volts": v})
        rms = float(np.sqrt(np.mean(df.volts ** 2)))

        fig, ax = plt.subplots()
        ax.plot(df.time, df.volts)
        plt.close(fig)
    except Exception as exc:
        report(FAIL, "numpy + pandas + matplotlib", f"{type(exc).__name__}: {exc}")
    else:
        report(PASS, "numpy + pandas + matplotlib", f"50 Hz waveform, RMS = {rms:.4f} V")


def main():
    print(__doc__.strip().splitlines()[0])
    print("=" * 72)

    check_python()
    check_packages()
    check_gui()
    check_workspace()
    smoke_test()

    failed = _results.count(FAIL)
    warned = _results.count(WARN)

    heading("Summary")
    print(f"  {_results.count(PASS)} passed, {warned} warning(s), {failed} failure(s)\n")

    if failed:
        print("  Not ready yet. Fix the [FAIL] lines above, then run this script again.")
        print("  The matching step is in PYTHON_SETUP.md.")
        return 1

    if warned:
        print("  Ready, with notes. Read the [WARN] lines - none of them stop you starting.")
        return 0

    print("  Everything checks out. See you on Day 1.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
