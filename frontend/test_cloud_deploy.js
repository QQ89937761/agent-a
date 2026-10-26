const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const html = fs.readFileSync(__dirname + '/src/settings.html', 'utf8');
const maintenance = html.match(/<section class="cloud-maintenance"[^>]*>([\s\S]*?)<\/section>/);
assert.ok(maintenance, 'cloud maintenance is a permanent section, not a collapsed details panel');
assert.doesNotMatch(maintenance[0], /\bhidden\b|<details\b|<summary\b/);
for (const id of ['cloud_check', 'cloud_repair', 'cloud_uninstall', 'cloud_purge']) {
  assert.ok(maintenance[1].includes(`id="${id}"`), `${id} stays in the visible maintenance section`);
}
const start = html.indexOf('    let cloudPollId = ');
const end = html.indexOf('    $("preview_voice").onclick', start);
const source = html.slice(start, end);
const elements = new Map();
const $ = id => {
  if (!elements.has(id)) elements.set(id, {textContent: '', className: '', value: '', hidden: false, disabled: false, dataset: {}, children: [{textContent: ''}, {textContent: ''}]});
  return elements.get(id);
};
let handler, timers = [], requests = [], prompts = [], confirms = [], loadCount = 0;
const context = vm.createContext({ $, API: 'http://127.0.0.1:1985', Date, Math,
  feishuInitialAppId: 'cli_existing',
  fetch: async (url, options) => { requests.push({url, options}); return handler(url, options); },
  setTimeout: callback => { timers.push(callback); return timers.length; },
  load: async () => { loadCount++; },
  window: {
    XiaoALanguage: {translateCloudLog: text => context.cloudLanguage === 'en' ? text.replace('云端连接成功，worker 已上线', 'Cloud connected successfully; worker is online') : text},
    addEventListener: (name, listener) => { if (name === 'xiaoa-ui-language-changed') context.languageChanged = listener; },
  },
  settingsConfirm: async () => confirms.shift() ?? true,
  settingsPrompt: async () => prompts.shift(),
});
vm.runInContext(source + '\nglobalThis.test = {renderCloudJob, pollCloudDeploy, restoreCloudDeploy, startCloudOperation};', context);
const releaseStart = html.indexOf('    function versionLabel(');
const releaseEnd = html.indexOf('    async function pollLocalUpdate(', releaseStart);
context.feishuAllowedIds = [];
context.cloudRuntimeAvailable = true;
context.runtimeIsHub = false;
context.desktopPackaged = true;
context.localAppVersion = "1.0.14";
context.latestRelease = null;
vm.runInContext(html.slice(releaseStart, releaseEnd) + '\nglobalThis.loadReleaseInfo = loadReleaseInfo;', context);
const api = context.test;
const titles = ['检查服务器', '上传安装文件', '安装运行环境', '更新配置与启动服务', '检查云端服务', '连接本机 worker'];
const job = (stage, overrides = {}) => ({job_id: 'job', host: '203.0.113.10', username: 'root', operation: 'deploy', version: '0.9.0', created_at: 1, updated_at: 2, stage, message: '真实进度',
  steps: titles.map((title, i) => ({title, state: stage === 'done' ? 'done' : i < 2 ? 'done' : i === 2 ? 'running' : 'pending'})), logs: [], ...overrides});
const reply = value => ({ok: true, status: 200, json: async () => value});
const flush = async () => { for (let i = 0; i < 20; i++) await Promise.resolve(); };

(async () => {
  let copiedAddress = "";
  context.window.isSecureContext = true;
  context.navigator = {clipboard: {writeText: async text => { copiedAddress = text; }}};
  const addressStart = html.indexOf('    function renderCloudWebAddress(');
  const addressEnd = html.indexOf('    async function load()', addressStart);
  vm.runInContext(html.slice(addressStart, addressEnd), context);
  for (const address of ['https://203.0.113.10:8457', 'https://198.51.100.20:9443', 'https://[2001:db8::1]:8448']) {
    context.renderCloudWebAddress(address);
    assert.equal($('cloud_web_url').textContent, address, 'address follows the saved user connection and chosen port');
    assert.equal($('copy_cloud_web_url').hidden, false);
    assert.equal($('cloud_web_empty').hidden, true);
    await $('copy_cloud_web_url').onclick();
    assert.equal(copiedAddress, address);
    assert.equal($('cloud_web_copy_status').textContent, '已复制');
  }
  context.navigator.clipboard.writeText = async () => { throw new Error('denied'); };
  let removed = 0, restored = 0, fallbackResult = true;
  context.document = {
    activeElement: {focus: () => { restored++; }},
    createElement: () => ({style: {}, focus() {}, select() {},
      setSelectionRange(start, end) { assert.equal(start, 0); assert.equal(end, this.value.length); },
      remove: () => { removed++; }}),
    body: {appendChild: input => { copiedAddress = input.value; }},
    execCommand: command => { assert.equal(command, 'copy'); return fallbackResult; },
  };
  await $('copy_cloud_web_url').onclick();
  assert.equal($('cloud_web_copy_status').textContent, '已复制', 'denied clipboard uses the browser copy fallback');
  context.navigator = {};
  context.window.isSecureContext = false;
  await $('copy_cloud_web_url').onclick();
  assert.equal($('cloud_web_copy_status').textContent, '已复制', 'older browsers without clipboard API still copy');
  assert.equal(removed, 2);
  assert.equal(restored, 2);
  fallbackResult = false;
  await $('copy_cloud_web_url').onclick();
  assert.match($('cloud_web_copy_status').textContent, /手动复制/);
  context.renderCloudWebAddress('');
  assert.equal($('cloud_web_url').textContent, '', 'unbinding clears the previous server address');
  assert.equal($('cloud_web_url').hidden, true);
  assert.equal($('copy_cloud_web_url').hidden, true);
  assert.equal($('cloud_web_empty').hidden, false);
  context.runtimeIsHub = true;
  handler = async () => { throw new Error('hub must not request desktop update'); };
  await context.loadReleaseInfo();
  context.runtimeIsHub = false;
  handler = async () => { throw new Error('website unavailable'); };
  await assert.rejects(context.loadReleaseInfo(), /website unavailable/);
  assert.doesNotMatch(html, /id="update_cloud"|\$\("update_cloud"\)/, 'cloud update has one entry in connections');
  assert.match(html, /class="msg err" id="cloud_new_version"[^>]*aria-live="polite"/, 'new version uses the existing red status style');
  api.renderCloudJob(job('done', {remote_status: {version: '1.0.12'}}));
  assert.match($('cloud_new_version').textContent, /可更新版本 v1\.0\.14/);
  assert.equal($('cloud_new_version').hidden, false, 'bundled cloud update is offered when website access fails');
  assert.equal($('connect_cloud').disabled, false);
  assert.equal($('update_local').hidden, true, 'local update still requires the website');
  handler = async () => reply({ok: true, current_version: '0.9.5', latest: {version: '0.9.5'}, update_available: false});
  await context.loadReleaseInfo();
  api.renderCloudJob(job('done', {remote_status: {version: '0.9.4'}}));
  assert.match($('cloud_new_version').textContent, /可更新版本 v0\.9\.5/);
  assert.doesNotMatch($('cloud_new_version').textContent, /请先更新本机/);
  for (let i = 0; i < 6; i++) assert(html.includes(`id="cloud_step_${i}"`));
  context.cloudLanguage = 'en';
  api.renderCloudJob(job('runtime', {total_bytes: 200, uploaded_bytes: 100, remote_status: {version: '0.9.6'}, logs: [
    {time: 1, message: '云端连接成功，worker 已上线'},
    {time: 1, message: '<img src=x onerror=bad>'},
  ]}));
  assert($('cloud_job_target').textContent.includes('云端当前版本 v0.9.6'));
  assert(!$('cloud_job_target').textContent.includes('本机版本'));
  assert.equal($('cloud_overall_progress').value, 2);
  assert.equal($('cloud_upload_progress').value, 50);
  assert.equal($('connect_cloud').disabled, true);
  assert.equal($('cloud_deploy_logs').textContent.includes('Cloud connected successfully; worker is online'), true, 'known cloud logs follow the English UI language');
  assert.equal($('cloud_deploy_logs').textContent.includes('<img src=x onerror=bad>'), true, 'unknown logs remain inert plain text');
  context.cloudLanguage = 'zh'; context.languageChanged();
  assert.equal($('cloud_deploy_logs').textContent.includes('云端连接成功，worker 已上线'), true, 'existing deployment logs refresh after switching to Chinese');
  api.renderCloudJob(job('partial', {cloud_ready: true}));
  assert($('cloud_job_target').textContent.includes('云端当前版本 未知'));
  assert.equal($('cloud_retry_worker').hidden, false);
  assert.equal($('connect_cloud').disabled, false);
  api.renderCloudJob(job('health', {remote_status: {}}));
  assert($('cloud_job_target').textContent.includes('云端当前版本 读取中…'));

  // Version comparisons use numbers and never mistake the deployment's target for remote state.
  context.localAppVersion = '1.0.14';
  const currentCloud = version => api.renderCloudJob(job('done', {version: '99.0.0', remote_status: {version}}));
  currentCloud('1.0.9');
  assert.equal($('cloud_new_version').hidden, false);
  assert.match($('cloud_new_version').textContent, /可更新版本 v1\.0\.14/);
  currentCloud('v1.0.14');
  assert.equal($('cloud_new_version').hidden, true, 'same version has no red notice');
  currentCloud('1.0.15');
  assert.equal($('cloud_new_version').hidden, true, 'newer remote must not offer a downgrade');
  for (const unknown of ['', undefined, 'unknown']) {
    currentCloud(unknown);
    assert.equal($('cloud_new_version').hidden, true, 'unknown remote version cannot establish an update');
  }
  currentCloud('1.0.12');
  handler = async () => reply({ok: true, current_version: '1.0.14', latest: {version: '1.0.15'}, update_available: true});
  await context.loadReleaseInfo();
  assert.match($('cloud_new_version').textContent, /可更新版本 v1\.0\.15.*请先更新本机/);
  currentCloud('1.0.15');
  assert.equal($('cloud_new_version').hidden, true, 'finishing the upgrade clears the notice');
  currentCloud('1.0.12');
  handler = async () => { throw new Error('offline'); };
  await assert.rejects(context.loadReleaseInfo(), /offline/);
  assert.match($('cloud_new_version').textContent, /可更新版本 v1\.0\.14/);
  assert.doesNotMatch($('cloud_new_version').textContent, /1\.0\.15/, 'failed check clears outdated website metadata');
  api.renderCloudJob(job('done', {operation: 'uninstall', remote_status: {version: '1.0.12'}}));
  assert.match($('cloud_job_target').textContent, /云端当前版本 未部署/);
  assert.equal($('cloud_new_version').hidden, true, 'uninstalled cloud is not offered an upgrade');

  let attempt = 0;
  handler = async () => { if (attempt++ === 0) throw new Error('temporary network loss'); return reply(job('partial', {cloud_ready: true})); };
  const polling = api.pollCloudDeploy('job');
  await flush();
  assert.equal($('connect_cloud').disabled, true, 'network loss must not enable duplicate deployments');
  assert.equal(timers.length, 1);
  timers.shift()();
  await polling;
  assert.equal($('connect_cloud').disabled, false);
  assert(loadCount > 0);

  handler = async () => reply({job: job('health', {username: 'xiaoa-admin'}), previous: job('done')});
  await api.restoreCloudDeploy();
  await flush();
  assert.equal($('cloud_host').value, '203.0.113.10');
  assert.equal($('cloud_username').value, 'xiaoa-admin', 'reopening preserves the enrolled account instead of silently switching to root');
  assert.equal($('cloud_step_4').children[1].textContent.startsWith('5. 检查云端服务'), true);
  assert.equal($('connect_cloud').disabled, true, 'reopening settings resumes the existing job');
  handler = async () => reply(job('done'));
  timers.shift()();
  await flush();
  assert.equal($('connect_cloud').disabled, false);
  timers = [];

  $('cloud_host').value = '203.0.113.10'; $('cloud_username').value = 'root';
  $('fs_app_id').value = 'cli_existing'; $('fs_app_secret').value = ''; $('cloud_password').value = '';
  handler = async url => url.endsWith('/latest') ? reply({job: null}) : reply({ok: false, error: 'validation'});
  requests = [];
  context.feishuAllowedIds = ['ou_existing', 'ou_second'];
  await $('connect_cloud').onclick();
  await flush();
  const body = JSON.parse(requests[0].options.body);
  assert.equal('use_release' in body, false, 'deployment always uses bundled files');
  assert.equal(body.password, '', 'subsequent key deployments do not require a password');
  assert.equal(body.feishu_app_secret, '', 'unchanged saved secret is not requested again');
  assert.equal(body.feishu_allowed_open_ids, 'ou_existing, ou_second', 'deployment preserves authorized users without a manual input');

  requests = []; prompts = [undefined]; $('cloud_password').value = 'page-value-must-not-be-used';
  await api.startCloudOperation('uninstall');
  assert.equal(requests.length, 0, 'uninstall needs the SSH password from the popup');
  $('cloud_purge').checked = true;
  prompts = ['remote-secret', 'wrong'];
  await api.startCloudOperation('uninstall');
  assert.equal(requests.length, 0, 'purging needs a separate exact confirmation');
  prompts = ['remote-secret', '删除数据 203.0.113.10'];
  await api.startCloudOperation('uninstall');
  const uninstallBody = JSON.parse(requests[0].options.body);
  assert.equal(uninstallBody.confirmation, 'remote_password');
  assert.equal(uninstallBody.password, 'remote-secret');
  assert.equal(uninstallBody.purge_data, true);

  api.renderCloudJob(job('partial', {cloud_ready: true}));
  requests = [];
  await api.startCloudOperation('retry');
  assert.equal(JSON.parse(requests[0].options.body).operation, 'retry', 'worker retry never requests a re-upload');
  console.log('cloud deployment: version notices, single update entry, permanent steps, byte progress, safe logs, restart recovery, network errors, key reuse and maintenance confirmations passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
