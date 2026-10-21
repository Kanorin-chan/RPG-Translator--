<div align="center">

# RPG-Translator

**Local-first game translation that cares more about getting it right than getting it fast.**

![python](https://img.shields.io/badge/python-3.12-3776AB?logo=python&logoColor=white)
![deps](https://img.shields.io/badge/dependencies-stdlib%20only-2ea44f)
![license](https://img.shields.io/badge/license-MIT-blue)
![status](https://img.shields.io/badge/status-early-orange)

</div>

Reads a game's own files, translates the text with a language model you choose
(a local one by default), checks every line, and patches the game with a backup
you can roll back. Unity games are translated live through XUnity.

> **Priority: completeness and precision of the translation, not speed.**
> Every line is validated. Lines the model gets wrong are kept as the original
> and flagged for you in a review list, never silently shipped.

## What it does

- **Reads games directly.** No MTool export needed for RPG Maker and Wolf.
- **Scene-aware.** Lines are translated in play order with the previous lines
  as context, so pronouns, speakers and ambiguous words resolve properly.
- **Safeguards on every line.** Formatting codes (`\N[1]`, `\C[6]`, hearts) must
  survive. The output must be in the target script only. A bad line is retried
  once, then kept as the original and flagged.
- **Review list.** Browse every line, flag lines, multi-select and re-translate
  them, each with its own few lines of context. Edit by hand; edits are protected.
- **Safe patching.** Staged write, verification, timestamped backup, manifest,
  one-click rollback. The game is untouched if any step fails.
- **Any provider.** OpenAI-compatible or Anthropic-style APIs, local (LM Studio,
  llama.cpp, Ollama) or cloud, with a fallback chain and a concurrency test.
- **Unity live mode.** A local server for XUnity.AutoTranslator, plus an
  installer that sets up BepInEx and the config.

## Engine status

Honest table. "Verified" means run on a real game on the author's machine.

| Engine | Extract | Patch + rollback | Notes |
|---|---|---|---|
| RPG Maker MV / MZ | Verified (real game, 17,560 lines) | Verified (real data copy, byte-identical rollback) | In-game launch not yet checked |
| RPG Maker XP / VX / VX Ace | Synthetic data only | Synthetic data only | No real game tested yet |
| Wolf RPG Editor | Verified (real game, 45,305 lines read from `.wolf` archives) | Verified on a scratch copy (loose files written, rollback exact) | Wolf 3.14 archives only. Not yet checked that the game loads patched files, see `readers/WOLF_NOTES.md` |
| Unity (XUnity live) | Live translation verified on one Mono/x64 game | n/a | TextMeshPro font gaps remain, see `STATUS.md` |
| MTool export (JSON) | Verified | n/a | Fallback input for any engine MTool supports |

## Quick start

Requires Python 3.12. No packages to install.

```bat
copy config.example.json config.json
set PYTHONUTF8=1
run-ui.bat
```

1. **Providers** page: add your model (for LM Studio use
   `http://127.0.0.1:8123/v1`), press *Test connection*, then *Run concurrency test*.
2. **Projects** page: choose the game folder, *Extract*, *Translate*.
3. **Review** page: check flagged lines, fix or re-translate them.
4. **Projects** page: *Patch game*. A backup is made first. *Roll back* undoes it.

Command line (offline MTool export): `python translate.py export.json --mode dynamic`

## Providers and the concurrency test

Each provider has a URL, key, model, API style, a thinking-off setting and a
concurrency value. The concurrency test sends sample batches at 1, 2, 4, 8, 16
parallel requests and reports two numbers: the *fastest* setting and the *safe*
one (no errors or rate limits, latency not blown up). The safe value is the default.
Local models usually want a low value; cloud APIs usually allow more.

Cloud providers may refuse or block explicit text. That is your choice and risk.
Keys are stored in `%APPDATA%\RPG-Translator\` and never logged.

## How it works

```
game files -> reader -> project (lines in play order)
                              |
                runner (per scene) -> core: skip filter, prompt, LLM call,
                              |             structure check, language check, retry
                              v
                       review list  ->  patcher (stage, verify, backup, swap, rollback)

Unity:  game -> XUnity -> server.py -> core -> your model
```

- `core.py` the shared brain: skip filter, prompts, validation
- `providers.py`, `probe.py` API clients and the concurrency test
- `readers/` one module per engine family
- `runner.py` scene-based translation job
- `patcher.py` backup, verify, swap, rollback
- `server.py`, `install_game.py` Unity live mode and installer

## Tests

```
set PYTHONUTF8=1
python -m unittest discover -p "test_*.py"
```

No model or game files needed. Passing tests mean the code agrees with its
fixtures. Compatibility with real games is tracked in the table above.

## Known gaps

- Only a few real games have been tested. Please file a *Game compatibility* report.
- Unity TextMeshPro fonts: some Traditional-only glyphs can render as boxes.
- Quality depends on the model. Small local models make more mistakes; the review
  list is there for that.

## Credits and license

MIT, see `LICENSE`. Format knowledge and ported code from other projects is listed
in `THIRD_PARTY.md`. Inspired by Translator++.
