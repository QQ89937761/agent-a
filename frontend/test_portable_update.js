const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const html = fs.readFileSync(__dirname + '/src/settings.html', 'utf8');
const start = html.indexOf('    function versionLabel(');
const end = html.indexOf('    $("retry_load").onclick', start);
const elements = new Map();
const $ = id => {
  if (!elements.has(id)) elements.set(id, {hidden: false, disabled: false, textContent: '', className: '', value: 0});
  return elements.get(id);
};
let state = {stage: 'staged', percent: 100, prepared_config: 'prepared.json', message: '已校验'};
let failure = false;
let releaseResponse = {ok: true, current_version: '1.0.52', latest: {version: '1.0.53', notes: '旧更新说明'}, update_available: true};
const requests = [];
const timers = new Set();
const context = vm.createContext({$, API: 'http://localhost', Date, desktopPackaged: true, cloudRuntimeAvailable: false,
  UPDATE_POLL_TIMEOUT_MS: 1000, updatePollTimer: null, updatePollStartedAt: 0,
  setInterval: cb => { timers.add(cb); return cb; }, clearInterval: cb => timers.delete(cb),
  settingsConfirm: async () => true,
  runtimeIsHub: false, localAppVersion: '', latestRelease: null, renderCloudVersion() {},
  fetch: async (url, options) => {
    requests.push({url, options});
    if (url.endsWith('/release')) return {ok: true, json: async () => releaseResponse};
    if (url.endsWith('/job')) return {ok: true, json: async () => state};
    if (url.endsWith('/cancel')) { state = {stage: 'cancelled', message: '已取消更新'}; return {ok: true, json: async () => ({ok: true})}; }
    return {ok: true, json: async () => failure ? {ok: false, error: '更新器未能启动'} : {ok: true}};
  },
});
vm.runInContext(html.slice(start, end) + '\nglobalThis.test = {loadReleaseInfo, pollLocalUpdate, startLocalUpdatePolling};', context);
(async () => {
  const repository = 'https://github.com/QQ89937761/agent-a';
  const link = html.match(/<a id="update_repository_link" href="([^"]+)" target="_blank" rel="noopener noreferrer">([^<]+)<\/a>/);
  assert(link, 'repository uses a native link with safe new-tab attributes');
  assert.equal(link[1], repository);
  assert.equal(link[2], repository, 'show the complete repository address');
  for (const latest of [releaseResponse.latest, null]) {
    releaseResponse = {...releaseResponse, latest};
    await context.test.loadReleaseInfo();
    assert.equal($('update_current_version').textContent, 'v1.0.52');
    assert.equal($('update_latest_version').textContent, latest ? 'v1.0.53' : '未知');
    assert(!elements.has('update_release_notes'), 'update checks no longer render release notes');
  }
  const main = fs.readFileSync(__dirname + '/main.js', 'utf8');
  const handlers = new Map(), opened = [];
  let openWindow;
  const path = require('node:path');
  vm.runInNewContext(main.slice(main.indexOf('function lockNavigation('), main.indexOf('function clampFloatToDisplay(')) +
    '\nlockNavigation(win, ["chat.html"]);', {
    path, fileURLToPath: require('node:url').fileURLToPath, console,
    shell: {openExternal: async url => {opened.push(url);}},
    win: {webContents: {setWindowOpenHandler(fn) {openWindow = fn;}, on(name, fn) {handlers.set(name, fn);}}},
  });
  for (const url of [link[1], repository + '/evil', repository + '?redirect=bad', 'https://example.com', 'javascript:alert(1)', 'file:///C:/private.txt']) {
    assert.equal(openWindow({url}).action, 'deny', 'never open an Electron popup');
  }
  assert.deepEqual(opened, [repository], 'only the exact repository opens in the system browser');
  let prevented = false;
  handlers.get('will-navigate')({preventDefault() {prevented = true;}}, repository);
  assert(prevented, 'external links cannot replace the local application');
  console.log('version card: repository link, update refresh and exact external navigation passed');
  context.test.startLocalUpdatePolling();
  await context.test.pollLocalUpdate();
  assert.equal($('restart_update').hidden, false);
  assert.equal($('cancel_update').hidden, false);
  assert.equal($('update_progress').hidden, true);
  assert.equal($('update_local').disabled, true);
  assert.equal(timers.size, 0, 'staged update must not keep polling until timeout');
  assert.equal(requests.some(r => r.url.endsWith('/restart')), false, 'download completion must not restart automatically');
  failure = true;
  await $('restart_update').onclick();
  assert.equal($('restart_update').disabled, false);
  assert.equal($('cancel_update').hidden, false);
  assert.match($('update_status').textContent, /未能启动/);
  await $('cancel_update').onclick();
  await context.test.pollLocalUpdate();
  assert.equal($('restart_update').hidden, true);
  assert.equal($('cancel_update').hidden, true);
  assert.equal($('update_local').disabled, false);
  state = {stage: 'staged', percent: 100, message: '等待替换'};
  await context.test.pollLocalUpdate();
  assert.equal($('restart_update').hidden, true);
  assert.equal($('cancel_update').hidden, false);
  assert.match($('update_status').textContent, /旧版更新/);
  state = {stage: 'downloading', percent: 60};
  await context.test.pollLocalUpdate();
  assert.equal($('cancel_update').hidden, false);
  assert.equal($('update_progress').hidden, false);
  assert.equal($('update_progress').value, 60);
  assert(fs.readFileSync(__dirname + '/main.js', 'utf8').includes('XIAOA_EXECUTABLE_PATH: process.env.PORTABLE_EXECUTABLE_FILE || process.execPath'), 'portable updates replace the outer EXE, not the temporary extracted Electron EXE');
  console.log('portable update: explicit restart, cancel, legacy state recovery, startup failure and outer EXE passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
