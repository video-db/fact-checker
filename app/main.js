const { app, BrowserWindow, Tray, ipcMain, nativeImage, screen, systemPreferences, shell } = require('electron');
const path = require('path');
const http = require('http');
const { spawn, execSync } = require('child_process');
const fs = require('fs');
const os = require('os');

// Project root is one level up from app/
const PROJECT_ROOT = path.resolve(__dirname, '..');
const BACKEND_DIR = path.join(PROJECT_ROOT, 'backend');

require('dotenv').config({ path: path.join(PROJECT_ROOT, '.env') });

const BACKEND_PORT = process.env.PORT || 5002;
const BACKEND_URL = `http://localhost:${BACKEND_PORT}`;

// ---------------------------------------------------------------------------
// Structured logging (Improvement 3)
// ---------------------------------------------------------------------------

function ts() { return new Date().toISOString().slice(11, 23); }

function log(tag, msg, data) {
  const extra = data !== undefined ? ` ${typeof data === 'string' ? data : JSON.stringify(data)}` : '';
  console.log(`${ts()} [${tag}] ${msg}${extra}`);
}

function warn(tag, msg, data) {
  const extra = data !== undefined ? ` ${typeof data === 'string' ? data : JSON.stringify(data)}` : '';
  console.warn(`${ts()} [${tag}] ${msg}${extra}`);
}

function logError(tag, msg, err) {
  const errMsg = err instanceof Error ? err.message : err !== undefined ? String(err) : '';
  console.error(`${ts()} [${tag}] ${msg}${errMsg ? ': ' + errMsg : ''}`);
}

// ---------------------------------------------------------------------------
// State
// ---------------------------------------------------------------------------

let backendProcess = null;
let clientProcess = null;
let sseRequest = null;
let tray = null;
let popupWindow = null;
let isShuttingDown = false;
let isPaused = false;

// SSE backoff state
let sseReconnectTimer = null;
let sseBackoffDelay = 2000;
const SSE_BACKOFF_INITIAL = 2000;
const SSE_BACKOFF_MULTIPLIER = 1.5;
const SSE_BACKOFF_MAX = 30000;

// ---------------------------------------------------------------------------
// Graceful shutdown (Improvement 1)
// ---------------------------------------------------------------------------

async function shutdownApp() {
  if (isShuttingDown) return;
  isShuttingDown = true;
  log('APP', 'Shutting down...');
  removeIpcHandlers();
  disconnectSSE();
  await stopClient();
  await stopBackend();
  if (tray) { tray.destroy(); tray = null; }
}

process.on('SIGINT', async () => { await shutdownApp(); process.exit(0); });
process.on('SIGTERM', async () => { await shutdownApp(); process.exit(0); });
process.on('uncaughtException', (err) => { logError('APP', 'Uncaught exception', err); });
process.on('unhandledRejection', (reason) => { logError('APP', 'Unhandled rejection', reason); });

// ---------------------------------------------------------------------------
// Stale process cleanup (Improvement 2)
// ---------------------------------------------------------------------------

function cleanupStaleProcesses() {
  // Kill anything on our port
  try {
    const pids = execSync(`lsof -ti:${BACKEND_PORT}`, { encoding: 'utf8', timeout: 3000 }).trim();
    if (pids) {
      for (const pid of pids.split('\n')) {
        try { process.kill(parseInt(pid, 10), 'SIGTERM'); } catch {}
      }
      log('CLEANUP', 'Killed stale processes on port', String(BACKEND_PORT));
    }
  } catch { /* no processes on port — expected */ }

  // Clean lock files
  const lockPaths = [
    path.join(BACKEND_DIR, 'videodb-recorder.lock'),
    path.join(os.tmpdir(), 'videodb-recorder.lock'),
  ];
  for (const p of lockPaths) {
    try {
      if (fs.existsSync(p)) {
        fs.unlinkSync(p);
        log('CLEANUP', 'Removed lock file:', p);
      }
    } catch {}
  }
}

// ---------------------------------------------------------------------------
// Python process management
// ---------------------------------------------------------------------------

function getPythonPath() {
  const venvPython = path.join(BACKEND_DIR, 'venv', 'bin', 'python');
  return venvPython;
}

function sendErrorToRenderer(title, detail) {
  if (popupWindow && !popupWindow.isDestroyed()) {
    popupWindow.webContents.send('error-message', { title, detail });
  }
}

function startBackend() {
  return new Promise((resolve, reject) => {
    if (backendProcess) {
      resolve();
      return;
    }

    const pythonPath = getPythonPath();

    // Verify the venv python binary exists before attempting to spawn
    if (!fs.existsSync(pythonPath)) {
      const msg = `Python not found at ${pythonPath}. Run ./scripts/setup.sh first.`;
      logError('BACKEND', msg);
      sendErrorToRenderer('Setup required', msg);
      reject(new Error(msg));
      return;
    }

    log('BACKEND', `Starting: ${pythonPath} backend.py`);

    backendProcess = spawn(pythonPath, ['backend.py'], {
      cwd: BACKEND_DIR,
      env: { ...process.env },
      stdio: ['ignore', 'pipe', 'pipe'],
    });

    let resolved = false;

    backendProcess.stdout.on('data', (data) => {
      const text = data.toString();
      process.stdout.write(`${ts()} [BACKEND] ${text}`);
      if (!resolved && text.includes('[READY] Backend running')) {
        resolved = true;
        resolve();
      }
    });

    backendProcess.stderr.on('data', (data) => {
      process.stderr.write(`${ts()} [BACKEND:ERR] ${data.toString()}`);
    });

    backendProcess.once('close', (code) => {
      log('BACKEND', `Exited with code ${code}`);
      backendProcess = null;
      if (popupWindow && !popupWindow.isDestroyed()) {
        popupWindow.webContents.send('backend-status', 'stopped');
      }
      // Cascade: kill client and disconnect SSE
      stopClient();
      disconnectSSE();
      sendErrorToRenderer('Backend crashed', `Backend process exited with code ${code}`);
      if (!resolved) {
        resolved = true;
        reject(new Error(`Backend exited with code ${code}`));
      }
    });

    // Timeout after 60s
    setTimeout(() => {
      if (!resolved) {
        resolved = true;
        reject(new Error('Backend failed to start within 60 seconds'));
      }
    }, 60000);
  });
}

// ---------------------------------------------------------------------------
// Backend health check with retry (Improvement 5)
// ---------------------------------------------------------------------------

async function waitForBackendReady(retries = 5, delay = 1000) {
  for (let i = 0; i < retries; i++) {
    try {
      await fetchJSON(`${BACKEND_URL}/health`);
      log('BACKEND', 'Health check passed');
      return true;
    } catch {
      if (i < retries - 1) {
        log('BACKEND', `Health check attempt ${i + 1}/${retries} failed, retrying...`);
        await new Promise(r => setTimeout(r, delay));
      }
    }
  }
  return false;
}

// ---------------------------------------------------------------------------
// Client process management
// ---------------------------------------------------------------------------

function startClient(sourceType, target) {
  return new Promise((resolve, reject) => {
    if (clientProcess) {
      reject(new Error('Client already running'));
      return;
    }

    const choiceMap = { youtube: '1', meet: '2', local: '3', stream: '4' };
    const choice = choiceMap[sourceType];
    if (!choice) {
      reject(new Error(`Unknown source type: ${sourceType}`));
      return;
    }

    const pythonPath = getPythonPath();
    log('CLIENT', `Starting: ${pythonPath} -u client.py (source=${sourceType})`);

    clientProcess = spawn(pythonPath, ['-u', 'client.py'], {
      cwd: BACKEND_DIR,
      env: { ...process.env },
      stdio: ['pipe', 'pipe', 'pipe'],
    });

    let resolved = false;

    // Automate the interactive menu (sanitize target to prevent stdin injection)
    const safeTarget = target.replace(/[\r\n]/g, '');
    try {
      clientProcess.stdin.write(`${choice}\n`);
      clientProcess.stdin.write(`${safeTarget}\n`);
      clientProcess.stdin.end();
    } catch (e) {
      logError('CLIENT', 'stdin write error', e);
    }

    clientProcess.stdout.on('data', (data) => {
      const text = data.toString();
      process.stdout.write(`${ts()} [CLIENT] ${text}`);
      if (!resolved && text.includes('[CAPTURE] Recording')) {
        resolved = true;
        resolve();
      }
      // Surface client errors (e.g. permission failures) to the UI
      if (text.includes('[ERROR]')) {
        const errorLine = text.split('\n').find(l => l.includes('[ERROR]')) || text;
        const detail = errorLine.replace(/^\[ERROR]\s*/, '').trim();
        sendErrorToRenderer('Capture error', detail);
        // Common cause: Screen Recording not granted — open System Settings
        if (detail.toLowerCase().includes('permission') || detail.toLowerCase().includes('capture')) {
          sendErrorToRenderer(
            'Screen Recording may be required',
            'Grant Screen Recording in System Settings > Privacy & Security > Screen & System Audio Recording, then restart the app.'
          );
          shell.openExternal('x-apple.systempreferences:com.apple.preference.security?Privacy_ScreenCapture');
        }
      }
    });

    clientProcess.stderr.on('data', (data) => {
      process.stderr.write(`${ts()} [CLIENT:ERR] ${data.toString()}`);
    });

    clientProcess.once('close', (code) => {
      log('CLIENT', `Exited with code ${code}`);
      clientProcess = null;
      setTrayActive(false);
      if (popupWindow && !popupWindow.isDestroyed()) {
        popupWindow.webContents.send('session-status', 'stopped');
      }
      // Disconnect SSE on client crash
      disconnectSSE();
      if (code !== 0 && code !== null) {
        sendErrorToRenderer('Client crashed', `Client process exited with code ${code}`);
      }
      if (!resolved) {
        resolved = true;
        resolve(); // resolve even on failure so UI can update
      }
    });

    // Timeout after 120s (client waits up to ~30s for session active before capture starts)
    setTimeout(() => {
      if (!resolved) {
        resolved = true;
        resolve();
      }
    }, 120000);
  });
}

function stopClient() {
  return new Promise((resolve) => {
    const proc = clientProcess;
    if (!proc) {
      resolve();
      return;
    }
    // Clear reference immediately to prevent double-stop
    clientProcess = null;
    log('CLIENT', 'Sending SIGINT...');

    const timeout = setTimeout(() => {
      log('CLIENT', 'Force killing...');
      try { proc.kill('SIGTERM'); } catch (e) { /* already dead */ }
      resolve();
    }, 10000);

    proc.once('close', () => {
      clearTimeout(timeout);
      resolve();
    });

    proc.kill('SIGINT');
  });
}

function stopBackend() {
  return new Promise((resolve) => {
    const proc = backendProcess;
    if (!proc) {
      resolve();
      return;
    }
    // Clear reference immediately to prevent double-stop
    backendProcess = null;
    log('BACKEND', 'Sending SIGTERM...');

    const timeout = setTimeout(() => {
      log('BACKEND', 'Force killing...');
      try { proc.kill('SIGKILL'); } catch (e) { /* already dead */ }
      resolve();
    }, 5000);

    proc.once('close', () => {
      clearTimeout(timeout);
      resolve();
    });

    proc.kill('SIGTERM');
  });
}

// ---------------------------------------------------------------------------
// SSE relay (main process -> renderer)
// ---------------------------------------------------------------------------

function connectSSE(lastId = 0) {
  disconnectSSE();

  const url = `${BACKEND_URL}/events`;
  log('SSE', `Connecting to ${url} (last-id=${lastId})...`);

  const options = {
    headers: { 'Last-Event-ID': String(lastId) },
  };

  sseRequest = http.get(url, options, (res) => {
    if (res.statusCode !== 200) {
      logError('SSE', `Bad status: ${res.statusCode}`);
      sendErrorToRenderer('SSE connection failed', `Backend returned status ${res.statusCode}`);
      scheduleSSEReconnect(lastId);
      return;
    }

    log('SSE', 'Connected.');
    // Reset backoff on successful connection
    sseBackoffDelay = SSE_BACKOFF_INITIAL;

    let buffer = '';
    let currentLastId = lastId;

    res.on('data', (chunk) => {
      buffer += chunk.toString();
      const messages = buffer.split('\n\n');
      buffer = messages.pop(); // keep incomplete message

      for (const msg of messages) {
        if (!msg.trim()) continue;

        let eventId = null;
        let eventData = null;

        for (const line of msg.split('\n')) {
          // Skip SSE comment lines (heartbeats)
          if (line.startsWith(':')) continue;

          if (line.startsWith('id: ')) {
            eventId = parseInt(line.slice(4), 10);
          } else if (line.startsWith('data: ')) {
            eventData = line.slice(6);
          }
        }

        if (eventData) {
          try {
            const parsed = JSON.parse(eventData);
            if (eventId) currentLastId = eventId;
            if (!isPaused && popupWindow && !popupWindow.isDestroyed()) {
              popupWindow.webContents.send('fact-check-alert', parsed);
            }
          } catch (e) {
            logError('SSE', 'Parse error', e);
          }
        }
      }
    });

    res.on('end', () => {
      log('SSE', 'Stream ended.');
      scheduleSSEReconnect(currentLastId);
    });

    res.on('error', (err) => {
      logError('SSE', 'Stream error', err);
      scheduleSSEReconnect(currentLastId);
    });
  });

  sseRequest.on('error', (err) => {
    logError('SSE', 'Connection error', err);
    scheduleSSEReconnect(lastId);
  });
}

function scheduleSSEReconnect(lastId) {
  if (sseReconnectTimer) return;
  log('SSE', `Reconnecting in ${sseBackoffDelay}ms...`);
  sseReconnectTimer = setTimeout(() => {
    sseReconnectTimer = null;
    connectSSE(lastId);
  }, sseBackoffDelay);
  // Increase backoff for next failure
  sseBackoffDelay = Math.min(sseBackoffDelay * SSE_BACKOFF_MULTIPLIER, SSE_BACKOFF_MAX);
}

function disconnectSSE() {
  if (sseReconnectTimer) {
    clearTimeout(sseReconnectTimer);
    sseReconnectTimer = null;
  }
  if (sseRequest) {
    sseRequest.destroy();
    sseRequest = null;
  }
  // Reset backoff on manual disconnect
  sseBackoffDelay = SSE_BACKOFF_INITIAL;
}

// ---------------------------------------------------------------------------
// Tray + Popover window
// ---------------------------------------------------------------------------

function loadTrayIcon(active) {
  const name = active ? 'trayActive.png' : 'trayTemplate.png';
  const icon = nativeImage.createFromPath(path.join(__dirname, 'icons', name));
  if (!active) icon.setTemplateImage(true);
  return icon;
}

function setTrayActive(active) {
  if (!tray) return;
  const icon = loadTrayIcon(active);
  tray.setImage(icon);
  tray.setToolTip(active ? 'Fact Checker (active)' : 'Fact Checker');
}

function createTray() {
  const icon = loadTrayIcon(false);
  tray = new Tray(icon);
  tray.setToolTip('Fact Checker');

  tray.on('click', () => {
    togglePopup();
  });
}

function createPopupWindow() {
  popupWindow = new BrowserWindow({
    width: 400,
    height: 580,
    show: false,
    frame: false,
    alwaysOnTop: true,
    skipTaskbar: true,
    resizable: false,
    movable: true,
    hasShadow: true,
    backgroundColor: '#1e1e1e',
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false,
    },
  });

  popupWindow.loadFile(path.join(__dirname, 'index.html'));
  popupWindow.setVisibleOnAllWorkspaces(true, { visibleOnFullScreen: true });

  // No blur-to-hide since the window is draggable/movable.
  // Users close via X button, Quit, or tray icon toggle.
}

function togglePopup() {
  if (!popupWindow) {
    log('APP', 'No popup window');
    return;
  }

  if (popupWindow.isVisible()) {
    popupWindow.hide();
    return;
  }

  // Position below the tray icon
  const bounds = tray.getBounds();
  const windowBounds = popupWindow.getBounds();

  let x, y;
  if (bounds && bounds.x > 0) {
    x = Math.round(bounds.x + bounds.width / 2 - windowBounds.width / 2);
    y = bounds.y + bounds.height + 4;
  } else {
    const display = screen.getPrimaryDisplay();
    x = display.workArea.x + display.workArea.width - windowBounds.width - 10;
    y = display.workArea.y + 4;
  }

  popupWindow.setPosition(x, y, false);
  popupWindow.show();
  popupWindow.focus();
}

// ---------------------------------------------------------------------------
// Permission handling (macOS)
// ---------------------------------------------------------------------------

async function ensurePermissions() {
  // Request microphone access (triggers macOS dialog if not yet granted)
  const micGranted = await systemPreferences.askForMediaAccess('microphone');
  log('PERMISSIONS', `Microphone: ${micGranted ? 'granted' : 'denied'}`);

  if (!micGranted) {
    sendErrorToRenderer('Microphone permission denied', 'Please grant Microphone access in System Settings > Privacy & Security > Microphone.');
    shell.openExternal('x-apple.systempreferences:com.apple.preference.security?Privacy_Microphone');
    return false;
  }

  // Screen Recording: macOS has no programmatic request API and
  // getMediaAccessStatus('screen') is unreliable in BOTH directions — it can
  // return 'granted' when access is actually denied, and 'denied' when it's
  // granted. We cannot block on it. Instead we log the status and let the
  // capture binary attempt to record; if it fails the error is surfaced to
  // the UI with guidance to grant Screen Recording.
  const screenStatus = systemPreferences.getMediaAccessStatus('screen');
  log('PERMISSIONS', `Screen Recording status: ${screenStatus} (not blocking — check is unreliable)`);

  return true;
}

// ---------------------------------------------------------------------------
// IPC handler cleanup (Improvement 6)
// ---------------------------------------------------------------------------

function removeIpcHandlers() {
  ipcMain.removeHandler('start-session');
  ipcMain.removeHandler('stop-session');
  ipcMain.removeHandler('get-stats');
  ipcMain.removeHandler('check-health');
  ipcMain.removeHandler('get-session-state');
  ipcMain.removeHandler('check-permissions');
  ipcMain.removeHandler('request-mic-permission');
  ipcMain.removeHandler('open-system-settings');
  ipcMain.removeHandler('pause-session');
  ipcMain.removeHandler('resume-session');
}

// ---------------------------------------------------------------------------
// IPC handlers — permissions (inspired by loom-electron reference)
// ---------------------------------------------------------------------------

ipcMain.handle('check-permissions', () => {
  if (process.platform !== 'darwin') {
    return { microphone: 'granted', screen: 'granted' };
  }
  const mic = systemPreferences.getMediaAccessStatus('microphone');
  const scr = systemPreferences.getMediaAccessStatus('screen');
  log('PERMISSIONS', `check: microphone=${mic}, screen=${scr}`);
  return { microphone: mic, screen: scr };
});

ipcMain.handle('request-mic-permission', async () => {
  if (process.platform !== 'darwin') return { granted: true };
  try {
    const granted = await systemPreferences.askForMediaAccess('microphone');
    log('PERMISSIONS', `Microphone request: ${granted ? 'granted' : 'denied'}`);
    return { granted };
  } catch (error) {
    logError('PERMISSIONS', 'Mic permission error', error);
    return { granted: false, error: error.message };
  }
});

ipcMain.handle('open-system-settings', (_event, type) => {
  const paneMap = {
    microphone: 'x-apple.systempreferences:com.apple.preference.security?Privacy_Microphone',
    screen: 'x-apple.systempreferences:com.apple.preference.security?Privacy_ScreenCapture',
  };
  const url = paneMap[type];
  if (url) {
    shell.openExternal(url);
    return { success: true };
  }
  return { success: false, error: 'Unknown permission type' };
});

// ---------------------------------------------------------------------------
// IPC handlers — session management
// ---------------------------------------------------------------------------

let _sessionOpInProgress = false;

function withTimeout(promise, ms, label) {
  return Promise.race([
    promise,
    new Promise((_, reject) =>
      setTimeout(() => reject(new Error(`${label} timed out after ${ms / 1000}s`)), ms)
    ),
  ]);
}

ipcMain.handle('start-session', async (_event, { sourceType, target }) => {
  if (_sessionOpInProgress) {
    return { success: false, error: 'Operation already in progress' };
  }
  _sessionOpInProgress = true;
  try {
    const permsOk = await ensurePermissions();
    if (!permsOk) {
      return { success: false, error: 'Screen Recording permission required' };
    }
    await withTimeout(startBackend(), 60000, 'Backend startup');

    // Health check with retry before proceeding (Improvement 5)
    const healthy = await waitForBackendReady();
    if (!healthy) {
      warn('BACKEND', 'Health check failed after retries, proceeding anyway');
    }

    if (!sseRequest) {
      connectSSE();
    }
    await withTimeout(startClient(sourceType, target), 120000, 'Client startup');
    isPaused = false;
    setTrayActive(true);
    return { success: true };
  } catch (err) {
    // Clean up any partially-started resources
    disconnectSSE();
    await stopClient();
    logError('IPC', 'start-session error', err);
    return { success: false, error: err.message };
  } finally {
    _sessionOpInProgress = false;
  }
});

ipcMain.handle('stop-session', async () => {
  if (_sessionOpInProgress) {
    return { success: false, error: 'Operation already in progress' };
  }
  _sessionOpInProgress = true;
  try {
    isPaused = false;
    setTrayActive(false);
    disconnectSSE();
    await stopClient();
    return { success: true };
  } catch (err) {
    logError('IPC', 'stop-session error', err);
    return { success: false, error: err.message };
  } finally {
    _sessionOpInProgress = false;
  }
});

ipcMain.handle('get-stats', async () => {
  try {
    return await fetchJSON(`${BACKEND_URL}/stats`);
  } catch (err) {
    return null;
  }
});

ipcMain.handle('check-health', async () => {
  try {
    return await fetchJSON(`${BACKEND_URL}/health`);
  } catch (err) {
    return null;
  }
});

ipcMain.handle('get-session-state', () => {
  return {
    backendRunning: backendProcess !== null,
    clientRunning: clientProcess !== null,
    sseConnected: sseRequest !== null,
    isPaused,
  };
});

// ---------------------------------------------------------------------------
// IPC handlers — pause/resume (Improvement 7)
// ---------------------------------------------------------------------------

ipcMain.handle('pause-session', () => {
  isPaused = true;
  disconnectSSE();
  log('IPC', 'Session paused');
  return { success: true };
});

ipcMain.handle('resume-session', () => {
  isPaused = false;
  connectSSE();
  log('IPC', 'Session resumed');
  return { success: true };
});

// ---------------------------------------------------------------------------
// IPC listeners (non-invoke)
// ---------------------------------------------------------------------------

ipcMain.on('hide-window', () => {
  app.quit();
});

ipcMain.on('quit-app', () => {
  app.quit();
});

ipcMain.on('open-external', (_event, url) => {
  if (typeof url !== 'string' || url.length > 2048) return;
  try {
    const parsed = new URL(url);
    // Only allow http/https, block localhost/private IPs
    if (!['https:', 'http:'].includes(parsed.protocol)) return;
    const host = parsed.hostname.toLowerCase();
    if (host === 'localhost' || host === '127.0.0.1' || host === '::1' || host.endsWith('.local')) return;
    shell.openExternal(url);
  } catch {
    // Invalid URL — ignore
  }
});

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function fetchJSON(url) {
  return new Promise((resolve, reject) => {
    http.get(url, (res) => {
      if (res.statusCode < 200 || res.statusCode >= 300) {
        res.resume(); // drain response
        reject(new Error(`HTTP ${res.statusCode}`));
        return;
      }
      let data = '';
      res.on('data', (chunk) => { data += chunk; });
      res.on('end', () => {
        try {
          resolve(JSON.parse(data));
        } catch (e) {
          reject(e);
        }
      });
    }).on('error', reject);
  });
}

// ---------------------------------------------------------------------------
// App lifecycle
// ---------------------------------------------------------------------------

app.whenReady().then(async () => {
  // Stale process cleanup (Improvement 2)
  cleanupStaleProcesses();

  // Request microphone permission BEFORE hiding dock — macOS requires a visible
  // dock presence to reliably show the permission dialog on fresh installs.
  if (process.platform === 'darwin') {
    const micStatus = systemPreferences.getMediaAccessStatus('microphone');
    log('PERMISSIONS', `Microphone status at launch: ${micStatus}`);
    if (micStatus !== 'granted') {
      log('PERMISSIONS', 'Requesting microphone access (dock visible)...');
      const granted = await systemPreferences.askForMediaAccess('microphone');
      log('PERMISSIONS', `Microphone request result: ${granted ? 'granted' : 'denied'}`);
    }
  }

  // Hide dock icon (tray-only app) — after permission dialog has been shown
  if (app.dock) {
    app.dock.hide();
  }

  createTray();
  createPopupWindow();

  log('APP', 'Fact Checker tray app ready. Click the tray icon to open.');
});

app.on('window-all-closed', (e) => {
  // No-op: tray-only app, don't quit
  e.preventDefault();
});

// Graceful shutdown (Improvement 1) — replaces old before-quit handler
app.on('before-quit', async (event) => {
  if (!isShuttingDown) {
    event.preventDefault();
    await shutdownApp();
    app.exit(0);
  }
});
