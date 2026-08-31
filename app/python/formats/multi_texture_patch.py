"""Multi-texture G1T patcher (title screen entry 6039, abbey map entry 6063 etc.).

Background: some G1T atlases in DATA1 contain MULTIPLE sub-textures (4 in the
case of entry 6039: 2× 2048×2048 BC3 + 2× 1024×1024 BC3, all 1 mip).

Pipeline mirrors entry72_patch but iterates over each sub-texture:
  1. Parse the G1T container → list of (tex_index, offset, width, height, format, payload_offset, payload_size)
  2. Export each sub-texture as standalone DDS (RenderDoc-compatible) for editing
  3. After user edits some/all DDS → splice payloads back at exact offsets, preserving header & metadata bytes byte-for-byte
  4. Write mods/<entry_id> as raw G1T (no Koei wrapper) so INFO0 routing works
     with is_compressed=False

Game expects:
- G1T magic 'GT1G' at offset 0
- Sub-texture offset table after the standard header
- Per-texture metadata (8 bytes + extra header) preserved byte-for-byte
- Texture payloads in the SAME ORDER and SAME SIZE as original (since dimensions
  are unchanged)
"""
from __future__ import annotations
import os
import struct
from dataclasses import dataclass
from pathlib import Path


@dataclass
class SubTexture:
    index: int
    width: int
    height: int
    format_byte: int        # 0x5B = BC3_UNORM for FE3H title atlas
    mip_count: int          # low nibble of byte 0
    metadata_offset: int    # absolute offset of texture metadata header (8 B + extra)
    payload_offset: int     # absolute offset where raw BC3 starts
    payload_size: int       # raw BC3 byte length (width*height for 1-mip BC3)


def parse_g1t_multi(blob: bytes) -> list[SubTexture]:
    """Return one SubTexture per sub-texture in the G1T container."""
    if blob[:4] != b"GT1G":
        raise ValueError(f"not a G1T (magic={blob[:4]!r})")
    table_off = struct.unpack("<I", blob[12:16])[0]
    entry_count = struct.unpack("<I", blob[16:20])[0]
    if entry_count < 1 or entry_count > 32:
        raise ValueError(f"implausible texture count {entry_count}")
    offset_table_start = 28 + entry_count * 4

    # Per-texture offset values
    tex_offs = []
    for i in range(entry_count):
        off = struct.unpack("<I", blob[offset_table_start + i*4 : offset_table_start + (i+1)*4])[0]
        tex_offs.append(off)

    subs: list[SubTexture] = []
    for i in range(entry_count):
        meta_off = table_off + tex_offs[i]
        mip_byte = blob[meta_off]
        format_byte = blob[meta_off + 1]
        size_byte = blob[meta_off + 2]
        width  = 1 << (size_byte & 0x0F)
        height = 1 << (size_byte >> 4)
        extra_ver = blob[meta_off + 7]
        payload_off = meta_off + 8
        if extra_ver > 0:
            extra_size = struct.unpack("<I", blob[payload_off : payload_off + 4])[0]
            payload_off += extra_size

        # Determine where this sub-texture's payload ends
        if i + 1 < entry_count:
            payload_end = table_off + tex_offs[i + 1]
        else:
            payload_end = len(blob)
        payload_size = payload_end - payload_off

        subs.append(SubTexture(
            index=i,
            width=width,
            height=height,
            format_byte=format_byte,
            mip_count=(mip_byte & 0x0F),
            metadata_offset=meta_off,
            payload_offset=payload_off,
            payload_size=payload_size,
        ))
    return subs


# ----- DDS helpers -----

DDS_HEADER_SIZE = 128


def make_dds_header(width: int, height: int, fmt_fourcc: bytes = b"DXT5") -> bytes:
    DDS_MAGIC = b"DDS "
    DDSD_CAPS = 0x1; DDSD_HEIGHT = 0x2; DDSD_WIDTH = 0x4
    DDSD_PIXELFORMAT = 0x1000; DDSD_MIPMAPCOUNT = 0x20000
    DDSD_LINEARSIZE = 0x80000
    DDPF_FOURCC = 0x4
    DDSCAPS_TEXTURE = 0x1000
    flags = DDSD_CAPS | DDSD_HEIGHT | DDSD_WIDTH | DDSD_PIXELFORMAT | DDSD_MIPMAPCOUNT | DDSD_LINEARSIZE
    linear_size = max(1, (width + 3) // 4) * max(1, (height + 3) // 4) * 16
    header = (
        struct.pack("<4sI", DDS_MAGIC, 124)
        + struct.pack("<IIIIIII", flags, height, width, linear_size, 0, 1, 0)
        + b"\x00" * (11 * 4)
        + struct.pack("<II4sIIIII", 32, DDPF_FOURCC, fmt_fourcc, 0, 0, 0, 0, 0)
        + struct.pack("<IIIII", DDSCAPS_TEXTURE, 0, 0, 0, 0)
    )
    assert len(header) == DDS_HEADER_SIZE, f"header size {len(header)} != {DDS_HEADER_SIZE}"
    return header


# ----- API -----

def export_subtextures(raw_g1t_path: Path, out_dir: Path) -> list[Path]:
    """Export each sub-texture inside raw_g1t as a standalone DDS file for
    editing in Photoshop / GIMP. Returns the list of DDS paths.

    Naming: <out_dir>/tex_0.dds, tex_1.dds, ...
    Game format byte 0x5B = BC3_UNORM (DXT5); we wrap with standard DDS header.
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    blob = Path(raw_g1t_path).read_bytes()
    subs = parse_g1t_multi(blob)
    out_paths: list[Path] = []
    for s in subs:
        if s.format_byte != 0x5B:
            raise NotImplementedError(
                f"sub-texture {s.index} format 0x{s.format_byte:02X} not supported "
                f"(only BC3/DXT5 = 0x5B for now)"
            )
        payload = blob[s.payload_offset : s.payload_offset + s.payload_size]
        if len(payload) != s.width * s.height:
            raise ValueError(
                f"sub-texture {s.index} payload size {len(payload):,} != expected "
                f"{s.width*s.height:,} for {s.width}x{s.height} BC3 (mip_count={s.mip_count})"
            )
        dds = make_dds_header(s.width, s.height) + payload
        out_path = out_dir / f"tex_{s.index}.dds"
        out_path.write_bytes(dds)
        out_paths.append(out_path)
    return out_paths


def patch_with_edited_ddses(raw_g1t_path: Path,
                            edited_dds_dir: Path,
                            out_g1t_path: Path) -> Path:
    """Splice edited DDS payloads back into the original G1T container.

    Only DDS files that EXIST in `edited_dds_dir` are spliced; missing ones
    fall back to the original payload (so user can edit just one sub-texture
    and leave the others intact).

    Strict size invariants: each edited DDS must be exactly DDS_HEADER_SIZE +
    width*height bytes (no mipmap chain, no DX10 extension).
    """
    blob = Path(raw_g1t_path).read_bytes()
    subs = parse_g1t_multi(blob)
    out = bytearray(blob)
    edited_count = 0
    for s in subs:
        cand = Path(edited_dds_dir) / f"tex_{s.index}.dds"
        if not cand.exists():
            continue
        dds = cand.read_bytes()
        if dds[:4] != b"DDS ":
            raise ValueError(f"{cand}: not a DDS")
        # Reject DXT10 header to keep the splice math simple
        if dds[84:88] == b"DX10":
            raise ValueError(
                f"{cand}: uses DXT10 extension (148-byte header); resave with "
                f"a legacy DDS_HEADER (128 bytes) — disable 'Use DXT10 Header' "
                f"in the NVIDIA Texture Tools Exporter"
            )
        expected_size = DDS_HEADER_SIZE + s.width * s.height
        if len(dds) != expected_size:
            raise ValueError(
                f"{cand}: size {len(dds):,} != expected {expected_size:,} "
                f"({s.width}×{s.height} BC3, 1 mip, 128-byte DDS header)"
            )
        new_payload = dds[DDS_HEADER_SIZE:]
        out[s.payload_offset : s.payload_offset + s.payload_size] = new_payload
        edited_count += 1
    if edited_count == 0:
        raise RuntimeError(
            f"no edited DDS files found in {edited_dds_dir} "
            f"(expected tex_0.dds, tex_1.dds, ...)"
        )
    # Post-conditions
    assert out[:4] == b"GT1G", "magic must remain GT1G"
    assert len(out) == len(blob), "splice changed file size — invariant broken"
    Path(out_g1t_path).write_bytes(bytes(out))
    return Path(out_g1t_path)


def safe_write_mod(patched_g1t_path: Path,
                   mods_path: Path) -> Path:
    """Write the patched G1T as raw bytes to mods/<id>. Always raw; INFO0
    patcher marks is_compressed=False and recomputes decomp_size."""
    data = Path(patched_g1t_path).read_bytes()
    if data[:4] != b"GT1G":
        raise ValueError(f"{patched_g1t_path}: not a G1T")
    out = Path(mods_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(data)
    written = out.read_bytes()
    assert written[:4] == b"GT1G", "mods file must begin with GT1G"
    return out


# ----- self test -----

def _self_test_roundtrip():
    """Round-trip test: export all sub-textures then re-splice them as-is →
    must produce a file byte-identical to the original raw G1T."""
    import tempfile
    src_g1t = Path(os.environ.get("FE3H_SELFTEST_G1T", "entry72_raw.g1t"))
    if not src_g1t.exists():
        print(f"skip — no source G1T at {src_g1t}")
        return
    with tempfile.TemporaryDirectory() as td:
        td_path = Path(td)
        export_subtextures(src_g1t, td_path)
        out_g1t = td_path / "round_trip.g1t"
        patch_with_edited_ddses(src_g1t, td_path, out_g1t)
        if out_g1t.read_bytes() == src_g1t.read_bytes():
            print("round-trip OK")
        else:
            print("round-trip MISMATCH — splice not lossless")


if __name__ == "__main__":
    _self_test_roundtrip()
