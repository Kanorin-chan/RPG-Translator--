#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Tests for core.language_ok — the language-drift guard.

Every pair below is REAL: captured from a shipped output,
蜜宵のファミリアver1.00/ManualTransFile.translated.json (5,200 translated
entries, target = Traditional Chinese). The model occasionally code-switches
mid-line into a third language (Thai, Korean) or leaves Japanese kana in, and
the structure check (hearts + \\N[1] codes) never looked at the script, so all
of it shipped.

No LLM required — language_ok is a pure function.

    PYTHONUTF8=1 python test_language_ok.py
"""

import unittest

from rpgt import core

CFG = {"target_language": "Traditional Chinese"}

# (why, source, translation) — the model drifted out of the target script.
MUST_FAIL = [
    (
        "thai: 'plugin' translated into THAI (ปลั๊กอิน)",
        "封印（Seal Skill）されたスキルだけを非表示にするプラグイン",
        "僅隱藏已被封印（Seal Skill）的技能的ปลั๊กอิน",
    ),
    (
        "thai: 'leave' translated into THAI (ออกจาก)",
        "ダンジョンの外へ出ますか？",
        "要ออกจาก地下城嗎？",
    ),
    (
        "korean: 'ogre' half-translated into KOREAN (오)",
        "サテュロスLv30, オーガLv30",
        "薩提爾 Lv30， 오格 Lv30",
    ),
    (
        "kana residue: particle を left untranslated",
        "お金を",
        "金錢を",
    ),
    (
        "kana residue: その left untranslated",
        "採取アイテム候補　その３",
        "採集物品候選 その3",
    ),
]

# (why, source, translation) — good translations that must NOT be rejected.
MUST_PASS = [
    (
        "latin identifier is legitimate (S1M)",
        "マーカー S1M",
        "標記 S1M",
    ),
    (
        "greek beta is NOT target script, but it IS in the source -> escape hatch",
        "β版発動時",
        "β版發動時",
    ),
    (
        "simplified 个 is out of scope (different concern) -- must still pass",
        "敵単体の防御力を弱体化させる。",
        "削弱單个敵人的防禦力。",
    ),
    (
        "a correct translation of the Thai-drift line above",
        "ダンジョンの外へ出ますか？",
        "要離開地下城嗎？",
    ),
]


class TestLanguageOk(unittest.TestCase):
    def test_must_fail(self):
        for why, src, out in MUST_FAIL:
            with self.subTest(why):
                self.assertFalse(
                    core.language_ok(src, out, CFG),
                    "should have been REJECTED (%s)\n  SRC: %s\n  OUT: %s"
                    % (why, src, out),
                )

    def test_must_pass(self):
        for why, src, out in MUST_PASS:
            with self.subTest(why):
                self.assertTrue(
                    core.language_ok(src, out, CFG),
                    "should have been ACCEPTED (%s)\n  SRC: %s\n  OUT: %s"
                    % (why, src, out),
                )


if __name__ == "__main__":
    unittest.main(verbosity=2)
