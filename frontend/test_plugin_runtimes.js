const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const {preparePluginRuntimes} = require("./plugin_runtimes");

(async () => {
const root = fs.mkdtempSync(path.join(os.tmpdir(), "xiaoa-plugin-runtime-"));
try {
  const source = path.join(root, "resources", "plugin-runtime");
  const appHome = path.join(root, "XiaoA-data");
  for (const relative of ["python/python.exe", "python/python313.dll", "python/vcruntime140.dll", "python/python313.zip", "node/node.exe", "node/npm.cmd"]) {
    const file = path.join(source, relative);
    fs.mkdirSync(path.dirname(file), {recursive: true});
    fs.writeFileSync(file, relative);
  }
  fs.writeFileSync(path.join(source, "runtime.json"), JSON.stringify({python: "3.13.15", node: "24.21.0"}));

  let releaseCopy, notifyCopy, copies = 0, finished = false;
  const copying = new Promise(resolve => { notifyCopy = resolve; });
  const copyGate = new Promise(resolve => { releaseCopy = resolve; });
  const runtimeFs = {...fs, promises: {...fs.promises, cp: async (...args) => {
    copies++;
    notifyCopy();
    await copyGate;
    return fs.promises.cp(...args);
  }}};
  const preparing = preparePluginRuntimes(path.dirname(source), appHome, runtimeFs)
    .then(value => { finished = true; return value; });
  await copying;
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(finished, false, "the UI event loop must keep running while runtime copy is pending");
  releaseCopy();
  const runtime = await preparing;
  assert.equal(fs.readFileSync(runtime.XIAOA_PLUGIN_PYTHON, "utf8"), "python/python.exe");
  assert.equal(fs.readFileSync(runtime.XIAOA_PLUGIN_NODE, "utf8"), "node/node.exe");
  assert.equal(fs.readFileSync(runtime.XIAOA_PLUGIN_NPM, "utf8"), "node/npm.cmd");
  assert.deepEqual(await preparePluginRuntimes(path.dirname(source), appHome, runtimeFs), runtime);
  assert.equal(copies, 1, "later launches must reuse the installed runtime");
  const destination = path.dirname(path.dirname(runtime.XIAOA_PLUGIN_PYTHON));
  const marker = path.join(destination, ".ready.json");
  const ready = fs.readFileSync(marker, "utf8");
  const cacheFs = {...fs, readdirSync() { throw new Error("cache hit must not scan source Python"); },
    statSync(file) {
      assert(!file.startsWith(source), "cache hit must not stat the source tree");
      return fs.statSync(file);
    }};
  assert.deepEqual(await preparePluginRuntimes(path.dirname(source), appHome, cacheFs), runtime);
  for (const invalid of ["{broken", "null", "{}", JSON.stringify({...JSON.parse(ready), files: {"../escape": 1}})]) {
    fs.writeFileSync(marker, invalid);
    assert.deepEqual(await preparePluginRuntimes(path.dirname(source), appHome), runtime);
    assert.equal(JSON.parse(fs.readFileSync(marker)).version, 1, "invalid marker must be rebuilt after full validation");
  }
  fs.unlinkSync(marker);
  assert.deepEqual(await preparePluginRuntimes(path.dirname(source), appHome), runtime);
  const otherResources = path.join(root, "other-resources");
  fs.cpSync(path.dirname(source), otherResources, {recursive: true});
  fs.appendFileSync(path.join(otherResources, "plugin-runtime", "python", "vcruntime140.dll"), "changed build");
  await assert.rejects(preparePluginRuntimes(otherResources, appHome), /目录不完整/,
    "another build with the same runtime versions must undergo full validation");
  const dll = path.join(path.dirname(runtime.XIAOA_PLUGIN_PYTHON), "vcruntime140.dll");
  const dllContents = fs.readFileSync(dll);
  fs.rmSync(dll);
  await assert.rejects(preparePluginRuntimes(path.dirname(source), appHome), /目录不完整/);
  fs.writeFileSync(dll, "truncated");
  await assert.rejects(preparePluginRuntimes(path.dirname(source), appHome), /目录不完整/);
  fs.writeFileSync(dll, dllContents);
  assert.deepEqual(await preparePluginRuntimes(path.dirname(source), appHome), runtime);
  assert.equal(copies, 1, "incomplete cached environments must not be overwritten while plugins may use them");
  const failedHome = path.join(root, "failed-home");
  let failedAttempts = 0;
  const brokenFs = {...fs, promises: {...fs.promises, cp: async (_source, staging) => {
    failedAttempts++;
    await fs.promises.mkdir(staging, {recursive: true});
    await fs.promises.writeFile(path.join(staging, "partial"), "incomplete");
    throw new Error("copy failed");
  }}};
  await assert.rejects(preparePluginRuntimes(path.dirname(source), failedHome, brokenFs), /copy failed/);
  assert.equal(failedAttempts, 1, "non-access errors must not be retried");
  assert.deepEqual(fs.readdirSync(path.join(failedHome, "runtimes", "plugin-runtime")), [], "failed copies must leave no installed or staging directory");
  let attempts = 0;
  const retryHome = path.join(root, "retry-home");
  const lockedFs = {...fs, promises: {...fs.promises, cp: async (src, staging, options) => {
    attempts++;
    if (attempts === 1) {
      await fs.promises.mkdir(path.join(staging, "python"), {recursive: true});
      await fs.promises.writeFile(path.join(staging, "python", "partial"), "incomplete");
      throw Object.assign(new Error("DLL temporarily locked"), {code: "EPERM"});
    }
    assert(!fs.existsSync(staging), "retry must discard the incomplete staging tree");
    return fs.promises.cp(src, staging, options);
  }}};
  const recovered = await preparePluginRuntimes(path.dirname(source), retryHome, lockedFs);
  assert.equal(attempts, 2);
  assert(fs.existsSync(recovered.XIAOA_PLUGIN_PYTHON));
  assert(!fs.existsSync(path.join(path.dirname(recovered.XIAOA_PLUGIN_PYTHON), "partial")));

  let blockedAttempts = 0;
  const blockedHome = path.join(root, "blocked-home");
  const blockedFs = {...fs, promises: {...fs.promises, cp: async () => {
    blockedAttempts++;
    throw Object.assign(new Error("persistent DLL access denial"), {code: "EACCES"});
  }}};
  await assert.rejects(preparePluginRuntimes(path.dirname(source), blockedHome, blockedFs), /persistent DLL access denial/);
  assert.equal(blockedAttempts, 4, "persistent access errors must stop after bounded retries");
  assert.deepEqual(fs.readdirSync(path.join(blockedHome, "runtimes", "plugin-runtime")), []);
  console.log("plugin runtimes: async copy, DLL completeness, bounded lock retries, cache reuse and failure cleanup passed");
} finally {
  fs.rmSync(root, {recursive: true, force: true});
}

})().catch(error => { console.error(error); process.exitCode = 1; });
