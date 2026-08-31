# FE3H Localization Tool — app

Electron GUI + Python sidecar. Повна документація — у [корені репозиторію](../README.md).

```
Renderer (React)  ──IPC──→  Main (Electron)  ──stdio JSON-RPC──→  Sidecar (Python)
                                                                     │
                                                                     ▼
                                                        formats: TextS / Scene / Caption /
                                                                 Credit / msgdata / G1T
```

## Розробка

```bash
npm install
npm run dev          # electron-vite dev (HMR)
npm run typecheck    # tsc --noEmit
npm run build        # production build → dist/ + dist-electron/
```

Python sidecar: `python/server.py`, формати — `python/formats/`,
тести — `python/tests/` (`python -m unittest discover -s tests`).
