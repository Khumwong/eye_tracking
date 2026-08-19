#!/usr/bin/env python3
"""Turn one session folder into the number the whole experiment exists to get:
how many milliseconds pass between the app commanding the beam off and the
protons actually stopping.

    python3 analyze_latency.py output/session_20260817_191837
    python3 analyze_latency.py --self-check          # newest session, health only

Offline and read-only with respect to the acquisition code — nothing here runs
while a measurement is in progress, so it can be as slow and as strict as it
likes.

Three files inside the session carry the measurement:

  session.json      alpide_error must be null, or the run has no detector data
  beam_events.csv   B1/B0 transitions stamped with the trigger pulse count
  alpide/*.raw      EUDAQ2 native file, one readout per trigger pulse

The shared clock is the trigger pulse: the Arduino counts the pulses it emits
(reset by T1), and the ALPIDE data carries the DAQ board's own count of the same
pulses. At TRIGGER_HZ = 1000 one pulse is one millisecond, so the latency is a
subtraction of two integers — see README.md, "ALPIDE acquisition".

Why the decoder below is a Python port rather than a call into EUDAQ2: the
pyeudaq built on this machine exposes FileReader but not StdEventConverter, so
the C++ ALPIDE→StandardEvent converter cannot be reached from Python here.
Hit counts are all this analysis needs, which is a small fraction of what that
converter does, so it is ported instead of rebuilding EUDAQ2.
"""
import argparse
import csv
import glob
import json
import os
import sys
import traceback

import numpy as np

import config

# ── Analysis tuning ──────────────────────────────────────────────────────────
# Triggers skipped after each commanded transition before the interval counts
# towards a beam/noise level. The beam does not change state on the trigger the
# command was logged at — that delay is the quantity being measured — so the
# start of every interval is contaminated by the previous state.
LEVEL_GUARD_TRIG = 200
# An interval must have at least this many triggers left after the guard to be
# worth taking a level from.
LEVEL_MIN_TRIG = 50
# Triggers either side of a transition summarised in the report, and the
# shortest stretch the changepoint search will look at.
REPORT_WIN = 20
# A plane is used only if the beam lifts it this many standard errors above its
# own noise, measured over REPORT_WIN triggers. Plane noise rates differ by a
# factor of ~50 between planes, so a fixed cut cannot work — each plane is
# judged against itself. The scale is REPORT_WIN rather than the whole run
# because the question is not "do these rates differ at all" (over 40,000
# triggers almost any drift would) but "can a transition be located to a few
# triggers", and that is decided by the spread over a few triggers.
PLANE_SNR = 5.0
# ... and the lift has to be a real fraction of the noise as well, so that a
# baseline drifting over a long run cannot pass the significance test alone.
PLANE_MIN_RATIO = 1.2


class DecodeError(Exception):
    pass


# ── ALPIDE raw block decoding ────────────────────────────────────────────────
# Port of eudaq2/user/ITS3/module/src/ALPIDERawEvent2StdEventConverter.cc, kept
# deliberately close to it so the two can be diffed. The pixel coordinate
# arithmetic is dropped: only the number of hits matters here, not where they
# landed. Everything that advances the read position is preserved exactly,
# because getting that wrong silently miscounts rather than failing.

def decode_block(d):
    """Return (trig_n, timestamp_ps, n_hits) for one plane's raw block."""
    n = len(d)
    if n < 20 or d[0:4] != b'\xaa\xaa\xaa\xaa':
        raise DecodeError('no event header')
    trig_n = int.from_bytes(d[4:8], 'little')
    # 80 MHz DAQ clock ticks; 12500 ps per tick
    ts_ps = int.from_bytes(d[8:16], 'little') * 12500

    i = 16
    hits = 0
    if (d[i] & 0xF0) == 0xE0:        # chip empty frame: no hits at all
        i += 4
    elif (d[i] & 0xF0) == 0xA0:      # chip header
        i += 2
        while i < n - 4:
            w = d[i]
            if (w & 0xC0) == 0x00:   # data long: one pixel + a 7-bit hit map
                hits += 1
                bitmap = d[i + 2]
                while bitmap:
                    hits += bitmap & 1
                    bitmap >>= 1
                i += 3
            elif (w & 0xC0) == 0x40:  # data short: one pixel
                hits += 1
                i += 2
            elif (w & 0xE0) == 0xC0:  # region header
                i += 1
            elif (w & 0xF0) == 0xB0:  # chip trailer, padded to a 4-byte boundary
                i += 1
                i = (i + 3) // 4 * 4
                break
            elif w == 0xFF:           # idle
                i += 1
            else:
                raise DecodeError('bad word 0x%02X at offset %d' % (w, i))
    else:
        raise DecodeError('no event start (0x%02X)' % d[i])

    if d[i:i + 4] != b'\xbb\xbb\xbb\xbb':
        raise DecodeError('bad or missing event trailer at offset %d' % i)
    return trig_n, ts_ps, hits


def _pyeudaq():
    """Import pyeudaq from the EUDAQ2 build tree.

    Unlike the acquisition path this needs no clean_env(): pyeudaq.so is a plain
    extension module that imports fine under this project's venv. Only the
    directory has to be on sys.path.
    """
    os.environ.setdefault('EUDAQ_LOG_LEVEL', 'ERROR')
    if config.EUDAQ_LIB not in sys.path:
        sys.path.insert(0, config.EUDAQ_LIB)
    try:
        import pyeudaq
    except ImportError as e:
        raise SystemExit('cannot import pyeudaq from %s: %s' % (config.EUDAQ_LIB, e))
    return pyeudaq


class RawRun:
    """Per-trigger hit counts for every plane, plus what went wrong reading them.

    Indexed by trigger number rather than by position in the file: events do get
    dropped at 1 kHz (~0.3%, see README), and the trigger number is what ties a
    readout to the Arduino's pulse count. Missing triggers stay NaN so a gap can
    never be mistaken for a quiet detector.
    """

    def __init__(self):
        self.hits = None          # float array [max_trig + 1, n_planes], NaN where missing
        self.ts_ps = None         # int64 array [max_trig + 1], 0 where missing
        self.n_events = 0         # ITS3global events in the file
        self.n_blocks = 0         # plane blocks successfully decoded
        self.errors = {}          # message -> count
        self.other_events = {}    # description -> count, for non-ITS3global events
        self.trig_mismatch = 0    # sub-event TriggerN != trigger number inside the block
        self.plane_disagree = 0   # events whose planes reported different trigger numbers
        self.plane_drift = {}     # device -> how often it ran ahead of the slowest plane
        self.planes = []          # device numbers seen


def read_raw(path, progress=True):
    pyeudaq = _pyeudaq()
    reader = pyeudaq.FileReader('native', path)
    run = RawRun()

    rows = {}          # trig_n -> {device_n: hits}
    stamps = {}        # trig_n -> timestamp_ps
    planes = set()

    while True:
        ev = reader.GetNextEvent()
        if ev is None:
            break
        desc = ev.GetDescription()
        if desc != 'ITS3global':
            # BORE and the per-plane status events carry no pixel data and no
            # usable trigger number.
            run.other_events[desc] = run.other_events.get(desc, 0) + 1
            continue
        run.n_events += 1
        if progress and run.n_events % 50000 == 0:
            print('  ... %d events' % run.n_events, file=sys.stderr)

        seen = {}
        for i in range(ev.GetNumSubEvent()):
            sub = ev.GetSubEvent(i)
            if sub.GetNumBlock() < 1:
                run.errors['no block'] = run.errors.get('no block', 0) + 1
                continue
            try:
                trig_n, ts_ps, hits = decode_block(bytes(sub.GetBlock(0)))
            except DecodeError as e:
                key = str(e).split(' at offset')[0]
                run.errors[key] = run.errors.get(key, 0) + 1
                continue
            dev = sub.GetDeviceN()
            planes.add(dev)
            run.n_blocks += 1
            if sub.GetTriggerN() != trig_n:
                run.trig_mismatch += 1
            seen[dev] = (trig_n, ts_ps, hits)

        if not seen:
            continue
        trig_ns = set(v[0] for v in seen.values())
        if len(trig_ns) != 1:
            # A plane can slip a trigger and then stay offset for the rest of
            # the run — measured on one real run where plane 1 ran one ahead of
            # the other five from the third event onwards. Discarding those
            # events threw away 19,409 of 19,411 and left the run looking empty.
            run.plane_disagree += 1
            for dev, (t, _, _) in seen.items():
                if t != min(trig_ns):
                    run.plane_drift[dev] = run.plane_drift.get(dev, 0) + 1
        # Each plane is filed under the trigger number it reported itself. That
        # number is the shared clock with the Arduino's pulse count, so a plane
        # that has slipped is still correctly placed in time — it just sits one
        # row from its neighbours instead of corrupting the whole event.
        for dev, (t, ts_ps, hits) in seen.items():
            rows.setdefault(t, {})[dev] = hits
        # Timestamps come only from the planes reporting the lowest trigger
        # number in the event. Where one plane has over-counted, the majority is
        # the better estimate of the true pulse, and mixing the two sources
        # makes the recorded time run backwards between rows.
        ref = min(trig_ns)
        for dev, (t, ts_ps, _) in seen.items():
            if t == ref:
                stamps.setdefault(ref, ts_ps)
                break

    run.planes = sorted(planes)
    if rows:
        n_trig = max(rows) + 1
        hits = np.full((n_trig, len(run.planes)), np.nan)
        ts = np.zeros(n_trig, dtype=np.int64)
        col = {dev: j for j, dev in enumerate(run.planes)}
        for trig_n, per_plane in rows.items():
            for dev, h in per_plane.items():
                hits[trig_n, col[dev]] = h
            # A slipped plane contributes trigger indices the reference planes
            # never reported; those rows have hits but no trusted time. 0 means
            # "unknown" and the period stats below skip it.
            ts[trig_n] = stamps.get(trig_n, 0)
        run.hits = hits
        run.ts_ps = ts
    return run


# ── Session inputs ───────────────────────────────────────────────────────────

def find_session(path):
    """Accept a session folder, the output folder, or nothing (newest session)."""
    if path is None:
        path = config.OUTPUT_DIR
    path = os.path.abspath(path)
    if os.path.isdir(os.path.join(path, 'alpide')) or \
            os.path.exists(os.path.join(path, 'session.json')):
        return path
    candidates = sorted(glob.glob(os.path.join(path, 'session_*')))
    if not candidates:
        raise SystemExit('no session folder found in %s' % path)
    return candidates[-1]


def read_session(folder):
    with open(os.path.join(folder, 'session.json')) as f:
        meta = json.load(f)
    # A failed ALPIDE run looks exactly like a good one from the outside: the
    # folder, the CSV and session.json are all written either way. This is the
    # only thing that distinguishes them, and skipping the check has already
    # cost one run.
    if meta.get('alpide_error') is not None:
        raise SystemExit('session has alpide_error=%r — the run recorded no '
                         'detector data, there is nothing to analyse'
                         % meta['alpide_error'])
    if 'alpide_error' not in meta:
        print('warning: session.json predates the alpide_error field, so a '
              'failed ALPIDE run cannot be told apart from a good one here')
    return meta


def read_beam_events(folder):
    """Transitions as (trig_n, event, monotonic), dropping the unusable rows.

    trig_running=0 means the pulse column is a leftover count from a previous
    run — T0 stops the trigger without clearing the counter, so those rows carry
    a number that looks perfectly reasonable and means nothing.
    """
    path = os.path.join(folder, 'alpide', 'beam_events.csv')
    if not os.path.exists(path):
        raise SystemExit('no beam_events.csv in %s — START was never pressed, '
                         'or the run was cancelled before it got that far'
                         % os.path.join(folder, 'alpide'))
    out, skipped = [], 0
    with open(path) as f:
        reader = csv.DictReader(f)
        if 'trig_running' not in (reader.fieldnames or []):
            # Assuming every row is good is precisely the mistake this column
            # was added to prevent: a leftover count looks like a valid one.
            raise SystemExit(
                '%s predates the trig_running column, so there is no way to '
                'tell which pulse counts are leftovers from an earlier run — '
                'the transitions in it cannot be trusted' % path)
        for row in reader:
            if row['trig_running'] != '1':
                skipped += 1
                continue
            # The Arduino counts completed periods, EUDAQ numbers the first
            # trigger it receives 0 — one trigger apart.
            out.append((int(row['pulse']) - 1, row['event'],
                        float(row['host_monotonic'])))
    return out, skipped


# ── Levels and transition finding ────────────────────────────────────────────

def beam_state_intervals(events, n_trig):
    """[(start_trig, end_trig, beam_on)] over the whole run.

    The state before the first logged transition is the opposite of what that
    transition switched to.
    """
    if not events:
        return []
    intervals = []
    state = events[0][1] == 'B0'      # a B0 means it was on until then
    start = 0
    for trig_n, ev, _ in events:
        trig_n = max(0, min(trig_n, n_trig))
        intervals.append((start, trig_n, state))
        state = (ev == 'B1')
        start = trig_n
    intervals.append((start, n_trig, state))
    return [iv for iv in intervals if iv[1] > iv[0]]


def _sample(hits, intervals, want_on):
    """Rows from intervals of the requested beam state, past the guard band."""
    parts = []
    for start, end, on in intervals:
        if on != want_on:
            continue
        s = start + LEVEL_GUARD_TRIG
        if end - s < LEVEL_MIN_TRIG:
            continue
        parts.append(hits[s:end])
    if not parts:
        return np.empty((0, hits.shape[1]))
    return np.concatenate(parts, axis=0)


def plane_levels(hits, intervals):
    """Per-plane (noise_mean, noise_std, beam_mean, usable) from the run itself."""
    off = _sample(hits, intervals, False)
    on = _sample(hits, intervals, True)
    levels = []
    for j in range(hits.shape[1]):
        noise = off[:, j][~np.isnan(off[:, j])] if off.size else np.array([])
        beam = on[:, j][~np.isnan(on[:, j])] if on.size else np.array([])
        nm = float(noise.mean()) if noise.size else float('nan')
        ns = float(noise.std()) if noise.size else float('nan')
        bm = float(beam.mean()) if beam.size else float('nan')
        # Judged against this plane's own noise: an absolute cut cannot serve
        # planes whose quiet rates differ by a factor of 50.
        margin = PLANE_SNR * max(ns, 1e-9) / np.sqrt(REPORT_WIN)
        usable = bool(noise.size and beam.size and
                      bm > nm + margin and bm > PLANE_MIN_RATIO * nm)
        levels.append((nm, ns, bm, usable))
    return levels


def find_settle(signal, start, limit, rate_from, rate_to):
    """The trigger at which the hit rate switches from `rate_from` to `rate_to`,
    or None if it never convincingly does before `limit`.

    Maximum-likelihood changepoint rather than "first window under a threshold".
    A threshold on a sliding window is biased early by roughly half the window:
    the window mean crosses the midpoint between the two rates when the window
    is only half past the real transition, which at a 20 ms window is a 10 ms
    error on a quantity of the same order. Hit counts are Poisson, so the
    likelihood is exact and needs no window at all — for known rates the
    log-likelihood of a changepoint at t reduces to a suffix sum, and its
    largest value is the estimate.
    """
    seg = signal[start:limit]
    if seg.size < REPORT_WIN:
        return None
    # Gain in log-likelihood from attributing one trigger to rate_to instead of
    # rate_from. Missing triggers score 0: they say nothing either way.
    w = np.log(rate_to / rate_from)
    gain = np.where(np.isnan(seg), 0.0,
                    np.nan_to_num(seg) * w + (rate_from - rate_to))
    suffix = np.empty(seg.size + 1)
    suffix[:-1] = np.cumsum(gain[::-1])[::-1]   # suffix[k] = gain[k:].sum()
    suffix[-1] = 0.0                            # no change at all
    k = int(np.argmax(suffix))
    if k >= seg.size:
        return None

    # The best changepoint of a signal that never changed is still a
    # changepoint, so check the fit before believing it.
    post = seg[k:]
    post = post[~np.isnan(post)]
    if post.size < REPORT_WIN or \
            abs(post.mean() - rate_to) >= abs(post.mean() - rate_from):
        return None
    if k > 0:
        pre = seg[:k]
        pre = pre[~np.isnan(pre)]
        if pre.size and abs(pre.mean() - rate_from) >= abs(pre.mean() - rate_to):
            return None
    return start + k


def measure(signal, events, noise, beam, n_trig, ms_per_trig):
    """One row per commanded transition, with the latency where measurable."""
    rows = []
    for k, (trig_n, ev, mono) in enumerate(events):
        nxt = events[k + 1][0] if k + 1 < len(events) else n_trig
        if trig_n < 0 or trig_n >= n_trig:
            rows.append(dict(event=ev, trig=trig_n, settle=None, latency=None,
                             before=float('nan'), after=float('nan'),
                             note='trigger %d outside the recorded run' % trig_n))
            continue
        pre = signal[max(0, trig_n - REPORT_WIN):trig_n]
        pre = float(np.nanmean(pre)) if pre.size and not np.all(np.isnan(pre)) else float('nan')
        rate_from, rate_to = (beam, noise) if ev == 'B0' else (noise, beam)
        settle = find_settle(signal, trig_n, nxt, rate_from, rate_to)
        post = float('nan')
        if settle is not None:
            w = signal[settle:settle + REPORT_WIN]
            post = float(np.nanmean(w)) if not np.all(np.isnan(w)) else float('nan')
        # A latency of ~0 can mean the beam was already in the commanded state,
        # which is not a fast response and must not be averaged in as one.
        already = bool(settle is not None and not np.isnan(pre) and
                       abs(pre - rate_to) < abs(pre - rate_from))
        rows.append(dict(
            event=ev, trig=trig_n, mono=mono, settle=settle, already=already,
            latency=None if settle is None else (settle - trig_n) * ms_per_trig,
            before=pre, after=post,
            note='already in the commanded state before the command' if already
                 else '' if settle is not None
                 else 'no convincing change before the next transition'))
    return rows


# ── Reporting ────────────────────────────────────────────────────────────────

def report_health(run, meta, events, skipped, n_trig):
    hz = meta.get('trigger_hz') or config.TRIGGER_HZ
    print('── data health ' + '─' * 50)
    print('  ITS3global events   : %d' % run.n_events)
    print('  plane blocks decoded: %d  (%d planes: %s)'
          % (run.n_blocks, len(run.planes), run.planes))
    print('  decode errors       : %s'
          % ('none' if not run.errors else run.errors))
    print('  other event types   : %s'
          % ('none' if not run.other_events else run.other_events))
    if run.plane_disagree:
        drift = ', '.join('p%d ×%d' % (d, n)
                          for d, n in sorted(run.plane_drift.items()))
        frac = 100.0 * run.plane_disagree / max(run.n_events, 1)
        print('  planes out of step  : %d events (%.1f%%) — ahead: %s'
              % (run.plane_disagree, frac, drift))
        print('                        each plane is filed under its own trigger '
              'number and the majority sets the clock,')
        print('                        so a slipped plane is offset by a trigger '
              'rather than corrupting the run')
    else:
        print('  planes out of step  : none')
    print('  TriggerN vs block   : %d mismatches' % run.trig_mismatch)

    observed = int(np.count_nonzero(~np.isnan(run.hits[:, 0])))
    print('  trigger numbers     : 0..%d, %d present, %d missing (%.2f%%)'
          % (n_trig - 1, observed, n_trig - observed,
             100.0 * (n_trig - observed) / max(n_trig, 1)))

    have = np.nonzero(run.ts_ps)[0]
    if have.size > 1:
        dt_us = np.diff(run.ts_ps[have]) / np.diff(have) / 1e6
        print('  trigger period      : min %.1f  median %.1f  max %.1f us '
              '(expected %.1f)'
              % (dt_us.min(), np.median(dt_us), dt_us.max(), 1e6 / hz))

    if events:
        last = events[-1][0]
        print('  last usable pulse   : trigger %d, run holds %d  (%+d)'
              % (last, n_trig, n_trig - 1 - last))
    print('  beam_events rows    : %d usable, %d skipped (trig_running=0)'
          % (len(events), skipped))

    print('  hits per trigger    : %s'
          % '  '.join('p%d %.3f' % (dev, np.nanmean(run.hits[:, j]))
                      for j, dev in enumerate(run.planes)))


def report_levels(run, levels, used):
    print('── beam vs noise (measured from this run) ' + '─' * 23)
    print('  %-6s %10s %10s %10s   %s' % ('plane', 'noise', 'noise sd', 'beam', 'used'))
    for j, dev in enumerate(run.planes):
        nm, ns, bm, usable = levels[j]
        print('  p%-5d %10.3f %10.3f %10.3f   %s'
              % (dev, nm, ns, bm, 'yes' if usable else 'no'))
    print('  planes used         : %s' % (used if used else 'none'))


def _event_latency_stats(rows, ev):
    """(values, dropped_count) for one event kind ('B0' or 'B1'), or (None, n)
    if nothing was measurable. Shared by report_transitions (printing) and the
    analysis.json / latency-budget writers, so the two can never disagree
    about which rows counted."""
    vals = [r['latency'] for r in rows if r['event'] == ev
            and r['latency'] is not None and not r['already']]
    dropped = sum(1 for r in rows if r['event'] == ev
                  and (r['latency'] is None or r['already']))
    return (np.array(vals) if vals else None), dropped


def report_transitions(rows, ms_per_trig):
    print('── transitions ' + '─' * 50)
    print('  %-4s %8s %8s %10s %10s %10s  %s'
          % ('ev', 'trigger', 'settled', 'latency ms', 'hits pre', 'hits post', ''))
    for r in rows:
        print('  %-4s %8d %8s %10s %10.2f %10.2f  %s'
              % (r['event'], r['trig'],
                 '-' if r['settle'] is None else r['settle'],
                 '-' if r['latency'] is None else '%.1f' % r['latency'],
                 r['before'], r['after'], r['note']))

    for ev, label in (('B0', 'beam off (the safety-critical one)'),
                      ('B1', 'beam on')):
        a, dropped = _event_latency_stats(rows, ev)
        print('── %s: %s ' % (ev, label) + '─' * max(0, 45 - len(label)))
        if dropped:
            print('  %d transition(s) excluded, see the notes above' % dropped)
        if a is None:
            print('  no measurable transitions')
            continue
        print('  n %d   mean %.1f ms   median %.1f ms   min %.1f   max %.1f'
              % (a.size, a.mean(), np.median(a), a.min(), a.max()))
        print('  (resolution is one trigger = %.1f ms)' % ms_per_trig)


def write_csv(folder, rows, ms_per_trig):
    path = os.path.join(folder, 'latency.csv')
    with open(path, 'w', newline='') as f:
        w = csv.writer(f)
        # `excluded` marks the rows the printed summary left out, so recomputing
        # the statistics from this file gives the same answer.
        w.writerow(['event', 'trig_commanded', 'trig_settled', 'latency_ms',
                    'hits_before', 'hits_after', 'ms_per_trigger', 'excluded',
                    'note'])
        for r in rows:
            w.writerow([r['event'], r['trig'],
                        '' if r['settle'] is None else r['settle'],
                        '' if r['latency'] is None else '%.1f' % r['latency'],
                        '%.3f' % r['before'], '%.3f' % r['after'],
                        ms_per_trig,
                        1 if (r['latency'] is None or r['already']) else 0,
                        r['note']])
    return path


# ── Latency budget: the other half of the chain ──────────────────────────────
# analyze_latency's transitions are the command-to-protons-stopped leg, timed
# on the pulse count — the tight half. track.csv (capture.py) adds the other
# half that used to have no record at all: how long detection and the gate
# decision take on this side of the serial link, per frame, on the same
# monotonic clock. Still missing is [1]→[3] — sensor exposure and the USB
# transfer, before any of this code ever sees a frame — which needs a hardware
# timestamp (an LED the Arduino lights and stamps itself) to measure at all.

def compute_budget(folder):
    """The track.csv half of the latency budget, as a dict — no printing, so
    it can be computed once and either reported alone (self-check, no beam
    signal) or augmented with the ALPIDE-side numbers once `rows` exists,
    without ever reading the file or printing the section twice.

    None if there is no track.csv to read: an older session, or one where the
    no-DAQ fallback was armed before this file existed.
    """
    path = os.path.join(folder, 'track.csv')
    if not os.path.exists(path):
        return None
    t_cap = []
    t_dec = []
    with open(path, newline='') as f:
        for r in csv.DictReader(f):
            try:
                t_cap.append(float(r['t_capture']))
                t_dec.append(float(r['t_decided']))
            except (KeyError, ValueError):
                continue
    if len(t_cap) < 2:
        return None
    t_cap = np.array(t_cap)
    t_dec = np.array(t_dec)
    decide_ms = (t_dec - t_cap) * 1000.0
    period_ms = np.diff(np.sort(t_cap)) * 1000.0
    # A seek or a loop restart (video mode only) can make consecutive capture
    # times go backwards or jump by seconds; drop anything outside one frame
    # at a plausible camera rate rather than let one bad row skew the median.
    period_ms = period_ms[(period_ms > 0) & (period_ms < 1000)]

    d = {
        'frames': int(t_cap.size),
        'detect_decide_ms': {
            'median': round(float(np.median(decide_ms)), 2),
            'p95': round(float(np.percentile(decide_ms, 95)), 2),
            'max': round(float(decide_ms.max()), 2),
        },
    }
    if period_ms.size:
        d['frame_period_ms'] = {'median': round(float(np.median(period_ms)), 2)}
    return d


def report_budget(budget, meta):
    """Print the base latency-budget section (the half computed from
    track.csv alone). Call once; see report_budget_closing_leg for the part
    that needs `rows`."""
    print()
    print('── latency budget ' + '─' * 47)
    print('  [3]→[5]  detect + decide     : median %6.2f ms   p95 %6.2f ms'
          % (budget['detect_decide_ms']['median'], budget['detect_decide_ms']['p95']))
    if 'frame_period_ms' in budget:
        print('           frame period         : median %6.2f ms'
              % budget['frame_period_ms']['median'])
    print('  not yet measured [1]→[3] (camera exposure + USB transfer) — needs')
    print('  an LED the Arduino lights and stamps itself; see next.md')
    hold = meta.get('min_off_s')
    if hold:
        budget['opening_leg_min_off_s'] = hold
        print('  opening leg additionally waits min_off_s = %.3g s once the '
              'beam is cut, before any of the above starts' % hold)


def report_budget_closing_leg(budget, rows):
    """Append the command→beam-off/on figures once `rows` exists (i.e. only
    on the path that reached measure()). Mutates and returns `budget` so the
    caller's analysis.json copy picks up the new fields too."""
    close_vals, _ = _event_latency_stats(rows, 'B0')
    if close_vals is not None:
        close_ms = round(float(np.median(close_vals)), 1)
        budget['command_to_beam_off_ms'] = close_ms
        total = round(budget['detect_decide_ms']['median'] + close_ms, 1)
        budget['total_close_leg_ms'] = total
        print('  [5]→[6]  command → beam off  : median %6.1f ms  '
              '(safety-critical leg)' % close_ms)
        print('  ' + '─' * 60)
        print('  measured total, closing leg  : %6.1f ms' % total)
    open_vals, _ = _event_latency_stats(rows, 'B1')
    if open_vals is not None:
        budget['command_to_beam_on_ms'] = round(float(np.median(open_vals)), 1)
    return budget


def health_summary(run, meta, events, skipped, n_trig, hz):
    """The same facts report_health() prints, as a dict — kept as a separate
    read of the same inputs rather than parsed out of the printed text, so a
    wording change to the report can never silently break analysis.json."""
    observed = int(np.count_nonzero(~np.isnan(run.hits[:, 0])))
    d = {
        'n_events':            run.n_events,
        'n_blocks':            run.n_blocks,
        'planes':              list(run.planes),
        'decode_errors':       dict(run.errors),
        'other_event_types':   dict(run.other_events),
        'planes_out_of_step':  run.plane_disagree,
        'planes_out_of_step_pct': round(
            100.0 * run.plane_disagree / max(run.n_events, 1), 2),
        'trig_mismatch':       run.trig_mismatch,
        'n_trig':              n_trig,
        'trig_present':        observed,
        'trig_missing':        n_trig - observed,
        'beam_events_usable':  len(events),
        'beam_events_skipped': skipped,
        'hits_per_trigger':    {
            'p%d' % dev: round(float(np.nanmean(run.hits[:, j])), 4)
            for j, dev in enumerate(run.planes)},
    }
    have = np.nonzero(run.ts_ps)[0]
    if have.size > 1:
        dt_us = np.diff(run.ts_ps[have]) / np.diff(have) / 1e6
        d['trigger_period_us'] = {
            'min': round(float(dt_us.min()), 1),
            'median': round(float(np.median(dt_us)), 1),
            'max': round(float(dt_us.max()), 1),
            'expected': round(1e6 / hz, 1),
        }
    return d


class _Tee:
    """Duplicates writes to the real stream and to an in-memory buffer, so the
    operator still sees progress live while main() also gets the full text to
    put in report.txt afterwards."""

    def __init__(self, stream):
        self._stream = stream
        self._buf = []

    def write(self, s):
        self._stream.write(s)
        self._buf.append(s)

    def flush(self):
        self._stream.flush()

    def getvalue(self):
        return ''.join(self._buf)


def write_report(folder, text):
    path = os.path.join(folder, 'report.txt')
    with open(path, 'w') as f:
        f.write(text)
    return path


def _json_default(o):
    if isinstance(o, np.integer):
        return int(o)
    if isinstance(o, np.floating):
        return float(o)
    return str(o)


def write_analysis_json(folder, analysis):
    path = os.path.join(folder, 'analysis.json')
    with open(path, 'w') as f:
        json.dump(analysis, f, indent=2, default=_json_default)
    return path
    return path


# ── Entry point ──────────────────────────────────────────────────────────────

def _run(args, analysis):
    """Does the actual work; main() wraps this so report.txt and
    analysis.json get written whichever way it exits — including the early
    SystemExit('...') paths below, which is why each one sets a verdict on
    `analysis` immediately before raising rather than leaving the caller to
    infer one from the exception text."""
    folder = find_session(args.session)
    analysis['folder'] = folder
    print('session: %s' % folder)
    meta = read_session(folder)
    analysis['meta'] = {k: meta.get(k) for k in
                        ('trigger_hz', 'trigger_duty', 'threshold_mm',
                         'min_off_s', 'started', 'stopped', 'ended',
                         'beam_on_s')}
    events, skipped = read_beam_events(folder)

    raws = sorted(glob.glob(os.path.join(folder, 'alpide', '*.raw')))
    if not raws:
        analysis['verdict'] = 'no_raw_file'
        raise SystemExit('no .raw file in %s — the run produced no detector '
                         'data at all, which the rest of the session folder '
                         'does not show' % os.path.join(folder, 'alpide'))
    if len(raws) > 1:
        print('warning: %d raw files, using %s' % (len(raws), os.path.basename(raws[-1])))
    run = read_raw(raws[-1], progress=not args.quiet)
    if run.hits is None:
        analysis['verdict'] = 'no_decodable_data'
        raise SystemExit('no decodable ALPIDE data in %s' % raws[-1])

    n_trig = run.hits.shape[0]
    hz = meta.get('trigger_hz') or config.TRIGGER_HZ
    ms_per_trig = 1000.0 / hz

    report_health(run, meta, events, skipped, n_trig)
    analysis['health'] = health_summary(run, meta, events, skipped, n_trig, hz)
    # Independent of whether this run has any beam signal at all: it is a
    # measurement of this app's own performance, not of the .raw. Computed
    # once here; report_budget_closing_leg augments the same dict later if
    # `rows` ends up existing, rather than this being read or printed twice.
    budget = compute_budget(folder)
    if budget:
        report_budget(budget, meta)
        analysis['latency_budget'] = budget

    if args.self_check:
        analysis['verdict'] = 'self_check'
        return 0

    if not events:
        analysis['verdict'] = 'no_transitions'
        raise SystemExit('no beam transitions with trig_running=1 — nothing to '
                         'measure against')

    intervals = beam_state_intervals(events, n_trig)
    levels = plane_levels(run.hits, intervals)
    used = [dev for j, dev in enumerate(run.planes) if levels[j][3]]
    report_levels(run, levels, used)
    analysis['levels'] = {
        'p%d' % dev: {'noise': round(nm, 3), 'noise_sd': round(ns, 3),
                     'beam': round(bm, 3), 'used': bool(usable)}
        for dev, (nm, ns, bm, usable) in zip(run.planes, levels)}
    analysis['planes_used'] = used

    if not used:
        print()
        print('no beam signal detected: no plane separates its beam-on from its '
              'beam-off periods.')
        # Two very different situations produce this, and saying the wrong one
        # sends the next person looking in the wrong place.
        if run.plane_disagree > 0.5 * run.n_events:
            print('Most events had planes out of step (see above), so this is a '
                  'DAQ synchronisation problem,')
            print('not a statement about protons. Fix the sync before reading '
                  'anything into this run.')
            analysis['verdict'] = 'daq_sync_problem'
        else:
            print('Expected for a run taken without protons — every frame is '
                  'noise, so there is no drop to time.')
            analysis['verdict'] = 'no_beam_signal'
        return 2

    cols = [j for j, dev in enumerate(run.planes) if levels[j][3]]
    signal = np.nansum(run.hits[:, cols], axis=1)
    signal[np.all(np.isnan(run.hits[:, cols]), axis=1)] = np.nan
    noise = sum(levels[j][0] for j in cols)
    beam = sum(levels[j][2] for j in cols)
    print('  combined            : noise %.2f  beam %.2f hits/trigger' % (noise, beam))

    rows = measure(signal, events, noise, beam, n_trig, ms_per_trig)
    report_transitions(rows, ms_per_trig)
    analysis['transitions'] = {
        ev: (lambda a, dropped: {'n': int(a.size), 'mean_ms': round(float(a.mean()), 1),
                                 'median_ms': round(float(np.median(a)), 1),
                                 'min_ms': round(float(a.min()), 1),
                                 'max_ms': round(float(a.max()), 1),
                                 'excluded': dropped}
                    if a is not None else {'n': 0, 'excluded': dropped})(
            *_event_latency_stats(rows, ev))
        for ev in ('B0', 'B1')}
    # `rows` exists now, so the closing-leg total (detect+decide plus
    # command-to-beam-off) can be appended to the section already printed
    # above — nothing gets printed twice.
    if budget:
        report_budget_closing_leg(budget, rows)
        analysis['latency_budget'] = budget
    analysis['verdict'] = 'ok'

    print()
    print('wrote %s' % write_csv(folder, rows, ms_per_trig))
    return 0


def _write_outputs(analysis, tee):
    """report.txt + analysis.json, if a session folder was ever identified —
    called from every exit path in main() so a session folder explains itself
    even when analyze_latency.py itself is what went wrong."""
    folder = analysis.get('folder')
    if not folder:
        return
    try:
        print('wrote %s' % write_report(folder, tee.getvalue()))
    except Exception as e:
        print('warning: could not write report.txt: %s' % e, file=sys.stderr)
    try:
        write_analysis_json(folder, analysis)
    except Exception as e:
        print('warning: could not write analysis.json: %s' % e, file=sys.stderr)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('session', nargs='?', default=None,
                    help='session folder, or a folder of them (default: newest '
                         'in %s)' % config.OUTPUT_DIR)
    ap.add_argument('--self-check', action='store_true',
                    help='report data health only, without measuring latency')
    ap.add_argument('--quiet', action='store_true', help='no progress output')
    args = ap.parse_args(argv)

    analysis = {}
    real_stdout = sys.stdout
    tee = _Tee(real_stdout)
    sys.stdout = tee
    try:
        try:
            code = _run(args, analysis)
        except SystemExit as e:
            # find_session() can also raise before any folder is known — in
            # that case there is nowhere to write outputs, same as before this
            # existed, so only print here and let the message reach stderr as
            # it always did.
            if isinstance(e.code, str):
                print(e.code)
                analysis.setdefault('verdict', 'error')
                analysis['error'] = e.code
                code = 1
            else:
                raise
        except Exception:
            # Anything unforeseen still gets a report rather than vanishing
            # into a bare traceback with nothing on disk — the whole point of
            # this file is that a session folder explains itself, including
            # when this script is what went wrong. Re-raised after writing so
            # the traceback still reaches stderr and the exit code stays
            # nonzero, same as before this existed.
            tb = traceback.format_exc()
            print(tb)
            analysis.setdefault('verdict', 'error')
            analysis['error'] = tb
            sys.stdout = real_stdout
            _write_outputs(analysis, tee)
            raise
    finally:
        sys.stdout = real_stdout

    _write_outputs(analysis, tee)
    return code


if __name__ == '__main__':
    sys.exit(main())
