import fs from 'node:fs';
import { ExtensionBridge } from './upstream/ws-bridge.ts';

const config = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
process.env.WECHATSYNC_TOKEN = config.token;
const bridge = new ExtensionBridge(config.port, { silent: true });
await bridge.start();
if (bridge.getMode() !== 'primary') {
  throw new Error('Wechatsync bridge port is already occupied');
}
console.log(JSON.stringify({ status: 'ready', port: config.port }));
const heartbeat = setInterval(() => {
  if (bridge.isConnected()) bridge.request('workbenchPing', {}).catch(() => {});
}, 20000);
for (const signal of ['SIGINT', 'SIGTERM']) {
  process.on(signal, () => { clearInterval(heartbeat); bridge.stop(); process.exit(0); });
}
