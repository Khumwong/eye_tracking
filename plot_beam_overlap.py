#!/usr/bin/env python3
"""Plot eye deviation and one ALPIDE plane's hit activity on one shared time
axis, so a beam-off transition can be read against both signals at once.

    python3 plot_beam_overlap.py output/session_<ts> [--plane N] [--bin-ms MS] [--out path.png]

Answers a different question than analyze_latency.py's own transition table:
that gives the number, this gives the picture — how the eye leaving the
target, the command going out, and the protons actually stopping line up (or
don't) as a shape in time, not just a millisecond count.

Needs a real .raw with decodable hits and a track.csv from the same run — a
no-beam or self-check-only session has nothing to put on the lower panel.

Reuses analyze_latency.py's own file readers rather than re-deriving them:
read_raw for the plane hit counts, read_beam_events for the trigger<->host-
clock bridge (the shared clock is the trigger pulse — see that module's own
notes on this), read_session for meta. This file only adds the plotting and
the continuous trigger->monotonic fit that turns event-level correlation into
something drawable as a line.
"""
import argparse
import csv
import glob
import os

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import numpy as np

import analyze_latency as al
import config


def trigger_to_monotonic_fit(events):
    """Least-squares fit of monotonic = a + b*trig_n from beam_events.csv's
    own (trig_n, monotonic) pairs.

    analyze_latency.py's own measure() matches trigger numbers to host events
    pointwise, one transition at a time; this fits the same relationship as a
    continuous function so every trigger in the run — not just the ones a
    transition happened to land on — can be placed on the host clock. Averaging
    over every row rather than anchoring on one absorbs the per-event jitter
    in host_monotonic (it is stamped when the app decided to send B0/B1, not a
    hardware timestamp), instead of inheriting whichever single event's own
    scheduling noise was largest.
    """
    trig = np.array([e[0] for e in events], dtype=float)
    mono = np.array([e[2] for e in events], dtype=float)
    b, a = np.polyfit(trig, mono, 1)
    return a, b


def read_track(folder):
    path = os.path.join(folder, 'track.csv')
    if not os.path.exists(path):
        raise SystemExit('no track.csv in %s' % folder)
    t, dev = [], []
    with open(path, newline='') as f:
        for r in csv.DictReader(f):
            try:
                t.append(float(r['t_capture']))
            except (KeyError, ValueError):
                continue
            dv = r.get('deviation_mm', '')
            dev.append(float(dv) if dv not in ('', 'inf') else np.nan)
    if not t:
        raise SystemExit('track.csv in %s has no usable t_capture rows' % folder)
    return np.array(t), np.array(dev)


def bin_hit_rate(trig_time, hits, bin_s):
    """Hits/second in fixed-width windows over trig_time.

    A single trigger's hit count is too sparse at 1 kHz to read as a line —
    binning is what turns it into a rate a beam-on plateau is visible in.
    """
    t0, t1 = trig_time.min(), trig_time.max()
    edges = np.arange(t0, t1 + bin_s, bin_s)
    if edges.size < 2:
        edges = np.array([t0, t0 + bin_s])
    bin_idx = np.clip(np.digitize(trig_time, edges) - 1, 0, len(edges) - 2)
    summed = np.zeros(len(edges) - 1)
    np.add.at(summed, bin_idx, hits)
    centers = edges[:-1] + bin_s / 2
    return centers, summed / bin_s


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('session', nargs='?', default=None,
                    help='session folder, output dir, or omit for newest')
    ap.add_argument('--plane', type=int, default=0,
                    help='plane device number to plot (default 0)')
    ap.add_argument('--bin-ms', type=float, default=50.0,
                    help='hit-rate bin width in ms (default 50)')
    ap.add_argument('--out', default=None,
                    help='PNG path (default <session>/plot_overlap.png)')
    args = ap.parse_args(argv)

    folder = al.find_session(args.session)
    meta = al.read_session(folder)
    if meta.get('alpide_error') is not None:
        raise SystemExit(
            "session has alpide_error=%r — no detector data, nothing for the "
            "lower panel" % meta['alpide_error'])

    events, skipped = al.read_beam_events(folder)
    if len(events) < 2:
        raise SystemExit(
            'fewer than 2 usable beam_events rows — not enough to fit the '
            'trigger<->clock bridge (need at least two B0/B1 transitions)')

    raws = sorted(glob.glob(os.path.join(folder, 'alpide', '*.raw')))
    if not raws:
        raise SystemExit('no .raw file in %s' % os.path.join(folder, 'alpide'))
    run = al.read_raw(raws[-1], progress=False)
    if run.hits is None:
        raise SystemExit('no decodable ALPIDE data in %s' % raws[-1])
    if args.plane not in run.planes:
        raise SystemExit('plane %d not in this run (planes present: %s)'
                         % (args.plane, run.planes))
    col = run.planes.index(args.plane)

    hz = meta.get('trigger_hz') or config.TRIGGER_HZ
    a, b = trigger_to_monotonic_fit(events)   # monotonic ≈ a + b * trig_n
    fit_ms_per_trig = b * 1000.0
    nominal_ms_per_trig = 1000.0 / hz
    drift_pct = 100.0 * abs(fit_ms_per_trig - nominal_ms_per_trig) / nominal_ms_per_trig

    t_track, dev = read_track(folder)

    n_trig = run.hits.shape[0]
    trig_idx = np.arange(n_trig)
    hits = np.nan_to_num(run.hits[:, col], nan=0.0)
    trig_time = a + b * trig_idx

    bin_s = args.bin_ms / 1000.0
    bin_centers, bin_rate = bin_hit_rate(trig_time, hits, bin_s)

    # Shared origin so the x-axis reads as elapsed seconds since whichever
    # signal started first, not a raw monotonic epoch nobody can parse by eye.
    t_origin = min(t_track.min(), trig_time.min())

    fig, (ax1, ax2) = plt.subplots(
        2, 1, figsize=(12, 7), sharex=True,
        gridspec_kw={'height_ratios': [1, 1]})

    ax1.plot(t_track - t_origin, dev, lw=0.8, color='#1f77b4')
    thr = meta.get('threshold_mm')
    if thr:
        ax1.axhline(thr, color='#d62728', ls='--', lw=1,
                    label='threshold %.1f mm' % thr)
        ax1.legend(loc='upper right', fontsize=8)
    ax1.set_ylabel('deviation (mm)')
    ax1.set_title('eye deviation vs plane %d hit rate — %s'
                  % (args.plane, os.path.basename(folder)))

    ax2.plot(bin_centers - t_origin, bin_rate, lw=0.8, color='#2ca02c')
    ax2.set_ylabel('plane %d hits/s (%.0f ms bins)' % (args.plane, args.bin_ms))
    ax2.set_xlabel('time (s)')

    for trig_n, ev, mono in events:
        x = mono - t_origin
        color = '#2ca02c' if ev == 'B1' else '#d62728'
        for ax in (ax1, ax2):
            ax.axvline(x, color=color, lw=0.8, alpha=0.5)

    out = args.out or os.path.join(folder, 'plot_overlap.png')
    fig.tight_layout()
    fig.savefig(out, dpi=130)
    plt.close(fig)

    print('wrote %s' % out)
    print('trigger<->clock fit: %.4f ms/trigger (nominal %.4f ms/trigger at %d Hz, %.2f%% off)'
         % (fit_ms_per_trig, nominal_ms_per_trig, hz, drift_pct))
    print('%d beam_events rows used for the fit (%d skipped, trig_running=0)'
         % (len(events), skipped))
    if drift_pct > 1.0:
        print('warning: fit is >1%% off the nominal trigger rate — the x-axis '
             'on the lower panel may be untrustworthy; check beam_events.csv '
             'for a bad row before reading the overlap off this plot')


if __name__ == '__main__':
    raise SystemExit(main())
