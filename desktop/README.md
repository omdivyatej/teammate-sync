# teammate-sync desktop (Electron)

Cross-platform desktop app for teammate-sync. Bundles its own Python runtime
so end users do **not** need Python or pipx installed — double-click the
`.dmg` / `.exe` and go.

## Architecture

```
┌─ Electron (main.js) ────────────────────────────────────────────┐
│                                                                 │
│  resolvePython()  → bundled python-build-standalone runtime     │
│                     (resources/python-runtime/bin/python3)      │
│                                                                 │
│  writeShim()      → TEAMMATE_SYNC_BIN points at a shim that     │
│                     execs `<python> -m teammate_sync.cli`       │
│                                                                 │
│  startDashboardServer()                                         │
│       spawn: <python> -m teammate_sync.cli dashboard            │
│                       --serve-only                              │
│       reads {"port": N} from stdout                             │
│                                                                 │
│  BrowserWindow.loadURL(http://127.0.0.1:N/)                     │
│       → renders the existing dashboard SPA (sidebar + 5 panels) │
│                                                                 │
│  Tray menu: open / start-stop daemon / install CLI / quit       │
└─────────────────────────────────────────────────────────────────┘
```

The dashboard UI itself is unchanged — it's the same Python HTTP server +
SPA used by `teammate-sync dashboard`. Electron just hosts the window and
bundles the runtime.

## Why the CLI shim matters

Claude Code integration (slash commands, hooks, the MCP server) shells out to
a `teammate-sync` binary on PATH. The bundled app therefore also installs a
shim at `~/.local/bin/teammate-sync` (Tray → "Install CLI integration") that
execs the bundled Python. Without it, `/connect`, `/ask`, and the MCP server
can't run.

## Develop

```
cd desktop
npm install
# Dev mode uses your pipx venv's python (or $TEAMMATE_SYNC_DEV_PYTHON).
npm start
```

In dev mode the app loads the dashboard from whatever `teammate_sync` is
importable by the resolved dev Python — typically your existing pipx install.
You must have run `teammate-sync init` already (so auth.json exists).

## Build a distributable

```
cd desktop
npm install
npm run dist:mac      # → dist/teammate-sync-<ver>.dmg  (current arch)
npm run dist:win      # → dist/teammate-sync Setup <ver>.exe
npm run dist:linux    # → dist/teammate-sync-<ver>.AppImage + .deb
```

`npm run dist` first runs `scripts/bundle-python.sh` to download + stage the
standalone Python runtime with the wheel installed, then electron-builder
packages it.

### Code signing / notarization (macOS)

For a warning-free install you must sign + notarize with an Apple Developer
ID ($99/yr). Set these env vars before `npm run dist:mac`:

```
export CSC_LINK=/path/to/DeveloperID.p12
export CSC_KEY_PASSWORD=...
export APPLE_ID=you@example.com
export APPLE_APP_SPECIFIC_PASSWORD=xxxx-xxxx-xxxx-xxxx
export APPLE_TEAM_ID=XXXXXXXXXX
```

electron-builder picks these up automatically. Unsigned builds still run but
show the "Apple cannot verify…" Gatekeeper prompt (right-click → Open to
bypass).

## Assets needed (placeholders for now)

- `assets/icon.icns` — macOS app icon
- `assets/icon.ico` — Windows app icon
- `assets/icon.png` — Linux app icon (512×512)
- `assets/trayTemplate.png` — menu bar / tray icon (macOS template image,
  ~16×16 @2x, black on transparent)

The app runs without these (falls back to a blank tray icon); they're
required for a polished release build.

## Status

Scaffold complete. Not yet tested end-to-end as a packaged build. Next steps:
1. `npm install` + `npm start` (dev mode against pipx python)
2. Add real icon assets
3. `npm run bundle-python` + `npm run dist:mac` to produce a .dmg
4. Sign + notarize for distribution
