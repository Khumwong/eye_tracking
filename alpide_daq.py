"""ALPIDE / EUDAQ2 acquisition control.

Thin wrapper over Kcmh-Tricker's own modules rather than a copy of them: the
config-generation logic there carries real content (the six DAQ serial numbers,
the per-plane VCASN/VCASN2 tables, the EUDAQ_FW_PATTERN) that would silently
rot if duplicated. Only the glue that needs different behaviour here is
reimplemented — see clean_env() below.

Everything in this module is best-effort by design. ALPIDE recording is an
addition to eye_tracking's job, never a precondition for it, so a failure here
must never block READY/START/STOP or the beam gating they drive.
"""
import os
import re
import shutil
import subprocess
import sys
import time

import config

# Kcmh-Tricker owns the EUDAQ2 config generation and the ALPIDE firmware images.
# Reaching into that checkout coupling eye_tracking to its path is deliberate:
# the alternative is two copies of hardware constants drifting apart.
_KCMH = config.KCMH_TRICKER_DIR
_FW_DIR = os.path.join(_KCMH, 'alpide')
EUDAQ_DIR = config.EUDAQ_DIR
TMUX_SESSION = 'ITS3'


def _eudaq_module():
    """Import Kcmh-Tricker's eudaq module (pure file/subprocess helpers — it
    pulls in no PyQt at module level, only inside install_firmware_auto)."""
    if _KCMH not in sys.path:
        sys.path.insert(0, _KCMH)
    import modules.eudaq as eudaq
    return eudaq


# ── environment ──────────────────────────────────────────────────────────────

def clean_env():
    """Environment for EUDAQ2 children, with this app's venv stripped out.

    EUDAQ2's scripts use `#!/usr/bin/env python3` and need urwid plus the
    editable alpidedaqboard install from the user site-packages. Inheriting
    eye_tracking's venv points them at an interpreter that has neither, and the
    failure is a bare ModuleNotFoundError inside a tmux pane — invisible unless
    you go looking. Worse, tmux panes inherit from whichever environment first
    started the tmux server, so one poisoned launch keeps failing until the
    server is killed.
    """
    env = dict(os.environ)
    venv = env.pop('VIRTUAL_ENV', None)
    env.pop('PYTHONHOME', None)
    env.pop('PYTHONPATH', None)      # the launch script sets its own
    if venv:
        bad = os.path.join(venv, 'bin')
        env['PATH'] = os.pathsep.join(
            p for p in env.get('PATH', '').split(os.pathsep) if p and p != bad)
    return env


# ── board state ──────────────────────────────────────────────────────────────
# Counted with lsusb (which reads sysfs) rather than pyusb: identifying an
# unprogrammed board by serial needs a USB control transfer, and issuing those
# while EUDAQ2 has the boards open risks disturbing a live run. Counts are all
# the UI needs anyway.

def _lsusb_count(vid_pid):
    try:
        out = subprocess.run(['lsusb', '-d', vid_pid], capture_output=True,
                             text=True, timeout=5).stdout
    except Exception:
        return 0
    return len([l for l in out.splitlines() if l.strip()])


def count_programmed():
    return _lsusb_count('1556:01b8')


def count_dfu():
    return _lsusb_count('04b4:00f3')


def status():
    """('ready'|'unprogrammed'|'partial'|'missing', message) for the UI."""
    prog, dfu = count_programmed(), count_dfu()
    want = config.ALPIDE_NUM
    if prog == want:
        return 'ready', f'{prog}/{want} programmed'
    if prog == 0 and dfu == 0:
        return 'missing', 'no boards found'
    if prog == 0:
        return 'unprogrammed', f'{dfu}/{want} need firmware'
    return 'partial', f'{prog} programmed, {dfu} in DFU'


# ── firmware ─────────────────────────────────────────────────────────────────
# The FX3 image lives in RAM, so boards fall back to DFU mode whenever they lose
# power or a run is killed abruptly. Re-flashing is a normal per-run step here,
# not a recovery action.

def firmware_files():
    fx3 = os.path.join(_FW_DIR, 'fx3.img')
    fpga = os.path.join(_FW_DIR, 'fpga-v1.0.0.bit')
    missing = [f for f in (fx3, fpga) if not os.path.isfile(f)]
    return fx3, fpga, missing


def _programmer():
    return (shutil.which('alpide-daq-program')
            or os.path.expanduser('~/.local/bin/alpide-daq-program'))


def install_firmware(on_line=None, on_done=None):
    """Flash all boards, streaming output to on_line(str). Blocking — call from
    a worker thread and marshal the callbacks back to the UI."""
    fx3, fpga, missing = firmware_files()
    if missing:
        if on_line:
            on_line('firmware image missing: ' + ', '.join(missing))
        if on_done:
            on_done(False)
        return False
    try:
        proc = subprocess.Popen(
            [_programmer(), f'--fx3={fx3}', f'--fpga={fpga}', '--all'],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            env=clean_env())
    except Exception as e:
        if on_line:
            on_line(f'cannot run alpide-daq-program: {e}')
        if on_done:
            on_done(False)
        return False
    for line in proc.stdout:
        line = line.rstrip()
        if on_line and line:
            on_line(line.split('\r')[-1])
    proc.wait()
    ok = proc.returncode == 0
    if on_done:
        on_done(ok)
    return ok


# ── acquisition ──────────────────────────────────────────────────────────────

def session_active():
    try:
        return subprocess.run(['tmux', 'has-session', '-t', TMUX_SESSION],
                              capture_output=True, timeout=5).returncode == 0
    except Exception:
        return False


def start_run(outpath, num_alpides=None, num_events=None, strobe=None, ithr=None):
    """Generate the EUDAQ2 config and launch the run. Returns the pid.

    Reuses Kcmh-Tricker's config writers, but does the launch here so the child
    gets clean_env(). RunControl configures and starts the run by itself and
    stops once NEVENTS is reached, so there is nothing further to drive.
    """
    eudaq = _eudaq_module()
    n = int(num_alpides if num_alpides is not None else config.ALPIDE_NUM)
    nev = int(num_events if num_events is not None else config.ALPIDE_EVENTS)
    stb = int(strobe if strobe is not None else config.ALPIDE_STROBE)
    thr = int(ithr if ithr is not None else config.ALPIDE_ITHR)

    os.makedirs(outpath, exist_ok=True)
    eudaq.gen_its3_ini(n)
    eudaq.gen_its3_conf(n, nev, stb, thr, outpath)

    # substitute the plane count into the launch script (the one part of
    # eudaq.default_run that is pure glue rather than hardware knowledge)
    src = os.path.join(EUDAQ_DIR, 'ITS3start_auto.sh')
    gen = os.path.join(EUDAQ_DIR, 'ITS3start_auto_gen.sh')
    with open(src) as f:
        body = f.read().replace('{num_alpides}', str(n))
    with open(gen, 'w') as f:
        f.write(body)
    try:
        open(os.path.join(EUDAQ_DIR, 'rc.log'), 'w').close()
    except Exception:
        pass

    proc = subprocess.Popen(
        ['bash', '-c', f'cd {EUDAQ_DIR} && bash ./ITS3start_auto_gen.sh'],
        env=clean_env(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return proc.pid


def stop_run(pid):
    """Terminate the run and tear the tmux session down."""
    try:
        _eudaq_module().stop(pid)
        return True
    except Exception:
        # eudaq.stop already swallows most failures; make sure the session is
        # gone regardless, or the next run refuses to start.
        try:
            subprocess.run(['tmux', 'kill-session', '-t', TMUX_SESSION],
                           capture_output=True, timeout=10)
        except Exception:
            pass
        return False


def raw_files(outpath):
    try:
        return sorted(f for f in os.listdir(outpath) if f.endswith('.raw'))
    except Exception:
        return []


def wait_for_running(timeout=40.0):
    """True once RunControl reports the producers RUNNING.

    Used in place of waiting for the .raw to grow, because the trigger is
    deliberately not started until the run is up — and with no trigger there is
    no data to wait for. Scraping the TUI is crude, but RunControl exposes its
    state nowhere else.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            out = subprocess.run(['tmux', 'capture-pane', '-p', '-t',
                                  f'{TMUX_SESSION}:rc'],
                                 capture_output=True, text=True, timeout=5).stdout
        except Exception:
            out = ''
        out = re.sub(r'\x1b\[[0-9;]*m', '', out)
        if re.search(r'ALPIDE_plane_\d\s+RUNNING', out):
            return True
        time.sleep(1.0)
    return False


def wait_for_data(outpath, timeout=20.0):
    """True once a .raw file exists and is growing — i.e. the run really came
    up. default_run returning a pid proves nothing on its own."""
    deadline = time.time() + timeout
    first = None
    while time.time() < deadline:
        files = raw_files(outpath)
        if files:
            size = os.path.getsize(os.path.join(outpath, files[0]))
            if first is None:
                first = size
            elif size > first:
                return True
        time.sleep(1.0)
    return False
