"""Tunable constants and the UI color palette, kept in one place.

Detection/hardware constants here are read by capture.py, detection.py, and
app.py so a value only needs to change in one spot instead of being hunted
down across files.
"""

# ── Detection tuning ─────────────────────────────────
IRIS_MM             = 11.0   # average human iris diameter (mm), used for px→mm conversion
EAR_BLINK_THRESHOLD = 0.20   # eye aspect ratio below this = eyes closed (blink)

# ── Defaults (initial UI state / CaptureThread params) ──
DEFAULT_CENTER       = (320, 240)
DEFAULT_THRESHOLD_MM = 3.0

# Once the beam has been cut, hold it off for at least this long before the eye
# is allowed to reopen it. An eye sitting exactly on the threshold otherwise
# chatters the shutter: 35 cuts in 26 s measured at 3.0 mm, and 19 off-periods
# shorter than 100 ms in 9 s in an earlier run.
#
# This only ever delays turning the beam ON — turning it off stays immediate, so
# the safety-critical path is untouched and the hold can only err towards less
# beam. It also does not affect either latency measurement: it delays when the
# B1 command is issued, not how fast the hardware responds to it.
#
# It is a trade rather than a fix, and the cost is beam duty cycle: measured at
# 92% with no hold, 41% at 1 s, and 14% at 5 s. The dose has to reach the same
# MU regardless, so a low duty cycle is paid for in how long the patient is held
# in position — which is why the hold is not simply set as long as possible.
#
# 1.0 s absorbs every sub-second off-period seen so far while keeping the duty
# cycle usable. Set 0 to disable. The value that should actually be used is a
# clinical judgement, not this default — see next.md.
DEFAULT_MIN_OFF_S = 1.0

# ── Target overlay ───────────────────────────────────
TARGET_MARK_PX   = 8   # half-length of the fixed crosshair at the target point
DETECTED_MARK_PX = 3   # radius of the dot marking the measured eye position

# ── Output ───────────────────────────────────────────
# Everything a session produces lands under one timestamped folder here, so
# reviewing an experiment afterwards means opening one directory rather than
# hunting through three.
OUTPUT_DIR = 'output'

# ── ALPIDE / EUDAQ2 acquisition ──────────────────────
# Kept here rather than in the UI: these change per experiment campaign, not per
# session, and putting them on screen invites fiddling mid-treatment.
KCMH_TRICKER_DIR = '/home/kobdaj/Kcmh-Tricker'   # owns EUDAQ2 config gen + firmware images
EUDAQ_DIR        = '/home/kobdaj/eudaq2/user/ITS3/misc/'
EUDAQ_LIB        = '/home/kobdaj/eudaq2/lib'   # holds pyeudaq.so, used by analyze_latency.py
ALPIDE_NUM       = 6
ALPIDE_EVENTS    = 500000   # RunControl stops the run at this many events
ALPIDE_STROBE    = 100
ALPIDE_ITHR      = 60

# Readout clock on D7 -> J1. 1 kHz gives 1 ms timing resolution, which is well
# under the tens of ms of gating latency being measured, and sustains far longer
# than 9500 Hz — where the six planes desync within about a second because the
# readout cannot keep up. The latency measurement relies on ALPIDE event index
# matching the trigger pulse count, which only holds while nothing is dropped.
TRIGGER_HZ   = 1000
TRIGGER_DUTY = 50

# ── ITS3 monitor ─────────────────────────────────────
# The EUDAQ2 RunControl TUI lives in a tmux pane and is otherwise invisible.
# Only the poll rate is tunable: what gets captured is deliberately the pane's
# current screen, since RunControl redraws a table in place rather than
# scrolling, and asking tmux for history on an alternate-screen pane returns
# whatever preceded the TUI instead of the table.
ITS3_POLL_MS = 1000

# ── Beam-cut snapshots ───────────────────────────────
# One image per beam-off transition, so a run always carries a picture of why
# the beam stopped even when nobody pressed RECORD.
CUT_QUEUE_MAX       = 8     # frames waiting to be written; over this they are dropped
CUT_JPEG_QUALITY    = 85
CUT_MAX_PER_SESSION = 500   # a flickering threshold must not fill the disk

# ── Arduino ──────────────────────────────────────────
ARDUINO_BAUD = 9600
ARDUINO_VID  = 0x1A86   # CH340
ARDUINO_PID  = 0x7523

# ── Clinical color palette ───────────────────────────
# Matches Kcmh-Tricker's light theme (its GLOBAL_STYLE in main.py) so the two
# applications in the same control room do not look like different products.
#
# Two families, and they are not interchangeable:
#   *B / *L  — a solid coloured badge or button, and the text that goes ON it.
#   *_TXT    — the same colour used as text on a pale surface.
# Under the old dark theme one value served both; on a light background it
# cannot, because text on white has to be dark and text on a saturated button
# has to be light.
BG     = '#EEF2F7'   # window
PANEL  = '#FFFFFF'   # sidebars and the FLOW column
CARD   = '#E8EEF7'   # raised card on a panel
INSET  = '#DCE4EF'   # recessed: entries, group headers, the ITS3 terminal
BORD   = '#C0CFE0'
CYAN   = '#1565C0'   # primary action (button fill)
CYAND  = '#FFFFFF'   # text on primary
CYANL  = '#1E3A5F'   # navy, for section headings on a pale surface
TEXT   = '#1E2D3D'
TEXT2  = '#5A6B7D'
MUTED  = '#BCC8D8'   # disabled fill
MUTEDT = '#7D8CA0'   # text on a disabled fill
GREENB = '#2E7D32'
GREENL = '#FFFFFF'
REDB   = '#C62828'
REDL   = '#FFFFFF'
AMBERB = '#F9A825'
AMBERL = '#3D2F00'   # amber is too light to carry white text
GREEN_TXT = '#2E7D32'
RED_TXT   = '#C62828'
AMBER_TXT = '#B26A00'
FEED_BG   = '#33404F'   # letterbox around the camera picture
HDR_SUB   = '#A8C4E0'   # subtitle on the navy header
