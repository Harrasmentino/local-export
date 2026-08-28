// Loaded before artifact-tool: report generation must not open network sockets.
import net from 'node:net';
import tls from 'node:tls';
import http from 'node:http';
import https from 'node:https';
import dgram from 'node:dgram';
import dns from 'node:dns';
import { syncBuiltinESMExports } from 'node:module';

function blocked() { throw new Error('NETWORK_DISABLED_FOR_LOCAL_REPORTS'); }
net.Socket.prototype.connect = blocked;
net.connect = net.createConnection = blocked;
tls.connect = blocked;
http.request = http.get = https.request = https.get = blocked;
dgram.createSocket = blocked;
dns.lookup = dns.resolve = blocked;
globalThis.fetch = blocked;
globalThis.WebSocket = class { constructor() { blocked(); } };
syncBuiltinESMExports();
