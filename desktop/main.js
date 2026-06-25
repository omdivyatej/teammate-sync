// teammate-sync desktop app — Electron main process.
//
// Responsibilities:
//   1. Resolve a Python interpreter:
//        - packaged: the bundled python-build-standalone runtime in resources
//        - dev:      $TEAMMATE_SYNC_DEV_PYTHON or `python3` on PATH
//      The interpreter has the `teammate_sync` package importable.
//   2. Write a shim (TEAMMATE_SYNC_BIN) that execs `<python> -m teammate_sync.cli`,
//      so the daemon, MCP, slash commands, and dashboard daemon-control all
//      dispatch through the bundled runtime — no system pipx install needed.
//   3. Spawn the dashboard server headless (`dashboard --serve-only`), read the
//      port it prints, and load it in a BrowserWindow.
//   4. Tray icon with quick actions (show, start/stop daemon, install CLI
//      integration, quit).
//
// The dashboard SPA (sidebar nav + 5 panels) is served by Python and rendered
// here — we reuse 100% of the existing dashboard.py web UI.

const { app, BrowserWindow, Tray, Menu, shell, dialog, nativeImage } = require('electron');
const { spawn, spawnSync } = require('child_process');
const path = require('path');
const fs = require('fs');
const os = require('os');

let mainWindow = null;
let tray = null;
let serveProc = null;
let dashboardUrl = null;

// ─── Python + shim resolution ──────────────────────────────────────────────

function resolvePython() {
  if (app.isPackaged) {
    const base = path.join(process.resourcesPath, 'python-runtime');
    if (process.platform === 'win32') {
      return path.join(base, 'python.exe');
    }
    return path.join(base, 'bin', 'python3');
  }
  // Dev: explicit override, else a venv next to the repo, else system python3.
  if (process.env.TEAMMATE_SYNC_DEV_PYTHON) {
    return process.env.TEAMMATE_SYNC_DEV_PYTHON;
  }
  // Common dev location: the pipx venv on this machine.
  const pipxPy = path.join(
    os.homedir(), '.local', 'pipx', 'venvs', 'teammate-sync', 'bin', 'python'
  );
  if (fs.existsSync(pipxPy)) return pipxPy;
  return 'python3';
}

// User-writable dir where the latest teammate-sync is installed from PyPI.
// Kept AHEAD of the bundled package on PYTHONPATH, so once self-update has run,
// the daemon, MCP server, hooks, and dashboard all pick up online updates with
// no .dmg re-download. Empty/missing on first run (bundled code is used).
function pkgDir() {
  return path.join(os.homedir(), '.teammate-sync', 'site-packages');
}

// Write a shim that execs `<python> -m teammate_sync.cli "$@"`. All of the
// product's subprocess dispatch (daemon up/down, slash commands, and the
// Claude-Code-invoked hooks + MCP server) goes through TEAMMATE_SYNC_BIN,
// which we point at this shim. The shim prepends the self-update dir to
// PYTHONPATH so those entry points also run updated code.
function writeShim(python) {
  const dir = app.getPath('userData');
  fs.mkdirSync(dir, { recursive: true });
  const pkg = pkgDir();
  if (process.platform === 'win32') {
    const shim = path.join(dir, 'teammate-sync.cmd');
    fs.writeFileSync(shim,
      '@echo off\r\n' +
      'set "PYTHONPATH=' + pkg + ';%PYTHONPATH%"\r\n' +
      '"' + python + '" -m teammate_sync.cli %*\r\n');
    return shim;
  }
  const shim = path.join(dir, 'teammate-sync');
  fs.writeFileSync(shim,
    '#!/bin/sh\n' +
    'export PYTHONPATH="' + pkg + '${PYTHONPATH:+:$PYTHONPATH}"\n' +
    'exec "' + python + '" -m teammate_sync.cli "$@"\n');
  fs.chmodSync(shim, 0o755);
  return shim;
}

function childEnv() {
  const dir = pkgDir();
  const prev = process.env.PYTHONPATH || '';
  return {
    ...process.env,
    TEAMMATE_SYNC_BIN: process.env.TEAMMATE_SYNC_BIN || globalShim,
    PYTHONPATH: prev ? `${dir}${path.delimiter}${prev}` : dir,
  };
}

// Pull the latest package from PyPI into pkgDir() in the background. It takes
// effect on the NEXT launch (this session keeps running the code it started
// with). Best-effort: if offline or pip fails, the app keeps using the
// currently-installed code.
function runSelfUpdateInBackground() {
  const dir = pkgDir();
  fs.mkdirSync(dir, { recursive: true });
  const proc = spawn(
    pythonPath,
    ['-m', 'teammate_sync.cli', 'self-update', '--target', dir],
    { env: childEnv() }
  );
  proc.stdout.on('data', (d) => process.stdout.write(`[self-update] ${d}`));
  proc.stderr.on('data', (d) => process.stderr.write(`[self-update] ${d}`));
  proc.on('error', () => { /* spawn failed; run on installed code */ });
}

let globalShim = null;
let pythonPath = null;

// ─── Dashboard server lifecycle ────────────────────────────────────────────

function startDashboardServer() {
  return new Promise((resolve, reject) => {
    const args = ['-m', 'teammate_sync.cli', 'dashboard', '--serve-only'];
    serveProc = spawn(pythonPath, args, { env: childEnv() });

    let buffered = '';
    let settled = false;

    const timeout = setTimeout(() => {
      if (!settled) {
        settled = true;
        reject(new Error('Dashboard server did not report a port within 20s.'));
      }
    }, 20000);

    serveProc.stdout.on('data', (data) => {
      buffered += data.toString();
      // Each line may be JSON: {"port": N, "url": "..."} or {"error": "..."}
      let nl;
      while ((nl = buffered.indexOf('\n')) >= 0) {
        const line = buffered.slice(0, nl).trim();
        buffered = buffered.slice(nl + 1);
        if (!line) continue;
        try {
          const obj = JSON.parse(line);
          if (obj.error && !settled) {
            settled = true;
            clearTimeout(timeout);
            reject(new Error(obj.error));
            return;
          }
          if (obj.url && !settled) {
            settled = true;
            clearTimeout(timeout);
            resolve(obj.url);
            return;
          }
        } catch (_) {
          // Non-JSON log line; ignore.
        }
      }
    });

    serveProc.stderr.on('data', (d) => {
      process.stderr.write(`[dashboard] ${d}`);
    });

    serveProc.on('exit', (code) => {
      if (!settled) {
        settled = true;
        clearTimeout(timeout);
        reject(new Error(`Dashboard server exited early (code ${code}). Have you run sign-in / init yet?`));
      }
    });
  });
}

function stopDashboardServer() {
  if (serveProc && !serveProc.killed) {
    try { serveProc.kill('SIGTERM'); } catch (_) {}
  }
  serveProc = null;
}

// ─── Daemon control (via shim) ─────────────────────────────────────────────

function runShim(args) {
  return spawnSync(globalShim, args, { env: childEnv(), encoding: 'utf8' });
}

function daemonStatus() {
  // Reads the pid file the same way the CLI does.
  const pidFile = path.join(os.homedir(), '.teammate-sync', 'state', 'daemon.pid');
  if (!fs.existsSync(pidFile)) return false;
  try {
    const pid = parseInt(fs.readFileSync(pidFile, 'utf8').trim(), 10);
    process.kill(pid, 0);
    return true;
  } catch (_) {
    return false;
  }
}

// ─── Window + tray ─────────────────────────────────────────────────────────

function createWindow() {
  mainWindow = new BrowserWindow({
    width: 1100,
    height: 720,
    minWidth: 880,
    minHeight: 560,
    title: 'CodeBaton',
    backgroundColor: '#14110d',
    titleBarStyle: process.platform === 'darwin' ? 'hiddenInset' : 'default',
    icon: process.platform === 'linux'
      ? path.join(__dirname, 'assets', 'icon.png')
      : undefined,
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false,
    },
  });

  if (dashboardUrl) {
    mainWindow.loadURL(dashboardUrl);
  }

  mainWindow.on('close', (e) => {
    // Hide instead of quit (keeps daemon + tray alive), unless really quitting.
    if (!app.isQuitting) {
      e.preventDefault();
      mainWindow.hide();
    }
  });
}

function showWindow() {
  if (!mainWindow) {
    createWindow();
  } else {
    mainWindow.show();
    mainWindow.focus();
  }
}

function buildTrayMenu() {
  const up = daemonStatus();
  return Menu.buildFromTemplate([
    { label: up ? '● daemon running' : '— daemon stopped', enabled: false },
    { type: 'separator' },
    { label: 'Open CodeBaton', click: showWindow },
    {
      label: up ? 'Stop daemon' : 'Start daemon',
      click: () => {
        runShim([up ? 'down' : 'up']);
        refreshTray();
      },
    },
    { type: 'separator' },
    {
      label: 'Check for Updates…',
      click: checkForUpdates,
    },
    { type: 'separator' },
    { label: 'Quit CodeBaton', click: () => { app.isQuitting = true; app.quit(); } },
  ]);
}

// Manual update: run self-update synchronously, then tell the user the result.
// Updated code applies after a restart (running processes hold the old code).
function checkForUpdates() {
  const dir = pkgDir();
  fs.mkdirSync(dir, { recursive: true });
  const res = spawnSync(
    pythonPath,
    ['-m', 'teammate_sync.cli', 'self-update', '--target', dir],
    { env: childEnv(), encoding: 'utf8', timeout: 120000 }
  );
  const out = `${res.stdout || ''}${res.stderr || ''}`.trim();
  const updated = /installed \d/.test(out);
  dialog.showMessageBox({
    type: updated ? 'info' : 'none',
    message: updated ? 'Update downloaded' : 'CodeBaton is up to date',
    detail: updated
      ? 'Quit and reopen CodeBaton to apply the update.'
      : (out || 'You already have the latest version.'),
    buttons: ['OK'],
  });
}

function refreshTray() {
  if (tray) tray.setContextMenu(buildTrayMenu());
}

function createTray() {
  // Use a template image so macOS renders it correctly in light/dark menu bar.
  let img = nativeImage.createFromPath(path.join(__dirname, 'assets', 'trayTemplate.png'));
  if (img.isEmpty()) {
    // Fallback: a tiny generated dot so the app still works without an icon asset.
    img = nativeImage.createEmpty();
  } else if (process.platform === 'darwin') {
    img.setTemplateImage(true);
  }
  tray = new Tray(img);
  tray.setToolTip('CodeBaton');
  tray.setContextMenu(buildTrayMenu());
  tray.on('click', showWindow);
}

// ─── App lifecycle ─────────────────────────────────────────────────────────

app.on('ready', async () => {
  if (process.platform === 'darwin') app.dock?.show();

  pythonPath = resolvePython();
  globalShim = writeShim(pythonPath);
  runSelfUpdateInBackground();

  createTray();

  try {
    // The dashboard server starts even when signed out — it shows the in-app
    // GitHub sign-in screen in the window itself (no terminal). So a failure
    // here is a genuine startup error, not a "needs setup" case.
    dashboardUrl = await startDashboardServer();
    createWindow();
    refreshTray();
  } catch (e) {
    const choice = dialog.showMessageBoxSync({
      type: 'error',
      buttons: ['Retry', 'Quit'],
      defaultId: 0,
      message: 'CodeBaton could not start its dashboard.',
      detail: `${e.message}\n\nThis is usually transient. Click Retry to try again.`,
    });
    if (choice === 0) {
      app.relaunch();
      app.exit(0);
    }
  }

  // Periodically refresh tray daemon status.
  setInterval(refreshTray, 5000);
});

app.on('window-all-closed', () => {
  // Stay alive in the tray (don't quit) — this is a background-ish app.
});

app.on('activate', () => {
  showWindow();
});

app.on('before-quit', () => {
  app.isQuitting = true;
  stopDashboardServer();
});
