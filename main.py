import faulthandler
import os
import signal
import sys
import tkinter as tk
from app import EyeTrackingApp

# If the UI ever freezes there is otherwise nothing to go on: the window stops
# repainting and the process gives no clue which thread is stuck. SIGUSR1 dumps
# every thread's stack to stderr without disturbing the process, so a live hang
# can be diagnosed instead of guessed at:
#
#     kill -USR1 $(pgrep -f "python.*main.py")
#
# faulthandler.enable() additionally prints a traceback if the interpreter dies
# on a fatal signal, which a bare segfault would otherwise hide.
faulthandler.enable()
if hasattr(faulthandler, 'register'):
    faulthandler.register(signal.SIGUSR1, all_threads=True, chain=True)

if __name__ == '__main__':
    debug = os.environ.get('EYE_TRACKING_DEBUG', '') not in ('', '0')
    print(f'[main] pid={os.getpid()}  '
          f'(kill -USR1 {os.getpid()} เพื่อดัมพ์ stack ตอนค้าง)', flush=True)
    root = tk.Tk()
    root.attributes('-zoomed', True)
    root.minsize(1200, 900)
    root.aspect(8, 5, 8, 5)
    app = EyeTrackingApp(root, debug=debug)
    root.protocol("WM_DELETE_WINDOW", app.on_close)
    root.mainloop()
