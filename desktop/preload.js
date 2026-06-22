// teammate-sync desktop — preload bridge.
//
// The dashboard SPA is fully self-contained (it talks to the local Python
// HTTP server over fetch), so right now no privileged bridge is required.
// This file exists so we can expose narrowly-scoped IPC later (e.g. native
// "reveal log in Finder", native notifications) without enabling
// nodeIntegration in the renderer.

const { contextBridge } = require('electron');

contextBridge.exposeInMainWorld('teammateSync', {
  version: '0.6.0',
  platform: process.platform,
});
