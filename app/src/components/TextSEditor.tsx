import { useEffect, useState } from "react";
import { api } from "../lib/api";
import type {
  OpenProjectResult,
  EntryKind,
  ReadEntryParams,
  SaveEntryParams,
  ReadEntryResult,
} from "../../shared/ipc";
import type { Selection } from "../App";
import {
  parseLine,
  portraitSrc,
  sanitizeMarkers,
  speakerLabel,
  splitMarkers,
} from "../lib/speakers";
import {
  IconChevronL,
  IconChevronR,
  IconClock,
  IconExport,
  IconImport,
  IconQuill,
  IconSave,
  IconVoice,
} from "./icons";

// Language slot ↔ text-dir mapping: slot 1 = EN-US (ENG_U), slot 2 = EN-UK
// (ENG_E). Per-entry Save honours the same slot selector as Apply bundle.
function slotLang(targetSlot: number): string {
  return targetSlot === 1 ? "ENG_U" : "ENG_E";
}

interface Props {
  selection: Selection;
  project: OpenProjectResult;
  sourceLang: string;
  targetSlot: number;
  onStatus: (s: string) => void;
  onDirtyChange?: (dirty: boolean) => void;
}

function computeRelFromRomfs(absPath: string, romfsPath: string): string {
  const a = absPath.replace(/\\/g, "/");
  const r = romfsPath.replace(/\\/g, "/").replace(/\/$/, "");
  if (!a.startsWith(r + "/"))
    throw new Error(`abs path ${a} not under romfs ${r}`);
  return a.slice(r.length + 1);
}

function retargetLang(rel: string, from: string, to: string): string {
  return rel.replace(`/text/${from}/`, `/text/${to}/`);
}

function buildReadParams(selection: Selection, project: OpenProjectResult): ReadEntryParams {
  if (selection.kind === "path") {
    return {
      source: "path",
      kind: selection.entry.kind ?? "texts",
      abs_path: selection.entry.abs_path,
    };
  }
  return {
    source: "data1",
    kind: selection.entry.kind,
    data0_path: project.data0_path,
    data1_path: project.data1_path,
    entry_id: selection.entry.entry_id,
  };
}

function buildSaveParams(
  selection: Selection,
  project: OpenProjectResult,
  sourceLang: string,
  targetSlot: number,
  data: ReadEntryResult,
  newStrings: string[],
): SaveEntryParams {
  const kind: EntryKind =
    selection.kind === "path"
      ? selection.entry.kind ?? "texts"
      : selection.entry.kind;
  if (selection.kind === "path") {
    const rel = computeRelFromRomfs(selection.entry.abs_path, project.romfs_path);
    return {
      project_path: project.project_path!,
      source: "path",
      kind,
      rel_from_romfs: retargetLang(rel, sourceLang, slotLang(targetSlot)),
      strings: newStrings,
      ...(data.timings ? { timings: data.timings } : {}),
      meta: data.meta,
      orig_abs_path: selection.entry.abs_path,
      target_slot: targetSlot,
    };
  }
  return {
    project_path: project.project_path!,
    source: "data1",
    kind,
    entry_id: selection.entry.entry_id,
    strings: newStrings,
    ...(data.timings ? { timings: data.timings } : {}),
    meta: data.meta,
    data0_path: project.data0_path,
    data1_path: project.data1_path,
    target_slot: targetSlot,
  };
}

// Serialize/parse the Unity-style TXT (id header + blank + body).
function serializeTxt(strings: string[]): string {
  const out: string[] = [];
  for (let i = 0; i < strings.length; i++) {
    out.push(`# === [${i}] ===`);
    out.push(strings[i]);
    out.push("");
  }
  return out.join("\n");
}

function parseTxt(text: string, expectedCount: number): string[] {
  const re = /^# === \[(\d+)\] ===\s*$/gm;
  const result = new Array(expectedCount).fill("");
  const matches: { idx: number; start: number; end: number }[] = [];
  let m: RegExpExecArray | null;
  while ((m = re.exec(text)) !== null) {
    matches.push({ idx: Number(m[1]), start: m.index + m[0].length, end: -1 });
  }
  for (let i = 0; i < matches.length; i++) {
    // end = start of the next header line, or EOF for the last chunk
    matches[i].end =
      i + 1 < matches.length ? text.indexOf("# === [", matches[i].start) : text.length;
    if (matches[i].end < 0) matches[i].end = text.length;
    let chunk = text.slice(matches[i].start, matches[i].end);
    chunk = chunk.replace(/^\r?\n/, "").replace(/\r?\n\s*$/, "");
    if (matches[i].idx < result.length) result[matches[i].idx] = chunk;
  }
  return result;
}

function Portrait({ name, size }: { name: string | null; size: number }) {
  const src = portraitSrc(name);
  if (src) {
    return (
      <img
        className="portrait"
        src={src}
        alt={name ?? ""}
        style={{ width: size, height: size }}
        draggable={false}
      />
    );
  }
  return (
    <span
      className="portrait portrait-fallback"
      style={{ width: size, height: size, fontSize: size * 0.42 }}
    >
      {name ? name[0] : "?"}
    </span>
  );
}

export function TextSEditor({
  selection,
  project,
  sourceLang,
  targetSlot,
  onStatus,
  onDirtyChange,
}: Props) {
  const [data, setData] = useState<ReadEntryResult | null>(null);
  const [translations, setTranslations] = useState<string[]>([]);
  const [dirty, setDirtyState] = useState(false);
  const [focusedIdx, setFocusedIdx] = useState(0);
  const targetLang = slotLang(targetSlot);

  function setDirty(d: boolean) {
    setDirtyState(d);
    onDirtyChange?.(d);
  }

  useEffect(() => {
    setData(null);
    setDirty(false);
    setFocusedIdx(0);
    (async () => {
      try {
        const params = buildReadParams(selection, project);
        const src = await api().rpc("read_entry", params);
        setData(src);
        // Try existing translation in project
        if (project.project_path) {
          try {
            const existingParams: {
              project_path: string;
              source: "path" | "data1";
              rel_from_romfs?: string;
              entry_id?: number;
              kind?: EntryKind;
              target_slot?: number;
            } = {
              project_path: project.project_path,
              source: selection.kind === "path" ? "path" : "data1",
              kind:
                selection.kind === "path"
                  ? selection.entry.kind ?? "texts"
                  : selection.entry.kind,
              target_slot: targetSlot,
            };
            if (selection.kind === "path") {
              const rel = computeRelFromRomfs(
                selection.entry.abs_path,
                project.romfs_path,
              );
              existingParams.rel_from_romfs = retargetLang(
                rel,
                sourceLang,
                targetLang,
              );
            } else {
              existingParams.entry_id = selection.entry.entry_id;
            }
            const ex = await api().rpc(
              "read_existing_translation_unified",
              existingParams,
            );
            if (ex.exists && ex.strings.length === src.strings.length) {
              setTranslations(ex.strings);
              onStatus(`Loaded existing translation (${ex.strings.length} strings)`);
              return;
            }
          } catch {
            // ignore
          }
        }
        setTranslations(src.strings.slice());
        onStatus(`Loaded ${src.strings.length} strings`);
      } catch (e) {
        onStatus(`Error: ${(e as Error).message}`);
      }
    })();
  }, [
    selection.kind,
    selection.kind === "path"
      ? selection.entry.abs_path
      : selection.entry.entry_id,
    project.project_path,
    project.romfs_path,
    sourceLang,
    targetSlot,
  ]);

  // Ctrl+S saves the current entry.
  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "s") {
        e.preventDefault();
        if (dirty && project.project_path) void onSave();
      }
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  });

  function updateOne(idx: number, value: string) {
    setTranslations((prev) => {
      const next = prev.slice();
      next[idx] = value;
      return next;
    });
    setDirty(true);
  }

  async function onSave() {
    if (!data) return;
    if (!project.project_path) {
      onStatus("Set project folder first");
      return;
    }
    onStatus("Saving…");
    try {
      // Trailing whitespace before a voice marker breaks the game's parser —
      // sanitize just before writing; the visible text is unaffected.
      const params = buildSaveParams(
        selection,
        project,
        sourceLang,
        targetSlot,
        data,
        translations.map(sanitizeMarkers),
      );
      const r = await api().rpc("save_entry", params);
      onStatus(`Saved ${r.bytes_written}B → ${r.path}`);
      setDirty(false);
    } catch (e) {
      onStatus(`Save failed: ${(e as Error).message}`);
    }
  }

  async function onExportTxt() {
    if (!data) return;
    const defaultName =
      (selection.kind === "path"
        ? selection.entry.name.replace(".bin", "")
        : `${selection.entry.entry_id}_${selection.entry.name.replace(".bin", "")}`) +
      ".txt";
    const path = await api().pickSaveTxt(defaultName);
    if (!path) return;
    try {
      await api().writeTextFile(path, serializeTxt(translations));
      onStatus(`Exported → ${path}`);
    } catch (e) {
      onStatus(`Export failed: ${(e as Error).message}`);
    }
  }

  async function onImportTxt() {
    if (!data) return;
    const path = await api().pickOpenTxt();
    if (!path) return;
    try {
      const text = await api().readTextFile(path);
      const parsed = parseTxt(text, data.strings.length);
      setTranslations(parsed);
      setDirty(true);
      onStatus(`Imported ${parsed.length} strings from ${path}`);
    } catch (e) {
      onStatus(`Import failed: ${(e as Error).message}`);
    }
  }

  // Presentation-only: move card focus and scroll it into view.
  function focusCard(idx: number) {
    if (!data) return;
    const clamped = Math.max(0, Math.min(data.strings.length - 1, idx));
    setFocusedIdx(clamped);
    document
      .getElementById(`msgcard-${clamped}`)
      ?.scrollIntoView({ block: "center", behavior: "smooth" });
  }

  if (!data) return <div className="empty">Loading…</div>;

  const headerName =
    selection.kind === "path"
      ? selection.entry.name
      : `${selection.entry.entry_id} · ${selection.entry.name}`;

  const kindBadge =
    selection.kind === "path" ? "texts (path)" : selection.entry.kind;

  const focusedSrc = data.strings[focusedIdx] ?? "";
  const focusedMeta = parseLine(focusedSrc);
  const focusedUa = translations[focusedIdx] ?? "";
  const focusedUaMeta = parseLine(focusedUa);
  const focusedName = speakerLabel(focusedMeta);
  const previewText = (focusedUaMeta.text || focusedMeta.text).trim();

  return (
    <section className="editor">
      <div className="editor-header">
        <span className={`kind-tag kind-${selection.kind === "path" ? selection.entry.kind ?? "texts" : selection.entry.kind}`}>
          {kindBadge}
        </span>
        <span className="title">{headerName}</span>
        <span className="editor-meta">
          {data.strings.length} strings
          {selection.kind === "path" ? ` · ${sourceLang}→${targetLang}` : ""}
        </span>
        <span className="editor-nav">
          <button
            className="iconbtn"
            title="Previous string"
            onClick={() => focusCard(focusedIdx - 1)}
            disabled={focusedIdx <= 0}
          >
            <IconChevronL />
          </button>
          <span className="editor-nav-pos">
            {focusedIdx + 1} / {data.strings.length}
          </span>
          <button
            className="iconbtn"
            title="Next string"
            onClick={() => focusCard(focusedIdx + 1)}
            disabled={focusedIdx >= data.strings.length - 1}
          >
            <IconChevronR />
          </button>
        </span>
        <button onClick={() => onExportTxt()} title="Save current strings to .txt">
          <IconExport /> Export
        </button>
        <button onClick={() => onImportTxt()} title="Load translations from .txt">
          <IconImport /> Import
        </button>
        <button
          className={dirty && project.project_path ? "primary" : ""}
          onClick={() => onSave()}
          disabled={!dirty || !project.project_path}
          title={
            !project.project_path ? "Set project folder to enable saving" : ""
          }
        >
          <IconSave />
          {!project.project_path
            ? "Set project…"
            : dirty
              ? "Save"
              : "Saved"}
        </button>
      </div>

      <div className="editor-body">
        <div className="string-list">
          {data.strings.map((src, i) => {
            const meta = parseLine(src);
            const name = speakerLabel(meta);
            const t = data.timings?.[i];
            // The textarea shows only the editable body; the [NNNN] speaker
            // prefix and ＠ voice markers are re-attached verbatim on change.
            const parts = splitMarkers(translations[i] ?? "");
            const uaLen = parts.body.length;
            return (
              <div
                key={i}
                id={`msgcard-${i}`}
                className={`msg-card ${focusedIdx === i ? "focused" : ""}`}
                onClick={() => setFocusedIdx(i)}
              >
                <div className="msg-card-head">
                  <span className="msg-idx">#{i}</span>
                  {name && (
                    <span className="chip chip-speaker">
                      <Portrait name={meta.speaker} size={18} />
                      {name}
                    </span>
                  )}
                  {meta.voice && (
                    <span className="chip" title="Voice line marker (kept on save)">
                      <IconVoice /> {meta.voice}
                    </span>
                  )}
                  {t && (
                    <span className="chip" title="Subtitle timing">
                      <IconClock /> {t.start.toFixed(2)}s · {t.duration.toFixed(2)}s
                    </span>
                  )}
                </div>

                <div className="msg-block msg-original">
                  <div className="msg-label">Original text</div>
                  <div className="msg-source">{meta.text || src}</div>
                </div>

                <div className="msg-block msg-edited">
                  <div className="msg-label">
                    <IconQuill /> UA translation
                  </div>
                  <textarea
                    rows={Math.max(3, src.split("\n").length + 1)}
                    value={parts.body}
                    onChange={(e) =>
                      updateOne(i, parts.pre + e.target.value + parts.post)
                    }
                    onFocus={() => setFocusedIdx(i)}
                    spellCheck={false}
                  />
                  <div className="msg-count">
                    {uaLen} chars · EN {meta.text.length}
                  </div>
                </div>
              </div>
            );
          })}
        </div>

        <aside className="infopanel">
          <div className="info-card">
            <div className="info-title">Message info</div>
            <div className="info-rows">
              <div className="info-row">
                <span className="k">Entry</span>
                <span className="v mono">{headerName}</span>
              </div>
              <div className="info-row">
                <span className="k">Format</span>
                <span className="v">{kindBadge}</span>
              </div>
              <div className="info-row">
                <span className="k">Source</span>
                <span className="v mono">
                  {selection.kind === "path" ? "path file" : `DATA1 #${selection.entry.entry_id}`}
                </span>
              </div>
              <div className="info-row">
                <span className="k">Language</span>
                <span className="v mono">
                  {selection.kind === "path"
                    ? `${sourceLang} → ${targetLang}`
                    : `${sourceLang} → slot ${targetSlot}`}
                </span>
              </div>
              <div className="info-row">
                <span className="k">String</span>
                <span className="v mono">
                  #{focusedIdx} of {data.strings.length}
                </span>
              </div>
              {focusedName && (
                <div className="info-row">
                  <span className="k">Speaker</span>
                  <span className="v">
                    {focusedName}
                    {focusedMeta.speakerId ? (
                      <span className="dim mono"> [{focusedMeta.speakerId}]</span>
                    ) : null}
                  </span>
                </div>
              )}
              {focusedMeta.voice && (
                <div className="info-row">
                  <span className="k">Voice</span>
                  <span className="v mono">＠{focusedMeta.voice}</span>
                </div>
              )}
              {data.timings?.[focusedIdx] && (
                <div className="info-row">
                  <span className="k">Timing</span>
                  <span className="v mono">
                    {data.timings[focusedIdx].start.toFixed(2)}s +
                    {data.timings[focusedIdx].duration.toFixed(2)}s
                  </span>
                </div>
              )}
              <div className="info-row">
                <span className="k">Length</span>
                <span className="v mono">
                  UA {focusedUaMeta.text.length} · EN {focusedMeta.text.length}
                </span>
              </div>
            </div>
          </div>

          <div className="info-card">
            <div className="info-title">Preview</div>
            <div className="preview-box">
              <div className="preview-plate">
                <span className="preview-name">{focusedName ?? "…"}</span>
              </div>
              <div className="preview-body">
                <div className="preview-text">
                  {previewText || <span className="dim">— empty —</span>}
                </div>
                <Portrait name={focusedMeta.speaker} size={64} />
              </div>
            </div>
            <div className="info-note">
              Speaker (<span className="mono">[NNNN]</span>) and voice (
              <span className="mono">＠NNNNNN</span>) markers come from the
              game data. They are hidden from the editing field and re-attached
              automatically on save.
            </div>
          </div>
        </aside>
      </div>
    </section>
  );
}
