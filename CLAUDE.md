# eroge-translator — CLAUDE.md

Local-first eroge (JP adult VN) translation toolkit. One shared Gemma brain, two
front doors: an **offline** MTool-export translator and a **live** XUnity server.

## ⛔ STOP. Read before you write.

**Do not write, edit, or plan code until you have read both of these.** It is two
files. If you are about to edit code and you have not read them, you have not
started work — you have started damage.

1. **`STATUS.md`** — what exists, where, its signature, wired?, verified?
2. **`docs/2026-07-12-skip-filter-redesign.md`** — the agreed next change to
   `core.py`'s filter. **SPEC, not implemented.** Gated on the §6 experiment.

**`docs/archive/` is NOT in the read path.** It holds history. Ignore it unless
you are deliberately hunting the past.

> ⚠ **`docs/archive/2026-07-10-NOTES-pre-build-ideation.md` says "no code yet."**
> That was true for one day. There are now 9 modules and ~3,500 lines. It is the
> pre-build ideation doc, kept for the reasoning only. **If you find yourself
> about to build this project from scratch, you are reading that file.** Stop.

## The docs can be wrong. The code cannot.

Where a doc and the code disagree, **the code is right.** Fix the doc then and
there, say so out loud, and continue. Do not quietly route around a doc you
believe is stale — routing around a lie is how projects get rebuilt.

## Before you write ANY function

**Search for it first.** `rpgt/core.py` already has the primitive.

- Exists and fits → **import it.** Never copy its body.
- Exists but doesn't fit → name the **`file:line`**, extend properly or ask Kano.
  **Never fork it.**
- Truly absent → build it properly, in the right module.

☠️ **`core.py:260 is_translatable()` is DEAD — do not call it.** Zero callers. It
wraps `should_translate` but defaults to `mode="strict_ja"` (the project default
is `dynamic`) and **drops the `origin` split entirely**. The name reads better
than the real one, which is exactly why it is dangerous.
**The real filter is `core.py:194 should_translate(s, mode, origin, require_cjk)`.**

## Hardcoding is a bug report, not an implementation

If you need an invented constant to make two modules fit, you have found an
interface mismatch. **Say so.** Do not paper over it and call it "wired."

## "It runs" is not done

Done = every value flowing through came out of a real module and you can name
which one. **Nothing here is field-tested in a live game.** "Self-validated"
means it agreed with itself.

## Project specifics

- **Stack:** Python 3, stdlib only + `tkinterdnd2` (UI). No venv, no packaging.
- **Always run with `PYTHONUTF8=1`.** CJK breaks otherwise. Never hand-test CJK
  through curl-from-Git-Bash — it mangles the argv.
- **Layout (2026-10-21):** all code is the `rpgt/` package (`core`, `providers`, `runner`, `patcher`, `readers/`, `gui/`, `unity/`, `mtool/`); tests in `tests/`. Older docs and STATUS.md name flat files such as `core.py`, `server.py`, `ui.py`: add `rpgt/`, `rpgt/unity/`, `rpgt/gui/app.py` in front.
- **Run it:** `run-ui.bat` = `python -m rpgt` (control UI) · `run-server.bat` = `python -m rpgt.unity.server` (live server only).
- **The brain is external:** local LLM at `http://127.0.0.1:8123/v1/chat/completions`,
  model `gemma4-12b-qat-uncensored-hauhaucs-balanced`, **thinking disabled
  server-side** (non-negotiable — reasoning-on burns 400–2000 tok/line and
  truncates). **It is often down.** Handle that, don't assume it.
- **Tests:** almost none. The one suite is `test_language_ok.py`
  (`PYTHONUTF8=1 python -m unittest tests.test_language_ok`, stdlib `unittest`, no LLM needed —
  it tests a pure function against real captured drift pairs). No runner.
- **Git:** not a repo. There is no undo. Back up before destructive edits.

### The thing that will bite you: the two paths have DIFFERENT skip policies

They are not an inconsistency. They see different text, and this is load-bearing.

- **Offline** (`translate.py`) reads an MTool export's **keys** = the game's raw
  string table — dialogue *and* JavaScript, shaders, notetags, asset filenames,
  damage formulas. `origin="offline"`, `require_cjk=True`.
- **Live** (`server.py`) only ever receives text the game **drew on screen** —
  never code. `origin="live"`, English UI copy is fair game.

Collapsing them into one policy corrupts one side or the other. `repair_output.py`
**must** mirror the offline policy exactly, or it resets good translations.
