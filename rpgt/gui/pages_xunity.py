# -*- coding: utf-8 -*-
"""XUnity / Offline (MTool) page: the original ui.py behaviour, moved intact.

Two sub-tabs: Offline (MTool export -> translate.run_translation on a worker
thread) and XUnity (live server start/stop, game detect, install wizard). No
translation logic here; workers hand results back via app.post() (queue)."""
import os
import subprocess
import sys
import threading
import time
import tkinter as tk
from tkinter import filedialog, ttk

from rpgt import core
from rpgt.unity import install_game
from rpgt.mtool import translate
from rpgt.gui.app_common import (LANG_BY_LABEL, LANG_CHOICES, LogText, dropped_path,
                        fmt_eta, oneline, register_drop_tree)

from rpgt import paths
_CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


class XUnityPage(ttk.Frame):
    def __init__(self, parent, app, start_threads=True):
        super().__init__(parent, padding=12)
        self.app = app
        self.root = app.root
        self.dnd_ok = app.dnd_ok
        # offline-tab state
        self.translate_path = None
        self.translate_stop = None
        self.translate_running = False
        self.translate_t0 = 0.0
        self.translate_out_path = None
        # xunity-tab state
        self.server_proc = None
        self.server_up = False
        self.game_root = None
        self.game_detect = None
        self._stop_health = threading.Event()
        cfg = core.load_config()
        self.host = cfg.get("xunity_host", "127.0.0.1")
        self.port = int(cfg.get("xunity_port", 4404))
        self._feed_buf = []

        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)
        ttk.Label(self, text="XUnity / Offline (MTool)", style="Title.TLabel").grid(
            row=0, column=0, sticky="w", pady=(0, 8))
        self.nb = ttk.Notebook(self)
        self.nb.grid(row=1, column=0, sticky="nsew")
        self.tab_translate = ttk.Frame(self.nb, padding=10)
        self.tab_xunity = ttk.Frame(self.nb, padding=10)
        self.nb.add(self.tab_translate, text="Offline (MTool export)")
        self.nb.add(self.tab_xunity, text="XUnity live server")
        self._build_translate_tab(self.tab_translate)
        self._build_xunity_tab(self.tab_xunity)

        for kind, fn in (("t_progress", self._on_t_progress),
                         ("t_line", self._on_t_line), ("t_done", self._on_t_done),
                         ("t_error", self._on_t_error),
                         ("x_serverlog", self._xlog),
                         ("x_serverexit", self._on_server_exit),
                         ("x_actionlog", self._xlog),
                         ("x_actiondone", self._on_action_done),
                         ("x_detect", self._apply_detect),
                         ("health", self._on_health)):
            app.on(kind, fn)
        app.on_flush(self._flush_feed)
        if start_threads:
            threading.Thread(target=self._health_loop, daemon=True).start()
            self.root.after(300, self._refresh_health_once)

    # ---------------------------------------------------------------- layout
    def _path_row(self, parent, var, browse_cmd, row):
        parent.columnconfigure(0, weight=1)
        e = ttk.Entry(parent, textvariable=var, state="readonly")
        e.grid(row=row, column=0, sticky="ew")
        ttk.Button(parent, text="Browse…", command=browse_cmd).grid(
            row=row, column=1, padx=(8, 0))
        return e

    def _build_translate_tab(self, parent):
        parent.columnconfigure(0, weight=1)
        parent.rowconfigure(2, weight=1)
        inp = ttk.LabelFrame(parent, text="MTool export (.json)", style="Drop.TLabelframe")
        inp.grid(row=0, column=0, sticky="ew", pady=(0, 10))
        self.t_drop = inp
        self.t_path_var = tk.StringVar(value="")
        self._path_row(inp, self.t_path_var, self._browse_translate, 0)
        ttk.Label(inp, text="Choose a file, or drop one onto this box."
                  if self.dnd_ok else "Choose a file.", style="Muted.TLabel").grid(
            row=1, column=0, columnspan=2, sticky="w", pady=(4, 0))
        register_drop_tree(inp, self._on_drop_translate, self.dnd_ok, inp,
                           "DropHover.TLabelframe", "Drop.TLabelframe")

        bar = ttk.Frame(parent)
        bar.grid(row=1, column=0, sticky="ew", pady=(0, 10))
        ttk.Label(bar, text="Target language").pack(side="left")
        self.t_lang_var = tk.StringVar(value=LANG_CHOICES[0][0])
        ttk.Combobox(bar, textvariable=self.t_lang_var, state="readonly", width=22,
                     values=[c[0] for c in LANG_CHOICES]).pack(side="left", padx=(8, 16))
        self.t_run_btn = ttk.Button(bar, text="Translate", style="Accent.TButton",
                                    command=self._start_translate)
        self.t_run_btn.pack(side="left")
        self.t_stop_btn = ttk.Button(bar, text="Stop", state="disabled",
                                     style="Danger.TButton", command=self._stop_translate)
        self.t_stop_btn.pack(side="left", padx=(8, 0))
        self.t_open_btn = ttk.Button(bar, text="Open output folder", state="disabled",
                                     command=self._open_output_folder)
        self.t_open_btn.pack(side="right")

        feed = ttk.LabelFrame(parent, text="Progress")
        feed.grid(row=2, column=0, sticky="nsew")
        feed.columnconfigure(0, weight=1)
        feed.rowconfigure(2, weight=1)
        self.t_prog = ttk.Progressbar(feed, mode="determinate", maximum=100)
        self.t_prog.grid(row=0, column=0, sticky="ew")
        self.t_prog_var = tk.StringVar(value="")
        ttk.Label(feed, textvariable=self.t_prog_var, style="Muted.TLabel").grid(
            row=1, column=0, sticky="w", pady=(4, 6))
        self._t_feed = LogText(feed, self.app.palette, self.app.fonts, height=10)
        self._t_feed.grid(row=2, column=0, sticky="nsew")
        self.t_feed = self._t_feed.text

    def _build_xunity_tab(self, parent):
        parent.columnconfigure(0, weight=1)
        parent.rowconfigure(3, weight=1)
        srv = ttk.LabelFrame(parent, text="Live translation server")
        srv.grid(row=0, column=0, sticky="ew", pady=(0, 10))
        self.x_srv_status = tk.StringVar(value="stopped")
        ttk.Label(srv, textvariable=self.x_srv_status, width=12).pack(side="left")
        self.x_srv_start = ttk.Button(srv, text="Start", style="Accent.TButton",
                                      command=self._start_server)
        self.x_srv_start.pack(side="left", padx=(0, 6))
        self.x_srv_stop = ttk.Button(srv, text="Stop", state="disabled",
                                     style="Danger.TButton", command=self._stop_server)
        self.x_srv_stop.pack(side="left")
        ttk.Label(srv, text="http://%s:%d" % (self.host, self.port),
                  style="Muted.TLabel").pack(side="right")

        game = ttk.LabelFrame(parent, text="Unity game folder", style="Drop.TLabelframe")
        game.grid(row=1, column=0, sticky="ew", pady=(0, 10))
        self.x_drop = game
        self.x_path_var = tk.StringVar(value="")
        self._path_row(game, self.x_path_var, self._browse_game, 0)
        det = ttk.Frame(game)
        det.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        self.x_detect_var = tk.StringVar(
            value="No game selected." + (" Drop a folder here." if self.dnd_ok else ""))
        ttk.Label(det, textvariable=self.x_detect_var).pack(side="left")
        self.x_lang_var = tk.StringVar(value=LANG_CHOICES[0][0])
        ttk.Combobox(det, textvariable=self.x_lang_var, state="readonly", width=20,
                     values=[c[0] for c in LANG_CHOICES]).pack(side="right")
        ttk.Label(det, text="Language", style="Muted.TLabel").pack(
            side="right", padx=(0, 6))
        register_drop_tree(game, self._on_drop_game, self.dnd_ok, game,
                           "DropHover.TLabelframe", "Drop.TLabelframe")

        act = ttk.Frame(parent)
        act.grid(row=2, column=0, sticky="ew", pady=(0, 10))
        self.x_install_btn = ttk.Button(
            act, text="Install", state="disabled", style="Accent.TButton",
            command=lambda: self._game_action("install"))
        self.x_reapply_btn = ttk.Button(
            act, text="Re-apply (redo)", state="disabled",
            command=lambda: self._game_action("install"))
        self.x_reconf_btn = ttk.Button(
            act, text="Reconfigure only", state="disabled",
            command=lambda: self._game_action("reconfigure"))
        self.x_uninstall_btn = ttk.Button(
            act, text="Uninstall", state="disabled", style="Danger.TButton",
            command=lambda: self._game_action("uninstall"))
        for b in (self.x_install_btn, self.x_reapply_btn, self.x_reconf_btn,
                  self.x_uninstall_btn):
            b.pack(side="left", padx=(0, 6))

        logc = ttk.LabelFrame(parent, text="Activity log (install steps + live server feed)")
        logc.grid(row=3, column=0, sticky="nsew")
        logc.columnconfigure(0, weight=1)
        logc.rowconfigure(0, weight=1)
        self._x_log = LogText(logc, self.app.palette, self.app.fonts, height=8)
        self._x_log.grid(row=0, column=0, sticky="nsew")
        self.x_log = self._x_log.text

    # ---------------------------------------------------------------- drop
    def _on_drop_translate(self, event):
        path = dropped_path(self.root, event.data)
        if path:
            self._set_translate_path(path)

    def _on_drop_game(self, event):
        path = dropped_path(self.root, event.data, keep_files=())
        if path:
            self._set_game_path(path)

    # ---------------------------------------------------------------- offline tab
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
        self.app.set_status("ready — output exists, will RESUME"
                            if os.path.exists(self.translate_out_path)
                            else "ready to translate")

    def _start_translate(self):
        if self.translate_running:
            return
        if not self.translate_path:
            self.app.set_status("pick a .json first")
            return
        target = LANG_BY_LABEL.get(self.t_lang_var.get(), LANG_CHOICES[0])[1]
        cfg = core.load_config()
        cfg["target_language"] = target
        glossary = core.load_glossary()
        mode = core.get_mode(cfg)
        in_path, out_path = self.translate_path, self.translate_out_path
        resuming = os.path.exists(out_path)
        self.translate_stop = threading.Event()
        self.translate_running = True
        self.translate_t0 = time.time()
        self.t_run_btn.config(state="disabled")
        self.t_stop_btn.config(state="normal")
        self.t_open_btn.config(state="disabled")
        self.t_prog.config(mode="determinate", maximum=100, value=0)
        self.t_prog_var.set("resuming…" if resuming else "starting…")
        self._t_feed.clear()
        self.app.set_status("translating (%s)%s" % (target, " — resuming" if resuming else ""))
        threading.Thread(target=self._translate_worker,
                         args=(in_path, out_path, cfg, glossary, mode), daemon=True).start()

    def _translate_worker(self, in_path, out_path, cfg, glossary, mode):
        post = self.app.post
        try:
            summary = translate.run_translation(
                in_path, out_path, cfg, glossary, mode,
                on_progress=lambda d, t, f: post("t_progress", d, t, f),
                on_line=lambda s, t: post("t_line", s, t),
                stop_flag=self.translate_stop)
            post("t_done", summary)
        except Exception as e:
            post("t_error", repr(e))

    def _stop_translate(self):
        if self.translate_stop is not None:
            self.translate_stop.set()
            self.app.set_status("stopping after this batch…")
            self.t_stop_btn.config(state="disabled")

    def _open_output_folder(self):
        path = self.translate_out_path
        if path and os.path.exists(path):
            try:
                os.startfile(os.path.dirname(os.path.abspath(path)))
            except Exception as e:
                self.app.set_status("could not open folder: %r" % e)

    # ---------------------------------------------------------------- server
    def _start_server(self):
        if self.server_proc is not None and self.server_proc.poll() is None:
            return
        env = dict(os.environ)
        env["PYTHONUTF8"] = "1"
        try:
            self.server_proc = subprocess.Popen(
                [sys.executable, "-m", "rpgt.unity.server"], cwd=paths.ROOT, env=env,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                encoding="utf-8", errors="replace", bufsize=1,
                creationflags=_CREATE_NO_WINDOW)
        except Exception as e:
            self._xlog("Failed to start server: %r" % e)
            return
        self._xlog("Starting server (PYTHONUTF8=1)…")
        self.x_srv_start.config(state="disabled")
        self.x_srv_stop.config(state="normal")
        threading.Thread(target=self._server_reader, args=(self.server_proc,),
                         daemon=True).start()

    def _server_reader(self, proc):
        try:
            for line in proc.stdout:
                self.app.post("x_serverlog", line.rstrip("\n"))
        except Exception as e:
            self.app.post("x_serverlog", "[reader error: %r]" % e)
        self.app.post("x_serverexit", proc.poll())

    def _stop_server(self):
        proc = self.server_proc
        if proc is None or proc.poll() is not None:
            return
        self._xlog("Stopping server…")
        try:
            proc.terminate()
        except Exception as e:
            self._xlog("terminate failed: %r" % e)
        threading.Thread(target=self._ensure_killed, args=(proc,), daemon=True).start()

    def _ensure_killed(self, proc):
        try:
            proc.wait(timeout=4)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass

    # ---------------------------------------------------------------- game / install
    def _browse_game(self):
        path = filedialog.askdirectory(title="Select the Unity game folder")
        if path:
            self._set_game_path(path)

    def _set_game_path(self, path):
        path = os.path.normpath(path)
        self.x_path_var.set(path)
        self.x_detect_var.set("detecting…")
        self._set_game_buttons(supported=False, applied=False, busy=True)
        threading.Thread(target=self._detect_worker, args=(path,), daemon=True).start()

    def _detect_worker(self, path):
        try:
            d = install_game.detect_game(path)
            applied = install_game.is_applied(path) if d.get("ok") else False
        except Exception as e:
            self.app.post("x_detect", {"ok": False, "error": repr(e), "root": None}, False)
            return
        self.app.post("x_detect", d, applied)

    def _apply_detect(self, d, applied):
        self.game_detect = d
        self.game_root = d.get("root")
        if not d.get("ok"):
            self.x_detect_var.set("Unsupported: %s" % (d.get("error") or "not a Unity game"))
            self._set_game_buttons(supported=False, applied=False)
            return
        label = d.get("label", "?")
        if not d.get("supported"):
            self.x_detect_var.set("%s — unsupported (%s)" % (label, d.get("error") or "no template"))
            self._set_game_buttons(supported=False, applied=applied)
            return
        state = "applied" if applied else "not applied"
        if d.get("experimental"):
            self.x_detect_var.set("%s — EXPERIMENTAL · %s" % (label, state))
        else:
            self.x_detect_var.set("%s — %s" % (label, state))
        self._set_game_buttons(supported=True, applied=applied)

    def _set_game_buttons(self, supported, applied, busy=False):
        def st(on):
            return "normal" if (on and not busy) else "disabled"
        self.x_install_btn.config(state=st(supported and not applied))
        self.x_reapply_btn.config(state=st(supported and applied))
        self.x_reconf_btn.config(state=st(supported and applied))
        self.x_uninstall_btn.config(state=st(applied))

    def _game_action(self, action):
        if not self.game_root:
            return
        lang = LANG_BY_LABEL.get(self.x_lang_var.get(), LANG_CHOICES[0])[2]
        self._set_game_buttons(supported=False, applied=False, busy=True)
        self.app.set_status("%s…" % action)
        self._xlog("=== %s (%s) ===" % (action, lang))
        threading.Thread(target=self._game_action_worker,
                         args=(action, self.game_root, lang), daemon=True).start()

    def _game_action_worker(self, action, root, lang):
        def on_log(msg):
            self.app.post("x_actionlog", msg)
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
            self.app.post("x_actionlog", "ERROR: %r" % e)
        self.app.post("x_actiondone", action, root)

    # ---------------------------------------------------------------- health
    def _health_loop(self):
        while not self._stop_health.is_set():
            up, _ = install_game.probe_health(self.host, self.port, timeout=1.0)
            self.app.post("health", up)
            self._stop_health.wait(2.0)

    def _refresh_health_once(self):
        threading.Thread(
            target=lambda: self.app.post(
                "health", install_game.probe_health(self.host, self.port, 1.0)[0]),
            daemon=True).start()

    # ---------------------------------------------------------------- queue handlers
    def _on_t_progress(self, done, total, failures):
        if total:
            self.t_prog.config(maximum=total, value=done)
        elapsed = time.time() - self.translate_t0
        eta = ""
        if done and total and elapsed > 0:
            rate = done / elapsed
            if rate > 0:
                eta = " · ETA %s" % fmt_eta(max(0, total - done) / rate)
        self.t_prog_var.set("%d/%d batches · %d failures%s" % (done, total, failures, eta))

    def _on_t_line(self, src, tr):
        self._feed_buf.append("%s  →  %s" % (oneline(src), oneline(tr)))

    def _flush_feed(self):
        if self._feed_buf:
            self._t_feed.append(self._feed_buf)
            self._feed_buf = []

    def _on_t_done(self, summary):
        self._flush_feed()
        self.translate_running = False
        self.t_run_btn.config(state="normal")
        self.t_stop_btn.config(state="disabled")
        self.t_open_btn.config(state="normal")
        name = os.path.basename(summary.get("out_path", ""))
        if summary.get("stopped"):
            self.t_prog_var.set("STOPPED after %d/%d batches · %d failures — progress saved"
                                % (summary["batches_done"], summary["total_batches"],
                                   summary["failures"]))
            self.app.set_status("stopped — partial output saved (%s)" % name)
        else:
            self.t_prog.config(value=self.t_prog.cget("maximum"))
            self.t_prog_var.set("done · %d batches · %d failures · %.1fs"
                                % (summary["total_batches"], summary["failures"],
                                   summary["elapsed"]))
            self.app.set_status("Wrote %s — import into MTool" % name)

    def _on_t_error(self, err):
        self.translate_running = False
        self.t_run_btn.config(state="normal")
        self.t_stop_btn.config(state="disabled")
        self.t_prog_var.set("error: %s" % err)
        self.app.set_status("translate error: %s" % err)

    def _on_server_exit(self, code):
        self.server_proc = None
        self.server_up = False
        self._xlog("[server process exited: %s]" % code)
        self.x_srv_start.config(state="normal")
        self.x_srv_stop.config(state="disabled")
        self._on_health(False)

    def _on_action_done(self, action, root):
        self.app.set_status("%s done" % action)
        self._xlog("=== %s complete ===" % action)
        threading.Thread(target=self._detect_worker, args=(root,), daemon=True).start()

    def _on_health(self, up):
        self.server_up = up
        if up:
            self.app.status_server_var.set("XUnity server: running on :%d" % self.port)
            self.x_srv_status.set("running")
            self.x_srv_start.config(state="disabled")
            self.x_srv_stop.config(state="normal")
        else:
            self.app.status_server_var.set("XUnity server: stopped")
            self.x_srv_status.set("stopped")
            if self.server_proc is None or self.server_proc.poll() is not None:
                self.x_srv_start.config(state="normal")
                self.x_srv_stop.config(state="disabled")

    def _xlog(self, text):
        self._x_log.append(text)

    # ---------------------------------------------------------------- shutdown
    def shutdown(self):
        self._stop_health.set()
        proc = self.server_proc
        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
            except Exception:
                pass
        if self.translate_stop is not None:
            self.translate_stop.set()
