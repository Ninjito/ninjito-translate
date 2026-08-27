"""Design tokens and shared widgets for the app's windows.

Why this exists: colours used to be written inline at every widget, so
`bg="#0a0a0a"` appeared about a hundred times and nothing could be
changed in one place. Padding was decided per widget too, which is why
the spacing never lined up. This module is the system those windows were
missing — one palette, one spacing scale, one set of controls.

Two rules shaped what's in here:

* **Gold is a signal, not decoration.** It marks the thing that is
  active or valuable, the way Dota itself uses gold. Spending it on
  every section header — which is what the old Settings did — means it
  stops meaning anything.
* **Nothing animates.** This app runs alongside a game and a lot of work
  went into cutting its idle CPU. Toggles switch instantly, hover is
  event-driven, and no widget in here repaints on a timer.
"""

from __future__ import annotations

import tkinter as tk
from tkinter import font as tkfont
from tkinter import ttk

# ---------------------------------------------------------------------------
# Colour
# ---------------------------------------------------------------------------

BG = "#0F0F10"          # window ground; blue-black rather than pure black
PANEL = "#16161A"       # card surface, one step up from the ground
RIDGE = "#1E1E24"       # raised controls — keycaps, toggle tracks, inputs
RIDGE_HOVER = "#26262E"
LINE = "#2A2A32"        # borders and dividers

GOLD = "#FFD84A"        # the accent. Active, selected, valuable.
GOLD_HOVER = "#FFE270"
GOLD_DIM = "#7A6620"    # gold that isn't currently active

TEXT = "#E8E6E1"        # warm off-white, tuned to sit next to the gold
TEXT_DIM = "#8E8C87"
MUTED = "#6A6862"

OK = "#7BD88F"
WARN = "#FFB86C"
DANGER = "#FF6B6B"

# On gold fills, text has to go dark — gold on gold is unreadable.
ON_GOLD = "#12100A"

# ---------------------------------------------------------------------------
# Spacing
# ---------------------------------------------------------------------------

XS, SM, MD, LG, XL = 4, 8, 12, 16, 24

# ---------------------------------------------------------------------------
# Type
# ---------------------------------------------------------------------------
#
# Segoe UI is the native Windows face. For a Windows game overlay that is
# the correct choice rather than the lazy one — it is the type the rest
# of the user's desktop is set in. Cascadia Code is kept for key combos
# and device names, where monospace carries meaning instead of being a
# leftover from when everything was Consolas.

_UI_STACK = ("Segoe UI", "Tahoma", "Arial")
_UI_SEMI_STACK = ("Segoe UI Semibold", "Segoe UI", "Tahoma")
_MONO_STACK = ("Cascadia Code", "Consolas", "Courier New")

_resolved: dict[str, str] = {}


def _pick(stack: tuple[str, ...]) -> str:
    """First family that's actually installed, else Tk's default."""
    key = stack[0]
    if key in _resolved:
        return _resolved[key]
    chosen = stack[-1]
    try:
        available = set(tkfont.families())
        for name in stack:
            if name in available:
                chosen = name
                break
    except Exception:
        pass
    _resolved[key] = chosen
    return chosen


def ui(size: int = 10, bold: bool = False) -> tuple:
    return (_pick(_UI_STACK), size, "bold") if bold else (_pick(_UI_STACK), size)


def semi(size: int = 9) -> tuple:
    """Header face. Falls back to bold Segoe UI where Semibold is absent."""
    family = _pick(_UI_SEMI_STACK)
    if family.endswith("Semibold"):
        return (family, size)
    return (family, size, "bold")


def mono(size: int = 9, bold: bool = False) -> tuple:
    return ((_pick(_MONO_STACK), size, "bold") if bold
            else (_pick(_MONO_STACK), size))


# ---------------------------------------------------------------------------
# Building blocks
# ---------------------------------------------------------------------------


def hover(widget, normal: str, active: str, attr: str = "bg") -> None:
    """Light up on pointer-over. Event-driven, so it costs nothing idle."""
    def enter(_e=None):
        try:
            widget.configure(**{attr: active})
        except Exception:
            pass

    def leave(_e=None):
        try:
            widget.configure(**{attr: normal})
        except Exception:
            pass

    widget.bind("<Enter>", enter, add="+")
    widget.bind("<Leave>", leave, add="+")


def section_header(parent, text: str) -> tk.Label:
    """An eyebrow above a card. Uppercase, quiet — it labels, nothing more."""
    lbl = tk.Label(parent, text=text.upper(), bg=parent["bg"],
                   fg=MUTED, font=semi(8), anchor="w")
    lbl.pack(fill="x", padx=LG, pady=(LG, XS))
    return lbl


def card(parent) -> tk.Frame:
    """A grouped surface. One flat step up from the ground, hairline border."""
    outer = tk.Frame(parent, bg=LINE)
    outer.pack(fill="x", padx=LG, pady=(0, XS))
    inner = tk.Frame(outer, bg=PANEL)
    inner.pack(fill="both", expand=True, padx=1, pady=1)
    return inner


def row(parent, label: str, hint: str = "",
        compact: bool = False) -> tk.Frame:
    """One setting: text on the left, control right-aligned on a shared axis.

    Returns the frame the caller packs its control into, so every row in
    the window lines its control up on the same edge. `compact` tightens
    the vertical rhythm for long lists that have no hint text.
    """
    outer = tk.Frame(parent, bg=PANEL)
    outer.pack(fill="x", padx=MD, pady=(XS if compact else SM))

    control = tk.Frame(outer, bg=PANEL)
    control.pack(side="right", padx=(MD, 0))

    text = tk.Frame(outer, bg=PANEL)
    text.pack(side="left", fill="x", expand=True)
    tk.Label(text, text=label, bg=PANEL, fg=TEXT,
             font=ui(10), anchor="w").pack(fill="x")
    if hint:
        tk.Label(text, text=hint, bg=PANEL, fg=TEXT_DIM, font=ui(8),
                 anchor="w", justify="left").pack(fill="x", pady=(1, 0))
    return control


def divider(parent) -> tk.Frame:
    line = tk.Frame(parent, bg=LINE, height=1)
    line.pack(fill="x", padx=MD)
    return line


def hint(parent, text: str, fg: str = TEXT_DIM) -> tk.Label:
    lbl = tk.Label(parent, text=text, bg=parent["bg"], fg=fg, font=ui(8),
                   anchor="w", justify="left")
    lbl.pack(fill="x", padx=LG, pady=(XS, 0))
    return lbl


class Toggle(tk.Canvas):
    """A switch, because Tk's Checkbutton renders as Windows 95.

    Reads as on or off at a glance, which is the whole job — this panel
    gets opened mid-match and closed again in seconds.
    """

    W, H = 38, 20

    def __init__(self, parent, value: bool = False, command=None,
                 bg: str = PANEL) -> None:
        super().__init__(parent, width=self.W, height=self.H, bg=bg,
                         highlightthickness=0, bd=0, cursor="hand2")
        self._value = bool(value)
        self._command = command
        self._hover = False
        self.bind("<Button-1>", self._clicked)
        self.bind("<Enter>", self._enter)
        self.bind("<Leave>", self._leave)
        self._draw()

    # -- painting --
    def _draw(self) -> None:
        self.delete("all")
        r = self.H // 2
        if self._value:
            track = GOLD_HOVER if self._hover else GOLD
            knob, knob_x = ON_GOLD, self.W - r
        else:
            track = RIDGE_HOVER if self._hover else RIDGE
            knob, knob_x = MUTED if not self._hover else TEXT_DIM, r
        # Rounded track: two caps plus the bar between them.
        self.create_oval(0, 0, self.H, self.H, fill=track, outline=track)
        self.create_oval(self.W - self.H, 0, self.W, self.H,
                         fill=track, outline=track)
        self.create_rectangle(r, 0, self.W - r, self.H,
                              fill=track, outline=track)
        self.create_oval(knob_x - 6, r - 6, knob_x + 6, r + 6,
                         fill=knob, outline=knob)

    # -- events --
    def _enter(self, _e=None):
        self._hover = True
        self._draw()

    def _leave(self, _e=None):
        self._hover = False
        self._draw()

    def _clicked(self, _e=None):
        self.set(not self._value)
        if self._command is not None:
            self._command(self._value)

    # -- state --
    def get(self) -> bool:
        return self._value

    def set(self, value: bool) -> None:
        """Set the switch without firing the callback."""
        self._value = bool(value)
        self._draw()


class Keycap(tk.Frame):
    """A hotkey combo drawn as a physical key.

    This app is driven almost entirely by keybinds, so the hotkey list is
    the part of Settings people actually come for. Rendering combos as
    bevelled caps rather than text buttons makes the list scannable at a
    glance and says the true thing about them: these are keys you press.

    While rebinding, the cap presses in and goes gold.
    """

    def __init__(self, parent, text: str, command=None) -> None:
        super().__init__(parent, bg=LINE, cursor="hand2")
        self._command = command
        self._listening = False
        self._label = tk.Label(self, text=text, bg=RIDGE, fg=TEXT,
                               font=mono(9, bold=True), padx=MD, pady=XS,
                               cursor="hand2")
        self._label.pack(padx=1, pady=1)
        for w in (self, self._label):
            w.bind("<Button-1>", self._clicked)
            w.bind("<Enter>", self._enter)
            w.bind("<Leave>", self._leave)

    def _clicked(self, _e=None):
        if self._command is not None:
            self._command()

    def _enter(self, _e=None):
        if not self._listening:
            self.configure(bg=GOLD_DIM)
            self._label.configure(bg=RIDGE_HOVER)

    def _leave(self, _e=None):
        if not self._listening:
            self.configure(bg=LINE)
            self._label.configure(bg=RIDGE)

    # -- state --
    def set_text(self, text: str) -> None:
        self._label.configure(text=text)

    def set_listening(self, listening: bool, text: str | None = None) -> None:
        """Pressed-in gold state while waiting for the new combo."""
        self._listening = bool(listening)
        if listening:
            self.configure(bg=GOLD)
            self._label.configure(bg=GOLD, fg=ON_GOLD,
                                  text=text or "Press keys…")
        else:
            self.configure(bg=LINE)
            self._label.configure(bg=RIDGE, fg=TEXT)
            if text is not None:
                self._label.configure(text=text)


class TabView(tk.Frame):
    """A tab strip and the panes below it.

    Replaces `ttk.Notebook`. Clam draws a raised box around every tab and
    a border around the client area, and neither is reachable through
    styling — you can only take them out by rewriting the widget layout,
    at which point you own the tab anyway. Doing it directly is less code
    than fighting the theme engine, and it allows the active tab to be
    marked the way modern apps mark it: an underline in the accent, not a
    box.

    Panes are plain Frames. Only the active one is packed, so the others
    cost nothing to keep around.
    """

    def __init__(self, parent, bg: str = BG) -> None:
        super().__init__(parent, bg=bg)
        self._bg = bg
        self._panes: dict[str, tk.Frame] = {}
        self._tabs: dict[str, tuple[tk.Label, tk.Frame]] = {}
        self._active: str | None = None

        self._strip = tk.Frame(self, bg=bg)
        self._strip.pack(fill="x", padx=LG, pady=(SM, 0))
        # Hairline under the whole strip; the active marker sits on it.
        tk.Frame(self, bg=LINE, height=1).pack(fill="x", padx=LG)
        self._body = tk.Frame(self, bg=bg)
        self._body.pack(fill="both", expand=True)

    def add(self, key: str, label: str) -> tk.Frame:
        holder = tk.Frame(self._strip, bg=self._bg)
        holder.pack(side="left")
        cell = tk.Label(holder, text=label, bg=self._bg, fg=TEXT_DIM,
                        font=ui(9), padx=MD, pady=SM, cursor="hand2")
        cell.pack()
        marker = tk.Frame(holder, bg=self._bg, height=2)
        marker.pack(fill="x")
        cell.bind("<Button-1>", lambda _e, k=key: self.select(k))
        cell.bind("<Enter>", lambda _e, k=key: self._on_enter(k))
        cell.bind("<Leave>", lambda _e, k=key: self._on_leave(k))

        pane = tk.Frame(self._body, bg=self._bg)
        self._panes[key] = pane
        self._tabs[key] = (cell, marker)
        if self._active is None:
            self.select(key)
        return pane

    def _on_enter(self, key: str) -> None:
        if key != self._active:
            self._tabs[key][0].configure(fg=TEXT)

    def _on_leave(self, key: str) -> None:
        if key != self._active:
            self._tabs[key][0].configure(fg=TEXT_DIM)

    def select(self, key: str) -> None:
        if key not in self._panes or key == self._active:
            if key != self._active:
                return
        for name, (cell, marker) in self._tabs.items():
            active = name == key
            cell.configure(fg=GOLD if active else TEXT_DIM)
            marker.configure(bg=GOLD if active else self._bg)
        if self._active is not None:
            self._panes[self._active].pack_forget()
        self._panes[key].pack(fill="both", expand=True)
        self._active = key

    def active(self) -> str | None:
        return self._active

    def panes(self) -> dict[str, tk.Frame]:
        return dict(self._panes)


class Segmented(tk.Frame):
    """A row of mutually exclusive choices; the picked one is gold.

    Replaces `tk.Radiobutton`, whose indicator dot is both ugly and hard
    to read at a glance. Here the selection is the only filled thing in
    the row, so which option is live is obvious without reading.
    """

    def __init__(self, parent, options: list[tuple[str, str]], value: str,
                 command=None, bg: str = PANEL) -> None:
        super().__init__(parent, bg=LINE)
        self._command = command
        self._value = value
        self._buttons: dict[str, tk.Label] = {}

        strip = tk.Frame(self, bg=LINE)
        strip.pack(padx=1, pady=1)
        for index, (val, label) in enumerate(options):
            cell = tk.Label(strip, text=label, font=ui(9), padx=MD, pady=XS,
                            cursor="hand2")
            cell.pack(side="left", padx=(0 if index == 0 else 1), pady=0)
            cell.bind("<Button-1>", lambda _e, v=val: self._pick(v))
            self._buttons[val] = cell
        self._repaint()

    def _repaint(self) -> None:
        for val, cell in self._buttons.items():
            if val == self._value:
                cell.configure(bg=GOLD, fg=ON_GOLD)
            else:
                cell.configure(bg=RIDGE, fg=TEXT_DIM)

    def _pick(self, value: str) -> None:
        if value == self._value:
            return
        self._value = value
        self._repaint()
        if self._command is not None:
            self._command(value)

    def get(self) -> str:
        return self._value

    def set(self, value: str) -> None:
        """Select without firing the callback."""
        if value in self._buttons:
            self._value = value
            self._repaint()


class Dropdown(tk.Frame):
    """A select, built rather than themed.

    `ttk.Combobox` draws its arrow well from clam's own palette and
    ignores the style's background in that one spot, which left a light
    grey slab on the right edge of an otherwise dark panel. Since the app
    has exactly one dropdown, owning it is cheaper than the workaround.
    """

    def __init__(self, parent, values: list[str], value: str = "",
                 command=None, bg: str = PANEL) -> None:
        super().__init__(parent, bg=LINE)
        self._values = list(values)
        self._value = value or (self._values[0] if self._values else "")
        self._command = command

        body = tk.Frame(self, bg=RIDGE)
        body.pack(fill="x", padx=1, pady=1)
        self._label = tk.Label(body, text=self._value, bg=RIDGE, fg=TEXT,
                               font=ui(9), anchor="w", padx=MD, pady=SM,
                               cursor="hand2")
        self._label.pack(side="left", fill="x", expand=True)
        self._caret = tk.Label(body, text="▾", bg=RIDGE, fg=TEXT_DIM,
                               font=ui(9), padx=MD, cursor="hand2")
        self._caret.pack(side="right")

        self._menu = tk.Menu(self, tearoff=0, bg=RIDGE, fg=TEXT,
                             activebackground=GOLD, activeforeground=ON_GOLD,
                             bd=0, relief="flat", font=ui(9))
        self._rebuild_menu()

        for w in (body, self._label, self._caret):
            w.bind("<Button-1>", self._open)
            w.bind("<Enter>", self._enter)
            w.bind("<Leave>", self._leave)

    def _rebuild_menu(self) -> None:
        self._menu.delete(0, "end")
        for value in self._values:
            self._menu.add_command(
                label=value, command=lambda v=value: self._pick(v))

    def _enter(self, _e=None):
        self._label.configure(bg=RIDGE_HOVER)
        self._caret.configure(bg=RIDGE_HOVER, fg=GOLD)
        self._label.master.configure(bg=RIDGE_HOVER)

    def _leave(self, _e=None):
        self._label.configure(bg=RIDGE)
        self._caret.configure(bg=RIDGE, fg=TEXT_DIM)
        self._label.master.configure(bg=RIDGE)

    def _open(self, _e=None):
        try:
            self._menu.tk_popup(self.winfo_rootx(),
                                self.winfo_rooty() + self.winfo_height())
        finally:
            self._menu.grab_release()

    def _pick(self, value: str) -> None:
        self.set(value)
        if self._command is not None:
            self._command(value)

    def get(self) -> str:
        return self._value

    def set(self, value: str) -> None:
        """Select without firing the callback."""
        self._value = value
        self._label.configure(text=value)


def pill(parent, text: str, fg: str = TEXT_DIM) -> tk.Label:
    """A small status chip — used for things like 'not installed'."""
    return tk.Label(parent, text=text, bg=RIDGE, fg=fg, font=ui(8),
                    padx=SM, pady=2)


# ---------------------------------------------------------------------------
# ttk styling
# ---------------------------------------------------------------------------


def style_widgets(window) -> ttk.Style:
    """Theme the ttk widgets we use: notebook, combobox, scrollbar."""
    style = ttk.Style(window)
    try:
        style.theme_use("clam")
    except tk.TclError:
        pass

    # Tabs read as a quiet row; the selected one is the only gold thing.
    # clam draws a raised box around each tab and a border around the
    # client area, which is most of what made the old panel look dated.
    # Neither is reachable through `configure`, so the elements that draw
    # them are removed from the layout instead.
    try:
        style.layout("App.TNotebook", [])
        style.layout("App.TNotebook.Tab", [
            ("Notebook.tab", {"sticky": "nswe", "children": [
                ("Notebook.padding", {"side": "top", "sticky": "nswe",
                                      "children": [
                    ("Notebook.label", {"side": "top", "sticky": ""}),
                ]}),
            ]}),
        ])
    except tk.TclError:
        pass
    style.configure("App.TNotebook", background=BG, borderwidth=0,
                    tabmargins=[LG, MD, LG, 0])
    style.configure("App.TNotebook.Tab", background=BG, foreground=TEXT_DIM,
                    padding=[MD, SM], borderwidth=0, font=ui(9))
    style.map("App.TNotebook.Tab",
              background=[("selected", BG), ("active", BG)],
              foreground=[("selected", GOLD), ("active", TEXT)])

    style.configure("App.TCombobox",
                    fieldbackground=RIDGE, background=RIDGE,
                    foreground=TEXT, arrowcolor=TEXT_DIM,
                    bordercolor=LINE, lightcolor=LINE, darkcolor=LINE,
                    borderwidth=1, padding=SM)
    style.configure("App.TCombobox", arrowsize=12,
                    selectbackground=RIDGE, selectforeground=TEXT)
    style.map("App.TCombobox",
              fieldbackground=[("readonly", RIDGE)],
              # Without this the arrow well keeps clam's light grey.
              background=[("readonly", RIDGE), ("active", RIDGE_HOVER),
                          ("pressed", RIDGE_HOVER)],
              foreground=[("readonly", TEXT)],
              selectbackground=[("readonly", RIDGE)],
              selectforeground=[("readonly", TEXT)],
              arrowcolor=[("active", GOLD)],
              bordercolor=[("focus", GOLD_DIM)],
              lightcolor=[("readonly", LINE)],
              darkcolor=[("readonly", LINE)])

    style.configure("App.Vertical.TScrollbar",
                    background=RIDGE, troughcolor=BG, bordercolor=BG,
                    arrowcolor=MUTED, lightcolor=RIDGE, darkcolor=RIDGE,
                    borderwidth=0)
    style.map("App.Vertical.TScrollbar",
              background=[("active", RIDGE_HOVER)])
    return style


def style_dropdown(window) -> None:
    """Recolour the combobox popup list, which ttk styling doesn't reach."""
    try:
        window.option_add("*TCombobox*Listbox.background", RIDGE)
        window.option_add("*TCombobox*Listbox.foreground", TEXT)
        window.option_add("*TCombobox*Listbox.selectBackground", GOLD)
        window.option_add("*TCombobox*Listbox.selectForeground", ON_GOLD)
        window.option_add("*TCombobox*Listbox.font", ui(9))
    except Exception:
        pass
