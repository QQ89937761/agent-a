// 背景分层：仅原生底层和页面背景透明，文字和控件不使用窗口 opacity。
const fs = require("node:fs");
const path = require("node:path");
const MODES = Object.freeze(["standard", "translucent", "transparent"]);
// 兼容旧设置；已移除的毛玻璃模式迁移为半透明。
const normalizeMode = mode => mode === "acrylic" ? "translucent" : MODES.includes(mode) ? mode : "standard";

function loadBackground(file) {
  try { return normalizeMode(JSON.parse(fs.readFileSync(file, "utf8")).mode); }
  catch (_) { return "standard"; }
}

function saveBackground(file, mode) {
  fs.mkdirSync(path.dirname(file), { recursive: true });
  fs.writeFileSync(file + ".tmp", JSON.stringify({ mode: normalizeMode(mode) }), "utf8");
  fs.renameSync(file + ".tmp", file);
}

function backgroundAvailability({ platform, hardwareAcceleration, nativeTheme }) {
  const blocked = nativeTheme.shouldUseHighContrastColors || nativeTheme.inForcedColorsMode || nativeTheme.prefersReducedTransparency;
  const translucentReason = blocked ? "系统已启用高对比度或减少透明效果"
    : !hardwareAcceleration ? "硬件加速未开启"
    : !["win32", "darwin"].includes(platform) ? "当前系统未验证透明兼容性" : "";
  return { translucentReason };
}

function applyBackground(win, requested, availability) {
  requested = normalizeMode(requested);
  let mode = requested;
  let reason = "";
  if (mode !== "standard" && availability.translucentReason) {
    mode = "standard";
    reason = availability.translucentReason;
  }
  let translucentReason = availability.translucentReason;
  try {
    // 标准模式由页面的实色背景填满，不使用原生白底以免破坏深色主题。
    win.setBackgroundColor("#00000000");
  } catch (error) {
    mode = "standard";
    reason = translucentReason = "系统窗口效果不可用，已恢复标准背景";
    console.error("聊天窗口背景切换失败:", error.message);
  }
  return { requested, mode, reason, translucentReason };
}

module.exports = { MODES, normalizeMode, loadBackground, saveBackground, backgroundAvailability, applyBackground };
