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
                s = serial.Serial(port, baud, timeout=1)
                time.sleep(2)
                with self._lock:
                    self._serial = s
                on_done(True)
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

    def close(self):
        with self._lock:
            if self._serial and self._serial.is_open:
                try:
                    self._serial.close()
                except Exception:
                    pass
            self._serial = None
