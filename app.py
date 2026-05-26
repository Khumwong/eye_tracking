import cv2
import os
import queue
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox

from PIL import Image, ImageTk

from arduino import ArduinoController
from capture import CaptureThread


class EyeTrackingApp:
    def __init__(self, root):
        self.root = root
        self.root.title("Eye Tracking Beam Control")
        self.root.configure(bg='#1e1e1e')

        self.cap = None
        self.capture: CaptureThread | None = None
        self.frame_queue = queue.Queue(maxsize=2)
        self.pause_event = threading.Event()
        self.pause_event.set()

        self.arduino = ArduinoController()

        # ── tkinter UI vars ────────────────────────────────────
        self.eye_side      = tk.StringVar(value='left')
        self.circle_radius = tk.IntVar(value=50)
        self.center_x      = tk.IntVar(value=320)
        self.center_y      = tk.IntVar(value=240)
        self.zoom_level    = tk.DoubleVar(value=1.0)
        self.zoom_offset_x = tk.IntVar(value=0)
        self.zoom_offset_y = tk.IntVar(value=0)

        self.input_mode    = tk.StringVar(value='camera')
        self.video_path    = tk.StringVar(value='')
        self.video_loop    = tk.BooleanVar(value=True)
        self.playback_speed = tk.DoubleVar(value=1.0)
        self.video_progress = tk.DoubleVar(value=0.0)

        self._video_fps          = 30.0
        self._video_total_frames = 0
        self._seeking            = False

        # Recording
        self._rec_start_time = 0.0
        self._rec_path       = ''

        # ── Plain dict for capture thread (no tkinter calls there) ──
        self._p: dict = {
            'zoom': 1.0, 'zoom_ox': 0, 'zoom_oy': 0,
            'radius': 50, 'cx': 320, 'cy': 240,
            'side': 'left', 'speed': 1.0, 'loop': True,
        }
        self._wire_params()

        self._build_ui()

        # Connect Arduino after the event loop starts (avoids blocking main thread)
        self.root.after(100, self._connect_arduino)

    # ── Param sync ────────────────────────────────────────────

    def _wire_params(self):
        def sync(key, var):
            self._p[key] = var.get()

        self.zoom_level.trace_add(    'write', lambda *_: sync('zoom',    self.zoom_level))
        self.zoom_offset_x.trace_add( 'write', lambda *_: sync('zoom_ox', self.zoom_offset_x))
        self.zoom_offset_y.trace_add( 'write', lambda *_: sync('zoom_oy', self.zoom_offset_y))
        self.circle_radius.trace_add( 'write', lambda *_: sync('radius',  self.circle_radius))
        self.center_x.trace_add(      'write', lambda *_: sync('cx',      self.center_x))
        self.center_y.trace_add(      'write', lambda *_: sync('cy',      self.center_y))
        self.eye_side.trace_add(      'write', lambda *_: sync('side',    self.eye_side))
        self.playback_speed.trace_add('write', lambda *_: sync('speed',   self.playback_speed))
        self.video_loop.trace_add(    'write', lambda *_: sync('loop',    self.video_loop))

    # ── Arduino ───────────────────────────────────────────────

    def _connect_arduino(self):
        def _on_done(ok):
            self.root.after(0, lambda: self.arduino_status.config(
                text="● On" if ok else "● Off",
                fg='#28a745' if ok else '#555'))
        self.arduino.connect_async('/dev/ttyUSB0', 9600, _on_done)

    # ── UI build ──────────────────────────────────────────────

    def _build_ui(self):
        left_outer = tk.Frame(self.root, bg='#252525', width=210)
        left_outer.pack(side=tk.LEFT, fill=tk.Y, padx=(8, 4), pady=8)
        left_outer.pack_propagate(False)

        canvas = tk.Canvas(left_outer, bg='#252525', highlightthickness=0)
        sb = tk.Scrollbar(left_outer, orient=tk.VERTICAL, command=canvas.yview)
        canvas.configure(yscrollcommand=sb.set)
        sb.pack(side=tk.RIGHT, fill=tk.Y)
        canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        left = tk.Frame(canvas, bg='#252525')
        win  = canvas.create_window((0, 0), window=left, anchor='nw')
        left.bind('<Configure>', lambda _: canvas.configure(scrollregion=canvas.bbox('all')))
        canvas.bind('<Configure>', lambda e: canvas.itemconfig(win, width=e.width))
        canvas.bind_all('<MouseWheel>', lambda e: canvas.yview_scroll(-1*(e.delta//120), 'units'))
        canvas.bind_all('<Button-4>',   lambda _: canvas.yview_scroll(-1, 'units'))
        canvas.bind_all('<Button-5>',   lambda _: canvas.yview_scroll( 1, 'units'))

        self.camera_label = tk.Label(self.root, bg='black')
        self.camera_label.pack(side=tk.RIGHT, fill=tk.BOTH, expand=True,
                               padx=(4, 8), pady=8)

        # Connection
        self._section(left, "CONNECTION")
        conn = tk.Frame(left, bg='#252525')
        conn.pack(fill=tk.X, padx=12, pady=(0, 4))
        self.arduino_status = self._dot(conn, "Arduino")
        self.source_status  = self._dot(conn, "Source")
        self._sep(left)

        # Source tabs
        self._section(left, "SOURCE")
        tabs = tk.Frame(left, bg='#252525')
        tabs.pack(fill=tk.X, padx=12, pady=(2, 0))
        tabs.columnconfigure(0, weight=1)
        tabs.columnconfigure(1, weight=1)
        self._cam_tab = tk.Button(tabs, text="CAMERA",
                                   command=lambda: self._set_source('camera'),
                                   relief='flat', cursor='hand2', pady=7,
                                   font=('Arial', 9, 'bold'), bd=0)
        self._cam_tab.grid(row=0, column=0, sticky='ew', padx=(0, 1))
        self._vid_tab = tk.Button(tabs, text="VIDEO",
                                   command=lambda: self._set_source('video'),
                                   relief='flat', cursor='hand2', pady=7,
                                   font=('Arial', 9, 'bold'), bd=0)
        self._vid_tab.grid(row=0, column=1, sticky='ew', padx=(1, 0))
        self._tab_row = tabs
        self._update_tabs()

        # Video panel (hidden until VIDEO tab selected)
        self.video_panel = tk.Frame(left, bg='#1e1e1e', pady=4)
        tk.Label(self.video_panel, text="File", bg='#1e1e1e', fg='#888',
                 font=('Arial', 8)).pack(anchor=tk.W, padx=10, pady=(4, 0))
        frow = tk.Frame(self.video_panel, bg='#1e1e1e')
        frow.pack(fill=tk.X, padx=10, pady=(0, 6))
        tk.Entry(frow, textvariable=self.video_path, bg='#333', fg='white',
                 insertbackground='white', relief='flat',
                 font=('Arial', 8)).pack(side=tk.LEFT, fill=tk.X, expand=True,
                                         padx=(0, 4), ipady=4)
        tk.Button(frow, text="Browse", command=self._browse,
                  bg='#444', fg='white', relief='flat', cursor='hand2',
                  font=('Arial', 8), padx=6, pady=3).pack(side=tk.LEFT)

        orow = tk.Frame(self.video_panel, bg='#1e1e1e')
        orow.pack(fill=tk.X, padx=10, pady=(0, 2))
        tk.Checkbutton(orow, text="Loop", variable=self.video_loop,
                       bg='#1e1e1e', fg='#ccc', selectcolor='#444',
                       activebackground='#1e1e1e', font=('Arial', 9)).pack(side=tk.LEFT)
        self.speed_lbl = tk.Label(orow, text="1.00x", bg='#1e1e1e',
                                   fg='#fd7e14', font=('Arial', 9, 'bold'), width=6)
        self.speed_lbl.pack(side=tk.RIGHT)
        tk.Label(orow, text="Speed:", bg='#1e1e1e', fg='#888',
                 font=('Arial', 8)).pack(side=tk.RIGHT)
        tk.Scale(self.video_panel, from_=0.25, to=4.0, resolution=0.25,
                 orient=tk.HORIZONTAL, variable=self.playback_speed,
                 bg='#1e1e1e', troughcolor='#444', highlightthickness=0,
                 showvalue=False, fg='white',
                 command=lambda v: self.speed_lbl.config(
                     text=f"{float(v):.2f}x")).pack(fill=tk.X, padx=10, pady=(0, 4))

        tk.Frame(self.video_panel, bg='#333', height=1).pack(fill=tk.X, padx=10, pady=(4, 4))
        self.prog_lbl = tk.Label(self.video_panel, text="--:-- / --:--",
                                  bg='#1e1e1e', fg='#888', font=('Arial', 8))
        self.prog_lbl.pack()
        self.prog_slider = tk.Scale(self.video_panel, from_=0, to=100, resolution=0.1,
                                     orient=tk.HORIZONTAL, variable=self.video_progress,
                                     bg='#1e1e1e', troughcolor='#444', highlightthickness=0,
                                     showvalue=False, fg='white', state=tk.DISABLED,
                                     command=self._on_seek)
        self.prog_slider.pack(fill=tk.X, padx=10, pady=(0, 6))

        self._sep(left)

        # Eye side
        self._section(left, "EYE SIDE")
        er = tk.Frame(left, bg='#252525')
        er.pack(fill=tk.X, padx=12, pady=(2, 6))
        for txt, val in (("Left", "left"), ("Right", "right")):
            tk.Radiobutton(er, text=txt, variable=self.eye_side, value=val,
                           **self._rb()).pack(side=tk.LEFT, padx=(0, 12))
        self._sep(left)

        # Settings
        self._section(left, "SETTINGS")
        rrow = tk.Frame(left, bg='#252525')
        rrow.pack(fill=tk.X, padx=12, pady=(2, 4))
        tk.Label(rrow, text="Radius:", bg='#252525', fg='#aaa',
                 font=('Arial', 9)).pack(side=tk.LEFT)
        tk.Spinbox(rrow, from_=10, to=300, textvariable=self.circle_radius,
                   width=5, bg='#333', fg='white',
                   buttonbackground='#444', relief='flat').pack(side=tk.LEFT, padx=6)
        tk.Label(rrow, text="px", bg='#252525', fg='#666',
                 font=('Arial', 9)).pack(side=tk.LEFT)

        zrow = tk.Frame(left, bg='#252525')
        zrow.pack(fill=tk.X, padx=12, pady=(4, 0))
        tk.Label(zrow, text="Zoom:", bg='#252525', fg='#aaa',
                 font=('Arial', 9)).pack(side=tk.LEFT)
        self.zoom_lbl = tk.Label(zrow, text="1.0x", bg='#252525',
                                  fg='#0d6efd', font=('Arial', 9, 'bold'), width=5)
        self.zoom_lbl.pack(side=tk.RIGHT)
        tk.Scale(left, from_=1.0, to=4.0, resolution=0.5,
                 orient=tk.HORIZONTAL, variable=self.zoom_level,
                 bg='#252525', troughcolor='#444', highlightthickness=0,
                 showvalue=False, fg='white',
                 command=self._on_zoom).pack(fill=tk.X, padx=12, pady=(0, 6))

        self.zpan_lbl = tk.Label(left, text="Pan view: X:0  Y:0",
                                  bg='#252525', fg='#888', font=('Arial', 8))
        self.zpan_lbl.pack(pady=(0, 2))
        bz = dict(bg='#2a2a2a', fg='#aaa', relief='flat', width=2,
                  cursor='hand2', activebackground='#444', font=('Arial', 10))
        zp = tk.Frame(left, bg='#252525')
        zp.pack(pady=(0, 4))
        tk.Button(zp, text="↑", command=lambda: self._zoom_pan(0, -20),  **bz).grid(row=0, column=1, padx=2, pady=1)
        tk.Button(zp, text="←", command=lambda: self._zoom_pan(-20, 0),  **bz).grid(row=1, column=0, padx=2, pady=1)
        tk.Button(zp, text="⊙", command=self._zoom_pan_reset,             **bz).grid(row=1, column=1, padx=2, pady=1)
        tk.Button(zp, text="→", command=lambda: self._zoom_pan(20, 0),   **bz).grid(row=1, column=2, padx=2, pady=1)
        tk.Button(zp, text="↓", command=lambda: self._zoom_pan(0, 20),   **bz).grid(row=2, column=1, padx=2, pady=1)
        self._sep(left)

        # Position (green circle)
        self._section(left, "POSITION")
        self.pos_lbl = tk.Label(left, text="X: 320   Y: 240",
                                 bg='#252525', fg='#888', font=('Arial', 8))
        self.pos_lbl.pack(pady=(0, 4))
        bp = dict(bg='#333', fg='white', relief='flat', width=3,
                  cursor='hand2', activebackground='#444', font=('Arial', 10))
        pg = tk.Frame(left, bg='#252525')
        pg.pack(pady=(0, 4))
        tk.Button(pg, text="↑", command=lambda: self._pan(0, -10),  **bp).grid(row=0, column=1, padx=2, pady=2)
        tk.Button(pg, text="←", command=lambda: self._pan(-10, 0),  **bp).grid(row=1, column=0, padx=2, pady=2)
        tk.Button(pg, text="⊙", command=self._pan_reset,             **bp).grid(row=1, column=1, padx=2, pady=2)
        tk.Button(pg, text="→", command=lambda: self._pan(10, 0),   **bp).grid(row=1, column=2, padx=2, pady=2)
        tk.Button(pg, text="↓", command=lambda: self._pan(0, 10),   **bp).grid(row=2, column=1, padx=2, pady=2)
        self._sep(left)

        # Beam
        self.beam_lbl = tk.Label(left, text="BEAM OFF", bg='#333', fg='#888',
                                  font=('Arial', 13, 'bold'), pady=10, relief='flat')
        self.beam_lbl.pack(fill=tk.X, padx=12, pady=6)
        self._sep(left)

        # Buttons
        self.start_btn = tk.Button(left, text="▶  START", command=self.start,
                                    bg='#28a745', fg='white', font=('Arial', 11, 'bold'),
                                    relief='flat', pady=9, cursor='hand2',
                                    activebackground='#218838')
        self.start_btn.pack(fill=tk.X, padx=12, pady=(4, 2))

        self.stop_btn = tk.Button(left, text="■  STOP", command=self.stop,
                                   bg='#dc3545', fg='white', font=('Arial', 11, 'bold'),
                                   relief='flat', pady=9, cursor='hand2',
                                   activebackground='#b02a37', state=tk.DISABLED)
        self.stop_btn.pack(fill=tk.X, padx=12, pady=2)

        self.pause_btn = tk.Button(left, text="⏸  PAUSE", command=self._toggle_pause,
                                    bg='#444', fg='#888', font=('Arial', 11, 'bold'),
                                    relief='flat', pady=9, cursor='hand2',
                                    activebackground='#555', state=tk.DISABLED)
        self.pause_btn.pack(fill=tk.X, padx=12, pady=(2, 6))
        self._sep(left)

        # Record
        self._section(left, "RECORD  (camera only)")
        self.rec_timer_lbl = tk.Label(left, text="", bg='#252525', fg='#dc3545',
                                       font=('Arial', 9, 'bold'))
        self.rec_timer_lbl.pack(pady=(0, 2))
        self.rec_btn = tk.Button(left, text="⏺  REC", command=self._toggle_rec,
                                  bg='#444', fg='#888', font=('Arial', 11, 'bold'),
                                  relief='flat', pady=9, cursor='hand2',
                                  activebackground='#555', state=tk.DISABLED)
        self.rec_btn.pack(fill=tk.X, padx=12, pady=(0, 8))

    # ── UI helpers ────────────────────────────────────────────

    def _section(self, p, title):
        tk.Label(p, text=title, bg='#252525', fg='#555',
                 font=('Arial', 8, 'bold')).pack(anchor=tk.W, padx=12, pady=(8, 2))

    def _sep(self, p):
        tk.Frame(p, bg='#3a3a3a', height=1).pack(fill=tk.X, padx=8, pady=4)

    def _dot(self, parent, label):
        tk.Label(parent, text=f"{label}:", bg='#252525', fg='#555',
                 font=('Arial', 8), width=8, anchor=tk.W).pack(side=tk.LEFT)
        lbl = tk.Label(parent, text="● Off", bg='#252525', fg='#555', font=('Arial', 8))
        lbl.pack(side=tk.LEFT, padx=(0, 12))
        return lbl

    def _rb(self):
        return dict(bg='#252525', fg='#ccc', selectcolor='#444',
                    activebackground='#252525', activeforeground='white',
                    font=('Arial', 9))

    # ── Source ────────────────────────────────────────────────

    def _set_source(self, value):
        if self.input_mode.get() == value:
            return
        was_running = self.capture is not None and self.capture.running
        if was_running:
            self.stop()
        self.input_mode.set(value)
        self._update_tabs()
        self._toggle_video_panel()
        if was_running:
            if value == 'camera':
                self.start()
            elif self.video_path.get().strip():
                self.start()

    def _update_tabs(self):
        cam = self.input_mode.get() == 'camera'
        self._cam_tab.config(bg='#0d6efd' if cam  else '#2a2a2a',
                             fg='white'   if cam  else '#555',
                             activebackground='#0a58ca' if cam else '#333',
                             activeforeground='white')
        self._vid_tab.config(bg='#fd7e14' if not cam else '#2a2a2a',
                             fg='white'   if not cam else '#555',
                             activebackground='#e8680a' if not cam else '#333',
                             activeforeground='white')

    def _toggle_video_panel(self):
        if self.input_mode.get() == 'video':
            self.video_panel.pack(fill=tk.X, after=self._tab_row)
        else:
            self.video_panel.pack_forget()

    def _browse(self):
        path = filedialog.askopenfilename(
            title="Select Video File",
            filetypes=[("Video files", "*.mp4 *.avi *.mov *.mkv *.wmv *.flv *.webm *.m4v"),
                       ("All files", "*.*")])
        if path:
            self.video_path.set(path)

    # ── Zoom / Pan ────────────────────────────────────────────

    def _on_zoom(self, val):
        self.zoom_lbl.config(text=f"{float(val):.1f}x")
        if float(val) <= 1.0:
            self._zoom_pan_reset()

    def _zoom_pan(self, dx, dy):
        self.zoom_offset_x.set(self.zoom_offset_x.get() + dx)
        self.zoom_offset_y.set(self.zoom_offset_y.get() + dy)
        self.zpan_lbl.config(text=f"Pan view: X:{self.zoom_offset_x.get()}  Y:{self.zoom_offset_y.get()}")

    def _zoom_pan_reset(self):
        self.zoom_offset_x.set(0)
        self.zoom_offset_y.set(0)
        self.zpan_lbl.config(text="Pan view: X:0  Y:0")

    def _pan(self, dx, dy):
        self.center_x.set(max(0, min(640, self.center_x.get() + dx)))
        self.center_y.set(max(0, min(480, self.center_y.get() + dy)))
        self.pos_lbl.config(text=f"X: {self.center_x.get()}   Y: {self.center_y.get()}")

    def _pan_reset(self):
        self.center_x.set(320)
        self.center_y.set(240)
        self.pos_lbl.config(text="X: 320   Y: 240")

    # ── Pause ─────────────────────────────────────────────────

    def _toggle_pause(self):
        if self.pause_event.is_set():
            self.pause_event.clear()
            self.pause_btn.config(text="▶  RESUME", bg='#fd7e14',
                                  fg='white', activebackground='#e8680a')
        else:
            self.pause_event.set()
            self.pause_btn.config(text="⏸  PAUSE", bg='#fd7e14',
                                  fg='white', activebackground='#e8680a')

    # ── Progress ──────────────────────────────────────────────

    def _on_seek(self, val):
        if self._video_total_frames > 0 and not self._seeking and self.capture:
            self.capture.seek_to(int(float(val) / 100.0 * self._video_total_frames))

    def _on_progress(self, progress, elapsed, total):
        self.root.after(0, lambda p=progress, e=elapsed, t=total:
                        self._update_progress_ui(p, e, t))

    def _update_progress_ui(self, progress, elapsed, total):
        self._seeking = True
        self.video_progress.set(progress)
        self._seeking = False
        self.prog_lbl.config(text=f"{self._fmt(elapsed)} / {self._fmt(total)}")

    @staticmethod
    def _fmt(s):
        s = int(s)
        m, s = divmod(s, 60)
        h, m = divmod(m, 60)
        return f"{h:02d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"

    # ── Recording ─────────────────────────────────────────────

    def _toggle_rec(self):
        if not self.capture or not self.capture._recording:
            self._start_rec()
        else:
            self._stop_rec()

    def _start_rec(self):
        ts = time.strftime("%Y%m%d_%H%M%S")
        path = os.path.join(os.path.dirname(__file__), 'video',
                            f"eye_tracking_{ts}.mp4")
        self.capture.start_recording(path)
        self._rec_path = path
        self._rec_start_time = time.time()
        self.rec_btn.config(text="⏹  STOP REC", bg='#dc3545',
                            fg='white', activebackground='#b02a37')
        self._tick_rec()

    def _stop_rec(self):
        if self.capture:
            self.capture.stop_recording()
        self.rec_btn.config(text="⏺  REC", bg='#444',
                            fg='#888', activebackground='#555')
        self.rec_timer_lbl.config(text="")
        if self._rec_path:
            self.video_path.set(self._rec_path)
            self._rec_path = ''
            messagebox.showinfo("Saved", "Recording saved.\nPath auto-filled in Video mode.")

    def _tick_rec(self):
        if not self.capture or not self.capture._recording:
            return
        m, s = divmod(int(time.time() - self._rec_start_time), 60)
        self.rec_timer_lbl.config(text=f"● REC  {m:02d}:{s:02d}")
        self.root.after(1000, self._tick_rec)

    # ── Start / Stop ──────────────────────────────────────────

    def start(self):
        is_video = self.input_mode.get() == 'video'

        if is_video:
            path = self.video_path.get().strip()
            if not path:
                messagebox.showerror("No File", "Please select a video file first.")
                return
            self.cap = cv2.VideoCapture(path)
            if not self.cap.isOpened():
                messagebox.showerror("Error", f"Cannot open video:\n{path}")
                self.source_status.config(text="● Off", fg='#555')
                return
        else:
            self.cap = cv2.VideoCapture(0)
            if not self.cap.isOpened():
                self.source_status.config(text="● Off", fg='#555')
                return

        self._video_fps          = self.cap.get(cv2.CAP_PROP_FPS) or 30.0
        self._video_total_frames = int(self.cap.get(cv2.CAP_PROP_FRAME_COUNT))

        self.source_status.config(text="● On", fg='#28a745')
        self.pause_event.set()

        self.capture = CaptureThread(
            cap=self.cap,
            params=self._p,
            arduino=self.arduino,
            is_video=is_video,
            fps=self._video_fps,
            total_frames=self._video_total_frames,
            frame_queue=self.frame_queue,
            pause_event=self.pause_event,
            on_video_end=lambda: self.root.after(0, self.stop),
            on_progress=self._on_progress,
        )
        self.capture.start()

        self.start_btn.config(state=tk.DISABLED)
        self.stop_btn.config(state=tk.NORMAL)

        if is_video:
            self.pause_btn.config(text="⏸  PAUSE", state=tk.NORMAL,
                                  bg='#fd7e14', fg='white',
                                  activebackground='#e8680a')
            self.prog_slider.config(state=tk.NORMAL)
            self.prog_lbl.config(
                text=f"00:00 / {self._fmt(self._video_total_frames / self._video_fps)}")
            self.rec_btn.config(state=tk.DISABLED, bg='#444', fg='#888')
        else:
            self.pause_btn.config(state=tk.DISABLED, bg='#444', fg='#888',
                                  text="⏸  PAUSE")
            self.prog_slider.config(state=tk.DISABLED)
            self.rec_btn.config(state=tk.NORMAL, bg='#444', fg='#888',
                                activebackground='#555')

        self._update_frame()

    def stop(self):
        if self.capture:
            self.capture.stop_recording()
            self.capture.stop()
            self.capture = None

        if self.cap:
            self.cap.release()
            self.cap = None

        self.arduino.send(b'B0\n')

        self.start_btn.config(state=tk.NORMAL)
        self.stop_btn.config(state=tk.DISABLED)
        self.pause_btn.config(text="⏸  PAUSE", state=tk.DISABLED,
                              bg='#444', fg='#888')
        self.prog_slider.config(state=tk.DISABLED)
        self.rec_btn.config(text="⏺  REC", state=tk.DISABLED,
                            bg='#444', fg='#888')
        self.rec_timer_lbl.config(text="")
        self.beam_lbl.config(text="BEAM OFF", bg='#333', fg='#888')
        self.camera_label.config(image='', bg='black')
        self.source_status.config(text="● Off", fg='#555')
        self.video_progress.set(0)
        self.prog_lbl.config(text="--:-- / --:--")

    # ── Frame display ─────────────────────────────────────────

    def _update_frame(self):
        if not self.capture or not self.capture.running:
            return
        try:
            frame, state = self.frame_queue.get_nowait()
            self.beam_lbl.config(
                text="BEAM ON"  if state == 'O' else "BEAM OFF",
                bg  ='#dc3545' if state == 'O' else '#333',
                fg  ='white'   if state == 'O' else '#888')
            w = max(self.camera_label.winfo_width(),  640)
            h = max(self.camera_label.winfo_height(), 480)
            img = Image.fromarray(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
            scale = min(w / img.width, h / img.height)
            img = img.resize((int(img.width * scale), int(img.height * scale)),
                             Image.LANCZOS)
            tk_img = ImageTk.PhotoImage(image=img)
            self.camera_label.imgtk = tk_img
            self.camera_label.config(image=tk_img)
        except queue.Empty:
            pass
        self.root.after(30, self._update_frame)

    # ── Close ─────────────────────────────────────────────────

    def on_close(self):
        self.stop()
        self.arduino.close()
        self.root.destroy()
