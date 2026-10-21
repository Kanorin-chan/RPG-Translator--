#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""
Regression test for the XUnity config PATH — the seam the first live launch
(kokageASMR, 2026-07-25) proved wrong.

The BepInEx plugin loads BepInEx\config\AutoTranslatorConfig.ini. Writing to the
ReiPatcher path BepInEx\AutoTranslator\Config.ini is silently ignored (the plugin
then generates its own English GoogleTranslate default). So:
  * install_game must render the game config to the config\ path
    (via variants.DEFAULT_CONFIG_REL / each variant's config_rel), and
  * configure_game.find_config must PREFER the config\ path when both exist, so
    "Reconfigure only" edits the file the game actually reads.

Also guards the TMP-font seam (2026-07-25): TextMeshPro text tofus (□) without a
CJK font, so the installed config must carry OverrideFontTextMeshPro chosen by
target language (variants.fonts_for) — verified through the real install render.

Pure/offline — no game, no server, no LLM.
    PYTHONUTF8=1 python test_config_path.py
"""

import os
import tempfile
import unittest

from rpgt.unity import configure_game
from rpgt.unity import install_game
from rpgt.unity import variants

CONFIG_REL = os.path.join("BepInEx", "config", "AutoTranslatorConfig.ini")
DEAD_REL = os.path.join("BepInEx", "AutoTranslator", "Config.ini")


class ConfigPathTest(unittest.TestCase):
    def test_default_config_rel_is_bepinex_config(self):
        self.assertEqual(variants.DEFAULT_CONFIG_REL, CONFIG_REL)

    def test_every_variant_routes_config_to_the_live_path(self):
        for key, var in variants.VARIANTS.items():
            self.assertEqual(var["config_rel"], CONFIG_REL,
                             "%s routes config to the wrong path" % (key,))

    def _touch(self, root, rel):
        p = os.path.join(root, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            f.write("[Service]\nEndpoint=GoogleTranslateV2\n")
        return p

    def test_find_config_prefers_the_live_path_when_both_exist(self):
        with tempfile.TemporaryDirectory() as root:
            self._touch(root, DEAD_REL)
            live = self._touch(root, CONFIG_REL)
            got = configure_game.find_config(root)
            self.assertTrue(got and os.path.samefile(got, live),
                            "find_config returned %r, expected the config\\ path" % got)

    def test_find_config_falls_back_to_dead_path_if_alone(self):
        with tempfile.TemporaryDirectory() as root:
            dead = self._touch(root, DEAD_REL)
            got = configure_game.find_config(root)
            self.assertTrue(got and os.path.samefile(got, dead),
                            "find_config returned %r, expected the fallback" % got)


class InstallFontTest(unittest.TestCase):
    def test_fonts_for_maps_by_language(self):
        self.assertEqual(variants.fonts_for("zh-Hant"),
                         ("Microsoft JhengHei", "Microsoft JhengHei"))
        self.assertEqual(variants.fonts_for("zh-Hans"),
                         ("Microsoft YaHei", "Microsoft YaHei"))
        self.assertEqual(variants.fonts_for("en"), ("", ""))
        # unknown -> Traditional (the project's default JP->zh-Hant direction)
        self.assertEqual(variants.fonts_for("xx"),
                         ("Microsoft JhengHei", "Microsoft JhengHei"))

    def test_write_config_sets_tmp_font_for_zh_hant(self):
        """The install render must CARRY the font settings (best-effort TMP font;
        the real modern-TMP fix is a font-asset bundle — STATUS #4). Exercised
        through install_game._write_config itself."""
        var = variants.VARIANTS[("Mono", "x64")]
        template = variants.config_template_path(var)
        if not os.path.isfile(template):  # unity-kits/ is not in git (big downloaded kits)
            self.skipTest("Unity kit not present: %s" % template)
        with tempfile.TemporaryDirectory() as root:
            install_game._write_config(
                root, template, variants.DEFAULT_CONFIG_REL, "zh-Hant",
                "http://127.0.0.1:4404/translate", [], [])
            out = os.path.join(root, variants.DEFAULT_CONFIG_REL)
            with open(out, encoding="utf-8-sig") as f:
                text = f.read()
        self.assertIn("OverrideFontTextMeshPro=Microsoft JhengHei", text)
        self.assertIn("Endpoint=CustomTranslate", text)
        self.assertIn("Language=zh-Hant", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
