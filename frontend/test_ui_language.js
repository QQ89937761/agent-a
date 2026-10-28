const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const {translate, translateCloudLog, normalize, EN, toolDescription} = require("./src/ui-language");
assert.equal(normalize("en"), "en");
for (const value of [null, undefined, "", "bad", "zh", "<img>"]) assert.equal(normalize(value), "zh");
assert.equal(translate("设置", "en"), "Settings");
assert.equal(translate("设置", "zh"), "设置");
assert.equal(translate(" \n给小A发送消息  ", "en"), " \nMessage XiaoA  ");
assert.equal(translate("模型可用 · 32ms", "en"), "Model available · 32ms");
assert.equal(translate("上游返回 12 个模型，点击下拉框即可填入", "en"), "12 models found; choose one from the list");
for (const value of ["自定义厂商", "gpt-4o", "D:\\项目\\报告.docx", "https://host/api", "__proto__", "constructor"]) assert.equal(translate(value, "en"), value);
assert(Object.keys(EN).length > 300);
const settingsHtml = fs.readFileSync("src/settings.html", "utf8");
for (const selectId of ["tts_voice", "baidu_tts_voice"]) {
  const start = settingsHtml.indexOf(`<select id="${selectId}">`);
  const end = settingsHtml.indexOf("</select>", start);
  assert(start >= 0 && end > start, `Missing voice select ${selectId}`);
  const html = settingsHtml.slice(start, end);
  for (const [, label] of html.matchAll(/<option\b[^>]*>([^<]+)<\/option>/g)) {
    assert(EN[label], `Missing English voice label: ${label}`);
  }
  for (const [, label] of html.matchAll(/<optgroup\b[^>]*label="([^"]+)"/g)) {
    assert(EN[label], `Missing English voice group: ${label}`);
  }
}
for (const [source, expected] of [
  ["推荐 · 大模型 · 中英混读（需相应权限）", "Recommended · Large-model · Chinese/English (permission required)"],
  ["度涵竹 · 开朗女声 · 4189", "Du Hanzhu · Cheerful female · 4189"],
  ["度沁遥 · 知性女声 · 4197", "Du Qinyao · Refined female · 4197"],
  ["度小美 · 标准女声 · 0", "Du Xiaomei · Standard female · 0"],
  ["智瑜 · 女声甜美", "Zhiyu · Sweet female"],
  ["删除已保存的腾讯云凭据", "Delete saved Tencent Cloud credentials"],
]) assert.equal(translate(source, "en"), expected);

// Exercise real persistence and toggle code without adding a DOM dependency.
function runtime(saved, blocked = false, content = null) {
  const nativeLanguages = [];
  const events = {}, buttonEvents = {}, storage = new Map([["xiaoa.uiLanguage", saved]]), frameMessages = [], languageEvents = [];
  const button = {textContent: "中", setAttribute(name, value) { this[name] = value; }, addEventListener(name, handler) { buttonEvents[name] = handler; }};
  const root = {nodeType: 1, childNodes: [], dataset: {}, matches: () => false, closest: () => null, hasAttribute: () => false};
  const document = {documentElement: root, head: root, body: content || root, getElementById: id => id === "languageToggle" ? button : null,
    querySelectorAll: () => [{contentWindow: {postMessage: data => frameMessages.push(data)}, addEventListener() {}}]};
  const window = {bridge:{setUiLanguage: value => nativeLanguages.push(value)},
    addEventListener: (name, fn) => { events[name] = fn; }, dispatchEvent: event => languageEvents.push(event)}; window.parent = window;
  let onMutation;
  const context = vm.createContext({window, document, CustomEvent: function (type, init) { this.type = type; this.detail = init.detail; }, MutationObserver: function (callback) { onMutation = callback; return {observe() {}, disconnect() {}}; },
    location: {origin: "https://xiaoa.test"}, localStorage: {
      getItem: key => { if (blocked) throw Error("blocked"); return storage.get(key); },
      setItem: (key, value) => { if (blocked) throw Error("blocked"); storage.set(key, value); },
    }});
  vm.runInContext(fs.readFileSync("src/ui-language.js", "utf8"), context);
  return {nativeLanguages, mutate: records => onMutation(records), window, document, events, button, click: buttonEvents.click, storage, frameMessages, languageEvents};
}
{
  const app = runtime(null);
  assert.equal(app.button.textContent, "中");
  assert.equal(app.nativeLanguages.at(-1), "zh");
  assert.equal(app.document.documentElement.lang, "zh-CN");
  app.click();
  assert.equal(app.button.textContent, "EN");
  assert.equal(app.button["aria-pressed"], "true");
  assert.equal(app.nativeLanguages.at(-1), "en");
  assert.equal(app.document.documentElement.lang, "en");
  assert.equal(app.storage.get("xiaoa.uiLanguage"), "en");
  assert.equal(app.frameMessages.at(-1).language, "en");
  assert.equal(app.languageEvents.at(-1).type, "xiaoa-ui-language-changed");
  assert.equal(app.languageEvents.at(-1).detail.language, "en");
  assert.equal(runtime(app.storage.get("xiaoa.uiLanguage")).button.textContent, "EN");
  app.click(); assert.equal(app.button.textContent, "中");
  app.storage.set("xiaoa.uiLanguage", "en"); app.events.storage({key: "xiaoa.uiLanguage"});
  assert.equal(app.button.textContent, "EN");
  app.events.message({source: {}, origin: "https://evil.test", data: {type: "xiaoa-ui-language", language: "zh"}});
  assert.equal(runtime("en").nativeLanguages.at(-1), "en", "startup restores the native menu language");
  assert.equal(app.button.textContent, "EN", "foreign messages must not change language");
}
{
  const app = runtime("en", true); app.click();
  assert.equal(app.button.textContent, "EN", "blocked storage still permits session-only switching");
}
const html = fs.readFileSync("src/index.html", "utf8");
assert(html.indexOf('id="languageToggle"') < html.indexOf('id="themeToggle"'));
assert(/:is\(#themeToggle, #languageToggle\)\s*\{[^}]*border-radius:\s*50%/.test(html));
assert(/:is\(#themeToggle, #languageToggle\)\s*\{[^}]*width:\s*32px; height:\s*32px/.test(html));
for (const page of ["index", "settings", "login", "voices", "float", "foreground_notice"]) assert(fs.readFileSync(`src/${page}.html`, "utf8").includes('<script src="./ui-language.js"></script>'));
assert(JSON.parse(fs.readFileSync("package.json", "utf8")).build.files.includes("src/ui-language.js"));
console.log("UI language: real toggle, persistence, blocked storage, frame synchronization, safe text and shared geometry passed");

for (const [source, expected] of [
  ["已启用 147 个内置工具，按工作类型分类显示。", "147 built-in tools enabled, grouped by work type."],
  ["电脑与窗口 · 18", "Computer & Windows · 18"],
  ["共 12 条，当前显示最近 5 条。", "12 records in total; showing the latest 5."],
  ["本地电脑（未配置云端连接）", "Local computer (no cloud connection configured)"],
  ["远程操控：关闭（仅聊天）", "Remote control: Off (chat only)"],
  ["已连接 · 管理员", "Connected · Administrator"],
  ["本库 3 条资料，项目对话保持 30 分钟。示例：我的中文项目，帮我看看这个项目的工期。", "3 entries; project session lasts 30 minutes. Example: 我的中文项目, help me check this project's schedule."],
  ["当前由环境变量 XIAOA_KEY 接管，此处填写不生效。", "Managed by environment variable XIAOA_KEY; edits here have no effect."],
  ["测试失败：保留原始诊断", "Test failed: 保留原始诊断"],
]) {
  assert.equal(translate(source, "en"), expected);
  assert.equal(translate(source, "zh"), source);
}
assert.equal(translate("云端记忆同步：今天尝试 2 次，成功 1 次；最近成功：尚无成功记录；有 1 次未完成；最近错误：保留诊断", "en"),
  "Cloud memory sync: 2 attempts today, 1 successful; last success: No successful sync yet; 1 pending; last error: 保留诊断");
assert.equal(translate("合成:腾讯云 未配置 ✗　识别:腾讯云 已启用 ✓", "en"), "Speech: Tencent Cloud Not configured ✗ · Recognition: Tencent Cloud Enabled ✓");
const releaseLines = ["新增并完善 CAD 二维绘图、编辑、9类尺寸标注、标注样式管理、常用三维实体、布尔运算和导出工具。", "CAD 功能需本机安装支持 COM 的 AutoCAD；保留完整 XiaoA-data。"];
assert.equal(translate(releaseLines.join("\n"), "en"), releaseLines.map(line => EN[line]).join("\n"));
assert.equal(translate("未来版本未翻译的发布说明", "en"), "未来版本未翻译的发布说明");
assert.match(toolDescription({name:"screen_capture"}), /^Capture the desktop/);
assert.match(toolDescription({name:"cad_erase_entities",danger:true}), /High-risk.*approval/);
assert.match(toolDescription({name:"excel_write_cells",side_effect:true}), /Changes system state/);
assert.equal(toolDescription({name:"custom_user_tool"}), "");
for (const [name, expected] of [
  ["pdf_read_text", "Read PDF text page by page"],
  ["pdf_list_pages", "Check that a PDF exists"],
  ["pdf_extract_images", "Extract embedded images from a PDF"],
  ["pdf_render_pages", "Render specified PDF pages as PNG files"],
  ["word_list_paragraphs", "List body paragraphs in an existing .docx"],
  ["word_apply_replacements", "Apply multiple find-and-replace operations"],
  ["word_append_paragraphs", "Append a set of formal paragraphs"],
  ["word_insert_paragraphs", "Insert paragraphs at a specified body position"],
  ["word_append_blocks", "Append structured sections, headings"],
]) {
  const description = toolDescription({name, side_effect:true, danger:name.startsWith("pdf_extract") || name === "pdf_render_pages" || name.startsWith("word_") && !name.includes("list_")});
  assert(description.startsWith(expected), `Missing English description for ${name}`);
  assert(!/[\u3400-\u9fff]/.test(description), `Chinese text remains in ${name}`);
  if (name === "pdf_extract_images" || name === "pdf_render_pages" || name.startsWith("word_apply") || name.startsWith("word_append") || name.startsWith("word_insert")) {
    assert(description.includes("High-risk") && description.includes("approval"), `Risk notice missing for ${name}`);
  }
}
// Keep every registered Office/PDF tool covered as the catalog grows.
const officeSource = fs.readFileSync("../backend/modules/office/office_module.py", "utf8");
for (const [, name] of officeSource.matchAll(/@registry\.tool\(\s*name\s*=\s*"([^"]+)"/g)) {
  assert(toolDescription({name}), `Missing Office/PDF English description: ${name}`);
}
// Minimal DOM: exercise real observer/persistence without adding a dependency.
function element(tag = "DIV", attrs = {}, parent = null) {
  const attributes = new Map(Object.entries(attrs));
  const node = {nodeType:1, tagName:tag, childNodes:[], parentElement:parent, value:"",
    hasAttribute: key => attributes.has(key), getAttribute: key => attributes.get(key) ?? null,
    setAttribute: (key, value) => attributes.set(key, value),
    matches: selector => selector === "input, textarea" && ["INPUT", "TEXTAREA"].includes(tag),
    closest(selector) {
      if (selector.startsWith("script, style")) return attributes.has("data-no-i18n") || tag === "PRE" ? node : parent?.closest(selector);
      return node; // Test labels are inside the settings shell.
    },
    get textContent() { return node.childNodes.map(child => child.nodeValue || "").join(""); },
  };
  return node;
}
function appendText(parent, value) {
  const text = {nodeType:3, parentElement:parent, nodeValue:value}; parent.childNodes.push(text); return text;
}
{
  const body = element(), status = element("SPAN", {}, body), option = element("OPTION", {}, body);
  body.childNodes.push(status, option);
  const first = appendText(status, "暂无已授权用户"), label = appendText(option, "未连接");
  const input = element("INPUT", {placeholder:"请输入 App Secret"}, body); input.value = "中文密钥不改"; body.childNodes.push(input);
  const custom = element("SPAN", {"data-no-i18n":""}, body); body.childNodes.push(custom);
  const customText = appendText(custom, "测试");
  const app = runtime("en", false, body);
  assert.equal(first.nodeValue, "No authorized users"); assert.equal(label.nodeValue, "Disconnected");
  assert.equal(option.getAttribute("value"), "未连接");
  assert.equal(input.getAttribute("placeholder"), "Enter App Secret");
  assert.equal(input.value, "中文密钥不改"); assert.equal(customText.nodeValue, "测试");
  const dynamic = element("SPAN", {}, body); body.childNodes.push(dynamic);
  const message = appendText(dynamic, "尚未安装");
  app.mutate([{type:"childList",addedNodes:[dynamic]}]);
  assert.equal(message.nodeValue, "Not installed");
  message.nodeValue = "正在本机运行"; app.mutate([{type:"characterData",target:message}]);
  assert.equal(message.nodeValue, "Running on this computer");
  app.window.XiaoALanguage.set("zh"); assert.equal(message.nodeValue, "正在本机运行");
  const bilingual = element("SPAN", {"data-i18n-en":"WeChat · 测试 · Administrator"}, body);
  body.childNodes.push(bilingual); const account = appendText(bilingual, "微信 · 测试 · 管理员");
  app.mutate([{type:"childList",addedNodes:[bilingual]}]);
  for (let i = 0; i < 3; i++) {
    app.window.XiaoALanguage.set("en");
    assert.equal(account.nodeValue, "WeChat · 测试 · Administrator");
    assert.equal(customText.nodeValue, "测试"); assert.equal(input.value, "中文密钥不改");
    app.window.XiaoALanguage.set("zh"); assert.equal(account.nodeValue, "微信 · 测试 · 管理员");
    assert.equal(option.getAttribute("value"), "未连接");
  }
}
{
  const body = element();
  const select = element("SELECT", {}, body); body.childNodes.push(select);
  const group = element("OPTGROUP", {label:"推荐 · 大模型 · 中英混读（需相应权限）"}, select); select.childNodes.push(group);
  const option = element("OPTION", {value:"4189"}, group); group.childNodes.push(option);
  const label = appendText(option, "度涵竹 · 开朗女声 · 4189");
  const app = runtime("zh", false, body);
  app.window.XiaoALanguage.set("en");
  assert.equal(group.getAttribute("label"), "Recommended · Large-model · Chinese/English (permission required)");
  assert.equal(label.nodeValue, "Du Hanzhu · Cheerful female · 4189");
  assert.equal(option.getAttribute("value"), "4189");
  app.window.XiaoALanguage.set("zh");
  assert.equal(group.getAttribute("label"), "推荐 · 大模型 · 中英混读（需相应权限）");
  assert.equal(label.nodeValue, "度涵竹 · 开朗女声 · 4189");
  // Options inserted after startup (for a saved voice absent from the catalogue) follow the same locale.
  const fallback = element("OPTION", {value:"4200", "data-i18n-en":"Current voice 4200"}, select);
  select.childNodes.push(fallback); const fallbackLabel = appendText(fallback, "当前音色 4200");
  app.mutate([{type:"childList", addedNodes:[fallback]}]);
  assert.equal(fallbackLabel.nodeValue, "当前音色 4200");
  app.window.XiaoALanguage.set("en");
  assert.equal(fallbackLabel.nodeValue, "Current voice 4200");
  assert.equal(fallback.getAttribute("value"), "4200");
}
assert(/html\[data-language="en"\] \.settings-nav::before\s*\{ content: "XiaoA settings"; \}/.test(fs.readFileSync("src/settings.html", "utf8")));
console.log("Settings language: dynamic lists, templates, bilingual labels, option values, placeholders, user data and round trips passed");


for (const [source, expected] of [
  ["服务器：my-host · 部署 / 更新 · 云端当前版本 未部署", "Server: my-host · Deploy / Update · Cloud version Not deployed"],
  ["云端端口：1985 · 服务运行中 · 健康检查通过 · 有未完成回退点，请修复", "Cloud port: 1985 · Service running · Health check passed · Unfinished rollback point; repair is required"],
  ["3. 安装运行环境 · 进行中 · 12秒", "3. Install runtime · In progress · 12s"],
  ["已完成 2/5 个执行步骤（另 1 步本次不执行） · 已排队", "2/5 steps completed (1 skipped this time) · Queued"],
  ["已完成 1/6 个执行步骤 · 保留原始错误", "1/6 steps completed · 保留原始错误"],
  ["已上传 50% · 100/200 字节", "Uploaded 50% · 100/200 bytes"],
]) {
  assert.equal(translate(source, "en"), expected);
  assert.equal(translate(source, "zh"), source);
}

{
  const body = element(), logs = element("PRE", {}, body);
  logs.id = "cloud_deploy_logs"; logs.dataset = {i18nEmpty:"true"};
  body.childNodes.push(logs); const label = appendText(logs, "尚无部署日志");
  const app = runtime("en", false, body);
  assert.equal(label.nodeValue, "No deployment logs yet");
  app.window.XiaoALanguage.set("zh"); assert.equal(label.nodeValue, "尚无部署日志");
  logs.dataset.i18nEmpty = "false";
  label.nodeValue = "尚无部署日志"; // A real log may happen to match a known label.
  app.window.XiaoALanguage.set("en"); assert.equal(label.nodeValue, "尚无部署日志");
  app.mutate([{type:"characterData",target:label}]); assert.equal(label.nodeValue, "尚无部署日志");
  logs.dataset.i18nEmpty = "true";
  app.mutate([{type:"characterData",target:label}]); assert.equal(label.nodeValue, "No deployment logs yet");
}
console.log("Cloud language: progress templates, reversible empty state and verbatim real logs passed");

// Tool descriptions inserted after load must switch both ways without changing tool IDs.
{
  const body = element();
  const descriptions = ["pdf_read_text", "pdf_list_pages", "pdf_extract_images", "pdf_render_pages",
    "word_list_paragraphs", "word_apply_replacements", "word_append_paragraphs", "word_insert_paragraphs", "word_append_blocks"];
  const app = runtime("en", false, body);
  for (const name of descriptions) {
    const source = `${name} 中文工具说明`, english = toolDescription({name, danger:true});
    const label = element("SPAN", {"data-i18n-en":english}, body);
    body.childNodes.push(label);
    const node = appendText(label, source);
    app.mutate([{type:"childList",addedNodes:[label]}]);
    assert.equal(node.nodeValue, english);
    for (let i = 0; i < 3; i++) {
      app.window.XiaoALanguage.set("zh"); assert.equal(node.nodeValue, source);
      app.window.XiaoALanguage.set("en"); assert.equal(node.nodeValue, english);
    }
  }
}
// Include tools registered via CAD helper decorators, not just @registry.tool.
const cadSource = fs.readFileSync("../backend/modules/cad/cad_module.py", "utf8");
const cadNames = JSON.parse(cadSource.match(/EXPOSED_TOOLS = (\[[\s\S]*?\])/)[1].replace(/,\s*\]/, "]"));
for (const name of cadNames) {
  const description = toolDescription({name, side_effect:true});
  assert(description, `Missing CAD English description: ${name}`);
  assert(!/[\u3400-\u9fff]/.test(description), name);
}
const lockHint = settingsHtml.match(/在飞书或微信发一句「锁定」[\s\S]*?远程无法解锁|在飞书或微信发一句「锁定」[\s\S]*?是无效的。/)[0];
const englishLockHint = translate(lockHint, "en");
assert(englishLockHint.includes('“lock”') && englishLockHint.includes('“unlock”'));
assert(!/[\u3400-\u9fff]/.test(englishLockHint));
// Exercise the real extension renderer, including the backend's explicit local marker.
const extensionCode = settingsHtml.slice(settingsHtml.indexOf("    function renderExtensionList("), settingsHtml.indexOf("    async function loadExtensions("));
const makeNode = () => ({children:[],dataset:{},append(...items){this.children.push(...items);},appendChild(item){this.children.push(item);}});
const list = makeNode();
const renderExtensions = new Function("document", "$", extensionCode + "; return renderExtensionList;")({createElement:makeNode},()=>list);
renderExtensions("skill", [{name:"security-test",version:1,source:"本地"},{name:"中文名称",source:"https://example.test/中文来源"}]);
const metadata = list.children.map(row=>row.children[0].children[1].children[0]);
assert.equal(metadata[0].textContent, "v1 · 本地 · ");
assert.equal(metadata[0].dataset.i18nEn, "v1 · Local · ");
assert(metadata[1].dataset.i18nEn.includes("https://example.test/中文来源"));
assert(!/id="modelMenu"[^>]*data-no-i18n/.test(html));
console.log("Remaining language gaps: full CAD manifest, local extension source, English lock commands and model menu passed");

// Use the renderer's real states so a renamed status cannot silently bypass translation.
const traceStates = vm.runInNewContext("(" + fs.readFileSync("src/index.html", "utf8").match(/const TRACE_STATES = (\{[^;]+\});/)[1] + ")");
for (const [state, english] of Object.entries({running:"Running", waiting:"Awaiting approval", completed:"Completed", failed:"Failed", stopped:"Stopped", interrupted:"Interrupted", limited:"Continuation needed"})) {
  for (const [kind, label] of [["回复过程", "Reply trace"], ["执行过程", "Execution trace"]]) {
    const source = `${traceStates[state]} · ${kind}`;
    assert.equal(translate(source, "en"), `${english} · ${label}`);
    assert.equal(translate(source, "zh"), source);
  }
}
for (const [source, expected] of [
  ["当前：正在准备回复", "Current: Preparing a reply"],
  ["当前：正在等待回复", "Current: Waiting for a reply"],
  ["当前：正在整理结果", "Current: Preparing results"],
  ["当前：等待你确认操作", "Current: Waiting for your approval"],
  ["当前：确认已收到，正在执行", "Current: Approval received; running"],
  ["当前：操作已拒绝，正在整理结果", "Current: Operation rejected; preparing results"],
  ["当前：正在从保存断点继续", "Current: Resuming from the saved checkpoint"],
  ["当前：连接已恢复，正在继续处理", "Current: Connection restored; resuming"],
  ["当前：自定义中文诊断", "Current: 自定义中文诊断"],
  ["0 次操作 · 3 秒", "0 actions · 3s"],
  ["2 次操作 · 1 分 5 秒", "2 actions · 1m 5s"],
  ["模型等待 3 秒 · 工具执行 1 分 2 秒", "Model wait 3s · Tool execution 1m 2s"],
  ["模型等待 3 秒", "Model wait 3s"],
  ["工具执行 0 秒", "Tool execution 0s"],
]) {
  assert.equal(translate(source, "en"), expected);
  assert.equal(translate(source, "zh"), source);
}
{
  const body = element(), current = element("DIV", {}, body);
  body.childNodes.push(current);
  // Unlike the settings mock, require the actual live progress selector to match.
  current.closest = selector => selector.startsWith("script, style") ? null : selector.split(/,\s*/).includes(".trace-current") ? current : null;
  const app = runtime("en", false, body);
  const status = appendText(current, "当前：正在准备回复");
  app.mutate([{type:"childList", addedNodes:[status]}]);
  assert.equal(status.nodeValue, "Current: Preparing a reply");
  status.nodeValue = "当前：正在整理结果";
  app.mutate([{type:"characterData", target:status}]);
  assert.equal(status.nodeValue, "Current: Preparing results");
  for (let i = 0; i < 3; i++) {
    app.window.XiaoALanguage.set("zh");
    assert.equal(status.nodeValue, "当前：正在整理结果");
    app.window.XiaoALanguage.set("en");
    assert.equal(status.nodeValue, "Current: Preparing results");
  }
}
console.log("Task trace language: live title, current status, durations, timings and repeated toggles passed");

const publishedNotes = [
  "新增“中 / EN”语言切换，设置页、悬浮图标右键菜单和工具说明同步切换；新消息默认按界面语言回复，用户明确指定的语言优先。",
  "完善 Office/WPS 文档与表格读取、编辑和保存的兼容处理；保留文件占用校验，不覆盖未保存内容或关闭用户原有实例。",
  "优化启动及插件运行时缓存复用，减少重复准备；修复运行时缓存标记导致正式包验收误报的问题。",
  "进一步加固 CAD/UIA 保存流程、窗口焦点和 PID 归属校验；未保存图纸、用户原有实例或任务失败时保留现场。",
  "保留 CAD 二维绘图、编辑、9类尺寸标注与常用三维工具；正式包内置同版本云端文件。",
];
const englishPublishedNotes = [
  "Added a Chinese/English toggle across settings, the floating-icon context menu and tool descriptions. New replies follow the interface language unless the user requests another language.",
  "Improved Office/WPS document and spreadsheet compatibility for reading, editing and saving. File-lock checks remain in place; unsaved work and existing app instances are preserved.",
  "Improved startup and plugin-runtime cache reuse to avoid repeated setup, and fixed a false release-validation failure caused by runtime cache markers.",
  "Hardened CAD/UIA save workflows, window focus and PID ownership checks. Unsaved drawings, existing user instances and failed-task state are preserved.",
  "Retained CAD 2D drawing and editing, nine dimension types and common 3D tools. The release includes matching-version cloud files.",
];
assert.equal(translate(publishedNotes.join("\n"), "en"), englishPublishedNotes.join("\n"));
assert.equal(translate(publishedNotes.join("\n"), "zh"), publishedNotes.join("\n"));
assert.equal(translate("当前下载版更新了什么", "en"), "What’s new in this release");
const latestReleaseNotes = [
  "- “语音与对话”新增百度智能云识别与合成，保留腾讯云，分别保存服务商的密钥、音色和语速；百度无需 AppID。",
  "- 增加大模型与精品中英混读音色，试听短句包含中文、英文及常见办公词汇；具体音色需账号开通相应权限。",
  "- 唤醒应答“我在”只使用已保存的云音色，首次等待预加载，切换服务商、音色或语速后刷新缓存；失败时提示并允许重试，不再切换系统声音。",
  "- 微信、飞书保留原有连接与消息处理方式，音频识别适配器可在保存语音设置后生效；补充 Ogg Opus、MP3、WAV 转换支持和云端配置同步。",
  "",
  "下载 `XiaoA.exe` 即可使用 Windows x64 便携版。升级前请正常退出小A，并保留原有 `data` 目录和配置。也可在小A内通过 GitHub Releases 更新通道升级。",
  "",
  "本项目按 PolyForm Noncommercial License 1.0.0 发布，禁止商业使用；商业用途需取得单独书面授权。",
].join("\n");
const englishLatestReleaseNotes = [
  "- Baidu AI Cloud speech recognition and synthesis are now available under Speech & Conversation, alongside Tencent Cloud. Each provider keeps its own credentials, voice and speed; Baidu does not require an AppID.",
  "- Added large-model and premium voices for mixed Chinese and English. Preview phrases include Chinese, English and common office terms; some voices require account permissions.",
  "- The “I’m here” wake response now uses only the saved cloud voice. It waits for the initial preload and refreshes its cache when the provider, voice or speed changes. Failures are reported with a retry option; it no longer falls back to a system voice.",
  "- WeChat and Feishu retain their existing connections and message handling. Audio transcription adapters take effect after saving speech settings; added Ogg Opus, MP3 and WAV conversion support, plus cloud configuration sync.",
  "",
  "Download `XiaoA.exe` to use the Windows x64 portable edition. Before upgrading, exit XiaoA normally and keep the existing `data` folder and settings. You can also update through the GitHub Releases channel in XiaoA.",
  "",
  "This project is released under the PolyForm Noncommercial License 1.0.0. Commercial use is prohibited unless separately authorized in writing.",
].join("\n");
const release1048Notes = [
  "## 小A 1.0.48",
  "",
  "- 消息定位预览固定使用当前明暗主题的实色背景，避免下方聊天文字透出，保留平滑展开与滚动。",
  "- 聊天回复支持本地公式渲染，正常显示米、平方米、上下标、比较符号和分数；兼容历史消息和流式回复，代码块及普通金额保持原样。",
  "- 修复 Windows 发布校验读取多层目录时的路径问题，逐项核对内置公式组件和许可文件。",
  "",
  "公式渲染内置于便携版，不依赖外部 CDN。此版本包含未发布成功的 1.0.47 全部修复。",
  "",
  "升级前请正常退出小A，保留同目录完整 `XiaoA-data` 文件夹。禁止商业使用，许可见仓库 LICENSE。",
  "",
].join("\n");
const englishRelease1048Notes = [
  "## XiaoA 1.0.48",
  "",
  "- Message navigation previews now use a solid background matching the current light or dark theme, preventing chat text from showing through while preserving smooth expansion and scrolling.",
  "- Chat replies now render formulas locally, displaying meters, square meters, superscripts, subscripts, comparison symbols and fractions correctly. This works with past messages and streaming replies; code blocks and ordinary monetary amounts remain unchanged.",
  "- Fixed a Windows release-verification path issue when reading nested directories, and verified the bundled formula components and license files individually.",
  "",
  "Formula rendering is built into the portable edition and does not depend on an external CDN. This version includes all fixes from the unsuccessful 1.0.47 release.",
  "",
  "Before upgrading, exit XiaoA normally and keep the entire `XiaoA-data` folder beside the EXE. Commercial use is prohibited; see LICENSE in the repository.",
  "",
].join("\n");
const release1049Notes = [
  "## 小A 1.0.49",
  "",
  "- 语音唤醒旁新增停止播放按钮，可立即停止当前朗读；关闭语音唤醒时，同时停止回复和唤醒应答。",
  "- 停止播放后，尚未完成的语音合成不会重新播放；任务和文字回复继续，下一轮语音对话可正常朗读。",
  "- 优化麦克风录音的采样率转换和抗混叠处理，长录音优先在停顿处切段；识别文本仍可在发送前审阅。",
  "- 补齐版本与更新说明的英文翻译，支持动态加载和中英切换。",
  "",
  "升级前请正常退出小A，保留同目录完整 `XiaoA-data` 文件夹。禁止商业使用，许可见仓库 LICENSE。",
  ""
].join("\n");
const englishRelease1049Notes = [
  "## XiaoA 1.0.49",
  "",
  "- Added a stop-playback button beside Voice wake-up to stop speech immediately. Turning off Voice wake-up also stops replies and the wake greeting.",
  "- Pending speech synthesis no longer restarts playback after you stop it. Tasks and text replies continue, and speech resumes on the next voice conversation turn.",
  "- Improved microphone sample-rate conversion and anti-aliasing, with long recordings split at pauses where possible. You can still review recognized text before sending.",
  "- Completed English translations for version and update notes, including dynamically loaded notes and Chinese/English switching.",
  "",
  "Before upgrading, exit XiaoA normally and keep the entire `XiaoA-data` folder beside the EXE. Commercial use is prohibited; see LICENSE in the repository.",
  ""
].join("\n");
const release1050Notes = [
  "## 小A 1.0.50",
  "",
  "- 修复聊天表格中 `<br>` 显示为文本的问题，正确显示单元格内换行。",
  "- 修复百分比及坐标公式残留反斜杠或 `$` 的问题，保留普通金额和代码原文。",
  "- 补齐 PDF 与 Word 工具目录的英文说明，支持中英切换并保留风险提示。",
  "- 本次更新说明支持英文显示、动态加载和中英往返切换。",
  "",
  "升级前请正常退出小A，保留同目录完整 `XiaoA-data` 文件夹。禁止商业使用，许可见仓库 LICENSE。",
  ""
].join("\n");
const englishRelease1050Notes = [
  "## XiaoA 1.0.50",
  "",
  "- Fixed literal `<br>` text in chat tables so line breaks display correctly inside cells.",
  "- Fixed stray backslashes and `$` delimiters in percentage and coordinate formulas while preserving ordinary monetary amounts and code.",
  "- Completed English descriptions for PDF and Word tools, including Chinese/English switching and risk notices.",
  "- These update notes support English display, dynamic loading and switching between Chinese and English.",
  "",
  "Before upgrading, exit XiaoA normally and keep the entire `XiaoA-data` folder beside the EXE. Commercial use is prohibited; see LICENSE in the repository.",
  ""
].join("\n");
for (const newline of ["\n", "\r\n"]) {
  assert.equal(translate(release1050Notes.replace(/\n/g, newline), "en"), englishRelease1050Notes.replace(/\n/g, newline));
  assert.equal(translate(release1050Notes.replace(/\n/g, newline), "zh"), release1050Notes.replace(/\n/g, newline));
}
for (const newline of ["\n", "\r\n"]) {
  assert.equal(translate(release1049Notes.replace(/\n/g, newline), "en"), englishRelease1049Notes.replace(/\n/g, newline));
  assert.equal(translate(release1049Notes.replace(/\n/g, newline), "zh"), release1049Notes.replace(/\n/g, newline));
}
for (const newline of ["\n", "\r\n"]) {
  const source = release1048Notes.replace(/\n/g, newline);
  const expected = englishRelease1048Notes.replace(/\n/g, newline);
  assert.equal(translate(source, "en"), expected);
  assert.equal(translate(source, "zh"), source);
}
assert.equal(translate("用户自定义更新说明保持原样\n不要修改文件名", "en"), "用户自定义更新说明保持原样\n不要修改文件名");
assert.equal(translate(latestReleaseNotes, "en"), englishLatestReleaseNotes);
assert.equal(translate(latestReleaseNotes, "zh"), latestReleaseNotes);
assert.equal(translate(latestReleaseNotes.replace(/\n/g, "\r\n"), "en"), englishLatestReleaseNotes.replace(/\n/g, "\r\n"));
for (const [source, expected] of [
  [latestReleaseNotes, englishLatestReleaseNotes],
  [release1048Notes, englishRelease1048Notes],
  [release1049Notes, englishRelease1049Notes],
  [release1050Notes, englishRelease1050Notes],
]) {
  const body = element(), releaseNotes = element("DIV", {}, body);
  body.childNodes.push(releaseNotes);
  releaseNotes.closest = selector => selector.startsWith("script, style") ? null
    : selector.split(/,\s*/).includes(".hint") ? releaseNotes : null;
  const app = runtime("en", false, body);
  const noteText = appendText(releaseNotes, source);
  app.mutate([{type:"childList", addedNodes:[noteText]}]);
  assert.equal(noteText.nodeValue, expected);
  for (let i = 0; i < 3; i++) {
    app.window.XiaoALanguage.set("zh");
    assert.equal(noteText.nodeValue, source);
    app.window.XiaoALanguage.set("en");
    assert.equal(noteText.nodeValue, expected);
  }
  noteText.nodeValue = source;
  app.mutate([{type:"characterData", target:noteText}]);
  assert.equal(noteText.nodeValue, expected);
}
for (const [line, expected] of [
  ["[08:05:27] 云端连接成功，worker 已上线", "[08:05:27] Cloud connected successfully; worker is online"],
  ["[08:05:17] 安装包上传完成，正在校验并创建回退点…", "[08:05:17] Package upload complete; verifying and creating a rollback point…"],
  ["[08:05:17] 依赖未变化，复用已有运行环境", "[08:05:17] Dependencies unchanged; reusing the existing runtime"],
  ["[08:05:18] 核验/生成本机连接 TLS 证书（已有指纹保留）", "[08:05:18] Verifying/creating the local-connection TLS certificate (existing fingerprint is preserved)"],
  ["[08:05:18] 保留已有 token、密码和配置；写入小A服务定义", "[08:05:18] Preserving existing tokens, passwords and settings; writing the XiaoA service definition"],
  ["[08:05:18] 申请公网 IP HTTPS 证书并配置自动续期（需放行公网 80）", "[08:05:18] Requesting a public-IP HTTPS certificate and configuring automatic renewal (public port 80 must be open)"],
  ["[08:05:22] 正在保留原有配置并同步本次模型/飞书设置", "[08:05:22] Preserving existing settings and syncing the selected model/Feishu settings"],
  ["[08:05:23] 检查 systemd 与 TLS 健康接口（最长 60 秒）", "[08:05:23] Checking systemd and the TLS health endpoint (up to 60 seconds)"],
  ["[08:05:25] 云端健康检查通过 · 更新", "[08:05:25] Cloud health check passed · Update"],
  ["[08:05:26] 云端已健康上线，正在保存本地连接配置", "[08:05:26] Cloud is healthy; saving the local connection settings"],
  ["[08:05:26] 本地配置已保存，正在连接云端 worker…", "[08:05:26] Local settings saved; connecting to the cloud worker…"],
  ["[08:05:27] 已上传 100% · 1024280/1024280 字节", "[08:05:27] Uploaded 100% · 1024280/1024280 bytes"],
]) assert.equal(translateCloudLog(line, "en"), expected);
assert.equal(translateCloudLog("[08:05:30] 用户自定义诊断保持中文", "en"), "[08:05:30] 用户自定义诊断保持中文");
assert.equal(translateCloudLog("[08:05:30] 云端连接成功，worker 已上线", "zh"), "[08:05:30] 云端连接成功，worker 已上线");
assert.equal(translate("云端连接成功，worker 已上线", "en"), "Cloud connected successfully; worker is online");
assert.equal(translate("已完成 6/6 个执行步骤 · 云端连接成功，worker 已上线", "en"), "6/6 steps completed · Cloud connected successfully; worker is online");
assert.equal(translate("最近操作：部署 / 更新 · 2026/10/26 08:05:27 · 云端连接成功，worker 已上线", "en"), "Latest operation: Deploy / Update · 2026/10/26 08:05:27 · Cloud connected successfully; worker is online");
console.log("Cloud deployment and release-note English: screenshots, timestamped logs, dynamic status, safe custom diagnostics and locale changes passed");

assert.equal(translate("合成:百度智能云 已填写（保存后生效） 识别:百度智能云 已填写（保存后生效）", "en"),
  "Speech: Baidu AI Cloud Entered (save to apply) · Recognition: Baidu AI Cloud Entered (save to apply)");

// Deletion labels, confirmations and environment errors round-trip in both languages.
for (const [source, english] of [
  [
    "删除已保存的百度智能云凭据",
    "Delete saved Baidu AI Cloud credentials"
  ],
  [
    "取消删除百度智能云凭据",
    "Cancel Baidu AI Cloud credential deletion"
  ],
  [
    "重试同步删除百度智能云凭据",
    "Retry Baidu AI Cloud credential deletion sync"
  ],
  [
    "确认删除已保存的百度智能云 API Key 和 Secret Key？点击“保存此页”后才会清除。",
    "Delete the saved Baidu AI Cloud API Key and Secret Key? They will be removed when you click “Save this page”."
  ],
  [
    "百度智能云凭据由环境变量管理，请在启动环境中移除后再操作。",
    "Baidu AI Cloud credentials are managed by environment variables. Remove them from the launch environment first."
  ],
  [
    "删除百度智能云凭据",
    "Delete Baidu AI Cloud credentials"
  ],
  [
    "百度智能云凭据由环境变量管理，请在启动环境中移除对应变量后再清除",
    "Baidu AI Cloud credentials are managed by environment variables. Remove the variables from the launch environment first."
  ]
]) {
  assert.equal(translate(source, "en"), english);
  assert.equal(translate(source, "zh"), source);
}

for (const [source, english] of [["试听内容", "Preview text"], ["输入你想试听的文字，支持中英文混读", "Enter text to preview, including mixed Chinese and English"], ["请输入试听内容（最多 500 个字符）", "Enter preview text (up to 500 characters)"]]) {
  assert.equal(translate(source, "en"), english);
  assert.equal(translate(source, "zh"), source);
}

assert.equal(translate("\u6d88\u606f\u5b9a\u4f4d", "en"), "Message navigation");
assert.equal(translate("\u9644\u4ef6\u6d88\u606f", "en"), "Attachment message");
