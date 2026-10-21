# UI Redesign Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give the Eroge Translator control UI a modern dark + light-blue look — better tabs, bigger drop zones with drag feedback, and a card-based layout — without changing any translation/install/server logic.

**Architecture:** A new `theme.py` owns the entire visual language (palette, fonts, a `ttk` "clam"-based dark style, dark Win11 title bar). `ui.py` calls `theme.apply_theme(root)` once, then rebuilds its two tabs into titled "cards" and swaps the two flat drop labels for tall drop zones that highlight on drag-over. All worker threads, the queue pump, drag-drop path resolution, and every translate/install/server call are untouched — this is presentation + layout only.

**Tech Stack:** Python 3 stdlib + `tkinterdnd2` (existing). `tkinter`/`tkinter.ttk`, `ctypes` (dark title bar). No new dependencies.

## Global Constraints

- **Stack:** stdlib only + `tkinterdnd2`. No new dependencies, no venv, no packaging. (verbatim from spec §2)
- **Always run with `PYTHONUTF8=1`** — CJK breaks otherwise. All commands below include it.
- **No git in this repo** — there is no undo. Back up a file before its first edit; the per-task gate is the smoke test, not a commit.
- **Presentation + layout ONLY.** Worker threads, `_poll_queue`, the health thread, `_resolve_dropped_path`, and every `core`/`translate`/`install_game`/`server` call stay behaviorally identical. Every existing widget attribute name (`self.t_drop`, `self.t_run_btn`, `self.t_prog`, `self.t_feed`, `self.x_drop`, `self.x_install_btn`, `self.x_log`, `self.status_server_var`, …) and every `command=`/`state=` binding MUST be preserved — the rest of the class references them. If a redesign step would require touching logic, STOP and flag it.
- **Dark theme only** ships now (light/auto out of scope). Accent = light-blue `#5aa9e6`.
- Spec: `docs/2026-07-12... ` n/a — this plan implements `docs/2026-07-22-ui-redesign-and-speed-fix.md` §2.

---

### Task 1: `theme.py` — palette, fonts, styles, dark title bar

**Files:**
- Create: `theme.py`
- Test: `test_ui_smoke.py` (created here; extended in later tasks)

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `PALETTE: dict[str,str]` and `FONTS: dict[str,tuple]` (module constants).
  - `apply_theme(root) -> (palette: dict, fonts: dict)` — installs the dark ttk theme on `root`, returns the palette+fonts dicts for styling non-ttk widgets.
  - `enable_dark_titlebar(root) -> None` — best-effort Win11 dark title bar; no-op on failure/non-Windows.
  - ttk style names created: `Heading.TLabel`, `Muted.TLabel`, `Card.TLabelframe`(+`.Label`), `Card.TFrame`, `Card.TLabel`, `CardMuted.TLabel`, `Accent.TButton`, `Secondary.TButton`, `Danger.TButton`, plus restyled `TNotebook`/`TNotebook.Tab`/`TEntry`/`TCombobox`/`TProgressbar`.

- [ ] **Step 1: Write the failing test** — create `test_ui_smoke.py`:

```python
# -*- coding: utf-8 -*-
"""Headless smoke test for the redesigned UI. Builds widgets once and destroys
them, asserting no exception — guards against bad ttk style names, bad tk widget
options, and layout errors. Appearance is NOT checked (human-verified). Skips
cleanly when no desktop/display is available.

Run: PYTHONUTF8=1 python test_ui_smoke.py
"""
import os
import sys
import unittest
import tkinter as tk
from tkinter import ttk

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)


class ThemeTest(unittest.TestCase):
    def _root(self):
        try:
            r = tk.Tk()
        except tk.TclError as e:
            self.skipTest("no display: %r" % e)
        r.withdraw()
        return r

    def test_apply_theme_returns_palette_and_styles_usable(self):
        import theme
        root = self._root()
        try:
            palette, fonts = theme.apply_theme(root)
            for key in ("bg", "surface", "text", "accent", "drop_idle",
                        "drop_hover", "danger", "success"):
                self.assertIn(key, palette)
            self.assertIn("mono", fonts)
            # every custom style must be creatable without raising
            for st in ("Accent.TButton", "Secondary.TButton", "Danger.TButton"):
                ttk.Button(root, text="x", style=st).update_idletasks()
            ttk.LabelFrame(root, text="c", style="Card.TLabelframe").update_idletasks()
            theme.enable_dark_titlebar(root)  # must not raise
        finally:
            root.destroy()


if __name__ == "__main__":
    unittest.main(verbosity=2)
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `PYTHONUTF8=1 python test_ui_smoke.py`
Expected: FAIL — `ModuleNotFoundError: No module named 'theme'` (or SKIP if the environment has no display; if it skips, that is an environment limit — proceed and rely on the visual gate in Task 6).

- [ ] **Step 3: Write `theme.py`** (complete module):

```python
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
    """Best-effort dark Windows-11 title bar. No-op off Windows or on failure."""
    if not sys.platform.startswith("win"):
        return
    try:
        import ctypes
        root.update_idletasks()
        hwnd = ctypes.windll.user32.GetParent(root.winfo_id())
        DWMWA_USE_IMMERSIVE_DARK_MODE = 20
        val = ctypes.c_int(1)
        ctypes.windll.dwmapi.DwmSetWindowAttribute(
            hwnd, DWMWA_USE_IMMERSIVE_DARK_MODE,
            ctypes.byref(val), ctypes.sizeof(val))
    except Exception:
        pass
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `PYTHONUTF8=1 python test_ui_smoke.py`
Expected: PASS (`test_apply_theme_returns_palette_and_styles_usable`), or SKIP if no display. If PASS, the theme module is sound.

- [ ] **Step 5: Checkpoint** (no git): confirm the smoke test is green (or skipped) before moving on. `theme.py` is new, so no backup needed.

---

### Task 2: Wire the theme into `ui.py` (base skin, unchanged layout)

**Files:**
- Modify: `ui.py` — `__init__` (apply theme, window size), `_build_status_line` (restyle), add helpers `_card`, `_make_dropzone`, `_style_text_widget`; extend `_register_drop` for drag feedback.

**Interfaces:**
- Consumes: `theme.apply_theme`, `theme.enable_dark_titlebar`, `theme.PALETTE`/`FONTS` via the returned dicts.
- Produces: `self.palette`, `self.fonts` (used by every later task); helpers `self._card(parent, title)`, `self._make_dropzone(parent, text, handler)`, `self._style_text_widget(widget)`; a `_register_drop` that also highlights on `<<DropEnter>>`/`<<DropLeave>>`.

- [ ] **Step 1: Back up `ui.py`** (first edit — no git):

Run: `cp ui.py ui.py.pre-redesign.bak`
Expected: `ui.py.pre-redesign.bak` exists.

- [ ] **Step 2: Add the import** — after the existing `import install_game` line near the top of `ui.py`, add:

```python
import theme            # noqa: E402
```

- [ ] **Step 3: Apply the theme in `__init__`** — replace the current three lines in `App.__init__`:

```python
        root.title("Eroge Translator")
        root.geometry("820x680")
        root.minsize(680, 560)
```

with:

```python
        root.title("Eroge Translator")
        self.palette, self.fonts = theme.apply_theme(root)
        root.geometry("900x780")
        root.minsize(760, 620)
        theme.enable_dark_titlebar(root)
```

- [ ] **Step 4: Add the three helpers + drag feedback.** Replace the whole existing `_register_drop` method with the helpers below (drop-in — same method name plus three new ones):

```python
    def _card(self, parent, title):
        """A titled 'card' container (surface background)."""
        return ttk.LabelFrame(parent, text=title, style="Card.TLabelframe")

    def _make_dropzone(self, parent, text, handler):
        """A tall drop target that highlights while a file is dragged over it."""
        p = self.palette
        dz = tk.Label(parent, text=text, justify="center",
                      bg=p["drop_idle"], fg=p["text_muted"], font=self.fonts["big"],
                      height=5, bd=0, highlightthickness=2,
                      highlightbackground=p["border"], highlightcolor=p["border"])
        self._register_drop(dz, handler)
        return dz

    def _style_text_widget(self, w):
        """Dark styling for a tk Text / ScrolledText."""
        p = self.palette
        w.config(bg=p["surface"], fg=p["text"], insertbackground=p["text"],
                 selectbackground=p["accent"], selectforeground=p["accent_fg"],
                 relief="flat", borderwidth=0, font=self.fonts["mono"])

    def _register_drop(self, widget, handler):
        if not self.dnd_ok:
            return
        p = self.palette

        def _idle():
            widget.config(bg=p["drop_idle"], fg=p["text_muted"],
                          highlightbackground=p["border"], highlightcolor=p["border"])

        def on_enter(event):
            widget.config(bg=p["drop_hover"], fg=p["text"],
                          highlightbackground=p["accent"], highlightcolor=p["accent"])
            return event.action

        def on_leave(event):
            _idle()

        def on_drop(event):
            _idle()
            return handler(event)

        try:
            widget.drop_target_register(DND_FILES)
            widget.dnd_bind("<<DropEnter>>", on_enter)
            widget.dnd_bind("<<DropLeave>>", on_leave)
            widget.dnd_bind("<<Drop>>", on_drop)
        except Exception as e:  # pragma: no cover
            sys.stderr.write("drop registration failed: %r\n" % e)
```

Note: the old `_register_drop` bound only `<<Drop>>` to `handler`; the new one wraps it so the highlight resets on drop. `handler` (`_on_drop_translate` / `_on_drop_game`) and `_resolve_dropped_path` are unchanged.

- [ ] **Step 5: Restyle the status bar.** Replace the whole `_build_status_line` method with:

```python
    def _build_status_line(self):
        p = self.palette
        bar = tk.Frame(self.root, bg=p["surface_alt"])
        bar.pack(side="bottom", fill="x")
        inner = tk.Frame(bar, bg=p["surface_alt"])
        inner.pack(fill="x", padx=10, pady=5)
        self.status_server_var = tk.StringVar(value="○ Server stopped")
        self.status_action_var = tk.StringVar(value="idle")
        self.status_server_lbl = tk.Label(
            inner, textvariable=self.status_server_var, bg=p["surface_alt"],
            fg=p["text_muted"], font=self.fonts["small"])
        self.status_server_lbl.pack(side="left")
        tk.Label(inner, text="│", bg=p["surface_alt"], fg=p["border"]).pack(
            side="left", padx=8)
        tk.Label(inner, textvariable=self.status_action_var, bg=p["surface_alt"],
                 fg=p["text_muted"], font=self.fonts["small"]).pack(side="left")
        if not self.dnd_ok:
            tk.Label(inner, text="(drag-drop unavailable — use Browse)",
                     bg=p["surface_alt"], fg=p["danger"],
                     font=self.fonts["small"]).pack(side="right")
```

Note: `status_server_var` text is still set verbatim by `_on_health` (`"Server ● running :%d"` / `"Server ○ stopped"`) — that logic is untouched. The initial value string changed only cosmetically.

- [ ] **Step 6: Extend the smoke test** — add this method to `ThemeTest`'s file `test_ui_smoke.py` as a new class:

```python
class UIBuildTest(unittest.TestCase):
    def _root(self):
        try:
            r = tk.Tk()
        except tk.TclError as e:
            self.skipTest("no display: %r" % e)
        r.withdraw()
        return r

    def test_full_ui_builds_without_error(self):
        import ui
        root = self._root()
        try:
            ui.App(root, dnd_ok=False)   # dnd off: no tkinterdnd2 needed to build
            root.update_idletasks()      # force realization -> surfaces style errors
        finally:
            root.destroy()
```

- [ ] **Step 7: Run the smoke test**

Run: `PYTHONUTF8=1 python test_ui_smoke.py`
Expected: PASS (both tests) or SKIP (no display). The existing layout now renders in dark; drop zones are still the old flat labels (replaced in Task 3).

- [ ] **Step 8: Checkpoint** — smoke green/skip. Proceed.

---

### Task 3: Rebuild the Translate tab as cards + big drop zone

**Files:**
- Modify: `ui.py` — replace the whole `_build_translate_tab` method.

**Interfaces:**
- Consumes: `self._card`, `self._make_dropzone`, `self._style_text_widget` (Task 2); `LANG_CHOICES` (existing module constant).
- Produces: same widget attributes as before — `self.t_drop`, `self.t_path_var`, `self.t_lang_var`, `self.t_run_btn`, `self.t_stop_btn`, `self.t_open_btn`, `self.t_prog`, `self.t_prog_var`, `self.t_feed` — with identical `command=`/`state=` bindings.

- [ ] **Step 1: Replace `_build_translate_tab`** with:

```python
    def _build_translate_tab(self, parent):
        parent.columnconfigure(0, weight=1)
        parent.rowconfigure(2, weight=1)

        # --- Input card ---
        input_card = self._card(parent, "  Input  ")
        input_card.grid(row=0, column=0, sticky="ew", pady=(0, 10))
        input_card.columnconfigure(0, weight=1)
        self.t_drop = self._make_dropzone(
            input_card, "⬇\n\nDrop an MTool .json here", self._on_drop_translate)
        self.t_drop.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        pathrow = ttk.Frame(input_card, style="Card.TFrame")
        pathrow.grid(row=1, column=0, sticky="ew")
        pathrow.columnconfigure(0, weight=1)
        self.t_path_var = tk.StringVar(value="(no file selected)")
        ttk.Entry(pathrow, textvariable=self.t_path_var, state="readonly").grid(
            row=0, column=0, sticky="ew")
        ttk.Button(pathrow, text="Browse…", style="Secondary.TButton",
                   command=self._browse_translate).grid(row=0, column=1, padx=(8, 0))

        # --- Options card ---
        opt_card = self._card(parent, "  Options  ")
        opt_card.grid(row=1, column=0, sticky="ew", pady=(0, 10))
        ttk.Label(opt_card, text="Target language", style="Card.TLabel").pack(
            side="left")
        self.t_lang_var = tk.StringVar(value=LANG_CHOICES[0][0])
        ttk.Combobox(opt_card, textvariable=self.t_lang_var, state="readonly",
                     width=22, values=[c[0] for c in LANG_CHOICES]).pack(
            side="left", padx=(8, 16))
        self.t_run_btn = ttk.Button(opt_card, text="Translate",
                                    style="Accent.TButton",
                                    command=self._start_translate)
        self.t_run_btn.pack(side="left")
        self.t_stop_btn = ttk.Button(opt_card, text="Stop", state="disabled",
                                     style="Danger.TButton",
                                     command=self._stop_translate)
        self.t_stop_btn.pack(side="left", padx=(8, 0))
        self.t_open_btn = ttk.Button(opt_card, text="Open output folder",
                                     state="disabled", style="Secondary.TButton",
                                     command=self._open_output_folder)
        self.t_open_btn.pack(side="right")

        # --- Progress & Feed card ---
        feed_card = self._card(parent, "  Progress & Feed  ")
        feed_card.grid(row=2, column=0, sticky="nsew")
        feed_card.columnconfigure(0, weight=1)
        feed_card.rowconfigure(2, weight=1)
        self.t_prog = ttk.Progressbar(feed_card, mode="determinate", maximum=100)
        self.t_prog.grid(row=0, column=0, sticky="ew")
        self.t_prog_var = tk.StringVar(value="")
        ttk.Label(feed_card, textvariable=self.t_prog_var,
                  style="CardMuted.TLabel").grid(row=1, column=0, sticky="w",
                                                 pady=(4, 6))
        self.t_feed = scrolledtext.ScrolledText(feed_card, height=14, wrap="none",
                                                state="disabled")
        self.t_feed.grid(row=2, column=0, sticky="nsew")
        self._style_text_widget(self.t_feed)
```

- [ ] **Step 2: Run the smoke test**

Run: `PYTHONUTF8=1 python test_ui_smoke.py`
Expected: PASS/SKIP — the Translate tab builds with cards + the tall drop zone.

- [ ] **Step 3: Checkpoint** — smoke green/skip. Proceed.

---

### Task 4: Rebuild the XUnity tab as cards + big drop zone

**Files:**
- Modify: `ui.py` — replace the whole `_build_xunity_tab` method.

**Interfaces:**
- Consumes: `self._card`, `self._make_dropzone`, `self._style_text_widget`, `self.host`, `self.port`, `LANG_CHOICES`.
- Produces: same attributes — `self.x_srv_status`, `self.x_srv_start`, `self.x_srv_stop`, `self.x_drop`, `self.x_path_var`, `self.x_detect_var`, `self.x_lang_var`, `self.x_install_btn`, `self.x_reapply_btn`, `self.x_reconf_btn`, `self.x_uninstall_btn`, `self.x_log` — identical `command=`/`state=` bindings.

- [ ] **Step 1: Replace `_build_xunity_tab`** with:

```python
    def _build_xunity_tab(self, parent):
        parent.columnconfigure(0, weight=1)
        parent.rowconfigure(3, weight=1)

        # --- Server card ---
        srv = self._card(parent, "  Live translation server  ")
        srv.grid(row=0, column=0, sticky="ew", pady=(0, 10))
        self.x_srv_status = tk.StringVar(value="○ stopped")
        ttk.Label(srv, textvariable=self.x_srv_status, width=14,
                  style="Card.TLabel").pack(side="left")
        self.x_srv_start = ttk.Button(srv, text="Start", style="Accent.TButton",
                                      command=self._start_server)
        self.x_srv_start.pack(side="left", padx=(0, 6))
        self.x_srv_stop = ttk.Button(srv, text="Stop", state="disabled",
                                     style="Danger.TButton",
                                     command=self._stop_server)
        self.x_srv_stop.pack(side="left")
        ttk.Label(srv, text="http://%s:%d" % (self.host, self.port),
                  style="CardMuted.TLabel").pack(side="right")

        # --- Game card ---
        game = self._card(parent, "  Game  ")
        game.grid(row=1, column=0, sticky="ew", pady=(0, 10))
        game.columnconfigure(0, weight=1)
        self.x_drop = self._make_dropzone(
            game, "⬇\n\nDrop a Unity game folder here", self._on_drop_game)
        self.x_drop.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        gpath = ttk.Frame(game, style="Card.TFrame")
        gpath.grid(row=1, column=0, sticky="ew")
        gpath.columnconfigure(0, weight=1)
        self.x_path_var = tk.StringVar(value="(no game selected)")
        ttk.Entry(gpath, textvariable=self.x_path_var, state="readonly").grid(
            row=0, column=0, sticky="ew")
        ttk.Button(gpath, text="Browse…", style="Secondary.TButton",
                   command=self._browse_game).grid(row=0, column=1, padx=(8, 0))
        detrow = ttk.Frame(game, style="Card.TFrame")
        detrow.grid(row=2, column=0, sticky="ew", pady=(8, 0))
        self.x_detect_var = tk.StringVar(value="No game selected.")
        ttk.Label(detrow, textvariable=self.x_detect_var,
                  style="Card.TLabel").pack(side="left")
        ttk.Label(detrow, text="Language", style="CardMuted.TLabel").pack(
            side="right", padx=(0, 6))
        self.x_lang_var = tk.StringVar(value=LANG_CHOICES[0][0])
        ttk.Combobox(detrow, textvariable=self.x_lang_var, state="readonly",
                     width=20, values=[c[0] for c in LANG_CHOICES]).pack(
            side="right")

        # --- Actions card ---
        act = self._card(parent, "  Actions  ")
        act.grid(row=2, column=0, sticky="ew", pady=(0, 10))
        self.x_install_btn = ttk.Button(
            act, text="Install", state="disabled", style="Accent.TButton",
            command=lambda: self._game_action("install"))
        self.x_reapply_btn = ttk.Button(
            act, text="Re-apply (redo)", state="disabled", style="Secondary.TButton",
            command=lambda: self._game_action("install"))
        self.x_reconf_btn = ttk.Button(
            act, text="Reconfigure only", state="disabled",
            style="Secondary.TButton",
            command=lambda: self._game_action("reconfigure"))
        self.x_uninstall_btn = ttk.Button(
            act, text="Uninstall", state="disabled", style="Danger.TButton",
            command=lambda: self._game_action("uninstall"))
        for b in (self.x_install_btn, self.x_reapply_btn, self.x_reconf_btn,
                  self.x_uninstall_btn):
            b.pack(side="left", padx=(0, 6))

        # --- Log card ---
        logc = self._card(parent,
                          "  Activity log (install steps + live server feed)  ")
        logc.grid(row=3, column=0, sticky="nsew")
        logc.columnconfigure(0, weight=1)
        logc.rowconfigure(0, weight=1)
        self.x_log = scrolledtext.ScrolledText(logc, height=14, wrap="none",
                                               state="disabled")
        self.x_log.grid(row=0, column=0, sticky="nsew")
        self._style_text_widget(self.x_log)
```

- [ ] **Step 2: Run the smoke test**

Run: `PYTHONUTF8=1 python test_ui_smoke.py`
Expected: PASS/SKIP — both tabs now build fully with cards + tall drop zones.

- [ ] **Step 3: Checkpoint** — smoke green/skip. Proceed.

---

### Task 5: Regression — confirm behavior is untouched (static diff review)

**Files:**
- Review only: `ui.py` vs `ui.py.pre-redesign.bak`.

**Interfaces:** none.

- [ ] **Step 1: Diff and audit** — verify only presentation changed:

Run: `diff ui.py.pre-redesign.bak ui.py`
Expected: changes are confined to `__init__` (theme/geometry lines), `_build_status_line`, `_register_drop`, `_build_translate_tab`, `_build_xunity_tab`, the new helper methods, and the `import theme` line. **No** change to `_poll_queue`, `_translate_worker`, `_start_translate`, `_game_action*`, `_server_reader`, `_health_loop`, `_on_*`, `_resolve_dropped_path`, `_append_feed`, `_append_text`, `_trim_text`, or `main`.

- [ ] **Step 2: Grep for preserved attribute names** — each must still exist exactly once as an assignment:

Run: `PYTHONUTF8=1 python -c "import re,io; s=open('ui.py',encoding='utf-8').read(); [print(n, 'OK' if (n+' =' in s or 'self.'+n in s) else 'MISSING') for n in ['t_drop','t_run_btn','t_stop_btn','t_open_btn','t_prog','t_prog_var','t_feed','x_drop','x_install_btn','x_reapply_btn','x_reconf_btn','x_uninstall_btn','x_log','x_srv_start','x_srv_stop','status_server_var','status_action_var']]"`
Expected: every name prints `OK`.

- [ ] **Step 3: Byte-compile check**

Run: `PYTHONUTF8=1 python -m py_compile ui.py theme.py test_ui_smoke.py`
Expected: no output (all compile).

- [ ] **Step 4: Checkpoint** — proceed to the human gate.

---

### Task 6: Human visual gate (live app)

**Files:** none (manual verification by Kano).

**Interfaces:** none.

- [ ] **Step 1: Launch the app**

Run: `run-ui.bat` (or `PYTHONUTF8=1 python ui.py`)
Expected: window opens dark with a dark title bar; two flat tabs with a light-blue selected indicator.

- [ ] **Step 2: Verify — Translate tab**
  - [ ] Three cards visible: Input / Options / Progress & Feed.
  - [ ] Drop zone is tall; dragging a `.json` over it highlights it (bg + accent border); dropping fills the path and clears the highlight.
  - [ ] `Browse…` opens the file picker and fills the path.
  - [ ] `Translate` (accent) starts a run; progress bar + feed update live; `Stop` (red) enables and works; `Open output folder` enables on completion.

- [ ] **Step 3: Verify — XUnity tab**
  - [ ] Four cards: Server / Game / Actions / Log.
  - [ ] `Start`/`Stop` server toggles; status bar shows `● running :4404` when up.
  - [ ] Dropping / browsing a game folder runs detection; the correct Install / Re-apply / Reconfigure / Uninstall buttons enable; the activity log streams.

- [ ] **Step 4: Sign-off** — if all boxes are checked, the redesign is done. If anything is off, note it and reopen the relevant task.

---

## Self-Review

**1. Spec coverage (spec §2):**
- §2.1 `theme.py` (palette, fonts, apply_theme, dark titlebar, all style names) → Task 1. ✅
- §2.2 card layout both tabs + button roles + window size → Tasks 2 (size), 3, 4. ✅
- §2.3 bigger drop zones + drag feedback → Task 2 (`_make_dropzone`, `_register_drop`), applied in 3 & 4. ✅
- §2.4 ScrolledText styling → Task 2 (`_style_text_widget`), applied in 3 & 4. ✅
- §2 "zero logic change" → Task 5 (diff + attr grep). ✅
- §2.5 out of scope (light/auto) — not built. ✅
- §4 verification (smoke + visual) → smoke test in Tasks 1–4, visual gate Task 6. ✅

**2. Placeholder scan:** No TBD/TODO; every code step contains full code; every command has an expected result. ✅

**3. Type/name consistency:** `apply_theme` returns `(palette, fonts)` (Task 1) and is unpacked into `self.palette, self.fonts` (Task 2), used by `_make_dropzone`/`_style_text_widget`/`_register_drop` (Task 2) and every tab task. Style names created in Task 1 (`Card.TFrame`, `Card.TLabel`, `CardMuted.TLabel`, `Accent/Secondary/Danger.TButton`, `Card.TLabelframe`) exactly match those referenced in Tasks 3–4. Widget attribute names in Tasks 3–4 match the originals asserted in Task 5. ✅
