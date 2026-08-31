// Long-lived Python sidecar via stdin/stdout JSON-RPC (one JSON object per line).
import { spawn, ChildProcessWithoutNullStreams } from "node:child_process";
import { existsSync } from "node:fs";
import { resolve } from "node:path";
import { app } from "electron";
import type {
  RpcRequest,
  RpcResponse,
  RpcMethods,
  RpcMethodName,
} from "../shared/ipc";

type Pending = {
  resolve: (value: unknown) => void;
  reject: (reason: Error) => void;
};

export class PythonBridge {
  private proc: ChildProcessWithoutNullStreams | null = null;
  private pending = new Map<number, Pending>();
  private nextId = 1;
  private stdoutBuffer = "";

  start(): void {
    if (this.proc) return;

    const scriptPath = app.isPackaged
      ? resolve(process.resourcesPath, "python", "server.py")
      : resolve(__dirname, "..", "python", "server.py");

    // Packaged builds ship their own embeddable CPython runtime so end
    // users don't need Python installed. Dev falls back to PATH.
    const bundledPython = app.isPackaged
      ? resolve(process.resourcesPath, "python-runtime", "python.exe")
      : resolve(__dirname, "..", "python-runtime", "python.exe");
    const pythonExe =
      process.env.FE3H_PYTHON ??
      (existsSync(bundledPython) ? bundledPython : "python");
    this.proc = spawn(pythonExe, [scriptPath], {
      stdio: ["pipe", "pipe", "pipe"],
      env: { ...process.env, PYTHONIOENCODING: "utf-8", PYTHONUNBUFFERED: "1" },
    });

    this.proc.stdout.setEncoding("utf-8");
    this.proc.stderr.setEncoding("utf-8");

    this.proc.stdout.on("data", (chunk: string) => {
      this.stdoutBuffer += chunk;
      let nl: number;
      while ((nl = this.stdoutBuffer.indexOf("\n")) !== -1) {
        const line = this.stdoutBuffer.slice(0, nl).trim();
        this.stdoutBuffer = this.stdoutBuffer.slice(nl + 1);
        if (!line) continue;
        this.handleLine(line);
      }
    });

    this.proc.stderr.on("data", (chunk: string) => {
      process.stderr.write(`[py] ${chunk}`);
    });

    this.proc.on("exit", (code) => {
      const err = new Error(`Python sidecar exited (code ${code})`);
      for (const p of this.pending.values()) p.reject(err);
      this.pending.clear();
      this.proc = null;
    });
  }

  private handleLine(line: string): void {
    let msg: RpcResponse;
    try {
      msg = JSON.parse(line) as RpcResponse;
    } catch (e) {
      process.stderr.write(`[py] non-JSON: ${line}\n`);
      return;
    }
    const p = this.pending.get(msg.id);
    if (!p) return;
    this.pending.delete(msg.id);
    if ("error" in msg) {
      p.reject(new Error(`${msg.error.code}: ${msg.error.message}`));
    } else {
      p.resolve(msg.result);
    }
  }

  // Long ops (extract/scan) legitimately run for minutes — generous cap so a
  // hung sidecar still surfaces as an error instead of a forever-pending call.
  private static CALL_TIMEOUT_MS = 10 * 60 * 1000;

  call<M extends RpcMethodName>(
    method: M,
    params?: RpcMethods[M]["params"],
  ): Promise<RpcMethods[M]["result"]> {
    // Lazy respawn: if the sidecar crashed, the next call restarts it.
    if (!this.proc) this.start();
    if (!this.proc) throw new Error("Python sidecar failed to start");
    const id = this.nextId++;
    const req: RpcRequest = { jsonrpc: "2.0", id, method, params };
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        if (this.pending.delete(id)) {
          reject(new Error(`RPC '${method}' timed out after 10 min`));
        }
      }, PythonBridge.CALL_TIMEOUT_MS);
      this.pending.set(id, {
        resolve: (v: unknown) => {
          clearTimeout(timer);
          (resolve as (v: unknown) => void)(v);
        },
        reject: (e: Error) => {
          clearTimeout(timer);
          reject(e);
        },
      });
      try {
        this.proc!.stdin.write(JSON.stringify(req) + "\n");
      } catch (e) {
        clearTimeout(timer);
        this.pending.delete(id);
        reject(e as Error);
      }
    });
  }

  stop(): void {
    this.proc?.kill();
    this.proc = null;
  }
}
