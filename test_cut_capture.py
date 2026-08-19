#!/usr/bin/env python3
"""Checks for the beam-cut snapshots in capture.py.

The property worth testing is not that the images come out — that is visible by
eye — but that handing a frame over never stalls the loop it is called from.
That loop decides when the beam turns off, and its period is the largest single
term in the latency the whole experiment exists to measure, so a snapshot that
blocked it would quietly inflate the number rather than break anything.

    python3 test_cut_capture.py
"""
import os
import queue
import shutil
import threading
import time

import numpy as np

import capture
import config

FAILURES = []
TMP = '/tmp/claude-1000/test_cut_capture'


def check(name, cond, detail=''):
    print('%-4s %s%s' % ('ok' if cond else 'FAIL', name,
                         '' if not detail else '  (%s)' % detail))
    if not cond:
        FAILURES.append(name)


def make_capture(threshold=3.0):
    """A CaptureThread with no camera: the snapshot path touches only params,
    the last deviation, and its own queue.

    armed=True: a cut mid-run is what these tests mean to exercise (trigger/
    deviation-based reasons) — _gate_reason() checks armed before any of that,
    so an unarmed fixture would report every cut as 'unarmed' regardless of
    what the test set up."""
    c = capture.CaptureThread(
        cap=None, params={'threshold_mm': threshold}, arduino=None,
        is_video=False, fps=30.0, total_frames=0,
        frame_queue=queue.Queue(maxsize=2), pause_event=threading.Event(),
        on_video_end=None, on_progress=None, armed=True)
    c._last_deviation_mm = 4.2
    return c


def frame_1080p():
    # Noise rather than a flat colour: a flat frame compresses to almost
    # nothing and would make the encode look far cheaper than it is.
    rng = np.random.default_rng(0)
    return rng.integers(0, 255, (1080, 1920, 3), dtype=np.uint8)


def fresh(sub):
    path = os.path.join(TMP, sub)
    shutil.rmtree(path, ignore_errors=True)
    return path


def read_csv(folder):
    import csv
    with open(os.path.join(folder, 'cuts.csv')) as f:
        return list(csv.DictReader(f))


def test_does_not_block():
    print('\n-- handing a frame over never stalls the caller --')
    folder = fresh('block')
    c = make_capture()
    frame = frame_1080p()

    # How long the writer actually needs, for comparison. If enqueueing were
    # doing the work itself, it would cost about this much.
    import cv2
    t0 = time.perf_counter()
    cv2.imwrite(os.path.join(TMP, 'encode_probe.jpg'), frame,
                [cv2.IMWRITE_JPEG_QUALITY, config.CUT_JPEG_QUALITY])
    encode_ms = (time.perf_counter() - t0) * 1000

    c.start_cut_capture(folder)
    worst = 0.0
    # Far more transitions than a real run sees, fired back to back so the
    # queue is full and the writer busy for most of them.
    for _ in range(200):
        t = time.perf_counter()
        c._queue_cut(frame, False, False)
        worst = max(worst, (time.perf_counter() - t) * 1000)
    c.stop_cut_capture()

    print('     encode costs %.1f ms; worst enqueue %.2f ms' % (encode_ms, worst))
    # Measured at 0.03 ms once the frame stopped being copied; 1 ms leaves room
    # for a loaded machine while still catching a regression that reintroduces
    # real work here (a 1080p copy alone measured 4.4 ms).
    check('enqueue stays under 1 ms', worst < 1.0, 'worst %.2f ms' % worst)
    check('enqueue is far cheaper than the encode it avoids',
          worst < encode_ms / 4, 'enqueue %.2f vs encode %.1f' % (worst, encode_ms))
    check('frames the writer could not keep up with are counted',
          c._cut_dropped > 0, 'dropped %d of 200' % c._cut_dropped)

    rows = read_csv(folder)
    saved = [r for r in rows if r['index']]
    check('every saved row has an image on disk',
          all(os.path.exists(os.path.join(folder, r['file'])) for r in saved),
          '%d rows' % len(saved))
    check('saved + dropped accounts for everything offered',
          len(saved) + c._cut_dropped == 200,
          'saved %d + dropped %d' % (len(saved), c._cut_dropped))
    check('the drop count is recorded in the CSV',
          any(r['reason'] == 'dropped' for r in rows))


def test_contents():
    print('\n-- what each row says --')
    folder = fresh('contents')
    c = make_capture(threshold=8.5)
    frame = np.zeros((240, 320, 3), dtype=np.uint8)

    c.start_cut_capture(folder)
    c._last_deviation_mm = 9.75
    c._queue_cut(frame, False, False)          # out of range
    time.sleep(0.2)
    c._last_deviation_mm = None
    c._queue_cut(frame, False, False)          # eyes closed
    time.sleep(0.2)
    c._queue_cut(frame, None, False)           # no face at all
    c.stop_cut_capture()

    rows = [r for r in read_csv(folder) if r['index']]
    check('one row per cut', len(rows) == 3, 'got %d' % len(rows))
    if len(rows) != 3:
        return
    check('reasons are distinguished',
          [r['reason'] for r in rows] == ['deviation', 'blink', 'no_face'],
          'got %s' % [r['reason'] for r in rows])
    check('deviation recorded when there was one',
          rows[0]['deviation_mm'] == '9.7500' and rows[1]['deviation_mm'] == '',
          'got %r / %r' % (rows[0]['deviation_mm'], rows[1]['deviation_mm']))
    check('threshold in force is recorded',
          all(r['threshold_mm'] == '8.50' for r in rows))
    check('monotonic clock is present and increasing',
          [float(r['host_monotonic']) for r in rows] ==
          sorted(float(r['host_monotonic']) for r in rows))
    check('files are numbered in order',
          [r['file'] for r in rows] ==
          ['cut_0001.jpg', 'cut_0002.jpg', 'cut_0003.jpg'],
          'got %s' % [r['file'] for r in rows])


def test_reason_is_why_the_beam_actually_went_off():
    print('\n-- the reason column tracks _gate_open(), not the eye alone --')
    # A real run caught this: STOP disarms mid-run while the eye happens to be
    # on target. The cut this fires still has to be logged (something turned
    # the beam off), but before _gate_reason() existed the label came from
    # trigger/deviation alone and called it 'deviation' — with deviation_mm
    # comfortably under threshold. Misleading: the eye had nothing to do with
    # it, disarming did.
    folder = fresh('reason_precedence')
    c = make_capture(threshold=6.0)
    frame = np.zeros((240, 320, 3), dtype=np.uint8)
    c.start_cut_capture(folder)

    c._last_deviation_mm = 0.69           # on target — well under 6.0 mm
    c.armed = False                       # ...but STOP just disarmed
    c._queue_cut(frame, True, False)      # trigger=True, gate_open=False
    time.sleep(0.2)

    c.armed = True
    c.kill_latched = True                 # ...or KILL BEAM just latched
    c._queue_cut(frame, True, False)
    time.sleep(0.2)
    c.stop_cut_capture()

    rows = [r for r in read_csv(folder) if r['index']]
    check('two rows recorded', len(rows) == 2, 'got %d' % len(rows))
    if len(rows) != 2:
        return
    check("disarm mid-run is labelled 'unarmed', not 'deviation'",
          rows[0]['reason'] == 'unarmed', 'got %r' % rows[0]['reason'])
    check("a latched kill is labelled 'kill', not 'deviation'",
          rows[1]['reason'] == 'kill', 'got %r' % rows[1]['reason'])
    check('deviation_mm is still recorded either way (it is diagnostic, '
          'just not the reason)',
          rows[0]['deviation_mm'] == '0.6900')


def test_limit():
    print('\n-- a flickering threshold cannot fill the disk --')
    folder = fresh('limit')
    original = config.CUT_MAX_PER_SESSION
    config.CUT_MAX_PER_SESSION = 5
    try:
        c = make_capture()
        frame = np.zeros((240, 320, 3), dtype=np.uint8)
        c.start_cut_capture(folder)
        for _ in range(40):
            c._queue_cut(frame, False, False)
            time.sleep(0.01)            # let the writer keep up, so drops aren't the cap
        c.stop_cut_capture()
    finally:
        config.CUT_MAX_PER_SESSION = original

    jpgs = [f for f in os.listdir(folder) if f.endswith('.jpg')]
    check('stops at the limit', len(jpgs) == 5, 'got %d files' % len(jpgs))
    check('the CSV says it stopped',
          any(r['reason'] == 'limit_reached' for r in read_csv(folder)))


def test_stop_is_idempotent():
    print('\n-- start/stop hygiene --')
    folder = fresh('hygiene')
    c = make_capture()
    c.stop_cut_capture()                # never started
    check('stopping before starting is harmless', True)

    c.start_cut_capture(folder)
    c.start_cut_capture(folder)         # restart replaces the first writer
    c._queue_cut(np.zeros((240, 320, 3), dtype=np.uint8), False, False)
    c.stop_cut_capture()
    c.stop_cut_capture()
    check('stopping twice is harmless', True)
    check('the restarted writer still wrote', len(read_csv(folder)) >= 1)

    c._queue_cut(np.zeros((240, 320, 3), dtype=np.uint8), False, False)
    check('queueing after stop is a no-op, not a crash', True)


# ── track.csv: the per-frame log ──────────────────────────────────────────────

def read_track_csv(folder):
    import csv
    with open(os.path.join(folder, 'track.csv')) as f:
        return list(csv.DictReader(f))


def test_track_does_not_block():
    print('\n-- writing a track.csv row never stalls the loop --')
    folder = fresh('track_block')
    c = make_capture()
    c.armed = True

    c.start_track_log(folder)
    worst = 0.0
    for i in range(500):
        t = time.perf_counter()
        c._write_track_row(t_capture=time.monotonic(), trigger=True,
                           armed_trigger=True)
        worst = max(worst, (time.perf_counter() - t) * 1000)
    c.stop_track_log()

    print('     worst per-row cost %.3f ms' % worst)
    # No encode here at all, just a buffered csv.writerow — this should be far
    # cheaper than the 1 ms cuts.csv budget, which does the same kind of write.
    # 0.5 ms leaves headroom for a loaded machine while still catching a
    # regression that adds real work (a flush every row, say) to this path.
    check('a row costs under 0.5 ms', worst < 0.5, 'worst %.3f ms' % worst)

    rows = read_track_csv(folder)
    check('every call produced a row', len(rows) == 500, 'got %d' % len(rows))


def test_track_contents():
    print('\n-- what each track.csv row says --')
    folder = fresh('track_contents')
    c = make_capture()
    c.armed = True
    c.kill_latched = False
    c.current_state = 'O'

    c.start_track_log(folder)
    c._last_deviation_mm = 2.1
    c._last_iris_px = 44.5
    c._write_track_row(t_capture=1000.0, trigger=True, armed_trigger=True)

    c._last_deviation_mm = 9.9
    c._write_track_row(t_capture=1000.05, trigger=False, armed_trigger=False)

    c._last_deviation_mm = None
    c._write_track_row(t_capture=1000.10, trigger=None, armed_trigger=False)
    c.stop_track_log()

    rows = read_track_csv(folder)
    check('one row per call', len(rows) == 3, 'got %d' % len(rows))
    if len(rows) != 3:
        return
    check('frame index counts up from 0',
          [r['frame'] for r in rows] == ['0', '1', '2'])
    check('t_capture round-trips',
          [float(r['t_capture']) for r in rows] == [1000.0, 1000.05, 1000.1])
    check('t_decided is stamped and no earlier than t_capture',
          all(float(r['t_decided']) >= float(r['t_capture']) for r in rows))
    check('deviation recorded when there was one, blank when not',
          rows[0]['deviation_mm'] == '2.1000' and rows[2]['deviation_mm'] == '')
    check('gate open has no reason', rows[0]['reason'] == '')
    check('gate shut on target gets a real reason, not blank',
          rows[1]['reason'] == 'deviation')
    check('no face is distinguished from a shut gate on target',
          rows[2]['reason'] == 'no_face')
    check('detect column mirrors trigger, blank on no-face',
          [r['detect'] for r in rows] == ['1', '0', ''])


def test_gate_reason_matches_gate_open():
    print('\n-- _gate_reason agrees with _gate_open on why, not just whether --')
    c = make_capture()
    now = 1000.0

    for armed, kill, trig, off_since, hold in [
        (True,  False, True,  None, 1.0),
        (True,  False, False, None, 1.0),
        (True,  False, None,  None, 1.0),
        (True,  True,  True,  None, 1.0),
        (False, False, True,  None, 1.0),
        (True,  False, True,  now - 0.1, 1.0),   # inside the min-off hold
    ]:
        c.armed = armed
        c.kill_latched = kill
        c._beam_off_since = off_since
        c.params['min_off_s'] = hold
        c._last_deviation_mm = 4.2 if trig is not None else None
        gate_open = c._gate_open(trig, now=now)
        reason = c._gate_reason(trig, gate_open)
        check('open=%s trig=%s armed=%s kill=%s -> reason=%r is consistent'
              % (gate_open, trig, armed, kill, reason),
              (reason == '') == gate_open,
              'gate_open=%s reason=%r' % (gate_open, reason))


def test_track_stop_is_idempotent():
    print('\n-- track.csv start/stop hygiene --')
    folder = fresh('track_hygiene')
    c = make_capture()
    c.armed = True
    c.stop_track_log()                  # never started
    check('stopping before starting is harmless', True)

    c.start_track_log(folder)
    c.start_track_log(folder)           # restart replaces the first writer
    c._write_track_row(time.monotonic(), True, True)
    c.stop_track_log()
    c.stop_track_log()
    check('stopping twice is harmless', True)
    check('the restarted writer still wrote', len(read_track_csv(folder)) >= 1)

    c._write_track_row(time.monotonic(), True, True)
    check('writing after stop is a no-op, not a crash', True)


if __name__ == '__main__':
    os.makedirs(TMP, exist_ok=True)
    test_does_not_block()
    test_contents()
    test_reason_is_why_the_beam_actually_went_off()
    test_limit()
    test_stop_is_idempotent()
    test_track_does_not_block()
    test_track_contents()
    test_gate_reason_matches_gate_open()
    test_track_stop_is_idempotent()
    print()
    if FAILURES:
        print('%d failed: %s' % (len(FAILURES), ', '.join(FAILURES)))
        raise SystemExit(1)
    print('all checks passed')
