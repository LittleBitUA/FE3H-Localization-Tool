import type { Api } from "../../electron/preload";

// Window-level api shim (preload exposes contextBridge.exposeInMainWorld("api", ...)).
declare global {
  interface Window {
    api: Api;
  }
}

export const api = (): Api => window.api;
