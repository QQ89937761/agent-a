// Test the actual image-preview IPC handler without starting Electron.
const assert = require("assert/strict");
const fs = require("fs");
const path = require("path");
const vm = require("vm");
const source = fs.readFileSync(path.join(__dirname, "main.js"), "utf8");
const begin = source.indexOf('ipcMain.on("open-image",');
const end = source.indexOf('\n});', begin) + 4;
assert(begin >= 0 && end > begin);
let handler;
const writes = [];
const opened = [];
const created = [];
const home = path.resolve(__dirname, "..", "data", "isolation", "tmp", "preview-test-home");
vm.runInNewContext(source.slice(begin, end), {
  ipcMain: { on(name, callback) { assert.equal(name, "open-image"); handler = callback; } },
  Buffer, path, require,
  fs: { mkdirSync(directory) { created.push(directory); }, writeFileSync(file, bytes) { writes.push({ file, bytes }); } },
  shell: { openPath(file) { opened.push(file); } },
  resolveAppHome() { return home; },
});
const png = Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]);
handler(null, png.toString("base64"));
handler(null, Buffer.from([0xff, 0xd8, 0xff]).toString("base64"));
handler(null, "not an image");
handler(null, "x".repeat(32 * 1024 * 1024 + 1));
handler(null, null);
assert.equal(writes.length, 2);
assert.equal(opened.length, 2);
const directory = path.join(home, "data", "isolation", "workspace", "screenshots");
assert(created.every(item => item === directory));
assert(writes.every(item => path.dirname(item.file) === directory));
assert(writes[0].bytes.equals(png));
assert(writes[0].file.endsWith(".png"));
assert(writes[1].file.endsWith(".jpg"));
assert.notEqual(writes[0].file, writes[1].file);
console.log("image preview workspace and validation passed");
