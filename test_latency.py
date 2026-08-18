#!/usr/bin/env python3
"""Checks for analyze_latency.py against data whose answer is known in advance.

The real .raw files available so far were all taken without protons, so they can
prove the decoder reads them and that no beam is claimed where none exists — but
they cannot prove the latency arithmetic, because nothing ever drops. That part
is checked here on synthetic runs with the drop planted at a known offset.

    python3 test_latency.py
"""
import numpy as np

import analyze_latency as al

FAILURES = []


def check(name, cond, detail=''):
    print('%-4s %s%s' % ('ok' if cond else 'FAIL', name,
                         '' if not detail else '  (%s)' % detail))
    if not cond:
        FAILURES.append(name)


# ── decoder ──────────────────────────────────────────────────────────────────

def block(trig_n, ts_ticks, payload):
    body = b'\xaa\xaa\xaa\xaa' + trig_n.to_bytes(4, 'little') + \
           ts_ticks.to_bytes(8, 'little') + payload
    body += b'\x00' * (-len(body) % 4)          # trailer sits on a 4-byte boundary
    return body + b'\xbb\xbb\xbb\xbb'


def test_decoder():
    print('\n-- decoder --')
    trig, ts, hits = al.decode_block(block(7, 3, b'\xe0\x03\xff\xff'))
    check('empty frame is zero hits', hits == 0, 'got %d' % hits)
    check('trigger number read back', trig == 7, 'got %d' % trig)
    check('timestamp in ps', ts == 3 * 12500, 'got %d' % ts)

    # chip header, region header, one data short
    _, _, hits = al.decode_block(block(0, 0, b'\xa0\x09\xc0\x40\x00\xb0'))
    check('data short is one hit', hits == 1, 'got %d' % hits)

    # data long whose hit map has three bits set: base pixel + 3
    _, _, hits = al.decode_block(block(0, 0, b'\xa0\x09\xc0\x00\x00\x2a\xb0'))
    check('data long counts its hit map', hits == 4, 'got %d' % hits)

    # two regions, mixed word types
    _, _, hits = al.decode_block(block(
        0, 0, b'\xa0\x09\xc0\x40\x00\xc1\x40\x01\x00\x00\x01\xff\xb0'))
    check('mixed words', hits == 4, 'got %d' % hits)

    for name, payload in (('bad word', b'\xa0\x09\x99\xb0'),
                          ('no event start', b'\x77\x00\x00\x00')):
        try:
            al.decode_block(block(0, 0, payload))
            check('%s is rejected' % name, False, 'decoded without complaint')
        except al.DecodeError:
            check('%s is rejected' % name, True)

    try:
        al.decode_block(b'\x00' * 24)
        check('bad header is rejected', False)
    except al.DecodeError:
        check('bad header is rejected', True)


# ── latency measurement ──────────────────────────────────────────────────────

def synth_run(n_trig, events, true_settle, beam_rate, noise_rate, drop=0.0,
              seed=1):
    """Hits for two planes: one the beam lights up, one that only ever sees
    noise, so plane selection has something to reject.

    `true_settle` maps a commanded trigger to the trigger where the beam really
    changed — the offset the analysis has to recover.
    """
    rng = np.random.default_rng(seed)
    on = np.zeros(n_trig, dtype=bool)
    state = events[0][1] == 'B0'
    prev = 0
    for trig, ev, _ in events:
        settle = true_settle[trig]
        on[prev:settle] = state
        state = (ev == 'B1')
        prev = settle
    on[prev:] = state

    hits = np.empty((n_trig, 2))
    hits[:, 0] = rng.poisson(np.where(on, beam_rate, noise_rate))
    hits[:, 1] = rng.poisson(0.05, n_trig)      # quiet plane, no beam response
    if drop:
        missing = rng.random(n_trig) < drop
        hits[missing, :] = np.nan
    return hits


def run_analysis(hits, events, n_trig, ms_per_trig=1.0):
    intervals = al.beam_state_intervals(events, n_trig)
    levels = al.plane_levels(hits, intervals)
    cols = [j for j in range(hits.shape[1]) if levels[j][3]]
    if not cols:
        return None, levels
    signal = np.nansum(hits[:, cols], axis=1)
    signal[np.all(np.isnan(hits[:, cols]), axis=1)] = np.nan
    noise = sum(levels[j][0] for j in cols)
    beam = sum(levels[j][2] for j in cols)
    return al.measure(signal, events, noise, beam, n_trig, ms_per_trig), levels


def test_latency():
    print('\n-- latency on a planted drop --')
    n_trig = 20000
    events = [(5000, 'B0', 0.0), (10000, 'B1', 0.0), (15000, 'B0', 0.0)]
    truth = {5000: 5040, 10000: 10012, 15000: 15040}
    hits = synth_run(n_trig, events, truth, beam_rate=20.0, noise_rate=3.0)

    rows, levels = run_analysis(hits, events, n_trig)
    check('lit plane is used', levels[0][3])
    check('quiet plane is rejected', not levels[1][3])
    check('measured something', rows is not None)
    if rows is None:
        return

    for r, want in zip(rows, (40, 12, 40)):
        got = r['latency']
        check('%s at %d recovers %d ms' % (r['event'], r['trig'], want),
              got is not None and abs(got - want) <= 1,
              'got %s' % got)


def test_latency_with_dropped_events():
    print('\n-- latency when 3% of events are dropped --')
    n_trig = 20000
    events = [(5000, 'B0', 0.0), (10000, 'B1', 0.0)]
    truth = {5000: 5075, 10000: 10008}
    hits = synth_run(n_trig, events, truth, beam_rate=20.0, noise_rate=3.0,
                     drop=0.03, seed=7)
    rows, _ = run_analysis(hits, events, n_trig)
    check('still measures through the gaps', rows is not None)
    if rows is None:
        return
    for r, want in zip(rows, (75, 8)):
        got = r['latency']
        check('%s at %d recovers %d ms' % (r['event'], r['trig'], want),
              got is not None and abs(got - want) <= 1, 'got %s' % got)


def test_weak_beam():
    print('\n-- how the measurement degrades as the beam approaches the noise --')
    # Real beam intensity is unknown until there are protons, so what matters is
    # that a modest beam still measures, and that a beam too weak to time is
    # rejected rather than timed badly.
    n_trig = 20000
    events = [(5000, 'B0', 0.0), (10000, 'B1', 0.0), (15000, 'B0', 0.0)]
    truth = {5000: 5040, 10000: 10012, 15000: 15040}

    for beam, tol in ((10.0, 2), (6.0, 5)):
        errs = []
        for seed in range(1, 6):
            hits = synth_run(n_trig, events, truth, beam_rate=beam,
                             noise_rate=3.0, seed=seed)
            rows, _ = run_analysis(hits, events, n_trig)
            if rows is None:
                errs.append(None)
                continue
            errs += [None if r['latency'] is None
                     else abs(r['latency'] - (truth[r['trig']] - r['trig']))
                     for r in rows]
        worst = max((e for e in errs if e is not None), default=None)
        check('beam %.0fx noise stays within %d ms' % (beam / 3.0, tol),
              None not in errs and worst is not None and worst <= tol,
              'worst %s ms' % worst)

    for beam in (4.0, 3.0):
        rejected = []
        for seed in range(1, 6):
            hits = synth_run(n_trig, events, truth, beam_rate=beam,
                             noise_rate=3.0, seed=seed)
            rows, _ = run_analysis(hits, events, n_trig)
            rejected.append(rows is None)
        check('beam of %.1f vs noise 3.0 is refused, not timed' % beam,
              all(rejected))


def test_no_beam():
    print('\n-- a run with no beam claims no latency --')
    n_trig = 20000
    events = [(5000, 'B0', 0.0), (10000, 'B1', 0.0), (15000, 'B0', 0.0)]
    truth = {5000: 5040, 10000: 10012, 15000: 15040}
    # beam_rate == noise_rate: the commands were sent, the detector saw nothing
    hits = synth_run(n_trig, events, truth, beam_rate=3.0, noise_rate=3.0)
    rows, levels = run_analysis(hits, events, n_trig)
    check('no plane is usable', rows is None,
          'levels %s' % [round(l[2], 2) for l in levels])


def test_no_settle():
    print('\n-- a B0 the beam ignores is reported, not guessed at --')
    # The middle B0 does nothing: the beam stays on until the B1 that follows
    # it, as a stuck relay would look. The other transitions behave, so the
    # noise and beam levels are still well measured.
    n_trig = 30000
    events = [(5000, 'B0', 0.0), (10000, 'B1', 0.0), (15000, 'B0', 0.0),
              (15500, 'B1', 0.0), (16000, 'B0', 0.0)]
    truth = {5000: 5040, 10000: 10012, 15000: 15500, 15500: 15500, 16000: 16040}
    hits = synth_run(n_trig, events, truth, beam_rate=20.0, noise_rate=3.0,
                     seed=3)
    rows, _ = run_analysis(hits, events, n_trig)
    check('measured the working transitions', rows is not None and
          rows[0]['latency'] is not None and rows[1]['latency'] is not None)
    if rows is None:
        return
    check('the ignored B0 is left blank',
          rows[2]['latency'] is None and bool(rows[2]['note']),
          'latency %s note %r' % (rows[2]['latency'], rows[2]['note']))
    check('the last B0 still measures 40 ms',
          rows[4]['latency'] is not None and abs(rows[4]['latency'] - 40) <= 1,
          'got %s' % rows[4]['latency'])


def test_never_changes():
    print('\n-- a stretch that never changes yields no changepoint --')
    rng = np.random.default_rng(11)
    beam_only = rng.poisson(20.0, 2000).astype(float)
    check('all-beam stretch after a B0',
          al.find_settle(beam_only, 0, 2000, 20.0, 3.0) is None)
    noise_only = rng.poisson(3.0, 2000).astype(float)
    check('all-noise stretch after a B1',
          al.find_settle(noise_only, 0, 2000, 3.0, 20.0) is None)
    check('a stretch shorter than the report window',
          al.find_settle(beam_only, 0, 5, 20.0, 3.0) is None)


def test_already_on():
    print('\n-- a command the beam was already obeying is not a fast response --')
    n_trig = 20000
    events = [(5000, 'B0', 0.0), (10000, 'B1', 0.0), (15000, 'B0', 0.0)]
    truth = {5000: 5040, 10000: 10012, 15000: 15040}
    hits = synth_run(n_trig, events, truth, beam_rate=20.0, noise_rate=3.0)
    # a second B0 while the beam is already off: latency would read ~0
    events.insert(1, (7000, 'B0', 0.0))
    rows, _ = run_analysis(hits, events, n_trig)
    stale = rows[1]
    check('flagged as already in the commanded state', stale['already'],
          'latency %s note %r' % (stale['latency'], stale['note']))
    check('the genuine transitions are not flagged',
          not rows[0]['already'] and not rows[2]['already'])


def test_csv_and_report(tmp='/tmp/claude-1000/test_latency_out'):
    print('\n-- report and CSV --')
    import csv
    import os
    n_trig = 20000
    events = [(5000, 'B0', 0.0), (10000, 'B1', 0.0), (15000, 'B0', 0.0)]
    truth = {5000: 5040, 10000: 10012, 15000: 15040}
    hits = synth_run(n_trig, events, truth, beam_rate=20.0, noise_rate=3.0)
    rows, _ = run_analysis(hits, events, n_trig)
    al.report_transitions(rows, 1.0)

    os.makedirs(tmp, exist_ok=True)
    path = al.write_csv(tmp, rows, 1.0)
    with open(path) as f:
        out = list(csv.DictReader(f))
    check('one CSV row per transition', len(out) == 3, 'got %d' % len(out))
    check('latencies survive the round trip',
          [r['latency_ms'] for r in out] == ['40.0', '12.0', '40.0'],
          'got %s' % [r['latency_ms'] for r in out])
    check('commanded trigger recorded',
          [int(r['trig_commanded']) for r in out] == [5000, 10000, 15000])


def test_intervals():
    print('\n-- beam state before the first command --')
    iv = al.beam_state_intervals([(100, 'B0', 0.0), (200, 'B1', 0.0)], 300)
    check('a leading B0 means the beam was on', iv[0] == (0, 100, True),
          'got %s' % (iv[0],))
    check('B0 turns it off', iv[1] == (100, 200, False), 'got %s' % (iv[1],))
    check('B1 turns it back on', iv[2] == (200, 300, True), 'got %s' % (iv[2],))

    iv = al.beam_state_intervals([(100, 'B1', 0.0)], 300)
    check('a leading B1 means the beam was off', iv[0] == (0, 100, False),
          'got %s' % (iv[0],))


if __name__ == '__main__':
    test_decoder()
    test_intervals()
    test_latency()
    test_latency_with_dropped_events()
    test_weak_beam()
    test_no_beam()
    test_no_settle()
    test_never_changes()
    test_already_on()
    test_csv_and_report()
    print()
    if FAILURES:
        print('%d failed: %s' % (len(FAILURES), ', '.join(FAILURES)))
        raise SystemExit(1)
    print('all checks passed')
