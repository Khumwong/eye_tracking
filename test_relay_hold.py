"""Bench tool: hold one DB9 state indefinitely so it can be probed with a
multimeter. test_relay.py cycles every 1.5 s, which is too fast to check
several pin pairs — this one waits for Enter between states instead.

ถอด DB9 ออกจาก TSS ก่อนใช้เสมอ (continuity mode จ่ายกระแสออกไป)
"""
import serial
import sys
import threading
import time

PORT = '/dev/ttyUSB1'   # Arduino = CH340 (1a86:7523). ttyUSB0 คือ Zaber — อย่าสับสน
BAUD = 9600

STATES = {
    '1': ("Beam OFF, Enable OFF  (boot state)", [b'B0\n', b'E0\n'],
          {'1-3': 'beep', '1-6': 'beep', '3-6': 'beep',
           '4-6': '-',    '1-4': '-',    '3-4': '-'}),
    '2': ("Beam OFF, Enable ON",                [b'E1\n', b'B0\n'],
          {'1-3': 'beep', '1-6': '-',    '3-6': '-',
           '4-6': 'beep', '1-4': '-',    '3-4': '-'}),
    '3': ("Beam ON,  Enable ON",                [b'E1\n', b'B1\n'],
          {'1-3': '-',    '1-6': 'beep', '3-6': '-',
           '4-6': 'beep', '1-4': 'beep', '3-4': '-'}),
}


def show(label, expect):
    print(f"\n  >>> {label}")
    print("      expected continuity:")
    for pair, want in expect.items():
        mark = '🔊 beep' if want == 'beep' else '   (silent)'
        print(f"        pin {pair:5s} {mark}")


def start_heartbeat(ser, lock):
    """The firmware drops the relays if it hears nothing for ~2 s, so a state
    held open across an input() wait needs a heartbeat or it would silently
    disappear mid-measurement."""
    def _beat():
        while True:
            with lock:
                try:
                    ser.write(b'H\n')
                except Exception:
                    return
            time.sleep(0.5)
    threading.Thread(target=_beat, daemon=True).start()


def main():
    print(f"Connecting to {PORT}...")
    try:
        s = serial.Serial(PORT, BAUD, timeout=3)
    except Exception as e:
        print(f"Failed to connect: {e}")
        return 1
    line = s.readline().decode('ascii', errors='ignore').strip()
    print("Arduino READY\n" if line == 'READY' else f"Got: '{line}' (continuing anyway)\n")

    lock = threading.Lock()
    start_heartbeat(s, lock)

    print("ถอด DB9 ออกจาก TSS ก่อนวัด — multimeter โหมด continuity")
    try:
        while True:
            print("\n" + "=" * 52)
            for key, (label, _, _) in STATES.items():
                print(f"  [{key}] {label}")
            print("  [q] quit (returns to Beam OFF / Enable OFF)")
            choice = input("  เลือก state> ").strip().lower()

            if choice == 'q':
                break
            if choice not in STATES:
                print("  ?")
                continue

            label, cmds, expect = STATES[choice]
            with lock:
                for c in cmds:
                    s.write(c)
            show(label, expect)
            input("\n      วัดให้เสร็จแล้วกด Enter เพื่อกลับไปเลือก state ถัดไป...")
    except (KeyboardInterrupt, EOFError):
        print()
    finally:
        # always leave the board in the safe state
        with lock:
            s.write(b'B0\n')
            s.write(b'E0\n')
        s.close()
        print("\nกลับสู่ Beam OFF / Enable OFF แล้ว — ปิด port เรียบร้อย")
    return 0


if __name__ == '__main__':
    sys.exit(main())
