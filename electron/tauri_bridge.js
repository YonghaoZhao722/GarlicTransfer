(() => {
  if (!window.__TAURI__) return;
  const { invoke } = window.__TAURI__.core;
  const { listen } = window.__TAURI__.event;
  const { open } = window.__TAURI__.dialog;

  const fileDialog = (title, filters, directory = false) => open({
    title,
    directory,
    multiple: false,
    filters,
  });

  window.saveTransfer = {
    pairingAvailable: true,
    firewallAvailable: /Windows/i.test(navigator.userAgent),
    chooseOutput: () => fileDialog('Choose console profile root folder', [], true),
    chooseMetadata: () => fileDialog('Choose metadata.db', [{ name: 'SQLite metadata', extensions: ['db'] }]),
    chooseAssets: () => fileDialog('Choose upload save folder', [], true),
    chooseExternalSave: () => fileDialog('Choose encrypted save container', [
      { name: 'PS5 save containers', extensions: ['bin', 'img', 'save'] },
    ]),
    pair: (options) => invoke('pair', { options }),
    pairingStatus: (identity) => invoke('pairing_status', { identity }),
    download: (options) => invoke('download', { options }),
    select: (entryIds) => invoke('select', { entryIds }),
    finish: () => invoke('finish'),
    upload: (options) => invoke('upload', { options }),
    users: (metadataPath) => invoke('users', { metadataPath }),
    inventory: (metadataPath) => invoke('inventory', { metadataPath }),
    discover: (options) => invoke('discover', { options }),
    power: () => invoke('power'),
    cancel: () => invoke('cancel'),
    getSettings: () => invoke('get_settings'),
    saveSettings: (settings) => invoke('save_settings', { settings }),
    version: () => invoke('app_version'),
    runFirewallHelper: () => invoke('run_firewall_helper'),
    onEvent: (callback) => {
      const unlisten = listen('transfer:event', (event) => callback(event.payload));
      return () => unlisten.then((dispose) => dispose());
    },
  };
})();
