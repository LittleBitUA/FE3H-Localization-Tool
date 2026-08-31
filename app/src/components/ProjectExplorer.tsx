import { useMemo, useState } from "react";
import type {
  OpenProjectResult,
  Data1Entry,
  PathFileEntry,
  EntryKind,
} from "../../shared/ipc";
import type { Selection } from "../App";
import { IconRefresh, IconSearch } from "./icons";

interface Props {
  project: OpenProjectResult;
  data1Index: Data1Entry[] | null;
  pathIndex: PathFileEntry[] | null;
  scanningData1: boolean;
  scanningPath: boolean;
  onScanData1: () => void;
  onScanPath: () => void;
  selection: Selection | null;
  onSelect: (s: Selection) => void;
  sourceLang: string;
}

const LANG_OF_SOURCE: Record<string, string> = {
  ENG_U: "ENG", ENG_E: "ENG",
  GER: "GER",
  FRA_E: "FRA", FRA_U: "FRA",
  ESP_E: "ESP", ESP_U: "ESP",
  ITA: "ITA",
  JP: "JP", KOR: "KOR", CHN: "CHN", TWN: "CHN",
};

const KIND_TAG: Record<string, string> = {
  texts: "T",
  scene: "S",
  caption: "C",
  credit: "CR",
  scrdata: "SD",
};

type UnifiedRow =
  | { source: "data1"; entry: Data1Entry; key: string; sortKey: string }
  | { source: "path"; entry: PathFileEntry; key: string; sortKey: string };

function pathDisplayName(p: PathFileEntry): string {
  // Show relative path so user can tell talk_event/.../X.bin from caption/Y.bin.
  return p.rel_path.replace(/^patch\d+\//, "");
}

function lastSegment(p: string | null): string {
  if (!p) return "";
  const parts = p.replace(/\\/g, "/").split("/").filter(Boolean);
  return parts[parts.length - 1] ?? "";
}

export function ProjectExplorer({
  project,
  data1Index,
  pathIndex,
  scanningData1,
  scanningPath,
  onScanData1,
  onScanPath,
  selection,
  onSelect,
  sourceLang,
}: Props) {
  const desiredLang = LANG_OF_SOURCE[sourceLang] ?? "ENG";
  const [filter, setFilter] = useState("");
  const [kindFilter, setKindFilter] = useState<string>("all");

  const unified = useMemo<UnifiedRow[]>(() => {
    const rows: UnifiedRow[] = [];
    if (data1Index) {
      for (const e of data1Index) {
        rows.push({
          source: "data1",
          entry: e,
          key: `data1:${e.entry_id}`,
          sortKey: `1:${e.kind}:${e.name}:${e.entry_id}`,
        });
      }
    }
    if (pathIndex) {
      for (const e of pathIndex) {
        rows.push({
          source: "path",
          entry: e,
          key: `path:${e.abs_path}`,
          sortKey: `0:${e.kind}:${e.rel_path}`,
        });
      }
    }
    rows.sort((a, b) => a.sortKey.localeCompare(b.sortKey));
    return rows;
  }, [data1Index, pathIndex]);

  const filtered = useMemo<UnifiedRow[]>(() => {
    return unified.filter((r) => {
      const kind: EntryKind = r.entry.kind;
      if (kindFilter !== "all" && kind !== kindFilter) return false;
      // For per-language formats (TextS, caption per-lang) filter by lang.
      const lang = r.entry.lang;
      if (lang && lang !== "UNKNOWN" && lang !== desiredLang) return false;
      if (filter.trim()) {
        const q = filter.trim().toLowerCase();
        const haystack =
          r.source === "data1"
            ? `${r.entry.entry_id} ${r.entry.name}`
            : `${r.entry.rel_path} ${r.entry.name}`;
        if (!haystack.toLowerCase().includes(q)) return false;
      }
      return true;
    });
  }, [unified, kindFilter, desiredLang, filter]);

  const counts = useMemo(() => {
    const c: Record<string, number> = {};
    for (const r of unified) c[r.entry.kind] = (c[r.entry.kind] ?? 0) + 1;
    return c;
  }, [unified]);

  return (
    <aside className="sidebar">
      <div className="side-section side-project">
        <div className="side-label">Project</div>
        <div className="side-project-card" title={project.romfs_path}>
          <div className="side-project-name">
            {lastSegment(project.project_path) || "no project folder"}
          </div>
          <div className="side-project-sub">
            dump: {lastSegment(project.romfs_path)} ·{" "}
            {project.data1_total_entries.toLocaleString()} entries
          </div>
        </div>
      </div>

      <div className="side-section">
        <div className="side-label side-label-row">
          <span>Files</span>
          <span className="side-actions">
            <button
              className="mini"
              onClick={() => onScanPath()}
              disabled={scanningPath}
              title="Recursively classify every .bin in patch1-4"
            >
              <IconRefresh />
              {scanningPath ? "…" : pathIndex ? "patch" : "scan patch"}
            </button>
            <button
              className="mini"
              onClick={() => onScanData1()}
              disabled={scanningData1}
              title="Decompress and classify every entry in DATA1"
            >
              <IconRefresh />
              {scanningData1 ? "…" : data1Index ? "DATA1" : "scan DATA1"}
            </button>
          </span>
        </div>
        <div className="side-filters">
          <select
            value={kindFilter}
            onChange={(e) => setKindFilter(e.target.value)}
          >
            <option value="all">all formats ({unified.length})</option>
            <option value="texts">texts ({counts.texts ?? 0})</option>
            <option value="caption">captions ({counts.caption ?? 0})</option>
            <option value="credit">credits ({counts.credit ?? 0})</option>
            <option value="scene">scene ({counts.scene ?? 0})</option>
            <option value="scrdata">scrdata ({counts.scrdata ?? 0})</option>
          </select>
          <div className="side-search">
            <IconSearch />
            <input
              placeholder={`Search ${filtered.length} files…`}
              value={filter}
              onChange={(e) => setFilter(e.target.value)}
            />
          </div>
        </div>
      </div>

      <div className="entry-list">
        {filtered.slice(0, 3000).map((r) => {
          const sel = selection
            ? selection.kind === "path"
              ? `path:${selection.entry.abs_path}`
              : `data1:${selection.entry.entry_id}`
            : "";
          const display =
            r.source === "data1"
              ? `${r.entry.entry_id} · ${r.entry.name}`
              : pathDisplayName(r.entry);
          const meta =
            r.source === "data1"
              ? `${r.entry.kind === "texts" ? r.entry.lang : r.entry.kind} · ${r.entry.string_count} strings`
              : `${r.entry.kind} · ${r.entry.string_count} strings · path`;
          return (
            <div
              key={r.key}
              className={`entry-row ${sel === r.key ? "selected" : ""}`}
              onClick={() => {
                if (r.source === "data1")
                  onSelect({ kind: "indexed", entry: r.entry });
                else
                  onSelect({
                    kind: "path",
                    entry: {
                      name: r.entry.name,
                      abs_path: r.entry.abs_path,
                      size: r.entry.size,
                      string_count: r.entry.string_count,
                      kind: r.entry.kind,
                    },
                  });
              }}
              title={
                r.source === "data1"
                  ? `DATA1 entry ${r.entry.entry_id}`
                  : r.entry.abs_path
              }
            >
              <span className={`kind-tag kind-${r.entry.kind}`}>
                {KIND_TAG[r.entry.kind] ?? "?"}
              </span>
              <span className="entry-text">
                <span className="index">{display}</span>
                <span className="meta">{meta}</span>
              </span>
            </div>
          );
        })}
        {filtered.length > 3000 && (
          <div className="entry-list-note">
            showing first 3000 of {filtered.length} — narrow the filter
          </div>
        )}
        {unified.length === 0 && (
          <div className="entry-list-note">
            Press <b>scan patch</b> and <b>scan DATA1</b> to populate.
          </div>
        )}
      </div>
    </aside>
  );
}
