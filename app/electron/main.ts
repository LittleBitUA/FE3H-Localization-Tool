import { app, BrowserWindow, ipcMain, dialog } from "electron";
import { resolve } from "node:path";
import { PythonBridge } from "./python-bridge";
import type { RpcMethods, RpcMethodName } from "../shared/ipc";

const python = new PythonBridge();
let mainWindow: BrowserWindow | null = null;

function createWindow() {
  mainWindow = new BrowserWindow({
    width: 1400,
    height: 900,
    backgroundColor: "#131019",
    webPreferences: {
      preload: resolve(__dirname, "preload.js"),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: false,
    },
  });

  if (process.env.ELECTRON_RENDERER_URL) {
    mainWindow.loadURL(process.env.ELECTRON_RENDERER_URL);
    mainWindow.webContents.openDevTools();
  } else {
    mainWindow.loadFile(resolve(__dirname, "../dist/index.html"));
  }
}

app.whenReady().then(() => {
  python.start();
  createWindow();

  app.on("activate", () => {
    if (BrowserWindow.getAllWindows().length === 0) createWindow();
  });
});

app.on("window-all-closed", () => {
  python.stop();
  if (process.platform !== "darwin") app.quit();
});

app.on("before-quit", () => python.stop());

// IPC: renderer → main → python
ipcMain.handle(
  "rpc",
  async <M extends RpcMethodName>(
    _evt: Electron.IpcMainInvokeEvent,
    method: M,
    params?: RpcMethods[M]["params"],
  ) => {
    return await python.call(method, params);
  },
);

// IPC: native dialogs
ipcMain.handle("dialog:pickDirectory", async () => {
  if (!mainWindow) return null;
  const r = await dialog.showOpenDialog(mainWindow, {
    properties: ["openDirectory"],
  });
  return r.canceled ? null : r.filePaths[0];
});

ipcMain.handle("dialog:saveTxt", async (_evt, defaultName: string) => {
  if (!mainWindow) return null;
  const r = await dialog.showSaveDialog(mainWindow, {
    defaultPath: defaultName,
    filters: [{ name: "Text", extensions: ["txt"] }],
  });
  return r.canceled ? null : r.filePath;
});

ipcMain.handle("dialog:openTxt", async () => {
  if (!mainWindow) return null;
  const r = await dialog.showOpenDialog(mainWindow, {
    properties: ["openFile"],
    filters: [{ name: "Text", extensions: ["txt"] }],
  });
  return r.canceled ? null : r.filePaths[0];
});

ipcMain.handle("fs:writeText", async (_evt, filePath: string, content: string) => {
  const { writeFile, mkdir } = await import("node:fs/promises");
  const { dirname } = await import("node:path");
  await mkdir(dirname(filePath), { recursive: true });
  await writeFile(filePath, content, "utf-8");
  return filePath;
});

ipcMain.handle("fs:readText", async (_evt, filePath: string) => {
  const { readFile } = await import("node:fs/promises");
  return await readFile(filePath, "utf-8");
});

ipcMain.handle("shell:showItemInFolder", async (_evt, fullPath: string) => {
  const { shell } = await import("electron");
  shell.showItemInFolder(fullPath);
});

// Cache parsed JSON in main process so we can serve pages without re-reading.
const jsonCache = new Map<string, { mtime: number; entries: unknown[] }>();

async function loadCached(filePath: string): Promise<unknown[]> {
  const { stat, readFile } = await import("node:fs/promises");
  const st = await stat(filePath);
  const cached = jsonCache.get(filePath);
  if (cached && cached.mtime === st.mtimeMs) return cached.entries;
  const raw = await readFile(filePath, "utf-8");
  const parsed = JSON.parse(raw);
  const entries: unknown[] = Array.isArray(parsed) ? parsed : parsed?.entries ?? [];
  jsonCache.set(filePath, { mtime: st.mtimeMs, entries });
  return entries;
}

// Total count of entries in a JSON file (entries field or top-level array).
ipcMain.handle("fs:countEntries", async (_evt, filePath: string) => {
  const entries = await loadCached(filePath);
  return entries.length;
});

// Read a page of entries from a JSON file. Avoids structured-clone overflow
// by serializing a sub-array small enough for Chromium IPC.
ipcMain.handle(
  "fs:readJsonPage",
  async (_evt, filePath: string, offset: number, limit: number) => {
    const entries = await loadCached(filePath);
    return entries.slice(offset, offset + limit);
  },
);
