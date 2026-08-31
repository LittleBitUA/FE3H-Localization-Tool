import { useEffect, useState } from "react";
import { api } from "./lib/api";
import { ProjectExplorer } from "./components/ProjectExplorer";
import { TextSEditor } from "./components/TextSEditor";
import {
  IconApply,
  IconChart,
  IconCrest,
  IconExtract,
  IconFolder,
  IconFont,
  IconImage,
  IconRocket,
} from "./components/icons";
import type {
  OpenProjectResult,
  TextFileEntry,
  Data1Entry,
  PathFileEntry,
} from "../shared/ipc";

export type Selection =
  | { kind: "path"; entry: TextFileEntry }
  | { kind: "indexed"; entry: Data1Entry };

export function App() {
  const [project, setProject] = useState<OpenProjectResult | null>(null);
  const [pythonVersion, setPythonVersion] = useState<string>("");
  const [data1Index, setData1Index] = useState<Data1Entry[] | null>(null);
  const [pathIndex, setPathIndex] = useState<PathFileEntry[] | null>(null);
  const [selection, setSelection] = useState<Selection | null>(null);
  const [status, setStatus] = useState<string>("");
  const [editorDirty, setEditorDirty] = useState(false);

  // Guard against silently losing unsaved edits when switching entries.
  function selectGuarded(s: Selection) {
    if (
      editorDirty &&
      !window.confirm(
        "У поточному файлі є незбережені зміни. Перемкнутись і втратити їх?",
      )
    ) {
      return;
    }
    setEditorDirty(false);
    setSelection(s);
  }

  useEffect(() => {
    if (!editorDirty) return;
    const onBeforeUnload = (e: BeforeUnloadEvent) => {
      e.preventDefault();
      e.returnValue = "";
    };
    window.addEventListener("beforeunload", onBeforeUnload);
    return () => window.removeEventListener("beforeunload", onBeforeUnload);
  }, [editorDirty]);
  const [sourceLang, setSourceLang] = useState<string>("ENG_U");
  const [scanningData1, setScanningData1] = useState(false);
  const [scanningPath, setScanningPath] = useState(false);
  const [packing, setPacking] = useState(false);
  const [extracting, setExtracting] = useState(false);
  const [targetSlot, setTargetSlot] = useState<number>(() => {
    const stored = localStorage.getItem("fe3h.target_slot");
    return stored ? Number(stored) : 1;
  });

  useEffect(() => {
    api()
      .rpc("ping")
      .then((r) => setPythonVersion(r.python_version))
      .catch((e) => setStatus(`Python sidecar failed: ${e.message}`));

    // Auto-restore last project on launch.
    const lastRomfs = localStorage.getItem("fe3h.romfs_path");
    const lastProject = localStorage.getItem("fe3h.project_path");
    if (lastRomfs) {
      loadProject(lastRomfs, lastProject ?? undefined).catch((e) => {
        setStatus(`Could not reopen last project: ${(e as Error).message}`);
        // Clear stale paths so next launch shows the empty state cleanly.
        localStorage.removeItem("fe3h.romfs_path");
      });
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  async function loadProject(romfsPath: string, projectPath?: string) {
    setStatus("Loading game dump…");
    const openParams: { romfs_path: string; project_path?: string } = {
      romfs_path: romfsPath,
    };
    if (projectPath) openParams.project_path = projectPath;
    const result = await api().rpc("open_project", openParams);
    setProject(result);
    setSelection(null);
    // Persist for next launch.
    localStorage.setItem("fe3h.romfs_path", result.romfs_path);
    if (result.project_path) {
      localStorage.setItem("fe3h.project_path", result.project_path);
    }
    setStatus(
      `Game dump loaded: ${result.data1_total_entries} DATA1 entries, ` +
        `${result.patch4_path_files} patch4 files. Press scan buttons.`,
    );
    return result;
  }

  async function onOpenGameDump() {
    const romfs = await api().pickDirectory();
    if (!romfs) return;
    try {
      const projectPath = project?.project_path ?? undefined;
      await loadProject(romfs, projectPath as string | undefined);
    } catch (e) {
      setStatus(`Error: ${(e as Error).message}`);
    }
  }

  async function onSelectProjectFolder() {
    if (!project) {
      setStatus("Open game dump first");
      return;
    }
    const dir = await api().pickDirectory();
    if (!dir) return;
    try {
      await loadProject(project.romfs_path, dir);
    } catch (e) {
      setStatus(`Error: ${(e as Error).message}`);
    }
  }

  async function onScanData1() {
    if (!project) return;
    setScanningData1(true);
    setStatus("Scanning DATA1…");
    try {
      const scanParams: {
        data0_path: string;
        data1_path: string;
        force_rescan: boolean;
        project_path?: string;
      } = {
        data0_path: project.data0_path,
        data1_path: project.data1_path,
        force_rescan: true,
      };
      if (project.project_path) scanParams.project_path = project.project_path;
      const summary = await api().rpc("scan_data1_texts", scanParams);
      if (!summary.cache_path) {
        setStatus("scan: no cache returned");
        return;
      }
      const PAGE = 2000;
      const total = await api().countJsonEntries(summary.cache_path);
      const collected: Data1Entry[] = [];
      for (let off = 0; off < total; off += PAGE) {
        setStatus(`Loading cache… ${off}/${total}`);
        const page = await api().readJsonPage<Data1Entry>(
          summary.cache_path,
          off,
          PAGE,
        );
        collected.push(...page);
      }
      setData1Index(collected);
      setStatus(
        `DATA1: ${summary.total} text-bearing entries · ` +
          Object.entries(summary.kinds)
            .map(([k, v]) => `${k}=${v}`)
            .join(" "),
      );
    } catch (e) {
      setStatus(`Scan failed: ${(e as Error).message}`);
    } finally {
      setScanningData1(false);
    }
  }

  // sourceLang is locked to ENG_U for the UA project; setter retained for
  // future use but not exposed in the UI right now.
  void setSourceLang;

  async function onScanPath() {
    if (!project) return;
    setScanningPath(true);
    setStatus("Scanning patch4 path-based files…");
    try {
      const params: { romfs_path: string; project_path?: string; force_rescan: boolean } = {
        romfs_path: project.romfs_path,
        force_rescan: true,
      };
      if (project.project_path) params.project_path = project.project_path;
      const summary = await api().rpc("survey_path_files", params);
      const PAGE = 2000;
      const total = await api().countJsonEntries(summary.cache_path);
      const collected: PathFileEntry[] = [];
      for (let off = 0; off < total; off += PAGE) {
        setStatus(`Loading path cache… ${off}/${total}`);
        const page = await api().readJsonPage<PathFileEntry>(
          summary.cache_path,
          off,
          PAGE,
        );
        collected.push(...page);
      }
      setPathIndex(collected);
      setStatus(
        `Path-based: ${summary.total} text-bearing · ` +
          Object.entries(summary.kinds)
            .map(([k, v]) => `${k}=${v}`)
            .join(" "),
      );
    } catch (e) {
      setStatus(`Path scan failed: ${(e as Error).message}`);
    } finally {
      setScanningPath(false);
    }
  }

  async function onPack(target: "build" | "eden" | "ryujinx" = "build") {
    if (!project?.project_path) {
      setStatus("Select project folder before packing");
      return;
    }
    setPacking(true);
    setStatus(
      target === "eden"
        ? "Packing + deploying to Eden…"
        : target === "ryujinx"
        ? "Packing + deploying to Ryujinx…"
        : "Packing for Switch…",
    );
    try {
      const r = await api().rpc("pack_for_switch", {
        project_path: project.project_path,
        romfs_path: project.romfs_path,
        deploy_to_eden: target === "eden",
        deploy_to_ryujinx: target === "ryujinx",
      });
      const summary = `${r.indexed_mods_copied} mods + ${r.path_based_files_copied} path files`;
      if (r.deployed_to_ryujinx) {
        setStatus(`Packed + deployed to ${r.deployed_to_ryujinx} · ${summary}`);
        await api().showInFolder(r.deployed_to_ryujinx);
      } else if (r.deployed_to_eden) {
        setStatus(`Packed + deployed to ${r.deployed_to_eden} · ${summary}`);
        await api().showInFolder(r.deployed_to_eden);
      } else {
        setStatus(`Packed → ${r.build_root} · ${summary}`);
        await api().showInFolder(r.build_root);
      }
    } catch (e) {
      setStatus(`Pack failed: ${(e as Error).message}`);
    } finally {
      setPacking(false);
    }
  }

  async function onExtractAll() {
    if (!project) {
      setStatus("Open game dump first");
      return;
    }
    // Auto-prompt for project folder if missing — extract needs a place to write.
    let projectPath = project.project_path;
    if (!projectPath) {
      setStatus("Choose where to save extracted .txt files…");
      const picked = await api().pickDirectory();
      if (!picked) return;
      try {
        const r = await api().rpc("open_project", {
          romfs_path: project.romfs_path,
          project_path: picked,
        });
        setProject(r);
        projectPath = r.project_path!;
      } catch (e) {
        setStatus(`Could not set project: ${(e as Error).message}`);
        return;
      }
    }
    setExtracting(true);
    setStatus(`Extracting ENG_U text → ${projectPath}/extracted/ (this may take ~30s)…`);
    try {
      const r = await api().rpc("extract_all_texts", {
        project_path: projectPath,
        data0_path: project.data0_path,
        data1_path: project.data1_path,
        romfs_path: project.romfs_path,
        source_lang: sourceLang,
      });
      setStatus(`Extracted ${r.total_written} entries → ${r.bundle_path}`);
      await api().showInFolder(r.bundle_path);
    } catch (e) {
      setStatus(`Extract failed: ${(e as Error).message}`);
    } finally {
      setExtracting(false);
    }
  }

  const [patchingFont, setPatchingFont] = useState(false);
  const [texEntryId, setTexEntryId] = useState("6039");
  const [progress, setProgress] = useState<null | {
    total_strings: number; translated: number; percent: number;
    chars_total: number; chars_translated: number; chars_percent: number;
    by_kind: { kind: string; done: number; total: number; pct: number }[];
  }>(null);
  const [progressLoading, setProgressLoading] = useState(false);
  const [applyWarnings, setApplyWarnings] = useState<null | {
    applied: number;
    skipped: number;
    errors_count: number;
    warnings: string[];
    warnings_count: number;
  }>(null);
  const [fontModalOpen, setFontModalOpen] = useState(false);
  const [textureModalOpen, setTextureModalOpen] = useState(false);
  const [deployMenuOpen, setDeployMenuOpen] = useState(false);

  useEffect(() => {
    if (!deployMenuOpen) return;
    const close = () => setDeployMenuOpen(false);
    window.addEventListener("click", close);
    return () => window.removeEventListener("click", close);
  }, [deployMenuOpen]);

  async function onPatchFont() {
    if (!project?.project_path) {
      setStatus("Set project folder first");
      return;
    }
    setPatchingFont(true);
    setStatus("Exporting font atlas for editing…");
    try {
      const r = await api().rpc("patch_font", {
        project_path: project.project_path,
        romfs_path: project.romfs_path,
      });
      setStatus(
        `Edit ${r.edit_dds_path} in Photoshop (BC3/DXT5, NO mipmaps, NO premultiplied alpha), ` +
          `then click "Apply font edit".`,
      );
    } catch (e) {
      setStatus(`Export failed: ${(e as Error).message}`);
    } finally {
      setPatchingFont(false);
    }
  }

  async function onApplyFontEdit() {
    if (!project?.project_path) {
      setStatus("Set project folder first");
      return;
    }
    setPatchingFont(true);
    setStatus("Applying edited DDS to mods/72 + mods/77…");
    try {
      const r = await api().rpc("apply_font_edit", {
        project_path: project.project_path,
        romfs_path: project.romfs_path,
      });
      const pairs = Object.entries(r.codepoint_to_gid)
        .map(([cp, gid]) => `${cp}→gid ${gid}`)
        .join(", ");
      setStatus(
        `Font patched: ${pairs}. mods/72=${r.mods_72_size.toLocaleString()} B, ` +
          `mods/77=${r.mods_77_size.toLocaleString()} B. Build+Deploy now.`,
      );
    } catch (e) {
      setStatus(`Apply failed: ${(e as Error).message}`);
    } finally {
      setPatchingFont(false);
    }
  }

  async function onExportTexture() {
    if (!project?.project_path) { setStatus("Set project folder first"); return; }
    const entry_id = parseInt(texEntryId.trim(), 10);
    if (!Number.isFinite(entry_id)) { setStatus("Bad entry ID — enter a number"); return; }
    setPatchingFont(true);
    setStatus(`Exporting entry ${entry_id} sub-textures…`);
    try {
      const r = await api().rpc("export_multitex", {
        project_path: project.project_path,
        romfs_path: project.romfs_path,
        entry_id,
      });
      setStatus(
        `Edit any of ${r.dds_paths.length} DDS in Photoshop (BC3, NO mipmaps, NO premultiplied alpha, NO DXT10), save over same file, then 'Apply texture'.`,
      );
    } catch (e) {
      setStatus(`Export texture failed: ${(e as Error).message}`);
    } finally {
      setPatchingFont(false);
    }
  }

  async function onApplyTexture() {
    if (!project?.project_path) { setStatus("Set project folder first"); return; }
    const entry_id = parseInt(texEntryId.trim(), 10);
    if (!Number.isFinite(entry_id)) { setStatus("Bad entry ID — enter a number"); return; }
    setPatchingFont(true);
    setStatus(`Applying entry ${entry_id} edited DDS → mods/${entry_id}…`);
    try {
      const r = await api().rpc("apply_multitex", {
        project_path: project.project_path,
        entry_id,
      });
      setStatus(`Texture patched: mods/${r.entry_id} (${r.mods_size.toLocaleString()} B). Build+Deploy now.`);
    } catch (e) {
      setStatus(`Apply texture failed: ${(e as Error).message}`);
    } finally {
      setPatchingFont(false);
    }
  }

  async function onCheckProgress() {
    if (!project) { setStatus("Open game dump first"); return; }
    if (!project.project_path) { setStatus("Set project folder first"); return; }
    setProgressLoading(true);
    setStatus("Scanning bundle vs originals…");
    try {
      const r = await api().rpc("translation_progress", {
        project_path: project.project_path,
        romfs_path: project.romfs_path,
      });
      setProgress(r);
      setStatus(
        `Progress: ${r.translated.toLocaleString()} / ${r.total_strings.toLocaleString()} strings (${r.percent.toFixed(2)}%)`,
      );
    } catch (e) {
      setStatus(`Progress check failed: ${(e as Error).message}`);
    } finally {
      setProgressLoading(false);
    }
  }

  async function onResetFontPatch() {
    if (!project?.project_path) {
      setStatus("Set project folder first");
      return;
    }
    setPatchingFont(true);
    try {
      const r = await api().rpc("reset_font_patch", {
        project_path: project.project_path,
      });
      setStatus(`Font patch reset. Removed mods: ${r.removed.join(", ") || "none"}.`);
    } catch (e) {
      setStatus(`Reset failed: ${(e as Error).message}`);
    } finally {
      setPatchingFont(false);
    }
  }

  async function onApplyBundle() {
    if (!project?.project_path) {
      setStatus("Set project folder first");
      return;
    }
    const bundlePath = await api().pickOpenTxt();
    if (!bundlePath) return;
    setStatus(`Applying ${bundlePath}…`);
    try {
      const r = await api().rpc("apply_bundle", {
        project_path: project.project_path,
        bundle_path: bundlePath,
        romfs_path: project.romfs_path,
        target_slot: targetSlot,
      });
      const errMsg =
        r.errors_count > 0 ? ` · ${r.errors_count} errors (sample: ${r.errors_sample.join("; ")})` : "";
      const warnMsg =
        r.warnings_count > 0 ? ` · ${r.warnings_count} warning${r.warnings_count === 1 ? "" : "s"}` : "";
      setStatus(
        `Applied ${r.applied} changed entries · ${r.skipped_unchanged} unchanged (no-op) → project/romfs/${errMsg}${warnMsg}`,
      );
      if (r.warnings_count > 0) {
        setApplyWarnings({
          applied: r.applied,
          skipped: r.skipped_unchanged,
          errors_count: r.errors_count,
          warnings: r.warnings,
          warnings_count: r.warnings_count,
        });
      }
    } catch (e) {
      setStatus(`Apply failed: ${(e as Error).message}`);
    }
  }

  return (
    <div className="app">
      <header className="topbar">
        <div className="brand">
          <span className="brand-crest"><IconCrest /></span>
          <span className="title">FE3H UA</span>
        </div>

        <div className="topbar-spacer" />

        <div className="topbar-group" title="Language slot the translation will be written into">
          <span className="topbar-mini-label">slot</span>
          <select
            value={targetSlot}
            onChange={(e) => {
              const v = Number(e.target.value);
              setTargetSlot(v);
              localStorage.setItem("fe3h.target_slot", String(v));
            }}
          >
            <option value={1}>EN-US (Eden)</option>
            <option value={2}>EN-UK (Switch)</option>
          </select>
        </div>

        <div className="topbar-divider" />

        <div className="topbar-group">
          <button onClick={() => onOpenGameDump()}>
            <IconFolder />
            {project ? "Reload dump" : "Open dump…"}
          </button>
          <button onClick={() => onSelectProjectFolder()} disabled={!project}>
            <IconFolder />
            {project?.project_path ? "Project…" : "Set project…"}
          </button>
        </div>

        <div className="topbar-divider" />

        <div className="topbar-group">
          <button
            onClick={() => onExtractAll()}
            disabled={!project || extracting}
            title="Dump every translatable entry into translation_bundle.txt"
          >
            <IconExtract />
            {extracting ? "Extracting…" : "Extract"}
          </button>
          <button
            className="primary"
            onClick={() => onApplyBundle()}
            disabled={!project?.project_path}
            title="Read translated bundle.txt and write per-entry .bin into project/romfs/"
          >
            <IconApply />
            Apply bundle
          </button>
          <button
            onClick={() => onCheckProgress()}
            disabled={!project?.project_path || progressLoading}
            title="Count translated vs untranslated strings"
          >
            <IconChart />
            {progressLoading ? "Counting…" : "Progress"}
          </button>
        </div>

        <div className="topbar-divider" />

        <div className="topbar-group">
          <button
            onClick={() => setFontModalOpen(true)}
            disabled={!project?.project_path}
            title="Patch font atlas to render Є/є glyphs"
          >
            <IconFont />
            Fonts…
          </button>
          <button
            onClick={() => setTextureModalOpen(true)}
            disabled={!project?.project_path}
            title="Edit multi-texture G1T entries (title screen, abbey map)"
          >
            <IconImage />
            Textures…
          </button>
        </div>

        <div className="topbar-divider" />

        <div
          className="topbar-menu"
          onClick={(e) => e.stopPropagation()}
        >
          <button
            className="primary"
            onClick={() => setDeployMenuOpen((v) => !v)}
            disabled={!project?.project_path || packing}
          >
            <IconRocket />
            {packing ? "Deploying…" : "Deploy ▾"}
          </button>
          {deployMenuOpen && (
            <div className="topbar-menu-items">
              <button
                onClick={() => {
                  setDeployMenuOpen(false);
                  onPack("build");
                }}
              >
                Build romfs only
                <span className="menu-hint">
                  Generate /build/atmosphere/ without deploying
                </span>
              </button>
              <button
                onClick={() => {
                  setDeployMenuOpen(false);
                  onPack("eden");
                }}
              >
                Build + Deploy to Eden
                <span className="menu-hint">
                  Copy to %APPDATA%/eden/load/&lt;TID&gt;/UA/
                </span>
              </button>
              <button
                onClick={() => {
                  setDeployMenuOpen(false);
                  onPack("ryujinx");
                }}
              >
                Build + Deploy to Ryujinx
                <span className="menu-hint">
                  Copy to Ryubing/mods/contents/&lt;TID&gt;/UA/ + launch
                </span>
              </button>
            </div>
          )}
        </div>
      </header>

      {progress && (
        <div className="modal-overlay" onClick={() => setProgress(null)}>
          <div className="modal" onClick={(e) => e.stopPropagation()} style={{ minWidth: 520 }}>
            <div className="modal-header">
              <h3>Translation progress</h3>
              <button className="close" onClick={() => setProgress(null)}>×</button>
            </div>
            <div className="modal-body">
              <div className="prog-hero">
                <span className="pct">
                  {progress.percent.toFixed(1)}
                  <span className="small">%</span>
                </span>
                <span className="label">
                  by string count
                </span>
              </div>
              <div className="prog-bar">
                <div style={{ width: `${Math.min(100, progress.percent)}%` }} />
              </div>

              <div className="prog-stats">
                <div className="prog-stat">
                  <div className="k">Strings</div>
                  <div className="v">
                    {progress.translated.toLocaleString()}{" "}
                    <span style={{ color: "var(--text-dim)", fontWeight: 400, fontSize: 13 }}>
                      / {progress.total_strings.toLocaleString()}
                    </span>
                  </div>
                  <div className="sub">
                    {(progress.total_strings - progress.translated).toLocaleString()} remaining
                  </div>
                </div>
                <div className="prog-stat">
                  <div className="k">Characters</div>
                  <div className="v">
                    {progress.chars_translated.toLocaleString()}{" "}
                    <span style={{ color: "var(--text-dim)", fontWeight: 400, fontSize: 13 }}>
                      / {progress.chars_total.toLocaleString()}
                    </span>
                  </div>
                  <div className="sub">
                    {progress.chars_percent.toFixed(1)}% by char count
                  </div>
                </div>
              </div>

              <div className="modal-section-title">Breakdown by kind</div>
              <div className="prog-kinds">
                {progress.by_kind.map((row) => (
                  <div className="prog-kind" key={row.kind}>
                    <span className="name">{row.kind}</span>
                    <div className="bar">
                      <div style={{ width: `${Math.min(100, row.pct)}%` }} />
                    </div>
                    <span className="count">
                      {row.done.toLocaleString()} / {row.total.toLocaleString()} ·{" "}
                      {row.pct.toFixed(1)}%
                    </span>
                  </div>
                ))}
              </div>
            </div>
          </div>
        </div>
      )}

      {applyWarnings && (
        <div className="modal-overlay" onClick={() => setApplyWarnings(null)}>
          <div
            className="modal"
            onClick={(e) => e.stopPropagation()}
            style={{ minWidth: 560, maxWidth: 780 }}
          >
            <div className="modal-header">
              <h3 style={{ color: "var(--warning)" }}>
                Bundle import warnings ({applyWarnings.warnings_count})
              </h3>
              <button className="close" onClick={() => setApplyWarnings(null)}>×</button>
            </div>
            <div className="modal-body">
              <div className="modal-note" style={{ marginBottom: 12 }}>
                Applied <b>{applyWarnings.applied}</b> · skipped{" "}
                <b>{applyWarnings.skipped}</b> · {applyWarnings.errors_count} errors.
                The entries below had structural issues — most were auto-recovered,
                but you should fix them in your bundle file to be safe.
              </div>
              <div
                style={{
                  background: "var(--bg)",
                  border: "1px solid var(--border)",
                  borderRadius: 4,
                  padding: 10,
                  fontFamily: "var(--mono)",
                  fontSize: 11,
                  lineHeight: 1.5,
                  maxHeight: "55vh",
                  overflowY: "auto",
                  whiteSpace: "pre-wrap",
                  wordBreak: "break-word",
                }}
              >
                {applyWarnings.warnings.map((w, i) => (
                  <div
                    key={i}
                    style={{
                      padding: "4px 0",
                      borderBottom:
                        i < applyWarnings.warnings.length - 1
                          ? "1px solid rgba(255,255,255,0.04)"
                          : "none",
                    }}
                  >
                    {w}
                  </div>
                ))}
                {applyWarnings.warnings_count > applyWarnings.warnings.length && (
                  <div style={{ opacity: 0.6, marginTop: 6, fontStyle: "italic" }}>
                    … and{" "}
                    {applyWarnings.warnings_count - applyWarnings.warnings.length} more
                  </div>
                )}
              </div>
            </div>
          </div>
        </div>
      )}

      {fontModalOpen && (
        <div className="modal-overlay" onClick={() => setFontModalOpen(false)}>
          <div className="modal" onClick={(e) => e.stopPropagation()} style={{ minWidth: 480 }}>
            <div className="modal-header">
              <h3>Font atlas — Є/є glyphs</h3>
              <button className="close" onClick={() => setFontModalOpen(false)}>×</button>
            </div>
            <div className="modal-body">
              <div className="modal-section">
                <div className="modal-section-title">Step 1 — Export</div>
                <div className="modal-note">
                  Extracts DATA1 entry 72 (font atlas) as DDS and opens{" "}
                  <code style={{ fontFamily: "var(--mono)" }}>
                    project/font_edit/font_edit.dds
                  </code>{" "}
                  in Explorer. Edit in Photoshop with the NVIDIA plugin: <b>BC3/DXT5</b>,{" "}
                  no mipmaps, no premultiplied alpha. Mirror the Э→Є and э→є cells, save
                  over the same file.
                </div>
              </div>
              <div className="modal-section">
                <div className="modal-section-title">Step 2 — Apply</div>
                <div className="modal-note">
                  Splices the edited DDS into{" "}
                  <code style={{ fontFamily: "var(--mono)" }}>mods/72</code> and swaps{" "}
                  <code style={{ fontFamily: "var(--mono)" }}>mods/77</code> (UTF8TBL)
                  so codepoints U+0404 (Є) → gid 285 and U+0454 (є) → gid 317.
                </div>
              </div>
              <div className="modal-section">
                <div className="modal-section-title">Reset</div>
                <div className="modal-note">
                  Removes <code style={{ fontFamily: "var(--mono)" }}>mods/72</code> and{" "}
                  <code style={{ fontFamily: "var(--mono)" }}>mods/77</code> to revert
                  to the vanilla atlas plus text-level fallback substitute (Є → Е etc.).
                </div>
              </div>
              <div className="modal-actions">
                <button
                  className="danger"
                  onClick={() => onResetFontPatch()}
                  disabled={patchingFont}
                >
                  Reset font patch
                </button>
                <div className="filler" />
                <button
                  onClick={() => onPatchFont()}
                  disabled={patchingFont}
                >
                  {patchingFont ? "Working…" : "1. Export for editing"}
                </button>
                <button
                  className="primary"
                  onClick={() => onApplyFontEdit()}
                  disabled={patchingFont}
                >
                  {patchingFont ? "Working…" : "2. Apply font edit"}
                </button>
              </div>
            </div>
          </div>
        </div>
      )}

      {textureModalOpen && (
        <div className="modal-overlay" onClick={() => setTextureModalOpen(false)}>
          <div className="modal" onClick={(e) => e.stopPropagation()} style={{ minWidth: 520 }}>
            <div className="modal-header">
              <h3>Multi-texture editor</h3>
              <button className="close" onClick={() => setTextureModalOpen(false)}>×</button>
            </div>
            <div className="modal-body">
              <div className="modal-section">
                <div className="modal-section-title">Quick presets</div>
                <div className="tex-presets">
                  <button
                    type="button"
                    className={`tex-preset ${texEntryId === "6039" ? "active" : ""}`}
                    onClick={() => setTexEntryId("6039")}
                  >
                    <div className="name">Title screen</div>
                    <div className="id">entry 6039 · 4 sub-textures</div>
                  </button>
                  <button
                    type="button"
                    className={`tex-preset ${texEntryId === "6063" ? "active" : ""}`}
                    onClick={() => setTexEntryId("6063")}
                  >
                    <div className="name">Abbey map</div>
                    <div className="id">entry 6063</div>
                  </button>
                </div>
              </div>

              <div className="modal-section">
                <div className="modal-section-title">Or custom entry ID</div>
                <div className="modal-row">
                  <input
                    type="text"
                    value={texEntryId}
                    onChange={(e) => setTexEntryId(e.target.value)}
                    placeholder="entry id"
                    style={{ flex: 1, maxWidth: 140 }}
                  />
                  <span style={{ fontSize: 11, color: "var(--text-dim)" }}>
                    must be a G1T container in DATA1
                  </span>
                </div>
              </div>

              <div className="modal-section">
                <div className="modal-note">
                  <b>Step 1 — Export</b> extracts each sub-texture as a standalone DDS
                  into{" "}
                  <code style={{ fontFamily: "var(--mono)" }}>
                    project/multitex_edit/&lt;entry_id&gt;/
                  </code>
                  . Edit any subset in Photoshop (BC3, NO mipmaps, NO premultiplied
                  alpha, NO DXT10).
                  <br />
                  <br />
                  <b>Step 2 — Apply</b> splices edited DDS payloads back into the G1T
                  container and writes to{" "}
                  <code style={{ fontFamily: "var(--mono)" }}>mods/&lt;entry_id&gt;</code>
                  . Unedited sub-textures fall back to the original.
                </div>
              </div>

              <div className="modal-actions">
                <div className="filler" />
                <button
                  onClick={() => onExportTexture()}
                  disabled={!project || patchingFont}
                >
                  1. Export texture
                </button>
                <button
                  className="primary"
                  onClick={() => onApplyTexture()}
                  disabled={!project?.project_path || patchingFont}
                >
                  2. Apply texture
                </button>
              </div>
            </div>
          </div>
        </div>
      )}

      <main className="main">
        {project ? (
          <>
            <ProjectExplorer
              project={project}
              data1Index={data1Index}
              pathIndex={pathIndex}
              scanningData1={scanningData1}
              scanningPath={scanningPath}
              onScanData1={onScanData1}
              onScanPath={onScanPath}
              selection={selection}
              onSelect={selectGuarded}
              sourceLang={sourceLang}
            />
            {selection ? (
              <TextSEditor
                selection={selection}
                project={project}
                sourceLang={sourceLang}
                targetSlot={targetSlot}
                onStatus={setStatus}
                onDirtyChange={setEditorDirty}
              />
            ) : (
              <div className="empty">Select a file from the left</div>
            )}
          </>
        ) : (
          <div className="empty" style={{ gridColumn: "1 / -1" }}>
            <div>
              <p>
                Press <b>«Open game dump…»</b> and point to your{" "}
                <code style={{ fontFamily: "var(--mono)" }}>romfs/</code> folder.
              </p>
              <p style={{ marginTop: 18, fontSize: 12, color: "var(--text-dim)" }}>
                expected layout:{" "}
                <code style={{ fontFamily: "var(--mono)" }}>
                  &lt;romfs&gt;/DATA0.bin + DATA1.bin + patch4/
                </code>
              </p>
            </div>
          </div>
        )}
      </main>

      <footer className="statusbar">
        <span
          className={`status-msg ${/fail|error|помилк/i.test(status) ? "err" : ""}`}
        >
          <span className="status-dot" />
          {status || "ready"}
        </span>
        <span>
          Python {pythonVersion || "…"} ·{" "}
          {project ? `dump: ${project.romfs_path} · ` : ""}
          {project?.project_path
            ? `project: ${project.project_path}`
            : "no project"}
        </span>
      </footer>
    </div>
  );
}
