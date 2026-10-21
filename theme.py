# -*- coding: utf-8 -*-
"""Dark visual theme for the Eroge Translator control UI.

Presentation only — NO app logic. ui.py calls apply_theme(root) once at startup
to install a dark, light-blue-accented ttk theme (base 'clam'), and uses the
returned palette/fonts to style the few non-ttk (tk) widgets it owns (the drop
zones, the ScrolledText feed/log, the status bar). enable_dark_titlebar(root)
best-effort darkens the Windows 11 title bar.

Stdlib only.
"""
import sys
import tkinter as tk
from tkinter import ttk

PALETTE = {
    "bg":            "#1e1f26",
    "surface":       "#2a2b34",
    "surface_alt":   "#33343f",
    "border":        "#3a3c47",
    "text":          "#e6e6ea",
    "text_muted":    "#9aa0ac",
    "accent":        "#5aa9e6",
    "accent_active": "#7cbcf0",
    "accent_fg":     "#10131a",
    "success":       "#5cc98d",
    "danger":        "#e06a6a",
    "danger_active": "#ef8080",
    "drop_idle":     "#262832",
    "drop_hover":    "#2f3a48",
}

FONTS = {
    "base":    ("Segoe UI", 10),
    "heading": ("Segoe UI", 11, "bold"),
    "small":   ("Segoe UI", 9),
    "big":     ("Segoe UI", 12),
    "mono":    ("Consolas", 9),
}


def apply_theme(root):
    """Install the dark theme on `root`. Returns (palette, fonts)."""
    p, f = PALETTE, FONTS
    root.configure(background=p["bg"])

    style = ttk.Style(root)
    try:
        style.theme_use("clam")   # most restyle-able built-in ttk theme
    except tk.TclError:
        pass

    style.configure(".", background=p["bg"], foreground=p["text"],
                    fieldbackground=p["surface"], bordercolor=p["border"],
                    focuscolor=p["accent"], font=f["base"])
    style.configure("TFrame", background=p["bg"])
    style.configure("TLabel", background=p["bg"], foreground=p["text"])
    style.configure("Heading.TLabel", background=p["bg"],
                    foreground=p["text"], font=f["heading"])
    style.configure("Muted.TLabel", background=p["bg"],
                    foreground=p["text_muted"], font=f["small"])

    # widgets that live INSIDE a card (surface bg) need matching backgrounds
    style.configure("Card.TFrame", background=p["surface"])
    style.configure("Card.TLabel", background=p["surface"], foreground=p["text"])
    style.configure("CardMuted.TLabel", background=p["surface"],
                    foreground=p["text_muted"], font=f["small"])

    # notebook + tabs
    style.configure("TNotebook", background=p["bg"], borderwidth=0,
                    tabmargins=(6, 6, 6, 0))
    style.configure("TNotebook.Tab", background=p["bg"],
                    foreground=p["text_muted"], padding=(20, 10),
                    borderwidth=0, font=f["base"])
    style.map("TNotebook.Tab",
              background=[("selected", p["surface"])],
              foreground=[("selected", p["accent"]), ("active", p["text"])])

    # buttons
    style.configure("TButton", background=p["surface_alt"], foreground=p["text"],
                    borderwidth=0, padding=(12, 7), font=f["base"])
    style.map("TButton",
              background=[("active", p["border"]), ("disabled", p["surface"])],
              foreground=[("disabled", p["text_muted"])])
    style.configure("Accent.TButton", background=p["accent"],
                    foreground=p["accent_fg"], font=f["heading"], padding=(14, 7))
    style.map("Accent.TButton",
              background=[("active", p["accent_active"]),
                          ("disabled", p["surface_alt"])],
              foreground=[("disabled", p["text_muted"])])
    style.configure("Secondary.TButton", background=p["surface_alt"],
                    foreground=p["text"])
    style.map("Secondary.TButton",
              background=[("active", p["border"]), ("disabled", p["surface"])],
              foreground=[("disabled", p["text_muted"])])
    style.configure("Danger.TButton", background=p["surface_alt"],
                    foreground=p["danger"])
    style.map("Danger.TButton",
              background=[("active", p["border"]), ("disabled", p["surface"])],
              foreground=[("active", p["danger_active"]),
                          ("disabled", p["text_muted"])])

    # entry / combobox
    style.configure("TEntry", fieldbackground=p["surface"], foreground=p["text"],
                    bordercolor=p["border"], insertcolor=p["text"], padding=6)
    style.map("TEntry", foreground=[("readonly", p["text_muted"])])
    style.configure("TCombobox", fieldbackground=p["surface"],
                    background=p["surface_alt"], foreground=p["text"],
                    arrowcolor=p["text"], bordercolor=p["border"], padding=5)
    style.map("TCombobox", fieldbackground=[("readonly", p["surface"])],
              foreground=[("readonly", p["text"])])
    # combobox drop-down list is a classic tk Listbox: colour it via options
    root.option_add("*TCombobox*Listbox.background", p["surface"])
    root.option_add("*TCombobox*Listbox.foreground", p["text"])
    root.option_add("*TCombobox*Listbox.selectBackground", p["accent"])
    root.option_add("*TCombobox*Listbox.selectForeground", p["accent_fg"])

    # progressbar
    style.configure("TProgressbar", background=p["accent"],
                    troughcolor=p["surface"], bordercolor=p["surface"],
                    lightcolor=p["accent"], darkcolor=p["accent"])

    # cards
    style.configure("Card.TLabelframe", background=p["surface"],
                    bordercolor=p["border"], relief="flat", borderwidth=1,
                    padding=12)
    style.configure("Card.TLabelframe.Label", background=p["surface"],
                    foreground=p["accent"], font=f["heading"])

    style.configure("TSeparator", background=p["border"])
    return p, f


def enable_dark_titlebar(root):
    r"""Best-effort dark Windows title bar. No-op off Windows or on failure.

    Must run AFTER the window is mapped (see the root.after(...) call in
    ui.App.__init__) — DWM ignores the attribute on an unmapped window. We set
    both attribute ids (20 = Win11/late-Win10, 19 = early builds) and force a
    non-client repaint so the frame recolours immediately.

    NOTE: even when the DWM call returns S_OK, Windows keeps the title bar in the
    user's ACCENT colour if Settings > Personalization > Colors > "Show accent
    colour on title bars" is ON (registry HKCU\...\DWM\ColorPrevalence = 1). That
    user setting overrides an app's dark-title-bar request by design; the client
    area is still fully themed. Not a bug — don't try to fight it."""
    if not sys.platform.startswith("win"):
        return
    try:
        import ctypes
        root.update_idletasks()
        hwnd = ctypes.windll.user32.GetParent(root.winfo_id())
        val = ctypes.c_int(1)
        for attr in (20, 19):   # DWMWA_USE_IMMERSIVE_DARK_MODE (build-dependent)
            try:
                ctypes.windll.dwmapi.DwmSetWindowAttribute(
                    hwnd, attr, ctypes.byref(val), ctypes.sizeof(val))
            except Exception:
                pass
        # Force the title bar (non-client area) to repaint with the new colour.
        if root.winfo_viewable():
            root.withdraw()
            root.deiconify()
    except Exception:
        pass
