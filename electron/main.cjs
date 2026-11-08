const { app, BrowserWindow, ipcMain, dialog } = require('electron');
const { spawn } = require('node:child_process');
const fs = require('node:fs');
const net = require('node:net');
const path = require('node:path');
const { startRemoteOracle, stopRemoteOracle } = require('./remote_oracle.cjs');
const { loadSettings, saveSettings } = require('./config.cjs');
const { titleName } = require('./titles.cjs');
const { pairingPrefixPath, existingPairingPrefix } = require('./pairing_store.cjs');
const { resourceRoot, oracleElf, workDir, pythonBin } = require('./paths.cjs');
let window;
let active = null;
let oracle = null;
let oracleRelay = null;

function send(type, detail = {}) {
  if (type === 'entries' && Array.isArray(detail.entries)) {
    detail = {
      ...detail,
      entries: detail.entries.map((entry) => ({ ...entry, name: titleName(entry.title) })),
    };
  }
  if (window && !window.isDestroyed()) {
    window.webContents.send('transfer:event', { type, ...detail });
  }
}

function stopOracle() {
  if (oracle && !oracle.destroyed) oracle.end();
  oracle = null;
  stopRemoteOracle(oracleRelay);
  oracleRelay = null;
}

function oracleReady(host, port) {
  return new Promise((resolve) => {
    const sock = net.createConnection({ host, port, timeout: 1200 });
    const done = (ready) => {
      sock.destroy();
      resolve(ready);
    };
    sock.once('connect', () => done(true));
    sock.once('timeout', () => done(false));
    sock.once('error', () => done(false));
  });
}

async function ensureOracle(options) {
  if (options.useRemoteOracle) {
    if (!options.workerKey) throw new Error('no server key set — check Oracle settings or disable the remote oracle');
    oracleRelay = await startRemoteOracle({
      serverUrl: options.serverUrl,
      workerKey: options.workerKey,
      onLog: (message) => send('log', { message }),
    });
    options.oracleHost = '127.0.0.1';
    options.oraclePort = oracleRelay.port;
    send('log', { message: `[oracle] using volunteer workers through ${options.serverUrl}` });
    return;
  }

  const host = options.oracleHost;
  const port = Number(options.oraclePort || 9022);
  if (await oracleReady(host, port)) {
    send('log', { message: `[oracle] using ${host}:${port}` });
    return;
  }
  if (oracle && !oracle.destroyed) {
    throw new Error('oracle listener is starting but not ready');
  }
  send('log', { message: `[oracle] deploying to ${host}:9021` });
  oracle = net.createConnection({ host, port: 9021 });
  oracle.on('error', (error) => send('log', { message: `[oracle] connection error: ${error.message}` }));
  oracle.on('data', (data) => send('log', { message: String(data).trim() }));
  fs.createReadStream(oracleElf).pipe(oracle);
  const deadline = Date.now() + 8000;
  while (Date.now() < deadline) {
    if (await oracleReady(host, port)) {
      send('log', { message: '[oracle] ready' });
      return;
    }
    if (oracle.destroyed) throw new Error('oracle payload exited before opening port 9022');
    await new Promise((resolve) => setTimeout(resolve, 200));
  }
  throw new Error('oracle did not open port 9022; check the jailbroken console');
}

function stopActive() {
  const session = active;
  active = null;
  if (session && session.powerTimer) clearTimeout(session.powerTimer);
  if (session && session.process && session.process.exitCode === null) {
    if (session.kind === 'upload' && session.process.stdin.writable) session.process.stdin.end();
    session.process.kill();
  }
  stopOracle();
}

function startDownload(options) {
  if (active) throw new Error('another transfer is already running');
  // The renderer never chooses a local credential file. Resolve it only from
  // the console identity reported by discovery, inside the main process.
  delete options.approvalPrefixPath;
  let paired = null;
  if (options.hostId && options.systemVersion) {
    paired = existingPairingPrefix(app.getPath('userData'), options);
    if (paired) options.approvalPrefixPath = paired;
  }
  const metadataPath = options.metadata || path.join(options.output || 'dt_downloads', 'metadata.db');
  if (!String(options.accountId || '').trim() && !paired && !fs.existsSync(metadataPath)) {
    startPairing(options);
    return;
  }
  active = { kind: 'download', completed: false, awaitingSelection: false, process: null };
  (async () => {
    await ensureOracle(options);
    const child = spawn(pythonBin(), ['-u', '-m', 'poc.dt_backend', 'download', '-'], {
      cwd: workDir,
      env: { ...process.env, PYTHONPATH: resourceRoot },
    });
    active.process = child;
    child.stdin.write(`${JSON.stringify(options)}\n`);
    let buffer = '';
    child.stdout.on('data', (chunk) => {
      buffer += chunk;
      while (active && buffer.includes('\n')) {
        const index = buffer.indexOf('\n');
        const line = buffer.slice(0, index);
        buffer = buffer.slice(index + 1);
        if (!line) continue;
        try {
          const event = JSON.parse(line);
          if (event.type === 'done') active.completed = true;
          if (event.type === 'ready') active.awaitingSelection = true;
          send(event.type, event);
        } catch {
          send('log', { message: line });
        }
      }
    });
    child.stderr.on('data', (data) => send('log', { message: String(data).trim() }));
    child.on('error', (error) => finish(error));
    child.on('close', (code) => {
      if (active && active.kind === 'download' && !active.completed && code !== 0) {
        finish(new Error(`download backend exited with status ${code}`));
      } else {
        finish();
      }
    });
  })().catch((error) => finish(error));
}

function startPairing(options) {
  if (active) throw new Error('another transfer is already running');
  if (net.isIP(options.clientHost) !== 4) throw new Error('Discover and select a PS5 IPv4 address first');
  const prefixPath = pairingPrefixPath(app.getPath('userData'), options);
  fs.mkdirSync(path.dirname(prefixPath), { recursive: true, mode: 0o700 });
  active = { kind: 'pair', completed: false, process: null };
  const session = active;
  send('pairingStarted');
  (async () => {
    await ensureOracle(options);
    if (active !== session) {
      if (!active) stopOracle();
      return;
    }
    const python = pythonBin();
    let command = python;
    let args = ['-u', '-m', 'poc.dt_pair', '-'];
    let environment = { ...process.env, PYTHONPATH: resourceRoot };
    if (process.platform === 'linux' && process.getuid() !== 0) {
      command = 'pkexec';
      args = [
        '/usr/bin/env', `PYTHONPATH=${resourceRoot}`,
        `DT_PAIRING_UID=${process.getuid()}`, `DT_PAIRING_GID=${process.getgid()}`,
        python, '-u', '-m', 'poc.dt_pair', '-',
      ];
      environment = process.env;
    }
    const child = spawn(command, args, {
      cwd: workDir,
      env: environment,
    });
    session.process = child;
    child.stdin.end(`${JSON.stringify({
      ps5Ip: options.clientHost,
      hostId: options.hostId,
      systemVersion: options.systemVersion,
      oracleHost: options.oracleHost,
      oraclePort: Number(options.oraclePort || 9022),
      prefixPath,
      duration: 300,
    })}\n`);
    let buffer = '';
    child.stdout.on('data', (chunk) => {
      buffer += chunk;
      while (active && buffer.includes('\n')) {
        const index = buffer.indexOf('\n');
        const line = buffer.slice(0, index);
        buffer = buffer.slice(index + 1);
        try {
          const event = JSON.parse(line);
          if (event.type === 'status') send('status', { message: String(event.message || '') });
          if (event.type === 'error') session.error = String(event.message || 'Pairing failed');
          if (event.type === 'paired') {
            if (!existingPairingPrefix(app.getPath('userData'), options)) {
              session.error = 'Pairing helper did not save a valid identifier';
            } else {
              session.completed = true;
            }
          }
        } catch {
          // Do not forward unrecognized helper output; it could contain request data.
          session.error = 'Pairing helper sent an unreadable response';
        }
      }
    });
    child.stderr.on('data', (data) => {
      send('log', { message: `[helper] ${String(data).trim()}` });
      session.error ||= 'Pairing helper failed';
    });
    child.on('error', (error) => {
      if (active === session) finish(error);
    });
    child.on('close', (code) => {
      if (active !== session) return;
      if (!session.completed || code !== 0) finish(new Error(session.error || `Pairing exited with status ${code}`));
      else {
        finish();
        send('paired', { message: 'Pairing saved for this PS5. Return to its home screen, then browse saves.' });
      }
    });
  })().catch((error) => {
    if (active === session) finish(error);
  });
}

function getConsoles(options) {
  return new Promise((resolve, reject) => {
    const { discoveryRoutes } = require('./discovery.cjs');
    const routes = net.isIP(options.targetHost) === 4
      ? [{ bind: '', target: options.targetHost }]
      : discoveryRoutes();
    const port = String(Number(options.discoveryPort) || 1006);
    const child = spawn(pythonBin(), [
      '-m', 'poc.dt_discovery', '--routes', JSON.stringify(routes), '--port', port,
    ], { cwd: workDir, env: { ...process.env, PYTHONPATH: resourceRoot } });
    let buffer = '';
    child.stdout.on('data', (data) => { buffer += data; });
    child.stderr.on('data', (data) => { buffer += data; });
    child.on('error', reject);
    child.on('close', (code) => {
      if (code !== 0) return reject(new Error(buffer.trim() || 'console discovery failed'));
      try {
        resolve(JSON.parse(buffer).consoles || []);
      } catch (error) {
        reject(error);
      }
    });
  });
}

function getUsers(metadataPath) {
  return new Promise((resolve, reject) => {
    const child = spawn(pythonBin(), ['-m', 'poc.dt_backend', 'users', metadataPath], { cwd: workDir, env: { ...process.env, PYTHONPATH: resourceRoot } });
    let buffer = '';
    child.stdout.on('data', (data) => { buffer += data; });
    child.stderr.on('data', (data) => { buffer += data; });
    child.on('error', reject);
    child.on('close', (code) => {
      if (code !== 0) return reject(new Error(buffer.trim() || 'could not read metadata users'));
      try {
        const event = JSON.parse(buffer.trim().split('\n').pop());
        resolve(event.users || []);
      } catch (error) {
        reject(error);
      }
    });
  });
}

function getInventory(metadataPath) {
  return new Promise((resolve, reject) => {
    const child = spawn(pythonBin(), ['-m', 'poc.dt_backend', 'inventory', metadataPath], { cwd: workDir, env: { ...process.env, PYTHONPATH: resourceRoot } });
    let buffer = '';
    child.stdout.on('data', (data) => { buffer += data; });
    child.stderr.on('data', (data) => { buffer += data; });
    child.on('error', reject);
    child.on('close', (code) => {
      if (code !== 0) return reject(new Error(buffer.trim() || 'could not read metadata inventory'));
      try {
        const event = JSON.parse(buffer.trim().split('\n').pop());
        resolve({
          users: event.users || [],
          entries: (event.entries || []).map((entry) => ({ ...entry, name: titleName(entry.title) })),
        });
      } catch (error) {
        reject(error);
      }
    });
  });
}

function createMetadata(options) {
  return new Promise((resolve, reject) => {
    fs.mkdirSync(options.output, { recursive: true });
    const generatedMetadata = path.join(options.output, 'generated-metadata.db');
    const generatedManifest = path.join(options.output, 'generated-upload-manifest.json');
    if (options.uploadKind === 'external') options.assets = path.join(options.output, 'external-save');
    const args = [
      '-m', 'poc.dt_metadata',
      '--output', generatedMetadata,
      '--user-id', options.userId,
      '--assets', options.assets,
      '--user-name', options.userName || 'user',
      '--manifest-output', generatedManifest,
    ];
    const sourceMetadata = options.metadata || path.join(options.output, 'metadata.db');
    if (options.uploadKind === 'external') {
      args.push('--external-image', options.externalImage, '--title-id', options.titleId || '', '--source', sourceMetadata);
    } else if (options.uploadKind === 'other') {
      args.push('--other', '--source', sourceMetadata);
    } else {
      args.push('--source', sourceMetadata);
    }
    const entryIds = Array.isArray(options.entryIds) ? options.entryIds : [];
    for (const entryId of entryIds) args.push('--entry-id', String(entryId));
    const child = spawn(pythonBin(), args, { cwd: workDir, env: { ...process.env, PYTHONPATH: resourceRoot } });
    let output = '';
    child.stdout.on('data', (data) => { output += data; });
    child.stderr.on('data', (data) => { output += data; });
    child.on('error', reject);
    child.on('close', (code) => {
      if (code !== 0) return reject(new Error(output.trim() || 'metadata generation failed'));
      options.metadata = generatedMetadata;
      options.uploadManifest = generatedManifest;
      send('log', { message: `[metadata] generated ${options.metadata} for ${entryIds.length || 1} save(s)` });
      resolve();
    });
  });
}

function startUpload(options) {
  if (active) throw new Error('another transfer is already running');
  active = { kind: 'upload', completed: false, process: null };
  (async () => {
    await createMetadata(options);
    await ensureOracle(options);
    const prefixPath = pairingPrefixPath(app.getPath('userData'), options);
    fs.mkdirSync(path.dirname(prefixPath), { recursive: true, mode: 0o700 });
    const sourceEnvironment = {
      DT_METADATA: path.resolve(options.metadata),
      DT_ASSETS: path.resolve(options.assets),
      DT_MANIFEST: path.resolve(options.uploadManifest),
      DT_UPLOAD_KIND: options.uploadKind || 'save',
      DT_LOG: path.join(path.resolve(options.output), 'dt-source.log'),
      DT_PAIRING_PREFIX: prefixPath,
      DT_EXPECTED_HOST_ID: String(options.hostId),
      DT_EXIT_ON_EOF: '1',
      DT_AUTO_POWER: '1',
      DT_PORT: String(options.sourcePort || 1004),
      DT_DATA_PORT: String(options.dataPort || 1005),
      DT_DISCOVERY_PORT: String(options.discoveryPort || 1006),
      // ensureOracle() has already resolved these: for the volunteer oracle it
      // points at the local relay it just started, otherwise at the configured
      // console.  dt_source used to fall back to its own 192.168.0.125:9022
      // default, so upload could never derive keys off the developer's LAN.
      DT_ORACLE_HOST: String(options.oracleHost || '127.0.0.1'),
      DT_ORACLE_PORT: String(options.oraclePort || 9022),
    };
    if (process.platform !== 'win32') {
      sourceEnvironment.DT_PAIRING_UID = String(process.getuid());
      sourceEnvironment.DT_PAIRING_GID = String(process.getgid());
    }
    if (options.advertiseAddress) {
      sourceEnvironment.DT_ADVERTISE = String(options.advertiseAddress);
    }
    if (options.systemVersion) {
      sourceEnvironment.DT_SYSTEM_VERSION = String(options.systemVersion);
    }
    const needsRoot = process.platform === 'linux' && process.getuid() !== 0 &&
      [sourceEnvironment.DT_PORT, sourceEnvironment.DT_DATA_PORT, sourceEnvironment.DT_DISCOVERY_PORT]
        .some((port) => Number(port) < 1024);
    const python = pythonBin();
    let command = python;
    let args = ['-u', '-m', 'poc.dt_source'];
    let environment = { ...process.env, PYTHONPATH: resourceRoot, ...sourceEnvironment };
    if (needsRoot) {
      command = 'pkexec';
      args = [
        '/usr/bin/env', `PYTHONPATH=${resourceRoot}`,
        ...Object.entries(sourceEnvironment).map(([name, value]) => `${name}=${value}`),
        python, '-u', '-m', 'poc.dt_source',
      ];
      environment = process.env;
      send('log', { message: '[privilege] authenticate to bind Data Transfer ports 1004–1006' });
    }
    const child = spawn(command, args, { cwd: workDir, env: environment });
    active.process = child;
    let buffer = '';
    child.stdout.on('data', (chunk) => {
      buffer += chunk;
      while (active && buffer.includes('\n')) {
        const index = buffer.indexOf('\n');
        const line = buffer.slice(0, index);
        buffer = buffer.slice(index + 1);
        if (!line) continue;
        send('log', { message: line });
        if (line.includes("type 'power'")) {
          send('status', { step: 3, message: 'Sending the source power signal automatically in 1 second.' });
        }
        if (line.toLowerCase().includes('cmd=0xb3')) {
          active.transferFinished = true;
          send('status', { step: 3, message: 'Save data sent. Waiting for the console to disconnect.' });
        }
        if (active.transferFinished && line.toLowerCase().includes('cmd=0x94')) {
          active.completed = true;
          send('done', { message: 'Save upload completed. The client may reboot to finish.' });
          stopActive();
          return;
        }
      }
    });
    child.stderr.on('data', (data) => send('log', { message: String(data).trim() }));
    child.on('error', (error) => finish(error));
    child.on('close', (code) => {
      if (active && active.kind === 'upload' && active.transferFinished && !active.completed && code === 0) {
        active.completed = true;
        send('done', { message: 'Save upload completed. The console closed the transfer session.' });
      }
      if (active && active.kind === 'upload' && !active.completed && code !== 0) {
        finish(new Error(`source emulator exited with status ${code}`));
      } else {
        finish();
      }
    });
  })().catch((error) => finish(error));
}

function finish(error) {
  const current = active;
  stopActive();
  if (error) send('error', { message: error.message });
  else if (current && !current.completed) send('status', { message: 'Transfer stopped.' });
}

app.whenReady().then(() => {
  window = new BrowserWindow({
    width: 1120,
    height: 860,
    minWidth: 900,
    minHeight: 680,
    backgroundColor: '#11151b',
    webPreferences: {
      preload: path.join(__dirname, 'preload.cjs'),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
    },
  });
  window.loadFile(path.join(__dirname, 'index.html'));
});

app.on('before-quit', stopActive);
app.on('window-all-closed', () => {
  if (process.platform !== 'darwin') app.quit();
});

ipcMain.handle('dialog:output', async () => {
  const result = await dialog.showOpenDialog(window, { properties: ['openDirectory', 'createDirectory'] });
  return result.canceled ? null : result.filePaths[0];
});

ipcMain.handle('dialog:metadata', async () => {
  const result = await dialog.showOpenDialog(window, {
    properties: ['openFile'],
    filters: [{ name: 'SQLite metadata', extensions: ['db'] }, { name: 'All files', extensions: ['*'] }],
  });
  return result.canceled ? null : result.filePaths[0];
});

ipcMain.handle('dialog:assets', async () => {
  const result = await dialog.showOpenDialog(window, { properties: ['openDirectory'] });
  return result.canceled ? null : result.filePaths[0];
});

ipcMain.handle('dialog:external-save', async () => {
  const result = await dialog.showOpenDialog(window, {
    properties: ['openFile'],
    filters: [{ name: 'PS5 save containers', extensions: ['bin', 'img', 'save'] }, { name: 'All files', extensions: ['*'] }],
  });
  return result.canceled ? null : result.filePaths[0];
});

ipcMain.handle('transfer:download', (_event, options) => startDownload(options));
ipcMain.handle('transfer:pair', (_event, options) => startPairing(options));
ipcMain.handle('transfer:pair-status', (_event, identity) => {
  if (!identity?.hostId || !identity?.systemVersion) return false;
  return Boolean(existingPairingPrefix(app.getPath('userData'), identity));
});
ipcMain.handle('transfer:select', (_event, entryIds) => {
  if (!active || active.kind !== 'download' || !active.awaitingSelection || !active.process) {
    throw new Error('no download session is waiting for a selection');
  }
  if (!Array.isArray(entryIds) || !entryIds.length) throw new Error('select at least one save');
  active.awaitingSelection = false;
  active.process.stdin.write(`${JSON.stringify({ entryIds })}\n`);
});
ipcMain.handle('transfer:finish', () => {
  if (!active || active.kind !== 'download' || !active.awaitingSelection || !active.process) {
    throw new Error('no download session is ready to finish');
  }
  active.awaitingSelection = false;
  active.process.stdin.write(`${JSON.stringify({ action: 'finish' })}\n`);
});
ipcMain.handle('transfer:upload', (_event, options) => startUpload(options));
ipcMain.handle('transfer:users', (_event, metadataPath) => getUsers(metadataPath));
ipcMain.handle('transfer:inventory', (_event, metadataPath) => getInventory(metadataPath));
ipcMain.handle('app:version', () => app.getVersion());
ipcMain.handle('windows:firewall-helper', () => {
  if (process.platform !== 'win32') throw new Error('the firewall helper is only available on Windows');
  const helper = app.isPackaged
    ? path.join(path.dirname(app.getPath('exe')), 'Garlic-Firewall-Helper.bat')
    : path.join(__dirname, '..', 'Garlic-Firewall-Helper.bat');
  if (!fs.existsSync(helper)) throw new Error('Garlic-Firewall-Helper.bat is missing beside GarlicTransfer.exe');
  return new Promise((resolve, reject) => {
    const child = spawn(process.env.ComSpec || 'cmd.exe', [
      '/d', '/s', '/c', 'start', '', helper, '/attach', String(process.pid),
    ], { cwd: path.dirname(helper), detached: true, stdio: 'ignore', windowsHide: false });
    child.once('error', reject);
    child.once('spawn', () => {
      child.unref();
      resolve();
    });
  });
});
ipcMain.handle('transfer:discover', (_event, options) => getConsoles(options));
ipcMain.handle('transfer:power', () => {
  if (active && active.kind === 'upload' && active.process && active.process.exitCode === null) {
    active.process.stdin.write('power\n');
  }
});
ipcMain.handle('transfer:cancel', stopActive);
ipcMain.handle('settings:get', () => loadSettings());
ipcMain.handle('settings:set', (_event, settings) => saveSettings(settings));
