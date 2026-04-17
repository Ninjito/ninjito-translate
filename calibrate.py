"""One-off calibration tool.

Finds the running Dota 2 window, overlays a translucent fullscreen
picker, and saves the selected rectangle into config.json *relative to
Dota's client area*. Running Dota windowed or borderless is fine; the
important thing is that the chat position within the window stays
consistent between calibration and gameplay.

Press ESC to cancel without saving.
"""

from __future__ import annotations

import json
import sys
import tkinter as tk
from pathlib import Path

from dota_ocr.dpi import enable_dpi_awareness
from dota_ocr.window import find_dota_hwnd, get_client_screen_rect

CONFIG_PATH = Path(__file__).resolve().parent / "config.json"


class RegionPicker:
    def __init__(self) -> None:
        self.bbox_abs: dict | None = None

        self.root = tk.Tk()
        self.root.attributes("-fullscreen", True)
        self.root.attributes("-alpha", 0.3)
        self.root.attributes("-topmost", True)
        self.root.configure(bg="black")
        self.root.config(cursor="cross")

        self.canvas = tk.Canvas(self.root, bg="black", highlightthickness=0, cursor="cross")
        self.canvas.pack(fill="both", expand=True)

        tk.Label(
            self.root,
            text="Drag a tight box around the Dota 2 chat messages.   ESC = cancel",
            bg="#ffd84d",
            fg="black",
            font=("Arial", 14, "bold"),
            padx=12,
            pady=6,
        ).place(relx=0.5, y=30, anchor="n")

        self._start: tuple[int, int] | None = None
        self._rect_id: int | None = None

        self.canvas.bind("<ButtonPress-1>", self._on_press)
        self.canvas.bind("<B1-Motion>", self._on_drag)
        self.canvas.bind("<ButtonRelease-1>", self._on_release)
        self.root.bind("<Escape>", lambda _e: self.root.destroy())

    def _on_press(self, e: tk.Event) -> None:
        self._start = (e.x_root, e.y_root)
        self._rect_id = self.canvas.create_rectangle(
            e.x, e.y, e.x, e.y, outline="#ff3b3b", width=3
        )

    def _on_drag(self, e: tk.Event) -> None:
        if self._rect_id is None or self._start is None:
            return
        # Convert start (screen) to canvas coords for drawing.
        start_canvas_x = self._start[0] - self.root.winfo_rootx()
        start_canvas_y = self._start[1] - self.root.winfo_rooty()
        self.canvas.coords(self._rect_id, start_canvas_x, start_canvas_y, e.x, e.y)

    def _on_release(self, e: tk.Event) -> None:
        if self._start is None:
            return
        x1, y1 = self._start
        x2, y2 = e.x_root, e.y_root
        left, top = min(x1, x2), min(y1, y2)
        width, height = abs(x2 - x1), abs(y2 - y1)
        if width < 20 or height < 10:
            self.root.destroy()
            return
        self.bbox_abs = {"left": int(left), "top": int(top),
                         "width": int(width), "height": int(height)}
        self.root.destroy()

    def run(self) -> dict | None:
        self.root.mainloop()
        return self.bbox_abs


def main() -> None:
    enable_dpi_awareness()

    hwnd = find_dota_hwnd()
    if hwnd is None:
        print("Dota 2 window not found. Launch Dota 2 first, then re-run this script.",
              file=sys.stderr)
        sys.exit(1)
    client = get_client_screen_rect(hwnd)
    if client is None:
        print("Dota 2 appears minimized. Bring it to the foreground and retry.",
              file=sys.stderr)
        sys.exit(1)
    cx, cy, cw, ch = client
    print(f"Found Dota 2 client area at screen ({cx}, {cy}) size {cw}x{ch}.")

    cfg: dict = {}
    if CONFIG_PATH.exists():
        with open(CONFIG_PATH, encoding="utf-8") as f:
            cfg = json.load(f)

    print("Drag a box around the chat messages. ESC to cancel.")
    picker = RegionPicker()
    bbox = picker.run()
    if not bbox:
        print("Cancelled.")
        return

    # Convert screen coords -> client-area-relative coords.
    rel_left = bbox["left"] - cx
    rel_top = bbox["top"] - cy
    rel = {
        "left": max(0, rel_left),
        "top": max(0, rel_top),
        "width": bbox["width"],
        "height": bbox["height"],
    }

    # Sanity-check that the selection is inside the Dota window.
    if (rel["left"] + rel["width"] > cw + 10 or
            rel["top"] + rel["height"] > ch + 10):
        print("Warning: selected region extends outside the Dota 2 window. "
              "It will be clipped at runtime.", file=sys.stderr)

    cfg["chat_region_relative"] = rel
    # Drop the legacy absolute region — runtime prefers relative.
    cfg.pop("chat_region", None)

    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)
    print(f"Saved relative chat region: {rel}")
    print(f"  (inside Dota client area {cw}x{ch})")


if __name__ == "__main__":
    main()
