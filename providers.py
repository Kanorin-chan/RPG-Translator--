"""Provider layer: OpenAI-compatible and Anthropic Messages chat, profiles, fallback."""
import json
import os
import re
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, "config.json")  # patched by tests
LOCAL_DEFAULT_ID = "local-default"
ANTHROPIC_VERSION = "2023-06-01"
USER_AGENT = "RPG-Translator/1"  # default urllib UA is blocked by some CDNs

RETRY_AFTER_CAP = 60.0   # never sleep longer than this on one 429
BACKOFF_BASE = 2.0       # seconds; doubles per 429 (module var so tests can shrink it)
BACKOFF_CAP = 60.0

# thinking_off strategies as data: extra top-level fields merged into the payload.
THINKING_OFF = {
    "reasoning_effort_none": {"reasoning_effort": "none"},  # LM Studio lever (STATUS.md)
    "none": {},
    "anthropic_budget_zero": {},  # off == the "thinking" field is simply omitted
}


class ProviderError(RuntimeError):
    def __init__(self, msg, status=None, retryable=False):
        super().__init__(msg)
        self.status = status
        self.retryable = retryable


class RateLimited(ProviderError):
    pass


# --------------------------------------------------------------------------- #
# profiles
# --------------------------------------------------------------------------- #
def mask_key(key):
    if not key:
        return ""
    if len(key) < 12:
        return "****"
    return key[:3] + "****" + key[-4:]


def new_provider(**kw):
    kind = kw.get("kind", "local")
    p = {
        "id": "", "label": "", "kind": kind, "style": "openai", "base_url": "",
        "api_key": "", "model": "", "temperature": None, "max_tokens": None,
        "thinking_off": "reasoning_effort_none" if kind == "local" else "none",
        "concurrency": 1, "rpm": 0, "enabled": True, "fallback": [],
    }
    p.update(kw)
    if not p["label"]:
        p["label"] = p["id"]
    return p


def normalize_url(base_url, style):
    u = (base_url or "").strip().rstrip("/")
    if style == "anthropic":
        for suffix in ("/v1/messages", "/v1"):
            if u.endswith(suffix):
                u = u[: -len(suffix)]
                break
        return u + "/v1/messages"
    if u.endswith("/chat/completions"):
        return u
    if re.match(r"^https?://[^/]+$", u):  # bare host
        return u + "/v1/chat/completions"
    return u + "/chat/completions"


def reasoning_effort_for(provider):
    extra = THINKING_OFF.get(provider.get("thinking_off"), {})
    return extra.get("reasoning_effort", "")


def providers_path():
    base = os.environ.get("APPDATA")
    d = os.path.join(base, "RPG-Translator") if base else HERE
    return os.path.join(d, "providers.json")


def _synth_local():
    cfg = {}
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            cfg = json.load(f)
    except (OSError, ValueError):
        pass
    p = new_provider(
        id=LOCAL_DEFAULT_ID, label="Local (from config.json)", kind="local",
        base_url=cfg.get("endpoint_url", "http://127.0.0.1:8123/v1/chat/completions"),
        model=cfg.get("model", ""),
        concurrency=int(cfg.get("concurrency", 1) or 1),
    )
    # config.json's reasoning_effort "none" maps to the strategy; empty/other = nothing
    p["thinking_off"] = ("reasoning_effort_none"
                         if cfg.get("reasoning_effort", "none") == "none" else "none")
    return p


def load_providers():
    try:
        with open(providers_path(), encoding="utf-8") as f:
            data = json.load(f)
        items = data.get("providers", []) if isinstance(data, dict) else data
        return [new_provider(**p) for p in items]
    except (OSError, ValueError):
        return [_synth_local()]


def save_providers(providers):
    path = providers_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"version": 1, "providers": providers}, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def make_cfg(base_cfg, provider):
    cfg = dict(base_cfg)
    cfg["endpoint_url"] = normalize_url(provider["base_url"], provider.get("style", "openai"))
    cfg["model"] = provider["model"]
    cfg["reasoning_effort"] = reasoning_effort_for(provider)
    cfg["concurrency"] = int(provider.get("concurrency") or 1)
    cfg["provider"] = provider
    return cfg


# --------------------------------------------------------------------------- #
# transport
# --------------------------------------------------------------------------- #
def backoff_delay(n, retry_after=None):
    try:
        if retry_after is not None:
            return min(max(float(retry_after), 0.0), RETRY_AFTER_CAP)
    except (TypeError, ValueError):
        pass  # HTTP-date form: fall through to exponential
    return min(BACKOFF_CAP, BACKOFF_BASE * (2 ** n))


def _redact(text, key):
    return text.replace(key, "***") if key else text


def build_request(provider, system, user, temperature, max_tokens):
    """Return (url, headers, payload) without sending anything."""
    style = provider.get("style", "openai")
    url = normalize_url(provider["base_url"], style)
    if temperature is None:
        temperature = provider.get("temperature")
    if max_tokens is None:
        max_tokens = provider.get("max_tokens")
    headers = {"Content-Type": "application/json", "User-Agent": USER_AGENT}
    key = provider.get("api_key") or ""
    if style == "anthropic":
        headers["x-api-key"] = key
        headers["anthropic-version"] = ANTHROPIC_VERSION
        payload = {
            "model": provider["model"],
            "max_tokens": int(max_tokens or 4096),  # required by the API
            "system": system,
            "messages": [{"role": "user", "content": user}],
        }
        if temperature is not None:
            payload["temperature"] = min(float(temperature), 1.0)
    else:
        if key:
            headers["Authorization"] = "Bearer " + key
        payload = {
            "model": provider["model"],
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user}],
            "stream": False,
        }
        if temperature is not None:
            payload["temperature"] = temperature
        if max_tokens is not None:
            payload["max_tokens"] = int(max_tokens)
    payload.update(THINKING_OFF.get(provider.get("thinking_off"), {}))
    return url, headers, payload


def _parse(provider, data):
    try:
        if provider.get("style", "openai") == "anthropic":
            return "".join(b.get("text", "") for b in data["content"]
                           if b.get("type") == "text")
        return data["choices"][0]["message"]["content"] or ""
    except (KeyError, IndexError, TypeError, AttributeError) as e:
        raise ProviderError("unexpected response shape: %r" % (e,))


def _once(provider, system, user, temperature, max_tokens, timeout):
    url, headers, payload = build_request(provider, system, user, temperature, max_tokens)
    req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"),
                                 headers=headers, method="POST")
    key = provider.get("api_key") or ""
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            body = e.read().decode("utf-8", "replace")[:200]
        except Exception:
            body = ""
        msg = _redact("HTTP %d: %s" % (e.code, body), key)
        if e.code == 429:
            ra = e.headers.get("Retry-After") if e.headers else None
            err = RateLimited(msg, 429, True)
            err.retry_after = ra
            raise err
        raise ProviderError(msg, e.code, e.code >= 500)
    except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
        raise ProviderError(_redact("network: %r" % (e,), key), None, True)
    except ValueError as e:
        raise ProviderError("bad JSON in response: %r" % (e,))
    return _parse(provider, data)


def chat(provider, system, user, temperature=None, max_tokens=None, timeout=600,
         rate_limit_retries=4):
    """One logical request. Retries only 429 (Retry-After, else exponential);
    other errors raise ProviderError for the caller's own retry policy."""
    n = 0
    while True:
        try:
            return _once(provider, system, user, temperature, max_tokens, timeout)
        except RateLimited as e:
            if n >= rate_limit_retries:
                raise RateLimited("rate limited after %d retries: %s" % (n, e), 429, True)
            time.sleep(backoff_delay(n, getattr(e, "retry_after", None)))
            n += 1


_REFUSAL_RES = [re.compile(p, re.I) for p in (
    r"^(i['’]?m|i am)? ?sorry,?( but)? i (can['’]?t|cannot|won['’]?t|am unable|['’]?m unable|am not able)",
    r"^i (can['’]?t|cannot|won['’]?t|am unable to|['’]?m unable to|am not able to) "
    r"(assist|help|comply|fulfill|translate|continue|provide|create|generate|engage)",
    r"^as an ai\b",
    r"^(申し訳|すみません|ごめんなさい)[^。\n]{0,30}(できません|いたしかねます|お断り|控えさせ)",
    r"^(私は|わたしは)?(その|この)?(リクエスト|要求|内容)[^。\n]{0,20}(できません|お断り)",
    r"^(很?抱歉|對不起|对不起)[，,。 ]*(我)?(無法|无法|不能|不可以)",
    r"^我(無法|无法|不能)(協助|协助|提供|滿足|满足|完成|翻譯|翻译|繼續|继续|生成)",
)]


def is_refusal(text):
    """Heuristic: short text opening with a typical refusal phrase (EN/JA/ZH)."""
    t = (text or "").strip()
    if not t or len(t) > 300:
        return False
    return any(r.search(t) for r in _REFUSAL_RES)


def chat_with_fallback(providers_by_id, provider_id, system, user, temperature=None,
                       max_tokens=None, timeout=600):
    """Try provider then its fallback chain; return (text, provider_id_used)."""
    order, seen, queue = [], set(), [provider_id]
    while queue:  # breadth-first over the fallback lists, no cycles
        pid = queue.pop(0)
        if pid in seen or pid not in providers_by_id:
            continue
        seen.add(pid)
        order.append(pid)
        queue.extend(providers_by_id[pid].get("fallback") or [])
    errors = []
    for pid in order:
        p = providers_by_id[pid]
        if pid != provider_id and not p.get("enabled", True):
            continue
        try:
            text = chat(p, system, user, temperature, max_tokens, timeout)
        except RuntimeError as e:  # includes ProviderError / RateLimited
            errors.append("%s: %s" % (pid, e))
            continue
        if not text.strip() or is_refusal(text):
            errors.append("%s: %s" % (pid, "empty" if not text.strip() else "refusal"))
            continue
        return text, pid
    raise RuntimeError("all providers failed: " + " | ".join(errors))
