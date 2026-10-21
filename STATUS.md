# eroge-translator — STATUS

> **2026-10-21 expansion (read this first).** Project is becoming **RPG-Translator**.
> Spec: `docs/2026-10-21-rpg-translator-expansion.md`. Added since the notes below:
> `providers.py` + `probe.py` (API styles, fallback, concurrency test) ·
> `project.py` (scene-ordered lines) · `readers/` (`rpgmmv`, `rpgmruby`, `wolf`,
> `wolfarchive`) · `runner.py` (scene-based translate, retranslate selected) ·
> `patcher.py` (stage/verify/backup/rollback) · `review_page.py`, `pages_*.py`, new `ui.py`.
> `should_translate` gained `origin="extracted"` + `kind`. Line numbers below are stale.
> **Real-game verified:** RPG Maker MV extract+patch+rollback; Wolf 3.14 extract from
> archives; a 16-line live LLM run. **Synthetic only:** RPG Maker XP/VX/VXAce.
> **Never run in-game:** any patched game. 159 unit tests, stdlib `unittest`.

> **Verified against code: 2026-07-12.** If the code and this doc disagree,
> **the code is right** — fix this file, then continue.

**State:** v1 built + **first live field test 2026-07-25** (kokageASMR, Unity 6,
Mono/x64). **XUnity now translates JP→zh-Hant on-screen in a live game** — a genuine
first. The test found + FIXED one real bug the self-validation missed: the wizard wrote
the game config to a path the BepInEx plugin silently ignores, so no install ever
reached our server. It also proved the TextMeshPro font problem is **still open on
modern TMP**: most glyphs render via the game's OWN Japanese font, but Traditional-only
ones (你, 醬) tofu — see open item #4. Prior "self-validated" state meant only that it
agreed with itself.

Legend: `[x] WIRED` (exists AND something calls it) · `[~] BUILT` (exists,
nothing calls it — the dangerous state) · `[ ] NOT BUILT`

---

## The brain (external — not our code)

Local LLM, OpenAI-compatible: `http://127.0.0.1:8123/v1/chat/completions`
Model `gemma4-12b-qat-uncensored-hauhaucs-balanced`, **thinking disabled** —
non-negotiable; reasoning-on burns 400–2000 tok/line and truncates.

> **2026-07-22:** LM Studio was found running with thinking **ON** — every call
> spent its whole `max_tokens` on `reasoning_content` and returned **empty**
> `content` (40-line batch: 2397 reasoning tok / 0 content / parse fail / 60.9s).
> Thinking-off is now **also enforced client-side** on every request:
> `core.py:call_llm` sends `reasoning_effort:"none"` (config `reasoning_effort`,
> default `"none"`). It is the ONLY toggle LM Studio's current build honors —
> `enable_thinking` / `chat_template_kwargs` / `thinking:{...}` / `/no_think` are
> all ignored. With it off: 328 content tok / 40-40 parsed / 8.4s (~7x). Do not
> rely on the server flag alone.

**VERIFIED:** preserves `♡ ♥` and RPG-Maker codes (`\N[1] \C[6] \V[3] \. \|`)
untouched; 0 refusals on explicit content; JA/EN/ZH.
**⚠ It is frequently DOWN.** Every caller must survive that.

---

## `core.py` — the shared translation core   `[x] WIRED`

Everything else imports this. 573 lines.

### The skip filter — the most important function in the project

`core.py:194 :: should_translate(s, mode="dynamic", origin="offline", require_cjk=True) -> bool`

- `origin` splits the two worlds and **is load-bearing** — see the policy note below.
- Engine-data rejection (script / shader / notetag) applies to **both** origins and
  **both** modes. Notetags embed Japanese (`<SC自動解除のタイミング:117>`), so they
  are matched **structurally, before any CJK test**.
- **VERIFIED:** against the real `nekonokonomachi` export — all 3,875 non-CJK keys
  were engine data, zero were dialogue.

☠️ `core.py:260 :: is_translatable(s, mode="strict_ja") -> bool` — **DEAD. ZERO
CALLERS. DO NOT USE.** It wraps `should_translate` but defaults to `strict_ja`
(project default is `dynamic`) and **drops `origin` entirely**. The name reads
better than the real one, which is precisely the trap. Delete on sight.

### The rest of the core surface

| Handle | What it does |
|---|---|
| `core.py:541 translate_text(cfg, glossary, source, mode=None, temperature=None, context=None)` | **One-shot single string.** The server's entry point. Returns `(result, status)`. |
| `core.py:520 validate_or_recover(cfg, glossary, source, translation, mode, context)` | Structure check (heart + code counts must match) **AND language check** → solo retry → **keep original**. Logs `structure_mismatch_after_retry` / `language_drift_after_retry`. |
| `core.py:267 structure_sig(s)` | The signature both sides of the validator compare. |
| `core.py:330 language_ok(source, translation, cfg)` | **Language-drift guard.** True iff the output is in the target script and no other. The model code-switches mid-line into Thai/Korean and leaves kana in; the structure check never looked. Allows Han + ASCII/fullwidth + symbols + codes + **any char also in the source** (escape hatch: `β版発動時`→`β版發動時`). **Kana is always rejected** — it is residue, not translation. Han-target only (a Japanese target is all kana). Pure function; unit-tested. |
| `core.py:438 call_llm(cfg, user_msg, temperature, max_tokens, mode)` | The LLM call, with retry. |
| `core.py:402 build_batch_user_msg(sources, glossary, mode, context)` | `⟦N⟧`-marker batch prompt. |
| `core.py:477 parse_markers(text, n)` | Parses the `⟦N⟧` batch response back into `n` items. |
| `core.py:328 context_block(context, header)` | Prior-dialogue block. **Reference-only, never a `⟦N⟧` segment** — so batch count-validation is untouched. |
| `core.py:162/166 load_config() / load_glossary()` | `config.json`, `glossary.json`. |
| `core.py:569 atomic_write_json(path, obj, indent=0)` | Crash-safe write. Use this, don't roll your own. |

---

## The two front doors

### `translate.py` — offline MTool-export translator   `[x] WIRED`

`translate.py:234 :: run_translation(in_path, out_path, cfg, glossary, mode, on_progress, on_line, stop_flag) -> dict`
(returns a summary incl. `batches_done`, `stopped` — this is the UI's entry point)

CLI: `PYTHONUTF8=1 python3 translate.py <input.json> [output.json] [--limit N] [--mode dynamic|strict_ja] [--concurrency N] [--no-context]`

- Eats an MTool **"Export Game Text"** JSON `{ja: value}`; fills the values.
- Writes `<input>.translated.json`. **Never overwrites the input.**
- **Resume = "value != key" means done.** ⚠ This is why `repair_output.py` exists:
  an entry wrongly translated *before* a filter fix will never be revisited.
- **VERIFIED:** real-file `--limit 20` run, 0 crash / 0 structure-fail, context on
  and off.

### `server.py` — live XUnity CustomTranslate server   `[x] WIRED`

`GET /translate?from=&to=&text=` → **plain-text** translation. `/health` → `ok`.
Port from `config.json` (`xunity_port`, default **4404**).

- `server.py:128 translate_one(from_code, to_code, text)` → calls `core.translate_text` with `origin="live"`.
- **Never 500s** — on any failure it returns the original text.
- Cache: in-memory + atomic on-disk (`server_cache.json`), keyed **`to|text`**.
  Survives restart. **Context is deliberately excluded from the key** → hit-rate
  preserved; a cache hit returns verbatim and ignores context.
- Rolling context window (thread-safe, last N translated pairs) injected as
  reference-only "PREVIOUS DIALOGUE (do not translate)".
- **VERIFIED:** rolling window confirmed (`ctx=0→1→2`); repeat line = ~1.5 ms cache hit.

---

## The XUnity install wizard

> **CHANGED 2026-07-25 — config-path fix (first field-test finding).** The game
> config is now written to `BepInEx\config\AutoTranslatorConfig.ini`
> (`variants.py:27 DEFAULT_CONFIG_REL`) — the path the BepInEx plugin actually
> loads. It previously went to `BepInEx\AutoTranslator\Config.ini` (the
> ReiPatcher/standalone convention), which the plugin **silently ignores**; XUnity
> then generated its own English GoogleTranslate default at the config\ path, so
> every wizard install's Endpoint/Url/Language did nothing. `configure_game.py`'s
> `find_config` now prefers the config\ path too. Regression guard:
> `test_config_path.py`. The template *files* were never wrong — only the output
> path in `variants`, so the "byte-identical" Mono/x64 claim below still holds.

> **CHANGED 2026-07-25 — install now sets fonts (PARTIAL — TMP still open).**
> `install_game._write_config` sets `OverrideFont` (legacy UGUI, works) and
> `OverrideFontTextMeshPro` from `variants.fonts_for(lang)` (zh-Hant→Microsoft
> JhengHei, zh-Hans→YaHei, en→none). ⚠ The TMP override is **best-effort only**: on
> Unity 6 XUnity can't build a TMP font from an OS-font NAME, so it does NOT fix TMP
> tofu there (log: `Could not find the TextMeshPro font asset`). See open #4 — a font
> asset bundle is the real fix. `build_template._staged_config_bytes` uses the same
> map. Guarded by `test_config_path.py`.

| Handle | State | Notes |
|---|---|---|
| `install_game.py:432 detect_game(path)` · `:459 is_applied(root)` · `:471 install(root, lang, on_log)` · `:479 reconfigure(root, lang)` · `:512 uninstall(root, on_log)` | `[x] WIRED` | The UI-facing API. CLI: `install_game.py [game] [--lang zh-Hant] [--uninstall] [--detect]`. Detects engine (Mono/IL2CPP) + arch (x64/x86 from the `UnityPlayer.dll` PE header), routes via `variants.py`, writes a manifest, probes `/health`. |
| `variants.py:80 variant_for(engine, arch)` · `:94 check_complete(variant)` | `[x] WIRED` | **Single source of truth** for the 3 template variants + routing. Imported by both build and install so they cannot disagree. |
| `configure_game.py:198 do_configure(cfg_path, lang)` | `[x] WIRED` | Section-aware INI patcher. Preserves BOM / CRLF / ordering / comments. Backs up to `.bak`. |
| `build_template.py:329 build(args)` | `[x] WIRED` | Assembles all 3 offline drop-in kits. Re-runnable; caches base/overlay zips under `downloads\`. |
| `repair_output.py:34 main()` | `[x] WIRED` | `repair_output.py <out.json> [--dry-run]`. Resets entries the *current* filter says should never have been sent. **Mirrors the offline policy exactly — if it diverges it resets good translations.** |
| `ui.py:82 App` | `[x] WIRED` | Tkinter control UI, 2 tabs. Thin layer, **no translation logic of its own.** **Redesigned 2026-07-22** (dark + light-blue, card layout, tall drop zones w/ drag highlight) — presentation only, all logic unchanged. |
| `theme.py:42 apply_theme` · `:134 enable_dark_titlebar` | `[x] WIRED` | Dark ttk theme (base `clam`) + palette/fonts, imported by `ui.py`. Presentation only. Dark title bar is best-effort: succeeds (S_OK) but the OS keeps the accent colour when `DWM\ColorPrevalence=1`. |
| `test_ui_smoke.py` | `[x] WIRED` | Headless build-and-destroy smoke test for `theme`+`ui` (skips w/o a display). `PYTHONUTF8=1 python test_ui_smoke.py`. |
| `test_config_path.py` | `[x] WIRED` | Regression guard for the 2026-07-25 config-path fix — asserts every variant's `config_rel` is `BepInEx\config\AutoTranslatorConfig.ini` and `find_config` prefers it. `PYTHONUTF8=1 python test_config_path.py` (4 tests, no LLM). |

### Template variants

| Variant | Template dir | State |
|---|---|---|
| Mono / x64 | `xunity-template-mono-x64\` | **PROVEN** (byte-identical to the v1 kit) |
| Mono / x86 | `xunity-template-mono-x86\` | Built from the official BepInEx-5 x86 base |
| IL2CPP / x64 | `xunity-template-il2cpp-x64\` | ⚠ **EXPERIMENTAL / UNVALIDATED** — no IL2CPP game was available to launch-test |

IL2CPP/x86 and arm64 are **refused** (exit 2), not half-installed.
**VERIFIED (structural only):** install → 6 required files + correct config →
uninstall → byte-for-byte clean. Against `騙され呑みニケーション_Ver1_0_1`. **Never
launched in-game.**

---

## Decisions locked — do not re-litigate

- **The two paths have DIFFERENT skip policies, because they see different text.**
  **Offline** reads an MTool export's *keys* = the raw string table (dialogue AND
  JS, GLSL, notetags, asset filenames, damage formulas). Prior translations live in
  the *values*, never the keys — **so an English key is engine data, not a prior
  translation.** Policy: reject engine data, and **require CJK**
  (`offline_require_cjk`, default true). **Live** only ever receives drawn text.
  Collapsing these corrupts one side.
- **Identifiers are never sent** (`EV107`, `SW01`). The model translates them
  non-deterministically — in one real run, 980 of 1,062 `EV###` came back untouched
  and 82 became `事件 018`.
- **`concurrency: 4`** — the LLM server's parallel slot count. Measured sweet spot;
  8 gives no gain. **`>1` auto-disables cross-batch context** (racy when batches
  overlap). `--concurrency 1` restores the sequential + context path.
- **Default direction JP → Traditional Chinese.** Glossary is Traditional
  (お兄ちゃん→歐尼醬, deliberately NOT flattened to 哥哥).
- **Dynamic mode is the default** — normalizes JA/EN/mixed → target, leaves good
  Chinese alone, fixes bad partial MTL. Target games are often already half-MTL'd.
- **XUnity = serve our own endpoint**, not hijack MTool's. MTool's Sugoi entry here
  is its *cloud* tier, not a local `:14366` server, so the hijack shortcut does not
  apply.

---

## Open / next

1. **⏳ THE §6 EXPERIMENT — gates everything else. 5 minutes.**
   *Is translating engine data corruption (our claim) or harmless waste (the MTool
   author's claim)?* Import one deliberately-wrong code-string translation → launch
   → poke the system that uses it → break or shrug. **Owed by Kano** (needs a game
   open). The answer sets the whole filter's aggressiveness.
   Full spec: `docs/2026-07-12-skip-filter-redesign.md` §6.
2. **Skip-filter redesign — SPEC WRITTEN, NOT IMPLEMENTED.**
   `docs/2026-07-12-skip-filter-redesign.md`: J1 tiered classifier / J2 循環替換
   guard / J3 invented-`\N[1]` strip. Kano's "LLM reads a batch first to filter"
   idea was **pushed back and rejected** for filtering (kept only for the UNCERTAIN
   residue). Implement **after** the §6 answer; order is in the spec's §9.
3. **🟡 FIELD TEST — first live launch done 2026-07-25 (kokageASMR, Mono/x64); it
   found + fixed the config-path bug above. Still owed: confirm text renders
   translated on-screen (watch for the TextMeshPro tofu gap, #4).**
4. **🔴 TextMeshPro CJK font — NOT solved on modern TMP.** `variants.fonts_for(lang)`
   sets `OverrideFont` (legacy UGUI, OS font — works) and `OverrideFontTextMeshPro`
   (OS-font NAME, best-effort). XUnity 5.4.5 can build a dynamic TMP font from an OS
   font on OLDER Unity, but on **Unity 6 (6000.3.11f1) it CANNOT** — log:
   `Could not find the TextMeshPro font asset: Microsoft JhengHei`. FIELD FINDING
   2026-07-25 (kokageASMR): the Traditional Chinese seen rendering was the GAME'S OWN
   Japanese TMP font (broad CJK coverage), NOT our override — which is why the few
   Traditional-only glyphs the JP font lacks (你 U+4F60, 醬 U+91AC) tofu **permanently**.
   Real fix on modern TMP: a prebuilt TMP SDF **FontAsset AssetBundle** with CJK
   coverage, referenced by `FallbackFontTextMeshPro` (fills gaps, keeps the game font)
   — **needs Unity 6 to build; NOT DONE.** Overflow is separate: `AutofitText` /
   `EnableUIResizing` are on.
5. **Context v2:** scene-boundary detection to RESET the window; per-session windows
   so concurrent scenes don't interleave the single global one.
6. **Scene *grouping*** (reorder the flat MTool dict into scene order) — the 90k
   vision. Rolling-window context is built; true grouping is not.
