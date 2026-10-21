# -*- coding: utf-8 -*-
"""Visual theme for RPG-Translator: one neutral palette + ONE accent, system font.

Presentation only. apply_theme(root, mode) installs a ttk theme (base 'clam') and
returns (palette, fonts) for the few classic-tk widgets (Text, status bar).
mode is "dark" or "light"; switching needs a restart (tk widgets are coloured at
creation). Stdlib only.
"""
import sys
import tkinter as tk
import tkinter.font as tkfont
from tkinter import ttk

PALETTES = {
    "dark": {
        "bg": "#202124", "surface": "#292a2e", "surface_alt": "#323338",
        "border": "#44464c", "text": "#e6e7e9", "text_muted": "#9b9ea6",
        "accent": "#4c8fdd", "accent_active": "#6aa3e8", "accent_fg": "#ffffff",
        "success": "#5fb37c", "danger": "#e0716f", "danger_active": "#ee8a88",
        "drop_idle": "#292a2e", "drop_hover": "#2c3646",
        "select": "#34517c", "stripe": "#2d2e33",
    },
    "light": {
        "bg": "#f1f1f2", "surface": "#ffffff", "surface_alt": "#e6e7e9",
        "border": "#c6c8cd", "text": "#1d1e20", "text_muted": "#62666e",
        "accent": "#2b6cc4", "accent_active": "#3d7fd8", "accent_fg": "#ffffff",
        "success": "#2f8a52", "danger": "#c03a38", "danger_active": "#d04e4c",
        "drop_idle": "#ffffff", "drop_hover": "#e3edfa",
        "select": "#cfe0f7", "stripe": "#f6f6f7",
    },
}
PALETTE = dict(PALETTES["dark"])   # back-compat; updated by apply_theme
FONTS = {}                   # filled by apply_theme (system font)


def _fonts(root):
    base = tkfont.nametofont("TkDefaultFont").actual()
    fam, size = base["family"], abs(int(base["size"])) or 9
    mono = "Consolas" if "Consolas" in tkfont.families(root) else "Courier New"
    FONTS.update({
        "base": (fam, size), "small": (fam, size), "big": (fam, size + 2),
        "heading": (fam, size, "bold"), "title": (fam, size + 4, "bold"),
        "mono": (mono, size),
    })
    return FONTS


def apply_theme(root, mode="dark"):
    """Install the theme on `root`. Returns (palette, fonts)."""
    p = dict(PALETTES.get(mode, PALETTES["dark"]))
    PALETTE.clear()
    PALETTE.update(p)
    f = _fonts(root)
    root.configure(background=p["bg"])
    style = ttk.Style(root)
    try:
        style.theme_use("clam")
    except tk.TclError:
        pass

    style.configure(".", background=p["bg"], foreground=p["text"],
                    fieldbackground=p["surface"], bordercolor=p["border"],
                    lightcolor=p["border"], darkcolor=p["border"],
                    focuscolor=p["accent"], troughcolor=p["surface_alt"],
                    selectbackground=p["select"], selectforeground=p["text"],
                    font=f["base"])
    style.map(".", foreground=[("disabled", p["text_muted"])])
    style.configure("TFrame", background=p["bg"])
    style.configure("Surface.TFrame", background=p["surface"])
    style.configure("Bar.TFrame", background=p["surface_alt"])
    style.configure("TLabel", background=p["bg"], foreground=p["text"])
    style.configure("Muted.TLabel", foreground=p["text_muted"])
    style.configure("Title.TLabel", font=f["title"])
    style.configure("Heading.TLabel", font=f["heading"])
    style.configure("Bar.TLabel", background=p["surface_alt"],
                    foreground=p["text_muted"])
    style.configure("Danger.TLabel", foreground=p["danger"])
    # legacy names used by older code / tests
    style.configure("Card.TFrame", background=p["bg"])
    style.configure("Card.TLabel", background=p["bg"], foreground=p["text"])
    style.configure("CardMuted.TLabel", background=p["bg"],
                    foreground=p["text_muted"])

    # sidebar navigation (radio toolbuttons)
    style.configure("Nav.TFrame", background=p["surface_alt"])
    style.configure("Nav.Toolbutton", background=p["surface_alt"],
                    foreground=p["text"], anchor="w", padding=(14, 8),
                    borderwidth=0, relief="flat", font=f["base"])
    style.map("Nav.Toolbutton",
              background=[("selected", p["bg"]), ("active", p["border"])],
              foreground=[("selected", p["accent"])])

    # buttons
    style.configure("TButton", background=p["surface_alt"], foreground=p["text"],
                    bordercolor=p["border"], padding=(10, 4), relief="flat",
                    borderwidth=1, anchor="center")
    style.map("TButton",
              background=[("disabled", p["surface"]), ("pressed", p["border"]),
                          ("active", p["border"])],
              bordercolor=[("focus", p["accent"])])
    style.configure("Accent.TButton", background=p["accent"],
                    foreground=p["accent_fg"], bordercolor=p["accent"])
    style.map("Accent.TButton",
              background=[("disabled", p["surface"]),
                          ("active", p["accent_active"])],
              foreground=[("disabled", p["text_muted"])],
              bordercolor=[("disabled", p["border"])])
    style.configure("Secondary.TButton")
    style.configure("Danger.TButton", foreground=p["danger"])
    style.map("Danger.TButton",
              foreground=[("disabled", p["text_muted"]),
                          ("active", p["danger_active"])])
    style.configure("Toolbar.TButton", padding=(10, 4))

    # inputs
    for name in ("TEntry", "TSpinbox", "TCombobox"):
        style.configure(name, fieldbackground=p["surface"], foreground=p["text"],
                        insertcolor=p["text"], bordercolor=p["border"],
                        padding=3)
    style.map("TEntry", fieldbackground=[("readonly", p["surface_alt"]),
                                         ("disabled", p["bg"])],
              bordercolor=[("focus", p["accent"])])
    style.map("TCombobox", fieldbackground=[("readonly", p["surface"])],
              foreground=[("readonly", p["text"])],
              bordercolor=[("focus", p["accent"])])
    style.configure("TCombobox", background=p["surface_alt"],
                    arrowcolor=p["text"])
    style.configure("TSpinbox", background=p["surface_alt"], arrowcolor=p["text"])
    root.option_add("*TCombobox*Listbox.background", p["surface"])
    root.option_add("*TCombobox*Listbox.foreground", p["text"])
    root.option_add("*TCombobox*Listbox.selectBackground", p["accent"])
    root.option_add("*TCombobox*Listbox.selectForeground", p["accent_fg"])
    root.option_add("*Listbox.background", p["surface"])
    root.option_add("*Listbox.foreground", p["text"])
    root.option_add("*Listbox.selectBackground", p["select"])
    root.option_add("*Listbox.selectForeground", p["text"])

    style.configure("TCheckbutton", background=p["bg"], indicatorcolor=p["surface"],
                    indicatorbackground=p["surface"])
    style.map("TCheckbutton", indicatorcolor=[("selected", p["accent"])],
              background=[("active", p["bg"])])
    style.configure("TRadiobutton", background=p["bg"])
    style.map("TRadiobutton", indicatorcolor=[("selected", p["accent"])],
              background=[("active", p["bg"])])

    # tables
    style.configure("Treeview", background=p["surface"], fieldbackground=p["surface"],
                    foreground=p["text"], bordercolor=p["border"], rowheight=22,
                    borderwidth=1)
    style.map("Treeview", background=[("selected", p["select"])],
              foreground=[("selected", p["text"])])
    style.configure("Treeview.Heading", background=p["surface_alt"],
                    foreground=p["text"], relief="flat", padding=(6, 4),
                    font=f["heading"])
    style.map("Treeview.Heading", background=[("active", p["border"])])

    style.configure("TScrollbar", background=p["surface_alt"], troughcolor=p["bg"],
                    bordercolor=p["bg"], arrowcolor=p["text_muted"], relief="flat")
    style.map("TScrollbar", background=[("active", p["border"])])
    style.configure("TProgressbar", background=p["accent"],
                    troughcolor=p["surface_alt"], bordercolor=p["border"],
                    lightcolor=p["accent"], darkcolor=p["accent"], thickness=10)
    style.configure("TSeparator", background=p["border"])

    # inner notebook + group boxes
    style.configure("TNotebook", background=p["bg"], bordercolor=p["border"],
                    tabmargins=(0, 0, 0, 0))
    style.configure("TNotebook.Tab", background=p["bg"], foreground=p["text_muted"],
                    padding=(14, 5), bordercolor=p["border"], lightcolor=p["border"],
                    darkcolor=p["border"], focuscolor=p["surface_alt"])
    style.map("TNotebook.Tab",
              background=[("selected", p["surface_alt"])],
              foreground=[("selected", p["text"])])
    for name in ("TLabelframe", "Card.TLabelframe", "Drop.TLabelframe"):
        style.configure(name, background=p["bg"], bordercolor=p["border"],
                        relief="solid", borderwidth=1, padding=8)
    style.configure("DropHover.TLabelframe", background=p["drop_hover"],
                    bordercolor=p["accent"], relief="solid", borderwidth=1,
                    padding=8)
    for name in ("TLabelframe.Label", "Card.TLabelframe.Label",
                 "Drop.TLabelframe.Label"):
        style.configure(name, background=p["bg"], foreground=p["text"],
                        font=f["heading"])
    style.configure("DropHover.TLabelframe.Label", background=p["drop_hover"],
                    foreground=p["text"], font=f["heading"])
    style.configure("Sash", sashthickness=6)
    return p, f


def style_text(w, p, f):
    """Colour a classic tk Text / ScrolledText to the palette."""
    w.config(bg=p["surface"], fg=p["text"], insertbackground=p["text"],
             selectbackground=p["select"], selectforeground=p["text"],
             relief="flat", borderwidth=0, highlightthickness=1,
             highlightbackground=p["border"], highlightcolor=p["accent"],
             font=f["mono"], padx=6, pady=4)


def enable_dark_titlebar(root, dark=True):
    r"""Best-effort dark Windows title bar. No-op off Windows or on failure.
    Runs after the window is mapped. Windows keeps the accent colour when
    'Show accent colour on title bars' is on; that is a user setting, not a bug."""
    if not sys.platform.startswith("win"):
        return
    try:
        import ctypes
        root.update_idletasks()
        hwnd = ctypes.windll.user32.GetParent(root.winfo_id())
        val = ctypes.c_int(1 if dark else 0)
        for attr in (20, 19):
            try:
                ctypes.windll.dwmapi.DwmSetWindowAttribute(
                    hwnd, attr, ctypes.byref(val), ctypes.sizeof(val))
            except Exception:
                pass
        if root.winfo_viewable():
            root.withdraw()
            root.deiconify()
    except Exception:
        pass
