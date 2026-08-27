"""The system tray icon.

Everything here runs against a fake pystray, so the menu and its wiring
are tested without a tray, a display, or the real library.

The case worth reading is `test_exit_removes_the_icon_before_quitting`.
The overlay's close path ends in `os._exit(0)`, which never returns — so
if the icon isn't already gone by then, Windows keeps drawing it long
after the process is dead.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from dota_ocr.tray import TrayIcon


# --------------------------------------------------------------------------
# Fakes
# --------------------------------------------------------------------------


class FakeMenuItem:
    def __init__(self, text, action=None, checked=None, default=False):
        self.text = text
        self.action = action
        self.checked = checked
        self.default = default


class FakeMenu:
    SEPARATOR = "---"

    def __init__(self, *items):
        self.items = list(items)


class FakeIcon:
    def __init__(self, name, image, title, menu):
        self.name = name
        self.image = image
        self.title = title
        self.menu = menu
        self.visible = True
        self.ran = False
        self.stopped = False
        self.menu_updates = 0

    def run(self):
        self.ran = True

    def stop(self):
        self.stopped = True

    def update_menu(self):
        self.menu_updates += 1


class FakePystray:
    MenuItem = FakeMenuItem
    Menu = FakeMenu
    Icon = FakeIcon


class FakeImageModule:
    """Stands in for PIL.Image, including raising on a path that isn't there."""

    opened: list = []

    @classmethod
    def open(cls, path):
        if path != "gg.ico":
            raise FileNotFoundError(path)
        cls.opened.append(path)
        return f"image({path})"


class FakeRoot:
    """Stands in for the Tk root; records what got marshalled onto it."""

    def __init__(self, broken=False):
        self.calls = []
        self.broken = broken

    def after(self, delay, fn=None, *args):
        if self.broken:
            raise RuntimeError("main loop is gone")
        self.calls.append((delay, fn))
        return "after#1"

    def run_pending(self):
        for _delay, fn in list(self.calls):
            if fn is not None:
                fn()
        self.calls.clear()


@pytest.fixture
def state():
    return {"voice": False, "visible": True, "events": []}


@pytest.fixture
def tray(state):
    root = FakeRoot()
    icon = TrayIcon(
        root=root,
        on_toggle_overlay=lambda: state["events"].append("overlay"),
        on_open_settings=lambda: state["events"].append("settings"),
        on_toggle_voice=lambda: state["events"].append("voice"),
        is_voice_on=lambda: state["voice"],
        is_overlay_visible=lambda: state["visible"],
        on_exit=lambda: state["events"].append("exit"),
        pystray_mod=FakePystray,
        image_mod=FakeImageModule,
        icon_path="gg.ico",
    )
    icon._root = root
    return icon


def items(tray):
    return tray._icon.menu.items


def find(tray, text):
    for item in items(tray):
        if getattr(item, "text", None) == text:
            return item
    raise AssertionError(f"no menu item {text!r}")


# --------------------------------------------------------------------------
# Menu
# --------------------------------------------------------------------------


class TestMenu:
    def test_starts_and_builds_the_menu(self, tray):
        assert tray.start() is True
        labels = [getattr(i, "text", i) for i in items(tray)]
        assert labels == ["Show overlay", "Settings", "Voice translation",
                          "---", "Exit"]

    def test_exit_is_last_and_separated(self, tray):
        tray.start()
        assert items(tray)[-1].text == "Exit"
        assert items(tray)[-2] == FakeMenu.SEPARATOR

    def test_double_start_reuses_the_icon(self, tray):
        tray.start()
        first = tray._icon
        assert tray.start() is True
        assert tray._icon is first

    def test_checkmarks_read_live_state(self, tray, state):
        tray.start()
        voice = find(tray, "Voice translation")
        overlay = find(tray, "Show overlay")
        assert voice.checked(voice) is False
        assert overlay.checked(overlay) is True
        state["voice"] = True
        state["visible"] = False
        assert voice.checked(voice) is True
        assert overlay.checked(overlay) is False

    def test_show_overlay_is_the_default_action(self, tray):
        """Left-clicking the tray icon should show the overlay."""
        tray.start()
        assert find(tray, "Show overlay").default is True


# --------------------------------------------------------------------------
# Threading
# --------------------------------------------------------------------------


class TestMarshalling:
    @pytest.mark.parametrize("label,event", [
        ("Show overlay", "overlay"),
        ("Settings", "settings"),
        ("Voice translation", "voice"),
    ])
    def test_actions_go_through_the_tk_thread(self, tray, state, label, event):
        """Menu callbacks arrive on pystray's thread; Tk needs its own."""
        tray.start()
        find(tray, label).action()
        # Nothing has run yet — it was handed to root.after, not called.
        assert state["events"] == []
        assert tray._root.calls and tray._root.calls[0][0] == 0
        tray._root.run_pending()
        assert state["events"] == [event]

    def test_a_dead_main_loop_does_not_raise(self, state):
        """Quitting mid-click must not blow up on pystray's thread."""
        tray = TrayIcon(
            root=FakeRoot(broken=True),
            on_toggle_overlay=lambda: None, on_open_settings=lambda: None,
            on_toggle_voice=lambda: None, is_voice_on=lambda: False,
            is_overlay_visible=lambda: True, on_exit=lambda: None,
            pystray_mod=FakePystray, image_mod=FakeImageModule,
            icon_path="gg.ico")
        tray.start()
        find(tray, "Settings").action()      # must not raise


# --------------------------------------------------------------------------
# Exit
# --------------------------------------------------------------------------


class TestExit:
    def test_exit_removes_the_icon_before_quitting(self, tray, state):
        """The close path ends in os._exit(0) and never comes back.

        If the icon is still registered at that point, Windows leaves a
        dead tray entry behind, so stop() has to have already run.
        """
        tray.start()
        icon = tray._icon
        find(tray, "Exit").action()
        assert icon.stopped is True
        assert icon.visible is False
        # Only now does the quit itself get marshalled.
        assert state["events"] == []
        tray._root.run_pending()
        assert state["events"] == ["exit"]

    def test_stop_is_safe_twice(self, tray):
        tray.start()
        tray.stop()
        tray.stop()
        assert tray._icon is None

    def test_stop_without_start_is_safe(self, tray):
        tray.stop()
        assert tray._icon is None


# --------------------------------------------------------------------------
# Failure
# --------------------------------------------------------------------------


class TestFailureIsSurvivable:
    def _tray(self, **kw):
        base = dict(
            root=FakeRoot(), on_toggle_overlay=lambda: None,
            on_open_settings=lambda: None, on_toggle_voice=lambda: None,
            is_voice_on=lambda: False, is_overlay_visible=lambda: True,
            on_exit=lambda: None, pystray_mod=FakePystray,
            image_mod=FakeImageModule)
        base.update(kw)
        return TrayIcon(**base)

    def test_icon_file_that_is_not_there_returns_false(self):
        tray = self._tray(icon_path="nowhere/missing.ico")
        assert tray.start() is False
        assert tray._icon is None
        assert tray.last_error

    def test_no_icon_found_at_all_returns_false(self, monkeypatch):
        """icon_path=None means auto-discover; this is discovery failing."""
        import dota_ocr.tray as tray_mod
        monkeypatch.setattr(tray_mod, "_icon_path", lambda: None)
        tray = self._tray(icon_path=None)
        assert tray.start() is False
        assert tray._icon is None
        assert "not found" in tray.last_error

    def test_broken_backend_returns_false(self, state):
        class Exploding:
            MenuItem, Menu = FakeMenuItem, FakeMenu

            class Icon:
                def __init__(self, *a, **k):
                    raise OSError("no tray on this system")

        tray = TrayIcon(
            root=FakeRoot(), on_toggle_overlay=lambda: None,
            on_open_settings=lambda: None, on_toggle_voice=lambda: None,
            is_voice_on=lambda: False, is_overlay_visible=lambda: True,
            on_exit=lambda: None, pystray_mod=Exploding,
            image_mod=FakeImageModule, icon_path="gg.ico")
        assert tray.start() is False
        assert "no tray" in tray.last_error

    def test_refresh_without_an_icon_is_safe(self, tray):
        tray.refresh()          # never started

    def test_refresh_updates_the_menu(self, tray):
        tray.start()
        tray.refresh()
        assert tray._icon.menu_updates == 1


# --------------------------------------------------------------------------
# Packaging
# --------------------------------------------------------------------------


def test_the_shipped_icon_exists():
    """_icon_path() has to resolve, or every launch logs a tray failure."""
    from dota_ocr.tray import _icon_path
    assert _icon_path(), "gg.ico not found next to the project root"


# --------------------------------------------------------------------------
# Overlay integration
# --------------------------------------------------------------------------


class TestOverlayShutdown:
    """The other half of the ghost-icon problem.

    The tray's Exit stops the icon itself, but the overlay's own X button
    and the Ctrl+Shift hotkeys reach `_close()` directly. That path has to
    remove the icon too, before its final os._exit(0).
    """

    def _shim(self, tray):
        from dota_ocr.overlay import Overlay

        class FakeTkRoot:
            destroyed = False

            def destroy(self):
                FakeTkRoot.destroyed = True

        class Shim:
            _close = Overlay._close

            def __init__(self):
                self._closing = False
                self._tray = tray
                self._logs_window = None
                self._paste_window = None
                self._settings_window = None
                self.root = FakeTkRoot()

        return Shim()

    def test_close_stops_the_tray_before_exiting(self, monkeypatch):
        import os
        order = []

        class RecordingTray:
            def stop(self):
                order.append("tray stopped")

        monkeypatch.setattr(os, "_exit",
                            lambda code: order.append(f"exit({code})"))
        self._shim(RecordingTray())._close()
        assert order == ["tray stopped", "exit(0)"]

    def test_close_works_with_no_tray(self, monkeypatch):
        """The tray is optional; failing to start one must not block quit."""
        import os
        seen = []
        monkeypatch.setattr(os, "_exit", lambda code: seen.append(code))
        self._shim(None)._close()
        assert seen == [0]

    def test_a_tray_that_fails_to_stop_still_quits(self, monkeypatch):
        import os
        seen = []

        class BrokenTray:
            def stop(self):
                raise OSError("tray backend already gone")

        monkeypatch.setattr(os, "_exit", lambda code: seen.append(code))
        self._shim(BrokenTray())._close()
        assert seen == [0]


class TestIconDiscovery:
    """Where gg.ico is looked for.

    The source-tree check above passed while the first frozen build still
    logged "gg.ico not found": PyInstaller unpacks bundled data into
    sys._MEIPASS, which for a onedir build is the `_internal` folder, not
    the directory holding the EXE. These cover the frozen layouts.
    """

    def test_prefers_the_pyinstaller_unpack_dir(self, tmp_path, monkeypatch):
        import sys as _sys
        from dota_ocr import tray as tray_mod

        meipass = tmp_path / "_internal"
        meipass.mkdir()
        (meipass / "gg.ico").write_bytes(b"icon")
        monkeypatch.setattr(_sys, "_MEIPASS", str(meipass), raising=False)
        monkeypatch.setattr(_sys, "frozen", True, raising=False)
        monkeypatch.setattr(_sys, "executable", str(tmp_path / "App.exe"),
                            raising=False)

        assert tray_mod._icon_path() == str(meipass / "gg.ico")

    def test_falls_back_to_the_exe_folder(self, tmp_path, monkeypatch):
        """Some builds drop data beside the EXE instead."""
        import sys as _sys
        from dota_ocr import tray as tray_mod

        (tmp_path / "gg.ico").write_bytes(b"icon")
        monkeypatch.delattr(_sys, "_MEIPASS", raising=False)
        monkeypatch.setattr(_sys, "frozen", True, raising=False)
        monkeypatch.setattr(_sys, "executable", str(tmp_path / "App.exe"),
                            raising=False)

        assert tray_mod._icon_path() == str(tmp_path / "gg.ico")

    def test_returns_none_when_there_is_no_icon_anywhere(self, tmp_path,
                                                        monkeypatch):
        import sys as _sys
        from dota_ocr import tray as tray_mod

        empty = tmp_path / "empty"
        empty.mkdir()
        monkeypatch.setattr(_sys, "_MEIPASS", str(empty), raising=False)
        monkeypatch.setattr(_sys, "frozen", True, raising=False)
        monkeypatch.setattr(_sys, "executable", str(empty / "App.exe"),
                            raising=False)
        monkeypatch.setattr(tray_mod.os.path, "isfile", lambda p: False)

        assert tray_mod._icon_path() is None
