"""System tray icon.

The app runs for a whole match in the background, and until now the only
way to quit it was the overlay's own X button — which meant the overlay
had to be on screen to close the app. This puts it in the tray instead.

Threading, which is the part that bites: pystray runs its own message
loop on its own thread, and Tk may only be touched from the thread
running its mainloop. So every menu action does nothing but hand a
closure to `root.after(0, ...)`, the same rule the keyboard hook and the
voice threads already follow.

Every dependency is injectable so the menu and its wiring can be tested
without a tray, a display, or pystray installed.
"""

from __future__ import annotations

import os
import sys
import threading
from typing import Callable, Optional

APP_NAME = "Ninjito Translate"


def _icon_path() -> Optional[str]:
    """Find gg.ico, in dev and under PyInstaller.

    Search order matters. PyInstaller unpacks bundled data into
    sys._MEIPASS — for a onedir build that is the `_internal` folder, not
    the folder holding the EXE. Looking only next to the executable is
    why the first frozen build started with "[tray] unavailable: gg.ico
    not found" while running fine from source.

    This mirrors `overlay._resource_path`, which already got this right.
    """
    candidates = []
    base = getattr(sys, "_MEIPASS", None)
    if base:
        candidates.append(os.path.join(base, "gg.ico"))
    if getattr(sys, "frozen", False):
        candidates.append(os.path.join(os.path.dirname(sys.executable),
                                       "gg.ico"))
    here = os.path.dirname(os.path.abspath(__file__))
    candidates.append(os.path.join(os.path.dirname(here), "gg.ico"))
    candidates.append(os.path.join(here, "gg.ico"))
    for path in candidates:
        try:
            if os.path.isfile(path):
                return path
        except Exception:
            continue
    return None


class TrayIcon:
    """The tray icon and its menu.

    `on_exit` is expected to tear the app down. Note that the overlay's
    close path ends in `os._exit(0)`, which skips cleanup — so `stop()`
    has to have already removed the icon by then, or Windows leaves a
    ghost in the tray until the user hovers over it.
    """

    def __init__(
        self,
        root,
        on_toggle_overlay: Callable[[], None],
        on_open_settings: Callable[[], None],
        on_toggle_voice: Callable[[], None],
        is_voice_on: Callable[[], bool],
        is_overlay_visible: Callable[[], bool],
        on_exit: Callable[[], None],
        pystray_mod=None,
        image_mod=None,
        icon_path: Optional[str] = None,
    ) -> None:
        self._root = root
        self._on_toggle_overlay = on_toggle_overlay
        self._on_open_settings = on_open_settings
        self._on_toggle_voice = on_toggle_voice
        self._is_voice_on = is_voice_on
        self._is_overlay_visible = is_overlay_visible
        self._on_exit = on_exit

        self._pystray = pystray_mod
        self._image = image_mod
        self._icon_path = icon_path if icon_path is not None else _icon_path()

        self._icon = None
        self._thread: threading.Thread | None = None
        self.last_error = ""

    # -- Tk marshalling -----------------------------------------------------

    def _dispatch(self, fn: Callable[[], None]) -> None:
        """Run `fn` on the Tk thread. Menu callbacks arrive on pystray's."""
        try:
            self._root.after(0, fn)
        except Exception as e:
            print(f"[tray] could not reach the UI thread: {e}", flush=True)

    # -- menu ---------------------------------------------------------------

    def _build_menu(self):
        ps = self._pystray
        item, menu = ps.MenuItem, ps.Menu

        return menu(
            item("Show overlay",
                 lambda: self._dispatch(self._on_toggle_overlay),
                 checked=lambda _i: bool(self._is_overlay_visible()),
                 default=True),
            item("Settings",
                 lambda: self._dispatch(self._on_open_settings)),
            item("Voice translation",
                 lambda: self._dispatch(self._on_toggle_voice),
                 checked=lambda _i: bool(self._is_voice_on())),
            menu.SEPARATOR,
            item("Exit", self._exit_clicked),
        )

    def _exit_clicked(self, *_args) -> None:
        """Remove the icon first, then quit.

        The order matters: the close path calls os._exit(0), which never
        returns, so anything left undone here stays undone — including a
        tray icon Windows would keep drawing.
        """
        self.stop()
        self._dispatch(self._on_exit)

    # -- lifecycle ----------------------------------------------------------

    def _load_image(self):
        if self._image is None:
            from PIL import Image as _Image
            self._image = _Image
        if not self._icon_path:
            raise FileNotFoundError("gg.ico not found")
        return self._image.open(self._icon_path)

    def start(self) -> bool:
        """Create and run the icon. False (and a log line) on failure.

        A tray icon is not worth a failed launch, so every failure here
        is survivable — the app keeps working exactly as it did before.
        """
        if self._icon is not None:
            return True
        try:
            if self._pystray is None:
                import pystray as _pystray
                self._pystray = _pystray
            image = self._load_image()
            self._icon = self._pystray.Icon(
                "ninjito_translate", image, APP_NAME, self._build_menu())
        except Exception as e:
            self.last_error = str(e)
            print(f"[tray] unavailable: {e}", flush=True)
            self._icon = None
            return False

        def run() -> None:
            try:
                self._icon.run()
            except Exception as e:
                print(f"[tray] stopped: {e}", flush=True)

        self._thread = threading.Thread(target=run, daemon=True,
                                        name="ninjito-tray")
        self._thread.start()
        print("[tray] icon installed", flush=True)
        return True

    def refresh(self) -> None:
        """Re-read the checkmarks after voice or the overlay changed."""
        icon = self._icon
        if icon is None:
            return
        try:
            icon.update_menu()
        except Exception:
            pass

    def stop(self) -> None:
        icon, self._icon = self._icon, None
        if icon is None:
            return
        try:
            icon.visible = False
        except Exception:
            pass
        try:
            icon.stop()
        except Exception:
            pass
