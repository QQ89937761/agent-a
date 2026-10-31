const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const html = fs.readFileSync(require('node:path').join(__dirname, 'src/index.html'), 'utf8').replace(/\r\n/g, '\n');
function section(start, end) {
  const from = html.indexOf(start), to = html.indexOf(end, from);
  assert(from >= 0 && to > from);
  return html.slice(from, to);
}
(async () => {
  let now = 1000, healthy = 0, diagnosed = 0;
  const timers = new Map(), intervals = new Map(), sockets = [];
  let sequence = 0;
  class Socket {
    static OPEN = 1; static CONNECTING = 0;
    constructor() {this.readyState = 0; this.sent = []; sockets.push(this);}
    close() {this.closed = true; this.readyState = 3;}
    send(data) {this.sent.push(JSON.parse(data));}
  }
  const context = vm.createContext({
    WebSocket: Socket, Date: {now: () => now}, JSON, Map, Boolean,
    WS_URL: 'ws://local.test/ws', IS_LOCAL_FILE: false, window: {addEventListener() {}}, document: {addEventListener() {}},
    setTimeout: (fn, delay) => {const id = ++sequence; timers.set(id, {fn, delay}); return id;},
    clearTimeout: id => timers.delete(id),
    setInterval: (fn, delay) => {const id = ++sequence; intervals.set(id, {fn, delay}); return id;},
    clearInterval: id => intervals.delete(id),
    refreshHealth() {healthy++;}, diagnoseConnection() {diagnosed++;},
    setConn() {}, finishStreamBubble() {}, setBusy() {}, stopContinuousMode() {},
    flushPendingDeliveries() {}, clearDeliveryTimer() {},
    pendingDeliveries: new Map(), statusEl: {}, traceClock: null,
    currentTaskCard: null, continuousActive: false, historyLoaded: false,
    addEvent() {throw Error('pong must not become a chat event');},
  });
  const run = source => vm.runInContext(source, context);
  run(`let ws, connecting = false, reconnectTimer = null;
    let connectedOnce = false, reconnectDelay = 300;
    const RECONNECT_MIN = 1000, RECONNECT_MAX = 15000;
    let lastSocketMessageAt = 0, heartbeatTimer = null;
    ${section('    function scheduleReconnect()', '    if (window.bridge && bridge.onBackendReady)')}
    ${section('    async function connect()', '    setConn(false);\n\n    // 把时间戳')}`);
  await run('connect()');
  const stalled = sockets[0];
  assert.equal(timers.size, 1);
  [...timers.values()].find(t => t.delay === 10000).fn();
  assert(stalled.closed, 'stalled handshake must close');
  assert.equal(run('ws'), null);
  assert([...timers.values()].some(t => t.delay === 300), 'handshake retries automatically');
  await run('connect()');
  const live = sockets[1]; live.readyState = 1; live.onopen();
  assert.equal(healthy, 1);
  assert.equal(intervals.size, 1);
  now += 15000; [...intervals.values()][0].fn();
  assert.equal(live.sent[0].type, 'ping');
  now += 25000; live.onmessage({data: '{"type":"pong","data":{}}'});
  now += 15000; [...intervals.values()][0].fn();
  assert(!live.closed, 'pong keeps quiet connection alive');
  now += 46000; [...intervals.values()][0].fn();
  assert(live.closed, 'silent half-open connection must close');
  assert.equal(intervals.size, 0);
  await run('connect()');
  const errorOnly = sockets[2]; errorOnly.readyState = 1; errorOnly.onopen();
  errorOnly.onerror();
  assert.equal(run('ws'), null, 'error without native close must reconnect');
  assert(errorOnly.closed);
  const pending = timers.size;
  errorOnly.onclose(); stalled.onclose();
  assert.equal(timers.size, pending, 'stale close must not disturb reconnect');
  assert(diagnosed >= 3);
  console.log('connection recovery: stalled handshake, pong, half-open socket, error-only and stale close passed');
})().catch(error => {console.error(error); process.exitCode = 1;});
