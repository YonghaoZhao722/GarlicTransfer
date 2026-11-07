const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('saveTransfer', {
  pairingAvailable: true,
  chooseOutput: () => ipcRenderer.invoke('dialog:output'),
  chooseMetadata: () => ipcRenderer.invoke('dialog:metadata'),
  chooseAssets: () => ipcRenderer.invoke('dialog:assets'),
  chooseExternalSave: () => ipcRenderer.invoke('dialog:external-save'),
  pair: (options) => ipcRenderer.invoke('transfer:pair', options),
  pairingStatus: (identity) => ipcRenderer.invoke('transfer:pair-status', identity),
  download: (options) => ipcRenderer.invoke('transfer:download', options),
  select: (entryIds) => ipcRenderer.invoke('transfer:select', entryIds),
  finish: () => ipcRenderer.invoke('transfer:finish'),
  upload: (options) => ipcRenderer.invoke('transfer:upload', options),
  users: (metadataPath) => ipcRenderer.invoke('transfer:users', metadataPath),
  inventory: (metadataPath) => ipcRenderer.invoke('transfer:inventory', metadataPath),
  discover: (options) => ipcRenderer.invoke('transfer:discover', options),
  power: () => ipcRenderer.invoke('transfer:power'),
  cancel: () => ipcRenderer.invoke('transfer:cancel'),
  getSettings: () => ipcRenderer.invoke('settings:get'),
  saveSettings: (settings) => ipcRenderer.invoke('settings:set', settings),
  onEvent: (callback) => {
    const listener = (_event, detail) => callback(detail);
    ipcRenderer.on('transfer:event', listener);
    return () => ipcRenderer.removeListener('transfer:event', listener);
  },
});
