import threading
import time

try:
    import serial
    SERIAL_AVAILABLE = True
except ImportError:
    SERIAL_AVAILABLE = False


class ArduinoController:
    """Serial link to the relay/trigger board.

    Writes are fire-and-forget under a lock. Reads run in their own thread
    because the board reports beam transitions unprompted ("EV B1 <pulses>")
    and those replies are what tie the beam state to a position in the ALPIDE
    data — see the trigger pulse counter in eye_tracking_beam.ino. Nothing is
    read on the write path, so a missed reply can never stall a beam command.
    """

    def __init__(self, on_line=None):
        self._serial = None
        self._lock = threading.Lock()
        self._on_line = on_line
        self._reader = None
        self._reading = False

    def connect_async(self, port: str, baud: int, on_done):
        """Connect in a background thread; call on_done(ok: bool) on completion."""
        def _work():
            if not SERIAL_AVAILABLE:
                on_done(False)
                return
            try:
                s = serial.Serial(port, baud, timeout=3)
                deadline = time.time() + 5
                while time.time() < deadline:
                    line = s.readline().decode('ascii', errors='ignore').strip()
                    if line == 'READY':
                        with self._lock:
                            self._serial = s
                        self._start_reader()
                        on_done(True)
                        return
                s.close()
                on_done(False)
            except Exception:
                on_done(False)
        threading.Thread(target=_work, daemon=True).start()

    def _start_reader(self):
        if self._on_line is None or self._reading:
            return
        self._reading = True

        def _read():
            while self._reading:
                ser = self._serial
                if ser is None or not ser.is_open:
                    break
                try:
                    raw = ser.readline()          # no lock: reads must never
                except Exception:                 # block a beam command write
                    break
                line = raw.decode('ascii', errors='ignore').strip()
                if not line:
                    continue                      # readline timeout
                try:
                    self._on_line(line)
                except Exception:
                    pass
            self._reading = False

        self._reader = threading.Thread(target=_read, daemon=True)
        self._reader.start()

    def send(self, msg: bytes):
        with self._lock:
            if self._serial and self._serial.is_open:
                try:
                    self._serial.write(msg)
                except Exception:
                    pass

    @property
    def is_connected(self):
        with self._lock:
            return bool(self._serial and self._serial.is_open)

    def close(self):
        self._reading = False
        with self._lock:
            if self._serial and self._serial.is_open:
                try:
                    self._serial.close()
                except Exception:
                    pass
            self._serial = None
