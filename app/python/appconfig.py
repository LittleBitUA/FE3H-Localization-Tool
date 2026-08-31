"""Machine-local configuration for the FE3H Localization Tool sidecar.

Looked up in this order:
    1. env FE3H_TOOL_CONFIG — explicit path to a JSON config
    2. <repo root>/fe3h-tool.config.json   (repo root = two levels above app/)
    3. <app dir>/fe3h-tool.config.json

The config file is machine-specific (emulator paths, game image, optional
reference translation) and is NOT committed — see
fe3h-tool.config.example.json in the repo root for the schema.

Recognized keys (all optional):
    title_id            game TitleID for LayeredFS deploys
                        (default: 010055D009F78000 — FE3H)
    eden_exe            path to the Eden emulator executable
    ryujinx_exe         path to the Ryujinx/Ryubing executable
    game_image          path to the game dump (.nsp/.xci) for auto-launch
    reference_mods_dir  path to a mods/ folder of an existing community
                        translation, used only as a "translatable entries"
                        filter for DATA1 scans
    names_json          path to a ThreeHousesFileNames.json mapping
                        (entry index -> readable name)
"""
from __future__ import annotations
import json
import os
from pathlib import Path

_CONFIG_CACHE: dict | None = None

DEFAULTS: dict = {
    "title_id": "010055D009F78000",
}


def _candidate_paths() -> list[Path]:
    out = []
    env = os.environ.get("FE3H_TOOL_CONFIG")
    if env:
        out.append(Path(env))
    here = Path(__file__).resolve().parent          # app/python
    out.append(here.parent.parent / "fe3h-tool.config.json")   # repo root
    out.append(here.parent / "fe3h-tool.config.json")          # app/
    return out


def get_config() -> dict:
    global _CONFIG_CACHE
    if _CONFIG_CACHE is not None:
        return _CONFIG_CACHE
    cfg = dict(DEFAULTS)
    for p in _candidate_paths():
        try:
            if p.is_file():
                cfg.update(json.loads(p.read_text(encoding="utf-8")))
                break
        except Exception:
            continue
    _CONFIG_CACHE = cfg
    return cfg
