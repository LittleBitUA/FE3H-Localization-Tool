import { contextBridge, ipcRenderer } from "electron";
import type { RpcMethods, RpcMethodName } from "../shared/ipc";

const api = {
  rpc: <M extends RpcMethodName>(
    method: M,
    params?: RpcMethods[M]["params"],
  ): Promise<RpcMethods[M]["result"]> =>
    ipcRenderer.invoke("rpc", method, params),
  pickDirectory: (): Promise<string | null> =>
    ipcRenderer.invoke("dialog:pickDirectory"),
  pickSaveTxt: (defaultName: string): Promise<string | null> =>
    ipcRenderer.invoke("dialog:saveTxt", defaultName),
  pickOpenTxt: (): Promise<string | null> =>
    ipcRenderer.invoke("dialog:openTxt"),
  writeTextFile: (filePath: string, content: string): Promise<string> =>
    ipcRenderer.invoke("fs:writeText", filePath, content),
  readTextFile: (filePath: string): Promise<string> =>
    ipcRenderer.invoke("fs:readText", filePath),
  showInFolder: (path: string): Promise<void> =>
    ipcRenderer.invoke("shell:showItemInFolder", path),
  countJsonEntries: (filePath: string): Promise<number> =>
    ipcRenderer.invoke("fs:countEntries", filePath),
  readJsonPage: <T = unknown>(
    filePath: string,
    offset: number,
    limit: number,
  ): Promise<T[]> => ipcRenderer.invoke("fs:readJsonPage", filePath, offset, limit),
};

contextBridge.exposeInMainWorld("api", api);

export type Api = typeof api;
