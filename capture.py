import cv2
import numpy as np
import math
import time
import threading

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

                frame = self._zoom(frame)
                raw = frame.copy()

                r  = self.params.get('radius', 50)
                cx = self.params.get('cx', 320)
                cy = self.params.get('cy', 240)
                center = (cx, cy)

                if face_mesh:
                    trigger = self._detect_mp(frame, center, r, face_mesh)
                else:
                    trigger = self._detect_hough(frame, center, r)

                cv2.circle(frame, center, r, (0, 255, 0), 2)

                if trigger and self.current_state != 'O':
                    self.arduino.send(b'B1\n')
                    self.current_state = 'O'
                elif not trigger and self.current_state != 'S':
                    self.arduino.send(b'B0\n')
                    self.current_state = 'S'

                label = "Open the shutter!" if self.current_state == 'O' else "Searching..."
                col   = (0, 0, 255) if self.current_state == 'O' else (0, 200, 0)
                cv2.putText(frame, label, (10, 30), cv2.FONT_HERSHEY_SIMPLEX, 1, col, 2)

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
                    self.queue.put((frame, self.current_state))

                if self.is_video:
                    dt = (frame_delay / self.params.get('speed', 1.0)) - (time.time() - t0)
                    if dt > 0:
                        time.sleep(dt)
        finally:
            if face_mesh:
                face_mesh.close()

    # ── Zoom ──────────────────────────────────────────────────

    def _zoom(self, frame):
        z = self.params.get('zoom', 1.0)
        if z <= 1.0:
            return frame
        h, w = frame.shape[:2]
        nh, nw = int(h / z), int(w / z)
        ox = self.params.get('zoom_ox', 0)
        oy = self.params.get('zoom_oy', 0)
        x1 = max(0, min(w - nw, w // 2 + ox - nw // 2))
        y1 = max(0, min(h - nh, h // 2 + oy - nh // 2))
        return cv2.resize(frame[y1:y1+nh, x1:x1+nw], (w, h),
                          interpolation=cv2.INTER_LINEAR)

    # ── Detection ─────────────────────────────────────────────

    def _detect_hough(self, frame, center, radius):
        blurred = cv2.GaussianBlur(
            cv2.bitwise_not(cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)),
            (9, 9), 0)
        circles = cv2.HoughCircles(blurred, cv2.HOUGH_GRADIENT,
                                   dp=1.2, minDist=50, param1=50, param2=30,
                                   minRadius=10, maxRadius=20)
        if circles is not None:
            for x, y, r in np.round(circles[0]).astype(int):
                cv2.circle(frame, (x, y), r, (0, 0, 255), 2)
                if (x - center[0])**2 + (y - center[1])**2 <= radius**2:
                    return True
        return False

    def _detect_mp(self, frame, center, radius, face_mesh):
        results = face_mesh.process(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
        if not results.multi_face_landmarks:
            return False
        h, w = frame.shape[:2]
        side = self.params.get('side', 'left')
        for face in results.multi_face_landmarks:
            if self._ear(face, h, w, side) < 0.20:
                return False
            ix, iy, ir = self._iris(face, h, w, side)
            cv2.circle(frame, (ix, iy), ir, (0, 0, 255), 2)
            if self._overlap(ix, iy, ir, center[0], center[1], radius) >= 0.85:
                return True
        return False

    @staticmethod
    def _iris(lms, h, w, side):
        ci = 468 if side == 'left' else 473
        bi = [469, 470, 471, 472] if side == 'left' else [474, 475, 476, 477]
        lm = lms.landmark
        cx = int(lm[ci].x * w)
        cy = int(lm[ci].y * h)
        r = int(np.mean([math.hypot(lm[i].x*w - cx, lm[i].y*h - cy) for i in bi]))
        return cx, cy, max(r, 1)

    @staticmethod
    def _ear(lms, h, w, side):
        idx = ([362, 385, 387, 263, 373, 380] if side == 'left'
               else [33, 160, 158, 133, 153, 144])
        lm = lms.landmark
        pts = [(lm[i].x*w, lm[i].y*h) for i in idx]
        v1 = math.hypot(pts[1][0]-pts[5][0], pts[1][1]-pts[5][1])
        v2 = math.hypot(pts[2][0]-pts[4][0], pts[2][1]-pts[4][1])
        hz = math.hypot(pts[0][0]-pts[3][0], pts[0][1]-pts[3][1])
        return (v1 + v2) / (2.0 * hz) if hz > 0 else 0.0

    @staticmethod
    def _overlap(ix, iy, ir, gx, gy, gr):
        d = math.hypot(ix-gx, iy-gy)
        if d + ir <= gr:
            return 1.0
        if d >= ir + gr:
            return 0.0
        ca = max(-1.0, min(1.0, (d*d + ir*ir - gr*gr) / (2*d*ir)))
        cb = max(-1.0, min(1.0, (d*d + gr*gr - ir*ir) / (2*d*gr)))
        a = math.acos(ca)
        b = math.acos(cb)
        area = (ir*ir * (a - math.sin(a)*math.cos(a)) +
                gr*gr * (b - math.sin(b)*math.cos(b)))
        return area / (math.pi * ir * ir)
