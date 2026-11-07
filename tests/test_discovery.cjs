const assert = require('node:assert/strict');
const { broadcastAddress, discoveryRoutes } = require('../electron/discovery.cjs');

assert.equal(broadcastAddress('192.168.1.10', '255.255.255.0'), '192.168.1.255');
assert.equal(broadcastAddress('10.20.30.40', '255.255.0.0'), '10.20.255.255');

const routes = discoveryRoutes({
  Ethernet: [{ family: 'IPv4', internal: false, address: '192.168.1.10', netmask: '255.255.255.0' }],
  WiFi: [{ family: 'IPv4', internal: false, address: '10.20.30.40', netmask: '255.255.0.0' }],
  Loopback: [{ family: 'IPv4', internal: true, address: '127.0.0.1', netmask: '255.0.0.0' }],
});
assert.deepEqual(routes, [
  { bind: '192.168.1.10', target: '192.168.1.255' },
  { bind: '10.20.30.40', target: '10.20.255.255' },
]);
console.log('Multi-interface discovery targets: PASS');
