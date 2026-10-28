// 安全桥:只暴露必要的 IPC 给页面,不开放 node 全权限。
const { contextBridge, ipcRenderer } = require("electron");

contextBridge.exposeInMainWorld("bridge", {
  toggleChat: () => ipcRenderer.send("toggle-chat"),
  hideChat: () => ipcRenderer.send("hide-chat"),
  toggleMaximize: () => ipcRenderer.invoke("toggle-chat-maximize"),
  getMaximized: () => ipcRenderer.invoke("get-chat-maximized"),
  getChatBackground: () => ipcRenderer.invoke("get-chat-background"),
  setUiLanguage: (language) => ipcRenderer.send("set-ui-language", language),
  setChatTheme: (theme) => ipcRenderer.invoke("set-chat-theme", theme),
  resizeChat: (action, edge) => ipcRenderer.send("resize-chat", action, edge),
  onChatBackground: (callback) => {
    if (typeof callback !== "function") return () => {};
    const handler = (_event, state) => callback(state);
    ipcRenderer.on("chat-background", handler);
    return () => ipcRenderer.removeListener("chat-background", handler);
  },
  setOpenAtLogin: (enabled) => ipcRenderer.invoke("set-open-at-login", Boolean(enabled)),
  onMaximizedChange: (callback) => {
    if (typeof callback !== "function") return () => {};
    const handler = (_event, maximized) => callback(Boolean(maximized));
    ipcRenderer.on("chat-maximized", handler);
    return () => ipcRenderer.removeListener("chat-maximized", handler);
  },
  dragBy: (dx, dy) => ipcRenderer.send("float-drag", { dx, dy }),
  notifyFloat: (kind) => ipcRenderer.send("float-notify", { kind }),
  clearFloatAlert: (kind) => ipcRenderer.send("float-alert-clear", { kind }),
  setForegroundNotice: (active) => ipcRenderer.send("foreground-control", Boolean(active)),
  onFloatAlert: (callback) => {
    if (typeof callback !== "function") return () => {};
    const handler = (_event, data) => callback(data);
    ipcRenderer.on("float-alert", handler);
    return () => ipcRenderer.removeListener("float-alert", handler);
  },
  openPage: (urlPath) => ipcRenderer.send("open-page", urlPath),
  openImage: (b64) => ipcRenderer.send("open-image", b64),
  revealPath: (filePath) => ipcRenderer.send("reveal-path", filePath),
  openLocalFile: (filePath) => ipcRenderer.invoke("open-local-file", filePath),
  // 取本机 UI 令牌:file:// 的悬浮球连 WebSocket 时要带上,证明自己是本机 Electron 而非恶意网页。
  getUiToken: () => ipcRenderer.invoke("get-ui-token"),
  onBackendReady: (callback) => {
    if (typeof callback !== "function") return () => {};
    const handler = () => callback();
    ipcRenderer.on("backend-ready", handler);
    return () => ipcRenderer.removeListener("backend-ready", handler);
  },
});
