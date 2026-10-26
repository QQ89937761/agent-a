const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const { MODES, normalizeMode, loadBackground, saveBackground, backgroundAvailability, applyBackground } = require("./chat_background");
const { createRoundedRectShape } = require("./float_position");
const base = { platform: "win32", hardwareAcceleration: true, nativeTheme: {} };
const available = overrides => backgroundAvailability({ ...base, ...overrides });
assert.deepEqual(MODES, ["standard", "translucent", "transparent"]);
assert.equal(available().translucentReason, "");
assert.equal(normalizeMode("acrylic"), "translucent");
assert(available({ platform: "linux" }).translucentReason);
assert.equal(available({ platform: "darwin" }).translucentReason, "");
assert(available({ hardwareAcceleration: false }).translucentReason);
for (const key of ["shouldUseHighContrastColors", "inForcedColorsMode", "prefersReducedTransparency"]) {
  assert(available({ nativeTheme: { [key]: true } }).translucentReason);
}
for (const mode of MODES) assert.equal(normalizeMode(mode), mode);
for (const invalid of [undefined, null, {}, "", "auto", "../../file"]) assert.equal(normalizeMode(invalid), "standard");

const tmpRoot = path.resolve(__dirname, "..", "data", "isolation", "workspace");
fs.mkdirSync(tmpRoot, { recursive: true });
const tmp = fs.mkdtempSync(path.join(tmpRoot, "chat-background-test-"));
const settings = path.join(tmp, "chat_background.json");
try {
  assert.equal(loadBackground(settings), "standard");
  for (const mode of MODES) { saveBackground(settings, mode); assert.equal(loadBackground(settings), mode); }
  fs.writeFileSync(settings, JSON.stringify({ mode: "acrylic" }));
  assert.equal(loadBackground(settings), "translucent");
  saveBackground(settings, "acrylic");
  assert.equal(JSON.parse(fs.readFileSync(settings, "utf8")).mode, "translucent");
  assert.equal(loadBackground(settings), "translucent");
  for (const bad of ["{", "null", '{"mode":"unknown"}']) {
    fs.writeFileSync(settings, bad);
    assert.equal(loadBackground(settings), "standard");
  }
} finally {
  if (fs.existsSync(settings)) fs.unlinkSync(settings);
  fs.rmdirSync(tmp);
}

// All three backgrounds use the same rounded native window region.
for (const [width, height] of [[420, 640], [320, 420], [900, 700], [1, 1], [20, 12]]) {
  const shape = createRoundedRectShape(width, height);
  const contains = (x, y) => shape.some(r => x >= r.x && x < r.x + r.width && y >= r.y && y < r.y + r.height);
  assert(contains(Math.floor(width / 2), Math.floor(height / 2)));
  for (const rect of shape) {
    assert(rect.x >= 0 && rect.y >= 0 && rect.width > 0 && rect.height > 0);
    assert(rect.x + rect.width <= width && rect.y + rect.height <= height);
  }
  if (width >= 32 && height >= 32) {
    for (const [x, y] of [[0, 0], [width - 1, 0], [0, height - 1], [width - 1, height - 1]]) assert(!contains(x, y));
    assert(contains(16, 0)); assert(contains(0, 16));
  }
}
// Native clipping must include partially covered CSS pixels in all four arcs.
for (const radius of [1, 2, 8, 16, 24]) {
  const width = radius * 2 + 20, height = radius * 2 + 30;
  const shape = createRoundedRectShape(width, height, radius);
  const contains = (x, y) => shape.some(r => x >= r.x && x < r.x + r.width && y >= r.y && y < r.y + r.height);
  for (let y = 0; y < radius; y++) {
    for (let x = 0; x < radius; x++) {
      // The point closest to the circle center lies at the pixel's inner edge.
      if ((radius - x - 1) ** 2 + (radius - y - 1) ** 2 >= radius ** 2) continue;
      for (const [px, py] of [[x, y], [width - 1 - x, y], [x, height - 1 - y], [width - 1 - x, height - 1 - y]]) {
        assert(contains(px, py), "native region must not cut away an antialiased edge pixel");
      }
    }
  }
}
for (const args of [[NaN, 100], [100, Infinity], [0, 100], [100, 100, -1]]) {
  assert.throws(() => createRoundedRectShape(...args), RangeError);
}
let calls = [];
const win = { setBackgroundMaterial() { throw Error("native material must not be used"); },
  setBackgroundColor(value) { calls.push(["color", value]); } };
for (const mode of [...MODES, "translucent", "standard"]) {
  calls = [];
  assert.equal(applyBackground(win, mode, available()).mode, mode);
  assert.deepEqual(calls, [["color", "#00000000"]]);
}
calls = [];
const migrated = applyBackground(win, "acrylic", available());
assert.equal(migrated.mode, "translucent");
assert.equal(migrated.requested, "translucent");
assert.equal(migrated.reason, "");
assert.deepEqual(calls, [["color", "#00000000"]]);
for (const overrides of [{ hardwareAcceleration: false }, ...["shouldUseHighContrastColors", "inForcedColorsMode", "prefersReducedTransparency"].map(key => ({ nativeTheme: { [key]: true } }))]) {
  const blocked = applyBackground(win, "transparent", available(overrides));
  assert.equal(blocked.mode, "standard"); assert(blocked.reason);
}
assert.equal(applyBackground({ setBackgroundColor() {} }, "transparent", available()).mode, "transparent");
const originalError = console.error; console.error = () => {};
try {
  const failColor = applyBackground({ ...win, setBackgroundColor() { throw Error("failure"); } }, "translucent", available());
  assert.equal(failColor.mode, "standard"); assert(failColor.reason); assert(failColor.translucentReason);
} finally { console.error = originalError; }

const main = fs.readFileSync(path.join(__dirname, "main.js"), "utf8").replace(/\r\n/g, "\n");
const html = fs.readFileSync(path.join(__dirname, "src", "index.html"), "utf8").replace(/\r\n/g, "\n");
const preload = fs.readFileSync(path.join(__dirname, "preload.js"), "utf8");
const handlers = {}, events = {}, saved = [], sent = [], boundsChanges = [];
let cursor = { x: 100, y: 100 }, maximized = false, fullScreen = false;
const shapes = [];
const sender = { send(channel, state) { sent.push({ channel, state }); } };
const chatWin = { ...win, webContents: sender, isDestroyed: () => false, isMaximized: () => maximized,
  getSize: () => [420, 640], isFullScreen: () => fullScreen, setShape: value => shapes.push(value),
  getBounds: () => ({ x: 50, y: 60, width: 420, height: 640 }), setBounds: b => boundsChanges.push(b) };
const context = vm.createContext({ path, uiLanguage: "zh", process: { platform: "win32" },
  nativeText: text => require("./src/ui-language").translate(text, context.uiLanguage || "zh"),
  resolveAppHome: () => path.resolve(__dirname, ".."), console, chatWin,
  nativeTheme: {}, app: { isHardwareAccelerationEnabled: () => true },
  createRoundedRectShape, loadBackground, saveBackground: (file, mode) => saved.push({ file, mode }), backgroundAvailability, applyBackground,
  screen: { getCursorScreenPoint: () => cursor },
  ipcMain: { handle(channel, fn) { handlers[channel] = fn; }, on(channel, fn) { events[channel] = fn; } } });
vm.runInContext('let chatBackgroundRequested = "standard"; let chatBackgroundState = {}; let chatResize = null;', context);
vm.runInContext(main.slice(main.indexOf("function backgroundSettingsPath()"), main.indexOf("function createFloat()")), context);
vm.runInContext(main.slice(main.indexOf('ipcMain.handle("get-chat-background"'), main.indexOf('ipcMain.handle("toggle-chat-maximize"')), context);
const menu = vm.runInContext('updateChatBackground(); chatBackgroundMenu()', context);
assert(!main.includes("BaseWindow"), "must not create a second native background window");
assert(!main.includes("setBackgroundMaterial"), "must not enable native background materials");
for (const mode of MODES) {
  vm.runInContext(`updateChatBackground("${mode}")`, context);
  assert.deepEqual(shapes.at(-1), createRoundedRectShape(420, 640));
}
maximized = true; vm.runInContext('updateChatShape()', context); assert.equal(shapes.at(-1).length, 0);
maximized = false; fullScreen = true; vm.runInContext('updateChatShape()', context); assert.equal(shapes.at(-1).length, 0);
fullScreen = false; vm.runInContext('updateChatShape()', context); assert.deepEqual(shapes.at(-1), createRoundedRectShape(420, 640));
vm.runInContext(main.slice(main.indexOf("  const sendMaximized = () => {"), main.indexOf('  chatWin.on("resize", updateChatShape)')).replace("const sendMaximized", "globalThis.sendMaximized"), context);
fullScreen = true; context.sendMaximized(); assert.equal(sent.at(-1).state, true);
fullScreen = false; context.sendMaximized(); assert.equal(sent.at(-1).state, false);
maximized = true; context.sendMaximized(); assert.equal(sent.at(-1).state, true);
maximized = false;
assert(main.includes('chatWin.on("resize", updateChatShape)'));
assert(html.includes(':root[data-desktop="true"] body { background: transparent;'));
assert(html.includes('clip-path: inset(0 round 16px)'));
assert.equal(menu.label, "聊天窗口背景");
assert.equal(menu.submenu.length, 3);
assert.deepEqual(Array.from(menu.submenu, item => item.label), ["标准背景", "半透明背景", "全透明背景"]);
assert.equal(menu.submenu[2].label, "全透明背景");
assert.equal(menu.submenu.filter(item => item.checked).length, 1);
menu.submenu[1].click();
assert.equal(saved[0].mode, "translucent");
assert(saved[0].file.endsWith(path.join("data", "chat_background.json")));
assert.equal(sent.at(-1).state.mode, "translucent");
assert.equal(handlers["get-chat-background"]({ sender: {} }), null);
assert.equal(handlers["get-chat-background"]({ sender }).mode, "translucent");
handlers["set-chat-theme"]({ sender: {} }, "dark"); assert.equal(context.nativeTheme.themeSource, undefined);
handlers["set-chat-theme"]({ sender }, "bad"); assert.equal(context.nativeTheme.themeSource, undefined);
handlers["set-chat-theme"]({ sender }, "dark"); assert.equal(context.nativeTheme.themeSource, "dark");

menu.submenu[2].click();
assert.equal(saved.at(-1).mode, "transparent");
assert.equal(sent.at(-1).state.mode, "transparent");
assert.equal(vm.runInContext("chatBackgroundMenu()", context).submenu[2].checked, true);
context.nativeTheme.prefersReducedTransparency = true;
assert.equal(vm.runInContext("updateChatBackground(); chatBackgroundMenu()", context).submenu[2].enabled, false);
context.nativeTheme.prefersReducedTransparency = false;

const resize = events["resize-chat"];
resize({ sender: {} }, "start", "se"); cursor = { x: 200, y: 200 }; resize({ sender }, "move");
assert.equal(boundsChanges.length, 0);
cursor = { x: 100, y: 100 }; resize({ sender }, "start", "se");
cursor = { x: 200, y: 180 }; resize({ sender }, "move");
assert.equal(boundsChanges.at(-1).width, 520); assert.equal(boundsChanges.at(-1).height, 720);
resize({ sender }, "end"); cursor = { x: 500, y: 500 }; resize({ sender }, "move");
assert.equal(boundsChanges.length, 1);
cursor = { x: 100, y: 100 }; resize({ sender }, "start", "nw");
cursor = { x: 900, y: 900 }; resize({ sender }, "move");
assert.equal(boundsChanges.at(-1).width, 360); assert.equal(boundsChanges.at(-1).height, 420);
assert.equal(boundsChanges.at(-1).x, 110); assert.equal(boundsChanges.at(-1).y, 280);
maximized = true; resize({ sender }, "move"); assert.equal(boundsChanges.length, 2);
maximized = false; resize({ sender }, "move"); assert.equal(boundsChanges.length, 2);

const dataset = {};
const rendererContext = vm.createContext({ document: { documentElement: { dataset } }, window: {} });
vm.runInContext(html.slice(html.indexOf("    function applyChatBackground("), html.indexOf("    if (window.bridge?.getChatBackground)")), rendererContext);
for (const mode of MODES) { vm.runInContext('applyChatBackground({ mode: "' + mode + '" })', rendererContext); assert.equal(dataset.background, mode); }
vm.runInContext('applyChatBackground({ mode: "bad" })', rendererContext); assert.equal(dataset.background, "standard");
// 测试真实 renderer 手柄逻辑：只响应左键、捕获指针、失焦/取消必须停止缩放。
const pointerCalls = [], listeners = {}, windowListeners = {};
const handle = { dataset: { edge: "se" }, setPointerCapture(id) { pointerCalls.push(["capture", id]); },
  addEventListener(type, fn) { listeners[type] = fn; } };
const pointerContext = vm.createContext({
  document: { documentElement: { dataset: {} }, querySelectorAll: () => [handle] },
  window: { bridge: { resizeChat(...args) { pointerCalls.push(args); } },
    addEventListener(type, fn) { windowListeners[type] = fn; } },
});
vm.runInContext(html.slice(html.indexOf("    if (window.bridge?.resizeChat)"), html.indexOf("    function applyTheme(")), pointerContext);
const pointerEvent = { button: 0, pointerId: 7, preventDefault() {} };
listeners.pointerdown({ ...pointerEvent, button: 2 }); assert.equal(pointerCalls.length, 0);
listeners.pointerdown(pointerEvent);
assert.deepEqual(pointerCalls[0], ["capture", 7]); assert.deepEqual(pointerCalls[1], ["start", "se"]);
windowListeners.pointermove({ pointerId: 8 }); assert.equal(pointerCalls.length, 2);
windowListeners.pointermove(pointerEvent); assert.deepEqual(pointerCalls.at(-1), ["move"]);
windowListeners.pointercancel(pointerEvent); assert.deepEqual(pointerCalls.at(-1), ["end"]);
const afterCancel = pointerCalls.length; windowListeners.pointermove(pointerEvent); assert.equal(pointerCalls.length, afterCancel);
listeners.pointerdown(pointerEvent); windowListeners.blur({}); assert.deepEqual(pointerCalls.at(-1), ["end"]);
listeners.pointerdown(pointerEvent); listeners.lostpointercapture(pointerEvent); assert.deepEqual(pointerCalls.at(-1), ["end"]);

// 菜单显示有效模式，并在回退/保存失败时明确说明原因。
const migratedMenu = vm.runInContext('updateChatBackground("acrylic"); chatBackgroundMenu()', context);
assert.equal(migratedMenu.submenu.length, 3);
assert.equal(migratedMenu.submenu[1].checked, true);
context.app.isHardwareAccelerationEnabled = () => false;
const fallbackMenu = vm.runInContext('updateChatBackground("transparent"); chatBackgroundMenu()', context);
assert.equal(fallbackMenu.submenu[0].checked, true);
assert.equal(fallbackMenu.submenu[1].enabled, false);
assert.equal(fallbackMenu.submenu[2].enabled, false);
assert(fallbackMenu.submenu[1].label.includes("硬件加速"));
assert(fallbackMenu.submenu.at(-1).label.includes("硬件加速"));
context.app.isHardwareAccelerationEnabled = () => true;
context.saveBackground = () => { throw Error("disk full"); };
context.console = { error() {} };
const failedSave = vm.runInContext('updateChatBackground("standard", true)', context);
assert.equal(failedSave.mode, "standard"); assert(failedSave.reason.includes("保存失败"));

assert(!main.includes("setOpacity("), "must not fade text/buttons");
assert(!main.includes("chatWin.setIgnoreMouseEvents"), "chat must not introduce click-through");
assert(!main.slice(main.indexOf("function updateChatBackground"), main.indexOf("function positionForegroundNotice")).includes("loadFile"), "switching backgrounds must not reload chat");
assert(preload.includes('ipcRenderer.removeListener("chat-background", handler)'));
assert(!html.includes("acrylic"), "removed background must not remain in renderer/CSS");
assert(html.includes('background: var(--window-bg)'));
assert(html.includes(':is(.toolbar label, #voiceHint)'), 'voice controls keep their background cards');
const stepRules = [...html.matchAll(/([^{}]+)\{([^{}]*)\}/g)].filter(([, selector]) => /\.step(?![\w-])/.test(selector));
assert(stepRules.length > 0, 'tool step styles must still exist');
for (const [, selector, declarations] of stepRules) {
  assert(!/(?:^|;)\s*(?:background(?:-[\w-]+)?|border(?:-[\w-]+)?|box-shadow)\s*:/.test(declarations), 'tool steps must not gain background boxes in any mode: ' + selector.trim());
}
assert(html.includes(':root[data-background="transparent"] { --surface-opacity: 70%; }'));
assert(!/(?:^|[;\s{])opacity\s*:/.test(html.slice(html.indexOf('/* 全透明模式：'), html.indexOf('    .window-resize'))), 'do not fade foreground content');
assert(html.includes('padding: 2px 8px'), 'voice wake-up control needs side breathing room');
assert(!html.includes(':is(.step, .event, .timesep, .task-duration, .toolbar label, #voiceHint)'), 'history, timestamps and durations must not receive opaque cards');
assert(/:root\[data-background="transparent"\] \{\s*--window-bg: transparent;\s*--window-panel: transparent;/.test(html));
assert(html.includes('background: var(--panel); border: 1px solid var(--border-strong)'));
assert(html.includes(':root[data-maximized="true"] .window-resize { display: none; }'));
const pkg = JSON.parse(fs.readFileSync(path.join(__dirname, "package.json"), "utf8"));
assert(pkg.build.files.includes("chat_background.js"));
console.log("chat background: three modes, legacy migration, rounded shape, persistence, fallbacks, menu, IPC and resize passed");

{
  sender.mainFrame = {};
  const floatSender = {};
  context.floatWin = {webContents:{on(event, callback) { assert.equal(event, "context-menu"); context.openFloatMenu = callback; }}};
  context.Menu = {buildFromTemplate(template) { context.floatMenu = template; return {popup() {}}; }};
  context.uiLanguage = "zh";
  vm.runInContext(main.slice(main.indexOf('ipcMain.on("set-ui-language"'), main.indexOf('ipcMain.handle("set-chat-theme"')), context);
  const start = main.indexOf('  floatWin.webContents.on("context-menu"');
  vm.runInContext(main.slice(start, main.indexOf('\n}\n', start)), context);
  context.nativeTheme = {};
  context.saveBackground = (file, mode) => saved.push({file,mode});
  vm.runInContext('updateChatBackground("standard")', context);
  const set = (language, source=sender, frame=source.mainFrame) => events["set-ui-language"]({sender:source,senderFrame:frame},language);
  const labels = () => { context.openFloatMenu(); return Array.from(context.floatMenu, item=>item.label).filter(Boolean); };
  assert.deepEqual(labels(), ["打开小A", "聊天窗口背景", "退出小A"]);
  set("en");
  assert.deepEqual(labels(), ["Open XiaoA", "Chat window background", "Quit XiaoA"]);
  assert.deepEqual(Array.from(context.floatMenu[1].submenu, item=>item.label), ["Standard background", "Translucent background", "Transparent background"]);
  context.floatMenu[1].submenu[1].click(); assert.equal(saved.at(-1).mode, "translucent");
  context.nativeTheme.prefersReducedTransparency = true;
  vm.runInContext('updateChatBackground("translucent")', context);
  labels();
  assert(context.floatMenu[1].submenu[1].label.includes("High contrast or reduced transparency is enabled"));
  assert.equal(context.floatMenu[1].submenu[1].enabled, false);
  set("zh", floatSender); assert.equal(context.uiLanguage,"en", "untrusted windows cannot change menu language");
  set("zh", sender, {}); assert.equal(context.uiLanguage,"en", "subframes cannot change menu language");
  set("bad"); assert.equal(context.uiLanguage,"en");
  set("zh"); assert.deepEqual(labels(), ["打开小A", "聊天窗口背景", "退出小A"]);
  assert(preload.includes('ipcRenderer.send("set-ui-language", language)'));
}
console.log("Native menus: live language changes, background options, fallbacks and trusted IPC passed");
