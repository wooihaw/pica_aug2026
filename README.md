# Python for Instrument Control and Automation

A 2-day hands-on training on using Python to compute, visualise and analyse measurement
data, build simple instrument front panels, and drive bench instruments over serial and
VISA.

No physical instrument is required. The repository ships with two **virtual instruments**
— a bench digital multimeter and a digital storage oscilloscope — that speak real SCPI
over a real TCP socket. The code you write against them is the same code you would run
against hardware; only the resource string changes.

## Learning outcomes

By the end of the training, participants will be able to:

1. Demonstrate an understanding of basic Python programming.
2. Use key Python libraries for numerical computation, data visualisation and data analysis.
3. Create basic graphical user interfaces with PySimpleGUI.
4. Connect to and control instruments using PySerial and PyVISA.

## Schedule

**Day 1 — Python and the scientific stack**

| Session | Material |
|---|---|
| Python fundamentals | `Python Primer.ipynb` |
| Numerical computing | `NumPy.ipynb` |
| Plotting | `Matplotlib.ipynb` |
| Data analysis | `Pandas.ipynb` |
| Practice | `Exercises.ipynb` |

**Day 2 — GUIs and instrument control**

| Session | Material |
|---|---|
| GUI basics with PySimpleGUI | `gui_hello_world.py`, `gui_long_operation.py` |
| Building an instrument front panel | `gui_oscilloscope.py` → `gui_oscilloscope_complete.py` |
| Serial instrument control | `PySerial_DMM.ipynb` + `virtual_dmm.py` |
| VISA instrument control | `PyVISA_Scope.ipynb` + `virtual_scope.py` |
| Putting it together | `gui_oscilloscope_working.py` |

## Before you arrive

Follow **[PYTHON_SETUP.md](PYTHON_SETUP.md)** — a step-by-step Windows guide that installs
`uv`, creates the project environment and verifies Jupyter Lab. It takes about 10 minutes,
mostly downloading. Every step ends with a **Check**; do not move on until it passes.

Packages used: `numpy`, `pandas`, `matplotlib`, `pysimplegui>=6`, `pyserial`, `pyvisa`,
`pyvisa-py`, plus `jupyterlab` and `ipykernel`.

Start Jupyter Lab with:

```powershell
uv run jupyter lab
```

## Notebooks

| File | What it covers |
|---|---|
| `Python Primer.ipynb` | Variables, conditionals, loops, strings, lists, slicing, list comprehensions, tuples, dictionaries, file I/O, exception handling and functions. |
| `NumPy.ipynb` | Array creation, indexing and slicing, multi-dimensional arrays, random numbers, element-wise operations, aggregation, sorting and linear algebra. |
| `Matplotlib.ipynb` | Line plots, styling and annotation, scatter and bubble charts, histograms, bar and pie charts, subplots, 3D surfaces and displaying images. |
| `Pandas.ipynb` | Series and DataFrames, time-based indexing with `timedelta`, resampling, handling missing data, reading CSV files, `groupby`, descriptive statistics, and plotting directly from a DataFrame. |
| `Exercises.ipynb` | Seven graded exercises: a chickens-and-rabbits puzzle solved by brute force and again by linear algebra, temperature statistics on the Heathrow dataset with plain Python and with Pandas, average and RMS of a sinusoidal waveform, current and power from logged data, and plotting a waveform downloaded from an oscilloscope. |
| `PySerial_DMM.ipynb` | Opening a port, line termination and timeouts, the distinction between SCPI commands and queries, `*IDN?`, configuring a DC-volts measurement, logging 100 readings for mean and standard deviation, and draining the error queue. Talks to `virtual_dmm.py`. |
| `PyVISA_Scope.ipynb` | Resource manager and resource strings, checking the error queue, vertical and timebase settings (and why you always read a setting back), automatic measurements, downloading a waveform as an IEEE 488.2 block and rescaling it with the preamble, and reading back settings changed on the front panel. Talks to `virtual_scope.py`. |

## Virtual instruments

Run these from a terminal — not from inside a notebook — and leave them running while you
work through the corresponding notebook.

| File | Description |
|---|---|
| `virtual_dmm.py` | A virtual bench multimeter in one file: signal source, instrument model, SCPI parser, TCP server, and a PySimpleGUI front panel with a 7-segment display, function keys and a traffic log. PySerial reaches it through `socket://127.0.0.1:5025`, which behaves exactly like a real serial port. Options: `--headless`, `--port`, `--host`, `-v`, `--strict`. |
| `virtual_scope.py` | A virtual 2-channel digital storage oscilloscope speaking SCPI over TCP, with a PyVISA driver and a PySimpleGUI display layered on top — the layers communicate over a real socket, not by calling each other's functions. Address it as `TCPIP0::127.0.0.1::5025::SOCKET`. Options: `--sim-only`, `--no-sim`, `--resource`, `-v`. Also implements a non-standard `:SIMulate:` subsystem for changing the signal on the probes. |

```powershell
uv run python virtual_dmm.py      # Day 2, PySerial session
uv run python virtual_scope.py    # Day 2, PyVISA session
```

> Both simulators listen on port **5025**. Run only one at a time, or start the second one
> with `--port 5030` and update the address in your notebook.

## GUI scripts

| File | Description |
|---|---|
| `gui_hello_world.py` | The smallest useful PySimpleGUI program, laid out in the five sections every GUI script follows: import, layout, window, event loop, close. |
| `gui_long_operation.py` | Why a long-running task freezes a GUI, and how `window.start_thread()` (v5: `perform_long_operation`) keeps the window responsive. |
| `gui_oscilloscope.py` | Skeleton of an oscilloscope front panel with the timebase controls and event handlers left as `...` for participants to complete. |
| `gui_oscilloscope_complete.py` | The finished version of the above. The Send button reports the settings in a popup — no instrument is contacted yet. |
| `gui_oscilloscope_working.py` | The same panel wired to a real instrument through PyVISA, updated for PySimpleGUI 6. Sets termination characters for SOCKET resources, checks `:SYSTem:ERRor?` after every command, and documents each change from the offline version. Use it with `virtual_scope.py`. |
| `gui_oscilloscope.png` | Screenshot of the finished front panel, used as the target for the GUI exercise and as the sample image in `Matplotlib.ipynb`. |

## Datasets

All files live in `data/`.

| File | Description |
|---|---|
| `Heathrow.txt` | Monthly mean high temperatures at Heathrow Airport, January 1948 to December 2016 — one value per line, 828 rows. Used in Exercises 3 and 4. |
| `iris_data.csv` | The classic iris dataset: 150 flowers, four measurements and a species label. Used throughout `Pandas.ipynb`. |
| `current_data.csv` | Time and current, 41 samples. Used in Exercise 6 to compute and plot power. |
| `noisy_signal.csv` | Time and a noisy signal, 201 samples. For filtering and smoothing practice. |
| `scope_0.csv` | A two-channel capture exported by a real oscilloscope, with the header rows the instrument writes. 2000 samples. |
| `waveform_data.csv` | Time and voltage for a waveform downloaded over PyVISA, ~10,000 samples. Used in Exercise 7. |

## Working through the material

- Run every cell yourself rather than reading the output. The notebooks are written to be
  edited — change a value, re-run, see what breaks.
- On Day 2, keep the simulator's traffic log visible next to your code. Watching the bytes
  go out and come back is most of what makes instrument control click.
- When something silently does nothing, check the error queue (`SYST:ERR?`). Instruments
  rarely complain out loud; they just drop the command.
