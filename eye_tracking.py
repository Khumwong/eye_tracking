import cv2
import numpy as np
import serial
import time

# ===== Serial to Arduino =====
USE_SERIAL = True
ARDUINO_PORT = '/dev/ttyUSB0'   # Linux
# ARDUINO_PORT = 'COM4'   # Windows
BAUDRATE = 9600
arduino = None
if USE_SERIAL:
    try:
        arduino = serial.Serial(ARDUINO_PORT, BAUDRATE, timeout=1)
        time.sleep(2)
        print(f"Connected to Arduino on {ARDUINO_PORT}")
    except Exception as e:
        print(f"Cannot connect to Arduino: {e}\n(Running without Arduino signal)")

def send_to_arduino(msg: bytes):
    if arduino and arduino.is_open:
        try:
            arduino.write(msg)
        except Exception as e:
            print(f"Cannot send to Arduino: {e}")

# ===== Pupil detection logic =====
def is_inside_circle(center, radius, point):
    return (point[0] - center[0])**2 + (point[1] - center[1])**2 <= radius**2

circle_center = (320, 240)
circle_radius  = 50

cap = cv2.VideoCapture(0)
if not cap.isOpened():
    print("Error: Could not open webcam.")
    raise SystemExit

current_state = 'S'
send_to_arduino(b'B0\n')

while True:
    ret, frame = cap.read()
    if not ret:
        print("Error: Failed to capture frame.")
        break

    gray     = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    inverted = cv2.bitwise_not(gray)
    blurred  = cv2.GaussianBlur(inverted, (9, 9), 0)

    circles = cv2.HoughCircles(
        blurred, cv2.HOUGH_GRADIENT,
        dp=1.2, minDist=50, param1=50, param2=30,
        minRadius=10, maxRadius=20
    )

    cv2.circle(frame, circle_center, circle_radius, (0, 255, 0), 2)

    trigger_open = False
    if circles is not None:
        circles = np.round(circles[0, :]).astype("int")
        for (x, y, r) in circles:
            cv2.circle(frame, (x, y), r, (0, 0, 255), 2)
            if is_inside_circle(circle_center, circle_radius, (x, y)):
                trigger_open = True
                break

    if trigger_open and current_state != 'O':
        send_to_arduino(b'B1\n')
        current_state = 'O'
        print("Open -> sent 'B1' (Beam ON)")
    elif (not trigger_open) and current_state != 'S':
        send_to_arduino(b'B0\n')
        current_state = 'S'
        print("Searching -> sent 'B0' (Beam OFF)")

    if current_state == 'O':
        cv2.putText(frame, "Open the shutter!", (10, 30),
                    cv2.FONT_HERSHEY_SIMPLEX, 1, (0, 0, 255), 2)
    else:
        cv2.putText(frame, "Searching...", (10, 30),
                    cv2.FONT_HERSHEY_SIMPLEX, 1, (0, 200, 0), 2)

    cv2.imshow("Pupil Tracker", frame)
    if cv2.waitKey(1) & 0xFF == ord('q'):
        break

cap.release()
cv2.destroyAllWindows()
if arduino and arduino.is_open:
    arduino.close()
