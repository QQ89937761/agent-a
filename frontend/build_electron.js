"use strict";
const fs = require("node:fs");
const path = require("node:path");
const os = require("node:os");
const crypto = require("node:crypto");
const {spawnSync} = require("node:child_process");

// electron-builder uses a different cache root from Electron's npm installer.
// Reuse only the exact pinned archive after validating Electron's own checksum.
function findElectronArchive(cacheRoot, filename, expectedHash) {
  if (!cacheRoot || !/^[a-f0-9]{64}$/i.test(expectedHash || "")) return null;
  let entries;
  try { entries = fs.readdirSync(cacheRoot, {withFileTypes:true}); }
  catch (error) { if (error.code === "ENOENT") return null; throw error; }
  const candidates = [path.join(cacheRoot, filename), ...entries.filter(entry => entry.isDirectory())
    .map(entry => path.join(cacheRoot, entry.name, filename))];
  for (const candidate of candidates) {
    let data;
    try { data = fs.readFileSync(candidate); }
    catch (error) { if (error.code === "ENOENT") continue; throw error; }
    if (crypto.createHash("sha256").update(data).digest("hex") === expectedHash.toLowerCase()) return candidate;
    console.warn(`Electron cache checksum mismatch, ignoring: ${candidate}`);
  }
  return null;
}

// Override only this builder process. Never patch the installed node_modules template.
function persistentPortableScript(script) {
  const start = script.indexOf('  StrCpy $INSTDIR "$PLUGINSDIR\\app"');
  const extract = script.indexOf('  !ifdef APP_DIR_64', start);
  const environment = script.indexOf("  System::Call 'Kernel32::SetEnvironmentVariable", extract);
  const cleanup = /[ \t]*RMDir \/r \$INSTDIR\s*SectionEnd/;
  if (start < 0 || extract < start || environment < extract || !cleanup.test(script)) {
    throw new Error("Unsupported electron-builder portable template; review cache integration before packaging.");
  }
  const include = path.join(__dirname, "portable-cache.nsh");
  return (`!include "${include}"\n` + script.slice(0, start) +
    "  !insertmacro xiaoaCacheStart\n" + script.slice(extract, environment) +
    "  !insertmacro xiaoaCacheReady\n" + script.slice(environment))
    .replace(cleanup, "  System::Call 'kernel32::CloseHandle(p r8)'\nSectionEnd")
    .replace('ExecWait "$INSTDIR\\${APP_EXECUTABLE_FILENAME} $R0" $0',
      "ExecWait '\"$INSTDIR\\${APP_EXECUTABLE_FILENAME}\" $R0' $0");

}

function installPortableCacheHook() {
  // Initialize the public entry first: loading NsisTarget directly creates a
  // circular PlatformPackager import in a fresh builder process.
  require("electron-builder");
  const {NsisTarget} = require("app-builder-lib/out/targets/nsis/NsisTarget");
  const original = NsisTarget.prototype.computeFinalScript;
  NsisTarget.prototype.computeFinalScript = function(script, ...args) {
    return original.call(this, this.isPortable ? persistentPortableScript(script) : script, ...args);
  };
}

async function runBuilder() {
  installPortableCacheHook();
  const {build, Platform, Arch} = require("electron-builder");
  const electronDist = process.argv[3];
  await build({projectDir: __dirname, targets: Platform.WINDOWS.createTarget("portable", Arch.x64),
    publish: "never", config: electronDist ? {electronDist} : {}});
}

function main() {
  const electron = require("electron/package.json");
  const filename = `electron-v${electron.version}-win32-x64.zip`;
  const checksums = require("electron/checksums.json");
  const cacheRoot = process.env.electron_config_cache || (process.platform === "win32"
    ? path.join(process.env.LOCALAPPDATA || path.join(os.homedir(), "AppData", "Local"), "electron", "Cache")
    : path.join(process.env.XDG_CACHE_HOME || path.join(os.homedir(), ".cache"), "electron"));
  const archive = findElectronArchive(cacheRoot, filename, checksums[filename]);
  const args = [__filename, "--builder"];
  if (archive) {
    console.log(`Using SHA-256 verified Electron ${electron.version} cache: ${archive}`);
    args.push(archive);
  } else {
    console.log("No verified Electron cache; using electron-builder's normal verified download.");
  }
  const result = spawnSync(process.execPath, args, {cwd:__dirname, stdio:"inherit"});
  if (result.error) throw result.error;
  process.exitCode = result.status === null ? 1 : result.status;
}

module.exports = {findElectronArchive, persistentPortableScript, installPortableCacheHook};
if (require.main === module) {
  if (process.argv[2] === "--builder") runBuilder().catch(error => { console.error(error); process.exitCode = 1; });
  else main();
}
