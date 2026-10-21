"""Tests for providers.py / probe.py / core.call_llm dispatch against a local fake server."""
import json
import os
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from rpgt import core
from rpgt import probe
from rpgt import providers

providers.BACKOFF_BASE = 0.01  # keep 429 tests fast


class Fake(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self):
        super().__init__(("127.0.0.1", 0), Handler)
        self.latency = 0.0
        self.script = []          # statuses popped per request: 429/500/"refuse"
        self.retry_after = "0"
        self.slots = None         # threading.Semaphore -> queue beyond capacity
        self.cap429 = None        # in-flight cap; above it answer 429
        self.inflight = 0
        self.lock = threading.Lock()
        self.seen = []            # (path, headers dict, body dict)

    @property
    def base(self):
        return "http://127.0.0.1:%d" % self.server_address[1]


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, obj, headers=None):
        b = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(b)

    def do_POST(self):
        s = self.server
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        with s.lock:
            s.seen.append((self.path, {k.lower(): v for k, v in self.headers.items()}, body))
            action = s.script.pop(0) if s.script else None
            s.inflight += 1
            over = s.cap429 is not None and s.inflight > s.cap429
        try:
            if over:
                return self._send(429, {"error": "busy"}, {"Retry-After": "0"})
            if action == 429:
                return self._send(429, {"error": "slow down"}, {"Retry-After": s.retry_after})
            if action == 500:
                return self._send(500, {"error": "boom"})
            if s.slots:
                s.slots.acquire()
            try:
                time.sleep(s.latency)
            finally:
                if s.slots:
                    s.slots.release()
            text = "I'm sorry, but I can't help with that." if action == "refuse" else "OK-" + self.path
            if self.path.endswith("/messages"):
                return self._send(200, {"content": [{"type": "text", "text": text}],
                                        "stop_reason": "end_turn"})
            self._send(200, {"choices": [{"message": {"content": text}}]})
        finally:
            with s.lock:
                s.inflight -= 1


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = Fake()
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def setUp(self):
        s = self.srv
        s.latency, s.script, s.slots, s.cap429, s.seen = 0.0, [], None, None, []

    def prov(self, **kw):
        d = dict(id="p", kind="cloud", style="openai", base_url=self.srv.base + "/v1",
                 model="m", api_key="sk-secret-key-123456")
        d.update(kw)
        return providers.new_provider(**d)


class TestStyles(Base):
    def test_openai_roundtrip(self):
        p = self.prov()
        out = providers.chat(p, "sys", "hi", 0.3, 50)
        self.assertEqual(out, "OK-/v1/chat/completions")
        path, h, body = self.srv.seen[0]
        self.assertEqual(h["authorization"], "Bearer sk-secret-key-123456")
        self.assertEqual(body["messages"][0], {"role": "system", "content": "sys"})
        self.assertEqual(body["max_tokens"], 50)

    def test_anthropic_roundtrip(self):
        p = self.prov(style="anthropic", base_url=self.srv.base)
        out = providers.chat(p, "sys", "hi", 0.3, 50)
        self.assertEqual(out, "OK-/v1/messages")
        path, h, body = self.srv.seen[0]
        self.assertEqual(h["x-api-key"], "sk-secret-key-123456")
        self.assertEqual(h["anthropic-version"], "2023-06-01")
        self.assertEqual(body["system"], "sys")
        self.assertEqual(body["messages"], [{"role": "user", "content": "hi"}])
        self.assertNotIn("thinking", body)
        self.assertNotIn("authorization", h)

    def test_url_normalisation(self):
        n = providers.normalize_url
        full = "http://h:1/v1/chat/completions"
        self.assertEqual(n("http://h:1/v1", "openai"), full)
        self.assertEqual(n("http://h:1/v1/", "openai"), full)
        self.assertEqual(n(full, "openai"), full)
        self.assertEqual(n("http://h:1", "openai"), full)
        self.assertEqual(n("https://api.anthropic.com", "anthropic"),
                         "https://api.anthropic.com/v1/messages")
        self.assertEqual(n("https://api.anthropic.com/v1", "anthropic"),
                         "https://api.anthropic.com/v1/messages")

    def test_thinking_strategies(self):
        def body(strategy, **kw):
            return providers.build_request(self.prov(thinking_off=strategy, **kw),
                                           "s", "u", 0.1, 10)[2]
        self.assertEqual(body("reasoning_effort_none")["reasoning_effort"], "none")
        self.assertNotIn("reasoning_effort", body("none"))
        self.assertNotIn("reasoning_effort", body("bogus"))
        self.assertNotIn("thinking", body("anthropic_budget_zero", style="anthropic"))
        self.assertEqual(providers.new_provider(kind="local")["thinking_off"],
                         "reasoning_effort_none")
        self.assertEqual(providers.new_provider(kind="cloud")["thinking_off"], "none")


class TestCoreDispatch(Base):
    def test_legacy_payload_exact(self):
        cfg = {"endpoint_url": self.srv.base + "/v1/chat/completions", "model": "m",
               "reasoning_effort": "none"}
        out = core.call_llm(cfg, "USER", 0.2, 77)
        self.assertTrue(out.startswith("OK-"))
        body = self.srv.seen[0][2]
        self.assertEqual(list(body), ["model", "messages", "temperature", "max_tokens",
                                      "stream", "reasoning_effort"])
        self.assertEqual(body["messages"][0]["content"], core.system_prompt(cfg))
        self.assertEqual(body["messages"][1], {"role": "user", "content": "USER"})
        self.assertIs(body["stream"], False)

    def test_provider_cfg_dispatch(self):
        p = self.prov(kind="local", thinking_off="none", api_key="")
        cfg = providers.make_cfg({"endpoint_url": "http://bad", "model": "x",
                                  "concurrency": 9}, p)
        self.assertEqual(cfg["concurrency"], 1)
        self.assertEqual(cfg["reasoning_effort"], "")  # cloud() default 'none' strategy
        p2 = self.prov(kind="local", thinking_off="reasoning_effort_none", api_key="")
        cfg2 = providers.make_cfg({}, p2)
        self.assertEqual(cfg2["reasoning_effort"], "none")
        out = core.call_llm(cfg2, "USER", 0.2, 33)
        self.assertTrue(out.startswith("OK-"))
        body = self.srv.seen[0][2]
        self.assertEqual(body["reasoning_effort"], "none")
        self.assertEqual(body["max_tokens"], 33)

    def test_legacy_429_then_success(self):
        self.srv.script = [429, 429]
        cfg = {"endpoint_url": self.srv.base + "/v1/chat/completions", "model": "m"}
        self.assertTrue(core.call_llm(cfg, "u", 0, 5).startswith("OK-"))
        self.assertEqual(len(self.srv.seen), 3)

    def test_legacy_429_exhausted_raises_ratelimited(self):
        self.srv.script = [429] * 10
        cfg = {"endpoint_url": self.srv.base + "/v1/chat/completions", "model": "m",
               "rate_limit_retries": 2}
        with self.assertRaises(providers.RateLimited):
            core.call_llm(cfg, "u", 0, 5)
        self.assertIsInstance(providers.RateLimited("x"), RuntimeError)


class TestRetryAndFallback(Base):
    def test_429_backoff_then_success(self):
        self.srv.script = [429, 429]
        self.assertTrue(providers.chat(self.prov(), "s", "u", 0, 5).startswith("OK-"))
        self.assertEqual(len(self.srv.seen), 3)

    def test_429_exhausted(self):
        self.srv.script = [429] * 10
        with self.assertRaises(providers.RateLimited):
            providers.chat(self.prov(), "s", "u", 0, 5, rate_limit_retries=1)

    def test_retry_after_capped(self):
        self.assertEqual(providers.backoff_delay(0, "9999"), providers.RETRY_AFTER_CAP)
        self.assertEqual(providers.backoff_delay(0, "0.5"), 0.5)
        self.assertEqual(providers.backoff_delay(3, None), providers.BACKOFF_BASE * 8)

    def test_fallback_on_500_and_refusal(self):
        a = self.prov(id="a", fallback=["b"])
        b = self.prov(id="b", fallback=["c"])
        c = self.prov(id="c")
        byid = {"a": a, "b": b, "c": c}
        self.srv.script = [500, "refuse"]  # a fails, b refuses, c answers
        text, used = providers.chat_with_fallback(byid, "a", "s", "u", 0, 5)
        self.assertEqual((used, text.startswith("OK-")), ("c", True))

    def test_fallback_all_fail(self):
        a = self.prov(id="a", fallback=["a"])  # cycle must not loop
        self.srv.script = [500]
        with self.assertRaises(RuntimeError):
            providers.chat_with_fallback({"a": a}, "a", "s", "u", 0, 5)

    def test_is_refusal(self):
        for t in ["I'm sorry, but I can't help with that.", "I cannot assist with this.",
                  "申し訳ございませんが、そのリクエストにはお応えできません。",
                  "抱歉，我無法協助翻譯這個內容。"]:
            self.assertTrue(providers.is_refusal(t), t)
        for t in ["抱歉", "對不起，我來晚了。", "I can see the sea.", "", "謝謝你。"]:
            self.assertFalse(providers.is_refusal(t), t)


class TestStorage(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.old = os.environ.get("APPDATA")
        os.environ["APPDATA"] = self.tmp
        self.oldcfg = providers.CONFIG_PATH

    def tearDown(self):
        providers.CONFIG_PATH = self.oldcfg
        if self.old is None:
            os.environ.pop("APPDATA", None)
        else:
            os.environ["APPDATA"] = self.old

    def test_first_load_synth_from_config_then_roundtrip(self):
        cp = os.path.join(self.tmp, "config.json")
        with open(cp, "w", encoding="utf-8") as f:
            json.dump({"endpoint_url": "http://x/v1/chat/completions", "model": "gm",
                       "reasoning_effort": "none", "concurrency": 4}, f)
        providers.CONFIG_PATH = cp
        lst = providers.load_providers()
        self.assertEqual(len(lst), 1)
        self.assertEqual((lst[0]["model"], lst[0]["concurrency"], lst[0]["kind"]),
                         ("gm", 4, "local"))
        cfg = providers.make_cfg({}, lst[0])
        self.assertEqual((cfg["reasoning_effort"], cfg["endpoint_url"]),
                         ("none", "http://x/v1/chat/completions"))
        self.assertFalse(os.path.exists(providers.providers_path()))
        lst[0]["api_key"] = "k"
        providers.save_providers(lst)
        self.assertEqual(providers.load_providers()[0]["api_key"], "k")
        self.assertTrue(providers.providers_path().startswith(self.tmp))

    def test_mask_key(self):
        self.assertEqual(providers.mask_key(""), "")
        self.assertEqual(providers.mask_key("short"), "****")
        m = providers.mask_key("sk-ant-abcdefghijklmnop")
        self.assertNotIn("abcdefgh", m)
        self.assertTrue(m.endswith("mnop"))


class TestProbe(Base):
    def test_connection(self):
        ok, msg, ms = probe.test_connection(self.prov())
        self.assertTrue(ok)
        self.srv.script = [500]
        ok, msg, ms = probe.test_connection(self.prov())
        self.assertFalse(ok)
        self.assertNotIn("sk-secret", msg)

    def test_probe_429_over_capacity(self):
        self.srv.latency = 0.1
        self.srv.cap429 = 4
        r = probe.run_probe(self.prov(), levels=(1, 2, 4, 8, 16), rounds=2)
        self.assertEqual(r["safe"], 4)
        self.assertIn(r["fastest"], (4,))
        self.assertIn("429", r["stopped_because"])
        self.assertEqual(r["levels"][-1]["n"], 8)
        self.assertGreater(r["levels"][-1]["rate_limited"], 0)

    def test_probe_queue_over_capacity_plateaus(self):
        self.srv.latency = 0.15
        self.srv.slots = threading.Semaphore(4)
        seen = []
        r = probe.run_probe(self.prov(), levels=(1, 2, 4, 8, 16), rounds=2,
                            on_level=seen.append)
        self.assertEqual(len(seen), len(r["levels"]))
        self.assertIn(r["fastest"], (4, 8))
        self.assertIn(r["safe"], (2, 4, 8))
        self.assertLessEqual(r["levels"][-1]["n"], 16)
        self.assertIn("gain", r["stopped_because"])

    def test_probe_stop_flag(self):
        ev = threading.Event()
        ev.set()
        r = probe.run_probe(self.prov(), stop_flag=ev)
        self.assertEqual(r["levels"], [])
        self.assertEqual(r["safe"], 0)

    def test_probe_dead_provider(self):
        p = self.prov(base_url="http://127.0.0.1:1")
        r = probe.run_probe(p, levels=(1, 2), rounds=1, timeout=2)
        self.assertEqual((r["safe"], r["fastest"]), (0, 0))


if __name__ == "__main__":
    unittest.main()
