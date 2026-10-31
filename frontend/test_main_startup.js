const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const assert = require("node:assert/strict");
const { EventEmitter } = require("node:events");
const main = fs.readFileSync(path.join(__dirname, "main.js"), "utf8");
const updater = fs.readFileSync(path.join(__dirname, "../backend/core/update_manager.py"), "utf8");
const idAt = main.indexOf('app.setAppUserModelId("com.xiaoa.desktop")');
assert(idAt >= 0 && idAt < main.indexOf("app.requestSingleInstanceLock()"));
assert(main.includes("XIAOA_LAUNCH_TEMP: os.tmpdir()"));
const launches = updater.split("\n").filter(line => line.includes("Start-Process -FilePath $Target"));
assert.equal(launches.length, 5);
assert(launches.every(line => line.includes("-WindowStyle Hidden")), "restart AND rollback must hide the launcher");

class FakeWindow extends EventEmitter {
  constructor(options) {
    super();
    this.options = options;
    this.visible = options.show !== false;
    this.destroyed = false;
    this.skipTaskbar = options.skipTaskbar;
    this.webContents = new EventEmitter();
  }
  isDestroyed() { return this.destroyed; }
  isVisible() { return this.visible; }
  getPosition() { return this.position || [this.options.x, this.options.y]; }
  getBounds() {
    const [x, y] = this.getPosition();
    return {x, y, width: this.options.width, height: this.options.height};
  }
  setSkipTaskbar(value) { this.skipTaskbar = value; }
  setAlwaysOnTop() {}
  setContentProtection(value) { this.contentProtected = value; }
  setIgnoreMouseEvents(value, options) { this.ignoreMouse = value; this.forwardMouse = options.forward; }
  getSize() { return [this.options.width, this.options.height]; }
  setPosition(x, y) { this.position = [x, y]; }
  hide() { this.visible = false; }
  close() { this.destroyed = true; this.emit("closed"); }
  setShape() {}
  loadFile() {}
  showInactive() { this.visible = true; this.emit("show"); }
  show() { this.visible = true; this.skipTaskbar = false; this.emit("show"); }
}
const shape = require("./float_position");
const ipcMain = new EventEmitter();
let positionSaves = 0;
const context = vm.createContext({
  BrowserWindow: FakeWindow, ...shape, path, __dirname, console, ipcMain,
  screen: {
    getCursorScreenPoint: () => ({x: 0, y: 0}),
    getDisplayNearestPoint: () => ({workArea: {x: 0, y: 0, width: 1920, height: 1080}}),
    getPrimaryDisplay: () => ({workAreaSize: {width: 1920, height: 1080}}),
  },
  loadFloatPosition: () => null, lockNavigation() {}, updateChatBackground() {},
  saveFloatPosition() { positionSaves++; }, clearFloatAlert() {}, updateChatShape() {}, app: {quit() {}},
  floatWin: null, chatWin: null, foregroundNoticeWin: null, quitting: false, chatResize: null,
  setTimeout(fn) { context.noticeTimeout = fn; return 1; }, clearTimeout() { context.noticeTimeout = null; },
});
function sourceBetween(start, end) {
  const a = main.indexOf(start);
  const b = main.indexOf(end, a + start.length);
  assert(a >= 0 && b > a, `startup source markers: ${start}`);
  return main.slice(a, b);
}
vm.runInContext(
  sourceBetween("function keepOutOfTaskbar(", "const hasSingleInstanceLock") +
  sourceBetween("function clampFloatToDisplay(", "function saveFloatPosition()") +
  sourceBetween("function positionForegroundNotice()", "function createFloat()") +
  sourceBetween("function createFloat()", "function waitForBackend(") +
  sourceBetween("function createChat()", "// 悬浮球被点击") +
  sourceBetween('ipcMain.on("float-drag"', "// 用系统默认浏览器"), context,
);
context.createForegroundNotice();
const notice = context.foregroundNoticeWin;
assert.equal(notice.visible, false);
assert.equal(notice.options.focusable, false);
assert.equal(notice.contentProtected, true, "takeover notice must be excluded from desktop observations");
assert.equal(notice.ignoreMouse, true);
assert.equal(notice.forwardMouse, true);
context.setForegroundNotice(true);
assert.equal(notice.visible, true);
assert.equal(typeof context.noticeTimeout, "function", "active notice must arm its recovery watchdog");
assert.deepEqual(notice.position, [700, 994], "notice stays above the taskbar at bottom center");
context.screen.getDisplayNearestPoint = () => ({workArea: {x: -1280, y: 60, width: 1280, height: 900}});
context.positionForegroundNotice();
assert.deepEqual(notice.position, [-900, 874], "secondary display taskbar coordinates");
context.setForegroundNotice(true);
assert.equal(notice.visible, true, "repeated active notifications keep desktop notice visible");
context.setForegroundNotice(false);
assert.equal(notice.visible, false, "release hides the notice immediately");
context.setForegroundNotice(true);
context.noticeTimeout();
assert.equal(notice.visible, false, "lost heartbeats must hide the notice");
const packageJson = JSON.parse(fs.readFileSync(path.join(__dirname, "package.json"), "utf8"));
assert(packageJson.build.files.includes("src/foreground_notice.html"), "notice must ship in ASAR");
const html = fs.readFileSync(path.join(__dirname, "src/index.html"), "utf8");
assert(html.indexOf('if (ev.type === "foreground_control")') < html.indexOf('const eventConversationId ='), "notice is machine-wide, including inactive chats");
assert(main.includes('webContents.on("render-process-gone", () => setForegroundNotice(false))'));
assert(main.includes('foregroundNoticeWin.close()'));
context.createFloat();
const float = context.floatWin;
assert.equal(float.options.y, 80, "floating icon should start above the chat panel");
assert.equal(float.visible, false, "do not inherit visible startup state from the Portable wrapper");
float.emit("ready-to-show");
assert.equal(float.visible, true, "a hidden updater must still leave a visible floating ball");
assert.equal(float.skipTaskbar, true);
float.show();
assert.equal(float.skipTaskbar, true, "reassert after Windows changes native window state");

context.createChat();
const chat = context.chatWin;
assert.equal(chat.options.minWidth, 360, "chat panel cannot be resized below the width needed by its controls");
assert.equal(chat.options.minHeight, 420, "chat panel cannot be resized below the height allowed by its resize handler");
assert.equal(chat.options.y, 100, "chat panel should start above the old baseline");
chat.emit("ready-to-show");
assert.equal(chat.visible, false, "do not open the chat panel on startup");
// Reproduce a saved floating position overlapping the panel's top edge.
context.screen.getDisplayNearestPoint = () => ({workArea: {x: 0, y: 0, width: 1920, height: 1080}});
float.setPosition(1700, 153);
chat.show();
assert.deepEqual(float.getPosition(), [1700, 153], "opening chat must preserve an overlapping icon position");
float.setPosition(1700, 95);
chat.emit("resize");
assert.deepEqual(float.getPosition(), [1700, 95], "resizing chat must not reposition the icon");
float.setPosition(1700, 120);
chat.emit("move");
assert.deepEqual(float.getPosition(), [1700, 120], "moving chat must not reposition the icon");
float.setPosition(1700, 0);
ipcMain.emit("float-drag", null, {dx: 0, dy: 153});
assert.deepEqual(float.getPosition(), [1700, 153], "dragging onto chat must keep the requested position");
assert.equal(positionSaves, 1, "dragged position must still be saved");
ipcMain.emit("float-drag", null, {dx: 1000, dy: 2000});
assert.deepEqual(float.getPosition(), [1832, 992], "screen boundaries must still keep the icon reachable");
assert.equal(positionSaves, 2);
ipcMain.emit("float-drag", null, {dx: NaN, dy: 0});
assert.deepEqual(float.getPosition(), [1832, 992]);
assert.equal(positionSaves, 2, "invalid drag input must not change the saved position");
assert.equal(chat.skipTaskbar, true, "opening chat must not recreate a taskbar button");
context.setForegroundNotice(true);
chat.webContents.emit("render-process-gone");
assert.equal(notice.visible, false, "renderer crash must not leave an orphan notice");
context.createFloat();
context.quitting = true;
context.floatWin.emit("ready-to-show");
assert.equal(context.floatWin.visible, false, "quitting before ready must not resurrect a window");
context.keepOutOfTaskbar(null);
float.destroyed = true;
context.keepOutOfTaskbar(float);
console.log("startup: visible float with hidden launcher; taskbar suppression survives ready/show; overlapping drag preserved; safe restart and rollback passed");

const http = {
  get(url, callback) {
    const request = new EventEmitter();
    request.setTimeout = () => {};
    request.destroy = () => request.emit("error", new Error("closed"));
    const response = new EventEmitter();
    response.setEncoding = () => {};
    response.statusCode = 200;
    callback(response);
    http.response = response;
    return request;
  },
};
let failures = 0, conflicts = 0, quits = 0;
const startupLogs = [];
const probeContext = vm.createContext({
  require: name => name === "http" ? http : {version: "1.0.8"},
  logStartup(message) { startupLogs.push(message); }, process: {pid: 1234}, console, startupStartedAt: Date.now(),
  backendProbeInFlight: false, backendHealthFailures: 0, backendFirstHealthy: false,
  backendStartupDeadline: 0, startBackendWatchdog() {}, chatWin: null,
  quitting: false, handleBackendFailure() { failures++; },
  dialog: {showErrorBox() { conflicts++; }}, app: {quit() { quits++; }},
});
vm.runInContext(sourceBetween("function startBackend()", "function handleBackendFailure()"), probeContext);
function probeHealth(health) {
  probeContext.startBackend();
  http.response.emit("data", JSON.stringify(health));
  http.response.emit("end");
}
probeHealth({ok: true, version: "1.0.8", frontend_pid: "1234"});
assert.equal(probeContext.backendFirstHealthy, true);
assert.equal(conflicts, 0);
for (const health of [
  {ok: true, version: "1.0.7", frontend_pid: "1234"},
  {ok: true, version: "1.0.8", frontend_pid: "9999"},
  {ok: true, version: "1.0.8"},
]) {
  probeHealth(health);
}
assert.equal(conflicts, 3, "old, foreign and unidentified backends must not be reused");
assert.equal(quits, 3);
assert.equal(failures, 0, "a foreign backend must never be killed or restarted");
probeHealth({ok: false});
assert.equal(failures, 1);
assert.equal(startupLogs.length, 1, "healthy watchdog probes must not keep appending startup logs");
console.log("backend ownership: current parent/version accepted; other instances rejected");

// Exercise timer changes and failure handling, not only source text.
let timerDelay, restarts = 0, readyNotifications = 0;
Object.assign(probeContext, {
  setInterval(_fn, delay) { timerDelay = delay; return 1; }, clearInterval() {},
  backendHealthTimer: null, backendFirstHealthy: false,
  backendProc: {killed: false}, backendStartupDeadline: Date.now() + 60000,
  BACKEND_FAILURE_LIMIT: 3, restartBackend() { restarts++; }, launchBackend() {},
  chatWin: {isDestroyed: () => false, webContents: {send: () => readyNotifications++}},
});
vm.runInContext(sourceBetween("function handleBackendFailure()", "if (hasSingleInstanceLock) app.whenReady()"), probeContext);
probeContext.startBackendWatchdog();
assert.equal(timerDelay, 250);
for (let i = 0; i < 300; i++) probeContext.handleBackendFailure();
assert.equal(restarts, 0, "rapid startup probes must not restart a normally loading backend");
probeHealth({ok: true, version: "1.0.8", frontend_pid: "1234"});
assert.equal(timerDelay, 5000);
assert.equal(readyNotifications, 1);
for (let i = 0; i < 3; i++) probeContext.handleBackendFailure();
assert.equal(restarts, 1, "watchdog must still recover a backend that fails after startup");

const reconnectDelays = [];
const reconnectContext = vm.createContext({
  window: {}, connect() {}, setTimeout(_fn, delay) { reconnectDelays.push(delay); },
});
const retryStart = html.indexOf("    let connectedOnce =");
vm.runInContext(html.slice(retryStart, html.indexOf("    let heartbeatTimer", retryStart)), reconnectContext);
for (let i = 0; i < 5; i++) vm.runInContext("scheduleReconnect()", reconnectContext);
assert.deepEqual(reconnectDelays, [300, 500, 1000, 1000, 1000]);
reconnectDelays.length = 0;
vm.runInContext("connectedOnce = true; reconnectDelay = RECONNECT_MIN;", reconnectContext);
for (let i = 0; i < 6; i++) vm.runInContext("scheduleReconnect()", reconnectContext);
assert.deepEqual(reconnectDelays, [1000, 2000, 4000, 8000, 15000, 15000]);
console.log("startup handshake: fast probes, grace period, ready notification and reconnect backoff passed");

(async () => {
  let releasePreparation, preparations = 0, spawned = 0;
  const preparation = new Promise(resolve => { releasePreparation = resolve; });
  const launchContext = vm.createContext({
    path, os: {homedir: () => "home", tmpdir: () => "tmp"}, __dirname,
    appendBackendLog() {}, logStartup() {}, console, startupStartedAt: Date.now(), process: {env: {}, pid: 1234, resourcesPath: "resources"},
    app: {isPackaged: true, getPath: () => "profile"}, resolveAppHome: () => "isolated-home",
    fs: {existsSync: () => true, mkdirSync() {}, openSync: () => 42, closeSync() {}},
    preparePluginRuntimes: () => { preparations++; return preparation; },
    rotateBackendLog() {},
    spawn: (_command, _args, options) => {
      spawned++;
      if (options.env.XIAOA_PLUGIN_RUNTIME_ERROR) {
        assert.match(options.env.XIAOA_PLUGIN_RUNTIME_ERROR, /copy failed/);
        assert.equal(options.env.XIAOA_PLUGIN_PYTHON, undefined);
      } else {
        assert.equal(options.env.XIAOA_PLUGIN_PYTHON, "plugin-python");
      }
      assert.equal(options.env.PYTHONUNBUFFERED, "1");
      assert.deepEqual(Array.from(options.stdio), ["ignore", "pipe", "pipe"]);
      const child = new EventEmitter();
      child.stdout = new EventEmitter();
      child.stderr = new EventEmitter();
      return child;
    },
    backendLaunchInFlight: false, backendProc: null, backendRestartInFlight: false,
    quitting: false,
  });
  vm.runInContext(sourceBetween("async function launchBackend()", "function startBackend()"), launchContext);
  const pending = launchContext.launchBackend();
  await launchContext.launchBackend();
  assert.equal(preparations, 1, "watchdog must not prepare another runtime during an async launch");
  assert.equal(spawned, 0, "backend must wait for its runtimes");
  releasePreparation({XIAOA_PLUGIN_PYTHON: "plugin-python"});
  await pending;
  assert.equal(spawned, 1);
  assert.equal(launchContext.backendLaunchInFlight, false);

  launchContext.backendProc = null;
  let releaseShutdown;
  launchContext.preparePluginRuntimes = () => new Promise(resolve => { releaseShutdown = resolve; });
  const shutdownPending = launchContext.launchBackend();
  launchContext.quitting = true;
  releaseShutdown({});
  await shutdownPending;
  assert.equal(spawned, 1, "quit during async preparation must not leave a new orphan backend");
  assert.equal(launchContext.backendLaunchInFlight, false);

  launchContext.quitting = false;
  launchContext.preparePluginRuntimes = async () => { throw new Error("copy failed"); };
  await launchContext.launchBackend();
  assert.equal(spawned, 2, "optional plugin preparation failure must not block the main backend");
  assert.equal(launchContext.backendLaunchInFlight, false);
  console.log("async startup: prepared runtime environment, single launch, quit and failure recovery passed");
})().catch(error => { console.error(error); process.exitCode = 1; });

// Exercise live rotation against real temporary files, including a single large chunk.
const os = require("node:os");
const logFolder = fs.mkdtempSync(path.join(os.tmpdir(), "xiaoa-log-test-"));
try {
  const context = vm.createContext({fs, path, Buffer, console,
    BACKEND_LOG_MAX_BYTES: 65536, BACKEND_LOG_KEEP: 3});
  vm.runInContext(sourceBetween("function rotateBackendLog(", "function terminateBackend()"), context);
  const logPath = path.join(logFolder, "backend.log");
  context.appendBackendLog(logPath, Buffer.alloc(6 * 65536, 120));
  assert.deepEqual(fs.readdirSync(logFolder).sort(), ["backend.log", "backend.log.1", "backend.log.2", "backend.log.3"]);
  for (const name of fs.readdirSync(logFolder)) assert(fs.statSync(path.join(logFolder, name)).size <= 65536);
  context.resolveAppHome = () => logFolder;
  context.logStartup("[startup] test milestone");
  assert(fs.readFileSync(path.join(logFolder, "data", "isolation", "logs", "startup.log"), "utf8").includes("test milestone"));
} finally {
  fs.rmSync(logFolder, {recursive: true, force: true});
}
