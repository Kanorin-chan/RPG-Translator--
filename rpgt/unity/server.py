#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Live CustomTranslate server for XUnity.AutoTranslator.

XUnity (Custom endpoint) calls:
    GET {Url}?from={from}&to={to}&text={urlencoded}
and expects the translated string back as the PLAIN-TEXT response body
(HTTP 200, Content-Type: text/plain; charset=utf-8). No JSON, no quotes.

We run every line through the shared core in DYNAMIC mode: the source may be
Japanese, English, Chinese or a mix, and already-good target text is returned
unchanged. `to` picks the target language; `from` is accepted but mostly
ignored (dynamic mode self-detects).

Robustness: a translation problem NEVER 500s — we return the original text so
the game shows source rather than an error. All issues are logged to server.log.

Standard library only. Run with PYTHONUTF8=1 on Windows:
    PYTHONUTF8=1 python3 server.py

Endpoints:
    GET /translate?from=ja&to=zh-Hant&text=...   -> translated plain text
    GET /  or  /health                            -> "ok"
"""

import collections
import datetime
import json
import os
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from rpgt import core

from rpgt import paths
os.makedirs(paths.DATA_DIR, exist_ok=True)
CACHE_PATH = os.path.join(paths.DATA_DIR, "server_cache.json")
LOG_PATH = os.path.join(paths.DATA_DIR, "server.log")

# loaded once at startup
CFG = core.load_config()
GLOSSARY = core.load_glossary()
QUIET = bool(CFG.get("quiet", False))
CACHE_SAVE_EVERY = int(CFG.get("cache_save_every", 10))
CONTEXT_ENABLED = bool(CFG.get("context_enabled", True))
CONTEXT_WINDOW = int(CFG.get("context_window", 4))

# --- in-memory cache (keyed by "to|text"), mirrored to disk ------------------
_cache = {}
_cache_lock = threading.Lock()
_dirty_since_save = 0

# --- rolling dialogue-context window -----------------------------------------
# Lines arrive in real play order. We keep the last CONTEXT_WINDOW translated
# (source, result) pairs and feed them to the model as reference-only context so
# the CURRENT line is disambiguated by what came before it (e.g. in a sex scene
# "Hard" -> rougher, not difficulty). Context is prompt-only: NO extra LLM call.
# A single global window is shared by all requests; concurrent requests from
# different scenes could interleave into one window — acceptable for v1
# (see v2 flag: per-session windows + scene-boundary reset). Guarded by a lock
# because this is a ThreadingHTTPServer.
_ctx_window = collections.deque(maxlen=CONTEXT_WINDOW)
_ctx_lock = threading.Lock()


def _log(line):
    stamp = datetime.datetime.now().strftime("%H:%M:%S")
    msg = "[%s] %s" % (stamp, line)
    if not QUIET:
        print(msg, flush=True)
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(msg + "\n")
    except OSError:
        pass


def _load_cache():
    global _cache
    if os.path.exists(CACHE_PATH):
        try:
            _cache = core.load_json(CACHE_PATH)
            if not isinstance(_cache, dict):
                _cache = {}
        except (OSError, json.JSONDecodeError):
            _cache = {}


def _save_cache(force=False):
    """Atomically flush the cache to disk (throttled unless force)."""
    global _dirty_since_save
    with _cache_lock:
        if not force and _dirty_since_save < CACHE_SAVE_EVERY:
            return
        snapshot = dict(_cache)
        _dirty_since_save = 0
    try:
        core.atomic_write_json(CACHE_PATH, snapshot)
    except OSError as e:
        _log("cache save failed: %r" % e)


# --- language mapping: XUnity `to` code -> target-language name --------------
def map_target(to):
    t = (to or "").strip().lower().replace("_", "-")
    if t in ("zh-hant", "zh-tw", "zh-hk", "zh-mo", "cht", "zh-hant-tw", "zht"):
        return "Traditional Chinese"
    if t in ("zh-hans", "zh-cn", "zh-sg", "chs", "zhs"):
        return "Simplified Chinese"
    if t == "zh" or t.startswith("zh-hant") or t.startswith("zh-tw"):
        return "Traditional Chinese"
    if t.startswith("zh-hans") or t.startswith("zh-cn"):
        return "Simplified Chinese"
    if t.startswith("en"):
        return "English"
    if t.startswith("ja"):
        return "Japanese"
    return CFG.get("target_language", "Traditional Chinese")


def _short(s, n=48):
    s = s.replace("\n", "\\n")
    return s if len(s) <= n else s[: n - 1] + "…"


def translate_one(from_code, to_code, text):
    """Return the translated/normalized text for a single line. Never raises."""
    global _dirty_since_save
    if text == "" or text.strip() == "":
        return text

    target = map_target(to_code)
    # Cache is keyed by "to|text" ONLY — context is deliberately NOT part of the
    # key, so the hit rate stays high. Tradeoff: a cache HIT returns the stored
    # translation verbatim and ignores the current rolling context; context only
    # improves the FIRST translation of each unique line, repeats stay instant.
    key = "%s|%s" % (to_code or "", text)

    # cache hit?
    with _cache_lock:
        cached = _cache.get(key)
        hit = key in _cache
    if hit:
        # Still advance the window so context tracks true play order for the
        # NEXT (uncached) line, even though this hit itself skipped the model.
        if CONTEXT_ENABLED:
            with _ctx_lock:
                _ctx_window.append((text, cached))
        return cached

    # Snapshot the current window as reference-only context for THIS translation.
    context = None
    if CONTEXT_ENABLED:
        with _ctx_lock:
            context = list(_ctx_window)

    # per-request cfg with the requested target language
    req_cfg = dict(CFG)
    req_cfg["target_language"] = target

    t0 = time.time()
    try:
        result, status = core.translate_text(
            req_cfg, GLOSSARY, text, mode="dynamic", context=context
        )
    except Exception as e:  # absolute belt-and-suspenders — never 500
        core.log_failure(text, "<server error: %r>" % e, "server_exception")
        result, status = text, "fail"
    dt_ms = int((time.time() - t0) * 1000)

    with _cache_lock:
        _cache[key] = result
        _dirty_since_save += 1
    _save_cache()

    # Push AFTER translating (drop oldest via maxlen).
    if CONTEXT_ENABLED:
        with _ctx_lock:
            _ctx_window.append((text, result))

    nctx = len(context) if context else 0
    tag = {"ok": "->", "fail": "!!", "skip": "=="}.get(status, "->")
    _log("%dms  %s %s  ctx=%d  %s %s %s"
         % (dt_ms, (from_code or "?"), (to_code or "?"), nctx,
            _short(text), tag, _short(result)))
    return result


class Handler(BaseHTTPRequestHandler):
    # silence the default noisy stderr access log; we do our own logging
    def log_message(self, fmt, *args):
        pass

    def _send_text(self, body, code=200):
        payload = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"

        if path in ("/", "/health"):
            self._send_text("ok")
            return

        if path == "/translate":
            qs = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
            text = (qs.get("text", [""])[0])
            from_code = qs.get("from", [""])[0]
            to_code = qs.get("to", [""])[0]
            # parse_qs already url-decodes; guard against total failure.
            try:
                result = translate_one(from_code, to_code, text)
            except Exception as e:
                core.log_failure(text, "<handler error: %r>" % e, "handler_exception")
                result = text  # return source, never error out
            self._send_text(result)
            return

        # unknown path — be lenient, echo nothing meaningful
        self._send_text("ok")


def main():
    _load_cache()
    host = CFG.get("xunity_host", "127.0.0.1")
    port = int(CFG.get("xunity_port", 4404))
    url = "http://%s:%d/translate" % (host, port)

    httpd = ThreadingHTTPServer((host, port), Handler)

    banner = [
        "=" * 64,
        " eroge-translator  ·  XUnity CustomTranslate server",
        "=" * 64,
        " Model      : %s" % CFG.get("model", "?"),
        " Mode       : dynamic (JA / EN / mixed -> %s)"
        % CFG.get("target_language", "Traditional Chinese"),
        " Cache      : %s (%d entries loaded)" % (
            os.path.basename(CACHE_PATH), len(_cache)),
        " Context    : %s (rolling window N=%d)" % (
            "on" if CONTEXT_ENABLED else "off", CONTEXT_WINDOW),
        " Health     : http://%s:%d/health" % (host, port),
        "",
        " Paste this into XUnity Config.ini  [Custom] Url =",
        "     %s" % url,
        " (or run:  python3 configure_game.py \"<game folder>\")",
        "=" * 64,
        " Listening. Ctrl+C to stop.",
    ]
    print("\n".join(banner), flush=True)
    _log("server up on %s" % url)

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down, flushing cache...", flush=True)
    finally:
        _save_cache(force=True)
        httpd.server_close()
        _log("server down (cache: %d entries)" % len(_cache))


if __name__ == "__main__":
    main()
