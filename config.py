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

# ── Target overlay ───────────────────────────────────
TARGET_MARK_PX   = 8   # half-length of the fixed crosshair at the target point
DETECTED_MARK_PX = 3   # radius of the dot marking the measured eye position

# ── Arduino ──────────────────────────────────────────
ARDUINO_BAUD = 9600
ARDUINO_VID  = 0x1A86   # CH340
ARDUINO_PID  = 0x7523

# ── Clinical color palette ───────────────────────────
BG     = '#0D1B2A'
PANEL  = '#112032'
CARD   = '#182D41'
INSET  = '#091525'
BORD   = '#1C3A56'
CYAN   = '#00B4D8'
CYAND  = '#0077B6'
CYANL  = '#90E0EF'
TEXT   = '#D0E8F2'
TEXT2  = '#5D7E98'
MUTED  = '#2A4058'
GREENB = '#054D26'
GREENL = '#2DCE89'
REDB   = '#5C0B07'
REDL   = '#F26157'
AMBERB = '#7A4500'
AMBERL = '#FFB700'
