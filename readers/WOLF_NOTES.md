# Wolf reader notes

Verified ONLY against synthetic data built in `test_wolf.py` from the format as read in
rewolf-trans / WolfTrans. No real Wolf game has been tested.

## Scope
- Reads an already-extracted `Data` folder (`Data/MapData/*.mps`, `Data/BasicData/CommonEvent.dat`,
  every `*.project` + same-stem `*.dat` pair except `SysDataBasic.project`, `Game.dat`).
  A root that directly contains `BasicData`/`MapData` also works.
- `.wolf` archives are NOT decrypted. `archive_status(root)` returns `"archive-encrypted"`
  (only a `.wolf` present), `"extracted"`, or `"none"`. `detect()` is False for archive-only roots.
- Method: the parser records each string's byte span; `apply()` splices new strings into the
  original bytes. Nothing is re-serialised, so unknown data survives byte-for-byte.
  A string edit changes only its u32 length prefix + bytes + NUL (no offset tables exist in
  these formats), except `Game.dat`'s total-size field, which is recomputed.

## Obfuscation (from rewolf `wolf-crypko.ts`)
- First byte != 0 means encrypted: 10-byte header kept verbatim, rest XORed.
- Seeds = header bytes at indices `[0,3,6]` (DataBase .dat, rewolf) or `[0,8,6]` (Game.dat, WolfTrans).
- Per seed, intervals `1, 2, 5`: `seed = (seed*0x343FD + 0x269EC3) & 0xFFFFFFFF; data[j] ^= (seed>>28)&7`.
- XOR is symmetric, so re-encrypting uses the same header and routine.
- Maps and CommonEvent.dat are not encrypted in rewolf (they start with 0x00).

## Encoding and `write_encoding`
- Read: UTF-8 strict first, else cp932 (a cp932 string almost never decodes as UTF-8). Raw bytes are
  kept in the locator, so untouched strings are never re-encoded.
- Write: `Reader(write_encoding=...)`, default `"utf-8"`. Any Python codec works (`cp932`, `gbk`).
  Unencodable text raises `PatchError` (never silently replaced).
- rewolf: reads `SHIFT_JIS`, writes `GBK` by default and has no UTF-8 path. That works only for a
  Chinese-locale setup on a cp932 engine.
- DECISION (uncertain): default UTF-8 because translated Chinese cannot be cp932. This is correct
  only if the game's engine reads UTF-8 (I believe Wolf 3.5+ does; NOT verified). On a cp932-only
  engine UTF-8 text shows as mojibake; use `gbk` with a Chinese locale emulator, or a UTF-8 patch.
  `reader.game_encoding` after `extract()` reports `utf-8` / `cp932` / `ascii` / `mixed`: if it is
  `cp932`, warn the user before writing UTF-8.

## Kind mapping (rewolf PatchFileCategory -> Line.kind)
| source | rewolf category | Line.kind |
|---|---|---|
| 101 Message, string arg 0 (one Line per command, embedded newlines kept) | Normal | dialogue |
| 102 Choices, each string | Normal | choice |
| DB string value, field type 0 (field name has 説明/解説/詳細/desc/help, or value has newline) | Normal | desc |
| DB string value, field type 0 (otherwise) | Normal | name |
| 150 Picture, text type, only if CJK | Normal | other |
| 122 SetString, only if CJK | Normal | other |
| 112 StringCondition, only if CJK | Normal (deviation) | script |
| 103 Comment, 106 DebugMessage, only if CJK | Extra | script |
| 250 Database cmd, all other known commands, unknown commands, CE strArgs/optarg/cself, only if CJK | Warn/Danger | script |
| `Game.dat` title | not handled by rewolf | other |

- Deviation: StringCondition is `script` (translating a compared string without its SetString
  breaks game logic). Dialogue, choice, name and desc are extracted when non-blank. DB values that
  look like file names (no CJK and a slash or extension) are dropped.
- Not extracted at all: map event names, CE names/descriptions, `.project` type/field/entry names
  (rewolf treats them as Context only), picture file names.
- No Line.speaker: Wolf Message commands carry one text string, no speaker field.
- Scene: `<MapStem>/ev<id>[:name]`, `CE<id>[:name]`, `DB:<stem>/t<type>`, `Game`.
- Order: maps (natural filename sort), CommonEvent, databases, Game.dat.
- Line.id: `<rel file>#<path>`, e.g. `Data/MapData/Map1.mps#e1/p0/c3/s0`, `...CommonEvent.dat#ce0/c2/s0`,
  `...DataBase.dat#t0/d1/f1`, `...Game.dat#title`.
- Locator: `ftype`, `off`, `len`, `raw` (hex of the original string bytes), `enc`. `apply()` raises
  `PatchError` if the length prefix, terminator or raw bytes at `off` differ.

## Unknown / not handled
- Maps whose header is neither rewolf's JP nor EN header (e.g. other Wolf editor versions) are skipped;
  the file is listed in `reader.warnings`. Same for any file that fails a structural check.
- Command ids outside rewolf's table are parsed generically (same layout) and their strings are `script`.
  Whether newer editor versions add text-bearing commands is unknown.
- Move command (201) route block is parsed per rewolf (5 unknown bytes + flags + routes); unverified.
- Wolf 3.5+ file layout differences (Game.dat, maps, DB) are unverified in both directions.
- Game.dat: layout from WolfTrans (seed indices 0,8,6; the "size minus one" field). The title is `other`;
  whether changing it has side effects (save folder name) is unknown.
- The trailing 0xC1 of a DB .dat is not enforced (rewolf only logs it); `.project` trailing bytes are not checked.
- Archive decryption: not attempted.

## Credits and licences
- File-format logic ported from rewolf-trans by KCFindstr, MIT. Notice (required to keep):

```
MIT License

Copyright (c) 2022 KCFindstr

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```
  (Copyright line confirmed against the rewolf-trans LICENSE file; body is the standard MIT text.)
- WolfTrans (elizagamedev, MPL-2.0) was read for format knowledge only (Game.dat layout, command
  semantics). No code copied, so no MPL obligations attach to `wolf.py`.
- Thanks to vgperson for the command-id mapping that WolfTrans credits.
