"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const crypto = require("node:crypto");
const {findElectronArchive} = require("./build_electron");
const packageConfig = require("./package.json");
assert(packageConfig.scripts.dist.includes("node build_electron.js"));
assert(!packageConfig.build.files.includes("build_electron.js"), "build helper must not ship in the app");
const {persistentPortableScript} = require("./build_electron");
// GitHub's frontend checks intentionally run without Electron/build dependencies.
let templatePath;
try { templatePath = require.resolve("app-builder-lib/templates/nsis/portable.nsi"); }
catch (error) { if (error.code !== "MODULE_NOT_FOUND") throw error; }
const originalTemplate = templatePath ? fs.readFileSync(templatePath, "utf8") : String.raw`
Section
  StrCpy $INSTDIR "$PLUGINSDIR\app"
  RMDir /r $INSTDIR
  SetOutPath $INSTDIR
  !ifdef APP_DIR_64
    File /r "payload\*.*"
  !endif
  System::Call 'Kernel32::SetEnvironmentVariable(t, t)i ("PORTABLE_EXECUTABLE_DIR", "$EXEDIR").r0'
  ExecWait "$INSTDIR\CACHE_APP_EXECUTABLE $R0" $0
  RMDir /r $INSTDIR
SectionEnd
`.replace("CACHE_APP_EXECUTABLE", "${APP_EXECUTABLE_FILENAME}");
const portable = persistentPortableScript(originalTemplate);
assert(portable.includes("!insertmacro xiaoaCacheStart"));
assert(portable.includes("!insertmacro xiaoaCacheReady"));
assert(!portable.includes("RMDir /r $INSTDIR"), "the generated shell must not delete cache on exit");
assert(portable.includes("ExecWait '\"$INSTDIR\\${APP_EXECUTABLE_FILENAME}\" $R0' $0"));
if (templatePath) assert.equal(fs.readFileSync(templatePath, "utf8"), originalTemplate, "installed builder template must stay untouched");
assert.throws(() => persistentPortableScript("changed upstream template"), /Unsupported/);
assert.deepEqual(packageConfig.build.electronLanguages, ["zh-CN", "en-US"]);
const root = fs.mkdtempSync(path.join(os.tmpdir(), "xiaoa-electron-cache-"));
try {
  const filename = "electron-v43.7.7-win32-x64.zip";
  const data = Buffer.from("verified archive fixture");
  const hash = crypto.createHash("sha256").update(data).digest("hex");
  assert.equal(findElectronArchive(path.join(root,"missing"),filename,hash), null);
  assert.equal(findElectronArchive(root,filename,""), null);
  fs.mkdirSync(path.join(root,"old"));
  fs.writeFileSync(path.join(root,"old",filename.replace("43.7.7","43.4.0")),data);
  fs.writeFileSync(path.join(root,filename),"incomplete download");
  assert.equal(findElectronArchive(root,filename,hash), null);
  fs.mkdirSync(path.join(root,"valid"));
  const archive = path.join(root,"valid",filename);
  fs.writeFileSync(archive,data);
  assert.equal(findElectronArchive(root,filename,hash), archive);
  fs.writeFileSync(archive,"corrupted");
  assert.equal(findElectronArchive(root,filename,hash), null);
  fs.writeFileSync(path.join(root,filename),data);
  assert.equal(findElectronArchive(root,filename,hash),path.join(root,filename));
  console.log("Electron cache: pinned archive, SHA-256 validation, missing and corrupt cache checks passed");
} finally {
  const relative = path.relative(fs.realpathSync(os.tmpdir()),fs.realpathSync(root));
  assert(relative && !relative.startsWith("..") && !path.isAbsolute(relative), "cleanup must stay in temp directory");
  fs.rmSync(root,{recursive:true,force:true});
}
