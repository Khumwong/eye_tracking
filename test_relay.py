import serial
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

for i in range(CYCLES):
    print(f"[{i+1}/{CYCLES}]  BEAM ON  *** RELAY SHOULD CLICK ***")
    s.write(b'B1\n')
    time.sleep(1.5)

    print(f"[{i+1}/{CYCLES}]  BEAM OFF *** RELAY SHOULD CLICK ***")
    s.write(b'B0\n')
    time.sleep(1.5)
    print()

s.close()
print("Test complete.")
