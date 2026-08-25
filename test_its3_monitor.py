#!/usr/bin/env python3
"""Checks for alpide_daq.py helpers that don't need real ALPIDE hardware:
RunControl pane scraping, and the firmware-flash watchdog.

Runs against a throwaway tmux session, never the real `ITS3` one — the six
boards and that session name are shared with Kcmh-Tricker, and a test that
attached to a live acquisition could disturb a run.

    python3 test_its3_monitor.py
"""
import os
import subprocess
import tempfile
import time

import alpide_daq

FAILURES = []
SESSION = 'eye_tracking_test_pane'

# What RunControl's connection table looks like, one line per producer.
SAMPLE = """\
 type          name             state       message
 Producer      ALPIDE_plane_0   RUNNING     -
 Producer      ALPIDE_plane_1   RUNNING     -
 Producer      ALPIDE_plane_2   RUNNING     -
 Producer      ALPIDE_plane_3   CONFIGURED  -
 Producer      ALPIDE_plane_4   ERROR       lost connection
 Producer      ALPIDE_plane_5   RUNNING     -
"""


def check(name, cond, detail=''):
    print('%-4s %s%s' % ('ok' if cond else 'FAIL', name,
                         '' if not detail else '  (%s)' % detail))
    if not cond:
        FAILURES.append(name)


def tmux(*args, **kw):
    return subprocess.run(['tmux', *args], capture_output=True, text=True, **kw)


def test_parsing_offline():
    print('\n-- parsing, without tmux in the picture --')
    states = alpide_daq.plane_states(SAMPLE)
    check('all six planes found', len(states) == 6, 'got %s' % sorted(states))
    check('running planes', [states[i] for i in (0, 1, 2, 5)] == ['RUNNING'] * 4)
    check('a plane mid-configuration is not called running',
          states[3] == 'CONFIGURED', 'got %r' % states.get(3))
    check('an errored plane keeps its state', states[4] == 'ERROR',
          'got %r' % states.get(4))

    check('empty text is empty, not an error', alpide_daq.plane_states('') == {})
    check('None is tolerated', alpide_daq.plane_states(None) == {})
    check('unrelated text finds nothing',
          alpide_daq.plane_states('no planes here') == {})

    # The pane holds scrollback, so a plane appears once per redraw; only the
    # newest line describes the present.
    stale = 'ALPIDE_plane_0   CONFIGURED\nALPIDE_plane_0   RUNNING\n'
    check('the most recent line for a plane wins',
          alpide_daq.plane_states(stale) == {0: 'RUNNING'},
          'got %s' % alpide_daq.plane_states(stale))

    # Left in, the state would read as '\x1b[32mRUNNING\x1b[0m' and never equal
    # 'RUNNING' — a live run would look like one that never came up.
    coloured = alpide_daq.plane_states('ALPIDE_plane_2   \x1b[32mRUNNING\x1b[0m')
    check('ANSI colour does not have to be stripped by the caller',
          coloured == {2: 'RUNNING'}, 'got %r' % coloured)


def test_missing_session():
    print('\n-- no tmux session --')
    tmux('kill-session', '-t', SESSION)
    text = alpide_daq.pane_text(session=SESSION)
    check('returns empty string, does not raise', text == '', 'got %r' % text[:40])
    check('and parses to nothing', alpide_daq.plane_states(text) == {})


def test_live_pane():
    print('\n-- reading a real tmux pane --')
    if tmux('-V').returncode != 0:
        check('tmux available', False, 'tmux not installed — skipping')
        return
    tmux('kill-session', '-t', SESSION)
    tmux('new-session', '-d', '-s', SESSION, 'sh -c "cat; sleep 60"')
    time.sleep(0.4)
    try:
        # Sent through tmux rather than echoed by the shell so the pane holds
        # exactly these lines.
        for line in SAMPLE.splitlines():
            tmux('send-keys', '-t', f'{SESSION}:0', line, 'Enter')
        time.sleep(0.5)

        text = alpide_daq.pane_text(pane='0', session=SESSION)
        check('pane text came back', bool(text.strip()), 'got %r' % text[:60])
        check('no ANSI escapes survive', '\x1b' not in text)

        states = alpide_daq.plane_states(text)
        check('all six planes parsed off a live pane', len(states) == 6,
              'got %s' % sorted(states.items()))
        check('states match what was written',
              states.get(3) == 'CONFIGURED' and states.get(4) == 'ERROR',
              'got %s' % states)

        capped = alpide_daq.pane_text(pane='0', lines=2, session=SESSION)
        check('the line cap is honoured',
              len(capped.splitlines()) <= len(text.splitlines()),
              '%d vs %d lines' % (len(capped.splitlines()), len(text.splitlines())))
    finally:
        tmux('kill-session', '-t', SESSION)


def test_leaves_real_session_alone():
    print('\n-- the real ITS3 session is untouched --')
    check('this test never names the shared session',
          SESSION != alpide_daq.TMUX_SESSION and SESSION != 'ITS3')


# ── firmware-flash watchdog ─────────────────────────────────────────────────
# alpide-daq-program has no timeout of its own: a board that fails to
# re-enumerate after being flashed leaves it blocked forever. Kcmh-Tricker's
# own UI documents hitting exactly this and guards it with a timeout + kill —
# these checks stand in a fake program for the real one so the hang (and the
# fast, ordinary path next to it) can be exercised without real hardware, in
# well under a second either way.

def _fake_firmware_files():
    """Point install_firmware() at throwaway files so it never depends on
    Kcmh-Tricker's real firmware images being present on whatever machine
    runs this test."""
    d = tempfile.mkdtemp()
    fx3 = os.path.join(d, 'fx3.img')
    fpga = os.path.join(d, 'fpga.bit')
    open(fx3, 'wb').close()
    open(fpga, 'wb').close()
    return fx3, fpga, []


def _write_fake_programmer(d, body):
    path = os.path.join(d, 'fake-alpide-daq-program')
    with open(path, 'w') as f:
        f.write('#!/bin/bash\n' + body)
    os.chmod(path, 0o755)
    return path


def test_firmware_watchdog_kills_a_hang():
    print('\n-- a board that never re-enumerates gets killed, not waited on forever --')
    d = tempfile.mkdtemp()
    # exec, not a bare `sleep` line: bash defers a received SIGTERM until its
    # current foreground command returns, so a plain `sleep 9999` run *as a
    # child of bash* would swallow terminate() and defeat the very timeout
    # under test. exec replaces the shell with sleep outright, so the signal
    # lands on the thing actually blocking, exactly like the real tool's own
    # blocking read does.
    programmer = _write_fake_programmer(d, 'echo "Waiting for re-enumeration..."\nexec sleep 9999\n')
    alpide_daq.firmware_files = _fake_firmware_files
    alpide_daq._programmer = lambda: programmer

    lines = []
    t0 = time.monotonic()
    ok = alpide_daq.install_firmware(on_line=lines.append, timeout_s=1)
    elapsed = time.monotonic() - t0

    check('reports failure', ok is False)
    check('killed close to the timeout, not left hanging',
          elapsed < 5, 'took %.2fs for a 1s timeout' % elapsed)
    check('says why', any('timed out' in l for l in lines), 'lines %s' % lines)


def test_firmware_ordinary_exit_is_not_a_timeout():
    print('\n-- an ordinary quick exit is not mistaken for one --')
    d = tempfile.mkdtemp()
    programmer = _write_fake_programmer(
        d, 'echo "Programming FX3(s): ..."\necho "ALL DONE"\nexit 0\n')
    alpide_daq.firmware_files = _fake_firmware_files
    alpide_daq._programmer = lambda: programmer

    lines = []
    t0 = time.monotonic()
    ok = alpide_daq.install_firmware(on_line=lines.append, timeout_s=30)
    elapsed = time.monotonic() - t0

    check('reports success', ok is True)
    check('returns promptly, not after waiting out the timeout',
          elapsed < 2, 'took %.2fs' % elapsed)
    check('no timeout line on a clean exit',
          not any('timed out' in l for l in lines), 'lines %s' % lines)


if __name__ == '__main__':
    test_parsing_offline()
    test_missing_session()
    test_live_pane()
    test_leaves_real_session_alone()
    test_firmware_watchdog_kills_a_hang()
    test_firmware_ordinary_exit_is_not_a_timeout()
    print()
    if FAILURES:
        print('%d failed: %s' % (len(FAILURES), ', '.join(FAILURES)))
        raise SystemExit(1)
    print('all checks passed')
