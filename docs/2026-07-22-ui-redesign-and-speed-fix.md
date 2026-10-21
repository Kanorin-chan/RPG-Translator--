# UI redesign + translation-speed audit/fix

**Date:** 2026-07-22
**Status:** SPEC. Speed fix (§1) is **DONE + verified**. UI redesign (§2) is
**NOT built** — this doc is the agreed design before implementation.
**Author:** Claude, at Kano's request ("UI redesign — modern, bigger drop box;
optimization — more translation speed").

---

## 0. TL;DR

- The "optimization" ask turned out to be a **correctness regression**, not a
  tuning problem. LM Studio was running with **thinking ON**; every call spent its
  whole `max_tokens` on `reasoning_content` and returned **empty** `content`.
  **Fixed** by sending `reasoning_effort:"none"` on every request. ~7x faster on
  the measured batch AND it restores output. See §1.
- The speed ideas that *sounded* good are dead on the data: in-file dedup 0.0%,
  cross-game TM 1–4% (cheap menu strings only), concurrency already at its locked
  ceiling. **Not building them.** See §1.3.
- UI: dark + light-blue theme, better tabs, bigger drop zones, card layout, in a
  new `theme.py`. **Zero translation-logic change.** See §2.

---

## 1. Speed / correctness — DONE

### 1.1 What the audit measured (LLM live, 2026-07-22)

Real prior run (`nekonokonomachi/run.log`): 25,946 keys → 19,933 translatable →
15,435 done in **10,176s (2.83h), 0.66s/entry**, batch 40 / concurrency 4.

Live benchmark of one fixed 40-line batch, **on this machine, now**:

| server state | completion tok | reasoning tok | time | finish | parse | content |
|---|---|---|---|---|---|---|
| thinking **ON** (found) | 2400 (cap) | 2397 | 60.9s | length | ❌ FAIL | **empty** |
| thinking **OFF** (`reasoning_effort:"none"`) | 328 | 0 | 8.4s | stop | ✅ 40/40 | correct |

The run is **decode-bound**: wall-time ≈ output tokens ÷ (4 parallel slots),
prefill is ~900 tok / ~2s. So the fix — decode ~15–25 content tok/line instead of
hundreds of reasoning tok/line — is the single biggest lever available.

### 1.2 The fix (applied)

- `core.py:call_llm` builds the payload dict and adds
  `payload["reasoning_effort"] = cfg.get("reasoning_effort", "none")` (skipped only
  if the config value is falsy). Sent on **every** request → fixes **both** front
  doors (offline `translate.py` and live `server.py`) at once.
- `config.json`: added `"reasoning_effort": "none"` + a `_comments` entry.
- `STATUS.md`: brain section records the 2026-07-22 finding + client-side
  enforcement.
- `reasoning_effort:"none"` is the **only** toggle LM Studio's current build
  honors. Verified ignored: `enable_thinking`, `chat_template_kwargs`,
  `thinking:{type:disabled}`, `reasoning:{enabled:false}`, prompt `/no_think`.
- **Verified end-to-end** through `core.call_llm`: content returned, 12/12 parsed,
  correct translations, 3.0s for a 12-line batch.

### 1.3 Rejected (with data — do not revisit without new evidence)

- **In-file dedup / normalized templating:** 7 of ~20k lines (0.0%). Worthless.
- **Persistent cross-game translation memory:** 1.1% / 4.0% overlap between the two
  real exports, and the shared lines are cheap RPG-Maker system strings
  (`オプション`, `決定`, `ゲーム終了`). Not worth the coupling.
- **Concurrency bump:** locked at 4 (server slot ceiling; "8 gives no gain").
- **Batch-size / prefill tuning:** marginal once reasoning is off; prefill is ~2s.

### 1.4 Optional follow-up (NOT in this change)

With reasoning off, `max_tokens_per_entry:60` is a loose ceiling (real content is
~8–25 tok/line). Lowering it saves nothing on speed (model stops at EOS now) but
would tighten KV reservation. Leave as-is unless a memory issue appears.

---

## 2. UI redesign — TO BUILD

**Decision (Kano):** dark theme + light-blue accent, "latest/chill" feel, **better
tab arrangement**, **restyle + layout rework** (card sections), **bigger drop
zones**. Stack stays stdlib + `tkinterdnd2` — no new deps.

**Non-negotiable:** presentation + layout only. Worker threads, the `queue.Queue`
pump (`_poll_queue`), health thread, drag-drop path resolution, and every
translate/install/server call stay **byte-for-byte behaviorally identical**. If a
redesign step would require touching logic, stop and flag it.

### 2.1 New module: `theme.py`

Keeps palette/style out of `ui.py` (project rule: right module, one purpose).

- `PALETTE` — dark dict: `bg #1e1f26`, `surface #2a2b34`, `surface_alt #33343f`,
  `border #3a3c47`, `text #e6e6ea`, `text_muted #9aa0ac`, `accent #5aa9e6`,
  `accent_active #7cbcf0`, `accent_fg #10131a`, `success #5cc98d`, `danger #e06a6a`,
  `danger_active #ef8080`, `drop_idle #262832`, `drop_hover #2f3a48`.
- `FONTS` — `base=("Segoe UI",10)`, `heading=("Segoe UI",11,"bold")`,
  `mono=("Consolas",9)`.
- `apply_theme(root) -> (palette, fonts)` — sets `root` bg; `ttk.Style` base
  `"clam"` (most restyle-able); configures & maps:
  - `TFrame`, `TLabel`, `Heading.TLabel`, `Muted.TLabel`
  - `TNotebook` (borderless, bg) + `TNotebook.Tab` (wide padding ~`(18,10)`, flat;
    **selected** = `surface` bg + `accent` fg indicator; **unselected** = `bg` +
    `text_muted`; hover map)
  - `Accent.TButton` (accent bg / accent_fg, hover=accent_active),
    `Secondary.TButton` (surface bg / text), `Danger.TButton` (surface bg /
    danger fg)
  - `TEntry`, `TCombobox` (surface field, text, border), `TProgressbar`
    (accent bar on surface trough), `Card.TLabelframe` + `.Label` (surface bg,
    heading label).
  Returns palette+fonts so `ui.py` can style the **tk** (non-ttk) widgets
  (drop zones, `ScrolledText`).
- `enable_dark_titlebar(root)` — best-effort Win11 dark title bar via
  `ctypes`/DWM (`DwmSetWindowAttribute`, attr 20). Guarded; no-op on failure.

### 2.2 `ui.py` — layout rework (cards)

Helper `_card(parent, title) -> ttk.LabelFrame` (Card style) for titled sections.

- **Translate tab:** `Input` card (big drop zone + path row) · `Options` card
  (target lang + Translate/Stop/Open) · `Progress & Feed` card (progressbar +
  status label + `ScrolledText` feed, expands).
- **XUnity tab:** `Server` card · `Game` card (big drop zone + path + detection +
  lang) · `Actions` card (install/re-apply/reconfigure/uninstall) · `Log` card
  (expands).
- Button roles: **Accent** = Translate, Install, Start. **Danger** = Stop,
  Uninstall. **Secondary** = Browse, Open folder, Re-apply, Reconfigure, Stop
  server. (Role = which ttk style name; commands unchanged.)
- Window default `900x780`, `minsize(760,620)` to seat the cards.

### 2.3 Bigger drop zones + drag feedback

Helper `_make_dropzone(parent, text, handler) -> widget`:

- A tall `tk.Frame`/`tk.Label` (~3–4x current height; big `⬇` glyph + label,
  `drop_idle` bg, accent 1px border feel via `highlightthickness`+`highlightbackground`).
- **Drag-over highlight:** extend `_register_drop` to also `dnd_bind`
  `<<DropEnter>>` → bg `drop_hover` + accent border, `<<DropLeave>>`/`<<Drop>>` →
  back to idle. Purely visual; the existing `<<Drop>>` handler and
  `_resolve_dropped_path` are unchanged.
- Degrade: when `dnd_ok` is false the zone shows "(drag-drop unavailable — use
  Browse)" exactly as today.

### 2.4 `ScrolledText` (feed + log) styling

Set via `.config()` with palette/fonts: `bg=surface`, `fg=text`, `insertbackground
=text`, `selectbackground=accent`, `font=mono`, flat relief. Trimming
(`MAX_FEED_LINES`) and the queue-driven append path are unchanged.

### 2.5 Out of scope

Light/auto themes (palette dict is structured for a future light variant, but only
dark ships now — YAGNI), runtime theme toggle, any translation/install/server
behavior change, new dependencies.

---

## 3. Files touched

| File | Change |
|---|---|
| `core.py` | **DONE** — `reasoning_effort` in `call_llm` (config-driven). |
| `config.json` | **DONE** — `reasoning_effort:"none"` + comment. |
| `STATUS.md` | **DONE** — brain-section note. |
| `theme.py` | **NEW** — palette, fonts, `apply_theme`, `enable_dark_titlebar`. |
| `ui.py` | Card layout, big drop zones + drag feedback, button roles, styled text widgets, dark title bar. **No logic change.** |

Backups already made: `core.py.pre-reasoning-fix.bak`,
`config.json.pre-reasoning-fix.bak`. Back up `ui.py` before editing (no git here).

## 4. Verification

- **Speed fix:** DONE — verified through `core.call_llm` (§1.2).
- **UI:** (a) headless smoke — import `ui`+`theme`, build the root + `App`, then
  destroy without `mainloop`, asserting no exception (catches bad style names /
  layout errors; needs a desktop session). (b) Kano launches `run-ui.bat` and
  visually confirms both tabs, both drop zones (incl. drag-over highlight), all
  button states, a real translate run, and server start/stop.
- "It runs" ≠ done: every widget must render and every existing command must still
  fire. Confirm the live feed and activity log still stream.
