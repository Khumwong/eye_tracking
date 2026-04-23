import cv2
import numpy as np
import serial
import time
import threading
import queue
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
        self.circle_radius = tk.IntVar(value=50)
        self.circle_center = (320, 240)

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

        # Settings
        self._section(left, "Settings")
        row = tk.Frame(left, bg='#2d2d2d')
        row.pack(fill=tk.X, padx=12, pady=4)
        tk.Label(row, text="Radius:", bg='#2d2d2d', fg='white').pack(side=tk.LEFT)
        tk.Spinbox(row, from_=10, to=300, textvariable=self.circle_radius,
                   width=5, bg='#444', fg='white', buttonbackground='#555').pack(side=tk.LEFT, padx=6)
        tk.Label(row, text="px", bg='#2d2d2d', fg='white').pack(side=tk.LEFT)
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

            radius = self.circle_radius.get()
            center = self.circle_center

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
                for idx in [468, 473]:
                    lm = face.landmark[idx]
                    x, y = int(lm.x * w), int(lm.y * h)
                    cv2.circle(frame, (x, y), 5, (0, 0, 255), -1)
                    if (x - center[0])**2 + (y - center[1])**2 <= radius**2:
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
