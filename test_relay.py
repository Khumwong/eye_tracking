import serial
import threading
import time

PORT = '/dev/ttyUSB1'
BAUD = 9600
CYCLES = 5

print(f"Connecting to {PORT}...")
try:
    s = serial.Serial(PORT, BAUD, timeout=3)
    line = s.readline().decode('ascii', errors='ignore').strip()
    if line == 'READY':
        print("Arduino READY\n")
    else:
        print(f"Got: '{line}' (continuing anyway)\n")
except Exception as e:
    print(f"Failed to connect: {e}")
    exit(1)

# Each step below sits just inside the firmware's ~2 s relay watchdog; a
# heartbeat makes that margin explicit instead of relying on the sleep length.
def _beat():
    while True:
        try:
            s.write(b'H\n')
        except Exception:
            return
        time.sleep(0.5)
threading.Thread(target=_beat, daemon=True).start()

# วัดที่ DB9 pin 1/3/4/6 ด้วย multimeter เทียบกับ truth table ในเอกสาร TSS ทุกขั้น
print("[BOOT STATE]  ควรเป็น Beam OFF, Enable OFF  (pin 1+3+6 short กัน)")
time.sleep(1.5)

print("ENABLE ON  *** RELAY A SHOULD CLICK ***  ควรเป็น {1+3} และ {4+6} แยกกัน")
s.write(b'E1\n')
time.sleep(1.5)
print()

for i in range(CYCLES):
    print(f"[{i+1}/{CYCLES}]  BEAM ON  *** RELAY B SHOULD CLICK ***  ควรเป็น 1+4+6 รวมกัน")
    s.write(b'B1\n')
    time.sleep(1.5)

    print(f"[{i+1}/{CYCLES}]  BEAM OFF *** RELAY B SHOULD CLICK ***  ควรกลับเป็น {{1+3}} และ {{4+6}} แยกกัน")
    s.write(b'B0\n')
    time.sleep(1.5)
    print()

print("ENABLE OFF *** RELAY A SHOULD CLICK ***  ควรกลับเป็น 1+3+6 short กัน (เหมือน boot state)")
s.write(b'E0\n')
time.sleep(1.5)

s.close()
print("Test complete.")
