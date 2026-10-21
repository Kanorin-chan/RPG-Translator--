# -*- coding: utf-8 -*-
"""Providers page: profiles (local/cloud), connection test, concurrency probe.
Keys are masked in the UI and never logged. Storage: providers.save_providers."""
import copy
import re
import threading
import time
import tkinter as tk
from tkinter import messagebox, ttk

from rpgt import probe
from rpgt import providers
from rpgt.gui.app_common import autowrap, make_table

KINDS = ("local", "cloud")
STYLES = ("openai", "anthropic")
CLOUD_NOTE = ("Cloud providers may refuse or block explicit text, and send your game "
              "text to a third party. Using one is your choice and your risk. "
              "Keys are stored in your user profile, not in the project.")


class ProvidersPage(ttk.Frame):
    def __init__(self, parent, app):
        super().__init__(parent, padding=12)
        self.app = app
        self.items = providers.load_providers()
        app.provider_items = self.items   # shared, mutated in place
        self.cur = None            # index of the profile shown in the form
        self._probe_stop = None
        self._probe_busy = False
        self._last_probe = None
        self.columnconfigure(0, weight=1)
        self.rowconfigure(2, weight=1)

        ttk.Label(self, text="Providers", style="Title.TLabel").grid(
            row=0, column=0, sticky="w", pady=(0, 8))
        top = ttk.Frame(self)
        top.grid(row=1, column=0, sticky="ew", pady=(0, 8))
        ttk.Label(top, text="Default provider").pack(side="left")
        self.default_var = tk.StringVar()
        self.default_cb = ttk.Combobox(top, textvariable=self.default_var,
                                       state="readonly", width=34)
        self.default_cb.pack(side="left", padx=(8, 0))
        self.default_cb.bind("<<ComboboxSelected>>", self._on_default)
        ttk.Label(top, text="Used by the Projects page.", style="Muted.TLabel").pack(
            side="left", padx=10)

        body = ttk.Frame(self)
        body.grid(row=2, column=0, sticky="nsew")
        body.columnconfigure(0, minsize=300)
        body.columnconfigure(1, weight=1)
        body.rowconfigure(0, weight=1)
        left = ttk.Frame(body)
        left.grid(row=0, column=0, sticky="nsew")
        left.columnconfigure(0, weight=1)
        left.rowconfigure(0, weight=1)
        fr, self.tree = make_table(left, [
            ("name", "Name", 120, "w"), ("kind", "Kind", 48, "w"),
            ("model", "Model", 100, "w"), ("conc", "N", 30, "e")], height=10)
        fr.grid(row=0, column=0, sticky="nsew")
        self.tree.column("conc", stretch=False)
        self.tree.column("kind", stretch=False)
        self.tree.bind("<<TreeviewSelect>>", self._on_select)
        lb = ttk.Frame(left)
        lb.grid(row=1, column=0, sticky="ew", pady=(6, 0))
        for text, cmd in (("Add", self._add), ("Duplicate", self._dup),
                          ("Delete", self._delete)):
            ttk.Button(lb, text=text, command=cmd).pack(side="left", padx=(0, 6))

        right = ttk.Frame(body, padding=(12, 0, 0, 0))
        right.grid(row=0, column=1, sticky="nsew")
        right.columnconfigure(0, weight=1)
        right.rowconfigure(1, weight=1)
        self._build_form(right)
        self._build_probe(right)

        self._refresh_tree()
        for k, fn in (("v_test", self._on_test), ("v_level", self._on_level),
                      ("v_probe_done", self._on_probe_done)):
            app.on(k, fn)
        want = app.settings.get("default_provider")
        sel = next((i for i, p in enumerate(self.items) if p["id"] == want), 0)
        self._select_index(sel)

    # ---------------------------------------------------------------- form
    def _build_form(self, parent):
        form = ttk.LabelFrame(parent, text="Profile")
        form.grid(row=0, column=0, sticky="ew")
        form.columnconfigure(1, weight=1)
        form.columnconfigure(3, weight=1)
        self.v = {k: tk.StringVar() for k in
                  ("label", "kind", "style", "base_url", "api_key", "model",
                   "thinking_off", "concurrency", "rpm")}
        self.v_enabled = tk.BooleanVar(value=True)
        self.v_show = tk.BooleanVar(value=False)

        def row(r, text, w, col=0, span=1):
            ttk.Label(form, text=text).grid(row=r, column=col, sticky="w",
                                            padx=(0, 8), pady=3)
            w.grid(row=r, column=col + 1, columnspan=span, sticky="ew", pady=3,
                   padx=(0, 12 if col == 0 and span == 1 else 0))

        row(0, "Name", ttk.Entry(form, textvariable=self.v["label"]), span=3)
        row(1, "Kind", ttk.Combobox(form, textvariable=self.v["kind"], values=KINDS,
                                    state="readonly", width=10))
        row(1, "API style", ttk.Combobox(form, textvariable=self.v["style"],
                                         values=STYLES, state="readonly", width=12),
            col=2)
        row(2, "Base URL", ttk.Entry(form, textvariable=self.v["base_url"]), span=3)
        self.key_entry = ttk.Entry(form, textvariable=self.v["api_key"], show="•")
        row(3, "API key", self.key_entry, span=2)
        ttk.Checkbutton(form, text="Show", variable=self.v_show,
                        command=self._toggle_key).grid(row=3, column=3, sticky="w",
                                                       padx=(8, 0))
        row(4, "Model", ttk.Entry(form, textvariable=self.v["model"]), span=3)
        row(5, "Thinking off", ttk.Combobox(
            form, textvariable=self.v["thinking_off"],
            values=list(providers.THINKING_OFF), state="readonly"))
        sp = ttk.Frame(form)
        ttk.Label(sp, text="Concurrency").pack(side="left")
        ttk.Spinbox(sp, from_=1, to=64, width=4,
                    textvariable=self.v["concurrency"]).pack(side="left", padx=(6, 12))
        ttk.Label(sp, text="RPM").pack(side="left")
        ttk.Spinbox(sp, from_=0, to=100000, width=6,
                    textvariable=self.v["rpm"]).pack(side="left", padx=(6, 0))
        sp.grid(row=5, column=2, columnspan=2, sticky="w", pady=3)
        ttk.Label(form, text="Fallback chain").grid(row=6, column=0, sticky="nw",
                                                    pady=(6, 3))
        fb = ttk.Frame(form)
        fb.grid(row=6, column=1, columnspan=3, sticky="ew", pady=(6, 3))
        fb.columnconfigure(0, weight=1)
        self.fb_list = tk.Listbox(fb, selectmode="multiple", height=2,
                                  exportselection=False, relief="flat",
                                  highlightthickness=1, borderwidth=0,
                                  highlightbackground=self.app.palette["border"],
                                  highlightcolor=self.app.palette["accent"],
                                  activestyle="none")
        self.fb_list.grid(row=0, column=0, sticky="ew")
        ttk.Label(fb, text="Tried in list order when this provider errors or refuses.",
                  style="Muted.TLabel").grid(row=1, column=0, sticky="w")
        bar = ttk.Frame(form)
        bar.grid(row=7, column=0, columnspan=4, sticky="ew", pady=(8, 0))
        ttk.Button(bar, text="Save", style="Accent.TButton",
                   command=self._save).pack(side="left")
        self.test_btn = ttk.Button(bar, text="Test connection", command=self._test)
        self.test_btn.pack(side="left", padx=6)
        ttk.Checkbutton(bar, text="Enabled", variable=self.v_enabled).pack(
            side="left", padx=(14, 0))
        self.test_var = tk.StringVar()
        ttk.Label(bar, textvariable=self.test_var, style="Muted.TLabel").pack(
            side="left", padx=6)
        note = ttk.Label(parent, text=CLOUD_NOTE, style="Muted.TLabel", justify="left")
        note.grid(row=3, column=0, sticky="ew", pady=(8, 0))
        autowrap(note)

    def _build_probe(self, parent):
        box = ttk.LabelFrame(parent, text="Concurrency test")
        box.grid(row=1, column=0, sticky="nsew", pady=(10, 0))
        box.columnconfigure(0, weight=1)
        box.rowconfigure(1, weight=1)
        bar = ttk.Frame(box)
        bar.grid(row=0, column=0, sticky="ew", pady=(0, 6))
        self.probe_btn = ttk.Button(bar, text="Run concurrency test",
                                    command=self._run_probe)
        self.probe_btn.pack(side="left")
        self.probe_stop_btn = ttk.Button(bar, text="Stop", state="disabled",
                                         style="Danger.TButton", command=self._stop_probe)
        self.probe_stop_btn.pack(side="left", padx=6)
        self.apply_btn = ttk.Button(bar, text="Apply safe value", state="disabled",
                                    command=self._apply_safe)
        self.apply_btn.pack(side="left")
        self.probe_var = tk.StringVar(
            value="Sends small test batches at N = 1, 2, 4, 8, 16 (3 rounds each).")
        ttk.Label(box, textvariable=self.probe_var, style="Muted.TLabel").grid(
            row=2, column=0, sticky="w", pady=(6, 0))
        fr, self.ptree = make_table(box, [
            ("n", "N", 50, "e"), ("ok", "OK", 60, "e"), ("err", "Errors", 60, "e"),
            ("rl", "429", 60, "e"), ("lps", "Lines/s", 80, "e"),
            ("p95", "p95 (s)", 80, "e")], height=4)
        fr.grid(row=1, column=0, sticky="nsew")

    def _toggle_key(self):
        self.key_entry.config(show="" if self.v_show.get() else "•")

    # ---------------------------------------------------------------- profile <-> form
    def _refresh_tree(self):
        self.tree.delete(*self.tree.get_children())
        for i, p in enumerate(self.items):
            self.tree.insert("", "end", iid=str(i), values=(
                p.get("label") or p["id"], p.get("kind", ""), p.get("model", ""),
                p.get("concurrency", 1)))
        labels = [p.get("label") or p["id"] for p in self.items]
        self.default_cb.config(values=labels)
        did = self.app.settings.get("default_provider")
        idx = next((i for i, p in enumerate(self.items) if p["id"] == did), 0)
        if labels:
            self.default_var.set(labels[idx])
            self.app.settings["default_provider"] = self.items[idx]["id"]
        else:
            self.default_var.set("")
        if self.cur is not None and self.cur < len(self.items):
            self.tree.selection_set(str(self.cur))
        self.app.providers_changed()

    def _select_index(self, i):
        if not self.items:
            self.cur = None
            return
        i = max(0, min(i, len(self.items) - 1))
        self.tree.selection_set(str(i))
        self.tree.see(str(i))
        self._load_form(i)

    def _load_form(self, i):
        self.cur = i
        p = self.items[i]
        for k in ("label", "kind", "style", "base_url", "api_key", "model",
                  "thinking_off", "concurrency", "rpm"):
            self.v[k].set(str(p.get(k) if p.get(k) is not None else ""))
        self.v_enabled.set(bool(p.get("enabled", True)))
        self.fb_list.delete(0, "end")
        self._fb_ids = [q["id"] for j, q in enumerate(self.items) if j != i]
        for qid in self._fb_ids:
            q = next(x for x in self.items if x["id"] == qid)
            self.fb_list.insert("end", q.get("label") or qid)
        for n, qid in enumerate(self._fb_ids):
            if qid in (p.get("fallback") or []):
                self.fb_list.selection_set(n)
        self.test_var.set("")

    def _commit_form(self):
        if self.cur is None or self.cur >= len(self.items):
            return
        p = self.items[self.cur]
        for k in ("label", "kind", "style", "base_url", "api_key", "model", "thinking_off"):
            p[k] = self.v[k].get().strip()
        for k, lo in (("concurrency", 1), ("rpm", 0)):
            try:
                p[k] = max(lo, int(float(self.v[k].get())))
            except ValueError:
                p[k] = lo
        p["enabled"] = bool(self.v_enabled.get())
        p["fallback"] = [self._fb_ids[n] for n in self.fb_list.curselection()
                         if n < len(self._fb_ids)]
        iid = str(self.cur)
        if self.tree.exists(iid):
            self.tree.item(iid, values=(p["label"] or p["id"], p["kind"], p["model"],
                                        p["concurrency"]))

    def _on_select(self, _e=None):
        sel = self.tree.selection()
        if not sel:
            return
        i = int(sel[0])
        if i == self.cur:
            return
        self._commit_form()
        self._load_form(i)

    def _on_default(self, _e=None):
        idx = self.default_cb.current()
        if 0 <= idx < len(self.items):
            self.app.settings["default_provider"] = self.items[idx]["id"]
            self.app.save_settings()
            self.app.providers_changed()

    def _unique_id(self, base):
        slug = re.sub(r"[^a-z0-9]+", "-", base.lower()).strip("-") or "provider"
        ids, cand, n = {p["id"] for p in self.items}, slug, 2
        while cand in ids:
            cand = "%s-%d" % (slug, n)
            n += 1
        return cand

    def _add(self):
        self._commit_form()
        label = "New provider"
        self.items.append(providers.new_provider(
            id=self._unique_id(label), label=label, kind="cloud", style="openai",
            base_url="https://api.openai.com/v1", thinking_off="none"))
        self._refresh_tree()
        self._select_index(len(self.items) - 1)

    def _dup(self):
        if self.cur is None:
            return
        self._commit_form()
        q = copy.deepcopy(self.items[self.cur])
        q["label"] = (q.get("label") or q["id"]) + " copy"
        q["id"] = self._unique_id(q["label"])
        self.items.append(q)
        self._refresh_tree()
        self._select_index(len(self.items) - 1)

    def _delete(self):
        if self.cur is None or not self.items:
            return
        p = self.items[self.cur]
        if not messagebox.askyesno("Delete provider", "Delete '%s'?" % (p.get("label") or p["id"]),
                                   parent=self):
            return
        self.items.pop(self.cur)
        for q in self.items:
            q["fallback"] = [x for x in (q.get("fallback") or []) if x != p["id"]]
        self.cur = None
        self._refresh_tree()
        self._select_index(0)

    def _save(self):
        self._commit_form()
        try:
            providers.save_providers(self.items)
        except OSError as e:
            messagebox.showerror("Save failed", str(e), parent=self)
            return False
        self.app.save_settings()
        self._refresh_tree()
        self.app.set_status("providers saved to %s" % providers.providers_path())
        return True

    # ---------------------------------------------------------------- test / probe
    def _current(self):
        self._commit_form()
        if self.cur is None:
            return None
        return copy.deepcopy(self.items[self.cur])

    def _test(self):
        p = self._current()
        if not p:
            return
        self.test_btn.config(state="disabled")
        self.test_var.set("testing…")
        self.app.run_thread(self._test_worker, p)

    def _test_worker(self, p):
        try:
            ok, msg, ms = probe.test_connection(p)
        except Exception as e:
            ok, msg, ms = False, providers._redact(repr(e), p.get("api_key")), 0
        self.app.post("v_test", ok, msg, ms)

    def _on_test(self, ok, msg, ms):
        self.test_btn.config(state="normal")
        self.test_var.set(("OK · %d ms" % ms) if ok else ("Failed: %s" % msg[:160]))

    def _run_probe(self):
        if self._probe_busy:
            return
        p = self._current()
        if not p:
            return
        if p.get("kind") == "cloud" and not messagebox.askokcancel(
                "Concurrency test",
                "This sends about 45 small requests to a cloud provider. "
                "It may cost money and can trigger rate limits. Continue?", parent=self):
            return
        self._probe_busy = True
        self._probe_stop = threading.Event()
        self._probe_pid = p["id"]
        self._last_probe = None
        self.ptree.delete(*self.ptree.get_children())
        self.probe_btn.config(state="disabled")
        self.probe_stop_btn.config(state="normal")
        self.apply_btn.config(state="disabled")
        self.probe_var.set("running…")
        self.app.run_thread(self._probe_worker, p, self._probe_stop)

    def _probe_worker(self, p, stop):
        try:
            res = probe.run_probe(p, stop_flag=stop,
                                  on_level=lambda lv: self.app.post("v_level", lv))
        except Exception as e:
            res = {"levels": [], "fastest": 0, "safe": 0,
                   "stopped_because": providers._redact(repr(e), p.get("api_key"))}
        self.app.post("v_probe_done", res)

    def _stop_probe(self):
        if self._probe_stop:
            self._probe_stop.set()
            self.probe_stop_btn.config(state="disabled")
            self.probe_var.set("stopping…")

    def _on_level(self, lv):
        self.ptree.insert("", "end", values=(
            lv["n"], lv["ok"], lv["errors"], lv["rate_limited"],
            "%.2f" % lv["lines_per_sec"], "%.2f" % lv["p95"]))

    def _on_probe_done(self, res):
        self._probe_busy = False
        self._last_probe = res
        self.probe_btn.config(state="normal")
        self.probe_stop_btn.config(state="disabled")
        self.probe_var.set("Fastest: N=%s    Safe: N=%s    (%s)" % (
            res.get("fastest") or "-", res.get("safe") or "-",
            res.get("stopped_because", "")))
        self.apply_btn.config(state="normal" if res.get("safe") else "disabled")

    def _apply_safe(self):
        res = self._last_probe
        if not res or not res.get("safe"):
            return
        idx = next((i for i, p in enumerate(self.items) if p["id"] == self._probe_pid), None)
        if idx is None:
            return
        self.items[idx]["concurrency"] = int(res["safe"])
        self.items[idx]["concurrency_tested"] = time.strftime("%Y-%m-%d %H:%M:%S")
        if idx == self.cur:
            self.v["concurrency"].set(str(res["safe"]))
        self._commit_form()
        if self._save():
            self.app.set_status("concurrency %d saved to '%s'" % (res["safe"], self._probe_pid))
