import collections
import cv2
import glob
import json
import os
import queue
import re
import socket
import subprocess
import sys
import threading
import time
import traceback
import tkinter as tk
from tkinter import filedialog
from datetime import datetime

from PIL import Image, ImageTk

import alpide_daq
from arduino import ArduinoController
from capture import CaptureThread
from config import (
    BG, PANEL, CARD, INSET, BORD,
    CYAN, CYAND, CYANL, TEXT, TEXT2, MUTED,
    GREENB, GREENL, REDB, REDL, AMBERB, AMBERL,
    MUTEDT, GREEN_TXT, RED_TXT, AMBER_TXT, FEED_BG, HDR_SUB,
)
import config


class EyeTrackingApp:
    # Size of the ITS3 sheet, in characters. RunControl draws an 80-column,
    # 39-line screen; the sheet is cut to that so it comes up showing the whole
    # thing at once, with neither scrollbars nor empty margin.
    _ITS3_COLS  = 80
    _ITS3_LINES = 40

    def __init__(self, root, debug=False):
        self.root = root
        self.root.title("Eye Tracking Beam Control")
        self.root.configure(bg=BG)

        # Debug mode (EYE_TRACKING_DEBUG=1) exposes dev/test-only affordances
        # that have no place in a real treatment session: the Video input
        # source (replays an arbitrary recorded clip through the same
        # detection pipeline as Camera) and the DEBUG frame-dump recorder.
        # Camera is the only input source a clinical operator ever sees.
        self.debug = debug

        self.cap = None
        self.capture: CaptureThread | None = None
        self.frame_queue = queue.Queue(maxsize=2)
        self.pause_event = threading.Event()
        self.pause_event.set()

        self.arduino = ArduinoController(on_line=self._on_arduino_line)
        self.ready = False
        self._cam_ok = False

        # debug/app.log — launch.sh does not redirect stdout, so opening from
        # a desktop icon loses every print() with nowhere to look afterwards.
        # Buffered from process start (before any session folder exists) and
        # flushed into the folder once one does — see log() and _open_app_log.
        self._log_buffer  = collections.deque(maxlen=2000)
        self._app_log_file = None
        self._log_lock = threading.Lock()   # log() runs from worker threads too
                                            # (_alpide_note) as well as the UI thread

        # ALPIDE acquisition (best-effort throughout — see alpide_daq docstring)
        self._alpide_pid     = None
        self._alpide_dir     = ''
        self._alpide_busy    = False   # a firmware flash is running
        self._alpide_msg     = 'idle'
        self._alpide_failed  = 'not started'   # None once recording is confirmed
        self._ev_log         = None    # open CSV correlating beam <-> pulses
        self._session_root   = ''      # output/session_<ts> for this run
        self._daq_ready      = False   # EUDAQ2 is up and RUNNING, trigger not yet started
        self._launch_state   = 'locked'
        self._launch_seq     = 0       # generation counter for the launch watchdog
        self._pending_start_hz = None  # START pressed while LAUNCH was still coming up
        self._killed         = False   # operator override: beam forced off
        # Beam time actually delivered this run. The beam is gated, so wall
        # clock says nothing about how much of the requested dose is spent:
        # a 20 s request takes 40 s of standing there if the eye is off target
        # half the time. Only the accumulated ON time compares to what was asked
        # for at the console.
        self._beam_on_since  = None    # monotonic stamp of the current ON period
        self._beam_on_total  = 0.0     # seconds the shutter has been open
        self.trigger_hz      = tk.IntVar(value=config.TRIGGER_HZ)

        self.eye_side       = tk.StringVar(value='left')
        self.center_x       = tk.IntVar(value=config.DEFAULT_CENTER[0])
        self.center_y       = tk.IntVar(value=config.DEFAULT_CENTER[1])
        self.detect_method  = tk.StringVar(value='facemesh')
        self.threshold_mm   = tk.DoubleVar(value=config.DEFAULT_THRESHOLD_MM)
        self.min_off_s      = tk.DoubleVar(value=config.DEFAULT_MIN_OFF_S)
        self.min_on_target_s = tk.DoubleVar(value=config.DEFAULT_MIN_ON_TARGET_S)
        self.manual_resume  = tk.BooleanVar(value=False)
        self.gray_crosscheck = tk.BooleanVar(value=config.GRAY_CROSSCHECK_ENABLED)
        self.preview_fps    = tk.IntVar(value=config.PREVIEW_FPS)
        self.input_mode     = tk.StringVar(value='camera')
        self.video_path     = tk.StringVar(value='')
        self.video_loop     = tk.BooleanVar(value=True)
        self.playback_speed = tk.DoubleVar(value=1.0)
        self.video_progress = tk.DoubleVar(value=0.0)
        # Intent, not state: checked before LAUNCH, acted on at LAUNCH (or at
        # the no-DAQ fallback start) rather than a button pressed mid-run — see
        # _maybe_auto_record.
        self.record_enabled = tk.BooleanVar(value=False)

        self._video_fps          = 30.0
        self._video_total_frames = 0
        self._seeking            = False
        self._rec_start_time     = 0.0
        self._rec_path           = ''
        self.last_recording_path = ''

        self._display_scale = 1.0
        self._frame_w       = 640
        self._frame_h       = 480

        # 3-way view swap: wide / zoomed eye (color) / zoomed eye (grayscale
        # cross-check). Whichever isn't main shows as a small clickable inset
        # — click an inset to promote it to main; clicking the main view only
        # adjusts the target, it never changes which view is main. See
        # CaptureThread._compose_display.
        self._last_view_meta = None
        # UI-thread mirror of CaptureThread.auto_latched, updated only from
        # _update_frame's metrics read — driving RESUME BEAM's visibility.
        self._auto_latched = False
        # Mirror of the raw per-frame trigger, same channel — lets
        # _resume_beam warn immediately on a click about to be discarded
        # instead of leaving the operator guessing why nothing happened.
        self._eye_on_target = False

        self._p: dict = {
            'cx': config.DEFAULT_CENTER[0], 'cy': config.DEFAULT_CENTER[1],
            'side': 'left', 'speed': 1.0, 'loop': True,
            'detect_method': 'facemesh', 'threshold_mm': config.DEFAULT_THRESHOLD_MM,
            'min_off_s': config.DEFAULT_MIN_OFF_S,
            'min_on_target_s': config.DEFAULT_MIN_ON_TARGET_S,
            'manual_resume': False,
            'capture_downscale': config.CAPTURE_DOWNSCALE,
            'gray_crosscheck': config.GRAY_CROSSCHECK_ENABLED,
            'preview_fps': config.PREVIEW_FPS,
            'main_view': 'wide',
        }
        self._wire_params()
        self._build_ui()
        self._refresh_flow()       # one source of truth for the FLOW colours
        self.root.after(100, self._connect_arduino)
        self.root.after(1500, self._alpide_status_tick)
        self.root.after(1800, self._its3_tick)
        self.root.after(200, self._maybe_start_preview)

    # ── Param sync ─────────────────────────────────

    def _wire_params(self):
        def s(k, v): self._p[k] = v.get()
        self.center_x.trace_add(      'write', lambda *_: s('cx',      self.center_x))
        self.center_y.trace_add(      'write', lambda *_: s('cy',      self.center_y))
        self.eye_side.trace_add(      'write', lambda *_: s('side',          self.eye_side))
        self.detect_method.trace_add( 'write', lambda *_: s('detect_method', self.detect_method))
        self.threshold_mm.trace_add(  'write', lambda *_: s('threshold_mm',  self.threshold_mm))
        self.min_off_s.trace_add(     'write', lambda *_: self._sync_min_off())
        self.min_on_target_s.trace_add('write', lambda *_: self._sync_min_on_target())
        self.preview_fps.trace_add(   'write', lambda *_: self._sync_preview_fps())
        self.playback_speed.trace_add('write', lambda *_: s('speed',         self.playback_speed))
        self.video_loop.trace_add(    'write', lambda *_: s('loop',    self.video_loop))

    # ── Checks ─────────────────────────────────────

    def _maybe_start_preview(self):
        """Auto-open a live (unarmed) camera preview so Eye Selection, Threshold,
        and Target Position can all be set by eye before ENABLE/START are ever
        touched — the beam relay stays gated off regardless (see CaptureThread.armed)."""
        if self.input_mode.get() == 'camera' and not (self.capture and self.capture.running):
            self._open_capture(armed=False)

    def _connect_arduino(self):
        def _on_done(ok):
            self.root.after(0, lambda: self._led_set(self._ard_led, ok))
        port = self._find_arduino_port()
        if port:
            self.arduino.connect_async(port, config.ARDUINO_BAUD, _on_done)
        self.root.after(2000, self._poll_arduino_status)

    def _poll_arduino_status(self):
        def _check():
            port = self._find_arduino_port()
            if not port:
                if self.arduino.is_connected:
                    self.arduino.close()
                self.root.after(0, lambda: self._led_set(self._ard_led, False))
            elif not self.arduino.is_connected:
                def _on_done(ok):
                    self.root.after(0, lambda: self._led_set(self._ard_led, ok))
                self.arduino.connect_async(port, config.ARDUINO_BAUD, _on_done)
            self.root.after(2000, self._poll_arduino_status)
        threading.Thread(target=_check, daemon=True).start()

    @staticmethod
    def _find_arduino_port():
        try:
            from serial.tools.list_ports import comports
            # CH340 VID:PID=1A86:7523
            for p in comports():
                if p.vid == config.ARDUINO_VID and p.pid == config.ARDUINO_PID:
                    return p.device
        except Exception:
            pass
        return None

    # ── LED indicators ─────────────────────────────

    def _led_set(self, led_info, ok):
        c, ov, lbl = led_info
        c.itemconfig(ov, fill=GREEN_TXT if ok else MUTED)
        lbl.config(text="Connected" if ok else "Offline",
                   fg=GREEN_TXT if ok else MUTED)

    # ── UI helpers ─────────────────────────────────

    # ── Sidebar scrolling ──────────────────────────

    def _sidebar_fits(self):
        """True when the whole sidebar is already visible."""
        box = self._sidebar.bbox('all')
        return not box or (box[3] - box[1]) <= self._sidebar.winfo_height()

    def _sidebar_fit(self, canvas, sb, area):
        """Keep the scrollregion current and hide the bar when nothing scrolls."""
        canvas.configure(scrollregion=canvas.bbox('all'))
        if self._sidebar_fits():
            sb.pack_forget()
            canvas.yview_moveto(0)      # nothing below, so do not sit part-way
        else:
            sb.pack(side=tk.RIGHT, fill=tk.Y, before=canvas)

    def _sidebar_wheel(self, on):
        for seq in ('<Button-4>', '<Button-5>', '<MouseWheel>'):
            if on:
                self._sidebar.bind_all(seq, self._sidebar_scroll)
            else:
                self._sidebar.unbind_all(seq)

    def _sidebar_scroll(self, event):
        if self._sidebar_fits():
            return                      # refuse to move a column that fits
        if event.num == 4:
            step = -1
        elif event.num == 5:
            step = 1
        else:
            step = -1 * (event.delta // 120)
        self._sidebar.yview_scroll(step, 'units')

    def _section_header(self, parent, title):
        f = tk.Frame(parent, bg=PANEL, pady=0)
        f.pack(fill=tk.X, padx=12, pady=(14, 4))
        tk.Frame(f, bg=CYAN, width=3, height=14).pack(side=tk.LEFT, padx=(0, 8))
        tk.Label(f, text=title, bg=PANEL, fg=CYANL,
                 font=('Helvetica', 8, 'bold')).pack(side=tk.LEFT, anchor='s')

    def _divider(self, parent):
        tk.Frame(parent, bg=BORD, height=1).pack(fill=tk.X, padx=12, pady=(8, 0))

    def _group_header(self, parent, title):
        f = tk.Frame(parent, bg=INSET, pady=8)
        f.pack(fill=tk.X, pady=(12, 2))
        tk.Label(f, text=title, bg=INSET, fg=CYANL,
                 font=('Helvetica', 10, 'bold')).pack(padx=12, anchor='w')

    def _flat_btn(self, parent, text, cmd, bg, fg, abg=None, state=tk.NORMAL):
        return tk.Button(parent, text=text, command=cmd,
                         bg=bg, fg=fg, activebackground=abg or bg,
                         activeforeground=fg, relief='flat', cursor='hand2',
                         font=('Helvetica', 10, 'bold'), pady=10,
                         state=state, bd=0)

    # ── Build UI ───────────────────────────────────

    def _build_ui(self):
        # ── Sidebar ────────────────────────────────
        sidebar = tk.Frame(self.root, bg=PANEL, width=260)
        sidebar.pack(side=tk.LEFT, fill=tk.Y)
        sidebar.pack_propagate(False)

        # ── Header (fixed) ─────────────────────────
        hdr = tk.Frame(sidebar, bg=CYANL, pady=0)
        hdr.pack(fill=tk.X)
        tk.Frame(hdr, bg=CYAN, height=3).pack(fill=tk.X)
        body = tk.Frame(hdr, bg=CYANL, pady=12)
        body.pack(fill=tk.X)
        tk.Label(body, text="EYE TRACKING", bg=CYANL, fg='white',
                 font=('Helvetica', 12, 'bold')).pack()
        tk.Label(body, text="Beam Control System", bg=CYANL, fg=HDR_SUB,
                 font=('Helvetica', 8)).pack()

        # ── Status (fixed, always visible above the scroll area) ──
        self._section_header(sidebar, "SYSTEM STATUS")
        st = tk.Frame(sidebar, bg=PANEL)
        st.pack(fill=tk.X, padx=14)

        f1 = tk.Frame(st, bg=PANEL)
        f1.pack(fill=tk.X, pady=2)
        c1 = tk.Canvas(f1, width=12, height=12, bg=PANEL, highlightthickness=0)
        c1.pack(side=tk.LEFT, padx=(0, 8))
        ov1 = c1.create_oval(2, 2, 10, 10, fill=MUTED, outline='')
        tk.Label(f1, text="Arduino", bg=PANEL, fg=TEXT2,
                 font=('Helvetica', 9), width=10, anchor='w').pack(side=tk.LEFT)
        lbl1 = tk.Label(f1, text="Offline", bg=PANEL, fg=MUTEDT,
                        font=('Helvetica', 9))
        lbl1.pack(side=tk.RIGHT)
        self._ard_led = (c1, ov1, lbl1)

        f2 = tk.Frame(st, bg=PANEL)
        f2.pack(fill=tk.X, pady=2)
        c2 = tk.Canvas(f2, width=12, height=12, bg=PANEL, highlightthickness=0)
        c2.pack(side=tk.LEFT, padx=(0, 8))
        ov2 = c2.create_oval(2, 2, 10, 10, fill=MUTED, outline='')
        tk.Label(f2, text="Camera", bg=PANEL, fg=TEXT2,
                 font=('Helvetica', 9), width=10, anchor='w').pack(side=tk.LEFT)
        lbl2 = tk.Label(f2, text="Offline", bg=PANEL, fg=MUTEDT,
                        font=('Helvetica', 9))
        lbl2.pack(side=tk.RIGHT)
        self._cam_led = (c2, ov2, lbl2)

        f3 = tk.Frame(st, bg=PANEL)
        f3.pack(fill=tk.X, pady=2)
        c3 = tk.Canvas(f3, width=12, height=12, bg=PANEL, highlightthickness=0)
        c3.pack(side=tk.LEFT, padx=(0, 8))
        ov3 = c3.create_oval(2, 2, 10, 10, fill=MUTED, outline='')
        tk.Label(f3, text="ALPIDE", bg=PANEL, fg=TEXT2,
                 font=('Helvetica', 9), width=10, anchor='w').pack(side=tk.LEFT)
        lbl3 = tk.Label(f3, text="—", bg=PANEL, fg=MUTEDT,
                        font=('Helvetica', 9))
        lbl3.pack(side=tk.RIGHT)
        self._alpide_sys_led = (c3, ov3, lbl3)

        # Precision indicator — primary (color) reading, with the grayscale
        # cross-check value shown right below each, smaller/muted, purely
        # for comparison (never drives threshold/trigger/Arduino).
        tk.Frame(st, bg=BORD, height=1).pack(fill=tk.X, pady=(8, 4))
        prow1 = tk.Frame(st, bg=PANEL)
        prow1.pack(fill=tk.X, pady=1)
        tk.Label(prow1, text="Iris size", bg=PANEL, fg=TEXT2,
                 font=('Helvetica', 9), width=10, anchor='w').pack(side=tk.LEFT)
        self._iris_px_lbl = tk.Label(prow1, text="— px", bg=PANEL, fg=MUTEDT,
                                      font=('Courier', 9, 'bold'))
        self._iris_px_lbl.pack(side=tk.RIGHT)
        prow1g = tk.Frame(st, bg=PANEL)
        prow1g.pack(fill=tk.X)
        tk.Label(prow1g, text="  ↳ B/W", bg=PANEL, fg=MUTEDT,
                 font=('Helvetica', 7), width=10, anchor='w').pack(side=tk.LEFT)
        self._iris_px_gray_lbl = tk.Label(prow1g, text="— px", bg=PANEL, fg=MUTEDT,
                                           font=('Courier', 7))
        self._iris_px_gray_lbl.pack(side=tk.RIGHT)

        prow2 = tk.Frame(st, bg=PANEL)
        prow2.pack(fill=tk.X, pady=(4, 1))
        tk.Label(prow2, text="Precision", bg=PANEL, fg=TEXT2,
                 font=('Helvetica', 9), width=10, anchor='w').pack(side=tk.LEFT)
        self._precision_lbl = tk.Label(prow2, text="— mm/px", bg=PANEL, fg=MUTEDT,
                                        font=('Courier', 9, 'bold'))
        self._precision_lbl.pack(side=tk.RIGHT)
        prow2g = tk.Frame(st, bg=PANEL)
        prow2g.pack(fill=tk.X)
        tk.Label(prow2g, text="  ↳ B/W", bg=PANEL, fg=MUTEDT,
                 font=('Helvetica', 7), width=10, anchor='w').pack(side=tk.LEFT)
        self._precision_gray_lbl = tk.Label(prow2g, text="— mm/px", bg=PANEL, fg=MUTEDT,
                                             font=('Courier', 7))
        self._precision_gray_lbl.pack(side=tk.RIGHT)

        prow3 = tk.Frame(st, bg=PANEL)
        prow3.pack(fill=tk.X, pady=(4, 1))
        tk.Label(prow3, text="2 mm =", bg=PANEL, fg=TEXT2,
                 font=('Helvetica', 9), width=10, anchor='w').pack(side=tk.LEFT)
        self._px2mm_lbl = tk.Label(prow3, text="— px", bg=PANEL, fg=MUTEDT,
                                    font=('Courier', 9, 'bold'))
        self._px2mm_lbl.pack(side=tk.RIGHT)

        # Deviation readout
        tk.Frame(st, bg=BORD, height=1).pack(fill=tk.X, pady=(8, 4))
        drow = tk.Frame(st, bg=PANEL)
        drow.pack(fill=tk.X, pady=1)
        tk.Label(drow, text="Deviation", bg=PANEL, fg=TEXT2,
                 font=('Helvetica', 9), width=10, anchor='w').pack(side=tk.LEFT)
        self._dev_lbl = tk.Label(drow, text="— mm", bg=PANEL, fg=MUTEDT,
                                  font=('Courier', 10, 'bold'))
        self._dev_lbl.pack(side=tk.RIGHT)
        drowg = tk.Frame(st, bg=PANEL)
        drowg.pack(fill=tk.X)
        tk.Label(drowg, text="  ↳ B/W", bg=PANEL, fg=MUTEDT,
                 font=('Helvetica', 7), width=10, anchor='w').pack(side=tk.LEFT)
        self._dev_gray_lbl = tk.Label(drowg, text="— mm", bg=PANEL, fg=MUTEDT,
                                       font=('Courier', 8))
        self._dev_gray_lbl.pack(side=tk.RIGHT)

        self._divider(sidebar)

        # ── Scrollable body (Setting / Flow / Debug) ────
        scroll_area = tk.Frame(sidebar, bg=PANEL)
        scroll_area.pack(side=tk.TOP, fill=tk.BOTH, expand=True)

        canvas = tk.Canvas(scroll_area, bg=PANEL, highlightthickness=0)
        sb = tk.Scrollbar(scroll_area, orient=tk.VERTICAL, command=canvas.yview,
                          bg=PANEL, troughcolor=PANEL, bd=0, width=6)
        canvas.configure(yscrollcommand=sb.set)
        sb.pack(side=tk.RIGHT, fill=tk.Y)
        canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        inner = tk.Frame(canvas, bg=PANEL)
        win = canvas.create_window((0, 0), window=inner, anchor='nw')
        inner.bind('<Configure>',
                   lambda _: self._sidebar_fit(canvas, sb, scroll_area))
        canvas.bind('<Configure>',
                    lambda e: (canvas.itemconfig(win, width=e.width),
                               self._sidebar_fit(canvas, sb, scroll_area)))
        self._sidebar = canvas

        # The wheel is bound only while the pointer is actually over the
        # sidebar. bind_all put it on the whole application, so scrolling over
        # the camera or the ITS3 strip dragged this column out of place — and it
        # scrolled even when everything already fitted, because yview_scroll
        # does not care whether there is anywhere to go.
        for w in (canvas, inner):
            w.bind('<Enter>', lambda _e: self._sidebar_wheel(True))
            w.bind('<Leave>', lambda _e: self._sidebar_wheel(False))

        # ── SETTING ──────────────────────────────────
        self._group_header(inner, "SETTING")

        # ── Source ─────────────────────────────────
        # Camera is the only input source a clinical operator ever sees or
        # needs — input_mode stays 'camera' for the life of the app unless
        # debug mode is on. The Video tab (replay a recorded clip through
        # the same pipeline) is a dev/regression-test tool only; there is no
        # real scenario for firing the beam from a pre-recorded file. See
        # the "Input Source — Camera vs Video" note in the 12.1 writeup.
        if self.debug:
            self._section_header(inner, "INPUT SOURCE")
            tabs = tk.Frame(inner, bg=INSET, padx=2, pady=2)
            tabs.pack(fill=tk.X, padx=12, pady=(0, 4))
            tabs.columnconfigure(0, weight=1)
            tabs.columnconfigure(1, weight=1)
            self._cam_tab = tk.Button(tabs, text="Camera",
                                       command=lambda: self._set_source('camera'),
                                       relief='flat', cursor='hand2', pady=7, bd=0,
                                       font=('Helvetica', 9, 'bold'))
            self._cam_tab.grid(row=0, column=0, sticky='ew', padx=(0, 1))
            self._vid_tab = tk.Button(tabs, text="Video",
                                       command=lambda: self._set_source('video'),
                                       relief='flat', cursor='hand2', pady=7, bd=0,
                                       font=('Helvetica', 9, 'bold'))
            self._vid_tab.grid(row=0, column=1, sticky='ew', padx=(1, 0))
            self._tab_row = tabs
            self._update_tabs()

        # Video panel (built even outside debug mode: harmless while unused —
        # it only ever becomes visible via _toggle_video_panel(), which is
        # only ever reached from the Video tab click handler above)
        self.video_panel = tk.Frame(inner, bg=CARD)
        tk.Label(self.video_panel, text="FILE PATH", bg=CARD, fg=TEXT2,
                 font=('Helvetica', 7, 'bold')).pack(anchor='w', padx=12, pady=(8, 2))
        frow = tk.Frame(self.video_panel, bg=CARD)
        frow.pack(fill=tk.X, padx=12, pady=(0, 6))
        tk.Entry(frow, textvariable=self.video_path, bg=INSET, fg=TEXT,
                 insertbackground=CYANL, relief='flat',
                 font=('Courier', 8)).pack(side=tk.LEFT, fill=tk.X, expand=True,
                                            padx=(0, 4), ipady=5)
        tk.Button(frow, text="Browse", command=self._browse, bg=CYAND, fg='white',
                  relief='flat', cursor='hand2', font=('Helvetica', 8, 'bold'),
                  padx=8, pady=4, activebackground=CYAN,
                  activeforeground='white').pack(side=tk.LEFT)

        orow = tk.Frame(self.video_panel, bg=CARD)
        orow.pack(fill=tk.X, padx=12, pady=(0, 4))
        tk.Checkbutton(orow, text="Loop", variable=self.video_loop,
                       bg=CARD, fg=TEXT2, selectcolor=INSET,
                       activebackground=CARD, font=('Helvetica', 9)).pack(side=tk.LEFT)
        self.speed_lbl = tk.Label(orow, text="1.00×", bg=CARD, fg=AMBER_TXT,
                                   font=('Courier', 9, 'bold'), width=6)
        self.speed_lbl.pack(side=tk.RIGHT)
        tk.Label(orow, text="Speed", bg=CARD, fg=TEXT2,
                 font=('Helvetica', 8)).pack(side=tk.RIGHT, padx=(0, 4))
        tk.Scale(self.video_panel, from_=0.25, to=4.0, resolution=0.25,
                 orient=tk.HORIZONTAL, variable=self.playback_speed,
                 bg=CARD, troughcolor=INSET, highlightthickness=0,
                 showvalue=False, fg=TEXT,
                 command=lambda v: self.speed_lbl.config(text=f"{float(v):.2f}×")
                 ).pack(fill=tk.X, padx=12, pady=(0, 4))

        tk.Frame(self.video_panel, bg=BORD, height=1).pack(fill=tk.X, padx=12, pady=4)
        self.prog_lbl = tk.Label(self.video_panel, text="--:-- / --:--",
                                  bg=CARD, fg=TEXT2, font=('Courier', 8))
        self.prog_lbl.pack()
        self.prog_slider = tk.Scale(self.video_panel, from_=0, to=100, resolution=0.1,
                                     orient=tk.HORIZONTAL, variable=self.video_progress,
                                     bg=CARD, troughcolor=INSET, highlightthickness=0,
                                     showvalue=False, fg=TEXT, state=tk.DISABLED,
                                     command=self._on_seek)
        self.prog_slider.pack(fill=tk.X, padx=12, pady=(0, 8))

        self._divider(inner)

        # ── Eye side ───────────────────────────────
        self._section_header(inner, "EYE SELECTION")
        er = tk.Frame(inner, bg=PANEL)
        er.pack(fill=tk.X, padx=12, pady=(0, 4))
        self._eye_radios = []
        for txt, val in (("Left Eye", "left"), ("Right Eye", "right")):
            rb = tk.Radiobutton(er, text=txt, variable=self.eye_side, value=val,
                                 bg=PANEL, fg=TEXT2, selectcolor=INSET,
                                 activebackground=PANEL, activeforeground=CYANL,
                                 font=('Helvetica', 9))
            rb.pack(side=tk.LEFT, padx=(0, 16))
            self._eye_radios.append(rb)

        self._divider(inner)

        # ── ALPIDE acquisition ─────────────────────
        # Deliberately just a status line and the trigger rate: the EUDAQ
        # parameters live in config.py because they change per campaign, not per
        # session, and having them on screen invites fiddling mid-treatment.
        self._section_header(inner, "ALPIDE ACQUISITION")
        arow = tk.Frame(inner, bg=PANEL)
        arow.pack(fill=tk.X, padx=12, pady=(0, 2))
        tk.Label(arow, text="Detector", bg=PANEL, fg=TEXT2,
                 font=('Helvetica', 9)).pack(side=tk.LEFT)
        self._alpide_lbl = tk.Label(arow, text="—", bg=PANEL, fg=MUTEDT,
                                     font=('Courier', 8))
        self._alpide_lbl.pack(side=tk.RIGHT)

        # Per-plane state, scraped from the RunControl TUI. The label above says
        # how many boards are attached; this says which of them are actually
        # taking data, which is the difference between a run and a run that
        # quietly recorded nothing.
        prow = tk.Frame(inner, bg=PANEL)
        prow.pack(fill=tk.X, padx=12, pady=(2, 4))
        self._plane_leds = []
        for i in range(config.ALPIDE_NUM):
            cell = tk.Frame(prow, bg=PANEL)
            cell.pack(side=tk.LEFT, expand=True)
            cv = tk.Canvas(cell, width=10, height=10, bg=PANEL,
                           highlightthickness=0)
            cv.pack()
            oval = cv.create_oval(1, 1, 9, 9, fill=MUTED, outline='')
            tk.Label(cell, text=f'p{i}', bg=PANEL, fg=TEXT2,
                     font=('Courier', 7)).pack()
            self._plane_leds.append((cv, oval))

        trow2 = tk.Frame(inner, bg=PANEL)
        trow2.pack(fill=tk.X, padx=12, pady=(2, 2))
        tk.Label(trow2, text="Trigger", bg=PANEL, fg=TEXT2,
                 font=('Helvetica', 9)).pack(side=tk.LEFT)
        self._trig_entry = tk.Entry(trow2, textvariable=self.trigger_hz, width=7,
                                     bg=INSET, fg=TEXT, insertbackground=TEXT,
                                     relief='flat', justify='right',
                                     font=('Courier', 9))
        self._trig_entry.pack(side=tk.RIGHT)
        tk.Label(trow2, text="Hz", bg=PANEL, fg=MUTEDT,
                 font=('Helvetica', 8)).pack(side=tk.RIGHT, padx=(0, 4))

        self._divider(inner)

        # ── Settings ───────────────────────────────
        self._section_header(inner, "DETECTION PARAMETERS")

        # Detection method toggle
        dtabs = tk.Frame(inner, bg=INSET, padx=2, pady=2)
        dtabs.pack(fill=tk.X, padx=12, pady=(2, 8))
        dtabs.columnconfigure(0, weight=1)
        dtabs.columnconfigure(1, weight=1)
        self._fm_tab = tk.Button(dtabs, text="FaceMesh",
                                  command=lambda: self._set_detect('facemesh'),
                                  relief='flat', cursor='hand2', pady=7, bd=0,
                                  font=('Helvetica', 9, 'bold'))
        self._fm_tab.grid(row=0, column=0, sticky='ew', padx=(0, 1))
        self._fp_tab = tk.Button(dtabs, text="FaceMesh + Pupil",
                                  command=lambda: self._set_detect('facemesh_pupil'),
                                  relief='flat', cursor='hand2', pady=7, bd=0,
                                  font=('Helvetica', 9, 'bold'))
        self._fp_tab.grid(row=0, column=1, sticky='ew', padx=(1, 0))
        self._update_detect_tabs()

        # Threshold
        trow = tk.Frame(inner, bg=PANEL)
        trow.pack(fill=tk.X, padx=12, pady=(6, 2))
        tk.Label(trow, text="Threshold", bg=PANEL, fg=TEXT2,
                 font=('Helvetica', 9)).pack(side=tk.LEFT)
        self._thr_lbl = tk.Label(trow, text="3.0 mm", bg=PANEL, fg=AMBER_TXT,
                                  font=('Courier', 10, 'bold'))
        self._thr_lbl.pack(side=tk.RIGHT)
        self._threshold_scale = tk.Scale(
            inner, from_=0.5, to=10.0, resolution=0.5,
            orient=tk.HORIZONTAL, variable=self.threshold_mm,
            bg=PANEL, troughcolor=INSET, highlightthickness=0,
            showvalue=False, fg=TEXT,
            command=lambda v: self._thr_lbl.config(text=f"{float(v):.1f} mm"))
        self._threshold_scale.pack(fill=tk.X, padx=12, pady=(0, 6))

        # Minimum time the beam stays off once cut. An eye resting exactly on
        # the threshold chatters the shutter without it — 35 cuts in 26 s at
        # 3.0 mm. Only ever delays reopening, never closing.
        # Typed rather than dragged: unlike the threshold, this is a number
        # somebody arrives at from the off-periods in a previous run, not one
        # they hunt for by feel against a live picture.
        mrow = tk.Frame(inner, bg=PANEL)
        mrow.pack(fill=tk.X, padx=12, pady=(6, 8))
        tk.Label(mrow, text="Min beam-off", bg=PANEL, fg=TEXT2,
                 font=('Helvetica', 9)).pack(side=tk.LEFT)
        tk.Label(mrow, text="s", bg=PANEL, fg=MUTEDT,
                 font=('Helvetica', 8)).pack(side=tk.RIGHT, padx=(4, 0))
        self._minoff_entry = tk.Entry(mrow, textvariable=self.min_off_s, width=6,
                                       bg=INSET, fg=TEXT, insertbackground=TEXT,
                                       relief='flat', justify='right',
                                       font=('Courier', 9))
        self._minoff_entry.pack(side=tk.RIGHT)

        # The same protection on the way back in. Kept as its own box rather
        # than folded into the one above because they answer different
        # questions — how long the beam rests after a cut, versus how steady
        # the eye has to be before it is trusted again — and a run may well
        # want one without the other.
        drow = tk.Frame(inner, bg=PANEL)
        drow.pack(fill=tk.X, padx=12, pady=(0, 8))
        tk.Label(drow, text="Min on-target", bg=PANEL, fg=TEXT2,
                 font=('Helvetica', 9)).pack(side=tk.LEFT)
        tk.Label(drow, text="s", bg=PANEL, fg=MUTEDT,
                 font=('Helvetica', 8)).pack(side=tk.RIGHT, padx=(4, 0))
        self._dwell_entry = tk.Entry(drow, textvariable=self.min_on_target_s,
                                     width=6, bg=INSET, fg=TEXT,
                                     insertbackground=TEXT, relief='flat',
                                     justify='right', font=('Courier', 9))
        self._dwell_entry.pack(side=tk.RIGHT)

        # Off by default: existing auto-reopen (with or without the Min
        # beam-off hold above) keeps working unchanged. When on, an
        # eye-triggered cut latches shut — like KILL BEAM, but eye-caused
        # rather than operator-caused — until RESUME BEAM is pressed, even
        # once the eye is back on target. See CaptureThread._closed_by_eye.
        arow = tk.Frame(inner, bg=PANEL)
        arow.pack(fill=tk.X, padx=12, pady=(0, 8))
        self._resume_chk = tk.Checkbutton(
            arow, text="Manual resume after cut", variable=self.manual_resume,
            command=self._sync_manual_resume,
            bg=PANEL, fg=TEXT2, selectcolor=INSET, activebackground=PANEL,
            activeforeground=TEXT, font=('Helvetica', 9))
        self._resume_chk.pack(side=tk.LEFT)

        # On by default. Only gates the "second opinion" comparison readout —
        # never the trigger/threshold/Arduino path (see
        # CaptureThread._detect_gray_iris). Takes effect the next time
        # preview opens (new CaptureThread), not instantly: the FaceMesh
        # instance it controls is created once at thread start, the same way
        # armed camera preview itself doesn't restart when you flip this.
        grow = tk.Frame(inner, bg=PANEL)
        grow.pack(fill=tk.X, padx=12, pady=(0, 8))
        self._gray_chk = tk.Checkbutton(
            grow, text="Grayscale cross-check", variable=self.gray_crosscheck,
            command=self._sync_gray_crosscheck,
            bg=PANEL, fg=TEXT2, selectcolor=INSET, activebackground=PANEL,
            activeforeground=TEXT, font=('Helvetica', 9))
        self._gray_chk.pack(side=tk.LEFT)

        # Only affects preview (armed=False) — read fresh every frame in the
        # capture loop, so unlike the checkbox above this one takes effect
        # immediately, no reopen needed. Never touches armed/latency at all.
        pfrow = tk.Frame(inner, bg=PANEL)
        pfrow.pack(fill=tk.X, padx=12, pady=(0, 8))
        tk.Label(pfrow, text="Preview FPS", bg=PANEL, fg=TEXT2,
                 font=('Helvetica', 9)).pack(side=tk.LEFT)
        self._preview_fps_entry = tk.Entry(
            pfrow, textvariable=self.preview_fps, width=6,
            bg=INSET, fg=TEXT, insertbackground=TEXT,
            relief='flat', justify='right', font=('Courier', 9))
        self._preview_fps_entry.pack(side=tk.RIGHT)

        self._divider(inner)

        # ── DEBUG ────────────────────────────────────
        # Dumps eye-crop frames + a log.csv for offline algorithm tuning —
        # a development tool, not something a clinical operator needs. The
        # widgets are always created (capture start/stop touches them
        # unconditionally below) but only shown when debug mode is on.
        self._debug_active = False
        self._debug_btn = self._flat_btn(
            inner, "⬤  START DEBUG", self._on_debug_toggle,
            CARD, TEXT2, INSET, state=tk.DISABLED)
        self._debug_lbl = tk.Label(inner, text="", bg=PANEL, fg=MUTEDT,
                                    font=('Courier', 8))
        if self.debug:
            self._group_header(inner, "DEBUG")
            self._debug_btn.pack(fill=tk.X, padx=12, pady=(0, 4))
            self._debug_lbl.pack(pady=(0, 10))

        # ── Right sidebar: FLOW (pinned, no scroll) ─
        right = tk.Frame(self.root, bg=PANEL, width=240)
        right.pack(side=tk.RIGHT, fill=tk.Y)
        right.pack_propagate(False)

        self._group_header(right, "FLOW")

        # ── Target position ────────────────────────
        self._section_header(right, "TARGET POSITION")
        self.pos_lbl = tk.Label(right, text="X: 320   Y: 240",
                                 bg=PANEL, fg=CYANL, font=('Courier', 10, 'bold'))
        self.pos_lbl.pack(pady=(2, 8))

        self._divider(right)

        # ── Beam status ────────────────────────────
        beam_outer = tk.Frame(right, bg=BORD, padx=1, pady=1)
        beam_outer.pack(fill=tk.X, padx=12, pady=10)
        self._beam_frame = tk.Frame(beam_outer, bg=CARD, pady=14)
        self._beam_frame.pack(fill=tk.X)
        beam_top = tk.Frame(self._beam_frame, bg=CARD)
        beam_top.pack()
        self._beam_dot = tk.Canvas(beam_top, width=14, height=14, bg=CARD,
                                    highlightthickness=0)
        self._beam_dot.pack(side=tk.LEFT, padx=(0, 8))
        self._beam_oval = self._beam_dot.create_oval(2, 2, 12, 12,
                                                      fill=MUTED, outline='')
        self.beam_lbl = tk.Label(beam_top, text="BEAM OFF",
                                  bg=CARD, fg=MUTEDT,
                                  font=('Helvetica', 13, 'bold'))
        self.beam_lbl.pack(side=tk.LEFT)
        self._beam_sub = tk.Label(self._beam_frame, text="Shutter: Closed",
                                   bg=CARD, fg=MUTEDT, font=('Helvetica', 8))
        self._beam_sub.pack(pady=(4, 0))
        # The run does have a defined end — the beam that was requested, fired
        # to completion — but the gate means the clock on the wall does not
        # measure it. This does.
        self._beam_used_lbl = tk.Label(self._beam_frame, text="beam used  0.0 s",
                                        bg=CARD, fg=MUTEDT,
                                        font=('Courier', 9, 'bold'))
        self._beam_used_lbl.pack(pady=(2, 0))

        # ── Controls ───────────────────────────────
        ctrl = tk.Frame(right, bg=PANEL)
        ctrl.pack(fill=tk.X, padx=12, pady=(4, 2))

        # A checkbox, matching Kcmh-Tricker's Enable exactly rather than a
        # button styled to look like a toggle — the two apps share a control
        # room and should read the same at a glance. tk flips enable_var
        # before calling the command, so _on_enable_toggle reads the state the
        # click already produced rather than the one it is leaving.
        self.enable_var = tk.BooleanVar(value=False)
        self.enable_chk = tk.Checkbutton(
            ctrl, text="  ENABLE", variable=self.enable_var,
            command=self._on_enable_toggle,
            bg=PANEL, fg=TEXT, activebackground=PANEL, activeforeground=TEXT,
            selectcolor=CARD, disabledforeground=MUTEDT,
            font=('Helvetica', 11, 'bold'), anchor='w', padx=10, pady=10,
            cursor='hand2', bd=0, highlightthickness=0)
        self.enable_chk.pack(fill=tk.X, pady=(0, 3))
        tk.Frame(ctrl, bg=BORD, height=1).pack(fill=tk.X)

        # Bringing EUDAQ2 up is its own step, as it is in Kcmh-Tricker ("Launch
        # default"). Folded into START it cost 7.2 s of armed-but-blind beam
        # gating in the last measured run — three transitions that could never
        # be timed. Pressed during setup instead, START has detector data from
        # the very first trigger.
        self.launch_btn = self._flat_btn(ctrl, "▸   LAUNCH DAQ",
                                          self._launch_daq, MUTED, PANEL, MUTED,
                                          state=tk.DISABLED)
        self.launch_btn.pack(fill=tk.X, pady=(3, 3))

        # Emergency fallback. START/STOP normally live inside a run only after
        # LAUNCH, matching Kcmh-Tricker's Run staying hidden until its own
        # Launch default — but ALPIDE is best-effort everywhere else in this
        # app, and gating the beam has to stay reachable when the detector is
        # the thing that is broken (board missing, flash failed, ITS3 tmux
        # fighting Kcmh-Tricker for the same six boards). Deliberately smaller
        # and off to the side, not styled as an alternative to LAUNCH: the
        # ordinary path is always LAUNCH first, and this is one extra,
        # separate click away from the flow that keeps all the data.
        self.start_fallback_btn = self._flat_btn(
            ctrl, "▸  start without DAQ", self.start, CARD, TEXT2, INSET)
        self.start_fallback_btn.config(font=('Helvetica', 8, 'bold'), pady=6)
        self._fallback_shown = False

        # Kcmh-Tricker's Run/Stop/Cancel appear together once Launch default
        # has been pressed; this group does the same. Shown/hidden as a unit
        # in _refresh_flow — CANCEL additionally hides on its own once armed,
        # since STOP is then the only way out of a run.
        self._run_group = tk.Frame(ctrl, bg=PANEL)
        self._group_shown = False

        self.start_btn = self._flat_btn(self._run_group, "▶   START TRACKING",
                                         self.start, MUTED, PANEL, MUTED)
        self.start_btn.pack(fill=tk.X, pady=(0, 3))

        self.stop_btn = self._flat_btn(self._run_group, "■   STOP",
                                        self.stop, CARD, TEXT2, INSET,
                                        state=tk.DISABLED)
        self.stop_btn.pack(fill=tk.X, pady=(0, 3))

        self.cancel_btn = self._flat_btn(self._run_group, "✕   CANCEL DAQ",
                                          self._cancel_daq, CARD, TEXT2, INSET)
        self._cancel_shown = False

        tk.Frame(ctrl, bg=BORD, height=1).pack(fill=tk.X, pady=(3, 0))

        # Always available once Enable is asserted, armed or not — cutting the
        # beam is never the action that needs a precondition. Latching, like
        # Kcmh-Tricker's Kill beam: the eye cannot reopen the shutter until an
        # operator releases it.
        tk.Frame(ctrl, bg=BORD, height=1).pack(fill=tk.X, pady=(6, 0))
        self.kill_btn = self._flat_btn(ctrl, "✕   KILL BEAM",
                                        self._toggle_kill, MUTED, PANEL, MUTED,
                                        state=tk.DISABLED)
        self.kill_btn.pack(fill=tk.X, pady=(6, 0))

        # Shown only while an eye-triggered latch (Manual resume after cut) is
        # tripped — see _refresh_flow. Separate from kill_btn: this releases
        # a cut the eye caused, not one the operator asked for.
        self.resume_btn = self._flat_btn(ctrl, "↻   RESUME BEAM",
                                          self._resume_beam, CARD, TEXT2, INSET)
        self._resume_shown = False

        self._divider(right)

        # ── Record ─────────────────────────────────
        # A setting, not a button pressed every run: checked once, then acted
        # on automatically at LAUNCH (or at the no-DAQ fallback start) — see
        # _maybe_auto_record. Five of the first eight sessions recorded no
        # video because nobody remembered to press a RECORD button, which is
        # exactly the failure mode a checkbox that only has to be set once
        # removes.
        self._section_header(right, "RECORDING  (camera only)")
        self.record_chk = tk.Checkbutton(
            right, text="  Record video (mp4)", variable=self.record_enabled,
            bg=PANEL, fg=TEXT2, activebackground=PANEL, activeforeground=TEXT,
            selectcolor=CARD, disabledforeground=MUTEDT,
            font=('Helvetica', 9, 'bold'), anchor='w', padx=12,
            cursor='hand2', bd=0, highlightthickness=0, state=tk.DISABLED)
        self.record_chk.pack(fill=tk.X, pady=(0, 2))
        self.rec_timer_lbl = tk.Label(right, text="", bg=PANEL, fg=RED_TXT,
                                       font=('Courier', 9, 'bold'))
        self.rec_timer_lbl.pack(pady=(0, 2))
        self.review_btn = self._flat_btn(right, "▶   REVIEW",
                                          self._review_last_recording,
                                          CARD, TEXT2, INSET, state=tk.DISABLED)
        self.review_btn.pack(fill=tk.X, padx=12, pady=(0, 8))

        # ── Camera feed ────────────────────────────
        feed = tk.Frame(self.root, bg=FEED_BG)
        feed.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        # ── ITS3 monitor (bottom strip, collapsed) ─
        # Along the bottom rather than as a column: RunControl draws an 80-column
        # table, which fits across the full width without scrolling and does not
        # fit in any column narrow enough to be worth giving up permanently.
        # Folded away by default — the six plane dots in the sidebar already
        # answer "are all six taking data?", and this is the drill-down for when
        # one of them says otherwise.
        self._its3_open = False
        self.camera_label = tk.Label(feed, bg=FEED_BG, cursor='crosshair')
        self.camera_label.pack(fill=tk.BOTH, expand=True)
        self.camera_label.bind('<Button-1>', self._on_feed_click)

        # place() rather than pack(): packing gave the strip its own row, which
        # squeezed the picture every time it opened. Placed, the sheet lies over
        # the picture instead, so the camera view never resizes or shifts — and
        # a view that jumps whenever a diagnostic is consulted is a bad view to
        # be aiming a proton beam with.
        self._its3_tab = tk.Label(feed, text="▸ ITS3", bg=PANEL, fg=TEXT2,
                                   font=('Helvetica', 8, 'bold'),
                                   padx=10, pady=3, cursor='hand2')
        self._its3_tab.place(relx=0.0, rely=1.0, x=8, y=-6, anchor='sw')
        self._its3_tab.bind('<Button-1>', lambda _e: self._toggle_its3())

        self._its3_body = tk.Frame(feed, bg=PANEL,
                                   highlightthickness=1, highlightbackground=CYANL)
        # Sized in characters, not pixels, and the frame is left to shrink-wrap
        # it — so the sheet comes up exactly as big as RunControl's screen with
        # no empty margin and nothing cut off.
        self._its3_text = tk.Text(self._its3_body,
                                   width=self._ITS3_COLS, height=self._ITS3_LINES,
                                   bg=INSET, fg=TEXT,
                                   font=('Courier', 8), relief='flat',
                                   highlightthickness=0, wrap='none',
                                   padx=6, pady=4, state=tk.DISABLED)
        self._its3_text.pack()
        # Colour is applied here rather than captured from the pane. RunControl
        # does emit ANSI, but reproducing its palette would mean parsing escape
        # codes off an alternate-screen pane to end up highlighting whatever the
        # TUI happened to choose. Tagging the plane states ourselves puts the
        # colour on the thing actually being looked for, and uses the same
        # green/amber/red as the six dots in the sidebar.
        for tag, colour in (('run', GREEN_TXT), ('err', RED_TXT),
                            ('warn', AMBER_TXT), ('name', CYANL),
                            ('chrome', MUTEDT)):
            self._its3_text.tag_configure(tag, foreground=colour)
        self._its3_text.tag_configure('run', font=('Courier', 8, 'bold'))
        self._its3_text.tag_configure('err', font=('Courier', 8, 'bold'))
        self._its3_text.tag_configure('name', font=('Courier', 8, 'bold'))

    # ── Source ─────────────────────────────────────

    def _set_source(self, value):
        if self.input_mode.get() == value:
            return
        if self.capture is not None and self.capture.running:
            self.arduino.send(b'B0\n')
            self._beam_off()
            self._teardown_capture()
            self._refresh_flow()
        self.input_mode.set(value)
        self._update_tabs()
        self._toggle_video_panel()
        if value == 'camera':
            self._open_capture(armed=False)

    def _update_tabs(self):
        cam = self.input_mode.get() == 'camera'
        self._cam_tab.config(
            bg=CYAN if cam else INSET, fg=CYAND if cam else MUTED,
            activebackground=CYANL if cam else CARD,
            activeforeground=CYAND)
        self._vid_tab.config(
            bg=AMBERB if not cam else INSET, fg=AMBERL if not cam else MUTED,
            activebackground=AMBERL if not cam else CARD,
            activeforeground=AMBERB)

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

    # ── Feed click ─────────────────────────────────

    def _on_feed_click(self, event):
        lw = self.camera_label.winfo_width()
        lh = self.camera_label.winfo_height()
        disp_w = int(self._frame_w * self._display_scale)
        disp_h = int(self._frame_h * self._display_scale)
        ox = (lw - disp_w) // 2
        oy = (lh - disp_h) // 2
        # Position within whatever frame is currently on screen — the wide
        # frame normally, or whichever view is main (CaptureThread swaps what
        # it sends; dimensions always stay frame_w x frame_h).
        fx = int((event.x - ox) / self._display_scale)
        fy = int((event.y - oy) / self._display_scale)
        fx = max(0, min(self._frame_w, fx))
        fy = max(0, min(self._frame_h, fy))

        vm = self._last_view_meta
        if vm:
            for inset in vm.get('insets', ()):
                ix1, iy1, ix2, iy2 = inset['rect']
                if ix1 <= fx <= ix2 and iy1 <= fy <= iy2:
                    # clicked a small inset — promote it to the main view,
                    # don't touch the target
                    self._p['main_view'] = inset['view']
                    return
            if vm.get('crop'):
                # clicked the big zoomed view — map back to the real frame position
                cx1, cy1, cx2, cy2 = vm['crop']
                scale = vm['scale']
                fx = int(max(0, min(self._frame_w, cx1 + fx / scale)))
                fy = int(max(0, min(self._frame_h, cy1 + fy / scale)))

        if self.capture and self.capture.armed:
            return
        self.center_x.set(fx)
        self.center_y.set(fy)
        self.pos_lbl.config(text=f"X: {fx:4d}   Y: {fy:4d}")

    # ── Progress ───────────────────────────────────

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

    # ── Recording ──────────────────────────────────

    def _maybe_auto_record(self):
        """Start the mp4 recorder if the operator checked the box.

        Called from both LAUNCH and the no-DAQ fallback start — whichever one
        actually begins a run — rather than from a button pressed mid-session.
        Guarded on _recording so calling it from both places when a run does
        go through LAUNCH is harmless."""
        if (self.input_mode.get() == 'camera' and self.record_enabled.get()
                and self.capture and not self.capture._recording):
            self._start_rec()

    def _start_rec(self):
        ts = time.strftime("%Y%m%d_%H%M%S")
        path = os.path.join(self._session_dir('video'), f"eye_tracking_{ts}.mp4")
        self.capture.start_recording(path)
        self._rec_path = path
        self._rec_start_time = time.time()
        self.review_btn.config(state=tk.DISABLED, bg=PANEL, fg=MUTEDT)
        self._tick_rec()

    def _stop_rec(self):
        """Stop the recorder and hand the clip to REVIEW.

        No confirmation dialog: recording now starts automatically (see
        _maybe_auto_record), so this fires on every STOP/CANCEL rather than
        only when an operator explicitly asked for one — a popup on every run
        would be the wrong trade for what used to be a rare manual action.
        Called from stop() and _cancel_daq(), the only two places a run
        that was recording can end.
        """
        if self.capture:
            self.capture.stop_recording()
        self.rec_timer_lbl.config(text="")
        if self._rec_path:
            self.last_recording_path = self._rec_path
            self.video_path.set(self._rec_path)
            self._rec_path = ''
            self.review_btn.config(state=tk.NORMAL, bg=PANEL, fg=TEXT2,
                                   activebackground=INSET, activeforeground=TEXT)

    def _review_last_recording(self):
        """Open the last recording in the OS default video player. Deliberately
        not routed through the in-app Video tab/pipeline — that stays a
        debug-only tool, this is just a quick look at what was captured."""
        if not self.last_recording_path:
            return
        try:
            subprocess.Popen(['xdg-open', self.last_recording_path])
        except FileNotFoundError:
            self._show_error(
                "Review",
                f"ไม่พบโปรแกรมเปิดวิดีโอ (xdg-open)\nไฟล์อยู่ที่:\n{self.last_recording_path}")

    def _tick_rec(self):
        if not self.capture or not self.capture._recording:
            return
        m, s = divmod(int(time.time() - self._rec_start_time), 60)
        self.rec_timer_lbl.config(text=f"● REC  {m:02d}:{s:02d}")
        self.root.after(1000, self._tick_rec)

    # ── Debug toggle ───────────────────────────────

    def _on_debug_toggle(self):
        if not self.capture or not self.capture.running:
            return
        import datetime
        if not self._debug_active:
            ts = datetime.datetime.now().strftime('%Y%m%d_%H%M%S')
            folder = self._session_dir('debug')
            self.capture._debug_folder   = folder
            self.capture._debug_frame_idx = 0
            self.capture.debug_enabled   = True
            self._debug_active = True
            self._debug_btn.config(text="⬛  STOP DEBUG",
                                   bg=AMBERB, fg=AMBERL,
                                   activebackground=AMBERB, activeforeground=AMBERL)
            self._debug_lbl.config(
                text=f"→ {os.path.basename(self._session_root)}/debug", fg=AMBER_TXT)
        else:
            self.capture.debug_enabled = False
            self._debug_active = False
            self._debug_btn.config(text="⬤  START DEBUG",
                                   bg=PANEL, fg=TEXT2,
                                   activebackground=INSET, activeforeground=TEXT)
            self._debug_lbl.config(text="saved", fg=GREEN_TXT)

    # ── Detection method ───────────────────────────

    def _set_detect(self, method):
        self.detect_method.set(method)
        self._update_detect_tabs()

    def _update_detect_tabs(self):
        fm = self.detect_method.get() == 'facemesh'
        self._fm_tab.config(
            bg=CYAN if fm else INSET, fg=CYAND if fm else MUTED,
            activebackground=CYANL, activeforeground=CYAND)
        self._fp_tab.config(
            bg=CYAN if not fm else INSET, fg=CYAND if not fm else MUTED,
            activebackground=CYANL, activeforeground=CYAND)

    # ── Precision indicator ────────────────────────

    def _update_precision(self, iris_px):
        if iris_px is None or iris_px <= 0:
            self._iris_px_lbl.config(text="— px",     fg=MUTEDT)
            self._precision_lbl.config(text="— mm/px", fg=MUTEDT)
            self._px2mm_lbl.config(text="— px",       fg=MUTEDT)
            return
        mm_per_px   = config.IRIS_MM / iris_px
        px_per_2mm  = 2.0 / mm_per_px
        if mm_per_px < 0.20:
            color, badge = GREEN_TXT, "GOOD"
        elif mm_per_px < 0.40:
            color, badge = CYANL,  "OK"
        elif mm_per_px < 0.60:
            color, badge = AMBER_TXT, "LOW"
        else:
            color, badge = RED_TXT,  "POOR"
        self._iris_px_lbl.config(
            text=f"{iris_px} px", fg=color)
        self._precision_lbl.config(
            text=f"{mm_per_px:.2f} mm/px  [{badge}]", fg=color)
        self._px2mm_lbl.config(
            text=f"{px_per_2mm:.1f} px", fg=color)

    def _update_deviation(self, deviation_mm):
        if deviation_mm is None:
            self._dev_lbl.config(text="— mm", fg=MUTEDT)
            return
        thr = self._p.get('threshold_mm', 3.0)
        if deviation_mm <= thr * 0.5:
            color = GREEN_TXT
        elif deviation_mm <= thr:
            color = AMBER_TXT
        else:
            color = RED_TXT
        self._dev_lbl.config(text=f"{deviation_mm:.2f} mm", fg=color)

    # ── Grayscale cross-check readouts (comparison only) ───────

    def _update_precision_gray(self, iris_px):
        if iris_px is None or iris_px <= 0:
            self._iris_px_gray_lbl.config(text="— px")
            self._precision_gray_lbl.config(text="— mm/px")
            return
        mm_per_px = config.IRIS_MM / iris_px
        self._iris_px_gray_lbl.config(text=f"{iris_px} px")
        self._precision_gray_lbl.config(text=f"{mm_per_px:.2f} mm/px")

    def _update_deviation_gray(self, deviation_mm):
        if deviation_mm is None:
            self._dev_gray_lbl.config(text="— mm", fg=MUTEDT)
            return
        thr = self._p.get('threshold_mm', 3.0)
        if deviation_mm <= thr * 0.5:
            color = GREEN_TXT
        elif deviation_mm <= thr:
            color = AMBER_TXT
        else:
            color = RED_TXT
        self._dev_gray_lbl.config(text=f"{deviation_mm:.2f} mm", fg=color)

    # ── Beam indicator ─────────────────────────────

    def beam_used_s(self):
        """Seconds the shutter has been open this run, the current period
        included. This is the number that compares to the beam that was
        requested at the console; wall clock does not, because every moment the
        eye is off target costs time without costing dose."""
        total = self._beam_on_total
        if self._beam_on_since is not None:
            total += time.monotonic() - self._beam_on_since
        return total

    def _beam_on(self):
        # Both indicator methods are called on every frame, not only on the
        # transition, so the accumulation has to key off _beam_on_since rather
        # than off being called.
        if self._beam_on_since is None:
            self._beam_on_since = time.monotonic()
        self._beam_frame.config(bg=REDB)
        self._beam_dot.config(bg=REDB)
        self._beam_dot.itemconfig(self._beam_oval, fill=REDL)
        self.beam_lbl.config(text="BEAM ACTIVE", bg=REDB, fg=REDL)
        self._beam_sub.config(text="Shutter: Open", bg=REDB, fg=REDL)
        self._beam_used_lbl.config(text="beam used  %.1f s" % self.beam_used_s(),
                                   bg=REDB, fg=REDL)

    def _beam_off(self):
        if self._beam_on_since is not None:
            self._beam_on_total += time.monotonic() - self._beam_on_since
            self._beam_on_since = None
        used = "beam used  %.1f s" % self._beam_on_total
        # Called from the frame loop on every off frame, so the killed state has
        # to be resolved here — set anywhere else it would be overwritten within
        # a frame. "Off because the eye moved" and "off because an operator cut
        # it" need to look different: the second one will not clear by itself.
        if self._killed:
            self._beam_frame.config(bg=REDB)
            self._beam_dot.config(bg=REDB)
            self._beam_dot.itemconfig(self._beam_oval, fill=REDL)
            self.beam_lbl.config(text="BEAM KILLED", bg=REDB, fg=REDL)
            self._beam_sub.config(text="Cut by operator", bg=REDB, fg=REDL)
            self._beam_used_lbl.config(text=used, bg=REDB, fg=REDL)
            return
        self._beam_frame.config(bg=CARD)
        self._beam_dot.config(bg=CARD)
        self._beam_dot.itemconfig(self._beam_oval, fill=MUTED)
        self.beam_lbl.config(text="BEAM OFF", bg=CARD, fg=MUTEDT)
        self._beam_sub.config(text="Shutter: Closed", bg=CARD, fg=MUTEDT)
        self._beam_used_lbl.config(text=used, bg=CARD, fg=MUTEDT)

    # ── Ready check ─────────────────────────────────
    # Precondition gate (Arduino + camera connected) that also drives the DB9
    # Enable line (relay A) — mirrors KCMH-Tricker's Enable checkbox: a human
    # must tick ENABLE before Enable asserts, and unticking it drops it again,
    # instead of Enable being tied permanently high the moment the board has
    # power.

    def _on_enable_toggle(self):
        """Checkbutton command — enable_var already holds the state the click
        just produced, not the one it is leaving (tk flips it before invoking
        the command). A precondition failure or a cancelled dialog reverts the
        checkbox with .set(), which does not re-fire this command."""
        enabling = self.enable_var.get()
        if enabling:
            if not self.arduino.is_connected:
                self._show_warning(
                    "Not connected", "Arduino ยังไม่เชื่อมต่อ กรุณาต่อ Arduino ก่อน")
                self.enable_var.set(False)
                return
            if not self._cam_ok:
                self._show_warning(
                    "Not connected", "กล้องยังไม่พร้อม กรุณาตรวจสอบกล้องก่อน")
                self.enable_var.set(False)
                return
            self.ready = True
            self.arduino.send(b'E1\n')
            self._alpide_prepare()      # flash FX3 images if boards are in DFU
            self._set_launch_state('idle')
        else:
            if self.capture and self.capture.running and self.capture.armed:
                choice = self._confirm_disable_dialog()
                if choice == 'cancel':
                    self.enable_var.set(True)
                    return
                if choice == 'kill':
                    self.arduino.send(b'B0\n')
                self.stop(ended='enable_off')
            self.arduino.send(b'E0\n')
            self.ready = False
            self._release_kill()        # Enable is down; the latch means nothing now
            # Enable coming down ends the session, so anything LAUNCH brought up
            # comes down with it. stop() above only runs for an armed run, and
            # without this a launched-but-never-started run left EUDAQ2, the
            # shared ITS3 tmux session and six powered chips behind — and the
            # next LAUNCH returning silently because _alpide_pid was still set.
            self._daq_teardown()
            self._set_launch_state('locked')

    def _confirm_disable_dialog(self):
        result = {'choice': 'cancel'}
        dlg = tk.Toplevel(self.root)
        dlg.title("Beam check")
        dlg.configure(bg=PANEL)
        dlg.transient(self.root)
        dlg.grab_set()
        tk.Label(dlg, text="Beam status before disabling?",
                 bg=PANEL, fg=TEXT, font=('Helvetica', 10, 'bold'),
                 padx=20, pady=14).pack()
        tk.Label(dlg, text="Press \"Kill beam\" if the beam may still be active.\n"
                            "Press \"Continue\" if the beam has ended.",
                 bg=PANEL, fg=TEXT2, font=('Helvetica', 9), padx=20,
                 justify='left').pack(pady=(0, 10))
        btnf = tk.Frame(dlg, bg=PANEL)
        btnf.pack(pady=(0, 14), padx=14)

        def pick(c):
            result['choice'] = c
            dlg.destroy()

        tk.Button(btnf, text="Kill beam", command=lambda: pick('kill'),
                  bg=REDB, fg=REDL, relief='flat', padx=12, pady=6,
                  font=('Helvetica', 9, 'bold')).pack(side=tk.LEFT, padx=4)
        tk.Button(btnf, text="Continue", command=lambda: pick('continue'),
                  bg=GREENB, fg=GREENL, relief='flat', padx=12, pady=6,
                  font=('Helvetica', 9, 'bold')).pack(side=tk.LEFT, padx=4)
        tk.Button(btnf, text="Cancel", command=lambda: pick('cancel'),
                  bg=AMBERB, fg=AMBERL, relief='flat', padx=12, pady=6,
                  font=('Helvetica', 9, 'bold')).pack(side=tk.LEFT, padx=4)
        dlg.protocol("WM_DELETE_WINDOW", lambda: pick('cancel'))
        dlg.update_idletasks()
        x = self.root.winfo_x() + (self.root.winfo_width()  // 2 - dlg.winfo_width()  // 2)
        y = self.root.winfo_y() + (self.root.winfo_height() // 2 - dlg.winfo_height() // 2)
        dlg.geometry(f"+{x}+{y}")
        dlg.wait_window()
        return result['choice']

    def _ack_dialog(self, title, message, kind):
        """Single-button acknowledgment dialog, styled like the rest of the
        app, replacing tkinter's unthemed messagebox.show*. Same construction
        pattern as _confirm_disable_dialog: transient + grab_set for
        modality, centered on root, WM_DELETE_WINDOW treated the same as
        pressing OK, wait_window() so callers can call it exactly like
        messagebox.show* did.
        """
        head_fg, btn_bg, btn_fg = (
            (RED_TXT, REDB, REDL) if kind == 'error'
            else (AMBER_TXT, AMBERB, AMBERL))
        dlg = tk.Toplevel(self.root)
        dlg.title(title)
        dlg.configure(bg=PANEL)
        dlg.transient(self.root)
        dlg.grab_set()
        tk.Label(dlg, text=title, bg=PANEL, fg=head_fg,
                 font=('Helvetica', 10, 'bold'), padx=20, pady=14).pack()
        tk.Label(dlg, text=message, bg=PANEL, fg=TEXT2, font=('Helvetica', 9),
                 padx=20, justify='left').pack(pady=(0, 10))
        btnf = tk.Frame(dlg, bg=PANEL)
        btnf.pack(pady=(0, 14), padx=14)
        tk.Button(btnf, text="OK", command=dlg.destroy,
                  bg=btn_bg, fg=btn_fg, relief='flat', padx=16, pady=6,
                  font=('Helvetica', 9, 'bold')).pack()
        dlg.protocol("WM_DELETE_WINDOW", dlg.destroy)
        dlg.update_idletasks()
        x = self.root.winfo_x() + (self.root.winfo_width()  // 2 - dlg.winfo_width()  // 2)
        y = self.root.winfo_y() + (self.root.winfo_height() // 2 - dlg.winfo_height() // 2)
        dlg.geometry(f"+{x}+{y}")
        dlg.wait_window()

    def _show_warning(self, title, message):
        self._ack_dialog(title, message, kind='warning')

    def _show_error(self, title, message):
        self._ack_dialog(title, message, kind='error')

    # ── Start / Stop ───────────────────────────────
    # Camera preview (unarmed) and beam tracking (armed) share the same
    # CaptureThread loop — arming only gates whether it may ever send B1 to
    # the Arduino (see CaptureThread.armed). This lets Eye Selection,
    # Threshold, and Target Position all be set against a live picture
    # before ENABLE/START are touched, without the beam ever being reachable.

    def _open_capture(self, armed):
        """Open the camera/video and start CaptureThread. armed=False is a
        preview-only session (beam relay stays gated off); armed=True is a
        real tracking run and requires the caller to have ticked ENABLE."""
        is_video = self.input_mode.get() == 'video'

        if is_video:
            path = self.video_path.get().strip()
            if not path:
                if armed:
                    self._show_error("No File", "Please select a video file first.")
                return
            self.cap = cv2.VideoCapture(path)
            if not self.cap.isOpened():
                if armed:
                    self._show_error("Error", f"Cannot open video:\n{path}")
                self._led_set(self._cam_led, False)
                return
        else:
            self.cap = cv2.VideoCapture(0)
            if not self.cap.isOpened():
                self._led_set(self._cam_led, False)
                self._cam_ok = False
                return
            self._cam_ok = True

        self._video_fps          = self.cap.get(cv2.CAP_PROP_FPS) or 30.0
        self._video_total_frames = int(self.cap.get(cv2.CAP_PROP_FRAME_COUNT))

        self._led_set(self._cam_led, True)
        self.pause_event.set()

        self.capture = CaptureThread(
            cap=self.cap, params=self._p, arduino=self.arduino,
            is_video=is_video, fps=self._video_fps,
            total_frames=self._video_total_frames,
            frame_queue=self.frame_queue, pause_event=self.pause_event,
            on_video_end=lambda: self.root.after(0, self.stop),
            on_progress=self._on_progress,
            armed=armed,
        )
        # A latch set before this thread existed still applies to it: swapping
        # the camera or the source must not quietly hand the beam back.
        self.capture.kill_latched = self._killed
        # Unlike kill_latched, an eye-triggered latch does not carry forward:
        # a fresh run starts unlatched regardless of a prior run's state, and
        # CaptureThread.__init__ already defaults auto_latched to False — this
        # just keeps the UI mirror and the RESUME BEAM button in step with it.
        self._auto_latched = False
        self.capture.start()

        self._refresh_flow()
        self._debug_btn.config(state=tk.NORMAL, bg=PANEL, fg=TEXT2,
                               activebackground=INSET, activeforeground=TEXT)

        if is_video:
            self.prog_slider.config(state=tk.NORMAL)
            self.prog_lbl.config(
                text=f"00:00 / {self._fmt(self._video_total_frames / self._video_fps)}")
            self.record_chk.config(state=tk.DISABLED)
        else:
            self.prog_slider.config(state=tk.DISABLED)
            self.record_chk.config(state=tk.NORMAL)

        self._update_frame()

    def _teardown_capture(self):
        """Fully close the capture thread and camera/video device."""
        if self.capture:
            self.capture.stop_recording()
            self.capture.stop_cut_capture()   # closes cuts.csv on any teardown,
                                              # not only the one that goes via stop()
            self.capture.stop_track_log()     # same reasoning: close track.csv
                                              # on any teardown, not only stop()
            self.capture.stop()
            self.capture = None
        if self.cap:
            self.cap.release()
            self.cap = None
        self.camera_label.config(image='', bg=FEED_BG)
        self._led_set(self._cam_led, False)
        self._update_precision(None)
        self._update_deviation(None)
        self._update_precision_gray(None)
        self._update_deviation_gray(None)
        self.video_progress.set(0)
        self.prog_lbl.config(text="--:-- / --:--")
        # No new capture thread means no more metrics ticks to clear this —
        # do it here so RESUME BEAM never lingers after the run it belonged to.
        self._auto_latched = False
        self._p['main_view'] = 'wide'
        self._last_view_meta = None
        self.prog_slider.config(state=tk.DISABLED)
        self.record_chk.config(state=tk.DISABLED)
        self.rec_timer_lbl.config(text="")
        if self._debug_active:
            self._debug_active = False
        self._debug_btn.config(text="⬤  START DEBUG", state=tk.DISABLED,
                               bg=PANEL, fg=MUTEDT)
        self._debug_lbl.config(text="")

    def _set_config_locked(self, locked):
        """Lock the tracking-config controls (eye side, detection method,
        threshold; target position is gated separately in _on_feed_click)
        while armed — a value silently changing mid-session (stray click,
        bumped slider) would change beam-gating behaviour with no record."""
        state = tk.DISABLED if locked else tk.NORMAL
        for rb in self._eye_radios:
            rb.config(state=state)
        self._fm_tab.config(state=state)
        self._fp_tab.config(state=state)
        self._threshold_scale.config(state=state)
        self._minoff_entry.config(state=state)
        self._dwell_entry.config(state=state)
        self._resume_chk.config(state=state)
        self._gray_chk.config(state=state)
        self._preview_fps_entry.config(state=state)
        self._trig_entry.config(state=state)
        if self.input_mode.get() == 'camera':
            self.record_chk.config(state=state)
        if self.debug:
            self._cam_tab.config(state=state)
            self._vid_tab.config(state=state)

    def start(self):
        if not self.ready:
            self._show_error("Not ready", "กรุณาติ๊ก ENABLE ก่อนเริ่ม tracking")
            return
        # A kill left latched from an earlier run would silently hold the beam
        # off through this one, looking exactly like an eye that never gets on
        # target. Refuse rather than start into that.
        if self._killed:
            self._show_warning(
                "Beam killed",
                "KILL BEAM ค้างอยู่ — กดปล่อยก่อนเริ่ม tracking")
            return
        self._set_config_locked(True)
        self._min_off_value()      # normalise the typed boxes against what is
        self._min_on_target_value()  # actually in force
        self._beam_on_total = 0.0  # beam delivered is counted per run, not per app
        self._beam_on_since = None
        # a fresh folder per run, unless LAUNCH/RECORD/debug already opened one
        if not self._session_root:
            self._session_dir()
        self._session_started = datetime.now().isoformat()
        self._write_session_json()
        # Normally a no-op: LAUNCH already started it. Only the no-DAQ fallback
        # (start_fallback_btn) reaches this without having gone through LAUNCH.
        self._maybe_auto_record()
        # Only the trigger is started here, and only if LAUNCH already brought
        # EUDAQ2 up; T1 resets the board's pulse counter, so it must not fire
        # before the run is RUNNING or the pulse counts and EUDAQ's trigger
        # numbers no longer share an origin.
        self._alpide_start(self._trigger_hz_value())
        self._set_launch_state('locked')
        if self.capture and self.capture.running:
            # preview is already live (camera mode) — just arm the beam relay
            self.capture.armed = True
            self._begin_cut_capture()
            self._begin_track_log()
            self._refresh_flow()
            return
        self._open_capture(armed=True)
        self._begin_cut_capture()
        self._begin_track_log()

    def _begin_cut_capture(self):
        """Save the screen at every beam-off for the whole armed window.

        Tied to arming, not to the RECORD button: five of the first eight
        sessions recorded no video because nobody pressed it, and a beam cut
        with no picture of the eye cannot be explained afterwards. Best-effort
        like the ALPIDE path — a failure here must not stop tracking.
        """
        if not self.capture:
            return
        try:
            self.capture.start_cut_capture(self._session_dir('cuts'))
        except Exception as e:
            self.log(f'[CUTS] not capturing: {type(e).__name__}: {e}')

    def _begin_track_log(self):
        """Write track.csv for the whole armed window: deviation, detection
        result, and gate decision on every frame, not only the ones a beam
        transition happens on — the only record of the run's steady state, and
        of how much of the beam's reaction time is spent on this side of the
        serial link. Best-effort, same as cuts — a failure here must not stop
        tracking."""
        if not self.capture:
            return
        try:
            self.capture.start_track_log(self._session_dir())
        except Exception as e:
            self.log(f'[TRACK] not logging: {type(e).__name__}: {e}')

    def stop(self, ended='stop'):
        """Disarm the beam relay. In camera mode the preview keeps running
        (so the picture never goes blank between runs); video playback fully
        stops, matching the old behaviour.

        `ended` goes straight into session.json — 'stop' unless a caller that
        forced this (a disabled Enable checkbox, an Arduino reset) says
        otherwise, so a run that ended for one of those reasons is not
        indistinguishable from an operator pressing STOP normally.
        """
        # Cutting the beam and dropping the gate are one action, so they happen
        # together and first. Disarming later — after the ALPIDE teardown and
        # the session write, as this used to — leaves the capture loop armed for
        # tens of milliseconds, and its periodic re-statement of the current
        # decision (CaptureThread._BEAM_REFRESH_N) would send B1 again if the
        # eye happened to be on target: the beam coming back on after STOP.
        if self.capture:
            self.capture.armed = False
        self.arduino.send(b'B0\n')
        self._beam_off()
        self._set_config_locked(False)
        self.arduino.send(b'T0\n')
        # Captured before _alpide_stop() can run — session_root is cleared a
        # few lines below, but analyze_latency.py needs the path and this is
        # the only place it is still known once the .raw is actually closed.
        stopped_folder = self._session_root
        self._alpide_stop(on_stopped=lambda: self._auto_analyze(stopped_folder))
        if self._session_root:
            # _beam_off() above closed the last ON period, so this is final
            self._write_session_json(stopped=datetime.now().isoformat(),
                                     beam_on_s=round(self._beam_on_total, 3),
                                     ended=ended)
            self.log(f'[SESSION] {self._session_root}')
            self._session_root = ''    # next run starts a new folder
            self._close_app_log()      # the next one gets a fresh app.log
        # The run is over and its .raw is closed, so the next one has to launch
        # its own — the output path was baked into the EUDAQ2 config.
        self._pending_start_hz = None   # a launch still in flight must not
                                        # start a trigger for a finished run
        self._set_launch_state('idle' if self.ready else 'locked')
        self._release_kill()           # end of run: back to a neutral state
        if self.capture:
            # After disarming, so the queue cannot gain another entry while it
            # is being drained. The beam is already off, so the few milliseconds
            # this waits for the writer cost nothing.
            self.capture.stop_cut_capture()
            self.capture.stop_track_log()

        if self.input_mode.get() == 'video':
            self._teardown_capture()
        elif self.capture and self.capture._recording:
            # preview keeps running, but a run that was being recorded should
            # still end its recording when disarmed — matches the old
            # behaviour where STOP always closed out any active recording.
            self._stop_rec()

        self._refresh_flow()

    # ── Frame display ──────────────────────────────

    def _update_frame(self):
        if not self.capture or not self.capture.running:
            return
        try:
            frame, state, metrics = self.frame_queue.get_nowait()
            if state == 'O':
                self._beam_on()
            else:
                self._beam_off()
            self._update_precision(metrics.get('iris_px'))
            self._update_deviation(metrics.get('deviation_mm'))
            self._update_precision_gray(metrics.get('iris_px_gray'))
            self._update_deviation_gray(metrics.get('deviation_mm_gray'))
            self._last_view_meta = metrics.get('view_meta')
            al = metrics.get('auto_latched', False)
            if al != self._auto_latched:
                self._auto_latched = al
                self._refresh_flow()
            self._eye_on_target = metrics.get('on_target', False)
            self._frame_h, self._frame_w = frame.shape[:2]
            w = max(self.camera_label.winfo_width(),  640)
            h = max(self.camera_label.winfo_height(), 480)
            scale = min(w / self._frame_w, h / self._frame_h)
            self._display_scale = scale
            # Downscale with OpenCV before PIL ever sees the frame. The old
            # path (PIL fromarray on the full frame, then resize with LANCZOS)
            # measured 42 ms on a 1080p camera — more than the 30 ms this
            # callback is scheduled at, so the UI could never catch up and the
            # picture appeared frozen while the process burned CPU. INTER_AREA
            # is 2 ms, and the colour conversion then runs on the small image.
            small = cv2.resize(
                frame, (max(1, int(self._frame_w * scale)),
                        max(1, int(self._frame_h * scale))),
                interpolation=cv2.INTER_AREA)
            tk_img = ImageTk.PhotoImage(
                image=Image.fromarray(cv2.cvtColor(small, cv2.COLOR_BGR2RGB)))
            self.camera_label.imgtk = tk_img
            self.camera_label.config(image=tk_img)
        except queue.Empty:
            pass
        except Exception:
            # Anything unhandled here used to skip the reschedule below and
            # kill the refresh loop for good — the picture froze while the rest
            # of the UI stayed alive, giving no clue why.
            traceback.print_exc()
        finally:
            # The reschedule has to be the one thing that cannot be skipped:
            # anything raising above it stops the frame loop for good and the
            # picture freezes with no other sign. That is exactly what happened
            # when _tick_heartbeat called is_alive() on CaptureThread, which is
            # not a Thread — and only while armed, so it froze the instant
            # START was pressed.
            try:
                self._tick_heartbeat()
            except Exception:
                traceback.print_exc()
            self.root.after(30, self._update_frame)

    # ── Trigger ────────────────────────────────────
    # Runs continuously while armed and is never gated with the beam: if the
    # trigger stopped when the beam did there would be no readout during beam-off
    # and "no protons" would be indistinguishable from "not looking", which is
    # exactly the comparison the measurement rests on.

    _MIN_OFF_MAX_S = 60.0

    def _sync_min_off(self):
        """Push the typed hold into the params the capture thread reads.

        Runs on every keystroke, so a half-typed box ("", "1.") must leave the
        gate on the last good value rather than throwing or resetting it to
        something the operator did not choose.
        """
        try:
            v = float(self.min_off_s.get())
        except (tk.TclError, ValueError):
            return
        self._p['min_off_s'] = max(0.0, min(self._MIN_OFF_MAX_S, v))

    def _sync_min_on_target(self):
        """Same contract as _sync_min_off for the on-target dwell: per
        keystroke, and a half-typed box leaves the last good value in force."""
        try:
            v = float(self.min_on_target_s.get())
        except (tk.TclError, ValueError):
            return
        self._p['min_on_target_s'] = max(0.0, min(self._MIN_OFF_MAX_S, v))

    def _sync_manual_resume(self):
        """Checkbutton callback: push the choice into the params the capture
        thread reads. A discrete per-click callback, unlike min_off_s's
        trace_add, because a Checkbutton has no half-typed intermediate state
        to guard against."""
        self._p['manual_resume'] = self.manual_resume.get()

    def _sync_gray_crosscheck(self):
        """Checkbutton callback. Only takes effect the next time preview
        (re)opens — CaptureThread creates its FaceMesh instances once at
        thread start, not per frame, so flipping this mid-session changes
        nothing until a fresh capture thread exists."""
        self._p['gray_crosscheck'] = self.gray_crosscheck.get()

    _PREVIEW_FPS_MAX = 30

    def _sync_preview_fps(self):
        """Push the typed rate into the params the capture thread reads —
        checked fresh every frame while unarmed (CaptureThread._loop), so
        unlike gray_crosscheck this takes effect immediately. Same half-typed
        guard as _sync_min_off: leave the last good value in force rather
        than throwing or resetting on '' mid-edit."""
        try:
            v = int(self.preview_fps.get())
        except (tk.TclError, ValueError):
            return
        self._p['preview_fps'] = max(1, min(self._PREVIEW_FPS_MAX, v))

    def _min_off_value(self):
        """Read, clamp and write back — called at START so the box agrees with
        what is actually in force, the same way the trigger rate is handled."""
        try:
            v = float(self.min_off_s.get())
        except (tk.TclError, ValueError):
            v = config.DEFAULT_MIN_OFF_S
        v = max(0.0, min(self._MIN_OFF_MAX_S, v))
        self.min_off_s.set(v)
        self._p['min_off_s'] = v
        return v

    def _min_on_target_value(self):
        """The dwell counterpart of _min_off_value, normalised at START for
        the same reason: the box must agree with what is in force."""
        try:
            v = float(self.min_on_target_s.get())
        except (tk.TclError, ValueError):
            v = config.DEFAULT_MIN_ON_TARGET_S
        v = max(0.0, min(self._MIN_OFF_MAX_S, v))
        self.min_on_target_s.set(v)
        self._p['min_on_target_s'] = v
        return v

    def _trigger_hz_value(self):
        """Read and clamp the rate on the UI thread — Tk variables must not be
        touched from the acquisition worker."""
        try:
            hz = int(self.trigger_hz.get())
        except (tk.TclError, ValueError):
            hz = config.TRIGGER_HZ
        hz = max(1, min(95000, hz))
        self.trigger_hz.set(hz)
        return hz

    def _trigger_on(self, hz):
        """Start the readout clock. T1 resets the board's pulse counter, so the
        moment this is called defines pulse 0 — which is why it is deferred
        until the EUDAQ2 run is actually running (see _alpide_start)."""
        self.arduino.send(f'TF{hz}\n'.encode())
        self.arduino.send(f'TD{config.TRIGGER_DUTY}\n'.encode())
        self.arduino.send(b'T1\n')

    # ── Session output folder ──────────────────────
    # One timestamped folder per run holds everything that run produced, split
    # by kind. Reviewing an experiment afterwards is then a single directory
    # rather than three scattered ones that have to be matched up by filename
    # timestamps.
    #
    #   output/session_<ts>/
    #     ├── alpide/        run*.raw + beam_events.csv    (the latency measurement)
    #     ├── cuts/          cut_*.jpg + cuts.csv           (screenshot on every beam-off)
    #     ├── video/         eye_tracking_*.mp4             (RECORD)
    #     ├── debug/
    #     │   ├── log.csv + crop_*.jpg                      (debug mode only)
    #     │   ├── app.log                                   ([ALPIDE]/[BEAM]/[Arduino] lines — see log())
    #     │   └── eudaq.log                                 (RunControl's last screen before STOP kills it)
    #     ├── track.csv      per-frame log for the whole armed window (see capture.py)
    #     ├── session.json   settings + timing + provenance + how the run ended
    #     └── latency.csv, report.txt, analysis.json         (analyze_latency.py, auto-run at STOP)

    def _session_dir(self, sub=None):
        """Path inside the current run's folder, creating it on first use.

        RECORD and debug can both be started outside a START..STOP window, so
        the folder is created on demand rather than only at START — otherwise
        those files would have nowhere to go."""
        if not self._session_root:
            ts = time.strftime('%Y%m%d_%H%M%S')
            self._session_root = os.path.join(
                os.path.dirname(__file__), config.OUTPUT_DIR, f'session_{ts}')
            self._open_app_log()
        path = os.path.join(self._session_root, sub) if sub else self._session_root
        os.makedirs(path, exist_ok=True)
        return path

    # ── App-wide event log ─────────────────────────────────
    # launch.sh does not redirect stdout, so every [ALPIDE]/[BEAM]/[Arduino]
    # print() is invisible when the app is opened from the desktop icon —
    # exactly the runs that most need explaining afterwards. log() replaces
    # print() everywhere in this file (and the modules it drives) so those
    # lines end up in debug/app.log as well as the terminal, when there is one.

    def log(self, msg):
        """print() plus a copy in debug/app.log. Called from worker threads
        (_alpide_note, the Arduino reader) as well as the UI thread, hence the
        lock around the buffer/file — a torn line is worse than a slow one.

        Writes through `self._app_log_file`, which is scoped to whichever
        session is current *right now* — a caller reporting on a session that
        has already ended (_auto_analyze, running well after STOP closed this
        session's handle) will silently miss the file, or worse, land in
        whatever session opens next. See log_to_folder for that case.
        """
        line = f'{datetime.now().isoformat()} {msg}'
        print(msg)
        with self._log_lock:
            self._log_buffer.append(line)
            if self._app_log_file is not None:
                try:
                    self._app_log_file.write(line + '\n')
                    self._app_log_file.flush()
                except Exception:
                    pass

    @staticmethod
    def log_to_folder(folder, msg):
        """Append directly to a specific session's debug/app.log, bypassing
        the current-session file handle log() uses. For callers that know
        exactly which folder they mean and must not depend on it still being
        "the current session" — _auto_analyze runs on a worker thread after
        STOP has already moved on, sometimes well after (analysis can take
        longer than the gap to the next LAUNCH), so log() would either drop
        the line or misfile it into whatever session comes next."""
        line = f'{datetime.now().isoformat()} {msg}'
        print(msg)
        try:
            path = os.path.join(folder, 'debug', 'app.log')
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, 'a') as f:
                f.write(line + '\n')
        except Exception:
            pass

    def _open_app_log(self):
        """Flush the backlog into debug/app.log for this session and keep
        appending. Called once, right after _session_root is assigned — most
        of what explains a bad run (Arduino connect, camera failing to open, a
        firmware flash) happens before LAUNCH, before any folder exists, which
        is why log() buffers from process start rather than only once this is
        open."""
        with self._log_lock:
            try:
                folder = os.path.join(self._session_root, 'debug')
                os.makedirs(folder, exist_ok=True)
                self._app_log_file = open(os.path.join(folder, 'app.log'), 'a')
                for line in self._log_buffer:
                    self._app_log_file.write(line + '\n')
                self._app_log_file.flush()
            except Exception:
                self._app_log_file = None

    def _close_app_log(self):
        """Called wherever _session_root is reset to '' — the next session
        gets its own folder and so must get its own app.log, opened fresh by
        the next _open_app_log() rather than appended to across runs."""
        with self._log_lock:
            f, self._app_log_file = self._app_log_file, None
        if f is not None:
            try:
                f.close()
            except Exception:
                pass

    def _write_session_json(self, **extra):
        """Snapshot of what produced the data. Without this the .raw and the CSV
        are unreadable a week later — nothing else records which eye was
        tracked, where the target was, or what threshold was in force.

        Also carries provenance (which commit, which machine) and how the run
        actually ended — `extra['ended']` is the caller's business (stop(),
        on_close(), the Arduino-reset handler each know their own reason);
        everything else here is read fresh, best-effort, field by field, so
        one failing (no git installed, camera gone) never costs the rest.
        """
        info = {
            'started':        getattr(self, '_session_started', None),
            'eye_side':       self.eye_side.get(),
            'detect_method':  self.detect_method.get(),
            'threshold_mm':   self.threshold_mm.get(),
            'min_off_s':      self._p.get('min_off_s', config.DEFAULT_MIN_OFF_S),
            'min_on_target_s': self._p.get('min_on_target_s',
                                           config.DEFAULT_MIN_ON_TARGET_S),
            'manual_resume':  self._p.get('manual_resume', False),
            'target_x':       self.center_x.get(),
            'target_y':       self.center_y.get(),
            'trigger_hz':     self.trigger_hz.get(),
            'trigger_duty':   config.TRIGGER_DUTY,
            'alpide_num':     config.ALPIDE_NUM,
            'alpide_events':  config.ALPIDE_EVENTS,
            'alpide_strobe':  config.ALPIDE_STROBE,
            'alpide_ithr':    config.ALPIDE_ITHR,
            # None means detector data was really recorded; anything else says
            # why it was not, so a run that produced no .raw says so in its own
            # folder instead of looking like every other run
            'alpide_error':   self._alpide_failed,
        }

        try:
            info['hostname'] = socket.gethostname()
        except Exception:
            pass
        try:
            here = os.path.dirname(__file__)
            commit = subprocess.run(
                ['git', 'rev-parse', '--short', 'HEAD'], cwd=here,
                capture_output=True, text=True, timeout=5).stdout.strip()
            dirty = bool(subprocess.run(
                ['git', 'status', '--porcelain'], cwd=here,
                capture_output=True, text=True, timeout=5).stdout.strip())
            if commit:
                info['git_commit'] = commit
                info['git_dirty'] = dirty
        except Exception:
            pass

        camera = {}
        try:
            if self.cap is not None:
                camera['width']  = int(self.cap.get(cv2.CAP_PROP_FRAME_WIDTH))
                camera['height'] = int(self.cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
            camera['nominal_fps'] = round(self._video_fps, 2)
            # width/height above are the camera's native resolution, not
            # necessarily what detection/recording actually used — this says
            # so, rather than letting a downscaled run look identical to a
            # full-res one.
            camera['capture_downscale'] = self._p.get('capture_downscale', 1.0)
        except Exception:
            pass
        try:
            # Only while track logging is actually live — at LAUNCH, before
            # START, _track_frame_idx is either 0 or a leftover from the
            # previous run and would report a meaningless rate.
            if self.capture is not None and self.capture._track_writer is not None:
                frames = self.capture._track_frame_idx
                started = getattr(self, '_session_started', None)
                if frames and started:
                    elapsed = (datetime.now()
                              - datetime.fromisoformat(started)).total_seconds()
                    if elapsed > 0:
                        info['frames'] = frames
                        camera['achieved_fps'] = round(frames / elapsed, 2)
        except Exception:
            pass
        if camera:
            info['camera'] = camera

        try:
            if self.capture is not None:
                info['cuts_saved']   = self.capture._cut_saved
                info['cuts_dropped'] = self.capture._cut_dropped
        except Exception:
            pass
        try:
            path = os.path.join(self._session_dir('alpide'), 'beam_events.csv')
            with open(path) as f:
                info['beam_transitions'] = max(sum(1 for _ in f) - 1, 0)  # - header
        except Exception:
            pass

        info.update(extra)
        try:
            with open(os.path.join(self._session_dir(), 'session.json'), 'w') as f:
                json.dump(info, f, indent=2)
        except Exception:
            pass

    # ── ALPIDE acquisition ─────────────────────────
    # Every entry point here is best-effort: ALPIDE recording is additive to
    # eye_tracking's job, so nothing in this section may block or delay READY,
    # START or STOP. Failures land in the status line, not in a modal.

    def _alpide_status_tick(self):
        """Poll board presence off the UI thread (lsusb shells out)."""
        def _work():
            try:
                state, msg = alpide_daq.status()
            except Exception as e:
                state, msg = 'missing', f'{type(e).__name__}'
            self.root.after(0, lambda: self._alpide_show(state, msg))
        if not self._alpide_busy:
            threading.Thread(target=_work, daemon=True).start()
        self.root.after(2000, self._alpide_status_tick)

    def _alpide_show(self, state=None, msg=None):
        if state is not None:
            self._alpide_state, self._alpide_board_msg = state, msg
            # Self-healing, matching Kcmh-Tricker's own poll loop
            # (_update_firmware_label calls install_firmware_auto on every
            # tick while unprogrammed, unconditionally — its QTimer starts at
            # widget construction, not at Enable). Boards drop back to DFU
            # whenever they lose power or a run is killed, so flashing has to
            # follow board presence on its own timeline, the same as the LED
            # below does — not wait for the operator to have pressed ENABLE
            # first, which would leave the board sitting unprogrammed (and
            # LAUNCH DAQ refusing) with no visible reason why.
            if state in ('unprogrammed', 'partial'):
                self._alpide_prepare()
        state = getattr(self, '_alpide_state', 'missing')
        msg = getattr(self, '_alpide_board_msg', '—')
        # 'missing' in red rather than muted: Kcmh-Tricker's own "No DAQ
        # found" is red for the same reason — grey reads as "nothing to
        # report", not "nothing is connected".
        colour = {'ready': GREEN_TXT, 'unprogrammed': AMBER_TXT,
                  'partial': AMBER_TXT, 'missing': RED_TXT}.get(state, MUTED)
        run = f'  ·  {self._alpide_msg}' if self._alpide_msg else ''
        self._alpide_lbl.config(text=f'{msg}{run}', fg=colour)
        # SYSTEM STATUS row — same live-before-ENABLE visibility as Arduino/
        # Camera above it, matching Kcmh-Tricker's own ALPIDE connection
        # button: state readable at a glance, with no need to press Enable
        # first just to find out whether the board is even usable.
        c, ov, lbl = self._alpide_sys_led
        c.itemconfig(ov, fill=colour)
        lbl.config(text=msg, fg=colour)
        self._refresh_flow()

    # ── ITS3 monitor ───────────────────────────────
    # RunControl's TUI is the only place the per-plane state exists, and it
    # lives in a tmux pane that nothing else looks at. One capture-pane per tick
    # feeds all three consumers: the plane dots, the terminal, and the log kept
    # for the session.

    def _toggle_its3(self):
        self._its3_open = not self._its3_open
        if self._its3_open:
            # Anchored to the bottom-left corner, sitting just above its own
            # tab: the eye is usually mid-frame, so the corner is the part of
            # the picture least likely to be worth seeing.
            self._its3_body.place(relx=0.0, rely=1.0, x=8, y=-28, anchor='sw')
            self._its3_body.lift()
            self._its3_tab.lift()
            self._its3_tab.config(text="▾ ITS3", bg=CYANL, fg='white')
            self._its3_last = None          # force a redraw on the next tick
        else:
            self._its3_body.place_forget()
            self._its3_tab.config(text="▸ ITS3", bg=PANEL, fg=TEXT2)

    def _its3_tick(self):
        """Same shape as _alpide_status_tick: tmux shells out, so it happens on
        a worker thread and only the widget update runs on the UI thread."""
        def _work():
            text = alpide_daq.pane_text()
            self.root.after(0, lambda: self._its3_show(text))
        threading.Thread(target=_work, daemon=True).start()
        self.root.after(config.ITS3_POLL_MS, self._its3_tick)

    def _its3_show(self, text):
        self._its3_pane = text          # kept for the log written at STOP
        states = alpide_daq.plane_states(text)
        for i, (canvas, oval) in enumerate(self._plane_leds):
            state = states.get(i)
            if state is None:
                colour = MUTED
            elif state == 'RUNNING':
                colour = GREEN_TXT
            elif 'ERROR' in state:
                colour = RED_TXT
            else:
                colour = AMBER_TXT
            canvas.itemconfig(oval, fill=colour)

        # Writing several hundred lines into a Text widget is the expensive part
        # of this, not the poll — so it only happens while the panel is open,
        # and only when the pane actually changed.
        if not self._its3_open or text == getattr(self, '_its3_last', None):
            return
        self._its3_last = text
        body = text.rstrip()
        self._its3_text.config(state=tk.NORMAL)
        self._its3_text.delete('1.0', tk.END)
        self._its3_text.insert('1.0', body)
        self._its3_colourise(body)
        # Top of the screen, pinned to the left margin: RunControl redraws a
        # fixed screen rather than scrolling, so its first line is the start of
        # the table and not stale history. see(END) would scroll sideways to the
        # end of the longest line as well, pushing the plane names off view.
        self._its3_text.yview_moveto(0.0)
        self._its3_text.xview_moveto(0.0)
        self._its3_text.config(state=tk.DISABLED)

    # The states RunControl puts in its connection table.
    _RE_PLANE = re.compile(r'ALPIDE_plane_\d+')
    _RE_STATE = re.compile(
        r'\b(RUNNING|CONFIGURED|STARTING|STOPPED|ERROR|UNINIT|UNCONF|BUSY)\b')
    _STATE_TAG = {'RUNNING': 'run', 'ERROR': 'err'}
    _CHROME = set('│─┌┐└┘├┤┬┴┼█▄▀▌▐ ')

    def _its3_colourise(self, body):
        """Tag plane names and states so the table can be read at a glance."""
        t = self._its3_text
        for tag in ('run', 'err', 'warn', 'name', 'chrome'):
            t.tag_remove(tag, '1.0', tk.END)
        for n, line in enumerate(body.splitlines(), start=1):
            if line and all(ch in self._CHROME for ch in line):
                t.tag_add('chrome', f'{n}.0', f'{n}.end')
                continue
            for m in self._RE_PLANE.finditer(line):
                t.tag_add('name', f'{n}.{m.start()}', f'{n}.{m.end()}')
            for m in self._RE_STATE.finditer(line):
                t.tag_add(self._STATE_TAG.get(m.group(1), 'warn'),
                          f'{n}.{m.start()}', f'{n}.{m.end()}')

    def _save_its3_log(self, folder):
        """Keep what RunControl said, before the session is killed.

        stop_run() tears the tmux session down and its scrollback goes with it,
        which is why a run that failed has so far left no trace of why.
        """
        text = getattr(self, '_its3_pane', '') or alpide_daq.pane_text()
        if not text.strip():
            return
        try:
            os.makedirs(folder, exist_ok=True)
            with open(os.path.join(folder, 'eudaq.log'), 'w') as f:
                f.write(text.rstrip() + '\n')
        except Exception as e:
            self.log(f'[ALPIDE] could not save eudaq.log: {type(e).__name__}: {e}')

    def _alpide_note(self, msg):
        """Called from worker threads, so the label refresh is marshalled back to
        the UI thread — the status poll is paused during a firmware flash, which
        is exactly when progress matters most."""
        self._alpide_msg = msg
        self.log(f'[ALPIDE] {msg}')
        try:
            self.root.after(0, self._alpide_show)
        except Exception:
            pass

    def _alpide_prepare(self):
        """Flash the FX3 images if the boards are sitting in DFU mode. The image
        lives in RAM, so this is a normal per-session step rather than recovery
        — boards fall back to DFU whenever they lose power or a run is killed."""
        if self._alpide_busy:
            return
        try:
            state, _ = alpide_daq.status()
        except Exception:
            return
        if state != 'unprogrammed':
            return
        self._alpide_busy = True
        self._alpide_note('flashing firmware…')
        self._refresh_flow()   # repaint LAUNCH DAQ now, not at the next poll

        def _done(ok):
            self._alpide_busy = False
            self._alpide_note('firmware installed' if ok
                              else 'firmware install FAILED')
            self.root.after(0, self._refresh_flow)

        threading.Thread(
            target=lambda: alpide_daq.install_firmware(on_done=_done),
            daemon=True).start()

    # ── FLOW button appearance ─────────────────────
    # Every button in the FLOW column is painted from one place, by state. The
    # order READY → LAUNCH → START → STOP is numbered on the buttons, and
    # exactly one of them is bright at any moment: the step to press next.
    # Previously each handler configured buttons itself from seventeen separate
    # places, and the colours ended up saying nothing about what came next.

    # bg, fg — by role rather than by button
    _NEXT   = (CYAN,  CYAND)    # the step to take now
    _DANGER = (REDB,  REDL)     # the step to take now, and it stops the beam
    _DONE   = (GREENB, GREENL)  # already done, nothing to do here
    _OPEN   = (CARD,  TEXT)     # allowed, but not the expected next step
    _OFF    = (MUTED, MUTEDT)   # not available yet

    def _paint(self, btn, text, role, enabled):
        bg, fg = role
        btn.config(text=text, bg=bg, fg=fg, activebackground=bg,
                   activeforeground=fg,
                   state=tk.NORMAL if enabled else tk.DISABLED)

    def _refresh_flow(self):
        """Repaint the whole FLOW column from current state. UI thread only."""
        armed = bool(self.capture and self.capture.armed)
        launching = self._launch_state == 'launching'

        # ENABLE — a checkbox, matching Kcmh-Tricker, outside the numbering.
        # Only the colour is set here. enable_var itself is never touched from
        # this method: every path that changes self.ready already keeps it in
        # step (_on_enable_toggle reads the click tk already applied to it;
        # _on_arduino_reset sets it explicitly) — resyncing it here as well
        # would read back whatever it currently holds and could stomp on a
        # toggle still being decided if this ever ran in between.
        self.enable_chk.config(bg=CARD if self.ready else PANEL,
                               fg=GREEN_TXT if self.ready else TEXT,
                               selectcolor=CARD)

        # 1 LAUNCH DAQ
        if not self.ready:
            self._paint(self.launch_btn, "▸   1  LAUNCH DAQ", self._OFF, False)
        elif self._alpide_busy:
            # Same reason 'launching' gets its own disabled label rather than
            # just staying OFF: LAUNCH DAQ would otherwise still be sitting
            # here painted bright/pressable (self.ready is already True) while
            # _launch_daq()'s own guard silently no-ops the click — the exact
            # "is it connected, can I press it yet" confusion this state
            # exists to rule out. Flashing runs on Enable, before there is a
            # folder or a launch sequence to watch, so this is its only cue.
            self._paint(self.launch_btn, "⏳   1  flashing ALPIDE firmware…",
                       self._OPEN, False)
        elif launching:
            self._paint(self.launch_btn, "    1  launching…", self._OPEN, False)
        elif self._daq_ready:
            self._paint(self.launch_btn, "✓   1  DAQ READY", self._DONE, False)
        elif armed:
            self._paint(self.launch_btn, "▸   1  LAUNCH DAQ", self._OFF, False)
        elif getattr(self, '_alpide_state', 'missing') != 'ready':
            # Kcmh-Tricker's Launch refuses the same way ("Unprogrammed DAQs
            # / Please program DAQs") rather than trying and failing deep
            # inside EUDAQ bring-up. _alpide_show already retries the flash
            # on its own every poll while this holds (see there) — nothing to
            # do here but say why LAUNCH DAQ will not go, and 'start without
            # DAQ' below stays open the whole time as the way to run anyway.
            reason = ('no ALPIDE found'
                     if getattr(self, '_alpide_state', 'missing') == 'missing'
                     else 'ALPIDE firmware not ready')
            self._paint(self.launch_btn, f"✕   1  {reason}", self._OFF, False)
        else:
            self._paint(self.launch_btn, "▸   1  LAUNCH DAQ", self._NEXT, True)

        # START / STOP / CANCEL appear together, as a group, only once there is
        # a DAQ session to run them against — matching Kcmh-Tricker's Run/Stop/
        # Cancel staying hidden until Launch default. _alpide_pid is checked
        # alongside the launch state so a bring-up that never reported back
        # (see _launch_watchdog) still keeps CANCEL reachable after the
        # watchdog has already let go of 'launching'.
        show_group = (armed or launching or self._daq_ready
                     or self._alpide_pid is not None)
        if show_group and not self._group_shown:
            self._run_group.pack(fill=tk.X, pady=(0, 3), after=self.launch_btn)
        elif self._group_shown and not show_group:
            self._run_group.pack_forget()
        self._group_shown = show_group

        # The one deliberate exception: ALPIDE is best-effort everywhere else
        # in this app, and gating the beam must stay reachable even with no
        # detector at all — this is what stands in for START before LAUNCH.
        show_fallback = self.ready and not show_group
        if show_fallback and not self._fallback_shown:
            self.start_fallback_btn.pack(fill=tk.X, pady=(2, 3),
                                         after=self.launch_btn)
        elif self._fallback_shown and not show_fallback:
            self.start_fallback_btn.pack_forget()
        self._fallback_shown = show_fallback
        if show_fallback:
            self._paint(self.start_fallback_btn, "▸  start without DAQ",
                       self._OPEN, True)

        # CANCEL — inside the group, but hides on its own once armed: STOP is
        # then the only way out of a run.
        show_cancel = show_group and not armed
        if show_cancel and not self._cancel_shown:
            self.cancel_btn.pack(fill=tk.X, pady=(0, 3), after=self.stop_btn)
        elif self._cancel_shown and not show_cancel:
            self.cancel_btn.pack_forget()
        self._cancel_shown = show_cancel
        if show_cancel:
            self._paint(self.cancel_btn, "✕   CANCEL DAQ", self._OPEN, True)

        # 2 START — bright only once the DAQ is actually up, so the bright
        # button always points at the path that keeps all the data.
        #
        # Held shut while a launch is in flight, the way Kcmh-Tricker holds Run
        # shut until rc.log says StartRun. This costs nothing at the console:
        # the ten seconds are spent during setup, and by the time START has to
        # land within three seconds of Beam On the DAQ has long been up. A
        # launch that fails or never answers releases it again (_launch_done,
        # _launch_watchdog) — the detector must never be able to trap the gate.
        if armed or not self.ready:
            self._paint(self.start_btn, "▶   2  START TRACKING", self._OFF, False)
        elif launching:
            self._paint(self.start_btn, "⏳   2  waiting for ITS3…", self._OFF, False)
        elif self._daq_ready:
            self._paint(self.start_btn, "▶   2  START TRACKING", self._NEXT, True)
        else:
            self._paint(self.start_btn, "▶   2  START TRACKING", self._OPEN, True)

        # 3 STOP — the next step for as long as a run is in progress
        self._paint(self.stop_btn, "■   3  STOP",
                    self._DANGER if armed else self._OFF, armed)

        # KILL BEAM — outside the numbering; available whenever Enable is up
        if self._killed:
            self._paint(self.kill_btn, "✕   KILLED — click to release",
                        self._DANGER, True)
        else:
            self._paint(self.kill_btn, "✕   KILL BEAM",
                        self._DANGER if self.ready else self._OFF, self.ready)

        # RESUME BEAM — outside the numbering, like KILL BEAM; appears only
        # while the eye-triggered latch (Manual resume after cut) is tripped.
        show_resume = self._auto_latched
        if show_resume and not self._resume_shown:
            self.resume_btn.pack(fill=tk.X, pady=(6, 0), after=self.kill_btn)
        elif self._resume_shown and not show_resume:
            self.resume_btn.pack_forget()
        self._resume_shown = show_resume
        if show_resume:
            self._paint(self.resume_btn, "↻   RESUME BEAM", self._DONE, True)

    def _set_launch_state(self, state):
        """UI thread only. 'idle' | 'launching' | 'ready' | 'locked'."""
        self._launch_state = state
        self._daq_ready = (state == 'ready')
        self._refresh_flow()

    # ── Kill beam ──────────────────────────────────
    # Kcmh-Tricker's Kill beam, which is a toggle rather than a one-shot: while
    # it is held the run cannot reopen the shutter. Here the "run" is the eye,
    # so latching is what makes the button mean anything — without it the next
    # frame with the eye on target would undo the cut within ~66 ms.

    def _toggle_kill(self):
        if self._killed:
            self._release_kill()
            return
        self._killed = True
        if self.capture:
            self.capture.kill_latched = True
        # Do not wait for the capture loop to notice: send it here as well, so
        # the shutter closes on the click rather than up to a frame later.
        self.arduino.send(b'B0\n')
        self._beam_off()
        self._refresh_flow()
        self.log('[BEAM] killed by operator')

    def _release_kill(self):
        """Hand the beam back to the eye. Safe to call when nothing is latched."""
        if not self._killed:
            return
        self._killed = False
        if self.capture:
            self.capture.kill_latched = False
        self._refresh_flow()
        # The frame loop repaints this anyway while a capture is running, but
        # not when one is not — repaint here so the panel cannot be left
        # claiming a kill that is no longer in force.
        self._beam_off()
        self.log('[BEAM] kill released')

    def _resume_beam(self):
        """Request that the eye-triggered latch (Manual resume after cut) be
        cleared. Only a request, not a guaranteed clear: the capture thread
        honours it solely on a frame where the eye is presently on target
        (CaptureThread._process_resume_request) — a click while it is still
        off target is discarded there rather than queued, so RESUME BEAM
        cannot arm an unattended reopen for whenever the eye later happens to
        drift back. The button's own visibility follows the next metrics tick
        in _update_frame, not immediately: unlike _toggle_kill, reopening has
        no urgency that would justify skipping ahead of the capture thread.

        Warns rather than silently doing nothing when the eye is off target —
        _process_resume_request would discard the request anyway, and a
        click with no visible effect at all reads as broken, not refused."""
        if not self._eye_on_target:
            self._show_warning(
                "ยังกด resume ไม่ได้",
                "ตายังไม่อยู่ในเป้า — รอให้ตากลับเข้าเป้าก่อนแล้วกดใหม่")
            return
        if self.capture:
            self.capture.resume_requested = True
        self.log('[BEAM] manual resume requested by operator')

    def _alpide_open_dir(self):
        """Bind this run's output folder. Whichever of LAUNCH or START happens
        first calls it — start_run() bakes the path into the EUDAQ2 config, so
        one launch means one folder and one .raw."""
        self._alpide_dir = self._session_dir('alpide')
        self._open_ev_log(self._alpide_dir)

    def _alpide_bring_up(self):
        """Get EUDAQ2 to RUNNING. Returns True if it got there.

        Deliberately stops short of the trigger: T1 resets the board's pulse
        counter and so defines pulse 0, which only lines up with EUDAQ's first
        trigger number if the run is already up. Started earlier the two differ
        by however many pulses were emitted while EUDAQ2 was coming up — about
        six thousand in one measured run.

        Runs on a worker thread in both callers; touches only _alpide_* state.
        """
        state = alpide_daq.session_state()
        if state == 'running':
            # genuinely acquiring — the ITS3 session and the six boards are
            # machine-wide, and racing KCMH-Tricker would corrupt both runs
            self._alpide_note('ITS3 กำลังใช้งานอยู่ — ไม่ได้บันทึก')
            self._alpide_failed = 'ITS3 session busy'
            return False
        if state == 'stale':
            self._alpide_note('เก็บกวาด ITS3 session ที่ค้างอยู่')
            alpide_daq.kill_session()
            time.sleep(2)
        bstate, msg = alpide_daq.status()
        if bstate != 'ready':
            self._alpide_note(f'ไม่ได้บันทึก ({msg})')
            self._alpide_failed = msg
            return False
        self._alpide_pid = alpide_daq.start_run(self._alpide_dir)
        self._alpide_note('acquisition starting…')
        if not alpide_daq.wait_for_running():
            self._alpide_note('run ไม่ขึ้นสถานะ RUNNING — ดู rc.log')
            self._alpide_failed = 'never reached RUNNING'
            return False
        return True

    def _alpide_run_trigger(self, hz):
        """Start the readout clock and confirm data is landing. Worker thread."""
        self._trigger_on(hz)      # defines pulse 0 == first trigN
        if alpide_daq.wait_for_data(self._alpide_dir):
            self._alpide_note(f'recording @ {hz} Hz')
        else:
            self._alpide_note('running but no data — check rc.log')
        self._alpide_failed = None

    def _launch_daq(self):
        """LAUNCH DAQ — Kcmh-Tricker's "Launch default" as its own step.

        Bringing EUDAQ2 up takes ~10 s. Done from inside START, that was 10 s of
        the beam being gated with no detector recording it: 7.2 s and three
        untimeable transitions in the last measured run. Done here, during
        setup, START has data from the first trigger and still arms instantly —
        which is what the three-second START/Beam On rule needs.
        """
        if self._alpide_pid is not None or self._alpide_busy:
            return
        self._alpide_open_dir()
        # so a folder that is launched but never started still describes itself
        self._session_started = datetime.now().isoformat()
        self._write_session_json()
        self._maybe_auto_record()
        self._set_launch_state('launching')
        self._launch_seq += 1
        seq = self._launch_seq
        self.root.after(self._LAUNCH_WATCHDOG_MS,
                        lambda: self._launch_watchdog(seq))

        def _work():
            ok = False
            try:
                ok = self._alpide_bring_up()
            except Exception as e:
                self._alpide_note(f'launch failed: {type(e).__name__}: {e}')
                self._alpide_failed = f'{type(e).__name__}: {e}'
            self.root.after(0, lambda: self._launch_done(ok))

        threading.Thread(target=_work, daemon=True).start()

    def _launch_done(self, ok):
        """UI thread: the launch worker has finished bringing EUDAQ2 up.

        Resolving the deferred start here rather than in the worker keeps every
        read and write of _daq_ready and _pending_start_hz on one thread, so
        START cannot slip in between the worker deciding it is done and the app
        recording that it is.
        """
        self._set_launch_state('ready' if ok else 'idle')
        hz, self._pending_start_hz = self._pending_start_hz, None
        if hz is None:
            if ok:
                self._alpide_note('DAQ พร้อม — รอ START')
                # START only just became pressable, and whoever pressed LAUNCH
                # has spent the last ten seconds looking at something else.
                try:
                    self.root.bell()
                except Exception:
                    pass
            return
        if ok:
            # START was pressed while this was still coming up
            threading.Thread(target=lambda: self._alpide_run_trigger(hz),
                             daemon=True).start()
        else:
            self._alpide_note('DAQ ไม่ขึ้น — ไม่มี trigger สำหรับ run นี้')
            # Keep whatever _alpide_bring_up() diagnosed ('ITS3 session busy',
            # a board message). 'not started' is only the initial value, so it
            # says nothing and is worth replacing with what actually happened.
            if self._alpide_failed in (None, 'not started'):
                self._alpide_failed = 'launch failed, no trigger started'

    # Longer than the worst case _alpide_bring_up() can take by itself
    # (wait_for_running is 40 s, plus a stale-session cleanup and two lsusb
    # polls). This is not a timeout on the launch — the worker is left alone to
    # finish — it only releases the START button if the worker never answers.
    _LAUNCH_WATCHDOG_MS = 60_000

    def _launch_watchdog(self, seq):
        """Give START back if a launch never reported either way.

        Holding START shut while the DAQ comes up is right; holding it shut
        forever because a bring-up wedged is not, because the beam is gated by
        this app and nothing else. If the worker does answer later, _launch_done
        still resolves normally and a START pressed meanwhile is still picked up
        through _pending_start_hz.
        """
        if seq != self._launch_seq or self._launch_state != 'launching':
            return
        self._alpide_note('launch ไม่ตอบใน 60 วิ — START กดได้แล้ว')
        self._set_launch_state('idle')

    def _daq_teardown(self):
        """Stop a EUDAQ2 run that no START will ever use, and release its folder.

        stop() does this for a run that was armed. Nothing did it for a run that
        was launched and then abandoned: _alpide_pid stayed set, and the first
        thing _launch_daq() does is return when it is — so the next LAUNCH did
        nothing at all while its button sat there lit as the step to press.

        Calls _alpide_stop(sync=True), for the same reason on_close() does
        (see its docstring): this call site cannot assume the process stays up
        long enough for a background thread to finish on its own — ENABLE
        coming down or CANCEL DAQ is exactly the kind of moment an operator
        closes the app right after, and a live run on 2026-08-25 did precisely
        that, leaving six orphaned producers for the next LAUNCH to collide
        with. Blocks the caller (all three are UI-thread callbacks) for ~6s
        only when there is a run to tear down — the common case, nothing
        launched, returns immediately and this is a no-op.
        """
        self._alpide_stop(sync=True)    # saves eudaq.log while _session_root lives
        self._session_root = ''         # the next run gets its own folder
        self._close_app_log()           # the next one gets a fresh app.log
        self._pending_start_hz = None

    def _cancel_daq(self):
        """CANCEL DAQ — undo LAUNCH, and only LAUNCH.

        Deliberately touches nothing on the beam side: no relay command, no
        change to Enable, no release of the kill latch. It is the detector that
        is being cancelled, and a button that quietly also moved the beam state
        would be the wrong thing to hand an operator mid-setup. A recording
        LAUNCH started is stopped here too — it is data about a run that is no
        longer happening, not part of the beam-side state.
        """
        if self.capture and self.capture.armed:
            return                      # during a run STOP is the only way out
        self._daq_teardown()
        if self.capture and self.capture._recording:
            self._stop_rec()
        self._alpide_note('launch cancelled')
        self._set_launch_state('idle' if self.ready else 'locked')

    def _alpide_start(self, hz):
        """Start recording for this run, arming regardless of how it goes.

        If LAUNCH already brought EUDAQ2 up, only the trigger is left and data
        begins at pulse 0. Otherwise this still does the whole bring-up itself,
        exactly as before — ALPIDE is additive and must never be a precondition
        for gating the beam, so skipping LAUNCH has to keep working.
        """
        if self._daq_ready:
            threading.Thread(
                target=lambda: self._alpide_run_trigger(hz), daemon=True).start()
            return
        if self._alpide_pid is not None:
            # A LAUNCH is still coming up. Hand the rate to that worker so it
            # starts the trigger the instant the run is RUNNING, rather than
            # returning — returning here skipped T1 for the whole run, leaving
            # the pulse counter at zero and every transition unusable. Measured
            # once for real, on a run that otherwise looked completely normal.
            #
            # START is deliberately still allowed during a launch: the beam arms
            # immediately either way, and the three-second START/Beam On rule
            # matters more than the detector.
            self._pending_start_hz = hz
            self._alpide_note('รอ DAQ ขึ้นก่อนเริ่ม trigger…')
            return
        self._alpide_open_dir()

        def _work():
            try:
                if self._alpide_bring_up():
                    self._alpide_run_trigger(hz)
            except Exception as e:
                self._alpide_note(f'start failed: {type(e).__name__}: {e}')
                self._alpide_failed = f'{type(e).__name__}: {e}'

        threading.Thread(target=_work, daemon=True).start()

    def _alpide_stop(self, on_stopped=None, sync=False):
        """Stop EUDAQ2.

        Two modes, chosen by how certain the caller needs to be that the ITS3
        tmux session is actually gone by the time this returns:

        sync=False (default) fires a daemon thread and returns immediately —
        `on_stopped`, if given, runs on that thread once stop_run() actually
        returns, not when this method does. stop() uses this for the ordinary
        end of a run: STOP is pressed often, and Kcmh-Tricker's eudaq.stop()
        sleeps ~6s before it issues the tmux kill-session, so blocking the UI
        for that on every STOP would be a real cost paid on the common path.
        Nothing here is on the beam-safety path either way — CaptureThread
        gates the beam on its own thread regardless of what this one is doing.

        sync=True calls stop_run() directly on the caller's own thread and
        only returns once the kill-session command has actually been issued —
        the same guarantee Kcmh-Tricker's own UI gets, by the same mechanism:
        Tk (like Qt) is single-threaded, so nothing else this app does,
        including root.destroy(), can run while this call is still blocking
        it. on_close() and _daq_teardown() use this: both are the rare,
        deliberate kind of action (closing the app, ENABLE coming down,
        CANCEL DAQ) rather than the frequent one, so trading a guaranteed
        ~6s pause for a guarantee that holds regardless of what happens the
        instant afterwards is the right side of that trade — a background
        thread here has nothing to protect against a process that might exit
        before it finishes. A live session on 2026-08-25 was exactly that:
        ENABLE came down, the old fire-and-forget call started the ~6s
        sequence, and the app process ended inside that window, cutting the
        daemon thread off before tmux kill-session ever ran and leaving six
        producers orphaned for the next LAUNCH to collide with.

        Returns the worker Thread when sync=False and one was started, else
        None — including the sync=True path, where by the time this returns
        there is nothing left to wait on.
        """
        pid, self._alpide_pid = self._alpide_pid, None
        self._close_ev_log()
        # Before the early return below, not after: a run that never started is
        # exactly the case where the log is worth having. Resolved here rather
        # than inside the thread because stop() clears _session_root as soon as
        # this returns — and only when a run actually opened a folder, since
        # _session_dir() would otherwise create one, leaving an empty session
        # behind every time the app is closed.
        if self._session_root:
            self._save_its3_log(self._session_dir('debug'))
        if pid is None:
            return None
        self._alpide_note('stopping acquisition…')

        def _work():
            alpide_daq.stop_run(pid)
            self._alpide_note('stopped')
            if on_stopped is not None:
                on_stopped()

        if sync:
            _work()
            return None
        thread = threading.Thread(target=_work, daemon=True)
        thread.start()
        return thread

    def _auto_analyze(self, folder):
        """Run analyze_latency.py against the run that just finished, so the
        data health / beam-vs-noise / transition report and report.txt /
        analysis.json exist without anyone having to remember the command.

        Runs on the worker thread _alpide_stop() already has going (the one
        that called stop_run() and is about to call this) — never the UI
        thread, and never before STOP itself has already returned. A
        subprocess rather than an in-process call for the same reason ALPIDE
        acquisition is a subprocess: numpy and the decoder have no business
        in the process that gates the beam, and if the analyzer crashes it
        crashes alone.

        eudaq.stop() already sleeps ~6 s before killing the tmux session
        specifically so RunControl finishes flushing the .raw first — but its
        exception fallback (stop_run()'s except branch) skips that wait
        entirely, so this still checks the file size has stopped moving
        before handing it to the analyzer, up to 5 s, best-effort rather than
        a guarantee.
        """
        if not folder:
            return
        raw_glob = os.path.join(folder, 'alpide', '**', '*.raw')
        try:
            raws = sorted(glob.glob(raw_glob, recursive=True))
        except Exception:
            raws = []
        if raws:
            path = raws[-1]
            last = -1
            for _ in range(5):
                try:
                    size = os.path.getsize(path)
                except OSError:
                    break
                if size == last:
                    break
                last = size
                time.sleep(1.0)

        self._alpide_note('analyzing…')
        script = os.path.join(os.path.dirname(__file__), 'analyze_latency.py')
        try:
            proc = subprocess.run(
                [sys.executable, script, folder, '--quiet'],
                capture_output=True, text=True, timeout=180)
            verdict = self._read_verdict(folder)
            self._alpide_note('analysis: %s' % (verdict or 'see report.txt'))
            # log_to_folder, not log(): by the time this runs, STOP has long
            # since closed this session's app.log (or a new session has
            # opened its own) — log() would drop the line or misfile it into
            # whichever session is current now instead of the one this is
            # actually about.
            self.log_to_folder(folder, '[ANALYZE] exit %d, verdict=%s'
                               % (proc.returncode, verdict))
            if proc.returncode not in (0, 2):   # 2 == no beam signal, expected
                                                # for every run without protons
                self.log_to_folder(folder, '[ANALYZE] stderr: %s'
                                   % proc.stderr.strip())
        except Exception as e:
            self._alpide_note('analysis failed to run: %s: %s'
                              % (type(e).__name__, e))
            self.log_to_folder(folder, '[ANALYZE] failed to run: %s: %s'
                               % (type(e).__name__, e))

        self._auto_plot(folder)

    def _auto_plot(self, folder):
        """Run plot_beam_overlap.py against the run that just finished, so
        plot_overlap.png exists next to report.txt without anyone remembering
        to ask for it.

        Separate try/except from the analyze_latency.py call above rather than
        folded into it: this can fail on its own (too few beam_events rows to
        fit the trigger<->clock bridge, no beam signal at all) on a run where
        the text analysis above succeeded fine, and the two are unrelated
        outcomes that should not be read as one — a run with nothing to plot
        is not a run analyze_latency.py failed on.
        """
        script = os.path.join(os.path.dirname(__file__), 'plot_beam_overlap.py')
        try:
            proc = subprocess.run(
                [sys.executable, script, folder],
                capture_output=True, text=True, timeout=60)
            if proc.returncode == 0:
                self.log_to_folder(folder, '[PLOT] %s'
                                   % (proc.stdout.strip().splitlines()[0]
                                      if proc.stdout.strip() else 'wrote plot_overlap.png'))
            else:
                # Expected on almost every run without real beam signal, or
                # with too few B0/B1 transitions to fit the clock bridge —
                # plot_beam_overlap.py says which on stderr and exits cleanly,
                # so this is a note for the log, not an operator-facing warning.
                msg = proc.stderr.strip().splitlines()[-1] if proc.stderr.strip() else '(no message)'
                self.log_to_folder(folder, '[PLOT] skipped: %s' % msg)
        except Exception as e:
            self.log_to_folder(folder, '[PLOT] failed to run: %s: %s'
                               % (type(e).__name__, e))

    @staticmethod
    def _read_verdict(folder):
        try:
            with open(os.path.join(folder, 'analysis.json')) as f:
                return json.load(f).get('verdict')
        except Exception:
            return None

    # ── Beam/pulse correlation log ─────────────────
    # The board reports the trigger pulse count at each beam transition, and the
    # pulse count is the same index as the ALPIDE event number while nothing is
    # dropped. This CSV is therefore what turns the .raw file into a latency
    # measurement: it says which event number the beam was cut at.

    def _open_ev_log(self, folder):
        self._close_ev_log()
        try:
            os.makedirs(folder, exist_ok=True)
            self._ev_log = open(os.path.join(folder, 'beam_events.csv'), 'w')
            self._ev_log.write(
                'host_time_iso,host_monotonic,event,pulse,trig_running\n')
            self._ev_log.flush()
        except Exception:
            self._ev_log = None

    def _close_ev_log(self):
        if self._ev_log:
            try:
                self._ev_log.close()
            except Exception:
                pass
        self._ev_log = None

    def _on_arduino_line(self, line):
        """Called from the serial reader thread — keep it short and non-blocking."""
        if line == 'READY':
            # The board only prints this in setup(), so seeing it after we are
            # already up means it reset — a power blip, a knocked USB lead. It
            # comes back with the relays dropped and the trigger stopped, and
            # nothing else tells us: the app would keep showing READY and BEAM
            # ACTIVE, keep logging transitions, and produce a run whose pulse
            # counts are all zero. Marshalled to the UI thread because this runs
            # on the serial reader.
            self.root.after(0, self._on_arduino_reset)
            return
        if not line.startswith('EV '):
            self.log(f'[Arduino] {line}')
            return
        parts = line.split()
        if len(parts) != 4:
            return
        _, ev, pulse, running = parts
        if self._ev_log:
            try:
                # trig_running=0 means the pulse column is a leftover count from
                # a previous run and cannot be matched against the .raw
                self._ev_log.write(
                    f'{datetime.now().isoformat()},{time.monotonic():.6f},'
                    f'{ev},{pulse},{running}\n')
                self._ev_log.flush()
            except Exception:
                pass

    def _on_arduino_reset(self):
        """The board restarted underneath us. UI thread.

        Its relays are already in the safe state — that is what a reset does —
        so nothing has to be cut here. What does have to happen is that the app
        stops claiming a state the board no longer holds: Enable is down, the
        trigger is stopped, and the pulse counter that ties the beam log to the
        detector data has gone back to zero, so the rest of this run cannot be
        matched against the .raw whatever else happens.
        """
        if not (self.ready or (self.capture and self.capture.armed)):
            return                       # the ordinary banner at connect time
        self.log('[Arduino] board reset mid-session — dropping to UNREADY')
        self._alpide_failed = 'Arduino reset mid-run (pulse counter lost)'
        if self.capture and self.capture.armed:
            self.stop(ended='arduino_reset')
        self.ready = False
        self.enable_var.set(False)       # the checkbox must not keep claiming Enable
        self.arduino.send(b'E0\n')       # match our state to the board's
        self._release_kill()
        self._daq_teardown()             # a launch left up behind Enable coming down
        self._set_launch_state('locked')
        self._alpide_note('Arduino รีเซ็ต — ติ๊ก ENABLE ใหม่')
        self._show_warning(
            "Arduino reset",
            "บอร์ดรีสตาร์ทระหว่างใช้งาน (ไฟตก/สายหลุด)\n\n"
            "รีเลย์ตกสู่สถานะปลอดภัยแล้ว และตัวนับพัลส์ถูกล้าง — "
            "ข้อมูลของ run นี้เทียบเวลากับ ALPIDE ไม่ได้\n\n"
            "ติ๊ก ENABLE ใหม่ก่อนเริ่ม run ถัดไป")

    # ── Watchdog heartbeat ─────────────────────────
    # The Arduino drops both relays if it hears nothing for WATCHDOG_MS (see
    # eye_tracking_beam.ino), so a crashed or hung app can no longer leave
    # Enable asserted. Re-asserting E1 rather than sending a bare ping keeps it
    # idempotent: if the watchdog ever did trip, the relay comes back to what
    # the app actually intends instead of staying stale.

    _HEARTBEAT_EVERY = 12          # _update_frame ticks (~30 ms each) ≈ 360 ms

    def _tick_heartbeat(self):
        self._hb_count = getattr(self, '_hb_count', 0) + 1
        if self._hb_count % self._HEARTBEAT_EVERY:
            return
        if not self.ready:
            return              # nothing asserted — let the watchdog idle
        # Armed means CaptureThread owns the beam decision. If that thread has
        # died the app can no longer honestly claim the beam state, so stop
        # feeding the watchdog and disarm — dropping the relays is correct.
        if self.capture is not None and self.capture.armed \
                and not self.capture.is_alive():
            self.stop()
            # Not a modal: this runs from the frame loop, and showerror would
            # block that loop until someone clicked it — freezing the very UI
            # it is trying to report on.
            self._alpide_note('capture thread ตาย — disarm และตัดบีมแล้ว')
            return
        self.arduino.send(b'E1\n')

    # ── Close ──────────────────────────────────────

    def on_close(self):
        # Disarm before the relay commands, for the same reason as stop(): an
        # armed capture loop re-states its decision periodically and would
        # otherwise race the B0 below.
        was_armed = bool(self.capture and self.capture.armed)
        if self.capture:
            self.capture.armed = False
        self.arduino.send(b'B0\n')
        self.arduino.send(b'T0\n')
        self.arduino.send(b'E0\n')
        if was_armed and self._session_root:
            # stop() never ran, so nothing recorded how this run actually
            # ended — without this a run the app died in the middle of looks
            # exactly like one that finished normally.
            self._write_session_json(ended='app_closed')
            self.log(f'[SESSION] {self._session_root} — app closed while armed')
        # sync=True here for the same reason _daq_teardown() uses it too (see
        # _alpide_stop's docstring): this is the last chance anything has to
        # run before root.destroy() ends the process outright, and a
        # fire-and-forget thread has no way to finish eudaq.stop()'s ~6s
        # sequence once that happens.
        self._alpide_stop(sync=True)
        self._teardown_capture()
        self._close_app_log()
        self.arduino.close()
        self.root.destroy()
