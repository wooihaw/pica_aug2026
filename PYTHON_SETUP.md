# Setup Guide for Python on Windows

Follow these steps in order. Each one ends with a **Check** — run it and confirm
the result before moving on. If a Check fails, stop and fix it there; the next
step will not work.

Total time: about 10 minutes, most of it downloading.

You will end up with this:

```
C:\Users\<you>\python_venv\           <- folder for Python projects
    pica-lab\                         <- this lab
        .venv\                        <- the virtual environment
        pyproject.toml                <- what the lab needs
        uv.lock                       <- exact versions
        ... the rest of the lab files
```

**Before you start** you need Windows 10 or 11, an internet connection, and
about 1 GB of free disk space.

---

## Step 1 — Open PowerShell

Press **Win + X**, then choose **Terminal** or **Windows PowerShell**.

> Use **PowerShell**, not Command Prompt, and not PowerShell ISE. The commands
> below assume PowerShell.

### Check

```powershell
$PSVersionTable.PSVersion
```

You should see a version table. If the window says "Command Prompt" at the top,
type `powershell` and press Enter.

---

## Step 2 — Create the `python_venv` folder

```powershell
cd $HOME
mkdir python_venv
cd python_venv
```

> **Why the home folder and not Documents or Desktop?** On most university and
> company laptops those two are redirected into OneDrive. OneDrive will try to
> sync the thousands of small files in a virtual environment, which makes
> installs crawl and can corrupt the environment outright. `C:\Users\<you>` is
> not synced, so we work there.

### Check

```powershell
pwd
```

Expect `C:\Users\<your-name>\python_venv`. If the path contains `OneDrive`,
you are in the wrong place — go back and run `cd $HOME` first.

---

## Step 3 — Install uv

`uv` is a single self-contained program. It does **not** need Python to already
be installed.

```powershell
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

> If your IT policy blocks that script, either of these works instead:
> ```powershell
> winget install --id=astral-sh.uv -e
> ```
> ```powershell
> pip install uv          # only if you already have some Python
> ```

**Now close PowerShell completely and restart a new PowerShell.** The installer adds `uv` to your PATH, and the current PowerShell you are in cannot see that change. This is by far the most common thing to go wrong at this step.

In the new PowerShell, go back to the folder:

```powershell
cd $HOME\python_venv
```

### Check

```powershell
uv --version
```

Expect something like `uv 0.9.x`. If you get *"uv is not recognized"*, see
[Troubleshooting](#troubleshooting).

---

## Step 4 — Install Python 3.13

`uv` downloads and manages its own copy of Python. This does not touch, replace
or interfere with any Python, Anaconda or Microsoft Store Python already on the
machine.

```powershell
uv python install 3.13
```

This downloads roughly 30 MB.

### Check

```powershell
uv python list
```

Look for a line containing **`cpython-3.13`** marked as installed. Other Python
versions may also be listed — that is normal and harmless, because Step 6 pins
which one this project uses.

---

## Step 5 — Create the project folder

```powershell
uv init pica-lab --python 3.13
cd pica-lab
```

This creates the folder and generates the starting files.

Delete the sample script it makes, which we do not need:

```powershell
del main.py
```

> If `del main.py` says the file does not exist, that is fine — some versions of
> `uv` do not create it. Carry on.

### Check

```powershell
dir
```

You should see at least `pyproject.toml` and `.python-version`.

---

## Step 6 — Pin Python 3.13

```powershell
uv python pin 3.13
```

This writes `3.13` into the `.python-version` file. From now on, every command
you run in this folder uses Python 3.13, no matter what else is installed on the
machine or what environment is active.

### Check

```powershell
type .python-version
```

Expect exactly `3.13`.

---

## Step 7 — Set the Python upper limit

This is the only step where you edit a file by hand.

```powershell
notepad pyproject.toml
```

Find this line:

```toml
requires-python = ">=3.13"
```

Change it to:

```toml
requires-python = ">=3.13,<3.14"
```

Save (**Ctrl + S**) and close Notepad.

> **Why.** One of the packages we install is pinned to an older version that was
> released before Python 3.14 existed. Without the upper limit, `uv` is allowed
> to pick 3.14 on a machine that has it, pairing an old library with an
> interpreter it has never been tested against. Setting the limit now, before
> installing anything, means the whole install is resolved against 3.13.

### Check

```powershell
type pyproject.toml
```

Confirm the line now reads `requires-python = ">=3.13,<3.14"`.

---

## Step 8 — Create the virtual environment

```powershell
uv venv
```

This creates a `.venv` folder containing an isolated Python 3.13. Nothing you
install later leaks out to the rest of your system.

You will see a message suggesting you activate it. **You do not need to.**
Every command from here uses `uv run`, which handles that for you — and on many
Windows machines the activation script is blocked by the execution policy
anyway.

### Check

```powershell
uv run python --version
```

Expect `Python 3.13.x`.

---

## Step 9 — Install the dependencies

Run these five commands one at a time, waiting for each to finish.

```powershell
uv add "pyvisa>=1.14" "pyvisa-py>=0.7.2"
```

```powershell
uv add "pysimplegui>=6"
```

```powershell
uv add "pyserial>=3.5"
```

```powershell
uv add "numpy>=2.0" "pandas>=2.2" "matplotlib>=3.9"
```

```powershell
uv add --dev "jupyterlab>=4.2" "ipykernel>=6.29"
```

> **The quotation marks are not optional.** In PowerShell, `>` means "redirect
> output to a file". Without the quotes, `uv add pyvisa>=1.14` creates a junk
> file called `=1.14` and installs the wrong thing, with no error message.

The third and fourth commands download the most (roughly 400 MB together), so
they take the longest.

### Check

```powershell
uv run python -c "import pyvisa, numpy, pandas, matplotlib, flet; print('all imports OK')"
```

Expect `all imports OK`.

---

## Step 10 — Verify Jupyter Lab

```powershell
uv run jupyter lab
```

Your browser should open at `http://localhost:8888/lab` within a few seconds.

> If no browser opens, look in the PowerShell window for a line starting with
> `http://localhost:8888/lab?token=...` and paste that whole address, token
> included, into your browser.

In Jupyter Lab:

1. Click **Notebook → Python 3 (ipykernel)** to create a new notebook.
2. Type this into the first cell and press **Shift + Enter**:

```python
import sys, pyvisa, numpy, pandas, matplotlib
print(sys.executable)
print("Jupyter is using the project environment")
```

### Check

The printed path must contain **`pica-lab\.venv`**. If it points
somewhere else, Jupyter is running from a different Python — see
[Troubleshooting](#troubleshooting).

**To shut down:** close the browser tab, then return to PowerShell and press
**Ctrl + C**. Confirm with `y` if asked.

---

## You are done

Your environment is ready. Nothing needs to stay running.

To come back to it later, open PowerShell and run:

```powershell
cd $HOME\python_venv\pica-lab
```

Everything from then on starts with `uv run`, for example
`uv run jupyter lab`. There is no environment to activate.

---

## Troubleshooting

| Problem | What to do |
|---|---|
| `uv is not recognized` | You did not open a **new** PowerShell window after installing. Close it completely and open a fresh one. |
| Still not recognized after restarting | The installer printed where it put `uv`, usually `C:\Users\<you>\.local\bin`. Add that folder to PATH: **Win + R** → `sysdm.cpl` → Advanced → Environment Variables → edit **Path** under *User variables*. Then open a new PowerShell. |
| `running scripts is disabled on this system` | Use the full install command in Step 3, including `-ExecutionPolicy ByPass`. Do not change your machine's execution policy. |
| A file called `=1.14` appeared | You omitted the quotation marks in Step 9. Delete it (`del "=1.14"`) and re-run that command **with** the quotes. |
| Installs are extremely slow | Check `pwd` — if the path contains `OneDrive`, you are in a synced folder. Start again from Step 2. Antivirus scanning can also slow things; this is normal on the first install only. |
| `No solution found when resolving` | The `requires-python` edit in Step 7 was mistyped. Open `pyproject.toml` and confirm it reads exactly `requires-python = ">=3.13,<3.14"`. |
| Jupyter's `sys.executable` points outside `.venv` | You launched Jupyter from somewhere else. Close it, `cd` to the project folder, and use `uv run jupyter lab`. |
| Anaconda seems to interfere | It should not — `uv run` ignores any active conda environment. If in doubt, open a plain PowerShell rather than an Anaconda Prompt. |
| Path-too-long errors | Your folder is nested too deep. The lab must be at `C:\Users\<you>\python_venv\pica-lab`, not inside further sub-folders. |
| Everything is broken and you want to restart | `cd $HOME\python_venv`, then `rmdir pica-lab -r -fo`, then start again from Step 5. |

---

## Quick reference

| Command | What it does |
|---|---|
| `cd $HOME\python_venv\pica-lab` | Go to the lab folder |
| `uv run python check_setup.py` | Re-check the environment |
| `uv run jupyter lab` | Start Jupyter Lab |
| `uv run python <file>.py` | Run any script in the environment |
| `uv sync` | Reinstall everything from `uv.lock` |
| `uv pip list` | List installed packages |
| `explorer .` | Open the current folder in File Explorer |
| **Ctrl + C** | Stop whatever is running in PowerShell |
