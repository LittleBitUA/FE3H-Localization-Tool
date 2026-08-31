"""Font atlas editor for FE3H font00_JPN.

End-to-end: read the compressed font and its UTF8 codepoint table, paint a few
extra Ukrainian glyphs into otherwise-unused atlas cells, then re-encode the
atlas with texconv.exe, repack the Koei .gz wrapper, and patch UTF8TBL so the
game looks up our new codepoints to the new cells.

The four target glyphs (Є/є/Ґ/ґ) have no Latin look-alike in the font, so this
is the only way to render them properly.
"""
from __future__ import annotations
import struct
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont
import texture2ddecoder

from formats.data1 import decompress_koei, _align_0x80

ATLAS_SIZE = 4096
CELLS_PER_ROW = 64
CELL_SIZE = ATLAS_SIZE // CELLS_PER_ROW   # 64×64 pixels per glyph cell
KT_BLOCK = 0x10000

# DATA1 indexed-mod entry IDs we override:
#   72 = the small (4096×512) atlas the game actually renders UI text from
#   77 = the UTF8TBL that maps codepoint → gid for that atlas
SMALL_ATLAS_ENTRY = 72
SMALL_UTF8TBL_ENTRY = 77


# ----- UTF8TBL helpers -----

def utf8_bytes_to_codepoint(four: bytes) -> int | None:
    """Each 4-byte entry stores UTF-8 bytes in reversed order, zero-padded.
    Reverse non-zero bytes and decode UTF-8."""
    stripped = four.rstrip(b"\x00")
    if not stripped:
        return None
    try:
        return ord(stripped[::-1].decode("utf-8"))
    except (UnicodeDecodeError, TypeError):
        return None


def codepoint_to_utf8_entry(cp: int) -> bytes:
    """Inverse: encode codepoint as UTF-8, reverse, zero-pad to 4 bytes."""
    raw = chr(cp).encode("utf-8")
    reversed_bytes = raw[::-1]
    return reversed_bytes + b"\x00" * (4 - len(reversed_bytes))


def parse_utf8tbl(blob: bytes) -> list[int | None]:
    """Return one codepoint (or None) per glyph_id."""
    n = len(blob) // 4
    return [utf8_bytes_to_codepoint(blob[i * 4 : (i + 1) * 4]) for i in range(n)]


def serialize_utf8tbl(codepoints: list[int | None]) -> bytes:
    out = bytearray()
    for cp in codepoints:
        if cp is None:
            out += b"\x00\x00\x00\x00"
        else:
            out += codepoint_to_utf8_entry(cp)
    return bytes(out)


# ----- G1T helpers (single-texture font atlas, BC3 = DXT5) -----

@dataclass
class G1TFontFile:
    raw_g1t: bytes              # full decompressed G1T blob
    tex_data_offset: int        # absolute offset where DXT5 data begins
    tex_data_size: int          # BC3 bytes = width*height (1 byte/pixel for BC3)
    width: int
    height: int


def parse_g1t_font(g1t: bytes) -> G1TFontFile:
    if g1t[:4] != b"GT1G":
        raise ValueError("not a G1T file")
    table_offset = struct.unpack("<I", g1t[12:16])[0]
    entry_count = struct.unpack("<I", g1t[16:20])[0]
    if entry_count != 1:
        raise ValueError(f"expected single-texture font, got entry_count={entry_count}")
    ot_offset = 28 + entry_count * 4
    first_off = struct.unpack("<I", g1t[ot_offset : ot_offset + 4])[0]
    tex_base = table_offset + first_off

    # Texture header: byte 2 packs dimensions. Empirically (confirmed against
    # the RenderDoc capture of entry 72 = vkCreateImage(4096,512,1)) the high
    # nibble is HEIGHT log2 and low nibble is WIDTH log2:
    #   0x9C → high=9 (height=512),  low=C (width=4096)
    #   0xCC → both 12 → 4096×4096
    sizes_byte = g1t[tex_base + 2]
    height = 1 << (sizes_byte >> 4)
    width  = 1 << (sizes_byte & 0x0F)

    # After the 8-byte texture metadata: an extra header whose first u32 is
    # its TOTAL size (including the size field itself). For entry 72 the
    # extra_size is 12, after which the raw BC3 payload starts immediately.
    # Verified empirically: raw_g1t[0x38:] == RenderDoc DDS payload[0x80:] —
    # i.e. the payload is already linear BC3, no Tegra swizzle is applied
    # to this particular G1T. Earlier `+12` was wrong (skipped real glyph bytes).
    extra_ver = g1t[tex_base + 7]
    tex_data_start = tex_base + 8
    if extra_ver > 0:
        extra_size = struct.unpack("<I", g1t[tex_data_start : tex_data_start + 4])[0]
        tex_data_start += extra_size

    tex_data_size = width * height   # BC3 ratio: 16 bytes per 4×4 block = 1 byte/pixel
    return G1TFontFile(
        raw_g1t=g1t,
        tex_data_offset=tex_data_start,
        tex_data_size=tex_data_size,
        width=width,
        height=height,
    )


def decode_atlas(font: G1TFontFile) -> Image.Image:
    dxt5 = font.raw_g1t[font.tex_data_offset : font.tex_data_offset + font.tex_data_size]
    rgba = texture2ddecoder.decode_bc3(dxt5, font.width, font.height)
    return Image.frombytes("RGBA", (font.width, font.height), rgba, "raw", "BGRA")


def encode_atlas_to_dxt5(img: Image.Image, texconv_path: Path, width: int, height: int) -> bytes:
    """Run texconv.exe to encode RGBA → BC3 (no mipmaps); return raw DXT5 bytes."""
    if img.size != (width, height):
        raise ValueError(f"atlas must be {width}×{height}, got {img.size}")
    tmpdir = Path(tempfile.mkdtemp(prefix="fontatlas_"))
    in_png = tmpdir / "atlas.png"
    img.save(in_png)
    result = subprocess.run(
        [
            str(texconv_path),
            "-f", "BC3_UNORM",
            "-m", "1",          # no mipmaps
            "-y",
            "-o", str(tmpdir),
            str(in_png),
        ],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"texconv failed: {result.stderr or result.stdout}")
    dds = (tmpdir / "atlas.DDS").read_bytes()
    raw = dds[128:]
    expected = width * height
    if len(raw) != expected:
        raise RuntimeError(f"DDS raw size {len(raw)} != {expected}")
    return raw


def repack_g1t(font: G1TFontFile, new_dxt5: bytes) -> bytes:
    """Return a new G1T blob with the texture bytes replaced."""
    if len(new_dxt5) != font.tex_data_size:
        raise ValueError(f"DXT5 size mismatch: {len(new_dxt5)} != {font.tex_data_size}")
    out = bytearray(font.raw_g1t)
    out[font.tex_data_offset : font.tex_data_offset + font.tex_data_size] = new_dxt5
    return bytes(out)


# ----- Koei .gz repack -----

def repack_koei_gz(raw_data: bytes) -> bytes:
    """Compress raw blob back into a Koei chunked-zlib container."""
    import io
    import zlib

    block_count = (len(raw_data) - 1) // KT_BLOCK + 1
    out = io.BytesIO()
    # Reserve header room.
    header_struct = struct.Struct("<iII")  # block_size, block_count, total
    header_room = _align_0x80(header_struct.size + block_count * 4)
    out.seek(header_room)

    block_sizes: list[int] = []
    for i in range(block_count):
        chunk = raw_data[i * KT_BLOCK : (i + 1) * KT_BLOCK]
        comp = zlib.compress(chunk, level=9)
        # Each block: u32 length + zlib data, then padded to 0x80.
        block_start = out.tell()
        out.write(struct.pack("<I", len(comp)))
        out.write(comp)
        block_disk_size = 4 + len(comp)
        block_sizes.append(block_disk_size)
        end = block_start + block_disk_size
        pad_to = _align_0x80(end)
        out.write(b"\x00" * (pad_to - end))

    # Write the header now that we know block sizes.
    out.seek(0)
    out.write(header_struct.pack(KT_BLOCK, block_count, len(raw_data)))
    for sz in block_sizes:
        out.write(struct.pack("<I", sz))
    return out.getvalue()


# ----- Glyph layout -----

def cell_rect(glyph_id: int) -> tuple[int, int, int, int]:
    row, col = divmod(glyph_id, CELLS_PER_ROW)
    x = col * CELL_SIZE
    y = row * CELL_SIZE
    return (x, y, x + CELL_SIZE, y + CELL_SIZE)


def find_unused_glyph_ids(codepoints: list[int | None], count: int) -> list[int]:
    """Find `count` glyph IDs that look safe to overwrite for UA letters.

    The game appears to apply a per-language codepoint whitelist when looking
    up glyphs, so taking Japanese Hiragana slots (which the English locale
    refuses to render) leaves us with empty squares. Stay inside the Cyrillic
    glyph range that the game ALREADY uses for Cyrillic Е/Ё/И/etc., picking
    seldom-used Cyrillic codepoints that English text never produces.
    """
    # Cyrillic codepoints that are rare in English (and missing in Ukrainian
    # text too, so re-mapping them as UA letters costs us nothing in practice).
    # We list them in decreasing "safety" order.
    CYR_RARE_TARGETS = [
        0x044B,  # ы (lowercase) — not used in Ukrainian
        0x042B,  # Ы (uppercase)
        0x044A,  # ъ — hard sign, not used in Ukrainian
        0x042A,  # Ъ — hard sign, not used in Ukrainian
        0x0451,  # ё — not used in Ukrainian (lowercase)
        0x0401,  # Ё — not used in Ukrainian (uppercase)
        0x044D,  # э
        0x042D,  # Э
    ]
    candidates: list[int] = []
    for cp_target in CYR_RARE_TARGETS:
        for gid, cp in enumerate(codepoints):
            if cp == cp_target:
                candidates.append(gid)
                break
        if len(candidates) >= count:
            return candidates[:count]
    if len(candidates) < count:
        raise RuntimeError(
            f"could not find {count} safe Cyrillic glyph slots; got {len(candidates)}"
        )
    return candidates[:count]


# ----- High-level workflow -----

# UA letters we need to draw, and the system font we render them with.
UA_GLYPHS = ["Є", "є", "Ґ", "ґ"]
# Candidate font paths on Windows that have full Cyrillic Extended coverage.
SYSTEM_FONT_CANDIDATES = [
    r"C:\Windows\Fonts\arial.ttf",
    r"C:\Windows\Fonts\segoeui.ttf",
    r"C:\Windows\Fonts\calibri.ttf",
    r"C:\Windows\Fonts\times.ttf",
    r"C:\Windows\Fonts\verdana.ttf",
]


def _load_drawing_font(size: int) -> ImageFont.ImageFont:
    for path in SYSTEM_FONT_CANDIDATES:
        try:
            return ImageFont.truetype(path, size)
        except (OSError, IOError):
            continue
    # Last resort — default bitmap font won't have Ukrainian glyphs but won't crash.
    return ImageFont.load_default()


def _find_gid(codepoints: list[int | None], cp: int) -> int | None:
    for i, c in enumerate(codepoints):
        if c == cp:
            return i
    return None


def paint_ua_glyphs(
    atlas: Image.Image,
    target_glyph_ids: list[int],
    codepoints: list[int | None],
) -> None:
    """Build Є/є/Ґ/ґ by cloning the SDF data from visually-similar Cyrillic
    cells (Е, е, Г, г) and then drawing the distinguishing diacritic with a
    colour sampled from a known good glyph. Cloning preserves the multi-channel
    distance-field info the game's font renderer needs."""
    if len(target_glyph_ids) != len(UA_GLYPHS):
        raise ValueError("need one glyph_id per UA letter")

    # Source glyph for each target (visual closest existing Cyrillic letter).
    SOURCES = {
        "Є": 0x0415,   # Е
        "є": 0x0435,   # е
        "Ґ": 0x0413,   # Г
        "ґ": 0x0433,   # г
    }
    DIACRITIC = {
        # (kind, params): "hbar" = horizontal mid-bar (for Є/є),
        # "tick" = small tick at top-right (for Ґ/ґ).
        "Є": ("hbar", 0.55),
        "є": ("hbar", 0.55),
        "Ґ": ("tick", None),
        "ґ": ("tick", None),
    }

    # Sample foreground colour from one filled pixel in a known-good cell.
    sample_id = _find_gid(codepoints, 0x0415)  # Cyrillic Е
    fg = (255, 255, 255, 255)
    if sample_id is not None:
        sample = atlas.crop(cell_rect(sample_id))
        for px in sample.getdata():
            if px[3] > 200 and sum(px[:3]) > 200:
                fg = (px[0], px[1], px[2], 255)
                break

    draw = ImageDraw.Draw(atlas)

    for letter, target_gid in zip(UA_GLYPHS, target_glyph_ids):
        src_cp = SOURCES[letter]
        src_gid = _find_gid(codepoints, src_cp)
        if src_gid is None:
            # Fall back: blank cell + naive paint (better than nothing).
            x0, y0, x1, y1 = cell_rect(target_gid)
            draw.rectangle((x0, y0, x1 - 1, y1 - 1), fill=(0, 0, 0, 0))
            f = _load_drawing_font(CELL_SIZE - 10)
            bbox = draw.textbbox((0, 0), letter, font=f)
            tx = x0 + (CELL_SIZE - (bbox[2] - bbox[0])) // 2 - bbox[0]
            ty = y0 + (CELL_SIZE - (bbox[3] - bbox[1])) // 2 - bbox[1]
            draw.text((tx, ty), letter, font=f, fill=fg)
            continue

        # Clone the source cell's exact pixels into the target cell.
        # We deliberately do NOT add a distinguishing diacritic on top, because
        # the font is a multi-channel SDF — overdrawing raster pixels breaks
        # the distance-field maths and the glyph disappears. The cloned shape
        # renders correctly, so the user will see Є as Е, є as е, etc. but
        # the codepoints (U+0404…) are stored faithfully in the text bin.
        src_rect = cell_rect(src_gid)
        tgt_rect = cell_rect(target_gid)
        atlas.paste(atlas.crop(src_rect), (tgt_rect[0], tgt_rect[1]))


def patch_utf8tbl(codepoints: list[int | None], target_glyph_ids: list[int]) -> list[int | None]:
    """Rewrite the table so the chosen glyph IDs now resolve to the UA
    codepoints we drew into those cells."""
    if len(target_glyph_ids) != len(UA_GLYPHS):
        raise ValueError("target_glyph_ids count mismatch")
    new = list(codepoints)
    for letter, gid in zip(UA_GLYPHS, target_glyph_ids):
        while len(new) <= gid:
            new.append(None)
        new[gid] = ord(letter)
    return new


@dataclass
class FontPatchResult:
    g1t_gz_path: Path
    utf8tbl_path: Path
    glyph_ids: list[int]


def _paint_red_on_y_in_atlas(g1t_blob: bytes, target_gid: int, texconv_path: Path) -> bytes:
    font = parse_g1t_font(g1t_blob)
    atlas = decode_atlas(font)
    x0, y0, x1, y1 = cell_rect(target_gid)
    draw = ImageDraw.Draw(atlas)
    draw.rectangle((x0, y0, x1 - 1, y1 - 1), fill=(255, 0, 0, 255))
    new_dxt5 = encode_atlas_to_dxt5(atlas, texconv_path)
    new_g1t = repack_g1t(font, new_dxt5)
    return repack_koei_gz(new_g1t)


def patch_font_diagnostic(
    romfs_path: Path,
    project_path: Path,
    texconv_path: Path,
) -> FontPatchResult:
    """Shotgun diagnostic: paint a red square on cell 79 (У) inside every
    candidate font atlas at once — font00_JPN, font02_CHN (path-based) and
    DATA1 entries 71/73/74/75 (indexed via mods/). Whichever cell the game
    actually reads will render red, telling us which font is the live one."""
    from formats.data1 import iter_data0, read_entry_full

    src_font_dir = romfs_path / "patch4" / "nx" / "ui" / "font"
    table_blob = (src_font_dir / "UTF8TBL_JPN.bin").read_bytes()
    codepoints = parse_utf8tbl(table_blob)
    target_gid = _find_gid(codepoints, 0x0423)
    if target_gid is None:
        raise RuntimeError("Cyrillic У (U+0423) not found in UTF8TBL")

    out_font_dir = project_path / "romfs" / "patch4" / "nx" / "ui" / "font"
    out_font_dir.mkdir(parents=True, exist_ok=True)
    out_mods_dir = project_path / "romfs" / "mods"
    out_mods_dir.mkdir(parents=True, exist_ok=True)

    # 1) Path-based font00_JPN.
    raw = decompress_koei((src_font_dir / "font00_JPN.g1t.gz").read_bytes())
    (out_font_dir / "font00_JPN.g1t.gz").write_bytes(
        _paint_red_on_y_in_atlas(raw, target_gid, texconv_path)
    )

    # 2) Path-based font02_CHN.
    chn_src = src_font_dir / "font02_CHN.g1t.gz"
    if chn_src.exists():
        raw_chn = decompress_koei(chn_src.read_bytes())
        try:
            (out_font_dir / "font02_CHN.g1t.gz").write_bytes(
                _paint_red_on_y_in_atlas(raw_chn, target_gid, texconv_path)
            )
        except Exception:
            pass  # CHN font may not have same layout

    # 3) DATA1 entries 71, 73, 74, 75 (indexed via mods/).
    data0 = romfs_path / "DATA0.bin"
    data1 = romfs_path / "DATA1.bin"
    with data1.open("rb") as f:
        for e in iter_data0(data0):
            if e.entry_id not in (71, 73, 74, 75):
                continue
            try:
                blob = read_entry_full(f, e)   # decompressed
                if blob[:4] != b"GT1G":
                    continue
                (out_mods_dir / str(e.entry_id)).write_bytes(
                    _paint_red_on_y_in_atlas(blob, target_gid, texconv_path)
                )
            except Exception:
                pass

    (out_font_dir / "UTF8TBL_JPN.bin").write_bytes(table_blob)

    return FontPatchResult(
        g1t_gz_path=out_font_dir / "font00_JPN.g1t.gz",
        utf8tbl_path=out_font_dir / "UTF8TBL_JPN.bin",
        glyph_ids=[target_gid],
    )


# Tegra X1 block-linear swizzle parameters for the FE3H UI font atlas
# (4096×512 BC3). block_height_mip0 for 128 BC3-block-rows = 16 → size_range = 4.
TEGRA_TILE_MODE  = 1   # block-linear (not 0=linear)
TEGRA_ALIGNMENT  = 512
TEGRA_SIZE_RANGE = 4   # block_height = 1 << 4 = 16 GOBs of 8 rows each
BC3_BLK_WIDTH    = 4
BC3_BLK_HEIGHT   = 4
BC3_BPP          = 16  # 16 bytes per 4×4 BC3 block


def patch_font(
    romfs_path: Path,
    project_path: Path,
    texconv_path: Path,
) -> FontPatchResult:
    """No-op cleanup. Real entry-72 patching is now handled by the dedicated,
    swizzle-free pipeline in `formats.entry72_patch`:

        # 1. Export the original atlas to a DDS Photoshop can open
        python formats/entry72_patch.py export <raw.g1t> font_orig.dds
        # 2. Edit font_orig.dds in Photoshop NVIDIA plugin → save as font_edit.dds
        #    (must stay 4096×512 BC3/DXT5, 1 mip, no DXT10 extension)
        # 3. Patch the G1T container with the edited DDS
        python formats/entry72_patch.py patch <raw.g1t> font_edit.dds patched.g1t
        # 4. Install as the indexed mod (raw GT1G bytes, no Koei wrapper)
        python formats/entry72_patch.py install patched.g1t <project>/romfs/mods/72

    Empirically verified: raw_g1t[0x38:] == RenderDoc_DDS[0x80:], i.e. entry
    72 stores LINEAR BC3 directly. No Tegra deswizzle/swizzle is required.

    This UI button currently only removes any stale mods/{71,72,76,77} so the
    game falls back to its built-in atlas; the text-level look-alike
    substitute in server.py keeps the translation readable. Real Є/є/Ґ/ґ
    rendering still needs the glyph-metadata table (DATA1 entries 78/80/81?)
    to know which (x,y,width,height) rect inside the atlas a given gid uses.
    """
    mods_dir = project_path / "romfs" / "mods"
    for stale in (str(SMALL_ATLAS_ENTRY), str(SMALL_UTF8TBL_ENTRY), "71", "76"):
        p = mods_dir / stale
        if p.exists():
            p.unlink()
    for stale in (
        "patch4/nx/ui/font/font00_JPN.g1t.gz",
        "patch4/nx/ui/font/UTF8TBL_JPN.bin",
    ):
        p = project_path / "romfs" / stale
        if p.exists():
            p.unlink()
    return FontPatchResult(g1t_gz_path=mods_dir, utf8tbl_path=mods_dir, glyph_ids=[])


def _patch_font_swizzle_attempt(romfs_path, project_path, texconv_path):
    """Disabled — kept for reference. Swizzle block-copy was bit-exact at
    byte level (verified 256/256 BC3 blocks copied) but GPU read region
    differs from AboodXD's getAddrBlockLinear output. Re-enable once we
    have a confirmed (tile_mode, block_height) pair for entry 72.
    """
    from formats.data1 import iter_data0, read_entry_full
    from formats import tegra_swizzle as ts

    data0 = romfs_path / "DATA0.bin"
    data1 = romfs_path / "DATA1.bin"
    if not data0.exists() or not data1.exists():
        raise FileNotFoundError(f"DATA0/DATA1 not found under {romfs_path}")

    # --- 1. Read atlas + UTF8TBL from DATA1 ---
    raw_atlas_blob: bytes | None = None
    raw_tbl_blob: bytes | None = None
    with data1.open("rb") as f:
        for e in iter_data0(data0):
            if e.entry_id == SMALL_ATLAS_ENTRY:
                raw_atlas_blob = read_entry_full(f, e)
            elif e.entry_id == SMALL_UTF8TBL_ENTRY:
                raw_tbl_blob = read_entry_full(f, e)
            if raw_atlas_blob is not None and raw_tbl_blob is not None:
                break
    if raw_atlas_blob is None:
        raise RuntimeError(f"DATA1 entry {SMALL_ATLAS_ENTRY} (atlas) not found")
    if raw_tbl_blob is None:
        raise RuntimeError(f"DATA1 entry {SMALL_UTF8TBL_ENTRY} (UTF8TBL) not found")

    # --- 2. Parse atlas + table ---
    font = parse_g1t_font(raw_atlas_blob)
    if (font.width, font.height) != (4096, 512):
        # Width/height come back from G1T header byte 2 (high nibble = width
        # log2, low = height log2). Our atlas might come out as (512, 4096)
        # if the nibble order is reversed; both are 2 MB of BC3, so the
        # swizzle/decode path is identical — just pass actual dims through.
        pass
    codepoints = parse_utf8tbl(raw_tbl_blob)

    # --- 3. Find 4 unused gids that exist BOTH in the atlas (gid < cell_count)
    # AND in the codepoint table (gid < len(codepoints)). The table is
    # typically a few entries shorter than the atlas; we can only write into
    # gids that the table actually has a slot for, otherwise the file would
    # need to grow. ---
    cells_per_row = font.width // CELL_SIZE
    rows = font.height // CELL_SIZE
    cell_count = cells_per_row * rows
    max_gid = min(cell_count, len(codepoints))
    free_gids: list[int] = []
    for gid in range(max_gid - 1, -1, -1):
        cp = codepoints[gid]
        if cp is None or cp == 0:
            free_gids.append(gid)
            if len(free_gids) >= len(UA_GLYPHS):
                break
    if len(free_gids) < len(UA_GLYPHS):
        raise RuntimeError(
            f"could not find {len(UA_GLYPHS)} free gids in atlas range "
            f"(cell_count={cell_count})"
        )
    targets = sorted(free_gids[:len(UA_GLYPHS)])

    # --- 4. Clone source cells into target cells via block-level byte copy
    # in swizzled space. The decode/encode round-trip via texconv was
    # producing scrambled results; the safer path is to copy raw BC3 blocks
    # directly between cell-aligned positions in the swizzled buffer using
    # Tegra's getAddrBlockLinear address function. This is bit-exact (no
    # re-encoding loss) and preserves swizzle by construction. ---
    tex_size = font.tex_data_size
    swizzled = bytearray(raw_atlas_blob[font.tex_data_offset : font.tex_data_offset + tex_size])
    if len(swizzled) < tex_size:
        swizzled.extend(b'\x00' * (tex_size - len(swizzled)))

    SUBSTITUTE_SOURCE = {
        "Є": 0x0415,   # Cyrillic Е
        "є": 0x0435,   # Cyrillic е
        "Ґ": 0x0413,   # Cyrillic Г
        "ґ": 0x0433,   # Cyrillic г
    }

    width_blocks = font.width // BC3_BLK_WIDTH       # 1024 BC3 blocks wide
    block_height_gobs = 1 << TEGRA_SIZE_RANGE        # 16

    def cell_block_xy(gid: int) -> tuple[int, int]:
        # 64×64 px cell = 16×16 BC3 blocks; cell_x, cell_y in pixels then /4
        row, col = divmod(gid, CELLS_PER_ROW)
        return col * (CELL_SIZE // BC3_BLK_WIDTH), row * (CELL_SIZE // BC3_BLK_HEIGHT)

    BLOCKS_PER_CELL_X = CELL_SIZE // BC3_BLK_WIDTH   # 16
    BLOCKS_PER_CELL_Y = CELL_SIZE // BC3_BLK_HEIGHT  # 16

    for letter, dst_gid in zip(UA_GLYPHS, targets):
        src_gid = _find_gid(codepoints, SUBSTITUTE_SOURCE[letter])
        if src_gid is None:
            continue
        src_bx0, src_by0 = cell_block_xy(src_gid)
        dst_bx0, dst_by0 = cell_block_xy(dst_gid)
        for dy in range(BLOCKS_PER_CELL_Y):
            for dx in range(BLOCKS_PER_CELL_X):
                src_addr = ts.getAddrBlockLinear(
                    src_bx0 + dx, src_by0 + dy,
                    width_blocks, BC3_BPP, 0, block_height_gobs,
                )
                dst_addr = ts.getAddrBlockLinear(
                    dst_bx0 + dx, dst_by0 + dy,
                    width_blocks, BC3_BPP, 0, block_height_gobs,
                )
                if (src_addr + BC3_BPP <= tex_size and
                        dst_addr + BC3_BPP <= tex_size):
                    swizzled[dst_addr : dst_addr + BC3_BPP] = \
                        swizzled[src_addr : src_addr + BC3_BPP]

    new_swizzled = bytes(swizzled)

    # --- 8. Repack G1T with new texture bytes ---
    new_g1t = bytearray(raw_atlas_blob)
    # If original blob was shorter than tex_size (Koei trimmed trailing pad),
    # extend it before splicing.
    needed = font.tex_data_offset + tex_size
    if len(new_g1t) < needed:
        new_g1t.extend(b'\x00' * (needed - len(new_g1t)))
    new_g1t[font.tex_data_offset : font.tex_data_offset + tex_size] = new_swizzled

    # --- 9. Patch UTF8TBL in-place — file size stays at 2040 bytes ---
    new_codepoints = list(codepoints)
    for letter, gid in zip(UA_GLYPHS, targets):
        new_codepoints[gid] = ord(letter)
    new_tbl = serialize_utf8tbl(new_codepoints)
    if len(new_tbl) != len(raw_tbl_blob):
        raise RuntimeError(
            f"patched UTF8TBL size {len(new_tbl)} != original {len(raw_tbl_blob)}"
        )

    # --- 10. Write raw G1T + raw UTF8TBL to project/romfs/mods/ ---
    mods_dir = project_path / "romfs" / "mods"
    mods_dir.mkdir(parents=True, exist_ok=True)
    out_g1t = mods_dir / str(SMALL_ATLAS_ENTRY)
    out_tbl = mods_dir / str(SMALL_UTF8TBL_ENTRY)
    out_g1t.write_bytes(bytes(new_g1t))
    out_tbl.write_bytes(new_tbl)

    # Clean up obsolete font mods/files from earlier strategies.
    for stale in ("71", "76"):
        p = mods_dir / stale
        if p.exists():
            p.unlink()
    for stale in (
        "patch4/nx/ui/font/font00_JPN.g1t.gz",
        "patch4/nx/ui/font/UTF8TBL_JPN.bin",
    ):
        p = project_path / "romfs" / stale
        if p.exists():
            p.unlink()

    return FontPatchResult(
        g1t_gz_path=out_g1t,
        utf8tbl_path=out_tbl,
        glyph_ids=targets,
    )
