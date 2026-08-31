# FE3H Localization Tool

**English** · [Українська](README.uk.md)

A toolkit for localizing **Fire Emblem: Three Houses** (Nintendo Switch):
game text extraction, a translation editor with speaker portraits, reinsertion
into the game's binary formats and LayeredFS deployment to an emulator or
console.

Built for the Ukrainian localization project of FE3H.

**Author:** Dmytro Bidlov («Little Bit» Team) · **License:** MIT

> This tool contains and distributes no game assets. It operates exclusively
> on your own legally obtained game dump.

---

## Features

- **Extract** — walks every text-bearing file (DATA1 indexed entries +
  path-based `patch1–4`), classifies the formats and produces a single
  `translation_bundle.txt`. Two-level deduplication (entry clusters +
  repeated strings) — every string is translated exactly once.
- **Editor** — Electron GUI: file navigator, Original/Translation cards,
  speaker recognition from the `[NNNN]` markers of scene dialogue (portraits,
  names, voice ids), game-style preview, Ctrl+S, unsaved-changes guard.
- **Apply** — multithreaded reinsertion of the bundle back into game formats
  with automatic propagation of translations to all duplicates and technical
  marker restoration; protects against typical translator mistakes (stray
  newline before a voice marker, etc.).
- **Deploy** — builds a LayeredFS overlay (`atmosphere/contents/<TitleID>/romfs`)
  with an INFO0/INFO2 patch, auto-deploys to Eden / Ryujinx and launches the game.
- **Font** — patches the G1T font atlas + UTF8TBL remap to render letters
  missing from the built-in font (Є/є), via DDS editing in Photoshop.
- **Textures** — export/reinsert of multi-texture G1T containers (title
  screen, monastery map) for translating baked-in art text.
- **Progress** — an accurate translated-strings counter (byte-level
  comparison against the originals, ignoring trailing whitespace).
- **Chunk workflow** — splits the bundle into portions for a translation team
  (`tools/split_bundle.py` / `tools/merge_bundle.py`) with strict marker
  validation on merge.

## Architecture

```
Renderer (React + TS)  ──IPC──→  Main (Electron)  ──stdio JSON-RPC──→  Python sidecar
      styles, editor,             dialogs, fs,          binary formats:
      speakers, preview           python bridge         TextS / Scene / Caption /
                                                        Credit / msgdata / G1T / DATA0-1
```

- UI: **electron-vite + React + TypeScript**
- Formats: **Python 3.11+** (no external dependencies)
- The RPC contract is typed in `app/shared/ipc.ts`

## Formats (implemented in `app/python/formats/`)

| Format | Files | Notes |
|---|---|---|
| DATA0/DATA1 | game archive | 32-byte records; chunked-zlib decompression |
| TextS (`_str.bin`) | UI text, support dialogue | UTF-8, original padding preserved |
| SceneText | talk_scinario | `[NNNN]` speaker + `＠NNNNNN` voice markers; strict layout validation |
| Caption / Credit | video subtitles, credits | f32 timings (caption); verbatim round-trip of unchanged entries |
| msgdata / ScrData | 12-language container | single language-slot replacement |
| INFO0/INFO2 | patch4 | cumulative overlay of indexed mods |
| G1T | textures/font | linear BC3, DDS interchange with Photoshop |

Byte layouts are verified against the 010 templates published by the
**[Three Houses Research Team](https://github.com/orgs/three-houses-research-team)**
(THRT) modding community. The serializers are covered by round-trip tests:
`parse → serialize` must reproduce the original bytes exactly.

## Installation

```bash
git clone https://github.com/LittleBitUA/FE3H-Localization-Tool
cd FE3H-Localization-Tool/app
npm install
python tools/fetch_portraits.py        # speaker portraits (optional)
npm run dev                            # run in dev mode
```

Requirements: **Node 20+**, **Python 3.11+** on PATH (or env `FE3H_PYTHON`).
Packaged releases ship their own Python runtime — see the
[Releases](https://github.com/LittleBitUA/FE3H-Localization-Tool/releases) page.

### Configuration

Copy `fe3h-tool.config.example.json` → `fe3h-tool.config.json` in the repo
root and fill in your paths (the file is never committed):

| Key | Purpose |
|---|---|
| `title_id` | game TitleID for LayeredFS (defaults to FE3H) |
| `eden_exe`, `ryujinx_exe` | emulator auto-launch after deploy |
| `game_image` | your game dump (.nsp/.xci) for auto-launch |
| `reference_mods_dir` | optional: a `mods/` folder of an existing reference translation, used as a "translatable entries" filter |
| `names_json` | optional: index→file-name mapping (ThreeHousesFileNames.json from THRT) |

### Game dump

Expected layout: `<romfs>/DATA0.bin + DATA1.bin + patch1..4/`
(a full RomFS dump from your console or emulator).

## Workflow

1. **Open dump…** → point at `romfs/`; **Set project…** → project folder.
2. **Scan patch + Scan DATA1** → the list of all text-bearing files.
3. **Extract** → `project/translation_bundle.txt` (re-running Extract merges
   with the existing bundle — translations are never lost).
4. Translate: directly in the editor (per file) or via the bundle/chunks.
5. **Apply bundle** → reinsertion into `project/romfs/`.
6. **Deploy** → LayeredFS build + copy to the emulator.

### Team translation via chunks

```bash
python tools/split_bundle.py     # bundle → chunks/chunk_NN_<topic>.txt + _manifest.json
# … chunks get translated …
python tools/merge_bundle.py     # chunks → bundle (strict #N marker validation)
```

## Tests

```bash
cd app/python
python -m unittest discover -s tests            # format unit tests
FE3H_TEST_ROMFS=/path/to/romfs \
python -m unittest discover -s tests            # + round-trip against a real dump
```

The round-trip tests are the main safety net: any serializer drift has
historically meant infinite-loading in game.

## Acknowledgements

- The **[Three Houses Research Team](https://github.com/orgs/three-houses-research-team)**
  community, for the 010 format templates and the file-name mapping.
- The Fire Emblem wiki, for reference material.
