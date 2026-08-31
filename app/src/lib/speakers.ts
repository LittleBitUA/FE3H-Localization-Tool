// Speaker metadata derived from the game data itself.
//
// Scene strings (talk_scinario) carry a `[NNNN]` speaker-id prefix and a
// `＠NNNNNN#N` voice-line marker. The id→name map below was derived
// empirically by matching signature lines in the ENG_U dump against the
// known script (see FE3H wiki). Unmapped ids render as "Speaker NNNN" —
// extend the map freely; it is presentation-only and never written back
// into game files.

export const SPEAKERS: Record<string, string> = {
  "0002": "Edelgard",
  "0003": "Dimitri",
  "0004": "Claude",
  "0012": "Dedue",
  "0013": "Felix",
  "0015": "Sylvain",
  "0016": "Mercedes",
  "0024": "Hilda",
  "0036": "Rhea",
  "0040": "Thales",
  "0084": "Anna",
  "1040": "Yuri",
  "1041": "Balthus",
  "1042": "Constance",
  "1043": "Hapi",
};

// Portraits bundled in app/public/portraits/<Name>.png (fetched from the
// FE3H wiki). Names absent from this set fall back to an initial avatar.
const PORTRAIT_FILES = new Set([
  "Anna", "Annette", "Ashe", "Balthus", "Bernadetta", "Byleth", "Caspar",
  "Catherine", "Claude", "Constance", "Cyril", "Dedue", "Dimitri",
  "Dorothea", "Edelgard", "Felix", "Ferdinand", "Flayn", "Gilbert", "Hapi",
  "Hilda", "Hubert", "Ignatz", "Ingrid", "Jeralt", "Leonie", "Linhardt",
  "Lorenz", "Lysithea", "Marianne", "Mercedes", "Petra", "Raphael", "Rhea",
  "Seteth", "Shamir", "Sothis", "Sylvain", "Yuri",
  "Thales", "Solon", "Kronya", "Jeritza", "Monica", "Aelfric", "Alois",
  "Manuela", "Hanneman", "Ladislava", "Randolph", "Judith", "Nader",
  "Rodrigue", "Gilbert", "Catherine", "Cyril",
]);

export function portraitSrc(name: string | null): string | null {
  if (!name) return null;
  return PORTRAIT_FILES.has(name) ? `portraits/${name}.png` : null;
}

export interface LineMeta {
  speakerId: string | null;
  /** Mapped character name, or null when the id is unknown. */
  speaker: string | null;
  /** Voice-line marker id (e.g. "035150"), or null. */
  voice: string | null;
  /** Text with the speaker prefix and voice markers stripped, for display. */
  text: string;
}

const SPEAKER_RE = /^\[(\d{4})\]/;
const VOICE_RE = /＠([A-Za-z0-9_]+)(?:#\d+)?/;

export function parseLine(raw: string): LineMeta {
  let text = raw;
  let speakerId: string | null = null;
  let voice: string | null = null;
  const m = SPEAKER_RE.exec(text);
  if (m) {
    speakerId = m[1];
    text = text.slice(m[0].length);
  }
  const v = VOICE_RE.exec(text);
  if (v) {
    voice = v[1];
    text = text.replace(/＠[A-Za-z0-9_]+(?:#\d+)?/g, "");
  }
  return {
    speakerId,
    speaker: speakerId ? SPEAKERS[speakerId] ?? null : null,
    voice,
    text: text.replace(/\s+$/, ""),
  };
}

// Split a raw game string into its technical markers and the editable body:
//   [NNNN]  speaker-id prefix   (kept as `pre`)
//   ＠XXX#N voice markers at the very end (kept as `post`)
// The editor shows only `body`; markers are re-attached verbatim on change,
// so what is saved keeps the exact original markers.
export interface MarkerParts {
  pre: string;
  body: string;
  post: string;
}

const SPLIT_RE = /^(\[\d{4}\])?([\s\S]*?)((?:＠[A-Za-z0-9_]+(?:#\d+)?)*)$/;

export function splitMarkers(raw: string): MarkerParts {
  const m = SPLIT_RE.exec(raw);
  if (!m) return { pre: "", body: raw, post: "" };
  return { pre: m[1] ?? "", body: m[2] ?? "", post: m[3] ?? "" };
}

// Trailing whitespace before a ＠ voice marker breaks the game's text parser
// (infinite-loading). Strip it only when a marker follows.
export function sanitizeMarkers(raw: string): string {
  const p = splitMarkers(raw);
  if (!p.post) return raw;
  return p.pre + p.body.replace(/\s+$/, "") + p.post;
}

export function speakerLabel(meta: LineMeta): string | null {
  if (meta.speaker) return meta.speaker;
  if (meta.speakerId && meta.speakerId !== "9999")
    return `Speaker ${meta.speakerId}`;
  return null;
}
