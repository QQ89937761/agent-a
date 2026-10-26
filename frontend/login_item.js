"use strict";

const LOGIN_ITEM_NAME = "com.xiaoa.desktop";

function applyLoginItemSetting(electronApp, enabled, env = process.env, executable = process.execPath) {
  if (!electronApp.isPackaged) {
    return { available: false, enabled: false, reason: "development" };
  }
  const target = env.PORTABLE_EXECUTABLE_FILE || executable;
  electronApp.setLoginItemSettings({
    openAtLogin: Boolean(enabled),
    path: target,
    args: [],
    name: LOGIN_ITEM_NAME,
  });
  return { available: true, enabled: Boolean(enabled), path: target };
}

module.exports = { applyLoginItemSetting, LOGIN_ITEM_NAME };
