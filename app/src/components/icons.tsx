// Tiny inline SVG icon set — presentation only.
import type { SVGProps } from "react";

function I(props: SVGProps<SVGSVGElement> & { children: React.ReactNode }) {
  const { children, ...rest } = props;
  return (
    <svg
      width="14"
      height="14"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.8"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      {...rest}
    >
      {children}
    </svg>
  );
}

export const IconFolder = () => (
  <I><path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V7Z" /></I>
);
export const IconRefresh = () => (
  <I><path d="M21 12a9 9 0 1 1-2.6-6.4" /><path d="M21 3v6h-6" /></I>
);
export const IconExtract = () => (
  <I><path d="M12 3v12" /><path d="m7 10 5 5 5-5" /><path d="M4 21h16" /></I>
);
export const IconApply = () => (
  <I><path d="M12 21V9" /><path d="m7 14 5-5 5 5" /><path d="M4 3h16" /></I>
);
export const IconChart = () => (
  <I><path d="M4 20V10" /><path d="M10 20V4" /><path d="M16 20v-7" /><path d="M22 20H2" /></I>
);
export const IconFont = () => (
  <I><path d="m5 20 7-16 7 16" /><path d="M8 13h8" /></I>
);
export const IconImage = () => (
  <I><rect x="3" y="4" width="18" height="16" rx="2" /><circle cx="9" cy="10" r="1.6" /><path d="m4 19 6-6 4 4 3-3 3 3" /></I>
);
export const IconRocket = () => (
  <I><path d="M12 15c5-4 6-8.5 6-11-2.5 0-7 1-11 6l-3 1 4 4 4 4 1-3Z" /><path d="M6 15c-1.5 1-2 4-2 4s3-.5 4-2" /></I>
);
export const IconExport = () => (
  <I><path d="M14 3h7v7" /><path d="M21 3 11 13" /><path d="M19 14v5a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h5" /></I>
);
export const IconImport = () => (
  <I><path d="M10 21H3v-7" /><path d="m3 21 10-10" /><path d="M5 10V6a2 2 0 0 1 2-2h11a2 2 0 0 1 2 2v11a2 2 0 0 1-2 2h-4" /></I>
);
export const IconSave = () => (
  <I><path d="M19 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11l5 5v11a2 2 0 0 1-2 2Z" /><path d="M17 21v-8H7v8" /><path d="M7 3v5h8" /></I>
);
export const IconSearch = () => (
  <I><circle cx="11" cy="11" r="7" /><path d="m21 21-4.3-4.3" /></I>
);
export const IconChevronL = () => (
  <I><path d="m15 18-6-6 6-6" /></I>
);
export const IconChevronR = () => (
  <I><path d="m9 18 6-6-6-6" /></I>
);
export const IconClock = () => (
  <I><circle cx="12" cy="12" r="9" /><path d="M12 7v5l3 2" /></I>
);
export const IconQuill = () => (
  <I><path d="M20 4c-6 0-12 4-14 12l-2 4 4-2c8-2 12-8 12-14Z" /><path d="M4 20 14 10" /></I>
);
export const IconVoice = () => (
  <I><path d="M12 3a3 3 0 0 1 3 3v6a3 3 0 0 1-6 0V6a3 3 0 0 1 3-3Z" /><path d="M19 11a7 7 0 0 1-14 0" /><path d="M12 18v3" /></I>
);
export const IconCrest = () => (
  <svg width="26" height="26" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">
    <path d="M12 1 15 8l7 4-7 4-3 7-3-7-7-4 7-4 3-7Z" opacity="0.9" />
    <path d="M12 6.5 13.6 10l3.4 2-3.4 2L12 17.5 10.4 14 7 12l3.4-2L12 6.5Z" fill="#131019" />
  </svg>
);
