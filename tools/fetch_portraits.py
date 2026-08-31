"""Download character portraits for the editor's speaker preview.

Portraits are not committed to the repo — run this once after cloning:

    python tools/fetch_portraits.py

Images are fetched from the Fire Emblem fandom wiki via its public
MediaWiki API and saved to app/src/public/portraits/<Name>.png (256 px).
"""
from __future__ import annotations
import json
import os
import time
import urllib.parse
import urllib.request
from pathlib import Path

UA = {"User-Agent": "Mozilla/5.0 (FE3H-Localization-Tool portrait fetcher)",
      "Accept": "image/png,image/*;q=0.8"}
API = "https://fireemblem.fandom.com/api.php"
OUT_DIR = Path(__file__).resolve().parent.parent / "app" / "src" / "public" / "portraits"

CHARACTERS = [
    "Edelgard", "Dimitri", "Claude", "Byleth", "Sothis", "Rhea", "Hubert",
    "Seteth", "Flayn", "Jeralt", "Felix", "Sylvain", "Dedue", "Mercedes",
    "Annette", "Ingrid", "Ashe", "Hilda", "Lysithea", "Marianne", "Lorenz",
    "Leonie", "Raphael", "Ignatz", "Caspar", "Linhardt", "Bernadetta",
    "Dorothea", "Ferdinand", "Petra", "Yuri", "Balthus", "Constance", "Hapi",
    "Anna", "Gilbert", "Catherine", "Shamir", "Alois", "Manuela", "Hanneman",
    "Cyril", "Thales", "Solon", "Kronya", "Jeritza", "Monica", "Aelfric",
    "Ladislava", "Randolph", "Judith", "Nader", "Rodrigue",
]

SPECIAL = {"Byleth": ["File:CYL Byleth M Academy Portrait.png"]}


def candidates(name: str) -> list[str]:
    return SPECIAL.get(name, []) + [
        f"File:CYL {name} Academy Portrait.png",
        f"File:CYL {name} Portrait.png",
        f"File:{name} Portrait 5Years.png",
        f"File:{name} Portrait 3H.png",
        f"File:Portrait {name} Heroes.png",
    ]


def api(params: dict) -> dict:
    url = API + "?" + urllib.parse.urlencode(params)
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA)) as r:
        return json.load(r)


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    allt = [(n, c) for n in CHARACTERS for c in candidates(n)]
    found: dict[str, str] = {}
    for i in range(0, len(allt), 45):
        batch = allt[i:i + 45]
        d = api({
            "action": "query", "format": "json", "prop": "imageinfo",
            "iiprop": "url", "iiurlwidth": "256",
            "titles": "|".join(c for _, c in batch),
        })
        for p in d["query"]["pages"].values():
            if "imageinfo" in p:
                found[p["title"]] = (
                    p["imageinfo"][0].get("thumburl") or p["imageinfo"][0]["url"]
                )

    ok, miss = [], []
    for name in CHARACTERS:
        dest = OUT_DIR / f"{name}.png"
        if dest.exists() and not os.environ.get("FE3H_PORTRAITS_FORCE"):
            ok.append(name)
            continue
        url = next((found[c] for c in candidates(name) if c in found), None)
        if not url:
            miss.append(name)
            continue
        # Ask the thumbnailer for real PNG bytes (it defaults to WebP).
        url += ("&" if "?" in url else "?") + "format=png"
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=UA)) as r:
                data = r.read()
            if not data.startswith(b"\x89PNG"):
                miss.append(f"{name} (not png)")
                continue
            dest.write_bytes(data)
            ok.append(name)
            time.sleep(0.15)
        except Exception as e:
            miss.append(f"{name} ({e})")

    print(f"ok: {len(ok)}")
    if miss:
        print("missing:", ", ".join(miss))
    print(f"→ {OUT_DIR}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
