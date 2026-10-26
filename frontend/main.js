// Electron 主进程:桌面悬浮小A + 可收起的对话面板。
const { app, BrowserWindow, Menu, screen, ipcMain, session, shell, nativeTheme, dialog } = require("electron");
const path = require("path");
const fs = require("fs");
const os = require("os");
const { fileURLToPath } = require("url");
const { spawn, spawnSync } = require("child_process");

const { translate: translateUi } = require("./src/ui-language");
let uiLanguage = "zh";
const nativeText = text => translateUi(text, uiLanguage);

const startupStartedAt = Date.now();
if (process.platform === "win32") {
  // Portable 每次解压路径可变，Windows 应用标识保持一致。
  app.setAppUserModelId("com.xiaoa.desktop");
}
let backendFirstHealthy = false;
const { applyLoginItemSetting } = require("./login_item");
const { preparePluginRuntimes } = require("./plugin_runtimes");
const { createFloatAlertQueue } = require("./float_alert");
const { loadBackground, saveBackground, backgroundAvailability, applyBackground } = require("./chat_background");
const {
  FLOAT_WIDTH,
  FLOAT_HEIGHT,
  clampFloatPosition,
  createCircleShape,
  createRoundedRectShape,
} = require("./float_position");

let floatWin; // 悬浮球窗口
let chatWin; // 对话面板窗口
let quitting = false;
let chatBackgroundRequested = "standard";
let chatBackgroundState = { requested: "standard", mode: "standard", reason: "" };
let chatResize = null;
let foregroundNoticeWin;
const FLOAT_POSITION_FILE = "float_position.json";
const floatAlerts = createFloatAlertQueue((state) => {
  if (floatWin && !floatWin.isDestroyed()) {
    floatWin.webContents.send("float-alert", state);
    // 闪烁时临时放大裁剪圆(72→86),让向外扩散的波纹有行程;结束回到默认,不残留。
    // 排队切换(红→绿)时 state.active 保持 true,不会来回抖动。
    floatWin.setShape(state.active ? createCircleShape(86, 1) : createCircleShape());
  }
});

function keepOutOfTaskbar(win) {
  if (!win || win.isDestroyed()) return;
  try { win.setSkipTaskbar(true); } catch (error) {
    console.error("隐藏任务栏按钮失败:", error.message);
  }
}

const hasSingleInstanceLock = app.requestSingleInstanceLock();
if (!hasSingleInstanceLock) {
  app.quit();
} else {
  app.on("second-instance", () => {
    clearFloatAlert();
    if (chatWin && !chatWin.isDestroyed()) {
      chatWin.show();
      chatWin.focus();
    }
  });
}

function lockNavigation(win, allowedFiles = []) {
  const allowed = new Set(allowedFiles.map((file) => path.resolve(file)));
  win.webContents.setWindowOpenHandler(() => ({ action: "deny" }));
  win.webContents.on("will-navigate", (event, url) => {
    if (!url.startsWith("file://")) return event.preventDefault();
    try {
      if (!allowed.has(path.resolve(fileURLToPath(url)))) event.preventDefault();
    } catch (_) {
      event.preventDefault();
    }
  });
}

function clampFloatToDisplay(x, y) {
  const display = screen.getDisplayNearestPoint({
    x: Number(x) + FLOAT_WIDTH / 2,
    y: Number(y) + FLOAT_HEIGHT / 2,
  });
  return clampFloatPosition(x, y, display.workArea);
}

function saveFloatPosition() {
  if (!floatWin || floatWin.isDestroyed()) return;
  try {
    const [x, y] = floatWin.getPosition();
    const directory = path.join(resolveAppHome(), "data");
    fs.mkdirSync(directory, { recursive: true });
    const target = path.join(directory, FLOAT_POSITION_FILE);
    const temporary = `${target}.tmp`;
    fs.writeFileSync(temporary, JSON.stringify({ x, y }), "utf8");
    fs.renameSync(temporary, target);
  } catch (e) {
    console.error("悬浮球位置保存失败:", e.message);
  }
}

function loadFloatPosition() {
  try {
    const raw = JSON.parse(fs.readFileSync(
      path.join(resolveAppHome(), "data", FLOAT_POSITION_FILE), "utf8",
    ));
    if (!Number.isFinite(raw.x) || !Number.isFinite(raw.y)) return null;
    return clampFloatToDisplay(raw.x, raw.y);
  } catch (_) {
    return null;
  }
}

function backgroundSettingsPath() {
  return path.join(resolveAppHome(), "data", "chat_background.json");
}

function updateChatShape() {
  if (process.platform !== "win32" || !chatWin || chatWin.isDestroyed()) return;
  // 普通窗口统一使用 16px 原生圆角，最大化或全屏恢复矩形区域。
  try {
    const [width, height] = chatWin.getSize();
    chatWin.setShape(chatWin.isMaximized() || chatWin.isFullScreen()
      ? [] : createRoundedRectShape(width, height));
  } catch (error) {
    console.error("聊天窗口圆角裁剪失败:", error.message); // CSS clipping remains as a fallback.
  }
}

function updateChatBackground(mode = chatBackgroundRequested, persist = false) {
  if (!chatWin || chatWin.isDestroyed()) return chatBackgroundState;
  chatBackgroundState = applyBackground(chatWin, mode, backgroundAvailability({
    platform: process.platform,
    hardwareAcceleration: app.isHardwareAccelerationEnabled(), nativeTheme,
  }));
  updateChatShape();
  chatBackgroundRequested = chatBackgroundState.requested;
  if (persist) {
    try { saveBackground(backgroundSettingsPath(), chatBackgroundRequested); }
    catch (error) {
      chatBackgroundState.reason = "效果已切换，但保存失败；重启后可能恢复原设置";
      console.error("聊天背景设置保存失败:", error.message);
    }
  }
  chatWin.webContents.send("chat-background", chatBackgroundState);
  return chatBackgroundState;
}

function chatBackgroundMenu() {
  const state = chatBackgroundState;
  const options = [["standard", "标准背景", ""],
    ["translucent", "半透明背景", state.translucentReason],
    ["transparent", "全透明背景", state.translucentReason]];
  const submenu = options.map(([mode, label, reason]) => ({
    label: reason ? nativeText(label) + (uiLanguage === "en" ? ` (${nativeText(reason)})` : `（${reason}）`) : nativeText(label),
    type: "radio", checked: state.mode === mode, enabled: !reason,
    click: () => updateChatBackground(mode, true),
  }));
  if (state.reason) submenu.push({ type: "separator" }, { label: nativeText(state.reason), enabled: false });
  return { label: nativeText("聊天窗口背景"), submenu };
}

function positionForegroundNotice() {
  if (!foregroundNoticeWin || foregroundNoticeWin.isDestroyed()) return;
  const cursor = screen.getCursorScreenPoint();
  const display = screen.getDisplayNearestPoint(cursor);
  const bounds = display.workArea;
  const [width] = foregroundNoticeWin.getSize();
  foregroundNoticeWin.setPosition(
    Math.round(bounds.x + (bounds.width - width) / 2),
    Math.round(bounds.y + bounds.height - foregroundNoticeWin.getBounds().height - 14),
  );
}

function createForegroundNotice() {
  foregroundNoticeWin = new BrowserWindow({
    width: 520,
    height: 72,
    frame: false,
    transparent: true,
    resizable: false,
    movable: false,
    focusable: false,
    skipTaskbar: true,
    show: false,
    hasShadow: false,
    webPreferences: {
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
    },
  });
  foregroundNoticeWin.setContentProtection(true); // Keep the takeover overlay out of desktop observations on Windows.
  foregroundNoticeWin.setAlwaysOnTop(true, "screen-saver");
  foregroundNoticeWin.setIgnoreMouseEvents(true, { forward: true });
  keepOutOfTaskbar(foregroundNoticeWin);
  lockNavigation(foregroundNoticeWin, [path.join(__dirname, "src", "foreground_notice.html")]);
  foregroundNoticeWin.loadFile(path.join(__dirname, "src", "foreground_notice.html"));
  foregroundNoticeWin.on("closed", () => { foregroundNoticeWin = null; });
}

function setForegroundNotice(active) {
  if (!foregroundNoticeWin || foregroundNoticeWin.isDestroyed()) return;
  if (active) {
    positionForegroundNotice();
    foregroundNoticeWin.setAlwaysOnTop(true, "screen-saver");
    foregroundNoticeWin.showInactive();
  } else {
    foregroundNoticeWin.hide();
  }
}

function createFloat() {
  const cursor = screen.getCursorScreenPoint();
  const display = screen.getDisplayNearestPoint(cursor);
  const fallback = clampFloatPosition(
    display.workArea.x + display.workArea.width - 98,
    display.workArea.y + 20,
    display.workArea,
  );
  const position = loadFloatPosition() || fallback;
  floatWin = new BrowserWindow({
    width: FLOAT_WIDTH,
    height: FLOAT_HEIGHT,
    x: position.x,
    y: position.y,
    frame: false,
    transparent: true,
    resizable: false,
    alwaysOnTop: true,
    skipTaskbar: true,
    show: false,
    webPreferences: {
      preload: path.join(__dirname, "preload.js"),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
    },
  });
  floatWin.setAlwaysOnTop(true, "screen-saver");
  keepOutOfTaskbar(floatWin);
  floatWin.once("ready-to-show", () => {
    if (quitting || floatWin.isDestroyed()) return;
    keepOutOfTaskbar(floatWin);
    // 更新器隐藏的是 Portable 外壳；悬浮球在加载完成后主动显示。
    floatWin.showInactive();
  });
  floatWin.on("show", () => keepOutOfTaskbar(floatWin));
  // 系统级圆形窗口：圆外不属于本窗口，天然穿透且不混用屏幕/CSS 坐标。
  floatWin.setShape(createCircleShape());
  const floatPage = path.join(__dirname, "src", "float.html");
  lockNavigation(floatWin, [floatPage]);
  floatWin.loadFile(floatPage);
  floatWin.on("close", () => {
    if (!quitting) {
      quitting = true;
      app.quit();
    }
  });
  floatWin.webContents.on("context-menu", () => {
    Menu.buildFromTemplate([
      {
        label: nativeText("打开小A"),
        click: () => {
          clearFloatAlert();
          if (chatWin) { chatWin.show(); chatWin.focus(); }
        },
      },
      chatBackgroundMenu(),
      { type: "separator" },
      { label: nativeText("退出小A"), click: () => { quitting = true; app.quit(); } },
    ]).popup({ window: floatWin });
  });
}

function waitForBackend(cb, tries) {
  // 等后端起来再加载页面(首次启动后端要下模型,可能要几十秒)
  tries = tries || 0;
  const http = require("http");
  const req = http.get("http://127.0.0.1:1985/health", (r) => {
    r.destroy();
    cb(true);
  });
  req.on("error", () => {
    if (tries > 60) { cb(false); return; }   // 最多等 ~60 秒
    setTimeout(() => waitForBackend(cb, tries + 1), 1000);
  });
  req.setTimeout(1500, () => req.destroy());
}

function createChat() {
  const { width } = screen.getPrimaryDisplay().workAreaSize;
  chatWin = new BrowserWindow({
    width: 420,
    minWidth: 360,
    height: 640,
    minHeight: 420,
    x: width - 460,
    y: 100,
    frame: false,
    transparent: true,
    backgroundColor: "#00000000",
    resizable: true,
    alwaysOnTop: true,
    skipTaskbar: true,
    show: false,
    webPreferences: {
      preload: path.join(__dirname, "preload.js"),
      contextIsolation: true,
      nodeIntegration: false,
      // 收进悬浮球后 chatWin 被 hide(),Electron 默认会节流后台窗口、暂停
      // requestAnimationFrame —— 靠 rAF 驱动的语音唤醒/静音检测(VAD)会整个停摆,
      // 而"收起也能喊小A"是唤醒功能的设计前提。关掉节流,隐藏时 rAF 仍照常跑。
      backgroundThrottling: false,
    },
  });
  keepOutOfTaskbar(chatWin);
  chatWin.on("ready-to-show", () => keepOutOfTaskbar(chatWin));
  chatWin.webContents.on("render-process-gone", () => setForegroundNotice(false));
  chatWin.webContents.on("did-start-loading", () => setForegroundNotice(false));
  chatWin.on("show", () => {
    keepOutOfTaskbar(chatWin);
    // If the panel fills the screen, keep the floating icon accessible above it.
    if (floatWin && !floatWin.isDestroyed()) floatWin.showInactive();
  });
  // 从本地文件加载界面(loadURL 到 http 会触发 Electron 的 SSL 握手报错)。
  // 本机连接已在后端免密码(_is_local),所以 file:// 下的 WebSocket 也能连上;
  // 后端没起来时前端自己每 1.5s 重连,起来后自动接上。
  const chatPage = path.join(__dirname, "src", "index.html");
  lockNavigation(chatWin, [chatPage]);
  updateChatBackground();
  chatWin.loadFile(chatPage);
  chatWin.webContents.on("context-menu", (_event, params) => {
    if (params.mediaType !== "image") return;
    Menu.buildFromTemplate([
      {
        label: nativeText("复制图片"),
        click: () => {
          if (chatWin && !chatWin.isDestroyed()) chatWin.webContents.copyImageAt(params.x, params.y);
        },
      },
    ]).popup({ window: chatWin });
  });
  const sendMaximized = () => {
    if (chatWin && !chatWin.isDestroyed()) {
      chatResize = null;
      updateChatShape();
      chatWin.webContents.send("chat-maximized", chatWin.isMaximized() || chatWin.isFullScreen());
    }
  };
  chatWin.on("resize", updateChatShape);
  chatWin.on("enter-full-screen", sendMaximized);
  chatWin.on("leave-full-screen", sendMaximized);
  chatWin.on("maximize", sendMaximized);
  chatWin.on("unmaximize", sendMaximized);
  chatWin.on("close", (e) => {
    if (quitting) return;
    // X 按钮表示退出小A；“收起”仍由界面按钮/悬浮球负责隐藏。
    quitting = true;
    app.quit();
  });
}

// 悬浮球被点击 -> 切换对话面板显隐
ipcMain.on("toggle-chat", () => {
  if (!chatWin) return;
  clearFloatAlert();
  if (chatWin.isVisible()) chatWin.hide();
  else chatWin.show();
});

// 对话面板里点"收起" -> 隐藏面板
ipcMain.on("hide-chat", () => {
  if (chatWin && chatWin.isVisible()) chatWin.hide();
});

ipcMain.handle("get-chat-background", (event) => {
  return event.sender === chatWin?.webContents ? chatBackgroundState : null;
});

// The chat's persisted UI preference also drives native menus; no second setting.
ipcMain.on("set-ui-language", (event, language) => {
  if (event.sender !== chatWin?.webContents || event.senderFrame !== event.sender.mainFrame) return;
  if (language === "zh" || language === "en") uiLanguage = language;
});

ipcMain.handle("set-chat-theme", (event, theme) => {
  if (event.sender !== chatWin?.webContents || !["light", "dark"].includes(theme)) return;
  nativeTheme.themeSource = theme;
});

// 透明窗口没有 Windows 原生 thick frame，用边缘手柄补足缩放；桌面 DIP 坐标避免 DPI 混用。
ipcMain.on("resize-chat", (event, action, edge) => {
  if (!chatWin || chatWin.isDestroyed() || event.sender !== chatWin.webContents) return;
  if (action === "end" || chatWin.isMaximized()) { chatResize = null; return; }
  if (action === "start" && ["n", "s", "e", "w", "ne", "nw", "se", "sw"].includes(edge)) {
    chatResize = { edge, bounds: chatWin.getBounds(), cursor: screen.getCursorScreenPoint() };
  } else if (action === "move" && chatResize) {
    const { edge, bounds, cursor } = chatResize;
    const current = screen.getCursorScreenPoint();
    const dx = current.x - cursor.x, dy = current.y - cursor.y;
    let { x, y, width, height } = bounds;
    if (edge.includes("e")) width = Math.max(360, width + dx);
    if (edge.includes("s")) height = Math.max(420, height + dy);
    if (edge.includes("w")) { width = Math.max(360, width - dx); x += bounds.width - width; }
    if (edge.includes("n")) { height = Math.max(420, height - dy); y += bounds.height - height; }
    chatWin.setBounds({ x, y, width, height });
  }
});

ipcMain.handle("toggle-chat-maximize", (event) => {
  const win = BrowserWindow.fromWebContents(event.sender);
  if (!win || win !== chatWin || win.isDestroyed()) return false;
  if (win.isMaximized()) win.unmaximize();
  else win.maximize();
  return win.isMaximized();
});

ipcMain.handle("get-chat-maximized", (event) => {
    const win = BrowserWindow.fromWebContents(event.sender);
    return Boolean(win && win === chatWin && !win.isDestroyed() && win.isMaximized());
});

ipcMain.handle("set-open-at-login", (event, enabled) => {
  const win = BrowserWindow.fromWebContents(event.sender);
  if (!win || win !== chatWin || win.isDestroyed()) return { available: false };
  return applyLoginItemSetting(app, Boolean(enabled));
});

function clearFloatAlert(kind) {
  floatAlerts.clear(kind);
}

ipcMain.on("foreground-control", (event, active) => {
  if (!chatWin || event.sender !== chatWin.webContents) return;
  setForegroundNotice(Boolean(active));
});

ipcMain.on("float-notify", (_e, { kind } = {}) => {
  if (chatWin && chatWin.isVisible()) return;
  floatAlerts.enqueue(kind);
});

ipcMain.on("float-alert-clear", (_e, { kind } = {}) => clearFloatAlert(kind));

// 悬浮球手动拖动:按住移动多少,窗口就移动多少
ipcMain.on("float-drag", (_e, { dx, dy }) => {
  if (!floatWin || floatWin.isDestroyed()) return;
  if (!Number.isFinite(dx) || !Number.isFinite(dy)) return;
  const [x, y] = floatWin.getPosition();
  const position = clampFloatToDisplay(x + dx, y + dy);
  floatWin.setPosition(position.x, position.y);
  saveFloatPosition();
});

// 用系统默认浏览器打开设置/音色页(避免在 Electron 里开成空白窗)
ipcMain.on("open-page", (_e, urlPath) => {
  if (!["/settings", "/voices"].includes(urlPath)) return;
  shell.openExternal("http://127.0.0.1:1985" + urlPath);
});

// 把截图存临时文件,用系统图片查看器打开(可全屏缩放)
ipcMain.on("open-image", (_e, b64) => {
  try {
    if (typeof b64 !== "string" || b64.length > 32 * 1024 * 1024) return;
    const buf = Buffer.from(b64, "base64");
    const pngSignature = Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]);
    const isPng = buf.length >= 8 && buf.subarray(0, 8).equals(pngSignature);
    const isJpeg = buf.length >= 3 && buf[0] === 0xff && buf[1] === 0xd8 && buf[2] === 0xff;
    if (!isPng && !isJpeg) return;
    const extension = isPng ? "png" : "jpg";
    // 截图预览与后端截图统一放进隔离工作区，不落到系统临时目录。
    const directory = path.join(resolveAppHome(), "data", "isolation", "workspace", "screenshots");
    fs.mkdirSync(directory, { recursive: true });
    const p = path.join(directory, "xiaoa-shot-" + require("crypto").randomUUID() + "." + extension);
    fs.writeFileSync(p, buf);
    shell.openPath(p);
  } catch (e) { /* ignore */ }
});

// 在资源管理器中定位任务产物/附件,不强制下载一份副本。
ipcMain.on("reveal-path", (_e, filePath) => {
  try {
    if (typeof filePath !== "string" || !path.isAbsolute(filePath)) return;
    const resolved = path.resolve(filePath);
    if (!fs.existsSync(resolved)) return;
    shell.showItemInFolder(resolved);
  } catch (e) { /* ignore */ }
});

// 后端进程句柄(打包成 exe 后由本程序拉起后端,不用再单独跑 bat)
let backendProc = null;
let backendProbeInFlight = false;
let backendLaunchInFlight = false;
let backendHealthTimer = null;
let backendHealthFailures = 0;
let backendStartupDeadline = 0;
let backendRestartInFlight = false;
const BACKEND_FAILURE_LIMIT = 3;
const BACKEND_LOG_MAX_BYTES = 5 * 1024 * 1024;
const BACKEND_LOG_KEEP = 3;

function resolveAppHome() {
  // 必须和后端 config.app_home() 解析出的目录一致,否则读不到后端写的 ui_token.txt。
  const portableDir = process.env.PORTABLE_EXECUTABLE_DIR;
  const defaultHome = app.isPackaged
    ? (portableDir ? path.join(portableDir, "XiaoA-data") : app.getPath("userData"))
    : path.join(__dirname, "..");
  return process.env.XIAOA_HOME || defaultHome;
}

// 把后端写的本机 UI 令牌读给渲染进程(只有本机进程读得到这个文件,恶意网页读不到)。
ipcMain.handle("get-ui-token", () => {
  try {
    return fs.readFileSync(path.join(resolveAppHome(), "ui_token.txt"), "utf8").trim();
  } catch (e) {
    return "";
  }
});

function rotateBackendLog(logPath) {
  try {
    if (!fs.existsSync(logPath) || fs.statSync(logPath).size < BACKEND_LOG_MAX_BYTES) return;
    const oldest = `${logPath}.${BACKEND_LOG_KEEP}`;
    if (fs.existsSync(oldest)) fs.rmSync(oldest, { force: true });
    for (let i = BACKEND_LOG_KEEP - 1; i >= 1; i -= 1) {
      const source = `${logPath}.${i}`;
      if (fs.existsSync(source)) fs.renameSync(source, `${logPath}.${i + 1}`);
    }
    fs.renameSync(logPath, `${logPath}.1`);
  } catch (e) {
    console.error("后端日志轮换失败:", e.message);
  }
}

function appendBackendLog(logPath, chunk) {
  try {
    const bytes = Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk);
    for (let offset = 0; offset < bytes.length; offset += 65536) {
      rotateBackendLog(logPath);
      fs.appendFileSync(logPath, bytes.subarray(offset, offset + 65536));
    }
  } catch (e) {
    console.error("写入运行日志失败:", e.message);
  }
}

function logStartup(message) {
  console.log(message);
  try {
    const directory = path.join(resolveAppHome(), "data", "isolation", "logs");
    fs.mkdirSync(directory, { recursive: true });
    appendBackendLog(path.join(directory, "startup.log"), `${new Date().toISOString()} ${message}\n`);
  } catch (e) {
    console.error("写入启动日志失败:", e.message);
  }
}

function terminateBackend() {
  const child = backendProc;
  backendStartupDeadline = 0;
  backendProc = null;
  backendLaunchInFlight = false;
  if (!child || child.killed) return;
  try {
    if (process.platform === "win32") {
      spawnSync("taskkill", ["/PID", String(child.pid), "/T", "/F"], {
        windowsHide: true, stdio: "ignore",
      });
    } else {
      child.kill("SIGTERM");
    }
  } catch (_) {}
}

function restartBackend() {
  if (backendRestartInFlight || quitting) return;
  backendRestartInFlight = true;
  console.error("后端连续健康检查失败,正在重启");
  terminateBackend();
  setTimeout(() => {
    backendRestartInFlight = false;
    backendHealthFailures = 0;
    launchBackend();
  }, 800);
}

async function launchBackend() {
  if (quitting || backendLaunchInFlight || (backendProc && !backendProc.killed)) return;
  logStartup(`[startup] launchBackend +${Date.now() - startupStartedAt}ms`);
  // 打包版使用内置后端;开发版使用本机 venv。
  const appHome = resolveAppHome();
  let command;
  let args;
  let cwd;
  if (app.isPackaged) {
    command = path.join(process.resourcesPath, "backend", "xiaoa-backend.exe");
    args = [];
    cwd = appHome;
  } else {
    const venvPy = path.join(os.homedir(), "xiaoa-venv", "Scripts", "python.exe");
    command = fs.existsSync(venvPy) ? venvPy : "python";
    args = [
      "-m", "uvicorn", "backend.api.server:app",
      "--host", "127.0.0.1", "--port", "1985",
      "--no-access-log",
    ];
    if (process.env.XIAOA_DEV_RELOAD === "1") {
      args.push("--reload", "--reload-dir", "backend");
    }
    cwd = path.join(__dirname, "..");
  }
  if (app.isPackaged && !fs.existsSync(command)) {
    console.error("内置后端不存在:", command);
    return;
  }
  backendLaunchInFlight = true;
  try {
    fs.mkdirSync(appHome, { recursive: true });
    const childEnv = {
      ...process.env,
      XIAOA_HOME: appHome,
      // 更新重启不应继承后端任务隔离区的 TEMP，否则外壳会解压进待清理缓存。
      XIAOA_LAUNCH_TEMP: os.tmpdir(),
      XIAOA_PARENT_PID: String(process.pid),
      XIAOA_USER_DATA_DIR: app.getPath("userData"),
      // PID 会复用；把 Electron 的创建时间一起传给后端核对进程身份。
      XIAOA_PARENT_START_TIME: typeof process.getCreationTime === "function"
        ? String(process.getCreationTime()) : "",
      ...(app.isPackaged ? { XIAOA_EXECUTABLE_PATH: process.env.PORTABLE_EXECUTABLE_FILE || process.execPath } : {}),
      HF_HOME: path.join(appHome, "model-cache"),
      PYTHONUTF8: "1",
      PYTHONUNBUFFERED: "1",
      PYTHONIOENCODING: "utf-8",
    };
    if (app.isPackaged) {
      // Optional plugin provisioning must not disable chat and built-in tools.
      delete childEnv.XIAOA_PLUGIN_RUNTIME_ERROR;
      try {
        Object.assign(childEnv, await preparePluginRuntimes(process.resourcesPath, appHome));
      } catch (error) {
        childEnv.XIAOA_PLUGIN_RUNTIME_ERROR = `插件运行环境初始化失败：${error.message}`;
        for (const key of ["XIAOA_PLUGIN_PYTHON", "XIAOA_PLUGIN_NODE", "XIAOA_PLUGIN_NPM"]) delete childEnv[key];
        logStartup(`[startup] ${childEnv.XIAOA_PLUGIN_RUNTIME_ERROR}`);
      }
    }
    // Runtime preparation can outlive the window; never launch after quitting.
    if (quitting) { backendLaunchInFlight = false; return; }
    const logDirectory = path.join(appHome, "data", "isolation", "logs");
    fs.mkdirSync(logDirectory, { recursive: true });
    const logPath = path.join(logDirectory, "backend.log");
    rotateBackendLog(logPath);
    backendProc = spawn(command, args, {
      cwd,
      env: childEnv,
      detached: false,
      windowsHide: true,
      stdio: ["ignore", "pipe", "pipe"],
    });
    const child = backendProc;
    backendStartupDeadline = Date.now() + 60000;
    logStartup(`[startup] backend spawned +${Date.now() - startupStartedAt}ms`);
    // Do not keep a Windows file handle open while rotating the destination.
    for (const stream of [child.stdout, child.stderr]) {
      stream.on("data", chunk => appendBackendLog(logPath, chunk));
      stream.on("error", e => console.error("读取后端日志失败:", e.message));
    }
    backendLaunchInFlight = false;
    child.on("error", (e) => {
      backendLaunchInFlight = false;
      if (backendProc === child) backendProc = null;
      console.error("拉起后端失败:", e.message);
    });
    child.on("exit", () => {
      if (backendProc === child) backendProc = null;
      backendLaunchInFlight = false;
      if (!quitting && !backendRestartInFlight) setTimeout(startBackend, 1500);
    });
    console.log("已拉起后端:", command);
  } catch (e) {
    backendLaunchInFlight = false;
    console.error("无法启动后端:", e.message);
  }
}

function startBackend() {
  if (backendProbeInFlight) return;
  backendProbeInFlight = true;
  const http = require("http");
  // A listening port can belong to another install; never silently use its code or data.
  const probe = http.get("http://127.0.0.1:1985/health", (r) => {
    let body = "";
    r.setEncoding("utf8");
    r.on("data", chunk => {
      body += chunk;
      if (body.length > 65536) probe.destroy();
    });
    r.on("error", () => probe.destroy());
    r.on("end", () => {
      backendProbeInFlight = false;
      let health;
      try { health = JSON.parse(body); } catch (_) {}
      if (r.statusCode < 200 || r.statusCode >= 300 || !health?.ok) {
        handleBackendFailure();
        return;
      }
      if (health.version !== require("./package.json").version ||
          health.frontend_pid !== String(process.pid)) {
        quitting = true;
        dialog.showErrorBox("小A启动冲突", "本机 1985 端口正在运行另一份小A或旧后端。请先正常退出其他小A，再从当前目录启动，避免连接旧版本。当前程序将退出。");
        app.quit();
        return;
      }
      backendHealthFailures = 0;
      backendStartupDeadline = 0;
      if (!backendFirstHealthy) {
        backendFirstHealthy = true;
        startBackendWatchdog();
        if (chatWin && !chatWin.isDestroyed()) chatWin.webContents.send("backend-ready");
        logStartup(`[startup] backend health ready after ${Date.now() - startupStartedAt}ms`);
      }
    });
  });
  probe.on("error", () => {
    backendProbeInFlight = false;
    handleBackendFailure();
  });
  probe.setTimeout(2000, () => probe.destroy());
}

function handleBackendFailure() {
  if (!backendProc || backendProc.killed) {
    launchBackend();
    return;
  }
  if (Date.now() < backendStartupDeadline) return;
  backendHealthFailures += 1;
  if (backendHealthFailures >= BACKEND_FAILURE_LIMIT) restartBackend();
}

function startBackendWatchdog() {
  if (backendHealthTimer) clearInterval(backendHealthTimer);
  backendHealthTimer = setInterval(() => {
    if (!quitting) startBackend();
  }, backendFirstHealthy ? 5000 : 250);
}

if (hasSingleInstanceLock) app.whenReady().then(() => {
  logStartup("[startup] app.whenReady");
  // 只允许本地界面申请麦克风,其它 Electron 权限一律拒绝。
  session.defaultSession.setPermissionRequestHandler((wc, perm, cb) => {
    cb(perm === "media" && wc.getURL().startsWith("file://"));
  });
  chatBackgroundRequested = loadBackground(backgroundSettingsPath());
  nativeTheme.on("updated", () => updateChatBackground());
  screen.on("display-metrics-changed", () => positionForegroundNotice());
  screen.on("display-added", () => positionForegroundNotice());
  screen.on("display-removed", () => positionForegroundNotice());
  startBackend();          // 先把后端拉起来(已在运行则跳过)
  startBackendWatchdog();
  createForegroundNotice();
  createFloat();
  createChat();
  logStartup(`[startup] windows created, total +${Date.now() - startupStartedAt}ms`);
});

app.on("window-all-closed", () => {
  if (process.platform !== "darwin") app.quit();
});

app.on("before-quit", () => {
  quitting = true;
  saveFloatPosition();
  if (foregroundNoticeWin && !foregroundNoticeWin.isDestroyed()) {
    setForegroundNotice(false);
    foregroundNoticeWin.close();
  }
  if (backendHealthTimer) {
    clearInterval(backendHealthTimer);
    backendHealthTimer = null;
  }
  terminateBackend();
});
// will-quit 兜底覆盖系统注销/关机等退出时序。
app.on("will-quit", terminateBackend);
