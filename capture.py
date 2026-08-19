import collections
import csv
from datetime import datetime
import cv2
import numpy as np
import os
import queue
import time
import threading

import config
import detection

try:
    import mediapipe as mp
    MEDIAPIPE_AVAILABLE = True
except (ImportError, AttributeError):
    MEDIAPIPE_AVAILABLE = False


class CaptureThread:
    """
    Runs in a background thread.
    Reads params from a plain dict (written only by the main thread) — no tkinter calls here.
    Communicates results back via frame_queue and root.after callbacks.
    """

    def __init__(self, cap, params, arduino, is_video, fps, total_frames,
                 frame_queue, pause_event, on_video_end, on_progress, armed=False):
        self.cap = cap
        self.params = params          # plain dict, main thread writes, we read
        self.arduino = arduino
        self.is_video = is_video
        self._fps = fps if fps > 0 else 30.0
        self._total = total_frames
        self.queue = frame_queue
        self.pause_event = pause_event
        self.on_video_end = on_video_end   # called via root.after — thread-safe
        self.on_progress = on_progress     # called via root.after — thread-safe

        # Gates whether a detection trigger may ever reach the Arduino relay.
        # False = preview only (picture + overlays run normally, beam forced
        # off) — lets Eye Selection/Threshold/Target Position be set live
        # before the operator ever arms tracking. Main thread flips this
        # directly (same pattern as debug_enabled below), no lock needed.
        self.armed = armed

        # Operator override: while latched, the beam stays off no matter what
        # the eye is doing. Same ownership rule as `armed` — main thread writes,
        # this thread reads, no lock. Mirrors Kcmh-Tricker's Kill beam toggle,
        # which is likewise a state the run cannot overrule rather than a
        # one-shot command.
        self.kill_latched = False
        # When the beam was last closed, for the minimum-off hold in
        # _gate_open. None means "not since this thread started", so the first
        # open of a run is never delayed.
        self._beam_off_since = None

        self.running = False
        self.current_state = 'S'
        self._seek = -1
        self._last_iris_px      = None
        self._last_iris_x       = None
        self._last_iris_y       = None
        self._last_deviation_mm = None
        # Survives blinks (unlike _last_iris_* above, which are cleared every
        # frame so a blink can't be mistaken for a real reading) — keeps the
        # eye-zoom crop anchored to the last real position instead of the
        # view flicking back to the wide frame on every blink.
        self._last_good_iris = None

        # Independent cross-check: same iris measurement, run on a grayscale
        # copy of the frame with its own FaceMesh pass, throttled (see
        # _GRAY_EVERY_N) since running detection twice per frame is expensive.
        # Purely observational — never read by the trigger/threshold/Arduino
        # path, only by the comparison readouts and the zoom_gray view.
        self._last_iris_px_gray      = None
        self._last_iris_x_gray       = None
        self._last_iris_y_gray       = None
        self._last_deviation_mm_gray = None
        self._last_good_iris_gray    = None
        self._frame_idx = 0

        self._strip_deque = collections.deque(maxlen=100)

        self.debug_enabled   = False
        self._debug_folder   = None
        self._debug_frame_idx = 0
        self._csv_file       = None
        self._csv_writer_obj = None

        self._recording = False
        self._writer = None
        self._ts_file = None
        self._rec_frame_idx = 0
        self._writer_lock = threading.Lock()
        self._thread = None

        # One snapshot per beam-off transition, written by a separate thread.
        # Encoding a 1080p JPEG costs 10-20 ms and this loop's period *is* the
        # beam's reaction time, so the detection loop only ever hands the frame
        # over — see _cut_writer.
        self._cut_queue   = None
        self._cut_thread  = None
        self._cut_dropped = 0
        self._cut_saved   = 0

        # Per-frame log for the whole armed window — every frame, not only the
        # ones a beam transition happens on. Written directly from this thread
        # (unlike cuts/, no queue+writer thread): there is no JPEG encode here,
        # just a short line, so the loop can afford to do it itself. See
        # start_track_log.
        self._track_file = None
        self._track_writer = None
        self._track_frame_idx = 0
        self._track_flush_every = 30    # ≈1 s at 30 fps; worst-case loss on a
                                        # crash, not a cost paid every frame

    # ── Public API (main thread only) ─────────────────────────

    def start(self):
        self.running = True
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def is_alive(self):
        """Whether the detection loop is still running. This class owns a
        thread rather than being one, so callers cannot rely on Thread's own
        is_alive — and a caller that assumed otherwise silently killed the UI
        refresh loop."""
        return bool(self._thread and self._thread.is_alive())

    def stop(self):
        self.running = False
        self.pause_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2.0)
        self._thread = None

    def seek_to(self, frame_num: int):
        self._seek = frame_num

    def start_recording(self, path: str):
        w = int(self.cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        h = int(self.cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        fourcc = cv2.VideoWriter_fourcc(*'mp4v')
        with self._writer_lock:
            self._writer = cv2.VideoWriter(path, fourcc, self._fps, (w, h))
            # The writer is told the camera's nominal rate, but frames arrive at
            # whatever rate detection manages — 27 fps against a claimed 50 in
            # one measured run, so the file plays back at nearly twice real
            # speed and its timestamps mean nothing. Rather than guess a better
            # number, log when each frame was actually written: correlating a
            # beam event with the picture then needs no fps at all.
            try:
                self._ts_file = open(os.path.splitext(path)[0] + '_frames.csv', 'w')
                self._ts_file.write('frame,host_time_iso,host_monotonic\n')
            except Exception:
                self._ts_file = None
            self._rec_frame_idx = 0
            self._recording = True

    def start_track_log(self, folder):
        """Write output/session_<ts>/track.csv for the whole armed window: one
        row per frame, not only the ones a beam transition happens on.

        This is the only record of where the eye was and how long deciding
        took on a run that never has protons flying through it and never will
        until this file exists — cuts.csv only fires on a beam-off transition,
        which says nothing about the run's steady state or how close a near
        miss came. See _gate_reason for how the `reason` column is derived
        without touching _gate_open itself.
        """
        self.stop_track_log()
        try:
            os.makedirs(folder, exist_ok=True)
            self._track_file = open(os.path.join(folder, 'track.csv'), 'w', newline='')
            self._track_writer = csv.writer(self._track_file)
            self._track_writer.writerow([
                'frame', 'host_time_iso', 't_capture', 't_decided',
                'deviation_mm', 'iris_px', 'detect', 'gate', 'beam_state',
                'reason'])
        except Exception:
            self._track_file = None
            self._track_writer = None
        self._track_frame_idx = 0

    def stop_track_log(self):
        f, self._track_file = self._track_file, None
        self._track_writer = None
        if f is not None:
            try:
                f.close()
            except Exception:
                pass

    def _gate_reason(self, trigger, gate_open):
        """Why the gate is or isn't open this frame, for track.csv only.

        Derived from the same inputs _gate_open already used, never from a
        second read of live state — this must not become a second place the
        beam decision is made. Empty string when the gate is open.
        """
        if gate_open:
            return ''
        if not self.armed:
            return 'unarmed'
        if self.kill_latched:
            return 'kill'
        if trigger is None:
            return 'no_face'
        if trigger is False:
            return 'blink' if self._last_deviation_mm is None else 'deviation'
        # trigger is True but the gate is still shut: only the min-off hold
        # can do that (see _gate_open) — anything else here would mean the two
        # methods have drifted apart.
        return 'hold'

    def _write_track_row(self, t_capture, trigger, armed_trigger):
        """Append one row to track.csv, if logging is on. Called from the
        detection loop every frame, so — like _queue_cut — it must never do
        real work: no encode, no I/O beyond a buffered write, flushed only
        every _track_flush_every rows. t_decided is stamped here, which is
        after any relay command for this frame has already gone out, so the
        row describes what just happened rather than predicting it.
        """
        if self._track_writer is None:
            return
        t_decided = time.monotonic()
        try:
            self._track_writer.writerow([
                self._track_frame_idx, datetime.now().isoformat(),
                f'{t_capture:.6f}', f'{t_decided:.6f}',
                '' if self._last_deviation_mm is None
                    else f'{self._last_deviation_mm:.4f}',
                '' if self._last_iris_px is None
                    else f'{self._last_iris_px:.2f}',
                '' if trigger is None else int(trigger),
                int(armed_trigger), self.current_state,
                self._gate_reason(trigger, armed_trigger)])
            self._track_frame_idx += 1
            if self._track_frame_idx % self._track_flush_every == 0:
                self._track_file.flush()
        except Exception:
            pass

    def start_cut_capture(self, folder):
        """Save the screen at every beam-off transition into `folder`.

        Tied to arming rather than to the RECORD button on purpose: five of the
        first eight sessions recorded no video at all because nobody pressed it,
        and those are exactly the runs where a beam cut left no picture of what
        the eye was doing.
        """
        self.stop_cut_capture()
        self._cut_dropped = 0
        self._cut_saved = 0
        self._cut_queue = queue.Queue(maxsize=config.CUT_QUEUE_MAX)
        self._cut_thread = threading.Thread(
            target=self._cut_writer, args=(folder, self._cut_queue), daemon=True)
        self._cut_thread.start()

    def stop_cut_capture(self):
        q, self._cut_queue = self._cut_queue, None
        if q is not None:
            q.put(None)                      # sentinel: flush and close the CSV
        t, self._cut_thread = self._cut_thread, None
        if t is not None:
            t.join(timeout=3.0)

    def _queue_cut(self, frame, trigger, gate_open):
        """Hand one frame to the writer. Called from the detection loop, so it
        must never block: a full queue drops the frame instead of stalling the
        decision that turns the beam off.

        The frame is passed by reference, not copied. `_compose_display` builds
        a fresh array every iteration and the two consumers downstream only read
        it — the video writer works from `raw`, and the UI resizes into a new
        array. Copying it here measured 4.4 ms of the detection loop's budget
        for no benefit; if anything downstream ever starts drawing into the
        displayed frame, this needs a copy again.

        `gate_open` (this frame's `_gate_open()` result — always False here,
        since this only fires on an on-to-off transition) goes to
        `_gate_reason()` for the label. A real run caught the bug in reasoning
        from trigger/deviation alone: STOP disarms mid-run while the eye
        happens to be on target, and the cut this fires for got labelled
        'deviation' with deviation_mm well under threshold — the beam went off
        because armed became False, nothing to do with the eye. _gate_reason()
        checks armed and kill_latched first, the same order _gate_open() does,
        so this and track.csv's `reason` column can never disagree again.
        """
        q = self._cut_queue
        if q is None:
            return
        meta = {
            'iso':       datetime.now().isoformat(),
            'monotonic': time.monotonic(),
            'deviation': self._last_deviation_mm,
            'threshold': self.params.get('threshold_mm', config.DEFAULT_THRESHOLD_MM),
            'reason':    self._gate_reason(trigger, gate_open),
        }
        try:
            q.put_nowait((frame, meta))
        except queue.Full:
            self._cut_dropped += 1

    def _cut_writer(self, folder, q):
        """Encode and index the snapshots, off the detection loop."""
        index = csv_file = writer = None
        try:
            os.makedirs(folder, exist_ok=True)
            csv_file = open(os.path.join(folder, 'cuts.csv'), 'w', newline='')
            writer = csv.writer(csv_file)
            # host_monotonic is the same clock as beam_events.csv and the video
            # frame log, so a snapshot joins straight onto the trigger pulse
            # count and from there onto the ALPIDE data.
            writer.writerow(['index', 'host_time_iso', 'host_monotonic',
                             'deviation_mm', 'threshold_mm', 'reason', 'file'])
            csv_file.flush()
            index = 0
        except Exception:
            csv_file = None

        while True:
            item = q.get()
            if item is None:
                break
            if csv_file is None or index >= config.CUT_MAX_PER_SESSION:
                continue
            frame, meta = item
            index += 1
            name = f'cut_{index:04d}.jpg'
            try:
                cv2.imwrite(os.path.join(folder, name), frame,
                            [cv2.IMWRITE_JPEG_QUALITY, config.CUT_JPEG_QUALITY])
                writer.writerow([
                    index, meta['iso'], f"{meta['monotonic']:.6f}",
                    '' if meta['deviation'] is None else f"{meta['deviation']:.4f}",
                    f"{meta['threshold']:.2f}", meta['reason'], name])
                csv_file.flush()
                self._cut_saved = index
            except Exception:
                pass
            if index >= config.CUT_MAX_PER_SESSION:
                # Say so in the file itself: a truncated set of snapshots that
                # does not admit it is worse than none.
                try:
                    writer.writerow(['', '', '', '', '', 'limit_reached',
                                     f'stopped after {index} snapshots'])
                    csv_file.flush()
                except Exception:
                    pass

        if csv_file is not None:
            if self._cut_dropped:
                try:
                    writer.writerow(['', '', '', '', '', 'dropped',
                                     f'{self._cut_dropped} frame(s) not written'])
                except Exception:
                    pass
            try:
                csv_file.close()
            except Exception:
                pass

    def stop_recording(self):
        self._recording = False
        with self._writer_lock:
            if self._writer:
                self._writer.release()
                self._writer = None
            if self._ts_file:
                try:
                    self._ts_file.close()
                except Exception:
                    pass
                self._ts_file = None

    # ── Internal loop ─────────────────────────────────────────

    _GRAY_EVERY_N = 3   # throttle: run the grayscale cross-check every Nth frame
    _BEAM_REFRESH_N = 10  # re-send the unchanged beam state every Nth frame

    def _loop(self):
        face_mesh = None
        face_mesh_gray = None
        if MEDIAPIPE_AVAILABLE:
            face_mesh = mp.solutions.face_mesh.FaceMesh(
                max_num_faces=1, refine_landmarks=True,
                min_detection_confidence=0.5, min_tracking_confidence=0.5)
            face_mesh_gray = mp.solutions.face_mesh.FaceMesh(
                max_num_faces=1, refine_landmarks=True,
                min_detection_confidence=0.5, min_tracking_confidence=0.5)

        frame_delay = 1.0 / self._fps

        try:
            while self.running:
                self.pause_event.wait()
                if not self.running:
                    break

                if self._seek >= 0:
                    self.cap.set(cv2.CAP_PROP_POS_FRAMES, self._seek)
                    self._seek = -1

                t0 = time.time()
                ret, frame = self.cap.read()
                # The moment the frame is actually in hand — start of the part
                # of the latency chain a track.csv row can measure. Everything
                # before this (sensor exposure, USB transfer) never reaches
                # software and needs a hardware timestamp (an LED the Arduino
                # lights and stamps itself) to measure at all — see next.md.
                t_capture = time.monotonic()

                if not ret:
                    if self.is_video and self.params.get('loop', True):
                        self.cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
                        continue
                    self.on_video_end()
                    break

                if not self.is_video:
                    frame = cv2.flip(frame, 1)

                raw = frame.copy()

                cx = self.params.get('cx', config.DEFAULT_CENTER[0])
                cy = self.params.get('cy', config.DEFAULT_CENTER[1])
                center = (cx, cy)

                self._last_iris_px      = None
                self._last_iris_x       = None
                self._last_iris_y       = None
                self._last_deviation_mm = None
                if face_mesh:
                    if self.params.get('detect_method') == 'facemesh_pupil':
                        result = self._detect_mp_pupil(frame, center, face_mesh)
                    else:
                        result = self._detect_mp(frame, center, face_mesh)
                else:
                    result = None
                trigger = result is True   # None (no face) or False (blink) → beam off
                self._strip_deque.append(self._last_deviation_mm)

                # independent grayscale cross-check, throttled — never touches
                # trigger/threshold/Arduino, only the comparison readouts below
                gray_bgr = cv2.cvtColor(cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY), cv2.COLOR_GRAY2BGR)
                self._frame_idx += 1
                if (face_mesh_gray and not self.armed
                        and self._frame_idx % self._GRAY_EVERY_N == 0):
                    # Skipped entirely while armed. It costs 27 ms on a 1080p
                    # frame and never touches the trigger, so during a run it
                    # buys nothing and takes 15% of the decision budget — worse,
                    # running only every third frame makes the gating period
                    # irregular, which is exactly what a latency measurement
                    # must not have. Still runs during preview, where comparing
                    # the two readings is the point.
                    self._detect_gray_iris(gray_bgr, face_mesh_gray, center)

                # debug CSV + crops
                self._handle_debug(raw, trigger)

                frame, view_meta = self._compose_display(frame, gray_bgr, center, trigger)

                armed_trigger = self._gate_open(trigger)
                if armed_trigger and self.current_state != 'O':
                    self.arduino.send(b'B1\n')
                    self.current_state = 'O'
                elif not armed_trigger and self.current_state != 'S':
                    self.arduino.send(b'B0\n')
                    self.current_state = 'S'
                    self._beam_off_since = time.monotonic()
                    # After the relay command, never before it: the snapshot is
                    # evidence about the beam cut, not part of causing it. Only
                    # on the transition — the periodic refresh below re-states a
                    # decision that already happened.
                    self._queue_cut(frame, trigger, armed_trigger)
                elif self._frame_idx % self._BEAM_REFRESH_N == 0:
                    # Re-state the unchanged decision periodically. On-change
                    # alone can go silent for minutes while the eye holds still,
                    # which the Arduino watchdog would read as a dead host — and
                    # if it ever did trip, this is what restores the relay to
                    # the current decision instead of leaving it stale.
                    self.arduino.send(b'B1\n' if armed_trigger else b'B0\n')

                # t_decided is stamped here, after any relay command for this
                # frame has already been sent — the row describes what just
                # happened, the same rule _queue_cut follows for cuts.csv.
                # t_decided - t_capture is the whole detect+decide cost per
                # frame, still on the clock the beam commands were sent on.
                self._write_track_row(t_capture, trigger, armed_trigger)

                if self.is_video and self._total > 0 and self._seek < 0:
                    cur = int(self.cap.get(cv2.CAP_PROP_POS_FRAMES))
                    self.on_progress(
                        (cur / self._total) * 100.0,
                        cur / self._fps,
                        self._total / self._fps)

                if self._recording:
                    with self._writer_lock:
                        if self._writer:
                            self._writer.write(raw)
                            if self._ts_file:
                                try:
                                    self._ts_file.write(
                                        f'{self._rec_frame_idx},'
                                        f'{datetime.now().isoformat()},'
                                        f'{time.monotonic():.6f}\n')
                                except Exception:
                                    pass
                            self._rec_frame_idx += 1

                if not self.queue.full():
                    metrics = {
                        'iris_px':      self._last_iris_px,
                        'iris_x':       self._last_iris_x,
                        'iris_y':       self._last_iris_y,
                        'deviation_mm': self._last_deviation_mm,
                        'iris_px_gray':      self._last_iris_px_gray,
                        'deviation_mm_gray': self._last_deviation_mm_gray,
                        'view_meta':    view_meta,
                    }
                    try:
                        self.queue.put_nowait(
                            (frame, self.current_state, metrics))
                    except queue.Full:
                        # Drop the frame rather than wait for the UI. A blocking
                        # put throttles this loop to the display rate, and this
                        # loop is what decides the beam — measured at 15 fps
                        # (65 ms per decision) while the renderer was the
                        # bottleneck. Display smoothness must never buy itself
                        # time out of the gating latency budget.
                        pass

                if self.is_video:
                    dt = (frame_delay / self.params.get('speed', 1.0)) - (time.time() - t0)
                    if dt > 0:
                        time.sleep(dt)
        finally:
            if face_mesh:
                face_mesh.close()
            if face_mesh_gray:
                face_mesh_gray.close()
            if self._csv_file:
                self._csv_file.close()
                self._csv_file = None

    # ── Target overlay ────────────────────────────────────────

    def _draw_target(self, frame, center, trigger):
        """Fixed crosshair at the target point + a threshold circle sized
        from the live iris measurement — so the drawn circle always matches
        the real accept radius, instead of an arbitrary fixed pixel radius."""
        cx, cy = center
        s = config.TARGET_MARK_PX
        cv2.line(frame, (cx - s, cy), (cx + s, cy), (0, 255, 0), 1)
        cv2.line(frame, (cx, cy - s), (cx, cy + s), (0, 255, 0), 1)

        if self._last_iris_px:
            thr_mm = self.params.get('threshold_mm', config.DEFAULT_THRESHOLD_MM)
            r_px = int(thr_mm * self._last_iris_px / config.IRIS_MM)
            color = (50, 200, 50) if trigger else (80, 80, 220)
            cv2.circle(frame, center, max(r_px, 2), color, 1)

    _ZOOM_FACTOR = 3.0
    _INSET_W = 190
    _INSET_LABELS = {'wide': 'FULL VIEW', 'zoom_color': 'EYE ZOOM',
                      'zoom_gray': 'EYE ZOOM B/W'}

    def _zoom_crop_bbox(self, fw, fh):
        """A crop around the last well-tracked iris (survives blinks — see
        _last_good_iris) sized fw/factor x fh/factor — same aspect ratio as
        the full frame, so stretching it back up to fw x fh never distorts
        the picture. Shifted (not shrunk) to stay in bounds so the zoom
        level stays constant even near the frame edges."""
        ix, iy, _ir = self._last_good_iris
        cw = min(max(int(fw / self._ZOOM_FACTOR), 40), fw)
        ch = min(max(int(fh / self._ZOOM_FACTOR), 40), fh)
        x1 = min(max(0, ix - cw // 2), fw - cw)
        y1 = min(max(0, iy - ch // 2), fh - ch)
        return x1, y1, x1 + cw, y1 + ch

    def _render_zoom(self, src, center, trigger, crop, marker):
        """src cropped to `crop` and stretched back up to src's own full
        size, with the target crosshair/threshold-circle and (optionally) a
        detected-position marker drawn in the zoomed coordinate space.
        marker is (x, y, radius) in ORIGINAL frame coordinates, or None to
        skip the dot (e.g. no live reading this frame)."""
        fh, fw = src.shape[:2]
        x1, y1, x2, y2 = crop
        scale = fw / (x2 - x1)
        out = cv2.resize(src[y1:y2, x1:x2], (fw, fh), interpolation=cv2.INTER_CUBIC)

        def to_zoom(px, py):
            return int((px - x1) * scale), int((py - y1) * scale)

        zcx, zcy = to_zoom(*center)
        s = config.TARGET_MARK_PX
        cv2.line(out, (zcx - s, zcy), (zcx + s, zcy), (0, 255, 0), 1)
        cv2.line(out, (zcx, zcy - s), (zcx, zcy + s), (0, 255, 0), 1)
        if self._last_iris_px:
            # always sized from the *color* reading — this is the real accept
            # radius used for the actual beam trigger, same in every view
            thr_mm = self.params.get('threshold_mm', config.DEFAULT_THRESHOLD_MM)
            r_px = int(thr_mm * self._last_iris_px / config.IRIS_MM * scale)
            color = (50, 200, 50) if trigger else (80, 80, 220)
            cv2.circle(out, (zcx, zcy), max(r_px, 2), color, 1)
        if marker:
            mx, my, mr = marker
            zmx, zmy = to_zoom(mx, my)
            zmr = max(int(mr * scale), 1)
            cv2.circle(out, (zmx, zmy), zmr, (0, 0, 255), 2)
            cv2.circle(out, (zmx, zmy), config.DETECTED_MARK_PX, (0, 255, 255), -1)
        return out

    def _render_inset(self, view_frame, label):
        """Shrink a full-size rendered view down to a small labelled thumbnail."""
        fh, fw = view_frame.shape[:2]
        iw = self._INSET_W
        ih = max(int(iw * fh / fw), 1)
        if fw < iw + 24 or fh < ih + 24:
            return None
        thumb = cv2.resize(view_frame, (iw, ih), interpolation=cv2.INTER_LINEAR)
        cv2.putText(thumb, label, (6, 14),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.32, (144, 224, 239), 1)
        return thumb

    def _compose_display(self, frame, gray_bgr, center, trigger):
        """Build the frame that actually gets displayed: one of {wide,
        zoom_color, zoom_gray} as the big main view (params['main_view']),
        the other two as small clickable insets stacked in the corner —
        clicking an inset (in the UI) promotes it to main; clicking the main
        view itself only adjusts the target, it never changes the view.
        Returns (frame_to_show, view_meta) — view_meta carries the crop/scale
        needed to map a main-view click back to a real frame position, plus
        each inset's on-screen rect."""
        fh, fw = frame.shape[:2]
        has_anchor = self._last_good_iris is not None

        wide = frame.copy()
        self._draw_target(wide, center, trigger)
        views = {'wide': wide}

        crop = None
        if has_anchor:
            crop = self._zoom_crop_bbox(fw, fh)
            color_marker = ((self._last_iris_x, self._last_iris_y, self._last_iris_px / 2)
                             if self._last_iris_x is not None else None)
            gray_marker = ((self._last_iris_x_gray, self._last_iris_y_gray,
                             self._last_iris_px_gray / 2)
                            if self._last_iris_x_gray is not None else None)
            views['zoom_color'] = self._render_zoom(frame, center, trigger, crop, color_marker)
            views['zoom_gray']  = self._render_zoom(gray_bgr, center, trigger, crop, gray_marker)

        requested = self.params.get('main_view', 'wide')
        main_view = requested if requested in views else 'wide'
        main = views[main_view]

        insets = []
        oy = fh - 12
        for vid in ('wide', 'zoom_color', 'zoom_gray'):
            if vid == main_view or vid not in views:
                continue
            thumb = self._render_inset(views[vid], self._INSET_LABELS[vid])
            if thumb is None:
                continue
            ih, iw = thumb.shape[:2]
            oy -= ih
            ox = fw - iw - 12
            cv2.rectangle(main, (ox - 2, oy - 2), (ox + iw + 2, oy + ih + 2),
                          (182, 119, 0), 2)
            main[oy:oy + ih, ox:ox + iw] = thumb
            insets.append({'view': vid, 'rect': (ox, oy, ox + iw, oy + ih)})
            oy -= 12

        self._draw_strip_chart(main)   # drawn last so it's visible in every view

        view_meta = {'main_view': main_view, 'insets': insets, 'crop': None, 'scale': None}
        if main_view != 'wide' and crop is not None:
            x1, y1, x2, y2 = crop
            view_meta['crop'] = (x1, y1, x2, y2)
            view_meta['scale'] = fw / (x2 - x1)
        return main, view_meta

    # ── Detection ─────────────────────────────────────────────

    def _gate_open(self, trigger, now=None):
        """Whether the beam may be on this frame — the whole beam decision.

        Kept as one named method rather than an expression inside the loop so
        that every condition the beam depends on is in one greppable place and
        can be tested without a camera. `trigger` is the detection result: True
        on target, False off target or blinking, None when no face was found.

        The minimum-off hold is applied here and only here, and only to opening:
        every path that closes the beam stays immediate. An eye resting exactly
        on the threshold would otherwise chatter the shutter many times a
        second, which leaves too little between transitions to time either one.

        It is a trade, not a fix: holding longer buys fewer relay cycles at the
        cost of beam duty cycle, measured at 92% with no hold down to 14% at a
        5 s hold. The dose still has to reach the same MU either way, so a low
        duty cycle is paid for in how long the patient is held in position. The
        right value is a clinical judgement — see next.md.
        """
        if not (trigger and self.armed and not self.kill_latched):
            return False
        hold = self.params.get('min_off_s', config.DEFAULT_MIN_OFF_S)
        if hold and self._beam_off_since is not None:
            if (now or time.monotonic()) - self._beam_off_since < hold:
                return False
        return True

    def _within_threshold(self, ex, ey, ir, center):
        """Return (triggered, deviation_mm). Uses iris size to convert px → mm."""
        d_mm = detection.deviation_mm(ex, ey, ir, center)
        self._last_deviation_mm = d_mm
        thr = self.params.get('threshold_mm', config.DEFAULT_THRESHOLD_MM)
        return d_mm <= thr, d_mm

    def _detect_mp_pupil(self, frame, center, face_mesh):
        """FaceMesh for ROI + dark pupil detection for precise center."""
        results = face_mesh.process(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
        if not results.multi_face_landmarks:
            self._last_good_iris = None
            return None
        h, w = frame.shape[:2]
        side = self.params.get('side', 'left')
        for face in results.multi_face_landmarks:
            if detection.eye_aspect_ratio(face, h, w, side) < config.EAR_BLINK_THRESHOLD:
                return False
            ix, iy, ir = detection.iris_from_landmarks(face, h, w, side)
            self._last_iris_px = ir * 2
            self._last_iris_x  = ix
            self._last_iris_y  = iy
            self._last_good_iris = (ix, iy, ir)
            pad = int(ir * 1.5)
            x1, y1 = max(0, ix - pad), max(0, iy - pad)
            x2, y2 = min(w, ix + pad), min(h, iy + pad)
            roi = frame[y1:y2, x1:x2]
            px_r, py_r, pr_r = detection.pupil_in_roi(roi) if roi.size > 0 else (None, None, None)
            if px_r is not None:
                px, py = x1 + px_r, y1 + py_r
                cv2.circle(frame, (ix, iy), ir,   (100, 100, 255), 1)
                cv2.circle(frame, (px, py), pr_r,  (0,   0,   255), 2)
                cv2.circle(frame, (px, py), config.DETECTED_MARK_PX, (0, 255, 255), -1)
                ok, _ = self._within_threshold(px, py, ir, center)
            else:
                cv2.circle(frame, (ix, iy), ir, (0, 0, 255), 2)
                cv2.circle(frame, (ix, iy), config.DETECTED_MARK_PX, (0, 255, 255), -1)
                ok, _ = self._within_threshold(ix, iy, ir, center)
            if ok:
                return True
        return False

    def _detect_mp(self, frame, center, face_mesh):
        results = face_mesh.process(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
        if not results.multi_face_landmarks:
            self._last_good_iris = None
            return None  # no face → beam off
        h, w = frame.shape[:2]
        side = self.params.get('side', 'left')
        for face in results.multi_face_landmarks:
            if detection.eye_aspect_ratio(face, h, w, side) < config.EAR_BLINK_THRESHOLD:
                return False
            ix, iy, ir = detection.iris_from_landmarks(face, h, w, side)
            self._last_iris_px = ir * 2
            self._last_iris_x  = ix
            self._last_iris_y  = iy
            self._last_good_iris = (ix, iy, ir)
            cv2.circle(frame, (ix, iy), ir, (0, 0, 255), 2)
            cv2.circle(frame, (ix, iy), config.DETECTED_MARK_PX, (0, 255, 255), -1)
            ok, _ = self._within_threshold(ix, iy, ir, center)
            if ok:
                return True
        return False

    def _detect_gray_iris(self, gray_bgr, face_mesh_gray, center):
        """Independent cross-check: the same iris measurement, run on a
        grayscale copy of this same frame with its own FaceMesh instance.
        Deliberately the simple 'facemesh' reading (not the pupil-refined
        variant) — it's a second opinion for the operator to compare against,
        not a replacement for the primary detection path. Never touches
        trigger/threshold/Arduino state, only _last_*_gray for display."""
        results = face_mesh_gray.process(cv2.cvtColor(gray_bgr, cv2.COLOR_BGR2RGB))
        if not results.multi_face_landmarks:
            self._last_good_iris_gray    = None
            self._last_iris_px_gray      = None
            self._last_iris_x_gray       = None
            self._last_iris_y_gray       = None
            self._last_deviation_mm_gray = None
            return
        h, w = gray_bgr.shape[:2]
        side = self.params.get('side', 'left')
        face = results.multi_face_landmarks[0]
        if detection.eye_aspect_ratio(face, h, w, side) < config.EAR_BLINK_THRESHOLD:
            self._last_iris_px_gray      = None
            self._last_iris_x_gray       = None
            self._last_iris_y_gray       = None
            self._last_deviation_mm_gray = None
            return
        ix, iy, ir = detection.iris_from_landmarks(face, h, w, side)
        self._last_iris_px_gray      = ir * 2
        self._last_iris_x_gray       = ix
        self._last_iris_y_gray       = iy
        self._last_good_iris_gray    = (ix, iy, ir)
        self._last_deviation_mm_gray = detection.deviation_mm(ix, iy, ir, center)

    def _draw_strip_chart(self, frame):
        vals = list(self._strip_deque)
        if not vals:
            return
        fh, fw = frame.shape[:2]
        CW, CH = 200, 60
        x0, y0 = 8, fh - CH - 8
        sub = frame[y0:y0+CH, x0:x0+CW]
        np.multiply(sub, 0.35, out=sub, casting='unsafe')
        frame[y0:y0+CH, x0:x0+CW] = sub

        thr   = self.params.get('threshold_mm', config.DEFAULT_THRESHOLD_MM)
        y_max = max(thr * 2, 6.0)
        thr_y = y0 + CH - 2 - int((thr / y_max) * (CH - 4))
        cv2.line(frame, (x0, thr_y), (x0 + CW, thr_y), (80, 80, 200), 1)

        n = len(vals)
        pts = []
        for i, v in enumerate(vals):
            if v is None:
                continue
            px = x0 + int(i / max(n - 1, 1) * (CW - 1))
            py = y0 + CH - 2 - int(min(v, y_max) / y_max * (CH - 4))
            pts.append((px, py, v))
        for k in range(1, len(pts)):
            x1, y1, _ = pts[k-1]
            x2, y2, v = pts[k]
            color = (50, 200, 50) if v <= thr else (80, 80, 220)
            cv2.line(frame, (x1, y1), (x2, y2), color, 1)

        last_v = next((v for _, _, v in reversed(pts)), None)
        if last_v is not None:
            col = (50, 220, 50) if last_v <= thr else (80, 80, 220)
            cv2.putText(frame, f"{last_v:.2f}mm",
                        (x0 + 3, y0 + 13), cv2.FONT_HERSHEY_SIMPLEX, 0.38, col, 1)
        cv2.putText(frame, f"thr:{thr:.1f}",
                    (x0 + 3, y0 + 25), cv2.FONT_HERSHEY_SIMPLEX, 0.32, (120, 120, 200), 1)

    def _handle_debug(self, raw, trigger):
        if not self.debug_enabled or self._debug_folder is None:
            if self._csv_file is not None:
                self._csv_file.close()
                self._csv_file = None
                self._csv_writer_obj = None
            return

        if self._csv_file is None:
            csv_path = os.path.join(self._debug_folder, 'log.csv')
            self._csv_file = open(csv_path, 'w', newline='')
            self._csv_writer_obj = csv.writer(self._csv_file)
            self._csv_writer_obj.writerow(
                ['frame', 'timestamp', 'iris_x', 'iris_y', 'iris_px',
                 'deviation_mm', 'triggered'])

        self._csv_writer_obj.writerow([
            self._debug_frame_idx,
            f"{time.time():.4f}",
            self._last_iris_x  if self._last_iris_x  is not None else '',
            self._last_iris_y  if self._last_iris_y  is not None else '',
            self._last_iris_px if self._last_iris_px is not None else '',
            f"{self._last_deviation_mm:.4f}" if self._last_deviation_mm is not None else '',
            1 if trigger else 0,
        ])

        if self._last_iris_x is not None and self._debug_frame_idx % 3 == 0:
            self._save_debug_crop(raw, self._debug_frame_idx)

        self._debug_frame_idx += 1

    def _save_debug_crop(self, raw, frame_idx):
        ix, iy = self._last_iris_x, self._last_iris_y
        ir  = (self._last_iris_px // 2) if self._last_iris_px else 20
        pad = max(int(ir * 2.5), 30)
        fh, fw = raw.shape[:2]
        x1, y1 = max(0, ix - pad), max(0, iy - pad)
        x2, y2 = min(fw, ix + pad), min(fh, iy + pad)
        crop = raw[y1:y2, x1:x2].copy()
        if crop.size == 0:
            return
        cx_r, cy_r = ix - x1, iy - y1
        cv2.circle(crop, (cx_r, cy_r), ir, (0, 0, 255), 1)
        cv2.circle(crop, (cx_r, cy_r), 2, (0, 255, 255), -1)
        if self._last_deviation_mm is not None:
            cv2.putText(crop, f"{self._last_deviation_mm:.2f}mm",
                        (4, 16), cv2.FONT_HERSHEY_SIMPLEX, 0.45, (0, 255, 255), 1)
        path = os.path.join(self._debug_folder, f"crop_{frame_idx:05d}.jpg")
        cv2.imwrite(path, crop, [cv2.IMWRITE_JPEG_QUALITY, 85])
