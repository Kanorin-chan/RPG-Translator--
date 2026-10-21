"""Concurrency probe: measure a provider at rising parallelism, report fastest and safe."""
import threading
import time

from rpgt import core
from rpgt import providers

SAMPLE_SOURCES = [
    "おはよう、今日もいい天気だね。",
    "\\N[1]、ちょっとこっちに来て。",
    "……やだ、そんなに見ないで♡",
    "このアイテムは売れません。",
    "ありがとう。助かったよ。",
    "それじゃあ、また明日ね。",
]


def _pctl(vals, q):
    if not vals:
        return 0.0
    s = sorted(vals)
    return s[min(len(s) - 1, int(round(q * (len(s) - 1))))]


def _run_level(provider, n, rounds, system, user, stop_flag, timeout):
    lat, errs, rl, ok = [], 0, 0, 0
    lock = threading.Lock()

    def worker():
        nonlocal errs, rl, ok
        t0 = time.perf_counter()
        try:
            text = providers.chat(provider, system, user, None, None, timeout,
                                  rate_limit_retries=0)  # probe must SEE 429s
            dt = time.perf_counter() - t0
            with lock:
                if text.strip():
                    ok += 1
                    lat.append(dt)
                else:
                    errs += 1
        except providers.RateLimited:
            with lock:
                rl += 1
        except RuntimeError:
            with lock:
                errs += 1

    start = time.perf_counter()
    for _ in range(rounds):
        if stop_flag is not None and stop_flag.is_set():
            break
        ts = [threading.Thread(target=worker) for _ in range(n)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
    wall = max(time.perf_counter() - start, 1e-9)
    return {"n": n, "ok": ok, "errors": errs, "rate_limited": rl,
            "lines_per_sec": ok * len(SAMPLE_SOURCES) / wall,
            "p50": _pctl(lat, 0.5), "p95": _pctl(lat, 0.95)}


def run_probe(provider, sample_sources=None, levels=(1, 2, 4, 8, 16), stop_flag=None,
              on_level=None, min_gain=1.15, rounds=3, max_p95_ratio=2.0, timeout=120):
    """safe = highest level with zero errors/429 and p95 <= max_p95_ratio * level-1 p95;
    fastest = level with the best lines/sec among levels that had any success."""
    sources = list(sample_sources or SAMPLE_SOURCES)
    system = core.system_prompt({})
    user = core.build_batch_user_msg(sources, {})
    res, why = [], "ran all levels"
    for n in levels:
        if stop_flag is not None and stop_flag.is_set():
            why = "stopped by user"
            break
        lv = _run_level(provider, n, rounds, system, user, stop_flag, timeout)
        res.append(lv)
        if on_level:
            on_level(lv)
        if lv["errors"] or lv["rate_limited"]:
            why = "errors/429 at N=%d" % n
            break
        if len(res) > 1:
            prev = res[-2]["lines_per_sec"]
            if prev > 0 and lv["lines_per_sec"] / prev < min_gain:
                why = "gain below %.2fx at N=%d" % (min_gain, n)
                break
    base_p95 = res[0]["p95"] if res and res[0]["ok"] else None
    safe = 0
    for lv in res:
        if (base_p95 is not None and lv["ok"] and not lv["errors"] and not lv["rate_limited"]
                and lv["p95"] <= max_p95_ratio * base_p95):
            safe = lv["n"]
    good = [lv for lv in res if lv["ok"]]
    fastest = max(good, key=lambda lv: lv["lines_per_sec"])["n"] if good else 0
    if not good:
        why = "level 1 failed: " + why
    return {"levels": res, "fastest": fastest, "safe": safe, "stopped_because": why}


def test_connection(provider, timeout=30):
    t0 = time.perf_counter()
    try:
        text = providers.chat(provider, "Reply with the single word OK.", "ping",
                              0, 16, timeout, rate_limit_retries=0)
    except RuntimeError as e:
        return False, providers._redact(str(e), provider.get("api_key")), \
            int((time.perf_counter() - t0) * 1000)
    ms = int((time.perf_counter() - t0) * 1000)
    if not text.strip():
        return False, "empty reply", ms
    return True, "ok", ms
