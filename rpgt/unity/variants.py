#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""
Single source of truth for the XUnity template VARIANTS and detection routing.

Both build_template.py (which assembles the templates) and install_game.py
(which routes a detected game to the right template) import this so the two can
never disagree about a variant's folder, its config path, or the files that
prove it complete.

A "variant" is one (engine, arch) flavor of Unity we can install into:
  * Mono   / x64  -> xunity-template-mono-x64    (BepInEx 5, the proven kit)
  * Mono   / x86  -> xunity-template-mono-x86    (BepInEx 5, 32-bit base)
  * IL2CPP / x64  -> xunity-template-il2cpp-x64  (BepInEx 6 IL2CPP, EXPERIMENTAL)

Standard library only.
"""

import os

from rpgt import paths

# XUnity.AutoTranslator (BepInEx plugin — Mono 5.x AND IL2CPP 6.x) reads and
# writes its config from BepInEx's config dir: BepInEx\config\AutoTranslatorConfig.ini.
# All three variants share it.
#   ⚠ The ReiPatcher/standalone convention is a DIFFERENT path,
#     BepInEx\AutoTranslator\Config.ini. Writing there is SILENTLY IGNORED by the
#     BepInEx plugin, which then generates its own English GoogleTranslate default
#     at the config\ path — no link to our server. That was the original bug.
#   FIELD-VERIFIED 2026-07-25: kokageASMR (mono-x64, byte-identical template,
#     XUnity 5.4.5) launched -> XUnity created BepInEx\config\AutoTranslatorConfig.ini
#     and left BepInEx\AutoTranslator\Config.ini untouched. See STATUS.md.
DEFAULT_CONFIG_REL = os.path.join("BepInEx", "config", "AutoTranslatorConfig.ini")

# The staged NEEDS-file dropped into an IL2CPP template that has its overlay in
# place but is still missing the BepInEx 6 base.
NEEDS_FILE = "NEEDS_BEPINEX6_BASE.txt"

# Files that prove a template is COMPLETE (a copy-ready install kit). Keyed by
# variant key. The loader (winhttp + doorstop) + the runtime core + XUnity's
# Common.dll + the CustomTranslate translator are the load-bearing pieces.
_REQUIRED_MONO = (
    "winhttp.dll",
    "doorstop_config.ini",
    os.path.join("BepInEx", "core", "BepInEx.Preloader.dll"),
    os.path.join("BepInEx", "core", "XUnity.Common.dll"),
    os.path.join("BepInEx", "plugins", "XUnity.AutoTranslator",
                 "Translators", "CustomTranslate.dll"),
)
_REQUIRED_IL2CPP = (
    "winhttp.dll",
    "doorstop_config.ini",
    os.path.join("dotnet", "coreclr.dll"),                 # BepInEx 6 .NET host
    os.path.join("BepInEx", "core", "BepInEx.Unity.IL2CPP.dll"),
    os.path.join("BepInEx", "core", "XUnity.Common.dll"),
    os.path.join("BepInEx", "plugins", "XUnity.AutoTranslator",
                 "Translators", "CustomTranslate.dll"),
)


def _v(key, engine, arch, template, required, experimental):
    return {
        "key": key,
        "engine": engine,
        "arch": arch,
        "label": "%s/%s" % (engine, arch),
        "template_dir": os.path.join(paths.UNITY_DIR, template),
        "template_name": template,
        "config_rel": DEFAULT_CONFIG_REL,
        "required": required,
        "experimental": experimental,
    }


# (engine, arch) -> variant descriptor
VARIANTS = {
    ("Mono", "x64"): _v("mono-x64", "Mono", "x64",
                        "xunity-template-mono-x64", _REQUIRED_MONO, False),
    ("Mono", "x86"): _v("mono-x86", "Mono", "x86",
                        "xunity-template-mono-x86", _REQUIRED_MONO, False),
    ("IL2CPP", "x64"): _v("il2cpp-x64", "IL2CPP", "x64",
                          "xunity-template-il2cpp-x64", _REQUIRED_IL2CPP, True),
}


def variant_for(engine, arch):
    """Return the variant descriptor for a detected (engine, arch), or None."""
    return VARIANTS.get((engine, arch))


def fonts_for(lang):
    """(OverrideFont, OverrideFontTextMeshPro) OS-font names for a XUnity target
    Language code.

    `OverrideFont` (legacy UGUI/NGUI) takes an OS-font name and works. For
    `OverrideFontTextMeshPro`, an OS-font NAME only works on older Unity where
    XUnity can build a dynamic TMP font from it; on Unity 6 / TMP 3 it FAILS
    ("Could not find the TextMeshPro font asset") and a prebuilt TMP FontAsset
    bundle is required instead — see STATUS.md open item #4. Both are set here as
    best-effort (the TMP one is harmless where unsupported — the game font is kept).
      Traditional -> Microsoft JhengHei (msjh, proper traditional glyph shapes)
      Simplified  -> Microsoft YaHei    (msyh)
      English     -> none (Latin is in every font; overriding just looks wrong)
      Unknown     -> Traditional (the project's default JP->zh-Hant direction)."""
    t = (lang or "").strip().lower().replace("_", "-")
    if t.startswith("zh-hans") or t in ("zh-cn", "zh-sg", "chs"):
        return ("Microsoft YaHei", "Microsoft YaHei")
    if t.startswith("en"):
        return ("", "")
    return ("Microsoft JhengHei", "Microsoft JhengHei")


def config_template_path(variant):
    """Path to the variant's staged Config.ini (source for the game's config)."""
    return os.path.join(variant["template_dir"], "_config_template", "Config.ini")


def needs_file_path(variant):
    return os.path.join(variant["template_dir"], NEEDS_FILE)


def check_complete(variant):
    """Return (is_complete, missing_list). is_complete is False if the template
    folder is absent or any required file is missing."""
    d = variant["template_dir"]
    if not os.path.isdir(d):
        return False, ["<template folder missing: %s>" % variant["template_name"]]
    missing = [r for r in variant["required"]
               if not os.path.isfile(os.path.join(d, r))]
    return (len(missing) == 0), missing
