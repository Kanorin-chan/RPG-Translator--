# RPG-Translator — expansion spec

> **SPEC, not implemented.** Written 2026-10-21 from a design chat with Kano plus a
> read of three reference repos (cloned to scratch, not vendored). Where this doc and
> the code disagree, the code is right.

## 1. Goal

Keep the existing core (shared Gemma brain, skip filter, `validate_or_recover`,
`language_ok`, XUnity live server) and add:

1. **Read the game ourselves** (RPG Maker MV/MZ/VX/VXAce/XP, Wolf) — no MTool export needed.
2. **Patch with backup, fallback and rollback.**
3. **Provider layer** — OpenAI-compatible and Anthropic Messages APIs, local or cloud.
4. **Concurrency probe** — test a provider before a run, pick a safe setting.
5. **Review list** — every line in scene order; flag, multi-select, re-translate with context.
6. **UI redesign** (Tkinter + ttk, no web server, no extra ports).
7. **A real README** that leads with the safeguards.

**Priority: completeness and precision of the translation over speed.** Defaults pick
the *safe* concurrency, not the fastest. This is the README's headline.

**Out of scope:** save files, cheats, anything but text. We only translate.

## 2. Name

Folder/product name `RPG-Translator--` (a nod to Translator++). Python modules use a
shell-safe name. Rename is the **last** step (it breaks the Claude memory path,
`.bat` files, and anything holding `F:\eroge-translator`). **Not a git repo — `git init`
and a full backup come first.**

## 3. References and licensing (what was actually read)

| Repo | License | Use |
|---|---|---|
| RPGMTL (MizaGBF) | MIT | File plugins (MV/MZ JSON+JS, Ruby marshal, Ren'Py, KiriKiri, YU-RIS…). Can borrow code; **keep their notice.** |
| rewolf-trans (KCFindstr) | MIT | Wolf `.dat/.mps/.project` reading. Can borrow; keep notice. TypeScript → port to Python. |
| WolfTrans (elizagamedev) | MPL-2.0 | Ruby. Read for format knowledge; write our own. If any file is copied, that file stays MPL. |
| RPGMLocalizer (Lord0fTurk) | GPL-3.0 | **Read for ideas only.** Copying its code would make this project GPL. Credit in README. |

All reference clones live in scratch and are never imported.

## 4. Facts established from the code (not guesses)

- **RPGMTL translator plugins** (`readme/plugins.md`): subclass `TranslatorPlugin`;
  implement `translate` and `translate_batch`. Two formats via `get_format`: STANDARD
  (list of strings in, list/None out) and AI (JSON with file + groups + prior
  translations as context in, `{id: translation}` out). Settings declared via
  `get_setting_infos` (`str/text/password/bool/num`). Needs Python 3.13 + aiohttp.
- **RPGMTL file plugins** return *String Groups* `[group_name, s1, s2…]` from `read`
  and patch back via `WalkHelper.apply_string`. Archives are "virtual files".
- **RPGMTL has no Wolf plugin** (plugin list checked). Wolf comes from rewolf-trans.
- **rewolf-trans Wolf**: ~1,500 lines across `src/wolf/*.ts`. Files carry a small
  LCG-XOR obfuscation (`wolf-crypko.ts`, header-seeded, per-interval XOR) — simple.
  It reads an already-**extracted** `Data` folder; it does **not** unpack `Data.wolf`
  archives. **Archive decryption is an unsolved gap** (UberWolf/WolfDec were not
  found by search). Until solved: user decrypts the archive externally first.
- **rewolf-trans classifies strings by modification risk** into separate patch files —
  the same problem our skip-filter redesign addresses. Read before implementing J1.

## 5. Architecture

```
game folder ──► Reader (per engine) ──► Project (scene-ordered lines, JSON on disk)
                                              │
              Provider layer ◄── core.py ◄────┤  skip filter, validate_or_recover,
              (OpenAI/Anthropic,              │  language_ok, glossary, context
               probe, fallback chain)         │
                                              ▼
                        Review list (UI) ──► Patcher ──► game (backup + manifest + rollback)

XUnity live server (server.py) — unchanged, shares core.py + providers.
MTool export (translate.py)    — kept as a fallback input, unchanged.
```

**Reuse, don't rebuild:** `core.py` stays the brain. New code imports
`should_translate`, `validate_or_recover`, `translate_text`, `atomic_write_json`.
☠️ `is_translatable` stays dead.

## 6. Provider layer

- **Config per provider:** name, API style (`openai` | `anthropic`), base URL, API key,
  model, temperature, max_tokens, `thinking_off` strategy, concurrency (from probe),
  RPM limit (if known), enabled.
- **`thinking_off` is per provider.** `reasoning_effort:"none"` is an LM Studio
  finding; sending it to a cloud API may be rejected. Strategy is a provider field,
  with the LM Studio value as the local default. Do not hardcode.
- **Fallback chain:** primary → fallback list. Triggers: refusal, repeated error, 429
  after backoff. A refusal from cloud can route to the local model.
- **Keys** stored outside the project folder; never logged; masked in the UI.
- **Cloud is opt-in by the user.** The provider page shows a short note that cloud
  providers may refuse or block explicit text. No other gating.
- `core.py:call_llm` is the seam. It currently assumes the one local endpoint — this
  is the main refactor. Interface to confirm by reading `call_llm` before editing.

## 7. Concurrency probe

Button on the provider page, runs before a job.

1. Send a fixed small sample batch at N = 1, 2, 4, 8, 16… in parallel.
2. Record per level: lines/sec, p50/p95 latency, error count, 429 count.
3. Stop climbing when throughput gains < threshold or errors/429 appear.
4. Report **two** numbers: *fastest* and *safe*. **Default = safe.**
5. Save to the provider profile with a timestamp; re-test on demand.
6. Exponential backoff on 429 during real runs, honoring `Retry-After`.

Thresholds are tunables to set from a real measurement — not invented constants.
(Today's `concurrency: 4` was hand-measured for local; the probe replaces that.)

## 8. Context vs concurrency (the conflict, resolved)

Existing rule: `concurrency > 1` disables cross-batch context. Resolution:

- The **unit of work is a scene**, not a line. Each worker gets a whole scene, so
  context lives *inside* the batch. Requires the reader to supply scene order
  (MTool's flat dict cannot — it keeps the old behavior).
- First pass is fast and parallel. Precision comes from the **review list** (§10).

## 9. Reader and patcher

- **Project file** per game: lines in game order with scene id, speaker (if the engine
  gives it), source, translation, status, flags. Atomic writes.
- **Reader plugins** per engine. RPG Maker MV/MZ first (JSON + `plugins.js`), then
  Ruby marshal (XP/VX/VXA), then Wolf. Start from RPGMTL/rewolf ideas; our own code.
- **Skip filter — a third policy.** Structured readers already separate engine data
  from dialogue, so neither `origin="offline"` nor `"live"` is right. **This is an
  interface mismatch to resolve, not to paper over** (e.g. a new `origin` value). It
  also partly answers the §6 experiment in `2026-07-12-skip-filter-redesign.md` for
  structured games. Decide with Kano before coding.
- **Patcher:** (1) back up every file it will touch into a timestamped folder,
  (2) write a manifest (path, hash before/after), (3) write patched files to a
  staging copy, (4) verify they re-read, (5) swap in. **Rollback** restores from the
  manifest. Failure at any step leaves the game untouched.
- Mirrors the XUnity installer's manifest approach (`install_game.py`).

## 10. Review list

- Table: scene, speaker, source, translation, status (`ok`, `kept-original`,
  `language-drift`, `structure-fail`, `flagged`, `edited`).
- **Auto-flag** on `validate_or_recover` / `language_ok` failure.
- **Manual flag**, multi-select (Ctrl/Shift-click).
- **Re-translate selected:** each line is translated alone with the previous *K* lines
  from game order as reference-only context (same "do not translate" block as
  `core.py:328 context_block`). K is a setting. Selection never merges contexts.
- Inline edit; edited lines are protected from re-translation unless forced.
- Filter by status/scene, search source and translation.

## 11. UI (Tkinter + ttk)

- Stay on stdlib Tkinter + ttk (existing `ui.py`, `theme.py` dark theme). `ttk.Treeview`
  for the review list — must stay responsive at tens of thousands of rows (verify;
  use lazy insert if not).
- Pages: **Projects** (open a game, extract, patch/rollback) · **Review** · **Providers**
  (config, test connection, concurrency probe) · **XUnity** (existing install wizard
  and server) · **Settings**.
- No web server, no extra ports. Heavy work runs off the UI thread.
- **Look:** a plain, modern desktop program, not a showpiece. Standard layout (sidebar
  or tabs, toolbar, table, status bar), one neutral palette, one accent colour, system
  font, real widgets. No gradients, glow, big hero text, emoji icons or marketing copy.
  Dense and readable beats pretty; effort goes to spacing, alignment and consistency.

## 12. README outline

Lead with *why*: completeness over speed, and the safeguards (structure check,
language-drift guard, skip policies, glossary, scene context, review list, patch
rollback). Then: supported engines (with honest per-engine status), install, quick
start, providers and the probe, XUnity live mode, credits (RPGMTL, rewolf-trans,
WolfTrans, RPGMLocalizer, Translator++ inspiration), known gaps. **Every "supported"
claim must name what was verified.**

## 13. Phases

0. `git init`, backup, resolve §9 policy question.
1. Provider layer + probe (`call_llm` refactor, providers page).
2. Scene-based work unit + project file.
3. RPG Maker MV/MZ reader + patcher/rollback. Then Ruby engines. Then Wolf.
4. Review list.
5. UI redesign pass.
6. README, STATUS.md rewrite, rename.

## 14. Open / unverified

- Wolf `Data.wolf` archive decryption: no tool found. Needs research or a user step.
- Anthropic Messages adapter: format differences (system prompt, no `choices`) to
  confirm against current docs when building.
- Cloud-model behaviour on this content is untested.
- Scale of Treeview with real game sizes is untested.
- Nothing here is field-tested in a live game.
