"""
gui_oscilloscope_working.py - a simple PySimpleGUI front end for an oscilloscope.

Updated from gui_oscilloscope(working).py for PySimpleGUI 6 and for the
scope_sim.py virtual instrument.  Deliberately kept in the same five-section
shape as the original - import, layout, window, event loop, close - so the two
files can be compared line by line.

    uv add "pysimplegui>=6.0" pyvisa pyvisa-py

Run the simulator in one terminal:   uv run virtual_scope.py
Run this in another:                 uv run gui_oscilloscope_working.py

What changed, and why
---------------------
1. window.perform_long_operation() -> window.start_thread().  The old name
   still works in v6, but only as an alias, so every current example and
   search result uses start_thread.

2. Termination characters are set for SOCKET resources.  A USB INSTR resource
   frames messages for you; a raw socket does not.  Writes happened to work
   without this because PyVISA defaults write_termination to '\\r\\n', but
   read_termination defaults to None, so the first query() would hang until it
   timed out.  Now that we query the error queue, this matters.

3. ':CHANnel1:OFFSet 1.5V' became ':CHANnel1:OFFSet 1.5'.  Real
   hardware accepts the unit suffix; scope_sim.py does float() on the argument
   and rejects it.  The failure was silent - the offset simply never changed.

4. Channels are (1, 2).  scope_sim.py is a 2-channel instrument and answers
   -222,"Data out of range: channel" for CH3 and CH4 - again, silently.

5. rm.list_resources() cannot discover a raw TCP socket, so the simulator will
   never appear in the dropdown.  Its resource string is offered as the default
   and the Combo is left editable so any address can be typed.

6. Every command is followed by ':SYSTem:ERRor?'.  This is the real fix: the
   old version reported "Command sent successfully" whether or not the
   instrument had accepted anything.
"""

import PySimpleGUI as sg
import pyvisa

# --------------------------------------------------------------------------
# instrument helpers - no PySimpleGUI calls in here, so they are safe to run
# on the worker thread started by window.start_thread()
# --------------------------------------------------------------------------
SIM_RESOURCE = "TCPIP0::127.0.0.1::5025::SOCKET"
NO_ERROR = "+0"                      # what an empty error queue returns


def open_instrument(rm, resource, timeout=5000):
    """Open a resource and prove it is really there with *IDN?."""
    instr = rm.open_resource(resource, open_timeout=timeout)
    instr.timeout = timeout

    # A raw socket carries no message framing of its own, so PyVISA has to be
    # told where a message ends.  Harmless on an INSTR resource.
    if resource.strip().upper().endswith("SOCKET"):
        instr.read_termination = "\n"
        instr.write_termination = "\n"

    idn = instr.query("*IDN?").strip()
    instr.write("*CLS")              # start from an empty error queue
    return instr, idn


def send_command(instr, command):
    """Write one command and return the instrument's complaint, or None.

    Polling :SYSTem:ERRor? after every write is the habit worth teaching.  A
    scope accepts a bad command without protest; the only way to find out is
    to ask.
    """
    instr.write(command)
    reply = instr.query(":SYSTem:ERRor?").strip()
    return None if reply.startswith(NO_ERROR) else reply


def send_settings(instr, channel, vscale, voffset, tscale, tpos):
    """Apply the whole front panel.  Returns a list of (command, error).

    An empty list means the instrument accepted everything.  This value is
    handed back through window.start_thread()'s end key, so the popup can tell
    the truth instead of assuming success.
    """
    commands = [
        f":CHANnel{channel}:SCALe {vscale:G}",
        f":CHANnel{channel}:OFFSet {voffset:G}",      # no 'V' suffix
        f":TIMebase:SCALe {tscale:G}",
        f":TIMebase:POSition {tpos:G}",
    ]
    failures = []
    for command in commands:
        try:
            error = send_command(instr, command)
        except Exception as exc:                              # noqa: BLE001
            failures.append((command, f"{type(exc).__name__}: {exc}"))
        else:
            if error:
                failures.append((command, error))
    return failures


# --------------------------------------------------------------------------
# 1 - set-up
# --------------------------------------------------------------------------
rm = pyvisa.ResourceManager()

# list_resources() finds USB/GPIB instruments but cannot discover a raw TCP
# socket, so the simulator is added by hand and the Combo stays editable.
resources = [SIM_RESOURCE] + [r for r in rm.list_resources() if r != SIM_RESOURCE]

channels = (1, 2)                    # scope_sim.py is a 2-channel instrument
vscales = ('100mV', '200mV', '500mV', '1V', '2V', '5V')
vscale_values = (0.1, 0.2, 0.5, 1.0, 2.0, 5.0)
tscales = ('50us', '100us', '200us', '500us', '1ms', '2ms', '5ms')
tscale_values = (50e-6, 100e-6, 200e-6, 500e-6, 1e-3, 2e-3, 5e-3)

text_size = (16, 1)
button_size = (10, 1)

# --------------------------------------------------------------------------
# 2 - layout
# --------------------------------------------------------------------------
layout = [
    [sg.Text('Device', size=text_size),
     sg.Combo(resources, default_value=SIM_RESOURCE, key='-device-', expand_x=True)],
    [sg.Text('Channel', size=text_size),
     sg.Combo(channels, default_value=channels[0], key='-channel-',
              readonly=True, expand_x=True)],
    [sg.Text('Vertical scale', size=text_size),
     sg.Combo(vscales, default_value=vscales[3], key='-vscale-',
              readonly=True, expand_x=True)],
    [sg.Text('Vertical offset (V)', size=text_size),
     sg.Slider(range=(-5.0, 5.0), default_value=0.0, resolution=0.1,
               orientation='h', key='-voffset-', expand_x=True)],
    [sg.Text('Timebase scale', size=text_size),
     sg.Combo(tscales, default_value=tscales[3], key='-tscale-',
              readonly=True, expand_x=True)],
    [sg.Text('Timebase position (div)', size=text_size),
     sg.Slider(range=(-5.0, 5.0), default_value=0.0, resolution=0.1,
               orientation='h', key='-tpos-', expand_x=True)],
    [sg.Text('Not connected', key='-status-', size=(52, 1),
             text_color='grey')],
    [sg.Button('Connect', size=button_size),
     sg.Button('Send', size=button_size, disabled=True),
     sg.Button('Close', size=button_size)],
]

# --------------------------------------------------------------------------
# 3 - window
# --------------------------------------------------------------------------
window = sg.Window('Oscilloscope', layout, element_justification='center')

mso = None

# --------------------------------------------------------------------------
# 4 - event loop
# --------------------------------------------------------------------------
while True:
    event, values = window.read()

    if event in (sg.WIN_CLOSED, 'Close'):
        break

    elif event == 'Connect':
        if mso is not None:                       # already connected - drop it
            mso.close()
            mso = None
            window['Send'].update(disabled=True)
            window['Connect'].update('Connect')
            window['-status-'].update('Not connected', text_color='grey')
            continue

        resource = values['-device-'].strip()
        if not resource:
            sg.popup_error('Please enter or select a VISA resource string.')
            continue
        try:
            mso, idn = open_instrument(rm, resource)
        except pyvisa.errors.VisaIOError as exc:
            mso = None
            sg.popup_error(
                'Could not talk to the instrument.',
                f'Resource: {resource}',
                str(exc),
                'If this is the simulator, check that scope_sim.py is running '
                'and that the port matches.')
        except Exception as exc:                              # noqa: BLE001
            mso = None
            sg.popup_error(f'{type(exc).__name__}: {exc}')
        else:
            window['Send'].update(disabled=False)
            window['Connect'].update('Disconnect')
            window['-status-'].update(idn, text_color='yellow')

    elif event == 'Send':
        channel = values['-channel-']
        vscale = vscale_values[vscales.index(values['-vscale-'])]
        voffset = values['-voffset-']
        tscale = tscale_values[tscales.index(values['-tscale-'])]
        tpos = tscale * values['-tpos-']          # slider is in divisions

        # Disabled so a second click cannot use the same VISA session from a
        # second thread while this one is still mid-query.
        window['Send'].update(disabled=True)
        window['-status-'].update('Sending...', text_color='grey')
        window.start_thread(
            lambda: send_settings(mso, channel, vscale, voffset, tscale, tpos),
            '-command_sent-')

    elif event == '-command_sent-':
        failures = values[event]
        window['Send'].update(disabled=False)
        if failures:
            window['-status-'].update(
                f'{len(failures)} command(s) rejected', text_color='red')
            sg.popup_error(
                'The instrument rejected some commands:',
                *[f'{command}\n    {error}' for command, error in failures],
                title='Command error')
        else:
            window['-status-'].update('All commands accepted',
                                      text_color='yellow')
            sg.popup('Command sent successfully',
                     'The error queue is empty, so the instrument accepted '
                     'every command.')

# --------------------------------------------------------------------------
# 5 - close
# --------------------------------------------------------------------------
if mso is not None:
    mso.close()
rm.close()
window.close()
