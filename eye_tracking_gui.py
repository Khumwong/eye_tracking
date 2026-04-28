import cv2
import numpy as np
import serial
import time
import threading
import queue
import math
import tkinter as tk
from PIL import Image, ImageTk

try:
    import mediapipe as mp
    _ = mp.solutions.face_mesh
    MEDIAPIPE_AVAILABLE = True
except (ImportError, AttributeError):
    MEDIAPIPE_AVAILABLE = False


class EyeTrackingApp:
    def __init__(self, root):
        self.root = root
        self.root.title("Eye Tracking Beam Control")
        self.root.configure(bg='#1e1e1e')

        self.running = False
        self.arduino = None
        self.cap = None
        self.current_state = 'S'
        self.frame_queue = queue.Queue(maxsize=2)
        self.mp_face_mesh = None

        self.detection_method = tk.StringVar(value='HoughCircles')
        self.eye_side = tk.StringVar(value='left')
        self.circle_radius = tk.IntVar(value=50)
        self.center_x = tk.IntVar(value=320)
        self.center_y = tk.IntVar(value=240)
        self.zoom_level = tk.DoubleVar(value=1.0)

        self.build_ui()
        self.connect_arduino()

    # ─────────────────────────── UI ───────────────────────────

    def build_ui(self):
        left = tk.Frame(self.root, bg='#2d2d2d', width=200)
        left.pack(side=tk.LEFT, fill=tk.Y, padx=(8, 4), pady=8)
        left.pack_propagate(False)

        self.camera_label = tk.Label(self.root, bg='black')
        self.camera_label.pack(side=tk.RIGHT, fill=tk.BOTH, expand=True, padx=(4, 8), pady=8)

        # Connection
        self._section(left, "Connection")
        self.arduino_status = self._status_row(left, "Arduino")
        self.camera_status  = self._status_row(left, "Camera")
        self._sep(left)

        # Detection Method
        self._section(left, "Detection Method")
        tk.Radiobutton(left, text="HoughCircles", variable=self.detection_method,
                       value='HoughCircles', **self._rb_style()).pack(anchor=tk.W, padx=12)
        rb = tk.Radiobutton(left, text="MediaPipe", variable=self.detection_method,
                            value='MediaPipe',
                            state=tk.NORMAL if MEDIAPIPE_AVAILABLE else tk.DISABLED,
                            **self._rb_style())
        rb.pack(anchor=tk.W, padx=12)
        if not MEDIAPIPE_AVAILABLE:
            tk.Label(left, text="  (pip install mediapipe)",
                     bg='#2d2d2d', fg='#666', font=('Arial', 8)).pack(anchor=tk.W, padx=12)
        self._sep(left)

        # Eye Selection (MediaPipe only)
        self._section(left, "Eye Selection")
        tk.Radiobutton(left, text="Left Eye", variable=self.eye_side,
                       value='left', **self._rb_style()).pack(anchor=tk.W, padx=12)
        tk.Radiobutton(left, text="Right Eye", variable=self.eye_side,
                       value='right', **self._rb_style()).pack(anchor=tk.W, padx=12)
        self._sep(left)

        # Settings
        self._section(left, "Settings")
        row = tk.Frame(left, bg='#2d2d2d')
        row.pack(fill=tk.X, padx=12, pady=4)
        tk.Label(row, text="Radius:", bg='#2d2d2d', fg='white').pack(side=tk.LEFT)
        tk.Spinbox(row, from_=10, to=300, textvariable=self.circle_radius,
                   width=5, bg='#444', fg='white', buttonbackground='#555').pack(side=tk.LEFT, padx=6)
        tk.Label(row, text="px", bg='#2d2d2d', fg='white').pack(side=tk.LEFT)

        row2 = tk.Frame(left, bg='#2d2d2d')
        row2.pack(fill=tk.X, padx=12, pady=(4, 0))
        tk.Label(row2, text="Zoom:", bg='#2d2d2d', fg='white').pack(side=tk.LEFT)
        self.zoom_label = tk.Label(row2, text="1.0x", bg='#2d2d2d', fg='#aaa', width=5)
        self.zoom_label.pack(side=tk.RIGHT)
        zoom_slider = tk.Scale(left, from_=1.0, to=4.0, resolution=0.5,
                               orient=tk.HORIZONTAL, variable=self.zoom_level,
                               bg='#2d2d2d', fg='white', troughcolor='#555',
                               highlightthickness=0, showvalue=False,
                               command=self._on_zoom_change)
        zoom_slider.pack(fill=tk.X, padx=12, pady=(0, 4))

        # Pan (circle position)
        self._section(left, "Position")
        self.pos_label = tk.Label(left, text="X:320  Y:240",
                                  bg='#2d2d2d', fg='#aaa', font=('Arial', 8))
        self.pos_label.pack(pady=(0, 4))
        btn_style = dict(bg='#444', fg='white', relief='flat',
                         width=3, cursor='hand2', activebackground='#555')
        pad = tk.Frame(left, bg='#2d2d2d')
        pad.pack()
        tk.Button(pad, text="↑", command=lambda: self._pan(0, -10),  **btn_style).grid(row=0, column=1, padx=2, pady=2)
        tk.Button(pad, text="←", command=lambda: self._pan(-10, 0),  **btn_style).grid(row=1, column=0, padx=2, pady=2)
        tk.Button(pad, text="⊙", command=self._pan_reset,             **btn_style).grid(row=1, column=1, padx=2, pady=2)
        tk.Button(pad, text="→", command=lambda: self._pan(10, 0),   **btn_style).grid(row=1, column=2, padx=2, pady=2)
        tk.Button(pad, text="↓", command=lambda: self._pan(0, 10),   **btn_style).grid(row=2, column=1, padx=2, pady=2)
        self._sep(left)

        # Beam Status
        self._section(left, "Beam Status")
        self.beam_label = tk.Label(left, text="BEAM OFF",
                                   bg='#444', fg='white',
                                   font=('Arial', 13, 'bold'),
                                   pady=10, relief='flat')
        self.beam_label.pack(fill=tk.X, padx=12, pady=6)
        self._sep(left)

        # Buttons
        self.start_btn = tk.Button(left, text="START", command=self.start,
                                   bg='#28a745', fg='white',
                                   font=('Arial', 11, 'bold'),
                                   relief='flat', pady=8, cursor='hand2')
        self.start_btn.pack(fill=tk.X, padx=12, pady=4)

        self.stop_btn = tk.Button(left, text="STOP", command=self.stop,
                                  bg='#dc3545', fg='white',
                                  font=('Arial', 11, 'bold'),
                                  relief='flat', pady=8, cursor='hand2',
                                  state=tk.DISABLED)
        self.stop_btn.pack(fill=tk.X, padx=12, pady=4)

    def _section(self, p, title):
        tk.Label(p, text=title, bg='#2d2d2d', fg='#aaa',
                 font=('Arial', 9, 'bold')).pack(anchor=tk.W, padx=12, pady=(8, 2))

    def _sep(self, p):
        tk.Frame(p, bg='#444', height=1).pack(fill=tk.X, padx=8, pady=6)

    def _status_row(self, p, label):
        row = tk.Frame(p, bg='#2d2d2d')
        row.pack(fill=tk.X, padx=12, pady=2)
        tk.Label(row, text=f"{label}:", bg='#2d2d2d', fg='#aaa', width=8, anchor=tk.W).pack(side=tk.LEFT)
        lbl = tk.Label(row, text="● Off", bg='#2d2d2d', fg='#dc3545')
        lbl.pack(side=tk.LEFT)
        return lbl

    def _pan(self, dx, dy):
        self.center_x.set(max(0, min(640, self.center_x.get() + dx)))
        self.center_y.set(max(0, min(480, self.center_y.get() + dy)))
        self.pos_label.config(text=f"X:{self.center_x.get()}  Y:{self.center_y.get()}")

    def _pan_reset(self):
        self.center_x.set(320)
        self.center_y.set(240)
        self.pos_label.config(text="X:320  Y:240")

    def _on_zoom_change(self, val):
        self.zoom_label.config(text=f"{float(val):.1f}x")

    def _rb_style(self):
        return dict(bg='#2d2d2d', fg='white', selectcolor='#555',
                    activebackground='#2d2d2d', activeforeground='white')

    # ─────────────────────────── Arduino ───────────────────────────

    def connect_arduino(self):
        try:
            self.arduino = serial.Serial('/dev/ttyUSB0', 9600, timeout=1)
            time.sleep(2)
            self.arduino_status.config(text="● On", fg='#28a745')
        except Exception:
            self.arduino = None
            self.arduino_status.config(text="● Off", fg='#dc3545')

    def send_to_arduino(self, msg: bytes):
        if self.arduino and self.arduino.is_open:
            try:
                self.arduino.write(msg)
            except Exception:
                pass

    # ─────────────────────────── Start / Stop ───────────────────────────

    def start(self):
        self.cap = cv2.VideoCapture(0)
        if not self.cap.isOpened():
            self.camera_status.config(text="● Off", fg='#dc3545')
            return

        self.camera_status.config(text="● On", fg='#28a745')
        self.running = True
        self.current_state = 'S'
        self.send_to_arduino(b'B0\n')

        self.start_btn.config(state=tk.DISABLED)
        self.stop_btn.config(state=tk.NORMAL)

        if self.detection_method.get() == 'MediaPipe' and MEDIAPIPE_AVAILABLE:
            self.mp_face_mesh = mp.solutions.face_mesh.FaceMesh(
                max_num_faces=1, refine_landmarks=True,
                min_detection_confidence=0.5, min_tracking_confidence=0.5)

        threading.Thread(target=self.capture_loop, daemon=True).start()
        self.update_frame()

    def stop(self):
        self.running = False
        self.send_to_arduino(b'B0\n')
        self.current_state = 'S'
        if self.cap:
            self.cap.release()
            self.cap = None
        if self.mp_face_mesh:
            self.mp_face_mesh.close()
            self.mp_face_mesh = None
        self.start_btn.config(state=tk.NORMAL)
        self.stop_btn.config(state=tk.DISABLED)
        self.beam_label.config(text="BEAM OFF", bg='#444')
        self.camera_label.config(image='', bg='black')
        self.camera_status.config(text="● Off", fg='#dc3545')

    # ─────────────────────────── Capture Loop ───────────────────────────

    def capture_loop(self):
        while self.running:
            ret, frame = self.cap.read()
            if not ret:
                break

            frame = cv2.flip(frame, 1)
            frame = self.apply_zoom(frame)
            radius = self.circle_radius.get()
            center = (self.center_x.get(), self.center_y.get())

            if self.detection_method.get() == 'MediaPipe' and self.mp_face_mesh is not None:
                trigger = self.detect_mediapipe(frame, center, radius)
            else:
                trigger = self.detect_hough(frame, center, radius)

            cv2.circle(frame, center, radius, (0, 255, 0), 2)

            if trigger and self.current_state != 'O':
                self.send_to_arduino(b'B1\n')
                self.current_state = 'O'
            elif not trigger and self.current_state != 'S':
                self.send_to_arduino(b'B0\n')
                self.current_state = 'S'

            label = "Open the shutter!" if self.current_state == 'O' else "Searching..."
            color = (0, 0, 255) if self.current_state == 'O' else (0, 200, 0)
            cv2.putText(frame, label, (10, 30), cv2.FONT_HERSHEY_SIMPLEX, 1, color, 2)

            if not self.frame_queue.full():
                self.frame_queue.put((frame, self.current_state))

    def _iris_circle(self, face_landmarks, h, w, side):
        if side == 'left':
            center_idx, boundary_idxs = 468, [469, 470, 471, 472]
        else:
            center_idx, boundary_idxs = 473, [474, 475, 476, 477]
        lm = face_landmarks.landmark
        cx = int(lm[center_idx].x * w)
        cy = int(lm[center_idx].y * h)
        r = int(np.mean([
            math.hypot(lm[i].x * w - cx, lm[i].y * h - cy)
            for i in boundary_idxs
        ]))
        return cx, cy, max(r, 1)

    def _eye_aspect_ratio(self, face_landmarks, h, w, side):
        # EAR landmarks: [outer, top-left, top-right, inner, bot-right, bot-left]
        idxs = [362, 385, 387, 263, 373, 380] if side == 'left' else [33, 160, 158, 133, 153, 144]
        lm = face_landmarks.landmark
        pts = [(lm[i].x * w, lm[i].y * h) for i in idxs]
        v1 = math.hypot(pts[1][0] - pts[5][0], pts[1][1] - pts[5][1])
        v2 = math.hypot(pts[2][0] - pts[4][0], pts[2][1] - pts[4][1])
        hz = math.hypot(pts[0][0] - pts[3][0], pts[0][1] - pts[3][1])
        return (v1 + v2) / (2.0 * hz) if hz > 0 else 0.0

    def _overlap_fraction(self, ix, iy, ir, gx, gy, gr):
        """Fraction of iris circle (r=ir) that lies inside green circle (r=gr)."""
        d = math.hypot(ix - gx, iy - gy)
        if d + ir <= gr:
            return 1.0
        if d >= ir + gr:
            return 0.0
        cos_a = max(-1.0, min(1.0, (d*d + ir*ir - gr*gr) / (2*d*ir)))
        cos_b = max(-1.0, min(1.0, (d*d + gr*gr - ir*ir) / (2*d*gr)))
        a = math.acos(cos_a)
        b = math.acos(cos_b)
        area = (ir*ir * (a - math.sin(a)*math.cos(a)) +
                gr*gr * (b - math.sin(b)*math.cos(b)))
        return area / (math.pi * ir * ir)

    def apply_zoom(self, frame):
        zoom = self.zoom_level.get()
        if zoom <= 1.0:
            return frame
        h, w = frame.shape[:2]
        new_h, new_w = int(h / zoom), int(w / zoom)
        y1, x1 = (h - new_h) // 2, (w - new_w) // 2
        cropped = frame[y1:y1 + new_h, x1:x1 + new_w]
        return cv2.resize(cropped, (w, h), interpolation=cv2.INTER_LINEAR)

    def detect_hough(self, frame, center, radius):
        gray     = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        inverted = cv2.bitwise_not(gray)
        blurred  = cv2.GaussianBlur(inverted, (9, 9), 0)
        circles  = cv2.HoughCircles(blurred, cv2.HOUGH_GRADIENT,
                                    dp=1.2, minDist=50, param1=50, param2=30,
                                    minRadius=10, maxRadius=20)
        if circles is not None:
            for (x, y, r) in np.round(circles[0]).astype(int):
                cv2.circle(frame, (x, y), r, (0, 0, 255), 2)
                if (x - center[0])**2 + (y - center[1])**2 <= radius**2:
                    return True
        return False

    def detect_mediapipe(self, frame, center, radius):
        if self.mp_face_mesh is None:
            return self.detect_hough(frame, center, radius)
        rgb     = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        results = self.mp_face_mesh.process(rgb)
        if results.multi_face_landmarks:
            h, w = frame.shape[:2]
            for face in results.multi_face_landmarks:
                side = self.eye_side.get()
                ear = self._eye_aspect_ratio(face, h, w, side)
                if ear < 0.20:  # blinking → force beam OFF
                    return False
                ix, iy, ir = self._iris_circle(face, h, w, side)
                cv2.circle(frame, (ix, iy), ir, (0, 0, 255), 2)
                frac = self._overlap_fraction(ix, iy, ir, center[0], center[1], radius)
                if frac >= 0.70:
                    return True
        return False

    # ─────────────────────────── GUI Update ───────────────────────────

    def update_frame(self):
        if not self.running:
            return
        try:
            frame, state = self.frame_queue.get_nowait()
            self.beam_label.config(
                text="BEAM ON"  if state == 'O' else "BEAM OFF",
                bg  ='#dc3545' if state == 'O' else '#444')

            w = max(self.camera_label.winfo_width(),  640)
            h = max(self.camera_label.winfo_height(), 480)
            img = Image.fromarray(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
            img = img.resize((w, h), Image.LANCZOS)
            imgtk = ImageTk.PhotoImage(image=img)
            self.camera_label.imgtk = imgtk
            self.camera_label.config(image=imgtk)
        except queue.Empty:
            pass
        self.root.after(30, self.update_frame)

    def on_close(self):
        self.stop()
        if self.arduino and self.arduino.is_open:
            self.arduino.close()
        self.root.destroy()


if __name__ == '__main__':
    root = tk.Tk()
    root.geometry("960x600")
    app = EyeTrackingApp(root)
    root.protocol("WM_DELETE_WINDOW", app.on_close)
    root.mainloop()
