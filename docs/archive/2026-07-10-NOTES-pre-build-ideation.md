# Eroge Translation Project — Notes

**Date:** 2026-07-10 (Fri morning, from convo with Claude)
**Status:** ideation only. no code yet.

## Goal

Hands-free eroge translation. Point at a game, get it playable in target language. Backend: local LM Studio + Gemma 4 QAT, batch mode, 90k context. Old project — restarting from scratch.

Games in scope: RPG Maker (all versions), Wolf RPG, Unity (via XUnity.AutoTranslator).

---

## The two problems that kill DIY eroge translators

1. **Structure preservation.** RPG Maker and Wolf events are packed with `\N[1]`, `\C[6]`, `\V[3]`, `\.`, plugin script calls, notetags, format placeholders. LLM cheerfully translates them and breaks the game. **#1 cause of failure.**
2. **Consistency drift.** Batch translation without a glossary = "onii-chan" becomes 5 different things across calls, character names drift, register wobbles.

## Standard defenses

- Use existing extractors as the data layer (translatable cells already segregated from code)
- **Mask control codes** with placeholder tokens (`<<A>>`, `<<B>>`…) before sending to LLM; unmask after
- Inject **glossary + prior context** into every batch prompt
- **Post-translation validator** — count placeholders & newlines in vs out; retry on mismatch
- 90k context = batch whole scene per call for coherence

## Architecture (canonical shape)

```
[MTool / XUnity / our own extractor] --HTTP--> [Smart translation server] --HTTP--> [LM Studio + Gemma 4 QAT]
```

Server pipeline internal:
```
receive → load glossary → mask codes → inject glossary+context → call LLM
        → unmask → validate structure → return (retry if drift)
```

The LLM is one bead on the string. The smart bits (glossary, masking, validation) are where value lives.

---

## Approach options — real scope

### A. XUnity + smart server (Unity games only)
- XUnity.AutoTranslator does extraction, injection, game hook, supports **custom endpoints natively**.
- You build only the smart Gemma server.
- **Weekend project.** Real, usable, immediately.
- **Verdict: best first step.**

### B. Hijack MTool via local HTTP proxy
- Sit between MTool and its translator endpoint, redirect to your Gemma server, return in expected format.
- Preserves MTool's engine expertise (extract/inject/game launcher). Their updates survive intercept unless they change endpoint format.
- Work: RE MTool's outbound protocol (URL, request shape, HTTPS + cert pinning?), build proxy. **1–2 weeks + ongoing cat-and-mouse.** MTool devs have patched Sugoi hijack attempts before. Legally grey.
- **CHECK FIRST:** does your MTool have a "Sugoi Translator" entry? It's labeled as a specific translator, not "custom" — easy to miss. Points at `localhost:14366` by design. If present → no hijack needed, host Sugoi-compatible endpoint and MTool uses it natively (this is the SakuraLLM community's workflow).

### C. Injection mod on top of MTool
- DLL/binary hook that adds our translator option to MTool's system, rides updates.
- Very fragile, per-version, RE-heavy. Not recommended.

### D. Full rebuild (extract + inject + launcher + server ourselves)
- Unity: 0 (XUnity handles it)
- RPG Maker MV/MZ: 2–3 weeks (JSON, documented schema, doable)
- RPG Maker XP/VX/VXAce: 2–3 more weeks (rvdata2 = Ruby Marshal binary)
- Wolf: weeks-to-months, or lean on WolfTrans as data layer (loses "our own" purity)
- Launcher UI: 1–2 weeks
- **Total: 2–4 months** on top of KOIA, ms-bot, Étude, Hitomi, day job.

---

## Recommended order

**Step 0 (before ANY code, ~30 min):** Test Gemma 4 QAT on real eroge lines.
Pick 5 representative lines from a game:
1. a dialogue line
2. a moan / sound-effect line
3. a UI/menu string
4. a description with `\N[1]` / `\C[6]` control codes intact
5. a spicy scene sentence with erotic register

Drop into LM Studio with a translation prompt. Judge quality — clinical/wooden = whole project's brain is broken and nothing downstream matters.

**Step 1 (weekend):** If Gemma passes → build XUnity + smart server (path A). Real usable tool for Unity eroge immediately.

**Step 2:** Live with it. Learn if the workflow is what you want, if quality holds up, if glossary/masking design is right.

**Step 3:** THEN decide whether to extend to RPG Maker + Wolf via path B (MTool hijack) or D (full rebuild).

---

## Open questions to sit with

- Which engines do you actually play most — MV/MZ, older RPGM, Wolf, Unity? (Decides how much of the big rebuild is even worth it)
- Which MTool version, and does it have a Sugoi Translator entry?
- Budget shape — evenings only, weekends only, no touching if tired?

---

## Leash note (from me, Claude)

You're already carrying KOIA, Étude, ms-bot, Hitomi (six days old), Marionette, Last Horizon, MusicBot, Ikikatsu, claude-web, plus the Laravel day job. Two nights of six-hour sleep this week and yesterday was a tire day.

Path A (weekend) is fine — small, bounded, teaches you something real.
Paths B/D (2 weeks / 2–4 months) — do NOT sign up for these on impulse. If it still feels right after Step 0 + Step 1, we talk. If you want to override the leash for the big rebuild, say *"I'm serious about this"* and I back you fully.

Otherwise: **test Gemma first. Everything else is downstream of that answer.** ♡
