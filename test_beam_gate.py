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


def test_auto_latch():
    print('\n-- the manual-resume latch outranks the eye, like the kill latch --')
    c = make_capture(armed=True)
    check('starts unlatched', c.auto_latched is False)

    c.auto_latched = True
    for trigger in (True, False, None):
        check('latched: %r stays shut' % (trigger,),
              c._gate_open(trigger) is False)

    c.auto_latched = False
    check('releasing hands the beam back to the eye',
          c._gate_open(True) is True)

    c.armed = False
    c.auto_latched = True
    check('latched and unarmed is still shut', c._gate_open(True) is False)


def test_closed_by_eye():
    print('\n-- only an eye-caused close trips the manual-resume latch --')
    c = make_capture(armed=True)
    check('off by default, regardless of kill_latched',
          c._closed_by_eye() is False)
    c.kill_latched = True
    check('still off by default, even during a kill',
          c._closed_by_eye() is False)
    c.kill_latched = False

    c.params['manual_resume'] = True
    check('on, and no kill in force: this close is the eye\'s',
          c._closed_by_eye() is True)

    c.kill_latched = True
    check('on, but KILL BEAM owns this close: not claimed here',
          c._closed_by_eye() is False)


def test_resume_request():
    print('\n-- RESUME BEAM only takes effect while the eye is on target --')
    c = make_capture(armed=True)

    c.auto_latched = True
    c.resume_requested = True
    c._process_resume_request(False)
    check('a request while off target is discarded, not queued',
          c.auto_latched is True and c.resume_requested is False)

    c.resume_requested = True
    c._process_resume_request(None)
    check('no face is treated the same as off target',
          c.auto_latched is True and c.resume_requested is False)

    c.resume_requested = True
    c._process_resume_request(True)
    check('a request while on target clears the latch',
          c.auto_latched is False and c.resume_requested is False)

    c.auto_latched = False
    c._process_resume_request(True)
    check('with nothing pending, an on-target frame does nothing on its own',
          c.auto_latched is False)


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

    def start_track_log(self, folder):
        self._log.append(('start_track_log', ''))

    def stop_track_log(self):
        self._log.append(('stop_track_log', ''))

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

    def _traced_alpide_stop(on_stopped=None):
        log.append(('_alpide_stop', ''))
        orig_alpide_stop(on_stopped=on_stopped)
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


# ── undoing a launch ─────────────────────────────────────────────────────────

def _idle_app(root):
    """An app that is READY with a preview running but nothing armed."""
    app = build_app(root)
    log = []
    app.arduino = _FakeArduino(log)
    cap = _FakeCapture(log)
    cap._armed = False              # bypass the setter: no event to record yet
    app.capture = cap
    app.input_mode.set('camera')
    app.ready = True
    return app, log


def _trace_alpide_stop(app, calls):
    def _traced():
        calls.append(True)
        app._alpide_pid = None      # what the real teardown ends up doing
    app._alpide_stop = _traced


def test_unready_tears_down_the_daq():
    print('\n-- UNREADY after a launch that was never started --')
    root = _try_root('900x700')
    if root is None:
        return
    app, log = _idle_app(root)
    calls = []
    _trace_alpide_stop(app, calls)
    app._alpide_pid = 4242          # LAUNCH brought EUDAQ2 up
    app._session_root = '/tmp/session_not_written'
    app._set_launch_state('ready')

    app._on_enable_toggle()         # UNREADY (enable_var defaults False)

    check('the abandoned run is stopped', calls == [True], 'calls %s' % calls)
    # _launch_daq() returns immediately while this is set, so leaving it behind
    # made every later LAUNCH a silent no-op with the button lit as the next step
    check('nothing is left holding the next LAUNCH', app._alpide_pid is None)
    check('the folder is released too', app._session_root == '')
    check('Enable still comes down', ('send', 'E0') in log,
          'sent %s' % [b for a, b in log if a == 'send'])
    teardown(app, root)


def test_cancel_daq():
    print('\n-- CANCEL DAQ undoes LAUNCH and nothing else --')
    root = _try_root('900x700')
    if root is None:
        return
    app, log = _idle_app(root)
    calls = []
    _trace_alpide_stop(app, calls)
    app._alpide_pid = 4242
    app._session_root = '/tmp/session_not_written'
    app._set_launch_state('ready')
    app._killed = True              # a latch in force must survive this
    app.capture.kill_latched = True

    app._cancel_daq()

    check('the DAQ is stopped', calls == [True], 'calls %s' % calls)
    check('the folder is released', app._session_root == '')
    check('LAUNCH is offered again', app._launch_state == 'idle',
          'state %r' % app._launch_state)
    # The whole point of a separate button: it is the detector being cancelled.
    check('nothing is sent to the Arduino',
          [b for a, b in log if a == 'send'] == [],
          'sent %s' % [b for a, b in log if a == 'send'])
    check('the gate is not touched', ('armed', False) not in log)
    check('the kill latch survives',
          app._killed is True and app.capture.kill_latched is True)

    # during a run, STOP is the only thing that ends anything
    calls.clear()
    app.capture._armed = True
    app._alpide_pid = 4243
    app._cancel_daq()
    check('refuses while a run is armed',
          calls == [] and app._alpide_pid == 4243)
    teardown(app, root)


def test_start_waits_for_its3():
    print('\n-- START is held shut while the DAQ comes up --')
    root = _try_root('900x700')
    if root is None:
        return
    app, _log = _idle_app(root)

    app._set_launch_state('launching')
    check('START is disabled during the launch',
          str(app.start_btn['state']) == 'disabled',
          'state %r' % str(app.start_btn['state']))
    check('and says what it is waiting for', 'ITS3' in app.start_btn['text'],
          'text %r' % app.start_btn['text'])
    check('CANCEL is offered', app._cancel_shown is True)

    app._set_launch_state('ready')
    check('pressable once the run is up',
          str(app.start_btn['state']) == 'normal')

    # A launch that fails releases it — the detector must never trap the gate.
    app._set_launch_state('launching')
    app._set_launch_state('idle')
    check('a failed launch gives START back',
          str(app.start_btn['state']) == 'normal')

    # ...and so does one that simply never answers.
    app._alpide_pid = None
    app._set_launch_state('launching')
    app._launch_watchdog(app._launch_seq - 1)
    check('a watchdog from an earlier launch is ignored',
          str(app.start_btn['state']) == 'disabled')
    app._launch_watchdog(app._launch_seq)
    check('a wedged launch gives START back',
          str(app.start_btn['state']) == 'normal')
    teardown(app, root)


# ── the LAUNCH-gated row ─────────────────────────────────────────────────────

def test_daq_row_visibility():
    print('\n-- START/STOP/CANCEL appear together, only once LAUNCH has happened --')
    root = _try_root('900x700')
    if root is None:
        return
    app, _log = _idle_app(root)
    app._refresh_flow()   # _idle_app sets app.ready directly, no refresh yet

    check('before LAUNCH: only the no-DAQ fallback is offered',
          app._group_shown is False and app._fallback_shown is True,
          'group %s fallback %s' % (app._group_shown, app._fallback_shown))

    app._set_launch_state('launching')
    check('LAUNCH pressed: the group appears, the fallback steps aside',
          app._group_shown is True and app._fallback_shown is False)

    app._set_launch_state('ready')
    check('stays up once the DAQ reports ready', app._group_shown is True)

    app.capture._armed = True
    app._refresh_flow()
    check('stays up for the run itself', app._group_shown is True)
    check('the fallback cannot reappear once a run exists',
          app._fallback_shown is False)

    app.capture._armed = False
    app._set_launch_state('idle')
    check('folds away once both the run and the launch are over',
          app._group_shown is False)
    check('the fallback is back for the next setup',
          app._fallback_shown is True)
    teardown(app, root)


# ── recording tied to LAUNCH, not to a button pressed every run ──────────────

def test_auto_record():
    print('\n-- the Record checkbox is intent, LAUNCH is the trigger --')
    root = _try_root('900x700')
    if root is None:
        return
    app, _log = _idle_app(root)
    started = []
    app._start_rec = lambda: started.append(True)

    app.record_enabled.set(False)
    app._maybe_auto_record()
    check('unchecked: nothing starts', started == [])

    app.record_enabled.set(True)
    app._maybe_auto_record()
    check('checked: LAUNCH (or the fallback start) starts one',
          started == [True])

    app.capture._recording = True   # what the real _start_rec would have set
    app._maybe_auto_record()
    check('already recording: calling again is a no-op', started == [True])

    stopped = []
    app._stop_rec = lambda: stopped.append(True)
    app._alpide_stop = lambda: None
    app._alpide_pid = 4242
    app._cancel_daq()
    check('CANCEL stops a recording it is responsible for', stopped == [True])
    teardown(app, root)


# ── the event log that survives launch.sh not redirecting stdout ─────────────

def test_app_log():
    print('\n-- debug/app.log gets what happened, including before LAUNCH --')
    root = _try_root('900x700')
    if root is None:
        return
    import config
    import shutil
    tmp_out = '/tmp/claude-1000/test_app_log_output'
    shutil.rmtree(tmp_out, ignore_errors=True)
    original_out = config.OUTPUT_DIR
    config.OUTPUT_DIR = tmp_out    # absolute path wins over os.path.join's
                                   # first argument — see os.path.join docs
    try:
        app = build_app(root)

        # Everything that matters before a folder exists — Arduino connect,
        # a camera failure, a firmware flash — must not be lost just because
        # nobody had opened a terminal.
        app.log('[ALPIDE] flashing firmware…')
        app.log('[Arduino] connected')
        check('nothing written to disk yet, only buffered',
              not os.path.isdir(tmp_out))

        folder = app._session_dir()   # what LAUNCH's _alpide_open_dir does
        check('debug/app.log exists as soon as a folder does',
              os.path.exists(os.path.join(folder, 'debug', 'app.log')))

        with open(os.path.join(folder, 'debug', 'app.log')) as f:
            text = f.read()
        check('the pre-folder backlog is in there',
              'flashing firmware' in text and 'connected' in text,
              'contents: %r' % text)

        app.log('[BEAM] killed by operator')
        with open(os.path.join(folder, 'debug', 'app.log')) as f:
            text2 = f.read()
        check('lines logged after the folder exists are appended too',
              'killed by operator' in text2)

        app._close_app_log()
        check('closing drops the file handle so the next _session_dir() '
              'reopens fresh rather than writing through a stale one',
              app._app_log_file is None)
        app.log('[SESSION] would be lost without a session')
        app._session_root = ''   # simulate the next run getting a fresh folder
        folder2 = app._session_dir()
        with open(os.path.join(folder2, 'debug', 'app.log')) as f:
            text3 = f.read()
        # the line logged between close and the next folder is still backlog,
        # whether or not the timestamp-named folder happens to collide with
        # the previous one within the same second
        check('the gap between sessions is not lost either',
              'would be lost without a session' in text3)

        teardown(app, root)
    finally:
        config.OUTPUT_DIR = original_out
        shutil.rmtree(tmp_out, ignore_errors=True)


# ── analyze_latency.py runs itself after STOP ─────────────────────────────────

def test_auto_analyze():
    print('\n-- STOP hands the finished run to analyze_latency.py --')
    root = _try_root('900x700')
    if root is None:
        return
    import json
    import shutil
    app, _log = _idle_app(root)

    folder = '/tmp/claude-1000/test_auto_analyze_session'
    shutil.rmtree(folder, ignore_errors=True)
    os.makedirs(os.path.join(folder, 'alpide'), exist_ok=True)
    with open(os.path.join(folder, 'session.json'), 'w') as f:
        json.dump({'alpide_error': None, 'trigger_hz': 1000}, f)
    # A real START writes this even before EUDAQ2 ever comes up, so an empty
    # one (header only) is what a run that never got a .raw actually looks
    # like — not the same as START never having been pressed at all.
    with open(os.path.join(folder, 'alpide', 'beam_events.csv'), 'w') as f:
        f.write('host_time_iso,host_monotonic,event,pulse,trig_running\n')
    # Deliberately no .raw file: exercises the real subprocess and the real
    # JSON round trip without needing to synthesize valid ALPIDE bytes —
    # the decoder itself is already covered by test_latency.py.

    app._auto_analyze(folder)

    check('report.txt was written',
          os.path.exists(os.path.join(folder, 'report.txt')))
    json_path = os.path.join(folder, 'analysis.json')
    check('analysis.json was written', os.path.exists(json_path))
    if not os.path.exists(json_path):
        teardown(app, root)
        return
    with open(json_path) as f:
        analysis = json.load(f)
    check('the verdict says why: no .raw file',
          analysis.get('verdict') == 'no_raw_file',
          'verdict %r' % analysis.get('verdict'))
    check('_read_verdict agrees with what is on disk',
          app._read_verdict(folder) == 'no_raw_file')
    check('the status line reflects it',
          'no_raw_file' in app._alpide_msg, 'msg %r' % app._alpide_msg)
    # Via log_to_folder(), not log()'s in-memory buffer/current-session file —
    # _auto_analyze runs after STOP has moved on, sometimes long after, so it
    # must land in *this* folder's app.log regardless of whatever session (if
    # any) is current by the time it finishes. A real run once produced a
    # report.txt with no trace of it in debug/app.log because of exactly this
    # race — this is the regression test for that.
    log_path = os.path.join(folder, 'debug', 'app.log')
    check('the run is noted in this session\'s own app.log, not wherever '
          'the shared handle happened to be pointing',
          os.path.exists(log_path) and
          any('[ANALYZE]' in line for line in open(log_path)),
          'exists=%s' % os.path.exists(log_path))
    teardown(app, root)


# ── session.json says how the run actually ended ─────────────────────────────

def test_ended_field():
    print('\n-- session.json records how the run actually ended, not just that it did --')
    import json
    import shutil

    def session_json_at(folder):
        with open(os.path.join(folder, 'session.json')) as f:
            return json.load(f)

    # Each case tears its own app and root down before the next one starts —
    # on_close() in particular destroys the root itself, so no case can share
    # one with another.
    def run_case(folder, setup_and_act, destroys_root=False):
        root = _try_root('900x700')
        if root is None:
            return None
        shutil.rmtree(folder, ignore_errors=True)
        app, _log = _idle_app(root)
        app._session_root = folder
        app.capture._armed = True
        setup_and_act(app)
        got = session_json_at(folder).get('ended')
        if not destroys_root:
            teardown(app, root)
        return got

    got = run_case('/tmp/claude-1000/test_ended_stop', lambda app: app.stop())
    check("ordinary STOP records ended='stop'", got == 'stop', 'got %r' % got)

    def _enable_off(app):
        app.capture.running = True
        app._confirm_disable_dialog = lambda: 'continue'
        app.enable_var.set(False)   # the click that unchecked it, already applied
        app._on_enable_toggle()
    got = run_case('/tmp/claude-1000/test_ended_enable_off', _enable_off)
    check("unchecking ENABLE mid-run records ended='enable_off'",
          got == 'enable_off', 'got %r' % got)

    def _arduino_reset(app):
        app._show_warning = lambda *a, **k: None
        app._on_arduino_reset()
    got = run_case('/tmp/claude-1000/test_ended_arduino_reset', _arduino_reset)
    check("a board reset mid-run records ended='arduino_reset'",
          got == 'arduino_reset', 'got %r' % got)

    got = run_case('/tmp/claude-1000/test_ended_app_closed',
                   lambda app: app.on_close(), destroys_root=True)
    check("closing the app mid-run records ended='app_closed'",
          got == 'app_closed', 'got %r' % got)


def test_session_json_provenance():
    print('\n-- session.json also says which code and which machine --')
    root = _try_root('900x700')
    if root is None:
        return
    import json
    import shutil
    app, _log = _idle_app(root)
    folder = '/tmp/claude-1000/test_session_provenance'
    shutil.rmtree(folder, ignore_errors=True)
    app._session_root = folder
    app.capture._armed = True
    app.stop()

    with open(os.path.join(folder, 'session.json')) as f:
        info = json.load(f)
    check('hostname is recorded', bool(info.get('hostname')))
    check('git commit is recorded (this repo has one)',
          bool(info.get('git_commit')), 'got %r' % info.get('git_commit'))
    check('git_dirty is a real bool, not left unset', 'git_dirty' in info)
    check('camera nominal_fps is recorded',
          info.get('camera', {}).get('nominal_fps') is not None,
          'camera %r' % info.get('camera'))
    teardown(app, root)


# ── beam time delivered ──────────────────────────────────────────────────────

def test_beam_used_counter():
    print('\n-- how much of the requested beam has been delivered --')
    root = _try_root('900x700')
    if root is None:
        return
    import time as _t
    app, _log = _idle_app(root)
    app._beam_on_total = 0.0
    app._beam_on_since = None

    check('nothing delivered before the shutter opens', app.beam_used_s() == 0.0)

    app._beam_on()
    _t.sleep(0.15)
    running = app.beam_used_s()
    check('it counts while the shutter is open', running >= 0.15,
          'got %.3f' % running)

    app._beam_on()      # the frame loop calls this on every ON frame
    _t.sleep(0.1)
    app._beam_off()
    first = app._beam_on_total
    check('re-entering ON does not restart the period', first >= 0.25,
          'got %.3f' % first)

    _t.sleep(0.15)
    app._beam_off()     # and every OFF frame
    check('time with the shutter shut does not count',
          app._beam_on_total == first, 'got %.3f' % app._beam_on_total)

    app._beam_on()
    _t.sleep(0.1)
    app._beam_off()
    check('a second period adds to the first',
          app._beam_on_total >= first + 0.1,
          'got %.3f' % app._beam_on_total)
    teardown(app, root)


def test_arduino_reset():
    print('\n-- the board resetting mid-run --')
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
    app._show_warning = lambda *a, **k: warned.append(a)

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


def _cleanup_stray_alpide_session():
    """Safety net after the whole suite runs. None of these tests drive a
    real LAUNCH DAQ — every _alpide_pid set above is a fake int, and
    _alpide_stop is monkeypatched away wherever it matters (see
    _trace_alpide_stop) — so this suite should never itself bring up the
    real ITS3 tmux session. But it runs right next to whatever the operator
    has open in the real app, so if a session is up when the suite ends,
    only clear it when it's 'stale' (a crashed/finished run's leftover
    shell — see alpide_daq.session_state's own docstring for why that's the
    one state that's safe to clear unconditionally). A 'running' session
    means every producer is still alive, i.e. someone may be genuinely
    acquiring real data right now — leave that alone and just say so, the
    same restraint app.py's own _alpide_bring_up applies before ever
    touching a live session.
    """
    import alpide_daq
    state = alpide_daq.session_state()
    if state == 'none':
        return
    if state == 'stale':
        print(f'\n[cleanup] ITS3 session left stale after the suite — clearing it')
        alpide_daq.kill_session()
        return
    print(f'\n[cleanup] ITS3 session is still RUNNING after the suite — '
          f'not touching it, this looks like a real acquisition in progress. '
          f'Check it manually if that is unexpected.')


if __name__ == '__main__':
    test_gate()
    test_kill_latch()
    test_min_off_hold()
    test_auto_latch()
    test_closed_by_eye()
    test_resume_request()
    test_min_off_disabled()
    test_chatter_is_absorbed()
    test_stop_order()
    test_start_during_launch()
    test_unready_tears_down_the_daq()
    test_cancel_daq()
    test_start_waits_for_its3()
    test_daq_row_visibility()
    test_auto_record()
    test_app_log()
    test_auto_analyze()
    test_ended_field()
    test_session_json_provenance()
    test_beam_used_counter()
    test_arduino_reset()
    _cleanup_stray_alpide_session()
    print()
    if FAILURES:
        print('%d failed: %s' % (len(FAILURES), ', '.join(FAILURES)))
        raise SystemExit(1)
    print('all checks passed')
