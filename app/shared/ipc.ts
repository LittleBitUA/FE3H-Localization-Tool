// Shared IPC contract between Electron main, preload, renderer, and Python sidecar.

export type RpcId = number;

export interface RpcRequest<P = unknown> {
  jsonrpc: "2.0";
  id: RpcId;
  method: string;
  params?: P;
}
export interface RpcSuccess<R = unknown> {
  jsonrpc: "2.0";
  id: RpcId;
  result: R;
}
export interface RpcError {
  jsonrpc: "2.0";
  id: RpcId;
  error: { code: number; message: string; data?: unknown };
}
export type RpcResponse<R = unknown> = RpcSuccess<R> | RpcError;

// ----- handlers -----

export interface PingResult {
  pong: true;
  python_version: string;
  cwd: string;
}

export interface OpenProjectParams {
  romfs_path: string;
  project_path?: string;
}
export interface OpenProjectResult {
  romfs_path: string;
  data0_path: string;
  data1_path: string;
  patch4_path: string | null;
  project_path: string | null;
  data1_total_entries: number;
  patch4_path_files: number;
  source_lang: string;
  target_lang: string;
}

export interface ListCategoriesParams {
  romfs_path: string;
  lang: string;
}
export interface TextCategory {
  name: string;
  lang_dir: string;
  file_count: number;
}

export interface ListInCategoryParams {
  lang_dir: string;
}
export interface TextFileEntry {
  name: string;
  abs_path: string;
  size: number;
  string_count: number;
  kind?: EntryKind;             // optional; defaults to 'texts' for legacy callers
}

export interface ScanData1Params {
  data0_path: string;
  data1_path: string;
  project_path?: string;
  force_rescan?: boolean;
}

export interface PathFileEntry {
  abs_path: string;
  rel_path: string;
  name: string;
  kind: EntryKind;
  lang: string;
  size: number;
  string_count: number;
}

export interface SurveyPathParams {
  romfs_path: string;
  project_path?: string;
  force_rescan?: boolean;
}

export interface SurveyPathSummary {
  cache_path: string;
  total: number;
  kinds: Record<string, number>;
  from_cache?: boolean;
}

export interface ScanData1Summary {
  cache_path: string | null;
  total: number;
  kinds: Record<string, number>;
  from_cache?: boolean;
  entries?: Data1Entry[];
}

export interface LoadCacheParams {
  cache_path: string;
}
export interface LoadCacheResult {
  entries: Data1Entry[];
}
export type EntryKind = "texts" | "caption" | "credit" | "scene" | "scrdata";

export interface Data1Entry {
  entry_id: number;
  name: string;
  decomp_size: number;
  compressed: boolean;
  string_count: number;
  kind: EntryKind;
  lang: string;
}

export interface EntryStrings {
  // Generic per-entry strings list (used by scene/caption/credit/texts).
  strings: string[];
  // For caption: per-entry timing (preserved on save).
  timings?: { start: number; duration: number }[];
}

export interface ReadEntryParams {
  source: "path" | "data1";
  // For source=path:
  abs_path?: string;
  // For source=data1:
  data0_path?: string;
  data1_path?: string;
  entry_id?: number;
  // kind hint (caller already classified):
  kind: EntryKind;
}

export interface ReadEntryResult {
  strings: string[];
  timings?: { start: number; duration: number }[];
  meta: Record<string, unknown>;     // format-specific (texts header, kind, etc)
}

export interface SaveEntryParams {
  project_path: string;
  source: "path" | "data1";
  kind: EntryKind;
  rel_from_romfs?: string;           // for path-based
  entry_id?: number;                 // for indexed
  strings: string[];
  timings?: { start: number; duration: number }[];
  meta?: Record<string, unknown>;    // round-trips header info (incl. blob_key)
  // Fallback source info so save works even if the sidecar restarted and its
  // blob cache is cold:
  orig_abs_path?: string;            // for path-based: the file read_entry used
  data0_path?: string;               // for indexed
  data1_path?: string;               // for indexed
  target_slot?: number;              // scrdata: language slot to write into
}

export interface ExtractAllParams {
  project_path: string;
  data0_path: string;
  data1_path: string;
  romfs_path: string;
  source_lang: string;               // ENG_U typically
}

export interface ExtractAllResult {
  out_dir: string;
  bundle_path: string;
  total_written: number;
}

export interface ApplyBundleParams {
  project_path: string;
  bundle_path: string;
  romfs_path: string;
  target_slot?: number;
}
export interface ApplyBundleResult {
  applied: number;
  skipped_unchanged: number;
  errors_count: number;
  errors_sample: string[];
  warnings_count: number;
  warnings: string[];
}

export interface PatchFontParams {
  project_path: string;
  romfs_path: string;
}
export interface PatchFontResult {
  edit_dds_path: string;
  raw_g1t_path: string;
  next_step: string;
}

export interface ApplyFontEditParams {
  project_path: string;
  romfs_path: string;
}
export interface ApplyFontEditResult {
  mods_72_path: string;
  mods_72_size: number;
  mods_77_path: string;
  mods_77_size: number;
  codepoint_to_gid: Record<string, number>;
}

export interface ResetFontPatchParams {
  project_path: string;
}
export interface ResetFontPatchResult {
  removed: string[];
}

export interface ExportMultitexParams {
  project_path: string;
  romfs_path: string;
  entry_id: number;
}
export interface ExportMultitexResult {
  entry_id: number;
  raw_g1t_path: string;
  dds_paths: string[];
  next_step: string;
}

export interface ApplyMultitexParams {
  project_path: string;
  entry_id: number;
}
export interface ApplyMultitexResult {
  entry_id: number;
  mods_path: string;
  mods_size: number;
}

export interface TranslationProgressParams {
  project_path: string;
  romfs_path: string;
}
export interface TranslationProgressKindRow {
  kind: string;
  done: number;
  total: number;
  pct: number;
}
export interface TranslationProgressResult {
  total_strings: number;
  translated: number;
  untranslated: number;
  percent: number;
  chars_total: number;
  chars_translated: number;
  chars_percent: number;
  by_kind: TranslationProgressKindRow[];
}

export interface TextSHeaderInfo {
  unk1: number;
  unk2: number;
  ptr_section_offset: number;
  ptr_section_size: number;
  ptr_count: number;
  reserved_raw_hex: string;
  pad_after_ptrs_hex: string;
}

export interface ReadPathTextsParams {
  path: string;
}
export interface ReadPathTextsResult {
  path: string;
  strings: string[];
  header: TextSHeaderInfo;
}

export interface ReadData1TextsParams {
  data0_path: string;
  data1_path: string;
  entry_id: number;
}
export interface ReadData1TextsResult {
  entry_id: number;
  size: number;
  compressed_in_data1: boolean;
  strings: string[];
  header: TextSHeaderInfo;
}

export interface SaveTranslationParams {
  project_path: string;
  rel_from_romfs: string;
  strings: string[];
  reserved_raw_hex?: string;
  pad_after_ptrs_hex?: string;
}
export interface SaveData1TranslationParams {
  project_path: string;
  entry_id: number;
  strings: string[];
  reserved_raw_hex?: string;
  pad_after_ptrs_hex?: string;
}
export interface SaveResult {
  path: string;
  bytes_written: number;
  identical_to_original: boolean;
  is_new: boolean;
}

export interface ReadExistingTranslationParams {
  project_path: string;
  rel_from_romfs: string;
}
export interface ReadExistingIndexedTranslationParams {
  project_path: string;
  entry_id: number;
}
export interface ExistingTranslationResult {
  exists: boolean;
  strings: string[];
}

export interface PackForSwitchParams {
  project_path: string;
  romfs_path: string;
  title_id?: string;
  deploy_to_eden?: boolean;
  deploy_to_ryujinx?: boolean;
}
export interface PackForSwitchResult {
  build_root: string;
  title_id: string;
  indexed_total_in_info0: number;
  indexed_added_to_original: number;
  indexed_mods_copied: number;
  path_based_files_copied: number;
  deployed_to_eden: string | null;
  launched_eden?: boolean;
  deployed_to_ryujinx: string | null;
  launched_ryujinx?: boolean;
  deploy_hint: string;
}

export interface RpcMethods {
  ping: { params: void; result: PingResult };
  open_project: { params: OpenProjectParams; result: OpenProjectResult };
  list_text_categories: { params: ListCategoriesParams; result: TextCategory[] };
  list_texts_in_category: { params: ListInCategoryParams; result: TextFileEntry[] };
  scan_data1_texts: { params: ScanData1Params; result: ScanData1Summary };
  survey_path_files: { params: SurveyPathParams; result: SurveyPathSummary };
  read_entry: { params: ReadEntryParams; result: ReadEntryResult };
  save_entry: { params: SaveEntryParams; result: SaveResult };
  read_existing_translation_unified: {
    params: {
      project_path: string;
      source: "path" | "data1";
      rel_from_romfs?: string;
      entry_id?: number;
      kind?: EntryKind;
      target_slot?: number;
    };
    result: ExistingTranslationResult;
  };
  extract_all_texts: { params: ExtractAllParams; result: ExtractAllResult };
  apply_bundle: { params: ApplyBundleParams; result: ApplyBundleResult };
  pack_for_switch: { params: PackForSwitchParams; result: PackForSwitchResult };
  patch_font: { params: PatchFontParams; result: PatchFontResult };
  apply_font_edit: { params: ApplyFontEditParams; result: ApplyFontEditResult };
  reset_font_patch: { params: ResetFontPatchParams; result: ResetFontPatchResult };
  export_multitex: { params: ExportMultitexParams; result: ExportMultitexResult };
  apply_multitex: { params: ApplyMultitexParams; result: ApplyMultitexResult };
  translation_progress: { params: TranslationProgressParams; result: TranslationProgressResult };
}

export type RpcMethodName = keyof RpcMethods;
