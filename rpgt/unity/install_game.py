#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""
One-command wizard: install XUnity.AutoTranslator into a Unity game and point it
at our local CustomTranslate server (server.py).

    python3 install_game.py [game_folder] [--lang zh-Hant] [--uninstall]

  * game_folder omitted  -> a Tkinter folder picker opens ("select a game").
  * --lang               -> target [General] Language (default zh-Hant).
  * --uninstall          -> remove exactly what a previous install added
                            (reads the manifest), restoring the game to clean.

What it does (install):
  1. Finds the Unity game root (folder with *_Data + main .exe + UnityPlayer.dll).
  2. Detects engine (Mono vs IL2CPP) and arch (x64 vs x86) and ROUTES to the
     matching template:
        Mono/x64   -> xunity-template-mono-x64    (proven)
        Mono/x86   -> xunity-template-mono-x86
        IL2CPP/x64 -> xunity-template-il2cpp-x64  (EXPERIMENTAL / UNVALIDATED)
     Unsupported flavors (e.g. IL2CPP/x86, arm64) are refused with a clear
     message; an IL2CPP template that is only staged (no BepInEx 6 base) is
     refused rather than half-installed.
  3. Copies the routed offline template (winhttp.dll, doorstop_config.ini,
     BepInEx/, and for IL2CPP the dotnet/ runtime) into the game root (merge;
     warns if a BepInEx already exists).
  4. Writes BepInEx\config\AutoTranslatorConfig.ini (the path the BepInEx plugin
     actually loads) from the staged config template
     with OUR server settings (Endpoint=CustomTranslate, Custom Url from
     config.json, FromLanguage=ja, Language=<lang>, font/overflow helpers).
  5. Records every added file/dir to BepInEx\.eroge_wizard_manifest.json so
     --uninstall is exact.
  6. Probes http://<host>:<port>/health and reports whether the server is up.

The template must be assembled first (once):  python3 build_template.py
Standard library only. Run with PYTHONUTF8=1 on Windows.
"""

import argparse
import datetime
import json
import os
import shutil
import struct
import sys
import urllib.request

from rpgt import core
from rpgt.unity import configure_game
from rpgt.unity import variants

from rpgt import paths

# in-game relative paths
MANIFEST_REL = os.path.join("BepInEx", ".eroge_wizard_manifest.json")

# PE machine constants
_PE_X64 = 0x8664
_PE_X86 = 0x014C
_PE_ARM64 = 0xAA64
_PE_NAMES = {_PE_X64: "x64", _PE_X86: "x86", _PE_ARM64: "arm64"}


# --------------------------------------------------------------------------- #
# folder picker
# --------------------------------------------------------------------------- #
def pick_folder():
    """Open a Tkinter askdirectory dialog. Returns a path or None (cancelled /
    Tkinter unavailable)."""
    try:
        import tkinter as tk
        from tkinter import filedialog
    except Exception:
        return None
    try:
        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        d = filedialog.askdirectory(
            title="Select the Unity game folder to install translation into")
        root.destroy()
        return d or None
    except Exception:
        return None


# --------------------------------------------------------------------------- #
# game detection
# --------------------------------------------------------------------------- #
def _game_root_here(d):
    """If d itself is a Unity game root, return (exe_name, data_dirname); else
    None. A root has a *_Data dir + UnityPlayer.dll + a main .exe."""
    try:
        entries = os.listdir(d)
    except OSError:
        return None
    if "UnityPlayer.dll" not in entries:
        return None
    data_dirs = [e for e in entries
                 if e.endswith("_Data") and os.path.isdir(os.path.join(d, e))]
    if not data_dirs:
        return None
    exes = [e for e in entries if e.lower().endswith(".exe")
            and not e.lower().startswith("unitycrashhandler")]
    for data in data_dirs:
        prefix = data[:-len("_Data")]
        expected = prefix + ".exe"
        if expected in entries:
            return expected, data
    # fall back: any non-crashhandler exe alongside a *_Data dir
    if exes:
        return exes[0], data_dirs[0]
    return None


def find_game_root(path):
    """Locate the game root at path or exactly one level below it.
    Returns (root_abspath, exe_name, data_dirname) or raises ValueError."""
    p = os.path.abspath(path)
    if not os.path.isdir(p):
        raise ValueError("Not a folder: %s" % p)
    here = _game_root_here(p)
    if here:
        return p, here[0], here[1]
    # scan immediate subdirectories
    matches = []
    for name in sorted(os.listdir(p)):
        sub = os.path.join(p, name)
        if os.path.isdir(sub):
            r = _game_root_here(sub)
            if r:
                matches.append((sub, r[0], r[1]))
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        raise ValueError(
            "Multiple Unity games found under %s — pass the exact game folder:\n  %s"
            % (p, "\n  ".join(m[0] for m in matches)))
    raise ValueError(
        "No Unity game found at (or one level under): %s\n"
        "Expected a folder containing a *_Data dir, UnityPlayer.dll and the "
        "game .exe." % p)


def _pe_machine(path):
    """Read the PE 'Machine' field of a DLL/EXE. Returns int or None."""
    try:
        with open(path, "rb") as f:
            if f.read(2) != b"MZ":
                return None
            f.seek(0x3C)
            e_lfanew = struct.unpack("<I", f.read(4))[0]
            f.seek(e_lfanew)
            if f.read(4) != b"PE\x00\x00":
                return None
            return struct.unpack("<H", f.read(2))[0]
    except (OSError, struct.error):
        return None


def detect_flavor(root, data_dir):
    """Return (engine, arch) where engine in {Mono, IL2CPP}, arch in
    {x64, x86, arm64, unknown}."""
    il2cpp = (os.path.isfile(os.path.join(root, "GameAssembly.dll"))
              or os.path.isdir(os.path.join(root, data_dir, "il2cpp_data")))
    engine = "IL2CPP" if il2cpp else "Mono"

    # arch: prefer PE machine of UnityPlayer.dll; fall back to crash handler name
    machine = _pe_machine(os.path.join(root, "UnityPlayer.dll"))
    if machine in _PE_NAMES:
        arch = _PE_NAMES[machine]
    elif os.path.isfile(os.path.join(root, "UnityCrashHandler64.exe")):
        arch = "x64"
    elif os.path.isfile(os.path.join(root, "UnityCrashHandler.exe")):
        arch = "x86"
    else:
        arch = "unknown"
    return engine, arch


# --------------------------------------------------------------------------- #
# manifest
# --------------------------------------------------------------------------- #
def _manifest_path(root):
    return os.path.join(root, MANIFEST_REL)


def load_manifest(root):
    p = _manifest_path(root)
    if not os.path.isfile(p):
        return None
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def _makedirs_tracked(abspath, root, created_dirs):
    """Create abspath, recording (relative to root) each dir newly created."""
    to_create = []
    p = abspath
    while not os.path.isdir(p):
        to_create.append(p)
        parent = os.path.dirname(p)
        if parent == p:
            break
        p = parent
    for d in reversed(to_create):
        os.mkdir(d)
        rel = os.path.relpath(d, root)
        if rel not in created_dirs:
            created_dirs.append(rel)


# --------------------------------------------------------------------------- #
# server health
# --------------------------------------------------------------------------- #
def probe_health(host, port, timeout=2.0):
    url = "http://%s:%d/health" % (host, port)
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            body = r.read().decode("utf-8", "replace").strip()
            return True, body
    except Exception as e:
        return False, "%s" % (e,)


# --------------------------------------------------------------------------- #
# install
# --------------------------------------------------------------------------- #
def _write_config(root, config_template, config_rel, lang, url,
                  added_files, created_dirs):
    """Render Config.ini from the staged template with our runtime settings."""
    ofont, ofont_tmp = variants.fonts_for(lang)
    desired = {
        "Service": {"Endpoint": "CustomTranslate"},
        "General": {"FromLanguage": "ja", "Language": lang},
        "Custom": {
            "Url": url,
            "EnableShortDelay": "True",
            "DisableSpamChecks": "True",
        },
        "Behaviour": {
            # TMP text tofus without a CJK font — set BOTH the legacy and the
            # TextMeshPro override, chosen by target language (variants.fonts_for).
            "OverrideFont": ofont,
            "OverrideFontTextMeshPro": ofont_tmp,
            "EnableUIResizing": "True",
            "AutofitText": "True",
        },
    }
    lines, bom, nl = configure_game._read_lines(config_template)
    new_lines, _changes = configure_game.apply_changes(lines, nl, desired)
    dst = os.path.join(root, config_rel)
    _makedirs_tracked(os.path.dirname(dst), root, created_dirs)
    configure_game._write_lines(dst, new_lines, bom)
    if config_rel not in added_files:
        added_files.append(config_rel)


def route(engine, arch):
    """Map a detected (engine, arch) to a template variant.

    Returns (variant, error_message, exit_code). On success error_message is
    None; on failure variant is None and (message, exit_code) explain why."""
    var = variants.variant_for(engine, arch)
    if var is None:
        supported = ", ".join(v["label"] for v in variants.VARIANTS.values())
        return None, (
            "No template for %s/%s. Supported flavors: %s.\n"
            "Install manually or add a template for this flavor."
            % (engine, arch, supported)), 2

    complete, missing = variants.check_complete(var)
    if not complete:
        needs = variants.needs_file_path(var)
        if os.path.isfile(needs):
            msg = ("%s support is staged but the BepInEx base isn't installed — "
                   "see %s" % (var["label"], needs))
        else:
            msg = ("The %s template (%s) is not assembled/complete.\n"
                   "Missing: %s\nRun:  PYTHONUTF8=1 python3 build_template.py"
                   % (var["label"], var["template_name"],
                      ", ".join(missing)))
        return None, msg, 2
    return var, None, 0


def _print_emit(msg, err=False):
    """Default log sink for the CLI: normal lines to stdout, errors to stderr —
    preserving the original stdout/stderr split the CLI had."""
    print(msg, file=(sys.stderr if err else sys.stdout))


def _run_install(root, exe, data_dir, lang, emit):
    """The install body, routing all output through emit(msg, err=False) so the
    UI can capture it into a log while the CLI keeps its exact stdout/stderr."""
    engine, arch = detect_flavor(root, data_dir)
    emit("Game folder : %s" % root)
    emit("Main exe    : %s" % exe)
    emit("Detected    : %s / %s" % (engine, arch))

    var, err, code = route(engine, arch)
    if var is None:
        emit("")
        emit(err, err=True)
        return code
    emit("Template    : %s (%s)" % (var["template_name"], var["key"]))

    template_dir = var["template_dir"]
    config_template = variants.config_template_path(var)
    config_rel = var["config_rel"]
    if not os.path.isfile(config_template):
        emit("ERROR: staged config template missing: %s" % config_template,
             err=True)
        return 3

    if var["experimental"]:
        emit("")
        emit("*" * 68)
        emit("* EXPERIMENTAL / UNVALIDATED: IL2CPP support ships a bleeding-edge")
        emit("* BepInEx 6 base and has NOT been tested against a real IL2CPP game")
        emit("* (none was available). It may fail to load or translate. Use")
        emit("* --uninstall to revert cleanly if it does not work.")
        emit("*" * 68)

    # warn on pre-existing loader / BepInEx (we merge/refresh, don't wipe)
    pre_bepinex = os.path.isdir(os.path.join(root, "BepInEx"))
    pre_manifest = load_manifest(root)
    if pre_bepinex and not pre_manifest:
        emit("WARNING: a BepInEx folder already exists here and was NOT created "
             "by this wizard. Merging on top (existing files may be "
             "overwritten). Uninstall will only remove what THIS wizard adds.")
    if os.path.isfile(os.path.join(root, "winhttp.dll")) and not pre_manifest:
        emit("WARNING: winhttp.dll already present — overwriting with the "
             "template's loader.")
    if pre_manifest:
        emit("Note: already installed by this wizard — refreshing files "
             "(idempotent).")

    # seed tracking from a prior manifest so the removal record stays complete
    added_files = list(pre_manifest.get("files", [])) if pre_manifest else []
    created_dirs = list(pre_manifest.get("dirs_created", [])) if pre_manifest else []

    # copy template (everything except the _config_template staging dir and any
    # NEEDS_* staging notes)
    for src_root, dirs, files in os.walk(template_dir):
        rel_root = os.path.relpath(src_root, template_dir)
        if rel_root == ".":
            dirs[:] = [d for d in dirs if d != "_config_template"]
            rel_root = ""
        for fn in files:
            if rel_root == "" and fn.startswith("NEEDS_"):
                continue
            src = os.path.join(src_root, fn)
            rel = os.path.join(rel_root, fn) if rel_root else fn
            dst = os.path.join(root, rel)
            _makedirs_tracked(os.path.dirname(dst) or root, root, created_dirs)
            shutil.copy2(src, dst)
            if rel not in added_files:
                added_files.append(rel)

    # server url from config.json
    cfg = core.load_config()
    host = cfg.get("xunity_host", "127.0.0.1")
    port = int(cfg.get("xunity_port", 4404))
    url = "http://%s:%d/translate" % (host, port)

    # write the game's Config.ini
    _write_config(root, config_template, config_rel, lang, url,
                  added_files, created_dirs)

    # write manifest
    manifest = {
        "wizard": "rpgt/unity/install_game.py",
        "manifest_version": 2,
        "installed_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "variant": var["key"],
        "template": var["template_name"],
        "engine": engine,
        "arch": arch,
        "lang": lang,
        "server_url": url,
        "config_rel": config_rel,
        "files": added_files,
        "dirs_created": created_dirs,
    }
    core.atomic_write_json(_manifest_path(root), manifest, indent=2)

    # health probe
    up, detail = probe_health(host, port)

    # summary
    emit("")
    emit("Installed %d files into the game (merge-safe, idempotent)."
         % len(added_files))
    emit("  loader    : winhttp.dll, doorstop_config.ini")
    if engine == "IL2CPP":
        emit("  runtime   : dotnet\\  +  BepInEx\\core\\  (BepInEx 6 IL2CPP + "
             "XUnity.Common)")
    else:
        emit("  runtime   : BepInEx\\core\\  (BepInEx + XUnity.Common)")
    emit("  plugin    : BepInEx\\plugins\\XUnity.AutoTranslator\\ "
         "(incl. Translators\\CustomTranslate.dll)")
    emit("  config    : %s" % config_rel)
    emit("  manifest  : %s" % MANIFEST_REL)
    emit("")
    emit("Config Endpoint = CustomTranslate")
    emit("Custom Url      = %s" % url)
    emit("Language        = %s   (FromLanguage=ja)" % lang)
    emit("")
    if up:
        emit("Server health  : UP (%s responded '%s')"
             % ("http://%s:%d/health" % (host, port), detail))
    else:
        emit("Server health  : DOWN — start it before launching the game:")
        emit("    PYTHONUTF8=1 python3 server.py")
        emit("    (%s)" % detail)
    emit("")
    emit("Done. Launch the game to translate; each new line is translated once "
         "then cached.")
    emit("To undo:  PYTHONUTF8=1 python3 install_game.py \"%s\" --uninstall"
         % root)
    return 0


def do_install(root, exe, data_dir, lang):
    """CLI entry point — identical stdout/stderr to before."""
    return _run_install(root, exe, data_dir, lang, _print_emit)


# --------------------------------------------------------------------------- #
# importable API for the control UI (ui.py). These wrap the same routines the
# CLI uses; the CLI keeps calling do_install / do_uninstall / do_detect.
# --------------------------------------------------------------------------- #
def detect_game(path):
    """Detect the Unity game at `path` (or one level below). Returns a dict:
        {ok, root, exe, data_dir, engine, arch, supported, variant,
         experimental, label, error}
    Never raises for a detection problem — ok=False + error carries the reason."""
    try:
        root, exe, data_dir = find_game_root(path)
    except ValueError as e:
        return {
            "ok": False, "root": None, "exe": None, "data_dir": None,
            "engine": None, "arch": None, "supported": False, "variant": None,
            "experimental": False, "label": None, "error": str(e),
        }
    engine, arch = detect_flavor(root, data_dir)
    var, err, _code = route(engine, arch)
    supported = var is not None
    return {
        "ok": True, "root": root, "exe": exe, "data_dir": data_dir,
        "engine": engine, "arch": arch,
        "supported": supported,
        "variant": (var["key"] if var else None),
        "experimental": bool(var["experimental"]) if var else False,
        "label": "%s/%s" % (engine, arch),
        "error": None if supported else (err.splitlines()[0] if err else None),
    }


def is_applied(game_root):
    """True if this game looks patched: our wizard manifest is present, or a
    BepInEx folder / winhttp loader exists (something is installed)."""
    try:
        root, _exe, _data = find_game_root(game_root)
    except ValueError:
        root = os.path.abspath(game_root)
    return (load_manifest(root) is not None
            or os.path.isdir(os.path.join(root, "BepInEx"))
            or os.path.isfile(os.path.join(root, "winhttp.dll")))


def install(game_root, lang="zh-Hant", on_log=None):
    """Install into game_root (resolving the real root). on_log(msg) receives
    each progress line. Returns the exit code (0 = success)."""
    root, exe, data_dir = find_game_root(game_root)
    emit = _print_emit if on_log is None else (lambda m, err=False: on_log(m))
    return _run_install(root, exe, data_dir, lang, emit)


def reconfigure(game_root, lang="zh-Hant"):
    """Rewrite ONLY the game's XUnity Config.ini (endpoint + Url + language),
    without touching installed files. Returns {config_path, changes, url}.
    Raises ValueError if no config is found (the UI catches and logs it)."""
    try:
        root, _exe, _data = find_game_root(game_root)
    except ValueError:
        root = os.path.abspath(game_root)
    cfg_path = configure_game.find_config(root)
    if not cfg_path:
        raise ValueError("No XUnity Config.ini found under %s (install first?)"
                         % root)
    cfg = core.load_config()
    host = cfg.get("xunity_host", "127.0.0.1")
    port = int(cfg.get("xunity_port", 4404))
    url = "http://%s:%d/translate" % (host, port)
    desired = {
        "Service": {"Endpoint": "CustomTranslate"},
        "Custom": {"Url": url, "DisableSpamChecks": "True",
                   "EnableShortDelay": "True"},
        "General": {"Language": lang},
    }
    lines, bom, nl = configure_game._read_lines(cfg_path)
    new_lines, changes = configure_game.apply_changes(lines, nl, desired)
    configure_game._write_lines(cfg_path, new_lines, bom)
    # keep the manifest's recorded language honest, if a manifest exists
    mani = load_manifest(root)
    if mani:
        mani["lang"] = lang
        core.atomic_write_json(_manifest_path(root), mani, indent=2)
    return {"config_path": cfg_path, "changes": changes, "url": url}


def uninstall(game_root, on_log=None):
    """Revert exactly what a previous install added. on_log(msg) receives each
    line. Returns the exit code (0 = success, 1 = no manifest)."""
    try:
        root, _exe, _data = find_game_root(game_root)
    except ValueError:
        root = os.path.abspath(game_root)
    emit = _print_emit if on_log is None else (lambda m, err=False: on_log(m))
    return _run_uninstall(root, emit)


# --------------------------------------------------------------------------- #
# uninstall
# --------------------------------------------------------------------------- #
def _run_uninstall(root, emit):
    """Uninstall body, routing output through emit(msg, err=False)."""
    mani = load_manifest(root)
    if not mani:
        emit("No wizard manifest found at %s — refusing to guess. Nothing "
             "removed.\n(If this game was installed some other way, uninstall "
             "it manually.)" % _manifest_path(root), err=True)
        return 1

    removed_files = 0
    for rel in mani.get("files", []):
        p = os.path.join(root, rel)
        if os.path.isfile(p):
            try:
                os.remove(p)
                removed_files += 1
            except OSError as e:
                emit("  could not remove %s: %r" % (rel, e), err=True)

    # remove the manifest itself
    mp = _manifest_path(root)
    if os.path.isfile(mp):
        os.remove(mp)

    # prune dirs we created, deepest first, only if now empty
    removed_dirs = 0
    for rel in sorted(set(mani.get("dirs_created", [])),
                      key=lambda r: r.count(os.sep), reverse=True):
        p = os.path.join(root, rel)
        if os.path.isdir(p) and not os.listdir(p):
            try:
                os.rmdir(p)
                removed_dirs += 1
            except OSError:
                pass

    emit("Uninstalled from: %s" % root)
    emit("  removed %d files, pruned %d directories, removed manifest."
         % (removed_files, removed_dirs))
    left_bepinex = os.path.isdir(os.path.join(root, "BepInEx"))
    left_winhttp = os.path.isfile(os.path.join(root, "winhttp.dll"))
    if left_bepinex or left_winhttp:
        emit("  note: some non-wizard files remained (a pre-existing BepInEx "
             "or loader was left intact).")
    else:
        emit("  game folder is back to a clean (unpatched) state.")
    return 0


def do_uninstall(root):
    """CLI entry point — identical stdout/stderr to before."""
    return _run_uninstall(root, _print_emit)


# --------------------------------------------------------------------------- #
# detect-only (no changes to the game)
# --------------------------------------------------------------------------- #
def do_detect(root, exe, data_dir):
    """Print detection + the template it would route to, WITHOUT installing."""
    engine, arch = detect_flavor(root, data_dir)
    print("Game folder : %s" % root)
    print("Main exe    : %s" % exe)
    print("Detected    : %s / %s" % (engine, arch))
    var, err, code = route(engine, arch)
    if var is None:
        print("Template    : <none> — %s" % err.splitlines()[0])
        return code
    complete, _missing = variants.check_complete(var)
    print("Template    : %s (%s)%s"
          % (var["template_name"], var["key"],
             "  [EXPERIMENTAL]" if var["experimental"] else ""))
    print("Status      : %s" % ("complete/ready" if complete else "incomplete"))
    return 0


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #
def main():
    ap = argparse.ArgumentParser(
        description="Install XUnity.AutoTranslator into a Unity game and point "
                    "it at the local CustomTranslate server.")
    ap.add_argument("game_folder", nargs="?",
                    help="the game folder (or its parent). Omit to open a picker.")
    ap.add_argument("--lang", default="zh-Hant",
                    help="target [General] Language (default: zh-Hant)")
    ap.add_argument("--uninstall", action="store_true",
                    help="remove exactly what a previous install added")
    ap.add_argument("--detect", action="store_true",
                    help="only detect engine/arch + routed template; do not "
                         "modify the game")
    args = ap.parse_args()

    target = args.game_folder
    if not target:
        target = pick_folder()
        if not target:
            print("No game folder selected. Pass a path:\n    python3 "
                  "install_game.py \"<game folder>\"", file=sys.stderr)
            sys.exit(1)

    try:
        root, exe, data_dir = find_game_root(target)
    except ValueError as e:
        print("ERROR: %s" % e, file=sys.stderr)
        sys.exit(1)

    if args.detect:
        sys.exit(do_detect(root, exe, data_dir))
    elif args.uninstall:
        sys.exit(do_uninstall(root))
    else:
        sys.exit(do_install(root, exe, data_dir, args.lang))


if __name__ == "__main__":
    main()
