import { defineConfig, externalizeDepsPlugin } from "electron-vite";
import react from "@vitejs/plugin-react";
import { resolve } from "path";

export default defineConfig({
  main: {
    plugins: [externalizeDepsPlugin()],
    build: {
      outDir: resolve(__dirname, "dist-electron"),
      emptyOutDir: false,
      rollupOptions: {
        input: resolve(__dirname, "electron/main.ts"),
        output: { entryFileNames: "main.js" },
      },
    },
  },
  preload: {
    plugins: [externalizeDepsPlugin()],
    build: {
      outDir: resolve(__dirname, "dist-electron"),
      emptyOutDir: false,
      rollupOptions: {
        input: resolve(__dirname, "electron/preload.ts"),
        output: { entryFileNames: "preload.js" },
      },
    },
  },
  renderer: {
    root: resolve(__dirname, "src"),
    plugins: [react()],
    build: {
      outDir: resolve(__dirname, "dist"),
      emptyOutDir: true,
      rollupOptions: {
        input: resolve(__dirname, "src/index.html"),
      },
    },
    resolve: {
      alias: {
        "@": resolve(__dirname, "src"),
        "@shared": resolve(__dirname, "shared"),
      },
    },
  },
});
