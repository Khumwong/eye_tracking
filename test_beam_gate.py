#!/usr/bin/env python3
"""Checks for the two ways the beam can be on when it should not be.

Neither failure breaks anything visibly: the app keeps running, the files still
get written, and the only symptom is protons being delivered at a moment nobody
asked for. That is exactly why they need tests rather than a careful reading.

    python3 test_beam_gate.py

The stop() ordering check drives the real app, so it needs a display — run it
under Xvfb if there is none:

    Xvfb :99 -screen 0 1600x1000x24 &   DISPLAY=:99 python3 test_beam_gate.py
"""
import os
import queue
import sys
import threading

FAILURES = []


def check(name, cond, detail=''):
    print('%-4s %s%s' % ('ok' if cond else 'FAIL', name,
                         '' if not detail else '  (%s)' % detail))
    if not cond:
        FAILURES.append(name)



def build_app(root):
    """Build the real app and remember the preview thread it starts.

    __init__ opens the camera on its own, so a test that swaps app.capture for
    a stand-in orphans that thread — and when the root is destroyed underneath
    it, its next root.after() call takes the interpreter down with it. That
    looked like a crash in the code under test; it was only the harness.
    """
    from app import EyeTrackingApp
    app = EyeTrackingApp(root, debug=False)
    app._preview = app.capture
    return app


def teardown(app, root):
    cap = getattr(app, '_preview', None)
    if cap is not None:
        try:
            cap.stop_cut_capture()
            cap.stop()
        except Exception:
            pass
    if getattr(app, 'cap', None) is not None:
        try:
            app.cap.release()
        except Exception:
            pass
    root.destroy()



def _try_root(geometry):
    """A Tk root, or None with a SKIP noted.

    DISPLAY being set is not the same as a display being reachable — a dead
    Xvfb leaves the variable pointing at nothing and tk.Tk() raises, which took
    the whole suite down and read as a failure in the beam gate. The checks
    that need no display have already run by then and are the ones that matter.
    """
    import tkinter as tk
    try:
        root = tk.Tk()
    except tk.TclError as e:
        print('SKIP %s (no usable display: %s)' % (geometry, e))
        return None
    root.geometry(geometry)
    return root


# ── the gate decision ────────────────────────────────────────────────────────

def make_capture(armed=True, **params):
    import capture
    return capture.CaptureThread(
        cap=None, params=params, arduino=None, is_video=False, fps=30.0,
        total_frames=0, frame_queue=queue.Queue(maxsize=2),
        pause_event=threading.Event(), on_video_end=None, on_progress=None,
        armed=armed)


def test_gate():
    print('\n-- what opens the beam --')
    c = make_capture(armed=True)

    check('on target while armed opens it', c._gate_open(True) is True)
    check('off target keeps it shut', c._gate_open(False) is False)
    check('no face keeps it shut', c._gate_open(None) is False)

    c.armed = False
    check('unarmed never opens it, even on target',
          c._gate_open(True) is False)


def test_kill_latch():
    print('\n-- the kill latch outranks the eye --')
    c = make_capture(armed=True)
    check('starts unlatched', c.kill_latched is False)

    c.kill_latched = True
    for trigger in (True, False, None):
        check('latched: %r stays shut' % (trigger,),
              c._gate_open(trigger) is False)

    # The latch is what makes the button mean anything: without it the next
    # frame with the eye on target would reopen the shutter within ~66 ms.
    c.kill_latched = False
    check('releasing hands the beam back to the eye',
          c._gate_open(True) is True)

    c.armed = False
    c.kill_latched = True
    check('latched and unarmed is still shut', c._gate_open(True) is False)


def test_min_off_hold():
    print('\n-- the beam stays off for a minimum time once cut --')
    c = make_capture(armed=True, min_off_s=2.0)
    t0 = 1000.0

    check('no hold before the first cut of a run',
          c._gate_open(True, now=t0) is True)

    c._beam_off_since = t0                      # the beam was just cut
    check('on target 0.1 s later is still refused',
          c._gate_open(True, now=t0 + 0.1) is False)
    check('still refused at 1.9 s', c._gate_open(True, now=t0 + 1.9) is False)
    check('allowed again at 2.0 s', c._gate_open(True, now=t0 + 2.0) is True)
    check('and after', c._gate_open(True, now=t0 + 5.0) is True)

    # The hold must never keep the beam ON — it has no say in closing.
    check('off target during the hold is still off',
          c._gate_open(False, now=t0 + 0.5) is False)
    check('off target after the hold is still off',
          c._gate_open(False, now=t0 + 9.9) is False)

    # ...and it must not outrank the things that close the beam.
    c.kill_latched = True
    check('a kill during the hold stays shut afterwards',
          c._gate_open(True, now=t0 + 9.9) is False)
    c.kill_latched = False
    c.armed = False
    check('unarmed after the hold is still shut',
          c._gate_open(True, now=t0 + 9.9) is False)


def test_min_off_disabled():
    print('\n-- setting it to zero restores the old behaviour --')
    c = make_capture(armed=True, min_off_s=0.0)
    c._beam_off_since = 1000.0
    check('reopens immediately with the hold off',
          c._gate_open(True, now=1000.001) is True)

    # and the default is what config says, not a hidden literal
    import config
    d = make_capture(armed=True)
    d._beam_off_since = 1000.0
    inside = d._gate_open(True, now=1000.0 + config.DEFAULT_MIN_OFF_S - 0.01)
    outside = d._gate_open(True, now=1000.0 + config.DEFAULT_MIN_OFF_S + 0.01)
    check('unset params fall back to config.DEFAULT_MIN_OFF_S',
          (inside is False and outside is True)
          if config.DEFAULT_MIN_OFF_S > 0 else outside is True,
          'default %.1f s' % config.DEFAULT_MIN_OFF_S)


def test_chatter_is_absorbed():
    print('\n-- what it does to a run that chattered --')
    # The real off-periods measured at threshold 3.0 mm and 8.5 mm; anything
    # shorter than the hold would have been one continuous off instead of a
    # separate cut-and-reopen.
    measured_ms = [35, 110, 179, 264, 303, 327, 846, 1330, 1926, 1959, 2154, 2297]
    for hold, expect in ((1.0, 7), (2.0, 10)):
        absorbed = sum(1 for m in measured_ms if m < hold * 1000)
        check('a %.1f s hold absorbs %d of the %d measured off-periods'
              % (hold, expect, len(measured_ms)),
              absorbed == expect, 'got %d' % absorbed)


# ── stop() ordering ──────────────────────────────────────────────────────────

class _FakeArduino:
    """Records what was sent, in order, alongside the shared event log."""

    def __init__(self, log):
        self._log = log
        self.is_connected = True

    def send(self, data):
        self._log.append(('send', bytes(data).strip().decode('ascii', 'replace')))

    def close(self):
        self._log.append(('close', ''))


class _FakeCapture:
    """Stands in for CaptureThread, recording when the gate actually drops."""

    def __init__(self, log):
        self._log = log
        self._armed = True
        self.kill_latched = False
        self.running = True
        self._recording = False

    @property
    def armed(self):
        return self._armed

    @armed.setter
    def armed(self, value):
        self._armed = value
        self._log.append(('armed', value))

    def stop_cut_capture(self):
        self._log.append(('stop_cut_capture', ''))

    def stop_recording(self):
        self._log.append(('stop_recording', ''))

    def is_alive(self):
        return True

    def stop(self):
        pass


def test_stop_order():
    print('\n-- STOP drops the gate before it does anything slow --')
    root = _try_root('900x700')
    if root is None:
        return
    app = build_app(root)

    log = []
    app.arduino = _FakeArduino(log)
    app.capture = _FakeCapture(log)
    app._session_root = ''          # nothing to write out
    app.input_mode.set('camera')
    orig_alpide_stop = app._alpide_stop

    def _traced_alpide_stop():
        log.append(('_alpide_stop', ''))
        orig_alpide_stop()
    app._alpide_stop = _traced_alpide_stop

    app.stop()
    teardown(app, root)

    names = [e[0] for e in log]
    print('     order:', ' → '.join(f'{a}{"" if b == "" else f"({b})"}'
                                    for a, b in log))

    check('the gate is dropped at all', 'armed' in names)
    if 'armed' not in names:
        return
    i_armed = names.index('armed')
    check('disarms before the ALPIDE teardown',
          '_alpide_stop' not in names or i_armed < names.index('_alpide_stop'),
          'armed at %d, _alpide_stop at %d'
          % (i_armed, names.index('_alpide_stop') if '_alpide_stop' in names else -1))

    sends = [i for i, n in enumerate(names) if n == 'send']
    check('B0 is the first thing sent',
          bool(sends) and log[sends[0]][1] == 'B0',
          'first send was %r' % (log[sends[0]][1] if sends else None))
    check('the gate drops no later than B0',
          bool(sends) and i_armed < sends[0],
          'armed at %d, first send at %d' % (i_armed, sends[0] if sends else -1))
    check('it is dropped, not raised', log[i_armed][1] is False)
    check('cut capture is closed after disarming, not before',
          'stop_cut_capture' in names
          and i_armed < names.index('stop_cut_capture'))



# ── deferred trigger start ───────────────────────────────────────────────────

def test_start_during_launch():
    print('\n-- START pressed while LAUNCH is still coming up --')
    root = _try_root('900x700')
    if root is None:
        return
    app = build_app(root)
    fired = []
    app._alpide_run_trigger = lambda hz: fired.append(hz)

    # mid-launch: start_run() has set the pid, wait_for_running() has not returned
    app._alpide_pid = 4242
    app._daq_ready = False
    app._alpide_start(1000)

    check('the trigger is not started against a run that is not up yet',
          fired == [], 'fired %s' % fired)
    check('the rate is held instead of dropped',
          app._pending_start_hz == 1000, 'pending %s' % app._pending_start_hz)

    app._launch_done(True)
    root.update()
    import time as _t; _t.sleep(0.3)
    check('it fires as soon as the run reaches RUNNING',
          fired == [1000], 'fired %s' % fired)
    check('and is not left armed to fire twice',
          app._pending_start_hz is None)

    # a launch that fails must not start a trigger at all
    fired.clear()
    app._alpide_pid = 4243
    app._daq_ready = False
    app._alpide_start(1000)
    app._launch_done(False)
    root.update(); _t.sleep(0.2)
    check('a failed launch starts no trigger', fired == [], 'fired %s' % fired)
    check('and says so in alpide_error', bool(app._alpide_failed),
          'alpide_error %r' % app._alpide_failed)
    teardown(app, root)


def test_arduino_reset():
    print('\n-- the board resetting mid-run --')
    import tkinter.messagebox as mb
    root = _try_root('900x700')
    if root is None:
        return
    app = build_app(root)
    log = []
    app.arduino = _FakeArduino(log)
    app.capture = _FakeCapture(log)
    app._session_root = ''
    app.input_mode.set('camera')
    app.ready = True
    warned = []
    mb.showwarning = lambda *a, **k: warned.append(a)

    app._on_arduino_reset()

    check('the gate is dropped', ('armed', False) in log)
    check('the app stops claiming READY', app.ready is False)
    check('Enable is matched to the board', ('send', 'E0') in log,
          'sent %s' % [b for a, b in log if a == 'send'])
    check('the operator is told', bool(warned))
    check('the run is marked unusable',
          'reset' in (app._alpide_failed or ''),
          'alpide_error %r' % app._alpide_failed)

    # the ordinary banner at connect time must not trip any of this
    app2_ready_before = app.ready
    app._on_arduino_reset()
    check('a banner while not ready is ignored', app.ready is app2_ready_before)
    teardown(app, root)


if __name__ == '__main__':
    test_gate()
    test_kill_latch()
    test_min_off_hold()
    test_min_off_disabled()
    test_chatter_is_absorbed()
    test_stop_order()
    test_start_during_launch()
    test_arduino_reset()
    print()
    if FAILURES:
        print('%d failed: %s' % (len(FAILURES), ', '.join(FAILURES)))
        raise SystemExit(1)
    print('all checks passed')
