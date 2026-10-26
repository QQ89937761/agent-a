const fs = require("node:fs");
const path = require("node:path");
const {createHash} = require("node:crypto");
const {setTimeout: delay} = require("node:timers/promises");

async function preparePluginRuntimes(resourcesPath, appHome, fileSystem = fs) {
  const source = path.join(resourcesPath, "plugin-runtime");
  const metadataText = fileSystem.readFileSync(path.join(source, "runtime.json"), "utf8");
  const metadata = JSON.parse(metadataText);
  const version = `${metadata.python}-${metadata.node}`;
  const destination = path.join(appHome, "runtimes", "plugin-runtime", version);
  const executables = {
    XIAOA_PLUGIN_PYTHON: path.join(destination, "python", "python.exe"),
    XIAOA_PLUGIN_NODE: path.join(destination, "node", "node.exe"),
    XIAOA_PLUGIN_NPM: path.join(destination, "node", "npm.cmd"),
  };

  const marker = path.join(destination, ".ready.json");
  const metadataHash = createHash("sha256").update(metadataText).digest("hex");
  try {
    const ready = JSON.parse(fileSystem.readFileSync(marker, "utf8"));
    const entries = Object.entries(ready?.files || {});
    const baseline = ["runtime.json", "python/python.exe", "node/node.exe", "node/npm.cmd"];
    if (ready?.version === 1 && ready.metadataHash === metadataHash &&
        ready.source === path.resolve(source) && ready.files &&
        typeof ready.files === "object" && !Array.isArray(ready.files) &&
        baseline.every(name => Object.hasOwn(ready.files, name)) &&
        entries.some(([name]) => /^python\/python\d+\.dll$/i.test(name)) &&
        entries.every(([name, size]) => {
          if (!/^(runtime\.json|(?:python|node)\/[\w.-]+)$/.test(name) ||
              !Number.isSafeInteger(size) || size < 0) return false;
          const actual = fileSystem.statSync(path.join(destination, name));
          return actual.isFile() && actual.size === size;
        })) return executables;
  } catch (error) {
    if (!(error instanceof SyntaxError) && error.code !== "ENOENT") throw error;
  }

  // Check the DLLs and standard library as well as launchers: three EXEs alone
  // do not prove that Python can start after an interrupted/blocked copy.
  const required = ["runtime.json", "python/python.exe", "node/node.exe", "node/npm.cmd",
    ...fileSystem.readdirSync(path.join(source, "python"))
      .filter(name => /\.(dll|pyd|zip|_pth)$/i.test(name)).map(name => path.join("python", name))];
  const complete = directory => required.every(relative => {
    try {
      const actual = fileSystem.statSync(path.join(directory, relative));
      return actual.isFile() && actual.size === fileSystem.statSync(path.join(source, relative)).size;
    } catch (error) {
      if (error.code === "ENOENT") return false;
      throw error;
    }
  });

  if (!complete(destination)) {
    if (fileSystem.existsSync(destination)) {
      throw new Error(`插件运行时目录不完整，无法安全覆盖: ${destination}`);
    }
    const parent = path.dirname(destination);
    const staging = `${destination}.tmp-${process.pid}`;
    await fileSystem.promises.mkdir(parent, {recursive: true});
    try {
      // Windows can temporarily lock DLLs while inspecting newly extracted files.
      // Retry only access/busy errors, with a clean staging tree on each attempt.
      for (let attempt = 0; ; attempt++) {
        try {
          await fileSystem.promises.cp(source, staging, {recursive: true, errorOnExist: true});
          if (!complete(staging)) throw new Error("插件运行时复制不完整，未启用该环境。");
          await fileSystem.promises.rename(staging, destination);
          break;
        } catch (error) {
          if (!["EPERM", "EACCES", "EBUSY"].includes(error.code) || attempt >= 3) throw error;
          await fileSystem.promises.rm(staging, {recursive: true, force: true, maxRetries: 3, retryDelay: 250});
          await delay([250, 1000, 3000][attempt]);
        }
      }
    } catch (error) {
      await fileSystem.promises.rm(staging, {recursive: true, force: true, maxRetries: 3, retryDelay: 250});
      throw error;
    }
  }

  // Store every required file, not only launchers. Cache hits still catch a
  // missing/truncated DLL without repeatedly stat-ing the packaged source tree.
  const files = Object.fromEntries(required.map(relative => [relative.replace(/\\/g, "/"),
    fileSystem.statSync(path.join(destination, relative)).size]));
  await fileSystem.promises.writeFile(marker, JSON.stringify({version: 1, metadataHash, source: path.resolve(source), files}));
  return executables;
}

module.exports = {preparePluginRuntimes};
