import collections
import csv
import cv2
import numpy as np
import os
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
                 frame_queue, pause_event, on_video_end, on_progress):
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

        self.running = False
        self.current_state = 'S'
        self._seek = -1
        self._last_iris_px      = None
        self._last_iris_x       = None
        self._last_iris_y       = None
        self._last_deviation_mm = None

        self._strip_deque = collections.deque(maxlen=100)

        self.debug_enabled   = False
        self._debug_folder   = None
        self._debug_frame_idx = 0
        self._csv_file       = None
        self._csv_writer_obj = None

        self._recording = False
        self._writer = None
        self._writer_lock = threading.Lock()
        self._thread = None

    # ── Public API (main thread only) ─────────────────────────

    def start(self):
        self.running = True
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

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
            self._recording = True

    def stop_recording(self):
        self._recording = False
        with self._writer_lock:
            if self._writer:
                self._writer.release()
                self._writer = None

    # ── Internal loop ─────────────────────────────────────────

    def _loop(self):
        face_mesh = None
        if MEDIAPIPE_AVAILABLE:
            face_mesh = mp.solutions.face_mesh.FaceMesh(
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

                # strip chart (always visible)
                self._strip_deque.append(self._last_deviation_mm)
                self._draw_strip_chart(frame)

                # debug CSV + crops
                self._handle_debug(raw, trigger)

                self._draw_target(frame, center, trigger)

                if trigger and self.current_state != 'O':
                    self.arduino.send(b'B1\n')
                    self.current_state = 'O'
                elif not trigger and self.current_state != 'S':
                    self.arduino.send(b'B0\n')
                    self.current_state = 'S'


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

                if not self.queue.full():
                    metrics = {
                        'iris_px':      self._last_iris_px,
                        'iris_x':       self._last_iris_x,
                        'iris_y':       self._last_iris_y,
                        'deviation_mm': self._last_deviation_mm,
                    }
                    self.queue.put((frame, self.current_state, metrics))

                if self.is_video:
                    dt = (frame_delay / self.params.get('speed', 1.0)) - (time.time() - t0)
                    if dt > 0:
                        time.sleep(dt)
        finally:
            if face_mesh:
                face_mesh.close()
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

    # ── Detection ─────────────────────────────────────────────

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
            cv2.circle(frame, (ix, iy), ir, (0, 0, 255), 2)
            cv2.circle(frame, (ix, iy), config.DETECTED_MARK_PX, (0, 255, 255), -1)
            ok, _ = self._within_threshold(ix, iy, ir, center)
            if ok:
                return True
        return False

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
