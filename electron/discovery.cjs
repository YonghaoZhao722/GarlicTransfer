const net = require('node:net');
const os = require('node:os');

function ipv4Number(address) {
  return address.split('.').reduce((value, part) => ((value << 8) | Number(part)) >>> 0, 0);
}

function ipv4String(value) {
  return [
    (value >>> 24) & 255,
    (value >>> 16) & 255,
    (value >>> 8) & 255,
    value & 255,
  ].join('.');
}

function broadcastAddress(address, netmask) {
  const network = ipv4Number(address) & ipv4Number(netmask);
  return ipv4String((network | ~ipv4Number(netmask)) >>> 0);
}

function discoveryRoutes(interfaces = os.networkInterfaces()) {
  const routes = [];
  const seen = new Set();
  const add = (bind, target) => {
    const key = `${bind}/${target}`;
    if (!seen.has(key)) {
      seen.add(key);
      routes.push({ bind, target });
    }
  };

  for (const addresses of Object.values(interfaces)) {
    for (const item of addresses || []) {
      if (item.internal || item.family !== 'IPv4' || !net.isIPv4(item.address)) continue;
      const netmask = net.isIPv4(item.netmask || '') ? item.netmask : '255.255.255.0';
      add(item.address, broadcastAddress(item.address, netmask));
    }
  }

  if (!routes.length) add('', '255.255.255.255');
  return routes;
}

module.exports = { broadcastAddress, discoveryRoutes };
