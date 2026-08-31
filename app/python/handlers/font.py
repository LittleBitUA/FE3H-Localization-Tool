"""Font-atlas and multi-texture G1T RPC handlers."""
from __future__ import annotations
from pathlib import Path


def handle_patch_font(params):
    """STEP 1: Export the original font atlas (DATA1 entry 72) as a standard
    DDS that Photoshop / GIMP can open. User edits it (mirror Э→Є cells,
    etc.) and saves over the same file. Then user clicks 'Apply font edit'
    which calls apply_font_edit below."""
    import subprocess
    from formats.data1 import iter_data0, read_entry_full
    from formats.entry72_patch import (G1T_HEADER_SIZE, BC3_PAYLOAD_SIZE,
                                        DDS_HEADER_SIZE)
    import struct

    project_path = Path(params["project_path"]).resolve()
    romfs_path = Path(params["romfs_path"]).resolve()

    # Extract entry 72 from DATA1 (raw G1T payload)
    data0 = romfs_path / "DATA0.bin"
    data1 = romfs_path / "DATA1.bin"
    raw_g1t = None
    with data1.open("rb") as f:
        for e in iter_data0(data0):
            if e.entry_id == 72:
                raw_g1t = read_entry_full(f, e)
                break
    if raw_g1t is None:
        raise RuntimeError("DATA1 entry 72 (font atlas) not found")
    if raw_g1t[:4] != b"GT1G":
        raise RuntimeError(f"unexpected magic {raw_g1t[:4]!r}")

    payload = raw_g1t[G1T_HEADER_SIZE:G1T_HEADER_SIZE + BC3_PAYLOAD_SIZE]
    if len(payload) != BC3_PAYLOAD_SIZE:
        raise RuntimeError(
            f"payload size {len(payload):,} != {BC3_PAYLOAD_SIZE:,}"
        )

    # Save raw G1T (so we can splice on apply) + edit-ready DDS
    out_dir = project_path / "font_edit"
    out_dir.mkdir(parents=True, exist_ok=True)
    raw_g1t_path = out_dir / "entry72_raw.g1t"
    raw_g1t_path.write_bytes(raw_g1t)

    # Build standard 128-byte DDS_HEADER for 4096×512 BC3 (DXT5, 1 mip)
    DDS_MAGIC = b"DDS "
    DDSD_CAPS = 0x1; DDSD_HEIGHT = 0x2; DDSD_WIDTH = 0x4
    DDSD_PIXELFORMAT = 0x1000; DDSD_MIPMAPCOUNT = 0x20000
    DDSD_LINEARSIZE = 0x80000
    DDPF_FOURCC = 0x4
    DDSCAPS_TEXTURE = 0x1000
    flags = DDSD_CAPS | DDSD_HEIGHT | DDSD_WIDTH | DDSD_PIXELFORMAT | DDSD_MIPMAPCOUNT | DDSD_LINEARSIZE
    width, height = 4096, 512
    linear_size = max(1, (width + 3) // 4) * max(1, (height + 3) // 4) * 16
    # DDS_HEADER (124 bytes) layout:
    #   size(4) flags(4) height(4) width(4) pitch(4) depth(4) mipMapCount(4)
    #   reserved1[11] (44) + DDS_PIXELFORMAT(32) + caps[4](16) + reserved2(4)
    header = struct.pack("<4sI", DDS_MAGIC, 124) + \
             struct.pack("<IIIIII", flags, height, width, linear_size, 0, 1) + \
             b"\x00" * (11 * 4) + \
             struct.pack("<II4sIIIII", 32, DDPF_FOURCC, b"DXT5", 0, 0, 0, 0, 0) + \
             struct.pack("<IIIII", DDSCAPS_TEXTURE, 0, 0, 0, 0)
    if len(header) != DDS_HEADER_SIZE:
        raise RuntimeError(f"DDS header size {len(header)} != {DDS_HEADER_SIZE}")

    edit_dds = out_dir / "font_edit.dds"
    edit_dds.write_bytes(header + payload)

    # Open Explorer with the file highlighted (user-friendly hand-off)
    try:
        subprocess.Popen(["explorer", "/select,", str(edit_dds)])
    except Exception:
        pass

    return {
        "edit_dds_path": str(edit_dds),
        "raw_g1t_path": str(raw_g1t_path),
        "next_step": (
            "Edit font_edit.dds in Photoshop NVIDIA plugin (preset: BC3 / "
            "DXT5 / 1 mip / NO premultiplied alpha / NO mipmaps / Color Map), "
            "save over the same file, then click 'Apply font edit' button."
        ),
    }


def handle_apply_font_edit(params):
    """STEP 2: Patch the raw G1T container with the user-edited DDS payload
    and write to project/romfs/mods/72. Also writes UTF8TBL swap to mods/77
    so Є/є codepoints map to gids 285/317 (the mirrored Э/э cells)."""
    from formats.data1 import iter_data0, read_entry_full
    from formats.entry72_patch import patch_entry72_with_dds, safe_write_mod_72
    from formats.fontedit import parse_utf8tbl, serialize_utf8tbl, _find_gid

    project_path = Path(params["project_path"]).resolve()
    romfs_path = Path(params["romfs_path"]).resolve()

    edit_dds = project_path / "font_edit" / "font_edit.dds"
    raw_g1t = project_path / "font_edit" / "entry72_raw.g1t"
    if not edit_dds.exists():
        raise RuntimeError(
            f"edited DDS not found at {edit_dds}. Click 'Patch font' first "
            f"to export the original atlas for editing."
        )
    if not raw_g1t.exists():
        raise RuntimeError(f"raw G1T not found at {raw_g1t}; click 'Patch font' first.")

    # Patch raw G1T with edited DDS payload → temp file → install to mods/72
    mods_dir = project_path / "romfs" / "mods"
    mods_dir.mkdir(parents=True, exist_ok=True)
    patched_tmp = project_path / "font_edit" / "entry72_patched.g1t"
    patch_entry72_with_dds(raw_g1t, edit_dds, patched_tmp)
    safe_write_mod_72(patched_tmp, mods_dir / "72", info0_is_compressed=False)

    # UTF8TBL swap: map UA codepoints onto rarely-used Cyrillic glyph gids
    # whose cells the user has mirror-flipped in the atlas. Currently:
    #   U+0404 Є → gid 285 (was Э)
    #   U+0454 є → gid 317 (was э)
    # Future: Ы→Ґ etc. when user paints them.
    raw_tbl = None
    with (romfs_path / "DATA1.bin").open("rb") as f:
        for e in iter_data0(romfs_path / "DATA0.bin"):
            if e.entry_id == 77:
                raw_tbl = read_entry_full(f, e)
                break
    if raw_tbl is None:
        raise RuntimeError("DATA1 entry 77 (UTF8TBL) not found")

    cps = parse_utf8tbl(raw_tbl)
    new_cps = list(cps)
    swaps_applied = {}
    for ua_cp, ru_cp in (
        (0x0404, 0x042D),   # Є → Э slot
        (0x0454, 0x044D),   # є → э slot
        # Add more pairs here as user paints them, e.g.
        # (0x0490, 0x042B), # Ґ → Ы slot
        # (0x0491, 0x044B), # ґ → ы slot
    ):
        gid = _find_gid(cps, ru_cp)
        if gid is not None:
            new_cps[gid] = ua_cp
            swaps_applied[f"U+{ua_cp:04X}"] = gid

    new_tbl = serialize_utf8tbl(new_cps)
    if len(new_tbl) != len(raw_tbl):
        raise RuntimeError(
            f"UTF8TBL size changed: {len(new_tbl)} vs {len(raw_tbl)}"
        )
    (mods_dir / "77").write_bytes(new_tbl)

    return {
        "mods_72_path": str(mods_dir / "72"),
        "mods_72_size": (mods_dir / "72").stat().st_size,
        "mods_77_path": str(mods_dir / "77"),
        "mods_77_size": (mods_dir / "77").stat().st_size,
        "codepoint_to_gid": swaps_applied,
    }


def handle_export_multitex(params):
    """Export multi-texture G1T entry (title screen, abbey map, etc.) as a
    set of standalone DDS files for editing.

    params: {project_path, romfs_path, entry_id}
    Returns: {dds_paths: [...], raw_g1t_path, entry_id}
    """
    from formats.data1 import iter_data0, read_entry_full
    from formats import multi_texture_patch as mtx
    import subprocess

    project_path = Path(params["project_path"]).resolve()
    romfs_path = Path(params["romfs_path"]).resolve()
    entry_id = int(params["entry_id"])

    # Extract raw G1T from DATA1
    blob = None
    with (romfs_path / "DATA1.bin").open("rb") as f:
        for e in iter_data0(romfs_path / "DATA0.bin"):
            if e.entry_id == entry_id:
                blob = read_entry_full(f, e)
                break
    if blob is None or blob[:4] != b"GT1G":
        raise RuntimeError(f"DATA1 entry {entry_id} not found or not a G1T")

    out_dir = project_path / "multitex_edit" / str(entry_id)
    out_dir.mkdir(parents=True, exist_ok=True)
    raw_g1t = out_dir / "raw.g1t"
    raw_g1t.write_bytes(blob)

    dds_paths = mtx.export_subtextures(raw_g1t, out_dir)

    # Open folder in Explorer
    try:
        subprocess.Popen(["explorer", str(out_dir)])
    except Exception:
        pass

    return {
        "entry_id": entry_id,
        "raw_g1t_path": str(raw_g1t),
        "dds_paths": [str(p) for p in dds_paths],
        "next_step": (
            f"Edit any subset of tex_*.dds in Photoshop (BC3/DXT5, NO mipmaps, "
            f"NO premultiplied alpha, NO DXT10 header, NO size change). Save over "
            f"the same file. Then click 'Apply multi-texture edit' with entry_id={entry_id}."
        ),
    }


def handle_apply_multitex(params):
    """Patch the raw G1T container with edited DDS payloads → mods/<entry_id>."""
    from formats import multi_texture_patch as mtx
    project_path = Path(params["project_path"]).resolve()
    entry_id = int(params["entry_id"])

    edit_dir = project_path / "multitex_edit" / str(entry_id)
    raw_g1t = edit_dir / "raw.g1t"
    if not raw_g1t.exists():
        raise RuntimeError(
            f"raw G1T not found at {raw_g1t}. Click 'Export multi-texture for editing' first."
        )

    patched_tmp = edit_dir / "patched.g1t"
    mtx.patch_with_edited_ddses(raw_g1t, edit_dir, patched_tmp)

    mods_dir = project_path / "romfs" / "mods"
    out_path = mods_dir / str(entry_id)
    mtx.safe_write_mod(patched_tmp, out_path)

    return {
        "entry_id": entry_id,
        "mods_path": str(out_path),
        "mods_size": out_path.stat().st_size,
    }


def handle_reset_font_patch(params):
    """Remove any installed font mods (mods/72, mods/77) so the game uses
    its built-in atlas. The text-level substitute kicks in as fallback."""
    project_path = Path(params["project_path"]).resolve()
    mods_dir = project_path / "romfs" / "mods"
    removed = []
    for name in ("72", "77", "71", "76"):
        p = mods_dir / name
        if p.exists():
            p.unlink()
            removed.append(name)
    return {"removed": removed}


