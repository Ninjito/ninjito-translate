"""Make the current process DPI-aware on Windows so that Tk widget
coordinates and mss screen coordinates line up 1:1 with physical pixels.
Without this, a user running at 125%/150% scaling will calibrate a chat
region in logical pixels but mss will capture the wrong area."""

import sys


def enable_dpi_awareness() -> None:
    if sys.platform != "win32":
        return
    try:
        from ctypes import windll
        # PROCESS_PER_MONITOR_DPI_AWARE = 2
        windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            from ctypes import windll
            windll.user32.SetProcessDPIAware()
        except Exception:
            pass
