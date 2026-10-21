#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Eroge Translator — native desktop control UI.

A thin Tkinter front-end over the existing toolkit. Two tabs:

  * Translate — pick/drop an MTool-exported JSON, choose a target language, run
    the offline batch translator (translate.run_translation) on a worker thread,
    watch progress + a live source->translation feed, open the output folder.
  * XUnity — start/stop the live translation server (server.py), pick/drop a
    Unity game folder, detect its engine/arch + applied state, and
    install / re-apply / reconfigure / uninstall the XUnity patch, all streamed
    into one combined log alongside the live server feed.

Design rules honored here:
  * NO translation logic lives here — it drives core/translate/install_game.
  * Long jobs run on WORKER THREADS. Tk widgets are touched ONLY from the main
    thread; workers hand updates back through a queue.Queue polled with after().
    The window must never freeze.
  * Drag-drop yields a real filesystem PATH (via tkinterdnd2), never an upload.

Run (Windows):  PYTHONUTF8=1 python3 ui.py
"""

import os
import queue
import subprocess
import sys
import threading
import time

import tkinter as tk
from tkinter import filedialog, ttk
from tkinter import scrolledtext

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import core            # noqa: E402
import translate       # noqa: E402
import install_game    # noqa: E402
import theme           # noqa: E402

# tkinterdnd2 is the drag-drop provider. Import is guarded so a broken install
# degrades to Browse-only rather than crashing the app on startup.
try:
    from tkinterdnd2 import TkinterDnD, DND_FILES
    _DND_IMPORTED = True
except Exception as _e:  # pragma: no cover - environment dependent
    TkinterDnD = None
    DND_FILES = None
    _DND_IMPORTED = False
    _DND_IMPORT_ERR = _e

# Windows: launch the server child process without popping a console window.
_CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# Target-language choices: (menu label, config target_language, XUnity lang code)
LANG_CHOICES = [
    ("Traditional Chinese", "Traditional Chinese", "zh-Hant"),
    ("Simplified Chinese", "Simplified Chinese", "zh-Hans"),
    ("English", "English", "en"),
]
_LANG_BY_LABEL = {c[0]: c for c in LANG_CHOICES}

MAX_FEED_LINES = 500  # cap the live-feed / log widgets so long jobs stay light


def create_root():
    """Build the Tk root, preferring TkinterDnD (drag-drop). Returns
    (root, dnd_ok). Never raises just because drag-drop is unavailable."""
    if _DND_IMPORTED:
        try:
            return TkinterDnD.Tk(), True
        except Exception as e:  # pragma: no cover - environment dependent
            sys.stderr.write("tkinterdnd2 init failed; drag-drop disabled: %r\n" % e)
    return tk.Tk(), False


class App:
    """The whole control UI. Construct with a Tk root and a dnd_ok flag."""

    def __init__(self, root, dnd_ok):
        self.root = root
        self.dnd_ok = dnd_ok
        self.q = queue.Queue()

        # --- translate-tab runtime state ---
        self.translate_path = None
        self.translate_stop = None
        self.translate_running = False
        self.translate_t0 = 0.0
        self.translate_out_path = None

        # --- xunity-tab runtime state ---
        self.server_proc = None
        self.server_up = False
        self.game_root = None
        self.game_detect = None
        self._stop_health = threading.Event()

        cfg = core.load_config()
        self.host = cfg.get("xunity_host", "127.0.0.1")
        self.port = int(cfg.get("xunity_port", 4404))

        root.title("Eroge Translator")
        self.palette, self.fonts = theme.apply_theme(root)
        root.geometry("900x780")
        root.minsize(760, 620)
        # DWM ignores the dark-title-bar attribute until the window is mapped,
        # so apply it after the event loop has shown the window.
        root.after(60, lambda: theme.enable_dark_titlebar(root))

        self._build_notebook()
        self._build_status_line()

        # start the pumps (these only schedule; they run under mainloop)
        root.after(80, self._poll_queue)
        self._start_health_thread()
        root.after(300, self._refresh_health_once)

        root.protocol("WM_DELETE_WINDOW", self._on_close)

    # ------------------------------------------------------------------ #
    # layout
    # ------------------------------------------------------------------ #
    def _build_notebook(self):
        self.nb = ttk.Notebook(self.root)
        self.tab_translate = ttk.Frame(self.nb, padding=10)
        self.tab_xunity = ttk.Frame(self.nb, padding=10)
        self.nb.add(self.tab_translate, text="  Translate  ")
        self.nb.add(self.tab_xunity, text="  XUnity  ")
        self.nb.pack(side="top", fill="both", expand=True)
        self._build_translate_tab(self.tab_translate)
        self._build_xunity_tab(self.tab_xunity)

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

    # ---- Tab 1: Translate -------------------------------------------- #
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

    # ---- Tab 2: XUnity ----------------------------------------------- #
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

    # ------------------------------------------------------------------ #
    # drag-drop
    # ------------------------------------------------------------------ #
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

    def _resolve_dropped_path(self, data):
        """Turn tkinterdnd2 drop payload into a single real filesystem path.
        tk.splitlist correctly unwraps the {braces} it puts around paths that
        contain spaces, and splits multiple dropped items — we take the first."""
        try:
            items = self.root.tk.splitlist(data)
        except Exception:
            items = [data.strip().strip("{}")]
        return items[0] if items else ""

    def _on_drop_translate(self, event):
        path = self._resolve_dropped_path(event.data)
        if path:
            self._set_translate_path(path)

    def _on_drop_game(self, event):
        path = self._resolve_dropped_path(event.data)
        if path:
            self._set_game_path(path)

    # ------------------------------------------------------------------ #
    # Translate tab logic
    # ------------------------------------------------------------------ #
    def _browse_translate(self):
        path = filedialog.askopenfilename(
            title="Select an MTool-exported JSON",
            filetypes=[("JSON files", "*.json"), ("All files", "*.*")])
        if path:
            self._set_translate_path(path)

    def _set_translate_path(self, path):
        path = os.path.normpath(path)
        if not os.path.isfile(path):
            self.t_path_var.set("(not a file) %s" % path)
            self.translate_path = None
            return
        self.translate_path = path
        self.t_path_var.set(path)
        root, ext = os.path.splitext(path)
        self.translate_out_path = root + ".translated" + ext
        if os.path.exists(self.translate_out_path):
            self.status_action_var.set("ready — output exists, will RESUME")
        else:
            self.status_action_var.set("ready to translate")

    def _start_translate(self):
        if self.translate_running:
            return
        if not self.translate_path:
            self.status_action_var.set("pick a .json first")
            return
        label = self.t_lang_var.get()
        target = _LANG_BY_LABEL.get(label, LANG_CHOICES[0])[1]

        cfg = core.load_config()
        cfg["target_language"] = target
        glossary = core.load_glossary()
        mode = core.get_mode(cfg)
        in_path = self.translate_path
        out_path = self.translate_out_path
        resuming = os.path.exists(out_path)

        self.translate_stop = threading.Event()
        self.translate_running = True
        self.translate_t0 = time.time()
        self.t_run_btn.config(state="disabled")
        self.t_stop_btn.config(state="normal")
        self.t_open_btn.config(state="disabled")
        self.t_prog.config(mode="determinate", maximum=100, value=0)
        self.t_prog_var.set("resuming…" if resuming else "starting…")
        self._clear_text(self.t_feed)
        self.status_action_var.set(
            "translating (%s)%s" % (target, " — resuming" if resuming else ""))

        t = threading.Thread(target=self._translate_worker,
                             args=(in_path, out_path, cfg, glossary, mode),
                             daemon=True)
        t.start()

    def _translate_worker(self, in_path, out_path, cfg, glossary, mode):
        def on_progress(done, total, failures):
            self.q.put(("t_progress", done, total, failures))

        def on_line(src, tr):
            self.q.put(("t_line", src, tr))

        try:
            summary = translate.run_translation(
                in_path, out_path, cfg, glossary, mode,
                on_progress=on_progress, on_line=on_line,
                stop_flag=self.translate_stop)
            self.q.put(("t_done", summary))
        except Exception as e:
            self.q.put(("t_error", repr(e)))

    def _stop_translate(self):
        if self.translate_stop is not None:
            self.translate_stop.set()
            self.status_action_var.set("stopping after this batch…")
            self.t_stop_btn.config(state="disabled")

    def _open_output_folder(self):
        path = self.translate_out_path
        if path and os.path.exists(path):
            try:
                os.startfile(os.path.dirname(os.path.abspath(path)))
            except Exception as e:
                self.status_action_var.set("could not open folder: %r" % e)

    # ------------------------------------------------------------------ #
    # XUnity tab logic — server
    # ------------------------------------------------------------------ #
    def _start_server(self):
        if self.server_proc is not None and self.server_proc.poll() is None:
            return
        env = dict(os.environ)
        env["PYTHONUTF8"] = "1"
        try:
            self.server_proc = subprocess.Popen(
                [sys.executable, os.path.join(HERE, "server.py")],
                cwd=HERE, env=env,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace",
                bufsize=1, creationflags=_CREATE_NO_WINDOW)
        except Exception as e:
            self._xlog("Failed to start server: %r" % e)
            return
        self._xlog("Starting server (PYTHONUTF8=1)…")
        self.x_srv_start.config(state="disabled")
        self.x_srv_stop.config(state="normal")
        threading.Thread(target=self._server_reader,
                         args=(self.server_proc,), daemon=True).start()

    def _server_reader(self, proc):
        try:
            for line in proc.stdout:
                self.q.put(("x_serverlog", line.rstrip("\n")))
        except Exception as e:
            self.q.put(("x_serverlog", "[reader error: %r]" % e))
        self.q.put(("x_serverexit", proc.poll()))

    def _stop_server(self):
        proc = self.server_proc
        if proc is None or proc.poll() is not None:
            return
        self._xlog("Stopping server…")
        try:
            proc.terminate()
        except Exception as e:
            self._xlog("terminate failed: %r" % e)
        # escalate to kill on a short delay, off the UI thread
        threading.Thread(target=self._ensure_killed, args=(proc,),
                         daemon=True).start()

    def _ensure_killed(self, proc):
        try:
            proc.wait(timeout=4)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass

    # ------------------------------------------------------------------ #
    # XUnity tab logic — game detection / install
    # ------------------------------------------------------------------ #
    def _browse_game(self):
        path = filedialog.askdirectory(title="Select the Unity game folder")
        if path:
            self._set_game_path(path)

    def _set_game_path(self, path):
        path = os.path.normpath(path)
        self.x_path_var.set(path)
        self.x_detect_var.set("detecting…")
        self._set_game_buttons(supported=False, applied=False, busy=True)
        threading.Thread(target=self._detect_worker, args=(path,),
                         daemon=True).start()

    def _detect_worker(self, path):
        try:
            d = install_game.detect_game(path)
            applied = install_game.is_applied(path) if d.get("ok") else False
        except Exception as e:
            self.q.put(("x_detect", {"ok": False, "error": repr(e),
                                     "root": None}, False))
            return
        self.q.put(("x_detect", d, applied))

    def _apply_detect(self, d, applied):
        self.game_detect = d
        self.game_root = d.get("root")
        if not d.get("ok"):
            self.x_detect_var.set("Unsupported: %s" % (d.get("error") or "not a Unity game"))
            self._set_game_buttons(supported=False, applied=False)
            return
        label = d.get("label", "?")
        if not d.get("supported"):
            self.x_detect_var.set("%s — unsupported (%s)"
                                  % (label, d.get("error") or "no template"))
            self._set_game_buttons(supported=False, applied=applied)
            return
        state = "applied" if applied else "not applied"
        if d.get("experimental"):
            self.x_detect_var.set("%s — EXPERIMENTAL · %s" % (label, state))
        else:
            self.x_detect_var.set("%s ✓ — %s" % (label, state))
        self._set_game_buttons(supported=True, applied=applied)

    def _set_game_buttons(self, supported, applied, busy=False):
        def st(on):
            return "normal" if (on and not busy) else "disabled"
        # not applied -> Install; applied -> Re-apply / Reconfigure / Uninstall
        self.x_install_btn.config(state=st(supported and not applied))
        self.x_reapply_btn.config(state=st(supported and applied))
        self.x_reconf_btn.config(state=st(supported and applied))
        self.x_uninstall_btn.config(state=st(applied))

    def _game_action(self, action):
        if not self.game_root:
            return
        label = self.x_lang_var.get()
        lang = _LANG_BY_LABEL.get(label, LANG_CHOICES[0])[2]
        self._set_game_buttons(supported=False, applied=False, busy=True)
        self.status_action_var.set("%s…" % action)
        self._xlog("=== %s (%s) ===" % (action, lang))
        threading.Thread(target=self._game_action_worker,
                         args=(action, self.game_root, lang), daemon=True).start()

    def _game_action_worker(self, action, root, lang):
        def on_log(msg):
            self.q.put(("x_actionlog", msg))
        try:
            if action == "install":
                install_game.install(root, lang, on_log=on_log)
            elif action == "uninstall":
                install_game.uninstall(root, on_log=on_log)
            elif action == "reconfigure":
                res = install_game.reconfigure(root, lang)
                if res.get("changes"):
                    for sec, key, old, new in res["changes"]:
                        on_log("  [%s] %s -> %s" % (sec, key, new))
                else:
                    on_log("No changes needed — already configured.")
                on_log("Config: %s" % res.get("config_path"))
        except Exception as e:
            self.q.put(("x_actionlog", "ERROR: %r" % e))
        self.q.put(("x_actiondone", action, root))

    # ------------------------------------------------------------------ #
    # health polling
    # ------------------------------------------------------------------ #
    def _start_health_thread(self):
        threading.Thread(target=self._health_loop, daemon=True).start()

    def _health_loop(self):
        while not self._stop_health.is_set():
            up, _ = install_game.probe_health(self.host, self.port, timeout=1.0)
            self.q.put(("health", up))
            self._stop_health.wait(2.0)

    def _refresh_health_once(self):
        # a one-shot fast probe so status isn't stale for ~2s at startup
        threading.Thread(
            target=lambda: self.q.put(
                ("health", install_game.probe_health(self.host, self.port, 1.0)[0])),
            daemon=True).start()

    # ------------------------------------------------------------------ #
    # queue pump (all Tk widget writes happen here, on the main thread)
    # ------------------------------------------------------------------ #
    def _poll_queue(self):
        feed_lines = []
        try:
            while True:
                msg = self.q.get_nowait()
                kind = msg[0]
                if kind == "t_progress":
                    self._on_t_progress(msg[1], msg[2], msg[3])
                elif kind == "t_line":
                    feed_lines.append((msg[1], msg[2]))
                elif kind == "t_done":
                    self._on_t_done(msg[1])
                elif kind == "t_error":
                    self._on_t_error(msg[1])
                elif kind == "x_serverlog":
                    self._xlog(msg[1])
                elif kind == "x_serverexit":
                    self._on_server_exit(msg[1])
                elif kind == "x_actionlog":
                    self._xlog(msg[1])
                elif kind == "x_actiondone":
                    self._on_action_done(msg[1], msg[2])
                elif kind == "x_detect":
                    self._apply_detect(msg[1], msg[2])
                elif kind == "health":
                    self._on_health(msg[1])
        except queue.Empty:
            pass
        if feed_lines:
            self._append_feed(feed_lines)
        self.root.after(80, self._poll_queue)

    # ---- queue handlers ---- #
    def _on_t_progress(self, done, total, failures):
        if total:
            self.t_prog.config(maximum=total, value=done)
        elapsed = time.time() - self.translate_t0
        eta = ""
        if done and total and elapsed > 0:
            rate = done / elapsed
            remaining = max(0, total - done)
            if rate > 0:
                eta = " · ETA %s" % self._fmt_eta(remaining / rate)
        self.t_prog_var.set("%d/%d batches · %d failures%s"
                            % (done, total, failures, eta))

    def _on_t_done(self, summary):
        self.translate_running = False
        self.t_run_btn.config(state="normal")
        self.t_stop_btn.config(state="disabled")
        self.t_open_btn.config(state="normal")
        name = os.path.basename(summary.get("out_path", ""))
        if summary.get("stopped"):
            self.t_prog_var.set(
                "STOPPED after %d/%d batches · %d failures — progress saved"
                % (summary["batches_done"], summary["total_batches"],
                   summary["failures"]))
            self.status_action_var.set("stopped — partial output saved (%s)" % name)
        else:
            self.t_prog.config(value=self.t_prog.cget("maximum"))
            self.t_prog_var.set(
                "done · %d batches · %d failures · %.1fs"
                % (summary["total_batches"], summary["failures"],
                   summary["elapsed"]))
            self.status_action_var.set(
                "Wrote %s — import into MTool" % name)

    def _on_t_error(self, err):
        self.translate_running = False
        self.t_run_btn.config(state="normal")
        self.t_stop_btn.config(state="disabled")
        self.t_prog_var.set("error: %s" % err)
        self.status_action_var.set("translate error: %s" % err)

    def _on_server_exit(self, code):
        self.server_proc = None
        self.server_up = False
        self._xlog("[server process exited: %s]" % code)
        self.x_srv_start.config(state="normal")
        self.x_srv_stop.config(state="disabled")
        self._on_health(False)

    def _on_action_done(self, action, root):
        self.status_action_var.set("%s done" % action)
        self._xlog("=== %s complete ===" % action)
        # re-detect to refresh the applied state + button set
        threading.Thread(target=self._detect_worker, args=(root,),
                         daemon=True).start()

    def _on_health(self, up):
        self.server_up = up
        if up:
            self.status_server_var.set("Server ● running :%d" % self.port)
            self.x_srv_status.set("● running")
            self.x_srv_start.config(state="disabled")
            self.x_srv_stop.config(state="normal")
        else:
            self.status_server_var.set("Server ○ stopped")
            self.x_srv_status.set("○ stopped")
            if self.server_proc is None or self.server_proc.poll() is not None:
                self.x_srv_start.config(state="normal")
                self.x_srv_stop.config(state="disabled")

    # ------------------------------------------------------------------ #
    # text-widget helpers
    # ------------------------------------------------------------------ #
    def _clear_text(self, widget):
        widget.config(state="normal")
        widget.delete("1.0", "end")
        widget.config(state="disabled")

    def _append_text(self, widget, text):
        widget.config(state="normal")
        widget.insert("end", text if text.endswith("\n") else text + "\n")
        self._trim_text(widget)
        widget.config(state="disabled")
        widget.see("end")

    def _append_feed(self, pairs):
        self.t_feed.config(state="normal")
        for src, tr in pairs:
            self.t_feed.insert("end", "%s  →  %s\n"
                               % (self._oneline(src), self._oneline(tr)))
        self._trim_text(self.t_feed)
        self.t_feed.config(state="disabled")
        self.t_feed.see("end")

    def _trim_text(self, widget):
        # keep only the last MAX_FEED_LINES lines
        try:
            last = int(widget.index("end-1c").split(".")[0])
        except Exception:
            return
        if last > MAX_FEED_LINES:
            widget.delete("1.0", "%d.0" % (last - MAX_FEED_LINES))

    def _xlog(self, text):
        self._append_text(self.x_log, text)

    @staticmethod
    def _oneline(s, n=120):
        s = s.replace("\r", "").replace("\n", " / ")
        return s if len(s) <= n else s[: n - 1] + "…"

    @staticmethod
    def _fmt_eta(seconds):
        seconds = int(seconds)
        if seconds < 60:
            return "%ds" % seconds
        m, s = divmod(seconds, 60)
        if m < 60:
            return "%dm%02ds" % (m, s)
        h, m = divmod(m, 60)
        return "%dh%02dm" % (h, m)

    # ------------------------------------------------------------------ #
    # shutdown
    # ------------------------------------------------------------------ #
    def _on_close(self):
        self._stop_health.set()
        proc = self.server_proc
        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
            except Exception:
                pass
        if self.translate_stop is not None:
            self.translate_stop.set()
        self.root.destroy()


def main():
    root, dnd_ok = create_root()
    App(root, dnd_ok)
    root.mainloop()


if __name__ == "__main__":
    main()
