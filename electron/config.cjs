const fs = require('node:fs');
const path = require('node:path');

const { app } = require('electron');
const { defaultDownloads } = require('./paths.cjs');
const configPath = app.isPackaged
  ? path.join(app.getPath('userData'), 'config.ini')
  : path.join(__dirname, '..', 'config.ini');
const fields = [
  'useRemoteOracle', 'serverUrl', 'workerKey', 'oracleHost', 'oraclePort',
  'interface', 'hardware', 'output', 'metadata', 'assets',
  'userId', 'titleId', 'entryId', 'userName', 'sourcePort', 'dataPort', 'discoveryPort', 'preferIpv6',
];
const defaults = {
  useRemoteOracle: true,
  serverUrl: 'https://garlicsaves.com',
  workerKey: 'd398596b0dd4ad811173e3324a94d1cc90246a1c7f75b405a5696eed200b353e',
  oracleHost: '192.168.0.125',
  oraclePort: 9022,
  interface: 'wlp4s0',
  hardware: 0,
  output: defaultDownloads,
  metadata: '',
  assets: defaultDownloads,
  userId: '',
  titleId: '',
  entryId: 137,
  userName: 'user',
  sourcePort: 1004,
  dataPort: 1005,
  discoveryPort: 1006,
  preferIpv6: false,
};

function parse(text) {
  const result = {};
  for (const line of text.split(/\r?\n/)) {
    const trimmed = line.trim();
    if (!trimmed || trimmed.startsWith(';') || trimmed.startsWith('#')) continue;
    const index = trimmed.indexOf('=');
    if (index < 0) continue;
    const key = trimmed.slice(0, index).trim();
    const value = trimmed.slice(index + 1).trim();
    if (fields.includes(key)) result[key] = value;
  }
  return result;
}

function loadSettings() {
  let stored = {};
  try {
    stored = parse(fs.readFileSync(configPath, 'utf8'));
  } catch (error) {
    if (error.code !== 'ENOENT') throw error;
  }
  return {
    ...defaults,
    ...stored,
    useRemoteOracle: stored.useRemoteOracle
      ? stored.useRemoteOracle === 'true'
      : defaults.useRemoteOracle,
    workerKey: String(stored.workerKey || '').trim() || defaults.workerKey,
    oraclePort: Number(stored.oraclePort || defaults.oraclePort),
    hardware: Number(stored.hardware || defaults.hardware),
    entryId: Number(stored.entryId || defaults.entryId),
    sourcePort: Number(stored.sourcePort || defaults.sourcePort),
    dataPort: Number(stored.dataPort || defaults.dataPort),
    discoveryPort: Number(stored.discoveryPort || defaults.discoveryPort),
    preferIpv6: stored.preferIpv6 === 'true',
  };
}

function saveSettings(settings) {
  const lines = ['; GarlicTransfer settings'];
  for (const field of fields) {
    const value = field === 'useRemoteOracle' ? Boolean(settings[field]) : settings[field];
    lines.push(`${field}=${value ?? ''}`);
  }
  fs.writeFileSync(configPath, `${lines.join('\n')}\n`, { mode: 0o600 });
}

module.exports = { loadSettings, saveSettings };
