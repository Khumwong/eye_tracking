"""Pure eye/pupil detection math — no threading, no hardware, no tkinter.

Kept separate from capture.py so the detection geometry can be unit-tested
against still images without a camera, MediaPipe runtime, or Arduino attached.
"""
import cv2
import math
import numpy as np

from config import IRIS_MM

_LEFT_IRIS_CENTER, _RIGHT_IRIS_CENTER = 468, 473
_LEFT_IRIS_RING  = [469, 470, 471, 472]
_RIGHT_IRIS_RING = [474, 475, 476, 477]
_LEFT_EAR_IDX  = [362, 385, 387, 263, 373, 380]
_RIGHT_EAR_IDX = [33, 160, 158, 133, 153, 144]


def iris_from_landmarks(lms, h, w, side):
    """Return (cx, cy, r) of the iris in pixel coords from a MediaPipe FaceMesh face."""
    ci = _LEFT_IRIS_CENTER if side == 'left' else _RIGHT_IRIS_CENTER
    bi = _LEFT_IRIS_RING if side == 'left' else _RIGHT_IRIS_RING
    lm = lms.landmark
    cx = int(lm[ci].x * w)
    cy = int(lm[ci].y * h)
    r = int(np.mean([math.hypot(lm[i].x * w - cx, lm[i].y * h - cy) for i in bi]))
    return cx, cy, max(r, 1)


def eye_aspect_ratio(lms, h, w, side):
    """Eye Aspect Ratio — drops sharply during a blink."""
    idx = _LEFT_EAR_IDX if side == 'left' else _RIGHT_EAR_IDX
    lm = lms.landmark
    pts = [(lm[i].x * w, lm[i].y * h) for i in idx]
    v1 = math.hypot(pts[1][0] - pts[5][0], pts[1][1] - pts[5][1])
    v2 = math.hypot(pts[2][0] - pts[4][0], pts[2][1] - pts[4][1])
    hz = math.hypot(pts[0][0] - pts[3][0], pts[0][1] - pts[3][1])
    return (v1 + v2) / (2.0 * hz) if hz > 0 else 0.0


def pupil_in_roi(roi):
    """Find the dark pupil in a pre-cropped iris ROI. Returns (cx, cy, r) or (None, None, None)."""
    gray = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY)
    refl = cv2.threshold(gray, 230, 255, cv2.THRESH_BINARY)[1]
    refl = cv2.dilate(refl, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3)))
    if cv2.countNonZero(refl) > 0:
        gray = cv2.inpaint(gray, refl, 5, cv2.INPAINT_TELEA)
    gray = cv2.GaussianBlur(gray, (7, 7), 0)
    _, thresh = cv2.threshold(gray, 40, 255, cv2.THRESH_BINARY_INV)
    k_close = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5))
    k_open  = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))
    thresh = cv2.morphologyEx(thresh, cv2.MORPH_CLOSE, k_close)
    thresh = cv2.morphologyEx(thresh, cv2.MORPH_OPEN,  k_open)
    cnts, _ = cv2.findContours(thresh, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not cnts:
        return None, None, None
    rh, rw = roi.shape[:2]

    def score(c):
        area = cv2.contourArea(c)
        if area < 30 or area > rw * rh * 0.6:
            return -1
        peri = cv2.arcLength(c, True)
        circ = (4 * math.pi * area / (peri * peri)) if peri > 0 else 0
        return area * circ

    best = max(cnts, key=score)
    if score(best) < 0:
        return None, None, None
    _, _, bw, bh = cv2.boundingRect(best)
    if bw > 0 and (bh / bw) < 0.4:
        return None, None, None
    M = cv2.moments(best)
    if M["m00"] == 0:
        return None, None, None
    px = int(M["m10"] / M["m00"])
    py = int(M["m01"] / M["m00"])
    pr = int(math.sqrt(cv2.contourArea(best) / math.pi))
    return px, py, max(pr, 1)


def deviation_mm(ex, ey, ir, center, iris_mm=IRIS_MM):
    """Pixel distance from center converted to mm, using iris radius as the px→mm scale."""
    d_px = math.hypot(ex - center[0], ey - center[1])
    return d_px * (iris_mm / (ir * 2)) if ir > 0 else float('inf')
