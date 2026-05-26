import tkinter as tk
from app import EyeTrackingApp

if __name__ == '__main__':
    root = tk.Tk()
    root.attributes('-zoomed', True)
    root.minsize(1200, 900)
    root.aspect(8, 5, 8, 5)
    app = EyeTrackingApp(root)
    root.protocol("WM_DELETE_WINDOW", app.on_close)
    root.mainloop()
