# Third-party credits

RPG-Translator reads other projects for file-format knowledge. Where code was
ported, the original licence applies to that code.

| Project | Licence | How it is used here |
|---|---|---|
| [rewolf-trans](https://gitee.com/rootjd/rewolf-trans) (KCFindstr) | MIT | Wolf data-file logic in `readers/wolf.py` is ported from it. See `readers/WOLF_NOTES.md` for the notice. |
| [WolfTrans](https://github.com/elizagamedev/wolftrans) (elizagamedev) | MPL-2.0 | Read for format knowledge only. No code copied. |
| [RPGMTL](https://github.com/MizaGBF/RPGMTL) (MizaGBF) | MIT | Studied for the plugin and string-group design. |
| [RPGMLocalizer](https://github.com/Lord0fTurk/RPGMLocalizer) (Lord0fTurk) | GPL-3.0 | Read for ideas only (scene grouping, code shielding). No code copied. |
| [XUnity.AutoTranslator](https://github.com/bbepis/XUnity.AutoTranslator) + [BepInEx](https://github.com/BepInEx/BepInEx) | MIT / LGPL | Installed into Unity games by the XUnity wizard. Not bundled in this repo. |
| [DxLib](https://dxlib.xsrv.jp/) archive format | zlib | Format reference for `.wolf` archives. |

Inspired by Translator++.
