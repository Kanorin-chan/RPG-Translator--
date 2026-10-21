# Eroge Translator — Control UI Design

**Date:** 2026-07-10
**Status:** design approved by Kano; ready to build.

## Purpose

A small native desktop app that makes the toolkit usable without the command
line. Two jobs in one window:

1. **Translate** an MTool-exported JSON (the offline path) — Kano's stated core need.
2. **Set up a game** for live XUnity translation + control the server.

It is a **thin layer** over the existing scripts (`core.py`/`translate.py`,
`server.py`, `install_game.py`, `configure_game.py`, `variants.py`). It adds NO
translation logic — only file/folder selection, buttons, status, and log display.

## Approach

- **Tkinter** (ships with Python) + **`tkinterdnd2`** (pip) for real
  drag-and-drop that yields the actual filesystem **path** (not a browser-style
  content upload).
- Rejected: a browser UI (browsers hide real file paths for security — fails the
  path requirement); Qt/PySimpleGUI (heavier, more deps).
- New dependencies: `tkinterdnd2` (required for drag-drop); optionally `windnd`
  as a secondary path-getter. Everything else stays stdlib.

## Architecture

One window, a tabbed notebook with two tabs, and a persistent status line at the
bottom.

```
┌─ Eroge Translator ─────────────────────────┐
│ [ Translate ] [ XUnity ]                    │  ← tabs
│                                             │
│   (active tab body)                         │
│                                             │
├─────────────────────────────────────────────┤
│ Server ● running :4404      | idle          │  ← status line (always visible)
└─────────────────────────────────────────────┘
```

- `ui.py` — the app. Runs the long jobs (translate / install) on **worker
  threads** so the window never freezes; worker → UI updates marshalled back to
  the Tk main thread via a thread-safe queue.
- Reuses existing modules by import/subprocess. The translate job drives the
  `translate.py` pipeline; install/uninstall drives `install_game.py`; server
  start/stop launches/kills `server.py`.

## Tab 1 — Translate (offline MTool JSON)

- **Input:** a drop zone ("drop a .json here") + [Browse…]. Shows the resolved
  path. Drag-drop yields the real path (see Drag-drop chain).
- **Target language:** dropdown — Traditional (default) · Simplified · English.
  **No "from" selector** — dynamic mode auto-normalizes any source (JA/EN/mixed).
- **[Translate]** starts the run on a worker thread.
- **Progress:** a bar with `done/total · N failures · ETA`, updated live.
- **Live feed:** a scrolling read-only text area showing `source → translation`
  as lines complete (kept lightweight — just appends).
- **Resume-aware:** if `<input>.translated.json` already exists, it continues
  (the pipeline already skips finished entries); the UI states "resuming".
- **On done:** "Wrote `<name>.translated.json` — import into MTool," with an
  [Open output folder] button.

## Tab 2 — XUnity (game setup + server)

- **Server control:** ●/○ status, [Start] / [Stop], shows `http://127.0.0.1:4404`.
  Starting launches `server.py` as a child process; stopping terminates it.
- **Game input:** drop a game folder + [Browse…].
- **Detection:** on select, show detected engine/arch (e.g. "Mono x64 ✓",
  "IL2CPP x64 — experimental", "unsupported") AND **applied-or-not** (checks the
  install manifest / BepInEx presence in the game).
- **Contextual actions:**
  - Not applied → **[Install]**.
  - Already applied → **[Re-apply (redo)]** · **[Reconfigure only]** · **[Uninstall]**.
- **Target language** dropdown for the game (writes into the game's XUnity config).
- **Log:** one combined scrolling area showing (a) install/uninstall steps as they
  happen, then (b) the live server translation feed while the game runs.

## Drag-drop chain (must yield a real path, never an upload)

1. **`tkinterdnd2`** — primary. On drop, delivers the actual file/folder path.
2. **`windnd`** — secondary, only if tkinterdnd2 cannot be made to work.
3. **Browse dialog** — last resort.

The build MUST verify drag-drop genuinely works on this machine (a real drag
test, not just that the import succeeds), before the UI is called done.

## Out of scope (explicitly not this task)

- No new translation logic (all in `core.py`).
- TextMeshPro Unity-6 font fix — separate task.
- 4-way translate concurrency + finish_reason truncation-guard — separate tasks
  (the UI will benefit once they land, no UI change needed).
- Packaging into a single .exe — later, if wanted.

## Validation before "done"

- Drag a real `.json` onto Tab 1 and confirm the path resolves (real drag, not
  just Browse).
- Run a small translate (`--limit`-style) from the UI and see the progress bar +
  live feed move, and the "open folder" on completion.
- On Tab 2: Start the server (status flips ●), drop the `騙され` game copy, see
  it detect "Mono x64 ✓ / not applied", Install (log shows steps), re-select and
  see "applied" with Re-apply/Reconfigure/Uninstall, then Uninstall back to clean.
- Confirm the window never freezes during a long job (worker-thread check).

## Notes

- Project is not a git repo; this spec is a plain file (no commit).
- Windows, Python 3.12 (Microsoft Store build); Tkinter present; `tkinterdnd2`
  to be pip-installed and verified.
