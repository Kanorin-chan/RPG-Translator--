# Skip-filter redesign — replacing the engine-data filter

**Date:** 2026-07-12
**Status:** SPEC / proposal. No code touched (a translation job was running when this was written).
**Author:** Claude, at Kano's request ("write a full spec to replace those code… push back if not [needed]").
**Context:** the MTool author's own description of his filter (see "The author's answer" below).

---

## 0. The author's answer (the evidence this spec is built on)

Paraphrased from the MTool author's reply:

1. **Doesn't look like the source language → don't translate.**
2. **Extremely simple / short strings can't be translated — otherwise you cause 循環替換 (circular replacement).**
3. That's the entire "don't translate" list.
4. **Code-looking strings can be translated — it just "没效果浪費" (has no effect, wasted).**
5. Everything else: 我不管 (don't care).

Two load-bearing takeaways:

- **(4) says translating code is WASTE, not corruption.** If true, the filter's only real job is *saving tokens/latency* — which means an expensive LLM filter is self-defeating (see §2). **UNVERIFIED** — our own `STATUS.md` claims corruption; nobody has field-tested. Resolve with the 5-minute experiment in §6 before tuning J1 aggressiveness.
- **(2) names 循環替換 as one of only two rules he bothers with.** That is a real, common, game-breaking failure mode our current filter does **not** guard against. It becomes J2 below.

---

## 1. Why replace the current code at all

`core.py`'s `_is_engine_data` + `should_translate` is **failure-driven patchwork**: every new game surfaces a new engine-data shape and we bolt on another regex (`_NOTETAG_INLINE_RE`, `_SCRIPT_RE`, `_CODE_TAIL_RE`, …). Problems:

- No structure — a flat pile of `if regex: return False`.
- No confidence — every decision is a hard bool; the genuinely-ambiguous middle is force-called with no signal.
- Unknown coverage — we can't reason about what it over- or under-rejects; we only find out when a game breaks.
- Two real gaps: **no 循環替換 guard (J2)**, and **no salvage for invented codes (J3)** — those just fall through to "keep original / untranslated."

The replacement is **not** "add an LLM." It is: refactor the heuristic into a clean, tiered, testable classifier, and add the two missing deterministic guards.

---

## 2. REJECTED design: LLM reads a batch first to filter

Kano's proposal: one LLM read-only pass classifies each line as needs-translation or not, before the translation pass.

**Rejected for the filtering job.** Reasons:

| # | Reason |
|---|---|
| 1 | **Spends tokens to save tokens.** An extra full pass over the export on a decode-bound 4070 — the exact cost a filter exists to avoid. Net-negative on throughput. |
| 2 | **Redundant on the clear cases.** `void main(){`, `a.def/2.5+20`, `<key:value>`, `EV107` are unambiguous; a regex classifies them in microseconds. A 12B model adds only latency. |
| 3 | **Non-deterministic filter.** The model classifies the same line differently across runs (observed: 82/1062 `EV###` drifted). Putting the LLM in the filter makes the filter unreliable — worse than a deterministic regex. |
| 4 | **Contradicts the author's own answer.** If code-translation is harmless *waste* (§0.4), the filter's only job is saving tokens — and an LLM filter is the one tool that can't. |

**The kernel of the idea is kept**, scoped to where it pays for itself: the small `UNCERTAIN` residue (§3, tier C), behind an optional flag, one batch, decisions cached. That is the *only* justified LLM-in-filter surface.

**The valuable read-first pass is a different feature:** an LLM glossary / character-bible pre-pass for *consistency* (names, honorifics, recurring terms → seed glossary). Out of scope here; spec separately if wanted.

---

## 3. J1 — the tiered classifier (deterministic, replaces `_is_engine_data`/`should_translate`)

Replace the bool-returning tangle with one function returning a **decision + reason + confidence**:

```
classify(s, *, mode, origin, corpus=None) -> Decision
Decision = (verdict: SKIP | TRANSLATE | UNCERTAIN, reason: str, confidence: HIGH | LOW)
```

Ordered tiers (first match wins). Rationale beside each so the next game's surprise slots into a *named* tier instead of a new stray regex:

**Tier A — SKIP, high confidence (structurally non-text):**
- empty / whitespace-only
- pure control-code / pure-heart payload (after stripping `CODE_RE`, `♡`, `♥`)
- pure numeric / punctuation / symbol payload
- internal identifier — `[A-Za-z]{1,6}\d{1,5}` (EV107, SW01)
- bare literal — true/false/null/undefined/NaN
- engine constant — 2–5 all-caps (BGS, BGM, SE)
- path / asset filename
- **engine data (script/shader/notetag)** — the existing `_SCRIPT_RE`, `_CODE_TAIL_RE`, `_NOTETAG_RE`, `_NOTETAG_INLINE_RE`, kept but *relocated into this named tier*. Checked before any CJK test (notetags embed Japanese).

**Tier B — TRANSLATE, high confidence (clear display text):**
- payload contains CJK and matched no Tier-A engine-data marker → real dialogue/UI text.

**Tier C — UNCERTAIN (the residue the regex genuinely can't call):**
- a bare ASCII word/phrase in a JA-source game (UI copy, or a code identifier?)
- bracket-prose with no colon — `<管理存在>`, `<事象改変>` (stylized dialogue, or a notetag we're mis-reading?)
- mixed code-ish + natural-language fragments

Resolution of Tier C (config `uncertain_policy`):
- **`conservative` (default, zero LLM):** in a JA-source game, uncertain-and-non-CJK → SKIP; uncertain-and-CJK-bearing → TRANSLATE. Mirrors today's `offline_require_cjk` behaviour but only on the residue, not the whole file.
- **`llm_adjudicate` (optional flag):** send *only* the Tier-C residue (typically 2–5% of entries) to ONE classification batch → per-line skip/translate; cache the verdicts keyed on the source string. This is the sole justified LLM-in-filter path (§2 kernel).

`origin` still splits the two worlds: **offline** (translate.py) sees the raw string table → Tier A applies in full; **live** (server.py) only ever sees drawn text → skip the offline-only "lone ASCII word = identifier" and asset/path aggressiveness (English UI copy is fair game live).

`confidence` lets `repair_output.py` and future tooling reason about *why* a line was skipped, instead of re-deriving it.

### J1 aggressiveness is a knob pending the §6 experiment
Config `code_skip: aggressive | lenient`:
- **aggressive** (assume corruption): Tier-A engine data is hard-skipped. Current behaviour.
- **lenient** (author is right — code = harmless waste): Tier-A *code* still skipped **for speed only**, and a false-negative (a code line that slips through) is cheap, not fatal. Lets us relax borderline Tier-A rules that currently risk eating real dialogue.

Default stays `aggressive` until §6 answers it.

---

## 4. J2 — 循環替換 (circular-replacement) guard (deterministic, OFFLINE only)

The gem from the author. The live server is single-line and has no notion of a corpus, so this is offline-only (`translate.py` / import path).

**Failure mode:** MTool patches the game so displaying `source` shows `translation`. If a `translation` value, when displayed, itself matches another `source` key → it gets replaced again → chains → corruption. Ultra-short sources are the worst offenders (a translated common word collides everywhere as a substring).

**The invariant to enforce over the whole export dict:**

1. **Disjointness:** no emitted `translation` value may equal any *other* `source` key in the export. If it does, that entry is refused (kept as source) and logged — importing it would re-trigger replacement.
2. **Simple-string refusal (author's rule):** a source that is "too simple" (≤ N chars after payload-strip, or a single token) is not translated — its translation is too likely to be a common word that collides. Config `min_translatable_len` (default TBD from real data, start 2).
3. **Substring safety:** a very short source that appears as a substring of ≥1 other source key is not translated (replacing it corrupts the containing strings).

All three are set / substring checks over the already-loaded dict — O(n) to O(n·k), no LLM, runs once before finalizing output. Add `--no-collision-guard` to bypass for debugging.

**Verify before shipping:** confirm against a real MTool re-import that (a) is the actual replacement mechanism (runtime hook vs blunt file find-replace changes the exact guard). Part of the §6 experiment.

---

## 5. J3 — invented-code reconciliation (salvages the 5 current failures)

`failures.log` shows the model INVENTING `\N[1]` on fragmentary skill-description lines that contain **no** code:

```
SRC: 'を高める　ターンを消費しない'      (0 codes)
OUT: '提升\N[1]不消耗回合'               (1 invented code) -> mismatch -> retry fails -> kept as JP
```

Current `validate_or_recover`: structure mismatch → one solo retry → keep original (untranslated). So these lines ship as raw Japanese.

**Fix:** before giving up, if the *only* structural difference is that the OUTPUT carries codes the SOURCE lacked, **strip exactly those extra codes** and re-check the signature.
- Only strip codes ABSENT from the source. Never touch, reorder, or drop a code that IS in the source.
- If the stripped output now matches the source signature → accept it (translated + cleaned).
- If it still mismatches (e.g. a source code was dropped) → existing fallback (keep original + log).

Deterministic, safe, purely additive to the existing validator. Turns 5 untranslated lines into 5 translated ones.

---

## 6. Open question that gates §3 aggressiveness and §4 mechanism — RUN THIS FIRST

**Is translating engine data corruption (our STATUS claim) or harmless waste (the author's claim)?** 5-minute experiment:

1. Take one code string from a real export (`a.def / 2.5 + 20`, or a shader line).
2. Put a deliberately-wrong translation in an MTool Custom Translation import.
3. Launch the game, exercise the relevant system (the skill using that formula / the screen using that shader).
4. Observe: does it **break** (formula wrong / shader tofu) or **silently ignore** it?

- **Breaks →** we're right; keep `code_skip: aggressive`; J4 disjointness must be strict.
- **Ignores →** author's right; `code_skip: lenient` is safe; the filter is purely a speed optimization and we stop being paranoid.

Either answer is a config default, not a redesign.

---

## 7. Concrete deltas to `core.py`

- **Replace** `_is_engine_data` + the bool tail of `should_translate` → `classify(...) -> Decision`. `should_translate` becomes a thin `classify(...).verdict != SKIP` wrapper (or resolves UNCERTAIN per policy) so existing call sites are unchanged.
- **Add** `collision_guard(corpus) -> (safe_map, refused)` — offline only; called by `translate.py` before finalizing, and mirrored by `repair_output.py`.
- **Add** `reconcile_codes(source, translation) -> translation'` — called inside `validate_or_recover` before the keep-original fallback.
- **Add** optional `llm_adjudicate(uncertain_sources) -> {src: bool}` behind `uncertain_policy: llm_adjudicate`; cached.
- **Config:** `code_skip` (aggressive|lenient), `uncertain_policy` (conservative|llm_adjudicate), `min_translatable_len`, `collision_guard` (bool).

## 8. Explicitly out of scope
- LLM classify-everything pass (§2, rejected).
- Glossary / character-bible LLM pre-pass (a separate consistency feature; spec on request).
- Live-server collision guard (no corpus; N/A).

## 9. Recommended order
1. §6 experiment (5 min) — settles `code_skip` default and the J2 mechanism.
2. J3 code reconciliation — smallest, purely additive, recovers real lines now.
3. J1 tiered `classify` refactor — behaviour-preserving reorg + confidence + UNCERTAIN tier.
4. J2 collision guard — the author's gem; needs the §6 mechanism confirmed.
5. (Optional) Tier-C `llm_adjudicate`; (separate) glossary pre-pass.
