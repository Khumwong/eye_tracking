#!/usr/bin/env python3
"""Checks for the RunControl pane scraping in alpide_daq.py.

Runs against a throwaway tmux session, never the real `ITS3` one — the six
boards and that session name are shared with Kcmh-Tricker, and a test that
attached to a live acquisition could disturb a run.

    python3 test_its3_monitor.py
"""
import subprocess
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


if __name__ == '__main__':
    test_parsing_offline()
    test_missing_session()
    test_live_pane()
    test_leaves_real_session_alone()
    print()
    if FAILURES:
        print('%d failed: %s' % (len(FAILURES), ', '.join(FAILURES)))
        raise SystemExit(1)
    print('all checks passed')
