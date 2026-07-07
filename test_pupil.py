"""
Visual debug tool for the FaceMesh + pupil detection pipeline in detection.py.
Runs the same detection code used by capture.py against a video file, so you
can see what capture.py sees without an Arduino or a live camera attached.

Controls:
  Space  — pause / resume
  q      — quit
  r      — restart from beginning
  s      — save current frame as PNG
"""

import sys

import cv2
import mediapipe as mp

import config
import detection

VIDEO_PATH = "video/1.MP4"
SIDE = "left"
THRESHOLD_MM = config.DEFAULT_THRESHOLD_MM


def process(frame, face_mesh, center):
    """Mirrors CaptureThread._detect_mp_pupil, drawing onto frame in-place."""
    results = face_mesh.process(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
    if not results.multi_face_landmarks:
        return "NO FACE", False

    h, w = frame.shape[:2]
    face = results.multi_face_landmarks[0]

    if detection.eye_aspect_ratio(face, h, w, SIDE) < config.EAR_BLINK_THRESHOLD:
        return "BLINK", False

    ix, iy, ir = detection.iris_from_landmarks(face, h, w, SIDE)
    pad = int(ir * 1.5)
    x1, y1 = max(0, ix - pad), max(0, iy - pad)
    x2, y2 = min(w, ix + pad), min(h, iy + pad)
    roi = frame[y1:y2, x1:x2]
    px_r, py_r, pr_r = detection.pupil_in_roi(roi) if roi.size > 0 else (None, None, None)

    if px_r is not None:
        px, py = x1 + px_r, y1 + py_r
        cv2.circle(frame, (ix, iy), ir, (100, 100, 255), 1)
        cv2.circle(frame, (px, py), pr_r, (0, 0, 255), 2)
        d_mm = detection.deviation_mm(px, py, ir, center)
    else:
        cv2.circle(frame, (ix, iy), ir, (0, 0, 255), 2)
        d_mm = detection.deviation_mm(ix, iy, ir, center)

    return f"dev={d_mm:.2f}mm", d_mm <= THRESHOLD_MM


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else VIDEO_PATH
    cap = cv2.VideoCapture(path)
    if not cap.isOpened():
        print(f"Cannot open: {path}")
        return

    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    face_mesh = mp.solutions.face_mesh.FaceMesh(
        max_num_faces=1, refine_landmarks=True,
        min_detection_confidence=0.5, min_tracking_confidence=0.5)

    paused = False
    vis = None
    frame_idx = 0

    try:
        while True:
            if not paused:
                ret, raw = cap.read()
                if not ret:
                    cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
                    continue

                frame_idx = int(cap.get(cv2.CAP_PROP_POS_FRAMES))
                frame = raw.copy()
                h, w = frame.shape[:2]
                center = (w // 2, h // 2)

                status, triggered = process(frame, face_mesh, center)

                if status == "BLINK":
                    bar_color = (200, 100, 0)
                elif status == "NO FACE":
                    bar_color = (60, 60, 200)
                else:
                    bar_color = (0, 180, 0) if triggered else (60, 60, 60)

                cv2.circle(frame, center, 20,
                           (0, 255, 0) if triggered else (200, 200, 200), 2)
                cv2.rectangle(frame, (0, 0), (w, 36), bar_color, -1)
                cv2.putText(frame, status, (10, 25),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255, 255, 255), 2)
                ts = f"{frame_idx/fps:.1f}s / {total/fps:.1f}s  {'[PAUSED]' if paused else ''}"
                cv2.putText(frame, ts, (w - 220, 25),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 255), 1)
                vis = frame

            if vis is not None:
                cv2.imshow("Pupil Detection Test", vis)

            wait = 0 if paused else max(1, int(1000 / fps))
            key = cv2.waitKey(wait) & 0xFF
            if key == ord('q'):
                break
            elif key == ord(' '):
                paused = not paused
            elif key == ord('r'):
                cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
                paused = False
            elif key == ord('s') and vis is not None:
                fname = f"frame_{frame_idx:04d}.png"
                cv2.imwrite(fname, vis)
                print(f"Saved {fname}")
    finally:
        face_mesh.close()
        cap.release()
        cv2.destroyAllWindows()


if __name__ == "__main__":
    main()
