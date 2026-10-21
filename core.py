#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Shared translation core for the eroge-translator toolkit.

Everything the batch translator (translate.py) and the live XUnity server
(server.py) both need lives here, in ONE place:

  * config + glossary loading
  * skip-filter (mode-aware)
  * prompt building (mode-aware: "dynamic" vs "strict_ja")
  * LLM call with network-retry
  * marker batching + parsing
  * structure validation (hearts + RPG-Maker codes) with solo-retry recovery
  * a one-shot single-string entry point (translate_text) for the server

Standard library only. Run callers with PYTHONUTF8=1 on Windows.

Modes
-----
  "strict_ja"  — the original v1 behavior. Assumes the source is Japanese,
                 translates JA -> target. Skips any line without CJK.
  "dynamic"    — the new default. The source may be Japanese, English, Chinese
                 or a mix (games are often already partially machine-translated).
                 Normalizes the line to natural target text; returns already-good
                 target text unchanged; fixes broken/partial machine translation.
                 Skips only true junk (whitespace / numbers / symbols / paths /
                 pure control-code lines) — English words ARE translated.
"""

import json
import os
import re
import time
import urllib.request
import urllib.error

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, "config.json")
GLOSSARY_PATH = os.path.join(HERE, "glossary.json")
FAILURES_LOG = os.path.join(HERE, "failures.log")

DEFAULT_MODE = "dynamic"

# --- Markers used to delimit ENTRIES in a batch (NOT lines). Robust to the
#     newlines and quotes that appear inside translations, unlike model-emitted
#     JSON which breaks on inner quotes. The model must echo these back. ---
MARK_OPEN = "⟦"   # LEFT WHITE SQUARE BRACKET
MARK_CLOSE = "⟧"  # RIGHT WHITE SQUARE BRACKET
MARK_SPLIT_RE = re.compile(MARK_OPEN + r"\s*(\d+)\s*" + MARK_CLOSE)

# --- Detect Japanese / CJK text. ---
CJK_RE = re.compile(
    "[぀-ゟ"   # Hiragana
    "゠-ヿ"    # Katakana
    "㐀-䶿"    # CJK Ext A
    "一-鿿"    # CJK Unified Ideographs
    "豈-﫿"    # CJK Compatibility Ideographs
    "ｦ-ﾝ]"   # Halfwidth Katakana
)

# --- Kana ONLY -- no Han. CJK_RE above deliberately matches kana AND Han in one
#     class, which is what the SKIP FILTER wants ("is there Japanese-ish text
#     here?") but is useless for VALIDATING a Chinese output: there, Han is the
#     target script and kana is untranslated residue. Same ranges as CJK_RE,
#     minus the ideographs. Used by language_ok().
#     Two deliberate holes: ゠ (U+30A0) and ・ (U+30FB) sit inside the katakana
#     block but are PUNCTUATION, not kana -- Chinese uses ・ as a name separator
#     (亞歷山大・大帝), so blocking it would falsely reject good output. The
#     prolonged sound mark ー (U+30FC) is NOT a hole: it is kana residue (マーカー).
KANA_RE = re.compile(
    "[ぁ-ゟ"    # Hiragana (letters, voiced marks, iteration marks)
    "ァ-ヺ"     # Katakana letters (skips ゠ U+30A0, stops before ・ U+30FB)
    "ー-ヿ"     # Prolonged sound mark + katakana iteration marks
    "ㇰ-ㇿ"     # Katakana phonetic extensions
    "ｦ-ﾟ]"    # Halfwidth katakana (+ halfwidth voiced marks)
)

# --- Every character a CHINESE translation may legitimately contain. Anything
#     outside this set is a script we never asked for (Thai, Hangul, Cyrillic,
#     Arabic, Devanagari, Hebrew...) -- unless the SOURCE also contains it, which
#     language_ok() treats as an escape hatch. Hearts (♡ U+2661, ♥ U+2665) fall
#     inside the misc-symbols row. Note the fullwidth row stops at U+FF60: it must
#     NOT reach U+FF66-U+FF9F, which is halfwidth KATAKANA, not punctuation. ---
_ALLOWED_RE = re.compile(
    "[\t\n\r"                 # whitespace
    " -~"           # printable ASCII: A-Z a-z 0-9 + punctuation (HP, S1M)
    " "                  # no-break space
    " -⁯"           # general punctuation: — – “ ” … ‧
    "←-⇿"           # arrows: → ← ⇒
    "①-⓿"           # enclosed alphanumerics: ① ② ③
    "■-➿"           # geometric shapes + misc symbols (♡ ♥ ★ ◆) + dingbats
    "　-〿"           # CJK punctuation: 、。「」『』〜 (U+3000 = ideographic space)
    "·゠・"           # NAME SEPARATORS. · (U+00B7) is the standard Chinese one
                              # (傑克·弗羅斯特) -- VERIFIED against the real output:
                              # rejecting it falsely killed 5 good translations.
                              # ゠ and ・ sit in the katakana block but are
                              # PUNCTUATION, not kana -- KANA_RE leaves both out.
    "㐀-䶿"           # CJK Ext A
    "一-鿿"           # CJK Unified Ideographs -- THE TARGET SCRIPT
    "豈-﫿"           # CJK Compatibility Ideographs
    "︰-﹏"           # CJK compatibility forms: ︰ ﹁ ﹂
    "！-｠"           # fullwidth ASCII: ！？（）， ０-９ Ａ-Ｚ
    "￠-￦"           # fullwidth signs: ￥ ￡ ￦
    "\U00020000-\U0002fa1f"   # CJK Ext B-F + compat supplement (rare Han)
    "]"
)

# --- Structure tokens whose counts must be preserved across translation:
#     hearts and RPG-Maker style backslash codes (\N[1], \C[6], \V[3], \., \|). ---
CODE_RE = re.compile(r"\\[A-Za-z]+\[\d+\]|\\[.|^!><{}]")

# --- Junk detection for the dynamic-mode skip filter. ---
_PURE_NUMERIC_RE = re.compile(r"[\d\s.,:：%+\-()（）/]+")
_ASSET_EXT_RE = re.compile(
    r"\.(png|jpe?g|gif|bmp|webp|ogg|wav|mp3|m4a|mp4|webm|avi|json|txt|csv|xml|"
    r"ini|ttf|otf|dll|js|css|html?|rpgmv[po]|efkefc|dat|sav)(\b|$)",
    re.I,
)
_PATHISH_RE = re.compile(r"^[\w./\\\-]+$")
_ALNUM_RE = re.compile(r"[A-Za-z0-9]")

# --- Internal engine identifiers: a short ASCII letter prefix followed by
#     digits, and nothing else -- EV107, SW01, MAP001, A1, SE12.
#     These are event/switch/map names in RPG Maker data, never display text.
#     Sent to the model they get "translated" NON-DETERMINISTICALLY (observed:
#     of 1062 EV### keys in one real game, 980 came back untouched but 82 became
#     "事件 018"), which corrupts the game and looks inconsistent. Never send them.
#     The {1,6}-letter cap deliberately spares real words like "Chapter1".
_IDENT_RE = re.compile(r"[A-Za-z]{1,6}\d{1,5}")

# --- Engine data that must NEVER reach the model. -------------------------
# An MTool export is the game's RAW string table: dialogue AND JavaScript,
# GLSL shader source, plugin notetags and parameter literals. Translating any
# of it corrupts the game (shaders stop compiling, event conditions break,
# plugins stop matching their own notetag names). Observed in a real export:
# "void main() { vec2 fragCoord = ...", "$gameMap.enableNameDisplay();",
# "if(v[173]", "<SC自動解除のタイミング:117>".
#
# NOTE the notetag case carries Japanese INSIDE it, so a "translate anything
# with CJK" rule does not save us -- it must be matched structurally, before
# the CJK test.

# A notetag opens with <name: or <name> ; e.g. <SBI追加画像1:file1>
_NOTETAG_RE = re.compile(r"^\s*<[^<>\s]{1,60}[:>]")

# A notetag carrying a colon INSIDE the angle brackets: <key:value>. This is
# exactly what RPG Maker's DataManager.extractMetadata reads out of a note field
# (it strips JS, then pulls <key:value> tags, truncating at the first '>'), so
# translating the key breaks the plugin. Matched ANYWHERE in the string — MTool
# splits multi-line note blocks into fragments that start mid-tag (e.g.
# "1>\n<SBI追加画像2:file2>\n<SBI追加条件2:"), so start-anchoring misses them.
# The required colon-inside-brackets is what separates a real notetag from
# stylistic prose brackets like <管理存在> / <事象改変> (no colon -> kept).
# [^<>\n] so it can't span lines or nest; trailing '>' optional to catch the
# broken tail fragment.
_NOTETAG_INLINE_RE = re.compile(r"<[^<>\n]{1,40}[:：][^<>\n]*>?")

# Strong, unambiguous source-code markers (JS / GLSL / RPG-Maker script).
_SCRIPT_RE = re.compile(
    r"this\.|\$game|\$data|\bfunction\b|=>|\bvar\s|\blet\s|\bconst\s"
    r"|\breturn\s|\);|\bvec[234]\b|\bsampler2D\b|\bvoid\s+\w+\s*\("
    r"|gl_[A-Za-z]|precision\s+\w+p\b|#ifdef|console\.|\bprototype\b"
    r"|\btypeof\b|\bif\s*\(|\bv\[\d+\]"
)
# Trailing code punctuation: "ttackDamageSum;"  "}"  "{"
_CODE_TAIL_RE = re.compile(r"[;{}]\s*$")

# Bare literals / engine constants: true, false, null, BGS, BGM, SE
_LITERAL_RE = re.compile(r"(?:true|false|null|undefined|NaN)", re.I)
_SHORT_CAPS_RE = re.compile(r"[A-Z]{2,5}")

# A lone ASCII word with no spaces (update, row, log). In a Japanese game's
# export these are code identifiers, not UI copy -- the UI copy is Japanese.
# Offline-only: the live server never receives code, only drawn text.
_LONE_WORD_RE = re.compile(r"[A-Za-z]+")


def _is_engine_data(s):
    """True when the string is game engine data (code/notetag), not display text.

    Deliberately checked BEFORE the CJK test, because notetags embed Japanese.
    """
    t = s.strip()
    if not t:
        return False
    if _NOTETAG_RE.match(t):
        return True
    if _NOTETAG_INLINE_RE.search(t):
        return True  # <key:value> plugin notetag (even as a split fragment)
    if _SCRIPT_RE.search(t):
        return True
    if _CODE_TAIL_RE.search(t):
        return True
    # Multi-line, no Japanese, and carrying code punctuation -> a script block.
    if ("\n" in t or "\r" in t) and not CJK_RE.search(t) and re.search(r"[;{}()]", t):
        return True
    return False


# --------------------------------------------------------------------------- #
# loading
# --------------------------------------------------------------------------- #
def load_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def load_config():
    return load_json(CONFIG_PATH)


def load_glossary():
    if os.path.exists(GLOSSARY_PATH):
        g = load_json(GLOSSARY_PATH)
        # drop reserved comment/example scaffolding if present
        return {k: v for k, v in g.items() if not k.startswith("_")}
    return {}


def get_mode(cfg, override=None):
    if override:
        return override
    return (cfg or {}).get("mode", DEFAULT_MODE)


# --------------------------------------------------------------------------- #
# skip filter (mode-aware)
# --------------------------------------------------------------------------- #
def _is_path_like(s):
    s2 = s.strip()
    if not s2 or CJK_RE.search(s2):
        return False
    if _ASSET_EXT_RE.search(s2):
        return True
    if ("/" in s2 or "\\" in s2) and " " not in s2 and _PATHISH_RE.match(s2):
        return True
    return False


_PLAYER_TEXT_KINDS = ("dialogue", "choice", "name", "desc", "term")


def _extracted_player_text(s, kind):
    """origin="extracted": a reader already knows this field is player-facing, so
    only blank / payload-less lines are junk. Engine-data regexes are NOT run --
    they misfire on English dialogue ("I will return home" matches `return\\s`)."""
    payload = CODE_RE.sub("", s).replace("♡", "").replace("♥", "").strip()
    if payload == "" or _PURE_NUMERIC_RE.fullmatch(payload):
        return False
    if not _ALNUM_RE.search(payload) and not CJK_RE.search(payload):
        return False
    if kind == "name" and _IDENT_RE.fullmatch(payload):
        return False
    return True


def should_translate(s, mode=DEFAULT_MODE, origin="offline", require_cjk=True,
                     kind=None):
    """Return True if this line is worth sending to the model.

    origin distinguishes the two paths, which see very different text:
      "offline" (translate.py / repair_output.py) -- keys of an MTool export:
                the game's RAW string table, i.e. dialogue AND code, shaders,
                notetags and plugin literals. Reject engine data aggressively.
      "live"    (server.py via translate_text) -- only text the game actually
                DREW on screen. Never code, so English UI copy is fair game.

    require_cjk (offline only): in a JAPANESE game's export, the player-facing
    text is Japanese; a string with no Japanese in it is engine data. Sampling a
    real export, every single non-CJK key was an asset filename ("z_goodnight",
    "re_30_13-14_20a"), a damage formula ("a.def / 2.5 + 20"), a placeholder
    ("%size%"), a CSS colour, or a plugin parameter name ("Fade Out Time") --
    never dialogue. Translating those corrupts asset lookups and combat math.
    Set config "offline_require_cjk": false for an export whose source text is
    genuinely English (e.g. an already-English-patched game).

    strict_ja: translate only lines containing CJK (original v1 behavior).
    dynamic:   translate almost everything; skip only true junk — whitespace,
               pure numbers, pure punctuation/symbols, path/filename-like, and
               pure control-code lines (codes/hearts with no real payload).
    """
    # "extracted": structured reader (project.py). kind says what the field is;
    # player-facing kinds skip the engine-data regexes, the rest use the live rules.
    if origin == "extracted":
        if kind in _PLAYER_TEXT_KINDS:
            return _extracted_player_text(s, kind)
        origin = "live"

    # Engine data (script / shader / notetag) is never display text, in ANY mode
    # or on either path. Checked first: notetags embed Japanese, so this must
    # win over the CJK test below.
    if _is_engine_data(s):
        return False

    if mode == "strict_ja":
        return bool(CJK_RE.search(s))

    # dynamic
    if s.strip() == "":
        return False
    # judge the "real payload" after removing RPG codes + hearts
    payload = CODE_RE.sub("", s).replace("♡", "").replace("♥", "").strip()
    if payload == "":
        return False  # pure control-code / pure-heart line
    if _PURE_NUMERIC_RE.fullmatch(payload):
        return False  # pure numeric
    if _IDENT_RE.fullmatch(payload):
        return False  # internal identifier (EV107, SW01) -- never display text
    if _LITERAL_RE.fullmatch(payload):
        return False  # bare literal: true / false / null
    if _SHORT_CAPS_RE.fullmatch(payload):
        return False  # engine constant: BGS, BGM, SE, ME
    if not _ALNUM_RE.search(payload) and not CJK_RE.search(payload):
        return False  # pure punctuation / symbol (e.g. ◆◆◆)
    if _is_path_like(payload):
        return False  # path / filename
    if origin == "offline" and not CJK_RE.search(payload):
        # No Japanese in a Japanese game's string table => engine data: asset
        # names, formulas, placeholders, plugin params. See the docstring.
        if require_cjk:
            return False
        # require_cjk disabled (English-source export): still reject a lone
        # ASCII word (update, row) -- a code identifier, not UI copy. Real
        # sentences ("Save the game?") have spaces and survive.
        if _LONE_WORD_RE.fullmatch(payload):
            return False
    return True  # when unsure, translate


# Back-compat alias used by older call sites.
def is_translatable(s, mode="strict_ja"):
    return should_translate(s, mode)


# --------------------------------------------------------------------------- #
# structure signature
# --------------------------------------------------------------------------- #
def structure_sig(s):
    """A signature that must match between source and translation."""
    return (
        s.count("♡"),                   # heart
        s.count("♥"),                   # black heart
        tuple(sorted(CODE_RE.findall(s))),   # backslash codes (multiset)
    )


# --------------------------------------------------------------------------- #
# language check
# --------------------------------------------------------------------------- #
# Only a HAN-SCRIPT target can use the rule "Han is the target script, kana is
# residue". Both front doors can select another target -- ui.py:60 LANG_CHOICES
# offers English, and server.py:106 map_target() will return "Japanese" for
# to=ja -- and a Japanese target is ALL kana, so running this check on it would
# reject every single line. Gate on the target language.
_HAN_TARGETS = ("chinese",)


def language_ok(source, translation, cfg):
    """True when the translation is written in the target script, and no other.

    The model occasionally CODE-SWITCHES mid-line into a random third language.
    structure_sig() validates STRUCTURE (hearts + codes) and never looked at the
    script, so all of this shipped in one real 5,200-entry output:

        ...するプラグイン  ->  ...的技能的ปลั๊กอิน   (Thai: "plugin")
        ダンジョンの外へ    ->  要ออกจาก地下城嗎？   (Thai: "leave")
        オーガLv30         ->  오格 Lv30            (Korean: "o[gre]")
        お金を             ->  金錢を               (kana left in, x34)

    A character is allowed when it is Han (the target script), an ASCII or
    fullwidth letter/digit/punctuation/symbol, a heart, part of an RPG-Maker
    code -- OR, the escape hatch, WHEN IT ALSO APPEARS IN THE SOURCE. The escape
    hatch is what lets β版発動時 -> β版發動時 through: β is Greek, but the source
    vouches for it. Without it we would falsely reject good translations.

    KANA IS THE ONE EXCEPTION TO THE ESCAPE HATCH. Kana is always in the source
    (the source is Japanese) -- and that is exactly the point: kana surviving
    into a Chinese output is untranslated residue, not a translation.

    Simplified-vs-Traditional (單个) is a DIFFERENT concern and is not checked
    here: 个 is Han, so it passes.
    """
    tgt = (cfg or {}).get("target_language", "Traditional Chinese").lower()
    if not any(h in tgt for h in _HAN_TARGETS):
        return True  # not a Han-script target -- this check does not apply

    if KANA_RE.search(translation):
        return False  # untranslated Japanese residue (お金を -> 金錢を)

    # RPG-Maker codes are STRUCTURE, not language -- structure_sig() already
    # guards them. Drop them so a code's insides can't be read as stray letters.
    rest = CODE_RE.sub("", translation)
    src_chars = set(source)
    for ch in rest:
        if _ALLOWED_RE.match(ch):
            continue
        if ch in src_chars:
            continue  # the source vouches for it (β, an odd symbol, ...)
        return False  # a script we never asked for: Thai, Hangul, Cyrillic...
    return True


# --------------------------------------------------------------------------- #
# prompts (mode-aware)
# --------------------------------------------------------------------------- #
def glossary_block(glossary):
    if not glossary:
        return ""
    lines = ["Glossary — translate these terms consistently EXACTLY as given:"]
    for ja, zh in glossary.items():
        lines.append("  %s -> %s" % (ja, zh))
    return "\n".join(lines) + "\n\n"


# --------------------------------------------------------------------------- #
# context (surrounding-dialogue) support
# --------------------------------------------------------------------------- #
# A reference-only block of recently-translated (source -> result) pairs is
# rendered BEFORE the segment(s) so the model can resolve pronouns, speaker and
# context-dependent words (e.g. in a sex scene "Hard" = rougher, not difficulty).
# It is NEVER one of the ⟦N⟧-counted segments, so it cannot perturb the
# count-in == count-out batch validation. The system_prompt clause below plus the
# block header tell the model to use it for understanding only — never translate,
# echo, or renumber it.

# Appended to system_prompt only when context is enabled (see _with_context_clause),
# so that with context DISABLED the system prompt is byte-identical to pre-context v1.
_CONTEXT_CLAUSE = (
    "CONTEXT AWARENESS: A block labelled \"PREVIOUS DIALOGUE\" or \"PREVIOUS "
    "CONTEXT\" may appear before the material you must translate. Those are earlier "
    "lines of the SAME scene, given purely as reference so you can resolve "
    "pronouns, the speaker, register, and ambiguous or borrowed words. NEVER "
    "translate, echo, quote, renumber, or mention that block. Translate ONLY the "
    "actual numbered segment(s) / single segment requested, and output only their "
    "result(s)."
)


def _with_context_clause(cfg, base):
    """Append the context clause to a system prompt iff context is enabled."""
    if (cfg or {}).get("context_enabled", True):
        return base + "\n" + _CONTEXT_CLAUSE
    return base


def _ctx_oneline(s, n=220):
    """Flatten a context line to one row (newlines collapsed) and cap its length,
    so a long prior line cannot bloat the prompt or look like a segment boundary."""
    s = s.replace("\r", "").replace("\n", " / ").strip()
    if len(s) > n:
        s = s[: n - 1] + "…"
    return s


def context_block(context, header="PREVIOUS DIALOGUE"):
    """Render recent (source, result) pairs as a reference-only context block.

    `context` is a list of (source_text, translated_result) tuples in play order
    (oldest first). Returns "" when there is no context — so a caller that passes
    None/[] produces a prompt byte-identical to the pre-context version."""
    if not context:
        return ""
    out = [
        "%s (reference only — do NOT translate, echo or renumber this block; "
        "use it ONLY to understand the segment(s) that follow):" % header
    ]
    for src, res in context:
        out.append("    %s => %s" % (_ctx_oneline(src), _ctx_oneline(res)))
    return "\n".join(out) + "\n\n"


def system_prompt(cfg, mode=DEFAULT_MODE):
    tgt = cfg.get("target_language", "Traditional Chinese")
    if mode == "strict_ja":
        return _with_context_clause(cfg, (
            "You are a professional Japanese-to-%s translator specializing in "
            "adult visual novels (eroge). Translate naturally and idiomatically "
            "into %s.\n"
            "STRICT RULES:\n"
            "1. Preserve EVERY formatting code EXACTLY and untouched: the heart "
            "symbols ♡ and ♥, and all backslash codes such as \\N[1], "
            "\\C[6], \\V[3], \\., \\|. Never translate, add, remove, or reorder "
            "them.\n"
            "2. Keep the erotic register explicit, natural and human — NOT "
            "clinical, NOT censored, NOT softened. Moans and lewd wording stay "
            "lewd.\n"
            "3. Preserve line breaks inside a segment.\n"
            "4. Honor the provided glossary exactly.\n"
            "5. Output ONLY the translation. No notes, no romaji, no explanations."
            % (tgt, tgt)
        ))
    # dynamic (default)
    return _with_context_clause(cfg, (
        "You are an expert game-text localizer producing natural %s. Each input "
        "is one line of text pulled live from a video game. Its source may be "
        "Japanese, English, Chinese, or a MIXTURE — the game may already be "
        "partially machine-translated. Your job is to NORMALIZE the line into "
        "fluent, natural %s.\n"
        "STRICT RULES:\n"
        "1. If the text is ALREADY entirely correct, natural %s, return it "
        "EXACTLY unchanged.\n"
        "2. Otherwise translate and rewrite it into natural %s.\n"
        "3. The result MUST be written ENTIRELY in %s. Leave NO Japanese and "
        "NO English words in the output — translate embedded foreign words "
        "and loanwords too (for example the English word \"Hard\" must become "
        "the %s word for it, it must NOT stay as \"Hard\"). The ONLY "
        "characters allowed that are not %s are the formatting codes in rule 5 "
        "and proper nouns fixed by the glossary.\n"
        "4. Do NOT pass through broken, awkward, half-translated or "
        "machine-stilted text — fix it into what a native %s speaker would "
        "actually say.\n"
        "5. Preserve EVERY formatting code EXACTLY and untouched: the heart "
        "symbols ♡ and ♥, all backslash codes such as \\N[1], \\C[6], "
        "\\V[3], \\., \\|, and all line breaks. Never translate, add, remove, or "
        "reorder them.\n"
        "6. Keep the register faithful — adult/erotic content stays "
        "explicit, natural and human, NOT clinical, NOT censored.\n"
        "7. Honor the provided glossary exactly.\n"
        "8. Output ONLY the resulting %s text. No notes, no romaji, no "
        "explanations, no surrounding quotes."
        % (tgt, tgt, tgt, tgt, tgt, tgt, tgt, tgt, tgt)
    ))


def _segment_word(mode):
    return "Japanese segment" if mode == "strict_ja" else "text segment"


def build_batch_user_msg(sources, glossary, mode=DEFAULT_MODE, context=None):
    parts = [glossary_block(glossary)]
    ctx = context_block(context, header="PREVIOUS CONTEXT")
    if ctx:
        parts.append(ctx)  # reference-only; NOT a ⟦N⟧ segment (count unaffected)
    parts.append(
        "Translate each numbered %s below. For EACH segment, output the SAME "
        "marker line %s N %s on its own line, then the result on the following "
        "line(s). Output the segments in order, nothing before the first marker, "
        "nothing after the last result.\n"
        % (_segment_word(mode), MARK_OPEN, MARK_CLOSE)
    )
    for i, s in enumerate(sources, 1):
        parts.append("%s%d%s\n%s" % (MARK_OPEN, i, MARK_CLOSE, s))
    return "\n".join(parts)


def build_solo_user_msg(source, glossary, mode=DEFAULT_MODE, context=None):
    if mode == "strict_ja":
        instr = (
            "Translate this single Japanese segment. Output ONLY the "
            "translation, preserving all formatting codes and line breaks:\n\n"
        )
    else:
        instr = (
            "Normalize this single game-text segment into natural target-language "
            "text (return it unchanged if it is already correct). Output ONLY the "
            "result, preserving all formatting codes and line breaks:\n\n"
        )
    ctx = context_block(context, header="PREVIOUS DIALOGUE")
    return glossary_block(glossary) + ctx + instr + source


# --------------------------------------------------------------------------- #
# LLM call
# --------------------------------------------------------------------------- #
def call_llm(cfg, user_msg, temperature, max_tokens, mode=DEFAULT_MODE):
    payload = {
        "model": cfg["model"],
        "messages": [
            {"role": "system", "content": system_prompt(cfg, mode)},
            {"role": "user", "content": user_msg},
        ],
        "temperature": temperature,
        "max_tokens": max_tokens,
        "stream": False,
    }
    # Disable server-side "thinking"/reasoning on EVERY request. thinking-off is
    # non-negotiable here: with it on, the model spends the whole max_tokens budget
    # on reasoning_content and returns EMPTY content (verified 2026-07-22 against
    # LM Studio: a 40-line batch => 2397 reasoning tok, 0 content, parse fail, 60.9s;
    # with it off => 328 content tok, 40/40 parsed, 8.4s). We enforce it client-side
    # so a server-side toggle can't silently break or ~7x-slow the pipeline again.
    # LM Studio honors OpenAI's `reasoning_effort`; "none" is the ONLY lever that
    # works on this server (enable_thinking / chat_template_kwargs / thinking:{...} /
    # prompt-level /no_think are all ignored). Config-driven; default "none".
    effort = cfg.get("reasoning_effort", "none")
    if effort:
        payload["reasoning_effort"] = effort
    body = json.dumps(payload).encode("utf-8")

    req = urllib.request.Request(
        cfg["endpoint_url"],
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    retries = int(cfg.get("network_retries", 3))
    timeout = int(cfg.get("request_timeout_seconds", 600))
    last_err = None
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            return data["choices"][0]["message"]["content"]
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError,
                ConnectionError) as e:
            last_err = e
            if attempt < retries - 1:
                time.sleep(2 * (attempt + 1))
    raise RuntimeError("LLM call failed after %d attempts: %r" % (retries, last_err))


# --------------------------------------------------------------------------- #
# marker parsing
# --------------------------------------------------------------------------- #
def parse_markers(text, n):
    """Parse a marker-delimited response into a list of n translations.

    Returns the list iff markers 1..n are ALL present exactly once and
    parseable; otherwise None (caller falls back to solo translation)."""
    chunks = MARK_SPLIT_RE.split(text)
    if len(chunks) < 3:
        return None
    found = {}
    for i in range(1, len(chunks) - 1, 2):
        try:
            idx = int(chunks[i])
        except ValueError:
            continue
        content = chunks[i + 1].strip("\n").strip()
        if idx in found:
            return None  # duplicate marker -> untrustworthy
        found[idx] = content
    if set(found.keys()) != set(range(1, n + 1)):
        return None
    return [found[i] for i in range(1, n + 1)]


# --------------------------------------------------------------------------- #
# solo translate + validate/recover
# --------------------------------------------------------------------------- #
def translate_solo(cfg, glossary, source, temperature, mode=DEFAULT_MODE, context=None):
    msg = build_solo_user_msg(source, glossary, mode, context)
    max_tokens = int(cfg.get("max_tokens_per_entry", 60)) * 4 + 64
    out = call_llm(cfg, msg, temperature, max_tokens, mode)
    return out.strip("\n").strip()


def log_failure(source, translation, reason):
    try:
        with open(FAILURES_LOG, "a", encoding="utf-8") as f:
            f.write("=== %s ===\n" % reason)
            f.write("SRC: %r\n" % source)
            f.write("OUT: %r\n\n" % translation)
    except OSError:
        pass


def _rejection_reason(cfg, source, translation):
    """Why this translation is unacceptable, or None when it is fine.

    Two independent checks, ONE shared retry (see validate_or_recover):
      structure — hearts + RPG-Maker codes must survive the translation.
      language  — the output must be in the target script and no other; the
                  model code-switches into Thai/Korean and leaves kana behind."""
    if structure_sig(translation) != structure_sig(source):
        return "structure_mismatch"
    if not language_ok(source, translation, cfg):
        return "language_drift"
    return None


def validate_or_recover(cfg, glossary, source, translation, mode=DEFAULT_MODE, context=None):
    """Return (text, ok) where text matches the source's structure signature AND
    is written in the target language.

    On either failure: one solo retry at retry_temperature; if it still fails,
    keep the ORIGINAL source and log."""
    if _rejection_reason(cfg, source, translation) is None:
        return translation, True
    try:
        retry = translate_solo(
            cfg, glossary, source, float(cfg.get("retry_temperature", 0.2)), mode,
            context
        )
    except Exception as e:  # network/other — keep original, log
        log_failure(source, "<retry error: %r>" % e, "retry_exception")
        return source, False
    reason = _rejection_reason(cfg, source, retry)
    if reason is None:
        return retry, True
    log_failure(source, retry, reason + "_after_retry")
    return source, False


def translate_text(cfg, glossary, source, mode=None, temperature=None, context=None):
    """One-shot single-string translate/normalize for the live server.

    Runs skip-filter -> solo translate -> structure validation/recovery.
    `context` (optional) is a list of recent (source, result) pairs rendered as a
    reference-only "PREVIOUS DIALOGUE" block so the current line is disambiguated
    by what came before it — it adds NO extra LLM call.
    Never raises for a translation problem: on any failure returns the ORIGINAL
    source unchanged (so a game shows source text rather than an error).
    Returns (text, status) where status is one of:
      "skip" (junk, returned as-is), "ok" (translated & validated),
      "fail" (attempted but fell back to original)."""
    mode = get_mode(cfg, mode)
    if not should_translate(source, mode, origin="live"):
        return source, "skip"
    temp = temperature if temperature is not None else float(cfg.get("temperature", 0.3))
    try:
        out = translate_solo(cfg, glossary, source, temp, mode, context)
    except Exception as e:
        log_failure(source, "<translate_text error: %r>" % e, "translate_text_exception")
        return source, "fail"
    fixed, ok = validate_or_recover(cfg, glossary, source, out, mode, context)
    return fixed, ("ok" if ok else "fail")


# --------------------------------------------------------------------------- #
# misc
# --------------------------------------------------------------------------- #
def atomic_write_json(path, obj, indent=0):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=indent)
    os.replace(tmp, path)
