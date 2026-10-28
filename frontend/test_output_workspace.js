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


// Exercise the real file-opening IPC and renderer, without opening user files.
async function testLocalFileLinks() {
  const handlers = new Map();
  const sender = {};
  const chatWin = { webContents: sender, isDestroyed: () => false };
  const file = "D:\\XiaoA\\成果文件 (final).docx";
  const calls = [];
  let statError = null;
  let fileExists = true;
  let openError = "";
  const start = source.indexOf('ipcMain.handle("open-local-file",');
  const stop = source.indexOf('\n});', start) + 4;
  vm.runInNewContext(source.slice(start, stop), {
    ipcMain: {handle(name, fn) {handlers.set(name, fn);}}, chatWin, path, Set,
    fs: {promises: {async stat(p) {
      calls.push(["stat", p]);
      if (statError) throw statError;
      return {isFile: () => fileExists};
    }}},
    shell: {async openPath(p) {calls.push(["open", p]); return openError;}},
  });
  const open = handlers.get("open-local-file");
  assert(open, "file-opening handler is installed");
  assert.equal((await open({sender}, file)).ok, true);
  assert.equal(calls.at(-1)[0], "open");
  assert.equal(calls.at(-1)[1], path.win32.normalize(file));
  const count = calls.length;
  for (const candidate of ["file:///D:/a.docx", "https://example.test/a.docx", "relative.docx", "\\\\server\\share\\a.docx", "D:\\a.exe", "D:\\a.cmd", "D:\\a.ps1", "D:\\a.lnk", "D:\\a.docx:payload", "D:\\a.docx\u0000", null]) {
    assert.equal((await open({sender}, candidate)).ok, false, String(candidate));
  }
  assert.equal((await open({sender: {}}, file)).ok, false);
  assert.equal(calls.length, count, "invalid paths and untrusted senders never reach disk or shell");
  fileExists = false;
  assert.equal((await open({sender}, file)).error, "此路径不是文件。");
  fileExists = true;
  statError = {code: "ENOENT"};
  assert.equal((await open({sender}, file)).error, "文件不存在，可能已移动或删除。");
  statError = null;
  openError = "No association";
  assert.match((await open({sender}, file)).error, /已安装/);

  const html = fs.readFileSync(path.join(__dirname, "src/index.html"), "utf8");
  const clicks = new Map();
  const context = vm.createContext({
    window: {bridge: {openLocalFile: async () => ({ok: true})}, XiaoALanguage: {translate: text => text}},
    log: {addEventListener(name, fn) {clicks.set(name, fn);}},
    document: {createElement() {return {setAttribute() {}};}},
  });
  const a = html.indexOf('    function _escapeHtmlText(');
  const b = html.indexOf('    async function copyText(', a);
  vm.runInContext(html.slice(a, b) + '\nglobalThis.test = {renderMarkdown};', context);
  const render = context.test.renderMarkdown;
  assert.match(render('`' + file + '`'), /class="local-file-link"/);
  assert.match(render(file), /class="local-file-link"/);
  assert.match(render("`D:\\客户 & O'Brien\\交付.docx`"), /&amp; O&#39;Brien/);
  assert.doesNotMatch(render('```\n' + file + '\n```'), /local-file-link/);
  for (const text of ['`D:\\run.exe`', '`D:\\run.docx:payload`', '`https://example.test/a.docx`', '`D:\\<img src=x onerror=alert(1)>.docx`']) {
    assert.doesNotMatch(render(text), /class="local-file-link"/);
    assert.doesNotMatch(render(text), /<img/);
  }
  context.window.bridge = null;
  assert.doesNotMatch(render('`' + file + '`'), /local-file-link/, "browser-only clients cannot open desktop paths");
  context.window.bridge = {openLocalFile: async () => ({ok: false, error: "文件不存在，可能已移动或删除。"})};
  const attrs = new Map();
  let failure;
  const link = {dataset: {localPath: file}, getAttribute: name => attrs.get(name), setAttribute: (name, val) => attrs.set(name, val), removeAttribute: name => attrs.delete(name), after: value => {failure = value;}};
  let prevented = false;
  await clicks.get("click")({target: {closest: () => link}, preventDefault() {prevented = true;}});
  assert.equal(prevented, true);
  assert.equal(failure.textContent, "文件不存在，可能已移动或删除。");
  assert.equal(attrs.has("aria-busy"), false);
  console.log("local file links, safe document IPC, and failure feedback passed");
}
testLocalFileLinks().catch(error => { console.error(error); process.exitCode = 1; });
