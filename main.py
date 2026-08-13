import os
import tkinter as tk
from app import EyeTrackingApp

if __name__ == '__main__':
    debug = os.environ.get('EYE_TRACKING_DEBUG', '') not in ('', '0')
    root = tk.Tk()
    root.attributes('-zoomed', True)
    root.minsize(1200, 900)
    root.aspect(8, 5, 8, 5)
    app = EyeTrackingApp(root, debug=debug)
    root.protocol("WM_DELETE_WINDOW", app.on_close)
    root.mainloop()
