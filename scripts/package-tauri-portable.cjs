const fs = require('node:fs');
const path = require('node:path');
const os = require('node:os');
const { execFileSync } = require('node:child_process');

const root = path.resolve(__dirname, '..');
const target = path.join(root, 'src-tauri', 'target', 'release');
const output = path.join(root, 'dist', 'tauri');
const platform = process.argv[2] || process.platform;
const version = require('../package.json').version;

function copy(source, destination) {
  if (!fs.existsSync(source)) throw new Error(`Portable input is missing: ${source}`);
  fs.mkdirSync(path.dirname(destination), { recursive: true });
  fs.cpSync(source, destination, { recursive: true });
}

function backend(stage) {
  const poc = path.join(root, 'poc');
  for (const name of fs.readdirSync(poc)) {
    if (/^dt_.*\.py$/.test(name)) copy(path.join(poc, name), path.join(stage, 'poc', name));
  }
  copy(path.join(poc, 'templates', 'single-save.db'), path.join(stage, 'poc', 'templates', 'single-save.db'));
  copy(
    path.join(poc, 'ncdt_oracle', 'ncdt_oracle_server.elf'),
    path.join(stage, 'poc', 'ncdt_oracle', 'ncdt_oracle_server.elf'),
  );
  for (const name of ['PS4_Titles.tsv', 'PS5_Titles.tsv']) {
    copy(path.join(root, 'data', name), path.join(stage, 'data', name));
  }
  copy(path.join(root, 'requirements.txt'), path.join(stage, 'requirements.txt'));
}

function archive(command, args) {
  execFileSync(command, args, { stdio: 'inherit' });
}

fs.mkdirSync(output, { recursive: true });
const stage = path.join(output, `stage-${platform}`);
fs.rmSync(stage, { recursive: true, force: true });
fs.mkdirSync(stage, { recursive: true });

if (platform === 'win32') {
  copy(path.join(target, 'garlic-transfer.exe'), path.join(stage, 'GarlicTransfer.exe'));
  copy(path.join(root, 'Garlic-Firewall-Helper.bat'), path.join(stage, 'Garlic-Firewall-Helper.bat'));
  copy(path.join(root, 'build', 'windows-python'), path.join(stage, 'python'));
  backend(stage);
  const destination = path.join(output, `GarlicTransfer-${version}-windows-x64-portable.zip`);
  fs.rmSync(destination, { force: true });
  archive('powershell.exe', [
    '-NoLogo', '-NoProfile', '-Command',
    `Compress-Archive -Path '${stage.replaceAll("'", "''")}\\*' -DestinationPath '${destination.replaceAll("'", "''")}'`,
  ]);
  console.log(destination);
} else if (platform === 'darwin') {
  const app = path.join(stage, 'GarlicTransfer.app');
  copy(path.join(target, 'garlic-transfer'), path.join(app, 'Contents', 'MacOS', 'GarlicTransfer'));
  backend(path.join(app, 'Contents', 'Resources'));
  const plist = `<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>CFBundleName</key><string>GarlicTransfer</string>
<key>CFBundleDisplayName</key><string>GarlicTransfer</string>
<key>CFBundleIdentifier</key><string>com.earthonion.garlictransfer</string>
<key>CFBundleVersion</key><string>${version}</string>
<key>CFBundleShortVersionString</key><string>${version}</string>
<key>CFBundleExecutable</key><string>GarlicTransfer</string>
<key>CFBundlePackageType</key><string>APPL</string>
<key>LSMinimumSystemVersion</key><string>11.0</string>
<key>NSHighResolutionCapable</key><true/>
</dict></plist>
`;
  fs.writeFileSync(path.join(app, 'Contents', 'Info.plist'), plist);
  const destination = path.join(output, `GarlicTransfer-${version}-macos-portable.zip`);
  fs.rmSync(destination, { force: true });
  archive('ditto', ['-c', '-k', '--keepParent', app, destination]);
  console.log(destination);
} else if (platform === 'linux') {
  copy(path.join(target, 'garlic-transfer'), path.join(stage, 'GarlicTransfer'));
  backend(stage);
  const destination = path.join(output, `GarlicTransfer-${version}-linux-x64-portable.tar.gz`);
  fs.rmSync(destination, { force: true });
  archive('tar', ['-czf', destination, '-C', stage, '.']);
  console.log(destination);
} else {
  throw new Error(`Unsupported portable platform: ${platform} (${os.type()})`);
}
