import threading
import time

try:
    import serial
    SERIAL_AVAILABLE = True
except ImportError:
    SERIAL_AVAILABLE = False


class ArduinoController:
    def __init__(self):
        self._serial = None
        self._lock = threading.Lock()

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
                        on_done(True)
                        return
                s.close()
                on_done(False)
            except Exception:
                on_done(False)
        threading.Thread(target=_work, daemon=True).start()

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
        with self._lock:
            if self._serial and self._serial.is_open:
                try:
                    self._serial.close()
                except Exception:
                    pass
            self._serial = None
