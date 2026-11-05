const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const test = require('node:test');
const { pairingPrefixPath, existingPairingPrefix } = require('../electron/pairing_store.cjs');

const consoleIdentity = { hostId: '5c96661f7c1a', systemVersion: '14220000' };

test('pairing is isolated by console and firmware and requires a 32-byte file', () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'garlic-pair-test-'));
  try {
    const file = pairingPrefixPath(root, consoleIdentity);
    assert.equal(existingPairingPrefix(root, consoleIdentity), null);
    fs.mkdirSync(path.dirname(file), { recursive: true });
    fs.writeFileSync(file, Buffer.alloc(31));
    assert.equal(existingPairingPrefix(root, consoleIdentity), null);
    fs.writeFileSync(file, Buffer.alloc(32));
    assert.equal(existingPairingPrefix(root, consoleIdentity), file);
    assert.equal(existingPairingPrefix(root, { ...consoleIdentity, systemVersion: '14230000' }), null);
    assert.equal(existingPairingPrefix(root, { ...consoleIdentity, hostId: '000000000001' }), null);
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});

test('untrusted console identifiers cannot escape the pairing directory', () => {
  const root = os.tmpdir();
  assert.throws(() => pairingPrefixPath(root, { ...consoleIdentity, hostId: '../elsewhere' }));
  assert.throws(() => pairingPrefixPath(root, { ...consoleIdentity, systemVersion: '../firmware' }));
});

test('pairing UI uses instructions and a green paired state without account ID or USB controls', () => {
  const root = path.join(__dirname, '..');
  const html = fs.readFileSync(path.join(root, 'electron', 'index.html'), 'utf8');
  const renderer = fs.readFileSync(path.join(root, 'electron', 'renderer.js'), 'utf8');
  const style = fs.readFileSync(path.join(root, 'electron', 'style.css'), 'utf8');
  const main = fs.readFileSync(path.join(root, 'electron', 'main.cjs'), 'utf8');
  assert.match(html, /id="pairingModal"/);
  assert.match(html, /GarlicTransfer Pairing/);
  assert.doesNotMatch(html, /accountId|findAccountIdBtn/);
  assert.match(renderer, /event\.type === 'pairingStarted'/);
  assert.match(renderer, /event\.type === 'paired'[\s\S]*?hidePairingModal\(\)[\s\S]*?classList\.add\('paired'\)/);
  assert.match(style, /#pairingState\.paired\s*\{[^}]*color:\s*#[0-9a-f]{6}/i);
  assert.doesNotMatch(main, /dialog:account-id|accountIdFromBackupDirectory/);
});

test('upload button opens dismissible console instructions', () => {
  const root = path.join(__dirname, '..');
  const html = fs.readFileSync(path.join(root, 'electron', 'index.html'), 'utf8');
  const renderer = fs.readFileSync(path.join(root, 'electron', 'renderer.js'), 'utf8');
  assert.match(html, /id="confirmUploadBtn"[^>]*>Upload save<\/button>/);
  assert.match(html, /id="uploadModal"[\s\S]*?Delete the selected save data[\s\S]*?GarlicTransfer[\s\S]*?import the data/);
  assert.doesNotMatch(html, /Delete selected original saves and start upload/);
  assert.match(renderer, /activeTransfer = 'upload';\s*showUploadModal\(\);/);
  assert.match(renderer, /uploadModalCloseBtn\.addEventListener\('click', hideUploadModal\)/);
  assert.match(renderer, /if \(!value\) \{[\s\S]*?hideUploadModal\(\);/);
});
