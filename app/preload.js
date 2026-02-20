const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('factChecker', {
  // Permissions
  checkPermissions: () => ipcRenderer.invoke('check-permissions'),
  requestMicPermission: () => ipcRenderer.invoke('request-mic-permission'),
  openSystemSettings: (type) => ipcRenderer.invoke('open-system-settings', type),

  // Session
  startSession: (opts) => ipcRenderer.invoke('start-session', opts),
  stopSession: () => ipcRenderer.invoke('stop-session'),
  getStats: () => ipcRenderer.invoke('get-stats'),
  checkHealth: () => ipcRenderer.invoke('check-health'),
  getSessionState: () => ipcRenderer.invoke('get-session-state'),

  // Pause/Resume (Improvement 7)
  pauseSession: () => ipcRenderer.invoke('pause-session'),
  resumeSession: () => ipcRenderer.invoke('resume-session'),

  // App
  quitApp: () => ipcRenderer.send('quit-app'),
  hideWindow: () => ipcRenderer.send('hide-window'),
  openExternal: (url) => ipcRenderer.send('open-external', url),

  // Events — return cleanup functions to prevent listener leaks (Improvement 4)
  onFactCheckAlert: (cb) => {
    const handler = (_event, data) => cb(data);
    ipcRenderer.on('fact-check-alert', handler);
    return () => ipcRenderer.removeListener('fact-check-alert', handler);
  },
  onSessionStatus: (cb) => {
    const handler = (_event, status) => cb(status);
    ipcRenderer.on('session-status', handler);
    return () => ipcRenderer.removeListener('session-status', handler);
  },
  onBackendStatus: (cb) => {
    const handler = (_event, status) => cb(status);
    ipcRenderer.on('backend-status', handler);
    return () => ipcRenderer.removeListener('backend-status', handler);
  },
  onErrorMessage: (cb) => {
    const handler = (_event, data) => cb(data);
    ipcRenderer.on('error-message', handler);
    return () => ipcRenderer.removeListener('error-message', handler);
  },
});
