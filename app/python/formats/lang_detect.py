"""Heuristic language detector for FE3H TextS samples.

Returns one of: JP, KOR, CHN, ENG, FRA, GER, ESP, ITA, UNKNOWN.
DATA1 doesn't tag entries with language, so we infer from first decoded strings.
"""
from __future__ import annotations
import re

# Stop-word lists (lowercase, surrounded by word boundaries during match).
# Picked to be discriminative between Romance/Germanic options.
WORDS = {
    "FRA": [
        "qu'", "d'", "n'", "l'", "c'", "s'", "j'", "t'",     # French elisions
        "vous", "nous", "est", "pas", "mais", "pour", "avec",
        "tout", "tous", "moi", "toi", "rien", "très",
    ],
    "GER": [
        "nicht", "ich", "der", "die", "das", "und", "ist", "ein",
        "mit", "aber", "auch", "sich", "auf", "mir", "wir", "sie",
    ],
    "ESP": [
        "que", "está", "estás", "no", "el", "la", "los", "las",
        "es", "se", "para", "por", "pero", "como", "más", "muy",
        "qué", "está", "estoy",
    ],
    "ITA": [
        "che", "non", "il", "lo", "la", "uno", "una", "sono",
        "essere", "molto", "ma", "ho", "ti", "mi", "le", "gli",
        "questo", "questa",
    ],
    "ENG": [
        "the", "you", "your", "and", "is", "are", "was", "this",
        "that", "have", "with", "for", "not", "but", "what", "who",
        "they", "their", "would", "could", "should",
    ],
}


def _char_buckets(text: str) -> dict:
    """Count code-point category occurrences."""
    out = {
        "kana": 0, "hangul": 0, "cjk": 0, "ascii": 0,
        "lat_extra": 0,            # any Latin char outside ASCII
        # Diacritic markers unique enough to disambiguate Romance/Germanic:
        "ss": 0,                   # ß           (GER)
        "ae_oe_ue": 0,             # ä ö ü       (GER mostly; FR rarely)
        "n_tilde": 0,              # ñ           (ESP)
        "spanish_marks": 0,        # ¿ ¡         (ESP)
        "cedilla": 0,              # ç           (FRA, rarely PT)
        "accent": 0,               # é è à ê î ô etc. (FRA/ITA/ESP/POR)
    }
    for ch in text:
        cp = ord(ch)
        if 0x3040 <= cp <= 0x30FF:
            out["kana"] += 1
        elif 0xAC00 <= cp <= 0xD7AF:
            out["hangul"] += 1
        elif 0x4E00 <= cp <= 0x9FFF:
            out["cjk"] += 1
        elif 0x20 <= cp <= 0x7E:
            out["ascii"] += 1
        elif 0xC0 <= cp <= 0xFF or 0x100 <= cp <= 0x17F:
            out["lat_extra"] += 1
            if ch == "ß":
                out["ss"] += 1
            elif ch in "äöüÄÖÜ":
                out["ae_oe_ue"] += 1
            elif ch in "ñÑ":
                out["n_tilde"] += 1
            elif ch in "¿¡":
                out["spanish_marks"] += 1
            elif ch in "çÇ":
                out["cedilla"] += 1
            elif ch in "áàâäãéèêëíìîïóòôöõúùûüýÿ":
                out["accent"] += 1
        elif ch in "¿¡":
            out["spanish_marks"] += 1
    return out


def _word_scores(text: str) -> dict[str, int]:
    """Count occurrences of stop-words per language, case-insensitive."""
    lower = text.lower()
    scores = {lang: 0 for lang in WORDS}
    for lang, words in WORDS.items():
        for w in words:
            # Use simple substring with boundary check to handle elisions like "qu'"
            # which already include a non-alpha char.
            if w.endswith("'"):
                scores[lang] += lower.count(w)
            else:
                # Whole-word match.
                scores[lang] += len(re.findall(r"\b" + re.escape(w) + r"\b", lower))
    return scores


def detect_lang_label(strings: list[str]) -> str:
    sample = "\n".join(strings[:5])
    if not sample.strip():
        return "UNKNOWN"

    b = _char_buckets(sample)

    # 1) Script-based fast paths.
    if b["kana"] >= 2:
        return "JP"
    if b["hangul"] >= 2:
        return "KOR"
    if b["cjk"] >= 5 and b["kana"] == 0 and b["hangul"] == 0:
        return "CHN"
    if b["ascii"] + b["lat_extra"] < 3:
        return "UNKNOWN"

    # 2) Diacritic-based hints (strong markers).
    if b["ss"] > 0 or b["ae_oe_ue"] >= 2:
        return "GER"
    if b["n_tilde"] > 0 or b["spanish_marks"] > 0:
        return "ESP"

    # 3) Stop-word voting (for the FRA/ITA/ENG and tie-break cases).
    ws = _word_scores(sample)
    # Cedilla alone is strong FRA hint, but only when no GER markers above.
    if b["cedilla"] >= 1:
        ws["FRA"] += 3
    if b["accent"] >= 3:
        # Romance language; bump FRA/ITA/ESP equally; let words decide.
        for k in ("FRA", "ITA", "ESP"):
            ws[k] += 1

    if all(v == 0 for v in ws.values()):
        # Pure ASCII without recognized stop-words; likely ENG.
        if b["lat_extra"] == 0:
            return "ENG"
        return "UNKNOWN"

    return max(ws.items(), key=lambda kv: kv[1])[0]


# Map source dropdown values (folder names) to detector labels.
SOURCE_TO_LABEL = {
    "JP": "JP",
    "ENG_U": "ENG", "ENG_E": "ENG",
    "GER": "GER",
    "FRA_E": "FRA", "FRA_U": "FRA",
    "ESP_E": "ESP", "ESP_U": "ESP",
    "ITA": "ITA",
    "KOR": "KOR",
    "CHN": "CHN", "TWN": "CHN",
}

# Reverse: filename language tokens → detector label.
FILENAME_TOKEN_TO_LABEL = {
    "JP":   "JP", "JPN":  "JP",
    "ENG":  "ENG", "ENG_U": "ENG", "ENG_E": "ENG", "ENG_R": "ENG",
    "GER":  "GER",
    "FRA":  "FRA", "FRA_E": "FRA", "FRA_U": "FRA",
    "ESP":  "ESP", "ESP_E": "ESP", "ESP_U": "ESP",
    "ITA":  "ITA",
    "KOR":  "KOR",
    "CHN":  "CHN", "TWN":  "CHN",
}


def detect_from_filename(name: str) -> str:
    """Recognize language tokens in filenames like 'MV_N_011_FRA_E_VJ.bin'."""
    if not name:
        return "UNKNOWN"
    base = name.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    base = base.rsplit(".", 1)[0]
    tokens = base.split("_")
    # Look for the LAST occurrence — typical pattern is "..._<LANG>_<LANG_VARIANT>_<VOICE>".
    for i in range(len(tokens) - 1, -1, -1):
        t = tokens[i].upper()
        # Try compound first: <i>_<i+1> (e.g. "ENG_E")
        if i + 1 < len(tokens):
            pair = f"{t}_{tokens[i + 1].upper()}"
            if pair in FILENAME_TOKEN_TO_LABEL:
                return FILENAME_TOKEN_TO_LABEL[pair]
        if t in FILENAME_TOKEN_TO_LABEL:
            return FILENAME_TOKEN_TO_LABEL[t]
    return "UNKNOWN"


def matches_source(lang_label: str, source: str) -> bool:
    if lang_label == "UNKNOWN":
        return True
    return SOURCE_TO_LABEL.get(source.upper()) == lang_label
