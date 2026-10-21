#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""
Assemble the reusable offline XUnity/BepInEx drop-in templates.

Builds up to THREE game-agnostic install kits, one per Unity flavor, each a
complete BepInEx loader + runtime + XUnity.AutoTranslator plugin (incl.
CustomTranslate.dll) plus a staged AutoTranslator Config.ini with OUR local
CustomTranslate server settings baked in:

  xunity-template-mono-x64\    BepInEx 5 Mono/x64   (the proven kit)
  xunity-template-mono-x86\    BepInEx 5 Mono/x86   (32-bit base + same plugin)
  xunity-template-il2cpp-x64\  BepInEx 6 IL2CPP/x64 (EXPERIMENTAL, bleeding edge)

After this runs, every future game install (install_game.py) is a pure offline
file-copy routed to the matching template by detection.

Sources
-------
  * mono-x64  : HARVEST_BASE folder (a working BepInEx-5 Mono/x64 eroge install;
                its root loader + BepInEx/core/ runtime) + OVERLAY_ZIP (the
                user's Mono XUnity plugin overlay). Kept byte-for-byte identical
                to the proven v1 kit.
  * mono-x86  : downloads/BepInEx_win_x86_5.4.*.zip (full 32-bit base) +
                OVERLAY_ZIP (SAME Mono plugin — plugin DLLs are managed/arch-
                independent; only the loader/winhttp differs).
  * il2cpp-x64: downloads/BepInEx-Unity.IL2CPP-win-x64-6.0.0-be.*.zip (full
                BepInEx 6 IL2CPP base incl. dotnet/ runtime) + the XUnity
                IL2CPP AutoTranslator + ResourceRedirector overlays. If the
                BepInEx 6 base is absent, the template is STAGED (overlay only)
                with a NEEDS_BEPINEX6_BASE.txt explaining the exact drop-in.

Each variant is skipped cleanly (logged) if its downloads are missing; the build
reports which succeeded. Re-runnable (rebuilds from scratch). Standard library
only. Run with PYTHONUTF8=1 on Windows.

    PYTHONUTF8=1 python3 build_template.py
"""

import argparse
import glob
import os
import shutil
import sys
import zipfile

import core
import configure_game
import variants

HERE = os.path.dirname(os.path.abspath(__file__))
DOWNLOADS_DIR = os.path.join(HERE, "downloads")

# --- sources (this machine; override via CLI if they ever move) --------------
HARVEST_BASE = (
    r"C:\Users\user\Desktop\MegaBasterdWINDOWS\R3946"
    r"\[silent hentai] 妹妹说要用身体偿还借来的钱 AI翻译版 V1.2"
)
OVERLAY_ZIP = (
    r"C:\Users\user\Downloads\Compressed\Unity"
    r"\XUnity.AutoTranslator-BepInEx-5.4.5.zip"
)
KNOWN_GOOD_CONFIG = (
    r"D:\Extras\Current\Current Playing\Current Playing"
    r"\騙され呑みニケーション_Ver1_0_1\BepInEx\AutoTranslator\Config.ini"
)

# downloaded base/overlay zips (globbed so a version bump is picked up)
BEPINEX5_X86_GLOB = os.path.join(DOWNLOADS_DIR, "BepInEx_win_x86_5.4.*.zip")
BEPINEX6_IL2CPP_GLOB = os.path.join(
    DOWNLOADS_DIR, "BepInEx-Unity.IL2CPP-win-x64-6.0.0-be.*.zip")
IL2CPP_AT_GLOB = os.path.join(
    DOWNLOADS_DIR, "XUnity.AutoTranslator-BepInEx-IL2CPP-*.zip")
IL2CPP_RR_GLOB = os.path.join(
    DOWNLOADS_DIR, "XUnity.ResourceRedirector-BepInEx-IL2CPP-*.zip")

# root loader files harvested from the mono-x64 base folder.
ROOT_LOADER_FILES = ("winhttp.dll", "doorstop_config.ini", ".doorstop_version")

# exact URL for the IL2CPP base, echoed into the NEEDS file when it is absent.
BEPINEX6_URL = (
    "https://builds.bepinex.dev/projects/bepinex_be  ->  latest "
    "BepInEx-Unity.IL2CPP-win-x64-6.0.0-be.*.zip"
)


# --------------------------------------------------------------------------- #
# shared helpers
# --------------------------------------------------------------------------- #
def _find_one(pattern):
    """Return the newest path matching a glob, or None."""
    hits = sorted(glob.glob(pattern), key=os.path.getmtime, reverse=True)
    return hits[0] if hits else None


def _valid_zip(path):
    if not path or not os.path.isfile(path):
        return False
    try:
        with zipfile.ZipFile(path) as z:
            return z.testzip() is None
    except (zipfile.BadZipFile, OSError):
        return False


def _fresh_dir(path):
    if os.path.isdir(path):
        shutil.rmtree(path)
    os.makedirs(path)


def _extract_zip(zip_path, dest, skip_basenames=()):
    """Extract every file member of zip_path into dest (dirs auto-created).
    Members whose basename is in skip_basenames are skipped."""
    with zipfile.ZipFile(zip_path) as z:
        for member in z.namelist():
            if member.endswith("/"):
                continue
            norm = member.replace("\\", "/")
            if os.path.basename(norm) in skip_basenames:
                continue
            out = os.path.join(dest, *norm.split("/"))
            os.makedirs(os.path.dirname(out), exist_ok=True)
            with z.open(member) as fsrc, open(out, "wb") as fdst:
                shutil.copyfileobj(fsrc, fdst)


def _overlay_zip(zip_path, dest, only_prefix="BepInEx/"):
    """Merge a plugin overlay zip on top of dest, restricted (for safety) to
    members under only_prefix."""
    with zipfile.ZipFile(zip_path) as z:
        for member in z.namelist():
            if member.endswith("/"):
                continue
            norm = member.replace("\\", "/")
            if only_prefix and not norm.startswith(only_prefix):
                continue
            out = os.path.join(dest, *norm.split("/"))
            os.makedirs(os.path.dirname(out), exist_ok=True)
            with z.open(member) as fsrc, open(out, "wb") as fdst:
                shutil.copyfileobj(fsrc, fdst)


def _staged_config_bytes(known_good, lang, url):
    """Build the staged Config.ini (lines, bom) from the known-good config with
    OUR settings applied, reusing configure_game's section-aware editor."""
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
            "OverrideFont": ofont,
            "OverrideFontTextMeshPro": ofont_tmp,
            "EnableUIResizing": "True",
            "AutofitText": "True",
        },
    }
    lines, bom, nl = configure_game._read_lines(known_good)
    new_lines, _changes = configure_game.apply_changes(lines, nl, desired)
    return new_lines, bom


def _stage_config(dest, known_good, lang, url):
    cfg_dir = os.path.join(dest, "_config_template")
    os.makedirs(cfg_dir, exist_ok=True)
    lines, bom = _staged_config_bytes(known_good, lang, url)
    configure_game._write_lines(os.path.join(cfg_dir, "Config.ini"), lines, bom)


def _server_url():
    cfg = core.load_config()
    host = cfg.get("xunity_host", "127.0.0.1")
    port = int(cfg.get("xunity_port", 4404))
    return "http://%s:%d/translate" % (host, port)


def _verify(dest, required):
    """Print a present/missing line per required file. Return (ok, missing)."""
    print("  Required-file check:")
    ok = True
    missing = []
    for rel in required:
        p = os.path.join(dest, rel)
        exists = os.path.isfile(p)
        if not exists:
            missing.append(rel)
        ok = ok and exists
        print("    [%s] %s" % ("OK" if exists else "MISSING", rel))
    return ok, missing


# --------------------------------------------------------------------------- #
# per-variant builders — each returns "built" | "staged" | "skipped"
# --------------------------------------------------------------------------- #
def build_mono_x64(harvest_base, overlay_zip, known_good, lang):
    """The proven kit. Folder-base (root loader + BepInEx/core) + Mono overlay.
    Kept byte-for-byte identical to the v1 xunity-template output."""
    var = variants.VARIANTS[("Mono", "x64")]
    dest = var["template_dir"]
    print("=== mono-x64  ->  %s" % var["template_name"])

    base_core = os.path.join(harvest_base, "BepInEx", "core")
    if not os.path.isdir(harvest_base) or not os.path.isdir(base_core):
        print("  SKIP: harvest base / BepInEx\\core not found: %s" % harvest_base)
        return "skipped"
    if not _valid_zip(overlay_zip):
        print("  SKIP: Mono overlay zip missing/invalid: %s" % overlay_zip)
        return "skipped"

    _fresh_dir(dest)
    # 1) root loader files
    for name in ROOT_LOADER_FILES:
        src = os.path.join(harvest_base, name)
        if os.path.isfile(src):
            shutil.copy2(src, os.path.join(dest, name))
        elif name in ("winhttp.dll", "doorstop_config.ini"):
            print("  SKIP: required loader file missing in base: %s" % src)
            return "skipped"
    # 2) the whole BepInEx/core/ runtime
    shutil.copytree(base_core, os.path.join(dest, "BepInEx", "core"))
    # 3) Mono overlay on top (adds XUnity.Common.dll + plugins)
    _overlay_zip(overlay_zip, dest)
    # 4) staged config
    _stage_config(dest, known_good, lang, _server_url())

    ok, _missing = _verify(dest, var["required"])
    return "built" if ok else "staged"


def build_mono_x86(x86_zip, overlay_zip, known_good, lang):
    """32-bit BepInEx 5 base zip + the SAME Mono XUnity overlay."""
    var = variants.VARIANTS[("Mono", "x86")]
    dest = var["template_dir"]
    print("=== mono-x86  ->  %s" % var["template_name"])

    if not _valid_zip(x86_zip):
        print("  SKIP: BepInEx-5 x86 base zip missing/invalid "
              "(expected downloads\\BepInEx_win_x86_5.4.*.zip)")
        return "skipped"
    if not _valid_zip(overlay_zip):
        print("  SKIP: Mono overlay zip missing/invalid: %s" % overlay_zip)
        return "skipped"

    _fresh_dir(dest)
    # 1) full 32-bit base (root loader + BepInEx/core), minus changelog noise
    _extract_zip(x86_zip, dest, skip_basenames=("changelog.txt",))
    # 2) Mono overlay on top (arch-independent managed DLLs)
    _overlay_zip(overlay_zip, dest)
    # 3) staged config
    _stage_config(dest, known_good, lang, _server_url())

    print("  base: %s" % os.path.basename(x86_zip))
    ok, _missing = _verify(dest, var["required"])
    return "built" if ok else "staged"


def build_il2cpp_x64(base_zip, at_zip, rr_zip, known_good, lang):
    """BepInEx 6 IL2CPP base (incl. dotnet/ runtime) + XUnity IL2CPP overlays.
    If the base is missing, STAGE the overlays and write NEEDS_BEPINEX6_BASE."""
    var = variants.VARIANTS[("IL2CPP", "x64")]
    dest = var["template_dir"]
    print("=== il2cpp-x64  ->  %s  (EXPERIMENTAL)" % var["template_name"])

    have_at = _valid_zip(at_zip)
    have_rr = _valid_zip(rr_zip)
    if not have_at:
        print("  SKIP: XUnity IL2CPP AutoTranslator overlay missing/invalid "
              "(expected downloads\\XUnity.AutoTranslator-BepInEx-IL2CPP-*.zip)")
        return "skipped"

    _fresh_dir(dest)
    have_base = _valid_zip(base_zip)
    if have_base:
        # 1) full BepInEx 6 IL2CPP base (root loader + dotnet/ + BepInEx/*)
        _extract_zip(base_zip, dest, skip_basenames=("changelog.txt",))
        print("  base: %s" % os.path.basename(base_zip))
    else:
        print("  NOTE: BepInEx 6 IL2CPP base absent -> staging overlay only.")

    # 2) XUnity IL2CPP overlays (AutoTranslator, then ResourceRedirector)
    _overlay_zip(at_zip, dest)
    print("  overlay: %s" % os.path.basename(at_zip))
    if have_rr:
        _overlay_zip(rr_zip, dest)
        print("  overlay: %s" % os.path.basename(rr_zip))
    else:
        print("  WARN: IL2CPP ResourceRedirector overlay missing (the "
              "AutoTranslator zip already bundles a copy; continuing).")

    # 3) staged config (same XUnity Config.ini schema as Mono)
    _stage_config(dest, known_good, lang, _server_url())

    ok, missing = _verify(dest, var["required"])
    if ok:
        return "built"

    # incomplete -> write the NEEDS file with the exact drop-in instructions
    needs = variants.needs_file_path(var)
    with open(needs, "w", encoding="utf-8") as f:
        f.write(
            "IL2CPP template is STAGED but INCOMPLETE.\n"
            "The XUnity IL2CPP plugin overlay is in place; the BepInEx 6 IL2CPP\n"
            "base (loader + dotnet/ runtime + BepInEx/core) is MISSING.\n\n"
            "To complete this template, obtain:\n"
            "  BepInEx-Unity.IL2CPP-win-x64-6.0.0-be.*.zip\n"
            "from:\n"
            "  %s\n\n"
            "Then either:\n"
            "  (a) drop that zip into  %s\\  and re-run:\n"
            "        PYTHONUTF8=1 python3 build_template.py\n"
            "  or\n"
            "  (b) extract it (root winhttp.dll + doorstop_config.ini + dotnet\\\n"
            "      + BepInEx\\core\\) directly into this template folder, on top\n"
            "      of the existing overlay.\n\n"
            "Missing required files:\n%s\n"
            % (BEPINEX6_URL, DOWNLOADS_DIR,
               "\n".join("  - " + m for m in missing))
        )
    print("  -> STAGED (incomplete). Wrote %s" % variants.NEEDS_FILE)
    return "staged"


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #
def build(args):
    x86_zip = _find_one(BEPINEX5_X86_GLOB)
    il2cpp_base = _find_one(BEPINEX6_IL2CPP_GLOB)
    il2cpp_at = _find_one(IL2CPP_AT_GLOB)
    il2cpp_rr = _find_one(IL2CPP_RR_GLOB)

    results = {}
    results["mono-x64"] = build_mono_x64(
        args.harvest_base, args.overlay_zip, args.known_good_config, args.lang)
    print("")
    results["mono-x86"] = build_mono_x86(
        x86_zip, args.overlay_zip, args.known_good_config, args.lang)
    print("")
    results["il2cpp-x64"] = build_il2cpp_x64(
        il2cpp_base, il2cpp_at, il2cpp_rr, args.known_good_config, args.lang)
    print("")

    url = _server_url()
    print("=" * 60)
    print("Build summary (staged config: Language=%s, Custom Url=%s)"
          % (args.lang, url))
    for key in ("mono-x64", "mono-x86", "il2cpp-x64"):
        var = next(v for v in variants.VARIANTS.values() if v["key"] == key)
        status = results[key]
        note = ""
        if status == "staged" and key == "il2cpp-x64":
            note = "  (overlay only — see %s)" % variants.NEEDS_FILE
        print("  %-12s %-8s -> %s%s"
              % (key, status.upper(), var["template_name"], note))
    print("=" * 60)

    # mono-x64 is the critical proven path; fail the build only if it didn't build
    if results["mono-x64"] != "built":
        print("ERROR: the proven mono-x64 template did not build.",
              file=sys.stderr)
        return 1
    return 0


def main():
    ap = argparse.ArgumentParser(
        description="Assemble the offline XUnity templates (all three variants).")
    ap.add_argument("--harvest-base", default=HARVEST_BASE)
    ap.add_argument("--overlay-zip", default=OVERLAY_ZIP)
    ap.add_argument("--known-good-config", default=KNOWN_GOOD_CONFIG)
    ap.add_argument("--lang", default="zh-Hant",
                    help="default [General] Language baked into staged configs")
    args = ap.parse_args()
    sys.exit(build(args))


if __name__ == "__main__":
    main()
