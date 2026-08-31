"""LayeredFS build + emulator deploy RPC handler."""
from __future__ import annotations
from pathlib import Path

from appconfig import get_config
from formats import info_patch


def handle_pack_for_switch(params):
    import shutil
    project_path = Path(params["project_path"]).resolve()
    romfs_path = Path(params["romfs_path"]).resolve()
    title_id = params.get("title_id") or get_config().get("title_id", "010055D009F78000")
    orig_info0 = romfs_path / "patch4" / "INFO0.bin"
    orig_info2 = romfs_path / "patch4" / "INFO2.bin"
    if not orig_info0.exists() or not orig_info2.exists():
        raise ValueError("original patch4/INFO0.bin or INFO2.bin not found")
    project_romfs = project_path / "romfs"
    project_mods = project_romfs / "mods"
    build_root = project_path / "build" / "atmosphere" / "contents" / title_id / "romfs"
    if build_root.exists():
        shutil.rmtree(build_root)
    build_root.mkdir(parents=True, exist_ok=True)

    # Font patcher already writes mods/72 + mods/77 directly into project_mods,
    # so we don't need to mirror anything further; build_info0_overlay will
    # pick them up by scanning the mods/ directory.

    new_info0, new_info2, added = info_patch.build_info0_overlay(
        original_info0_path=orig_info0,
        original_info2_path=orig_info2,
        mods_dir=project_mods,
    )
    patch4_out = build_root / "patch4"
    patch4_out.mkdir(parents=True, exist_ok=True)
    (patch4_out / "INFO0.bin").write_bytes(new_info0)
    (patch4_out / "INFO2.bin").write_bytes(new_info2)

    mod_count = 0
    if project_mods.is_dir():
        out_mods = build_root / "mods"
        out_mods.mkdir(parents=True, exist_ok=True)
        for f in project_mods.iterdir():
            if f.is_file() and f.name.isdigit():
                shutil.copy2(f, out_mods / f.name)
                mod_count += 1

    path_files = 0
    if project_romfs.is_dir():
        for src in project_romfs.rglob("*"):
            if not src.is_file():
                continue
            rel = src.relative_to(project_romfs).as_posix()
            if rel.startswith("mods/"):
                continue
            dst = build_root / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            path_files += 1

    # Optional: also mirror the generated romfs into the Eden emulator's
    # per-title load folder (overwriting whatever's there) and launch Eden.
    deployed_to = None
    launched = False
    if params.get("deploy_to_eden"):
        import os, time, subprocess
        # Kill any lingering Eden process that might be holding files open.
        # WinError 32 ("being used by another process") here is almost always
        # Eden mmap'ing mods/* files; a fresh process forces a clean handoff.
        try:
            subprocess.run(
                ["taskkill", "/F", "/IM", "eden.exe"],
                capture_output=True, timeout=5,
            )
        except Exception:
            pass
        time.sleep(0.5)
        eden_load = Path(os.path.expandvars(r"%APPDATA%\eden\load")) / title_id
        eden_root = eden_load / "UA" / "romfs"
        # Probe the actual target file (mods/72 is the one Eden mmap-locks).
        try:
            eden_root.mkdir(parents=True, exist_ok=True)
            probe_target = eden_root / "mods" / "72"
            probe_target.parent.mkdir(parents=True, exist_ok=True)
            if probe_target.exists():
                probe_target.unlink()    # raises if locked
            probe_target.write_bytes(b"")
            probe_target.unlink()
        except OSError as e:
            raise RuntimeError(
                f"Eden mod folder {eden_root} is locked (WinError 32). "
                f"This is a known Windows kernel-handle leak from Eden. "
                f"Fix: close Eden completely (Task Manager → eden.exe → End task), "
                f"then REBOOT Windows. After reboot, deploy works cleanly."
            ) from e
        # Retry rmtree a few times — Windows may still hold a handle briefly
        # after a process exits.
        for attempt in range(5):
            if not eden_root.exists():
                break
            try:
                shutil.rmtree(eden_root)
                break
            except OSError:
                time.sleep(0.5)
        eden_root.mkdir(parents=True, exist_ok=True)
        for src in build_root.rglob("*"):
            if not src.is_file():
                continue
            rel = src.relative_to(build_root).as_posix()
            dst = eden_root / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            # Retry copy if target file is locked by stale handle.
            last_err = None
            for attempt in range(5):
                try:
                    if dst.exists():
                        dst.unlink()
                    shutil.copy2(src, dst)
                    last_err = None
                    break
                except OSError as e:
                    last_err = e
                    time.sleep(0.3)
            if last_err is not None:
                raise last_err
        deployed_to = str(eden_root)

        # Force OS to flush our writes to disk so Eden sees the new files
        # rather than stale/empty pages when it opens them.
        import time
        for f in eden_root.rglob("*"):
            if f.is_file():
                try:
                    fd = os.open(str(f), os.O_RDONLY)
                    os.fsync(fd)
                    os.close(fd)
                except Exception:
                    pass
        time.sleep(2.0)  # safety margin before Eden starts reading

        # Launch the emulator with the game directly (paths from config).
        import subprocess
        cfg = get_config()
        eden_exe = Path(cfg.get("eden_exe", ""))
        game_nsp = Path(cfg.get("game_image", ""))
        if cfg.get("eden_exe") and eden_exe.exists() and game_nsp.exists():
            try:
                subprocess.Popen(
                    [str(eden_exe), "-f", "-g", str(game_nsp)],
                    creationflags=getattr(subprocess, "DETACHED_PROCESS", 0),
                    close_fds=True,
                )
                launched = True
            except Exception:
                pass

    # Optional: deploy to Ryubing/Ryujinx. Ryujinx supports TWO mod surfaces:
    #   1. native mods: <RyuData>/mods/contents/<TID>/<modname>/romfs/...
    #      (Ryujinx's own LayeredFS implementation, definitely read)
    #   2. sdcard atmosphere stub: <RyuData>/sdcard/atmosphere/contents/<TID>/romfs/...
    #      (full Atmosphere CFW convention; may or may not be picked up
    #       depending on Ryujinx fork & sdcard-emulation settings)
    # We mirror our build_root into BOTH so whichever surface is honoured.
    deployed_to_ryujinx = None
    launched_ryujinx = False
    if params.get("deploy_to_ryujinx"):
        import os
        ryu_candidates = [
            Path(os.path.expandvars(r"%APPDATA%\Ryubing")),
            Path(os.path.expandvars(r"%APPDATA%\Ryujinx")),
        ]
        ryu_data = next((p for p in ryu_candidates if p.exists()), ryu_candidates[0])

        targets = [
            ryu_data / "mods" / "contents" / title_id.lower() / "UA" / "romfs",
            ryu_data / "sdcard" / "atmosphere" / "contents" / title_id.lower() / "romfs",
        ]
        for ryu_root in targets:
            if ryu_root.exists():
                shutil.rmtree(ryu_root, ignore_errors=True)
            ryu_root.mkdir(parents=True, exist_ok=True)
            for src in build_root.rglob("*"):
                if not src.is_file():
                    continue
                rel = src.relative_to(build_root).as_posix()
                dst = ryu_root / rel
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)
        deployed_to_ryujinx = str(targets[0].parent.parent)  # mods/contents/<TID>/

        import time
        for ryu_root in targets:
            for f in ryu_root.rglob("*"):
                if f.is_file():
                    try:
                        fd = os.open(str(f), os.O_RDONLY)
                        os.fsync(fd)
                        os.close(fd)
                    except Exception:
                        pass
        time.sleep(1.0)

        import subprocess
        cfg = get_config()
        ryu_exe_candidates = [
            Path(cfg["ryujinx_exe"]) if cfg.get("ryujinx_exe") else None,
            Path(os.path.expandvars(r"%LOCALAPPDATA%\Programs\Ryubing\Ryujinx.exe")),
            ryu_data / "Ryujinx.exe",
        ]
        ryu_exe = next((p for p in ryu_exe_candidates if p and p.exists()), None)
        game_nsp = Path(cfg.get("game_image", ""))
        if ryu_exe and cfg.get("game_image") and game_nsp.exists():
            try:
                subprocess.Popen(
                    [str(ryu_exe), str(game_nsp)],
                    creationflags=getattr(subprocess, "DETACHED_PROCESS", 0),
                    close_fds=True,
                )
                launched_ryujinx = True
            except Exception:
                pass

    return {
        "build_root": str(build_root),
        "title_id": title_id,
        "indexed_total_in_info0": len(new_info0) // 0x120,
        "indexed_added_to_original": added,
        "indexed_mods_copied": mod_count,
        "path_based_files_copied": path_files,
        "deployed_to_eden": deployed_to,
        "launched_eden": launched,
        "deployed_to_ryujinx": deployed_to_ryujinx,
        "launched_ryujinx": launched_ryujinx,
        "deploy_hint": (
            f"Copy contents of '{(project_path / 'build')}' to root of your SD card "
            f"(merge into existing atmosphere/ folder)."
        ),
    }


