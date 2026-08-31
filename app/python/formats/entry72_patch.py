"""Safe DATA1 entry 72 (UI font atlas) patching pipeline.

Key fact established empirically:

    entry72_raw.g1t[0x38:] == font.dds[0x80:]

i.e. the G1T container at DATA1 entry 72 stores its BC3 payload as
LINEAR (already-deswizzled) BC3 bytes, identical to what RenderDoc
exports as a DXT5 DDS from GPU memory.

Consequences:
  * Tegra block-linear swizzle/deswizzle is NOT required for this texture.
  * Do not call AboodXD `getAddrBlockLinear` / deswizzle / swizzle.
  * Simply splice DDS BC3 payload into the G1T container after offset 0x38.

Earlier bug we are guarding against: a previous parser used offset 0x44
instead of 0x38 (an erroneous `+12` after extra header), shrinking the
payload by 12 bytes (2,097,140 instead of 2,097,152) and corrupting the
first 12 bytes of BC3 blocks. We refuse any payload shorter than expected.

INFO0 also flags entry 72 as `is_compressed=False`, so mods/72 must be
RAW G1T (magic "GT1G" at offset 0). NEVER wrap in a Koei .gz container.
"""
from __future__ import annotations
import os
from pathlib import Path


# ----- constants tied to the FE3H entry 72 atlas (4096×512 BC3) -----

G1T_HEADER_SIZE   = 0x38         # 56 bytes: GT1G + version + sizes + texture metadata + 12-B extra
DDS_HEADER_SIZE   = 0x80         # 128 bytes: standard DDS_HEADER (no DXT10 extension)
BC3_PAYLOAD_SIZE  = 4096 * 512   # 1 byte/pixel for BC3 → 2,097,152 bytes
RAW_G1T_SIZE      = G1T_HEADER_SIZE + BC3_PAYLOAD_SIZE   # 2,097,208
RAW_DDS_SIZE      = DDS_HEADER_SIZE + BC3_PAYLOAD_SIZE   # 2,097,280


# ----- 1. validation -----

def validate_entry72(raw_g1t_path: Path, renderdoc_dds_path: Path) -> None:
    """Verify that the original G1T payload byte-matches the RenderDoc DDS,
    confirming that no swizzle is applied to this texture."""
    raw = Path(raw_g1t_path).read_bytes()
    dds = Path(renderdoc_dds_path).read_bytes()

    if raw[:4] != b"GT1G":
        raise ValueError(f"{raw_g1t_path}: not a G1T file (magic={raw[:4]!r})")
    if dds[:4] != b"DDS ":
        raise ValueError(f"{renderdoc_dds_path}: not a DDS file (magic={dds[:4]!r})")
    if len(raw) != RAW_G1T_SIZE:
        raise ValueError(f"{raw_g1t_path}: size {len(raw):,} != expected {RAW_G1T_SIZE:,}")
    if len(dds) != RAW_DDS_SIZE:
        raise ValueError(f"{renderdoc_dds_path}: size {len(dds):,} != expected {RAW_DDS_SIZE:,}")

    raw_payload = raw[G1T_HEADER_SIZE:]
    dds_payload = dds[DDS_HEADER_SIZE:]
    if len(raw_payload) != BC3_PAYLOAD_SIZE:
        raise ValueError(f"raw G1T payload size {len(raw_payload):,} != {BC3_PAYLOAD_SIZE:,}")
    if len(dds_payload) != BC3_PAYLOAD_SIZE:
        raise ValueError(f"DDS payload size {len(dds_payload):,} != {BC3_PAYLOAD_SIZE:,}")
    if raw_payload != dds_payload:
        # Find first diff for diagnostics
        for i, (a, b) in enumerate(zip(raw_payload, dds_payload)):
            if a != b:
                raise ValueError(
                    f"payloads differ at byte {i}: raw=0x{a:02X} dds=0x{b:02X} — "
                    f"this texture IS swizzled and needs deswizzle (not implemented)."
                )
        # Length matched but bytes not equal (impossible after above checks) — defensive
        raise ValueError("payloads differ in unknown way")
    print("OK: entry72 payload is linear BC3 and matches RenderDoc DDS. No swizzle needed.")


# ----- 2. extract -----

def extract_entry72_payload(raw_g1t_path: Path, out_bc3_path: Path) -> Path:
    """Slice the BC3 payload out of the raw G1T container at the CORRECT
    offset 0x38. Forbids the buggy 0x44 offset by checking the resulting size."""
    raw = Path(raw_g1t_path).read_bytes()
    if raw[:4] != b"GT1G":
        raise ValueError(f"{raw_g1t_path}: not a G1T")
    payload = raw[G1T_HEADER_SIZE:]
    if len(payload) != BC3_PAYLOAD_SIZE:
        raise ValueError(
            f"refusing to write payload of {len(payload):,} bytes (expected {BC3_PAYLOAD_SIZE:,}). "
            f"Did you slice from offset 0x44 instead of 0x{G1T_HEADER_SIZE:X}?"
        )
    Path(out_bc3_path).write_bytes(payload)
    return Path(out_bc3_path)


# ----- 3. patch G1T with an edited DDS -----

def patch_entry72_with_dds(raw_g1t_path: Path,
                            edited_dds_path: Path,
                            out_g1t_path: Path) -> Path:
    """Replace the BC3 payload inside the original G1T container with the
    BC3 payload from an edited DDS. The G1T header (bytes [0:0x38])
    stays bit-identical to the original. Output size equals input size."""
    raw = Path(raw_g1t_path).read_bytes()
    dds = Path(edited_dds_path).read_bytes()

    if raw[:4] != b"GT1G":
        raise ValueError(f"{raw_g1t_path}: not a G1T")
    if dds[:4] != b"DDS ":
        raise ValueError(f"{edited_dds_path}: not a DDS")
    if len(raw) != RAW_G1T_SIZE:
        raise ValueError(f"{raw_g1t_path}: unexpected size {len(raw):,}")
    new_payload = dds[DDS_HEADER_SIZE:]
    if len(new_payload) != BC3_PAYLOAD_SIZE:
        raise ValueError(
            f"DDS payload size {len(new_payload):,} != {BC3_PAYLOAD_SIZE:,}. "
            f"DDS must be 4096×512 BC3/DXT5, 1 mip, no DXT10 extension."
        )

    header = raw[:G1T_HEADER_SIZE]
    patched = header + new_payload

    # Post-conditions
    assert len(patched) == len(raw), "size invariant violated"
    assert patched[:4] == b"GT1G", "magic invariant violated"
    assert patched[G1T_HEADER_SIZE:] == new_payload, "splice integrity violated"

    Path(out_g1t_path).write_bytes(patched)
    return Path(out_g1t_path)


# ----- 4. safely write the indexed mod file -----

def safe_write_mod_72(patched_g1t_path: Path,
                      mods_72_path: Path,
                      info0_is_compressed: bool) -> Path:
    """Write the patched G1T to <mods>/72 according to the INFO0 flag.

    For our case info0_is_compressed is False → write RAW G1T bytes. We
    explicitly REFUSE to ever wrap with a Koei .gz container in this path,
    because that would mean the first bytes of mods/72 are NOT 'GT1G' and
    the game would fail to parse the texture container."""
    patched = Path(patched_g1t_path).read_bytes()
    if patched[:4] != b"GT1G":
        raise ValueError(f"{patched_g1t_path}: not a G1T (magic={patched[:4]!r})")

    if info0_is_compressed:
        raise NotImplementedError(
            "info0_is_compressed=True path not used for entry 72. If you ever need it, "
            "add the Koei wrapper here — but DO NOT call this function in that case."
        )

    # Uncompressed indexed mod: raw G1T bytes.
    out = Path(mods_72_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(patched)

    written = out.read_bytes()
    assert written[:4] == b"GT1G", "mods/72 must begin with 'GT1G' for uncompressed entry"
    assert b"\x01\x00\x00\x00" != written[:4], "Koei wrapper detected — forbidden"
    return out


# ----- 5. diagnostic for the legacy bad payload -----

def reject_bad_payload(payload_path: Path) -> None:
    """Refuse to use a payload extracted at the wrong offset (size 2,097,140).
    Such files come from the legacy bug that sliced at 0x44 instead of 0x38."""
    sz = Path(payload_path).stat().st_size
    if sz == 2_097_140:
        raise ValueError(
            f"{payload_path}: wrong payload size 2,097,140 — likely extracted from "
            f"offset 0x44 instead of 0x38. Expected {BC3_PAYLOAD_SIZE:,} bytes. "
            f"Do NOT deswizzle this; re-extract from raw G1T at 0x38."
        )
    if sz != BC3_PAYLOAD_SIZE:
        raise ValueError(
            f"{payload_path}: payload size {sz:,} != expected {BC3_PAYLOAD_SIZE:,}"
        )


# ----- 6. (intentional) no swizzle code here -----
# entry72_raw.g1t stores linear BC3 payload. RenderDoc DDS payload matches raw
# G1T payload 1:1. Swizzle is not required for this texture. Do not import
# tegra_swizzle.deswizzle/swizzle/getAddrBlockLinear in this module.


# ----- 7. self-test -----

def _self_test_a_original_validation():
    """Test A: raw G1T payload matches the original RenderDoc DDS payload."""
    raw = Path(os.environ.get("FE3H_SELFTEST_G1T", "entry72_raw.g1t"))
    dds = Path(os.environ.get("FE3H_SELFTEST_DDS", "font.dds"))
    validate_entry72(raw, dds)


def _self_test_b_noop_patch():
    """Test B: patching with the ORIGINAL DDS yields a file byte-identical to raw G1T."""
    import os, tempfile
    raw_path = Path(os.environ.get("FE3H_SELFTEST_G1T", "entry72_raw.g1t"))
    dds_path = Path(os.environ.get("FE3H_SELFTEST_DDS", "font.dds"))
    fd, tmp = tempfile.mkstemp(suffix=".g1t")
    os.close(fd)
    out_path = Path(tmp)
    try:
        patch_entry72_with_dds(raw_path, dds_path, out_path)
        assert out_path.read_bytes() == raw_path.read_bytes(), \
            "no-op patch with original DDS must be byte-identical to raw G1T"
        print("Test B passed: no-op patch is byte-identical to original.")
    finally:
        try:
            out_path.unlink()
        except OSError:
            pass


def export_for_editing(raw_g1t_path: Path, out_dds_path: Path) -> Path:
    """Wrap the raw G1T BC3 payload into a standard DDS so it can be opened
    by Photoshop NVIDIA plugin / GIMP / paint.NET for visual editing.
    Output is bit-identical to the RenderDoc DDS export."""
    import struct
    raw = Path(raw_g1t_path).read_bytes()
    if raw[:4] != b"GT1G":
        raise ValueError(f"{raw_g1t_path}: not a G1T")
    payload = raw[G1T_HEADER_SIZE:]
    if len(payload) != BC3_PAYLOAD_SIZE:
        raise ValueError(f"payload size {len(payload):,} != {BC3_PAYLOAD_SIZE:,}")

    # Build a standard 128-byte DDS_HEADER for 4096×512 BC3 (DXT5), 1 mip.
    DDS_MAGIC = b"DDS "
    DDSD_CAPS = 0x1; DDSD_HEIGHT = 0x2; DDSD_WIDTH = 0x4
    DDSD_PIXELFORMAT = 0x1000; DDSD_MIPMAPCOUNT = 0x20000
    DDSD_LINEARSIZE = 0x80000
    DDPF_FOURCC = 0x4
    DDSCAPS_TEXTURE = 0x1000

    flags = DDSD_CAPS | DDSD_HEIGHT | DDSD_WIDTH | DDSD_PIXELFORMAT | DDSD_MIPMAPCOUNT | DDSD_LINEARSIZE
    width, height = 4096, 512
    linear_size = max(1, (width + 3) // 4) * max(1, (height + 3) // 4) * 16
    header = struct.pack("<4sI",
        DDS_MAGIC, 124,
    ) + struct.pack("<IIIIIII",
        flags, height, width, linear_size, 0, 1, 0,
    ) + b"\x00" * (11 * 4) + struct.pack("<II4sIIIII",
        32, DDPF_FOURCC, b"DXT5", 0, 0, 0, 0, 0,
    ) + struct.pack("<IIIII",
        DDSCAPS_TEXTURE, 0, 0, 0, 0,
    )
    assert len(header) == DDS_HEADER_SIZE, f"DDS header should be {DDS_HEADER_SIZE} bytes, got {len(header)}"
    Path(out_dds_path).write_bytes(header + payload)
    return Path(out_dds_path)


def make_diagnostic_patched_g1t(raw_g1t_path: Path,
                                 original_dds_path: Path,
                                 out_g1t_path: Path,
                                 mark_rect_px: tuple[int, int, int, int] = (32, 32, 96, 96),
                                 ) -> Path:
    """Test C helper: produce a patched G1T where one small rectangle of the
    atlas is replaced with bright-red pixels. Re-encodes only the affected
    BC3 blocks (4×4-aligned), leaving everything else byte-identical.
    Deploy this to mods/72 and check in-game: if the corresponding atlas
    region shows red garbage where empty space was, the mod path is live
    AND linear BC3 is the right format. mark_rect is (x0, y0, x1, y1) px,
    auto-rounded to 4-pixel boundaries."""
    import struct
    raw = Path(raw_g1t_path).read_bytes()
    dds = Path(original_dds_path).read_bytes()
    if raw[:4] != b"GT1G" or dds[:4] != b"DDS ":
        raise ValueError("bad magics")

    # Round rect to 4-px boundaries (BC3 block alignment).
    x0, y0, x1, y1 = mark_rect_px
    x0 -= x0 % 4; y0 -= y0 % 4
    x1 += (-x1) % 4; y1 += (-y1) % 4
    bx0, by0 = x0 // 4, y0 // 4
    bx1, by1 = x1 // 4, y1 // 4
    print(f"  marking BC3 blocks  x=[{bx0},{bx1})  y=[{by0},{by1})")

    # A bright-red BC3 block: alpha=opaque, colour=red.
    # BC3 layout: 8B alpha (a0=0xFF,a1=0xFF, indices=all 0) + 8B color (DXT1).
    # DXT1 red: c0=0xF800 (R=31,G=0,B=0), c1=0xF800, indices = all 0.
    red_block = bytes([
        0xFF, 0xFF,              # a0=255, a1=255 (fully opaque)
        0x00, 0x00, 0x00, 0x00, 0x00, 0x00,   # alpha indices = all 0
        0x00, 0xF8, 0x00, 0xF8,  # c0=0xF800, c1=0xF800 (565: red, green=0, blue=0)
        0x00, 0x00, 0x00, 0x00,  # color indices = all 0
    ])
    assert len(red_block) == 16

    width_blocks = 4096 // 4   # 1024
    new_payload = bytearray(dds[DDS_HEADER_SIZE:])
    for by in range(by0, by1):
        for bx in range(bx0, bx1):
            off = (by * width_blocks + bx) * 16
            new_payload[off:off+16] = red_block

    # Build patched G1T
    patched = raw[:G1T_HEADER_SIZE] + bytes(new_payload)
    assert len(patched) == len(raw), "size invariant"
    assert patched[:4] == b"GT1G", "magic invariant"
    Path(out_g1t_path).write_bytes(patched)
    return Path(out_g1t_path)


def _self_test_c_diagnostic_patch():
    """Test C: paint a 64×64 px red square at the top-left corner of the
    atlas. Install as mods/72 in the project. Deploy + run game manually."""
    raw = Path(os.environ.get("FE3H_SELFTEST_G1T", "entry72_raw.g1t"))
    dds = Path(os.environ.get("FE3H_SELFTEST_DDS", "font.dds"))
    project_mods = Path(os.environ.get("FE3H_SELFTEST_MODS", "mods"))
    project_mods.mkdir(parents=True, exist_ok=True)
    out = project_mods / "72"

    # Pick a rectangle that is empty in the original atlas — somewhere in the
    # very bottom strip below the last printable row. The user can change it.
    make_diagnostic_patched_g1t(raw, dds, out, mark_rect_px=(0, 480, 256, 512))
    # Verify post-write invariants
    written = out.read_bytes()
    assert written[:4] == b"GT1G", "mods/72 must be raw G1T"
    assert len(written) == 2_097_208, f"unexpected size {len(written)}"
    print(f"Test C ready: wrote diagnostic mods/72 ({len(written):,} bytes). "
          f"Red rectangle at px (0,480)-(256,512). Build+Deploy to Eden and look "
          f"for a red blob at the very bottom of the font atlas in RenderDoc, "
          f"OR a red garbage glyph somewhere in-game.")


if __name__ == "__main__":
    import sys
    if len(sys.argv) <= 1:
        _self_test_a_original_validation()
        _self_test_b_noop_patch()
        sys.exit(0)
    cmd = sys.argv[1]
    if cmd == "export":
        # python entry72_patch.py export <raw.g1t> <out.dds>
        export_for_editing(Path(sys.argv[2]), Path(sys.argv[3]))
        print(f"Exported {sys.argv[3]} — open in Photoshop NVIDIA DDS plugin.")
    elif cmd == "patch":
        # python entry72_patch.py patch <raw.g1t> <edited.dds> <out.g1t>
        patch_entry72_with_dds(Path(sys.argv[2]), Path(sys.argv[3]), Path(sys.argv[4]))
        print(f"Wrote patched G1T to {sys.argv[4]}")
    elif cmd == "install":
        # python entry72_patch.py install <patched.g1t> <project>/romfs/mods/72
        safe_write_mod_72(Path(sys.argv[2]), Path(sys.argv[3]), info0_is_compressed=False)
        print(f"Installed mods/72 at {sys.argv[3]}")
    elif cmd == "validate":
        # python entry72_patch.py validate <raw.g1t> <orig.dds>
        validate_entry72(Path(sys.argv[2]), Path(sys.argv[3]))
    elif cmd == "test-c":
        # python entry72_patch.py test-c
        _self_test_c_diagnostic_patch()
    else:
        print(f"Unknown command: {cmd}")
        print("Commands: (no args = run tests A+B), export, patch, install, validate")
        sys.exit(2)
