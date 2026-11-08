const byId = (id) => document.getElementById(id);
const controls = {};
for (const id of [
  'oracleHost', 'interface', 'hardware', 'output', 'metadata', 'assets',
  'useRemoteOracle', 'serverUrl', 'workerKey', 'settingsSave', 'settingsStatus',
  'userSelect', 'userSelectLabel', 'uploadSaveSelect', 'uploadSaveSelectLabel', 'confirmUploadBtn', 'sourcePort', 'dataPort', 'discoveryPort',
  'downloadBtn', 'downloadSelectedBtn', 'uploadBtn', 'finishBtn', 'cancelBtn', 'powerBtn', 'status', 'entries', 'appEntries', 'profileEntries', 'mediaEntries', 'otherEntries', 'log',
  'consoleSelect', 'discoverBtn', 'manualHost', 'manualConnectBtn', 'pairBtn', 'pairingState', 'preferIpv6', 'selectAllEntries', 'entryCount', 'powerModal',
  'pairingModal', 'pairingCancelBtn', 'uploadModal', 'uploadModalCloseBtn', 'externalSavePath', 'externalTitleId', 'uploadExternalBtn', 'chooseExternalSaveBtn',
]) controls[id] = byId(id);
let activeTransfer = null;
let availableUsers = [];
let availableEntries = [];
let requestedDownloadIds = [];
let pendingUploadOptions = null;
let browserHost = null;
let pendingBrowserHost = null;
let dataTransferArmedHost = null;
let browseSessionActive = false;
let profileRoot = null;
const selectedEntryIds = new Set();
const downloadedEntryIds = new Set();

function options() {
  const clientHost = controls.consoleSelect.value;
  return {
    oracleHost: controls.oracleHost.value.trim(),
    oraclePort: 9022,
    useRemoteOracle: controls.useRemoteOracle.checked,
    serverUrl: controls.serverUrl.value.trim(),
    workerKey: controls.workerKey.value.trim(),
    clientHost,
    hostId: controls.consoleSelect.selectedOptions[0]?.dataset.hostId || '',
    systemVersion: controls.consoleSelect.selectedOptions[0]?.dataset.firmware || '',
    interface: controls.interface.value.trim(),
    hardware: Number(controls.hardware.value || 0),
    output: controls.output.value.trim() || 'dt_downloads',
    metadata: controls.metadata.value.trim(),
    assets: controls.assets.value.trim() || controls.output.value.trim() || 'dt_downloads',
    titleId: controls.externalTitleId.value.trim().toUpperCase(),
    userId: controls.userSelect.value,
    userName: controls.userSelect.selectedOptions[0]?.dataset.name || 'user',
    sourcePort: Number(controls.sourcePort.value || 1004),
    dataPort: Number(controls.dataPort.value || 1005),
    discoveryPort: Number(controls.discoveryPort.value || 1006),
    preferIpv6: controls.preferIpv6.checked,
    skipLaunch: dataTransferArmedHost === clientHost && !controls.preferIpv6.checked,
    skipPower: dataTransferArmedHost === clientHost && !controls.preferIpv6.checked,
  };
}

function formatFirmware(value) {
  const text = String(value || 'unknown');
  return /^\d{8}$/.test(text) ? `${Number(text.slice(0, 2))}.${text.slice(2, 4)}` : text;
}

function profilePath(root, child) {
  const separator = root.includes('\\') ? '\\' : '/';
  return `${root.replace(/[\\/]+$/, '')}${separator}${child}`;
}

function applyConsoleProfile() {
  const root = profileRoot || 'dt_downloads';
  const selected = controls.consoleSelect.selectedOptions[0];
  const identity = selected?.dataset.hostId || selected?.value || 'default';
  const profile = identity.replace(/[^A-Za-z0-9_.-]/g, '_');
  controls.output.value = profilePath(root, profile);
  controls.assets.value = controls.output.value;
  controls.metadata.value = '';
  refreshPairingStatus();
}

async function refreshPairingStatus() {
  const selected = controls.consoleSelect.selectedOptions[0];
  const identity = { hostId: selected?.dataset.hostId || '', systemVersion: selected?.dataset.firmware || '' };
  const supported = window.saveTransfer.pairingAvailable && Boolean(selected?.value);
  controls.pairingState.classList.remove('paired');
  controls.pairBtn.disabled = !supported || Boolean(activeTransfer) || browseSessionActive;
  controls.pairingState.textContent = supported
    ? 'Checking whether this PS5 has been paired…'
    : 'Discover a PS5 to check pairing.';
  if (!supported) return;
  try {
    const paired = await window.saveTransfer.pairingStatus(identity);
    if (controls.consoleSelect.selectedOptions[0] !== selected) return;
    controls.pairingState.textContent = paired ? 'Paired!' : 'Not paired yet. Pair this PS5 to continue.';
    controls.pairingState.classList.toggle('paired', paired);
  } catch (error) {
    if (controls.consoleSelect.selectedOptions[0] === selected) {
      controls.pairingState.textContent = `Could not check pairing: ${error.message}`;
    }
  }
}

function updateSelection() {
  const count = selectedEntryIds.size;
  controls.entryCount.textContent = count
    ? `${count} of ${availableEntries.length} save(s) selected`
    : `${availableEntries.length} save(s); none selected`;
  controls.selectAllEntries.checked = count > 0 && count === availableEntries.length;
  controls.selectAllEntries.indeterminate = count > 0 && count < availableEntries.length;
  controls.selectAllEntries.disabled = Boolean(activeTransfer) || !availableEntries.length;
  controls.downloadSelectedBtn.disabled = Boolean(activeTransfer) || count === 0;
  controls.finishBtn.hidden = !browseSessionActive || Boolean(activeTransfer);
  for (const checkbox of document.querySelectorAll('.entry-check')) checkbox.disabled = Boolean(activeTransfer);
  for (const button of document.querySelectorAll('.row-download')) button.disabled = Boolean(activeTransfer);
}

function busy(value) {
  controls.downloadBtn.disabled = value;
  controls.uploadBtn.disabled = value;
  controls.pairBtn.disabled = !window.saveTransfer.pairingAvailable || value || browseSessionActive;
  controls.cancelBtn.disabled = !value;
  controls.consoleSelect.disabled = value || browseSessionActive;
  controls.discoverBtn.disabled = value || browseSessionActive;
  if (!value) {
    hidePowerModal();
    hideUploadModal();
  }
  updateSelection();
}

function status(message) {
  controls.status.textContent = message;
}

function appendLog(message) {
  controls.log.textContent += `${message}\n`;
  controls.log.scrollTop = controls.log.scrollHeight;
}

function showPowerModal() {
  if (!controls.powerModal.open) controls.powerModal.showModal();
}

function hidePowerModal() {
  if (controls.powerModal.open) controls.powerModal.close();
}

function showPairingModal() {
  if (!controls.pairingModal.open) controls.pairingModal.showModal();
}

function hidePairingModal() {
  if (controls.pairingModal.open) controls.pairingModal.close();
}

function showUploadModal() {
  if (!controls.uploadModal.open) controls.uploadModal.showModal();
}

function hideUploadModal() {
  if (controls.uploadModal.open) controls.uploadModal.close();
}

function renderEntryTable(container, entries, emptyText) {
  container.replaceChildren();
  if (!entries.length) {
    container.innerHTML = `<tr><td colspan="5" class="empty">${emptyText}</td></tr>`;
    return;
  }
  const userNames = new Map(availableUsers.map((user) => [user.id.toLowerCase(), user.name]));
  for (const entry of entries) {
    const row = document.createElement('tr');

    const selectCell = document.createElement('td');
    const checkbox = document.createElement('input');
    checkbox.type = 'checkbox';
    checkbox.className = 'entry-check';
    checkbox.checked = selectedEntryIds.has(entry.id);
    checkbox.setAttribute('aria-label', `Select ${entry.name || entry.title}`);
    checkbox.addEventListener('change', () => {
      if (checkbox.checked) selectedEntryIds.add(entry.id);
      else selectedEntryIds.delete(entry.id);
      updateSelection();
    });
    selectCell.appendChild(checkbox);
    row.appendChild(selectCell);

    const titleCell = document.createElement('td');
    const name = document.createElement('span');
    name.className = 'title-name';
    name.textContent = entry.name || 'Unknown title';
    const titleId = document.createElement('span');
    titleId.className = 'title-id';
    titleId.textContent = `${entry.title} · ${entry.kind || 'save'} · entry ${entry.id}`;
    titleCell.append(name, titleId);
    row.appendChild(titleCell);

    const ownerCell = document.createElement('td');
    const ownerName = userNames.get(String(entry.owner).toLowerCase());
    ownerCell.textContent = ownerName ? `${ownerName} (${entry.owner})` : entry.owner;
    row.appendChild(ownerCell);

    const sizeCell = document.createElement('td');
    sizeCell.textContent = (entry.size / 0x100000).toFixed(1);
    row.appendChild(sizeCell);

    const actionCell = document.createElement('td');
    const download = document.createElement('button');
    download.type = 'button';
    download.className = 'row-download';
    download.textContent = downloadedEntryIds.has(entry.id) ? 'Download again' : 'Download';
    download.addEventListener('click', () => startDownload([entry.id]));
    actionCell.appendChild(download);
    if (downloadedEntryIds.has(entry.id)) {
      const downloaded = document.createElement('span');
      downloaded.className = 'downloaded';
      downloaded.textContent = 'Downloaded';
      actionCell.appendChild(downloaded);
    }
    row.appendChild(actionCell);
    container.appendChild(row);
  }
}

function renderEntries() {
  const saves = availableEntries.filter((entry) => entry.kind === 'save');
  const others = availableEntries.filter((entry) => entry.kind !== 'save');
  renderEntryTable(controls.entries, saves, 'No save entries were found.');
  byId('otherBlock').hidden = !others.length;
  for (const [kind, block, control] of [
    ['app', 'appBlock', controls.appEntries],
    ['profile', 'profileBlock', controls.profileEntries],
    ['media', 'mediaBlock', controls.mediaEntries],
    ['other', 'miscBlock', controls.otherEntries],
  ]) {
    const entries = others.filter((entry) => entry.kind === kind);
    byId(block).hidden = !entries.length;
    renderEntryTable(control, entries, `No ${kind} entries were found.`);
  }
  updateSelection();
}

async function browseSaves(forUpload = false) {
  if (!forUpload && browseSessionActive) {
    status('The download session is already open. Select saves from the browser.');
    return;
  }
  const current = options();
  if (!current.clientHost) {
    status('Discover and select a console first.');
    return;
  }
  pendingBrowserHost = current.clientHost;
  activeTransfer = forUpload ? 'upload-metadata' : 'browse';
  busy(true);
  status('Step 1/3: downloading the console save list.');
  if (!forUpload) {
    byId('saveBrowser').hidden = true;
    controls.entries.innerHTML = '<tr><td colspan="5" class="empty">Downloading metadata…</td></tr>';
  }
  try {
    await window.saveTransfer.download({
      ...current,
      metadataOnly: forUpload,
      waitForSelection: !forUpload,
      entryIds: [],
    });
  } catch (error) {
    status(`Could not browse saves: ${error.message}`);
    activeTransfer = null;
    busy(false);
  }
}

async function startDownload(entryIds) {
  if (!entryIds.length) {
    status('Select at least one save to download.');
    return;
  }
  if (!browseSessionActive) {
    status('The download session is not open. Browse the console again first.');
    return;
  }
  requestedDownloadIds = entryIds;
  activeTransfer = 'download';
  busy(true);
  status(`Downloading ${entryIds.length} selected save(s) on the open Data Transfer session.`);
  try {
    await window.saveTransfer.select(entryIds);
  } catch (error) {
    status(`Could not start download: ${error.message}`);
    activeTransfer = null;
    browseSessionActive = false;
    busy(false);
  }
}

async function startUploadSource() {
  const selectedUser = controls.userSelect.selectedOptions[0];
  const entryIds = [...controls.uploadSaveSelect.selectedOptions].map((option) => Number(option.value));
  if (!selectedUser || !entryIds.length) {
    status('Select one user and at least one downloaded entry before continuing.');
    return;
  }
  const uploadKinds = new Set(entryIds.map((id) => {
    const entry = availableEntries.find((candidate) => candidate.id === id);
    return entry?.kind === 'save' ? 'save' : 'other';
  }));
  if (uploadKinds.size !== 1) {
    status('Upload saves and other content in separate batches.');
    return;
  }
  const current = {
    ...(pendingUploadOptions || options()),
    userId: selectedUser.value,
    userName: selectedUser.dataset.name || 'user',
    entryIds,
    uploadKind: uploadKinds.values().next().value,
  };
  activeTransfer = 'upload';
  showUploadModal();
  busy(true);
  controls.confirmUploadBtn.hidden = true;
  controls.userSelectLabel.hidden = true;
  controls.uploadSaveSelectLabel.hidden = true;
  status(`Step 1/3: building metadata for ${entryIds.length} entries.`);
  try {
    await window.saveTransfer.upload(current);
  } catch (error) {
    status(`Could not start upload: ${error.message}`);
    activeTransfer = null;
    busy(false);
  }
}

function showUploadConfirmation() {
  controls.userSelect.replaceChildren();
  for (const user of availableUsers) {
    const option = document.createElement('option');
    option.value = user.id;
    option.dataset.name = user.name;
    option.textContent = `${user.name} (${user.id})`;
    controls.userSelect.appendChild(option);
  }
  controls.uploadSaveSelect.replaceChildren();
  const downloadedEntries = availableEntries.filter((entry) => entry.downloaded);
  for (const entry of downloadedEntries) {
    const option = document.createElement('option');
    option.value = entry.id;
    option.textContent = `${entry.name || entry.title} (${entry.title}) — ${entry.owner} — entry ${entry.id}`;
    controls.uploadSaveSelect.appendChild(option);
  }
  if (!availableUsers.length || !downloadedEntries.length) {
    controls.userSelectLabel.hidden = true;
    controls.uploadSaveSelectLabel.hidden = true;
    controls.confirmUploadBtn.hidden = true;
    status('No downloaded files are available. Download content before uploading it.');
    return;
  }
  controls.userSelectLabel.hidden = false;
  controls.uploadSaveSelectLabel.hidden = false;
  controls.confirmUploadBtn.hidden = false;
  status('Select one user and one or more entries.');
}

async function startUpload() {
  const current = options();
  if (!current.clientHost) {
    status('Discover and select a console first.');
    return;
  }
  byId('uploadDestination').hidden = false;
  if (browseSessionActive) {
    await window.saveTransfer.cancel();
    browseSessionActive = false;
    dataTransferArmedHost = null;
  }
  current.entryIds = [];
  pendingUploadOptions = current;
  const hasExplicitMetadata = Boolean(current.metadata);
  const previousMetadata = current.metadata || `${current.output}/metadata.db`;
  try {
    const inventory = await window.saveTransfer.inventory(previousMetadata);
    availableUsers = inventory.users;
    availableEntries = inventory.entries;
    if (!availableUsers.length) throw new Error('the metadata contains no users');
    current.metadata = previousMetadata;
    controls.metadata.value = previousMetadata;
    showUploadConfirmation();
    return;
  } catch (error) {
    if (hasExplicitMetadata) {
      status(`Could not read metadata inventory: ${error.message}`);
      return;
    }
    appendLog(`[metadata] no usable previous metadata at ${previousMetadata}; fetching from console`);
  }
  pendingBrowserHost = current.clientHost;
  activeTransfer = 'upload-metadata';
  busy(true);
  status('Step 1/3: fetching the client user and save list before upload.');
  try {
    await window.saveTransfer.download({ ...current, metadataOnly: true, entryIds: [] });
  } catch (error) {
    status(`Could not fetch metadata: ${error.message}`);
    activeTransfer = null;
    busy(false);
  }
}

function updateOracleMode() {
  const remote = controls.useRemoteOracle.checked;
  controls.oracleHost.disabled = remote;
  controls.serverUrl.disabled = !remote;
  controls.workerKey.disabled = !remote;
}
controls.useRemoteOracle.addEventListener('change', updateOracleMode);
updateOracleMode();

for (const tab of document.querySelectorAll('.tab')) {
  tab.addEventListener('click', () => {
    for (const other of document.querySelectorAll('.tab')) other.classList.toggle('active', other === tab);
    byId('transferTab').hidden = tab.dataset.tab !== 'transfer';
    byId('settingsTab').hidden = tab.dataset.tab !== 'settings';
  });
}

controls.discoverBtn.addEventListener('click', async () => {
  controls.discoverBtn.disabled = true;
  status('Discovering consoles with SRCH…');
  try {
    const consoles = await window.saveTransfer.discover(options());
    controls.consoleSelect.replaceChildren();
    for (const console of consoles) {
      const option = document.createElement('option');
      option.value = console.host;
      option.dataset.hostId = console.hostId;
      option.dataset.firmware = console.firmware;
      option.textContent = `${console.name} — ${console.host} (${formatFirmware(console.firmware)})`;
      controls.consoleSelect.appendChild(option);
    }
    if (!consoles.length) {
      const option = document.createElement('option');
      option.value = '';
      option.textContent = 'No consoles found';
      controls.consoleSelect.appendChild(option);
    }
    applyConsoleProfile();
    status(consoles.length
      ? `Found ${consoles.length} console(s). Each console uses its own metadata profile.`
      : 'No consoles found. Check the network and firewall.');
  } catch (error) {
    status(`Discovery failed: ${error.message}`);
  } finally {
    controls.discoverBtn.disabled = false;
  }
});

controls.manualConnectBtn.addEventListener('click', async () => {
  const host = controls.manualHost.value.trim();
  if (!/^\d{1,3}(?:\.\d{1,3}){3}$/.test(host)) {
    status('Enter a valid PS5 IPv4 address.');
    return;
  }
  controls.manualConnectBtn.disabled = true;
  status(`Contacting PS5 at ${host}…`);
  try {
    const consoles = await window.saveTransfer.discover({ ...options(), targetHost: host });
    if (!consoles.length) {
      status(`No PS5 responded at ${host}. Check the address and network.`);
      return;
    }
    controls.consoleSelect.replaceChildren();
    for (const console of consoles) {
      const option = document.createElement('option');
      option.value = console.host;
      option.dataset.hostId = console.hostId;
      option.dataset.firmware = console.firmware;
      option.textContent = `${console.name} — ${console.host} (${formatFirmware(console.firmware)})`;
      controls.consoleSelect.appendChild(option);
    }
    applyConsoleProfile();
    status(`Using ${consoles[0].name} at ${host} (${formatFirmware(consoles[0].firmware)}).`);
  } catch (error) {
    status(`Could not use ${host}: ${error.message}`);
  } finally {
    controls.manualConnectBtn.disabled = false;
  }
});

controls.settingsSave.addEventListener('click', async () => {
  const current = options();
  await window.saveTransfer.saveSettings({ ...current, output: profileRoot || current.output });
  controls.settingsStatus.textContent = 'Settings saved to config.ini.';
});

async function loadSettings() {
  const settings = await window.saveTransfer.getSettings();
  for (const [name, value] of Object.entries(settings)) {
    const control = controls[name];
    if (!control) continue;
    if (control.type === 'checkbox') control.checked = Boolean(value);
    else control.value = String(value ?? '');
  }
  profileRoot = controls.output.value || 'dt_downloads';
  applyConsoleProfile();
  updateOracleMode();
}
controls.consoleSelect.addEventListener('change', applyConsoleProfile);
loadSettings();

controls.pairBtn.addEventListener('click', async () => {
  const current = options();
  if (!current.clientHost) {
    status('Discover and select the PS5 first.');
    return;
  }
  activeTransfer = 'pair';
  showPairingModal();
  busy(true);
  status('Starting the pairing listener. Follow the instructions in the pairing window.');
  try {
    await window.saveTransfer.pair(current);
  } catch (error) {
    activeTransfer = null;
    hidePairingModal();
    busy(false);
    status(`Could not start pairing: ${error.message}`);
  }
});

controls.pairingCancelBtn.addEventListener('click', async () => {
  await window.saveTransfer.cancel();
  activeTransfer = null;
  hidePairingModal();
  busy(false);
  status('Pairing cancelled.');
});

controls.uploadModalCloseBtn.addEventListener('click', hideUploadModal);

controls.downloadBtn.addEventListener('click', () => browseSaves(false));
controls.downloadSelectedBtn.addEventListener('click', () => startDownload([...selectedEntryIds]));
controls.selectAllEntries.addEventListener('change', () => {
  selectedEntryIds.clear();
  if (controls.selectAllEntries.checked) {
    for (const entry of availableEntries) selectedEntryIds.add(entry.id);
  }
  renderEntries();
});
controls.uploadBtn.addEventListener('click', startUpload);
controls.confirmUploadBtn.addEventListener('click', startUploadSource);
controls.chooseExternalSaveBtn.addEventListener('click', async () => {
  const selected = await window.saveTransfer.chooseExternalSave();
  if (selected) controls.externalSavePath.value = selected;
});
controls.uploadExternalBtn.addEventListener('click', async () => {
  if (activeTransfer || browseSessionActive) return;
  const selectedUser = controls.userSelect.selectedOptions[0];
  const externalImage = controls.externalSavePath.value.trim();
  const titleId = controls.externalTitleId.value.trim().toUpperCase();
  if (!selectedUser || !externalImage || !/^[A-Z0-9]{9}$/.test(titleId)) {
    status('Select a target user, an encrypted save image, and a nine-character title ID.');
    return;
  }
  const current = {
    ...(pendingUploadOptions || options()),
    userId: selectedUser.value,
    userName: selectedUser.dataset.name || 'user',
    titleId,
    externalImage,
    entryIds: [],
    uploadKind: 'external',
  };
  activeTransfer = 'upload';
  showUploadModal();
  busy(true);
  status('Step 1/3: building metadata for the external save.');
  try {
    await window.saveTransfer.upload(current);
  } catch (error) {
    status(`Could not start external save upload: ${error.message}`);
    activeTransfer = null;
    busy(false);
  }
});
controls.finishBtn.addEventListener('click', async () => {
  if (!browseSessionActive || activeTransfer) return;
  if (!window.confirm('Close Data Transfer and reboot the console now?')) return;
  activeTransfer = 'finish';
  busy(true);
  status('Sending DisconnectPS5. The console will reboot.');
  try {
    await window.saveTransfer.finish();
  } catch (error) {
    activeTransfer = null;
    status(`Could not finish the download session: ${error.message}`);
    busy(false);
  }
});
controls.cancelBtn.addEventListener('click', async () => {
  await window.saveTransfer.cancel();
  activeTransfer = null;
  hidePairingModal();
  pendingUploadOptions = null;
  dataTransferArmedHost = null;
  browseSessionActive = false;
  busy(false);
  status('Transfer cancelled.');
});
controls.powerBtn.addEventListener('click', async () => {
  await window.saveTransfer.power();
  controls.powerBtn.hidden = true;
  status('Power signal sent. Continue the transfer on the client console.');
});

for (const name of ['output', 'metadata', 'assets']) {
  document.querySelector(`[data-action="${name}"]`).addEventListener('click', async () => {
    const path = await window.saveTransfer[`choose${name[0].toUpperCase()}${name.slice(1)}`]();
    if (!path) return;
    controls[name].value = path;
    if (name === 'output') {
      profileRoot = path;
      applyConsoleProfile();
    }
    if (name === 'metadata' && !controls.assets.value) controls.assets.value = path.replace(/[/\\][^/\\]+$/, '');
    if (name === 'assets' && !controls.metadata.value) {
      const parent = path.replace(/[/\\][^/\\]+$/, '');
      controls.metadata.value = `${parent}${parent.includes('\\') ? '\\' : '/'}metadata.db`;
    }
  });
}

window.saveTransfer.onEvent((event) => {
  if (event.type === 'log') appendLog(event.message);
  if (event.type === 'pairingStarted') {
    activeTransfer = 'pair';
    showPairingModal();
    busy(true);
  }
  if (event.type === 'paired') {
    hidePairingModal();
    activeTransfer = null;
    busy(false);
    controls.pairingState.textContent = 'Paired!';
    controls.pairingState.classList.add('paired');
    status(event.message);
  }
  if (event.type === 'powerRequired') showPowerModal();
  if (event.type === 'status') {
    status(event.message);
    if (event.step === 3) hidePowerModal();
    if (event.message === 'Transfer stopped.') {
      activeTransfer = null;
      browseSessionActive = false;
      busy(false);
    }
  }
  if (event.type === 'entries') {
    byId('saveBrowser').hidden = false;
    if (browserHost !== null && browserHost !== pendingBrowserHost) {
      selectedEntryIds.clear();
      downloadedEntryIds.clear();
    }
    browserHost = pendingBrowserHost;
    availableEntries = event.entries || [];
    availableUsers = event.users || [];
    for (const entry of availableEntries) {
      if (entry.downloaded) downloadedEntryIds.add(entry.id);
    }
    renderEntries();
  }
  if (event.type === 'ready') {
    browseSessionActive = true;
    dataTransferArmedHost = browserHost;
    activeTransfer = null;
    availableUsers = event.users || availableUsers;
    controls.metadata.value = `${options().output}/metadata.db`;
    status(event.message);
    busy(false);
  }
  if (event.type === 'downloaded') {
    for (const id of event.entryIds || requestedDownloadIds) downloadedEntryIds.add(id);
    controls.metadata.value = `${options().output}/metadata.db`;
    status(event.message);
    controls.powerBtn.hidden = true;
  }
  if (event.type === 'done' && event.metadataOnly) {
    const mode = activeTransfer;
    browseSessionActive = false;
    dataTransferArmedHost = event.consoleDisconnected ? null : browserHost;
    activeTransfer = null;
    availableUsers = event.users || [];
    controls.metadata.value = `${options().output}/metadata.db`;
    if (mode === 'upload-metadata') {
      pendingUploadOptions.metadata = controls.metadata.value;
      showUploadConfirmation();
    } else {
      status('Save list ready. Select saves to download, or select downloaded saves to upload.');
    }
    busy(false);
    return;
  }
  if (event.type === 'done') {
    const mode = activeTransfer;
    browseSessionActive = false;
    if (mode === 'finish') dataTransferArmedHost = null;
    activeTransfer = null;
    availableUsers = event.users || availableUsers;
    if (mode === 'download') {
      dataTransferArmedHost = browserHost;
      for (const id of event.entryIds || requestedDownloadIds) downloadedEntryIds.add(id);
      controls.metadata.value = `${options().output}/metadata.db`;
      renderEntries();
    }
    status(event.message);
    busy(false);
    controls.powerBtn.hidden = true;
  }
  if (event.type === 'error') {
    activeTransfer = null;
    hidePairingModal();
    browseSessionActive = false;
    dataTransferArmedHost = null;
    status(`Stopped: ${event.message}\nCheck the log and the current step, then retry.`);
    appendLog(`[error] ${event.message}`);
    busy(false);
    controls.powerBtn.hidden = true;
  }
});
