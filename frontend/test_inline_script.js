const fs = require("node:fs");
const assert = require("node:assert/strict");
const { applyLoginItemSetting } = require("./login_item");
const packageJson = JSON.parse(fs.readFileSync("package.json", "utf8"));

for (const file of ["src/index.html", "src/settings.html", "src/float.html"]) {
  const page = fs.readFileSync(file, "utf8");
  const scripts = [...page.matchAll(/<script(?:\s[^>]*)?>([\s\S]*?)<\/script>/g)];
  for (const match of scripts) {
    const source = match[1].trim();
    if (source) new Function(source);
  }
}
const html = fs.readFileSync("src/index.html", "utf8").replace(/\r\n/g, "\n");
const settingsCspHtml = fs.readFileSync("src/settings.html", "utf8");
assert.match(html, /@media \(max-width: 560px\)\s*\{\s*header \{ flex-wrap: nowrap;/, "compact windows keep the header controls in one row");
assert.match(html, /header \.brand > \* \{ flex: none; \}/, "compact header buttons retain their dimensions");
assert.match(html, /header \.right \{ flex: 0 0 auto;/, "right controls do not get squeezed by the brand group");
assert.match(html, /\.toolbar \{ flex-wrap: nowrap; white-space: nowrap; \}/, "wake control stays on one line in compact windows");
assert.match(html, /\.composer-right \{ flex: none; \}/, "send controls retain their width in compact windows");
if (!settingsCspHtml.includes("img-src 'self' data:")) {
  throw new Error("settings page must allow generated data URL QR images");
}
if (!settingsCspHtml.includes("media-src 'self' data: blob:")) {
  throw new Error("settings page must allow blob audio previews");
}
if (!html.includes('img.src = "data:image/jpeg;base64," + b64;') ||
    !html.includes('img.className = "message-image";') ||
    !html.includes('width: 170px; max-width: 100%; max-height: 220px; height: auto; object-fit: contain;') ||
    !/\.message-attachments\s*\{[^}]*align-items:\s*flex-start/.test(html)) {
  throw new Error("screenshots and attachments must share bounded, aspect-ratio-preserving thumbnails");
}
{
  const assert = require("node:assert/strict");
  const imageFunctions = html.slice(html.indexOf("    function appendAttachments("), html.indexOf("    function appendReplyAttachments(")) +
    html.slice(html.indexOf("    function addScreenshot("), html.indexOf("    let historyLoaded ="));
  const element = () => ({
    children: [], style: {}, listeners: {},
    appendChild(child) { this.children.push(child); },
    querySelector(selector) { return this.children.find(child => selector === `.${child.className}`); },
    addEventListener(type, callback) { this.listeners[type] = callback; },
  });
  const log = element(), window = {}, opened = [];
  const {appendAttachments, addScreenshot} = new Function("document", "log", "window", "showImageOverlay", "scrollIfNear", "API",
    `${imageFunctions}; return {appendAttachments, addScreenshot};`
  )({createElement: element}, log, window, src => opened.push(src), () => {}, "http://local");
  for (const role of ["user", "assistant"]) {
    const bubble = element(); bubble.role = role;
    appendAttachments(bubble, [{type: "image/jpeg", data: "data:image/jpeg;base64,attachment", name: "test.jpg"}]);
    const img = bubble.children[0].children[0];
    assert.equal(img.className, "message-image", `${role} attachments must use shared thumbnails`);
    img.onclick(); assert.equal(opened.pop(), img.src);
  }
  addScreenshot("screenshot");
  const img = log.children[0].children[1];
  assert.equal(img.className, "message-image", "live screenshots must use shared thumbnails");
  assert.equal(img.style.maxWidth, undefined, "inline sizing must not override thumbnail bounds");
  img.onclick(); assert.equal(opened.pop(), img.src, "browser screenshot enlargement must still work");
  window.bridge = {openImage: value => opened.push(value)};
  img.onclick(); assert.equal(opened.pop(), "screenshot", "desktop screenshot viewer must still work");
}
// Tool failures keep semantic uncertainty visible and retain full, inert details.
{
  const assert = require("node:assert/strict");
  const events = [];
  const start = html.indexOf("    function addToolFailure(");
  const end = html.indexOf("    function addTaskDuration(", start);
  const addToolFailure = new Function("TOOL_LABEL", "addEvent",
    `${html.slice(start, end)}; return addToolFailure;`
  )({send_text_to_window: "发送到窗口"}, (...args) => events.push(args));
  for (const [status, cls, phrase] of [
    ["window_unavailable", "error", "未发送"],
    ["window_not_found", "error", "不代表账号未登录"],
    ["uia_target_not_found", "error", "未发送"],
    ["destination_not_confirmed", "error", "未发送"],
    ["sent_unverified", "warning", "不要直接重发"],
    ["previous_send_unconfirmed", "warning", "已阻止重复发送"],
    ["background_partial", "warning", "发送结果不确定"],
  ]) {
    const result = {ok: false, status, note: "x".repeat(500)};
    addToolFailure({name: "send_text_to_window", result: JSON.stringify(result)});
    const [actualClass, message, detail] = events.pop();
    assert.equal(actualClass, cls);
    assert(message.includes(phrase));
    assert(message.includes("发送到窗口"));
    assert.deepEqual(JSON.parse(detail), result, "raw details must not be cut to 200 characters");
  }
  addToolFailure({name: "send_text_to_window", result: {status: "destination_not_confirmed", verification_reason: "看图核验请求异常"}});
  assert(events.pop()[1].includes("看图核验请求异常"));
  addToolFailure({name: "other_tool", result: {status: "sent_unverified", error: "真正的错误"}});
  assert.equal(events.pop()[0], "error", "do not relabel unrelated tool failures");
  addToolFailure({name: "other_tool", result: "not-json"});
  assert.equal(events.pop()[2], "not-json");
  const makeNode = tag => ({tag, children: [], appendChild(child) { this.children.push(child); }});
  const log = makeNode("div");
  const eventStart = html.indexOf("    function addEvent(");
  const addEvent = new Function("document", "log", "scrollIfNear",
    `${html.slice(eventStart, start)}; return addEvent;`
  )({createElement: makeNode}, log, () => {});
  const untrusted = '<img src=x onerror="alert(1)">';
  addEvent("warning", untrusted, untrusted);
  assert.equal(log.children[0].textContent, untrusted);
  assert.equal(log.children[0].children[0].children[1].textContent, untrusted);
  assert.equal(log.children[0].children[0].tag, "details");
}
// Exercise the actual error handler and shared state functions, not source-string matching.
{
  const assert = require("node:assert/strict");
  const vm = require("node:vm");
  function section(start, end) {
    const from = html.indexOf(start), to = html.indexOf(end, from);
    assert(from >= 0 && to > from, `missing error regression section: ${start}`);
    return html.slice(from, to);
  }
  function element(className = "") {
    const node = {
      className, textContent: "", value: "", attributes: {},
      setAttribute(name, value) { this.attributes[name] = value; },
      classList: {
        contains(name) { return node.className.split(" ").includes(name); },
        toggle(name, on) { this[name] = on; },
      },
    };
    return node;
  }
  function runtime() {
    const events = [], notifications = [], timers = [], bubbles = [], copies = [];
    const oldSpeech = {cancelled: false};
    let speechCancelled = 0, continuousStopped = 0, healthRefreshes = 0;
    const context = vm.createContext({
      window: {
        bridge: {clearFloatAlert() {}, notifyFloat: value => notifications.push(value)},
        speechSynthesis: {cancel() { speechCancelled++; }},
      },
      statusEl: element(), sendBtn: element(), connEl: element("conn ok"),
      textEl: element(), pendingAttachments: [], pendingDeliveries: new Map(),
      WebSocket: {OPEN: 1}, ws: {readyState: 1},
      clearTimeout: timer => timers.push(timer),
      addEvent: (...args) => events.push(args), addTaskDuration() {}, refreshHealth() { healthRefreshes++; },
      setBubbleContent: (_bubble, text) => bubbles.push(text),
      addCopyAction: (_bubble, text) => copies.push(text),
      stopContinuousMode() { continuousStopped++; vm.runInContext("continuousActive = false", context); },
    });
    vm.runInContext(`
      let taskBusy = false, serviceFailed = false;
      let continuousActive = true, speakNextReply = true, wakeReplyText = "旧开场白";
      let streamBubble = {}, streamText = "已生成的部分正文", streamSpeech = null;
      let speakSeq = 0, ttsAudio = null, ttsResolve = null, browserSpeechResolve = null;
      ${section("    function setServiceFailed(", "    function newClientId(")}
      ${section("    function clearDeliveryTimer(", "    function failDelivery(")}
      ${section("    function finishStreamBubble(", "    function addEvent(")}
      ${section("    function addToolFailure(", "    function addTaskDuration(")}
      const TOOL_LABEL = {send_text_to_window: "发送到窗口"};
      ${section("    function handleEvent(", "    // 收到飞书消息:")}
      ${section("    function setBusy(", "    function send(")}
      ${section("    function cancelSpeech(", "    // 把长文本按标点/长度")}
      setBusy(true); statusEl.textContent = "工作中…第5步";
    `, context);
    context.pendingDeliveries.set("request-1", {payload: {client_id: "request-1"}, timer: 123});
    context.oldSpeech = oldSpeech;
    vm.runInContext("streamSpeech = oldSpeech", context);
    return {
      context, events, notifications, timers, bubbles, copies, oldSpeech,
      value: source => vm.runInContext(source, context),
      speechCancelled: () => speechCancelled, continuousStopped: () => continuousStopped,
      healthRefreshes: () => healthRefreshes,
    };
  }
  for (const data of [
    {where: "llm.config"}, // old backend, without client_id or terminal marker
    {where: "llm.config", client_id: "request-1", task_finished: true},
    {where: "llm.chat"}, // old service-error payload
    {where: "llm.chat", client_id: "request-1", task_finished: true, service_failed: true},
    {client_id: "request-1"}, // rejected input
    {task_finished: true}, // explicit terminal error
  ]) {
    const r = runtime(), c = r.context;
    c.handleEvent({type: "error", data: {...data, msg: "网关返回 400"}});
    const failed = data.where === "llm.chat" || !!data.service_failed;
    assert.equal(r.healthRefreshes(), data.where === "llm.config" ? 1 : 0);
    assert.equal(r.value("taskBusy"), false);
    assert.equal(c.sendBtn.title, "发送");
    assert.equal(c.sendBtn.attributes["aria-label"], "发送");
    assert.equal(c.sendBtn.classList["stop-mode"], false);
    assert(c.sendBtn.innerHTML.includes("<path"), "must restore send icon");
    assert.equal(c.sendBtn.disabled, failed);
    assert.equal(r.value("serviceFailed"), failed, "config errors must not turn the connection red");
    assert.equal(c.connEl.className, failed ? "conn" : "conn ok");
    assert.equal(c.statusEl.textContent, failed ? "服务异常" : "");
    assert.equal(c.pendingDeliveries.size, data.client_id ? 0 : 1);
    assert.equal(r.timers.length, data.client_id ? 1 : 0);
    assert.deepEqual(r.bubbles, ["已生成的部分正文"]);
    assert.deepEqual(r.copies, ["已生成的部分正文"]);
    assert.equal(r.value("streamBubble"), null);
    assert.equal(r.value("speakNextReply"), false);
    assert.equal(r.value("wakeReplyText"), "");
    assert.equal(r.oldSpeech.cancelled, true);
    assert.equal(r.continuousStopped(), 1);
    assert.equal(r.speechCancelled(), 1);
    c.handleEvent({type: "error", data}); // repeated terminal events are safe
    assert.equal(r.continuousStopped(), 1);
    assert.equal(r.bubbles.length, 1);
    assert.equal(r.notifications.length, 0, "failure must not be reported as success");
  }
  for (const where of ["memory.recall", "skills.prompt"]) {
    const r = runtime();
    r.context.handleEvent({type: "error", data: {where, msg: "可恢复错误"}});
    assert.equal(r.value("taskBusy"), true);
    assert.equal(r.context.sendBtn.title, "停止任务");
    assert.equal(r.context.pendingDeliveries.size, 1);
    assert.equal(r.oldSpeech.cancelled, false);
    assert.equal(r.continuousStopped(), 0);
    assert.equal(r.speechCancelled(), 0);
  }
  console.log("terminal/config/service errors restore send state; recoverable errors keep running");
}
const screenshotMainSource = fs.readFileSync("main.js", "utf8");
if (!screenshotMainSource.includes("const isJpeg =") ||
    !screenshotMainSource.includes('const extension = isPng ? "png" : "jpg";')) {
  throw new Error("desktop screenshot viewer must accept JPEG screenshots");
}
if (!html.includes("let serviceFailed = false;")) {
  throw new Error("service-failed state flag missing (config error must not stick red)");
}
if (!html.includes("if (serviceFailed && ws && ws.readyState === WebSocket.OPEN)")) {
  throw new Error("refreshHealth should auto-recover service-failed state");
}
if (!html.includes("if (ws !== socket) return;")) throw new Error("stale WebSocket guard missing");
if (!/fromWake = false;\s+await cancelRecording\(\);/.test(html)) throw new Error("wake failure reset missing");
if (html.includes('id="approvalButton"') || html.includes('id="reasoningButton"') ||
    !html.includes('approval_mode: "full"') || !html.includes('reasoning_effort: getReasoningEffort()')) {
  throw new Error("chat permission/reasoning controls were not moved out of the composer");
}
if (!html.includes("width: 40px; height: 40px") ||
    !html.includes('closeSettingsModalButton.addEventListener("pointerdown"') ||
    !html.includes('closeSettingsModalButton.addEventListener("click", closeSettingsModal)')) {
  throw new Error("settings close button must have a generous pointer target and fast close path");
}
if (!html.includes("refreshHealth();")) throw new Error("health refresh missing");
if (!html.includes('case "settings_changed":') ||
    !html.includes("healthRefreshQueued = true") ||
    !html.includes("if (healthRefreshQueued)")) {
  throw new Error("live settings refresh or its in-flight request guard missing");
}
if (!html.includes('case "interjected":') ||
    !html.includes("已收到你的插话；当前") ||
    !html.includes("完成或超时返回后会处理") ||
    !html.includes('document.querySelectorAll(".confirm-box").forEach(box => box.remove())') ||
    !html.includes("function stopCurrentTask()") ||
    !html.includes("taskBusy && !hasActionInput()") ||
    !html.includes('stopping ? "停止任务" : taskBusy ? "插入新要求" : "发送"') ||
    !html.includes('sendBtn.disabled = !stopping && !connEl.classList.contains("ok")') ||
    html.includes('id="stop"')) {
  throw new Error("running task interjection or merged stop/send action missing");
}
const stopBody = html.slice(html.indexOf("async function stopCurrentTask()"), html.indexOf("sendBtn.onclick"));
if (!stopBody.includes('ws.send(JSON.stringify({ type: "stop" }))') ||
    stopBody.includes("ws.close(") ||
    stopBody.includes("userStopping")) {
  throw new Error("stop must preserve WebSocket and terminal errors must clear busy status");
}
if (!html.includes('case "resume_available":') ||
    !html.includes('showContinue("saved", d.summary)') ||
    !html.includes('任务：${escapeHtml(summary)}')) {
  throw new Error("persisted checkpoint summary or resume card handling missing");
}
if (!html.includes("window.bridge.setOpenAtLogin(d.open_at_login)")) {
  throw new Error("login item live sync missing");
}
const loginCalls = [];
const devApp = {isPackaged: false, setLoginItemSettings: value => loginCalls.push(value)};
if (applyLoginItemSetting(devApp, true).available !== false || loginCalls.length) {
  throw new Error("development mode must not register login item");
}
const packagedApp = {isPackaged: true, setLoginItemSettings: value => loginCalls.push(value)};
applyLoginItemSetting(
  packagedApp, true, {PORTABLE_EXECUTABLE_FILE: "C:\\Apps\\XiaoA.exe"}, "C:\\Temp\\app.exe",
);
if (loginCalls[0].openAtLogin !== true || loginCalls[0].path !== "C:\\Apps\\XiaoA.exe") {
  throw new Error("portable executable path was not registered");
}
applyLoginItemSetting(
  packagedApp, false, {PORTABLE_EXECUTABLE_FILE: "D:\\XiaoA.exe"}, "C:\\Temp\\app.exe",
);
if (loginCalls[1].openAtLogin !== false || loginCalls[1].path !== "D:\\XiaoA.exe") {
  throw new Error("login item was not removed with the current portable path");
}
if (!html.includes('id="modelButton"') || !html.includes('title="切换模型"')) {
  throw new Error("model switch tooltip missing");
}
const dotRules = [...html.matchAll(/header \.dot\s*\{([^}]+)\}/g)].map(match => match[1]);
const connRules = [...html.matchAll(/header \.conn\s*\{([^}]+)\}/g)].map(match => match[1]);
if (!dotRules.some(rule => rule.includes("border-radius: 50%"))) {
  throw new Error("header assistant icon must remain circular");
}
if (!connRules[0]?.includes("width: 32px") || !connRules[0]?.includes("height: 32px") ||
    !connRules[0]?.includes("display: inline-grid") ||
    !html.includes("header .conn { width: 28px; height: 28px; border-radius: 8px; }") ||
    !html.includes("width: 9px; height: 9px; border-radius: 50%")) {
  throw new Error("connection light size or spacing is incorrect");
}
if (!html.includes("header .right { gap: 4px; }")) {
  throw new Error("header control spacing is too wide");
}
const continuousRules = [...html.matchAll(/#continuousButton\s*\{([^}]+)\}/g)].map(match => match[1]);
if (!continuousRules.some(rule => /height:\s*30px/.test(rule))) {
  throw new Error("continuous conversation control height must match the 30px header capsules");
}
if (!html.includes("modelsLoaded = true;\n        restoreConversationModel();") ||
    !html.includes("function selectedModelUnavailable()")) {
  throw new Error("health refresh must restore the conversation model without replacing unavailable selections");
}
const optionDescCss = html.match(/\.menu-option \.option-desc\s*\{([^}]+)\}/)?.[1] || "";
if (/white-space:\s*nowrap/.test(optionDescCss) || !/white-space:\s*normal/.test(optionDescCss)) {
  throw new Error("menu descriptions must wrap");
}
const composerMenuCss = html.match(/\.composer-menu\s*\{([^}]+)\}/)?.[1] || "";
if (!/position:\s*fixed/.test(composerMenuCss) || !/width:\s*min\(280px,\s*calc\(100vw\s*-\s*24px\)\)/.test(composerMenuCss) ||
    !/max-width:\s*calc\(100vw\s*-\s*24px\)/.test(composerMenuCss) ||
    !/overflow-x:\s*hidden/.test(composerMenuCss) ||
    !html.includes("const buttonRect = button.getBoundingClientRect()") ||
    !html.includes("menu.style.left = `${left}px`") || !html.includes("menu.style.top = `${top}px`")) {
  throw new Error("composer menu overflow guard missing");
}
if (!html.includes("#modelMenu {\n      right: auto; bottom: auto; width: max-content; min-width: 0; padding: 4px;") ||
    !html.includes("white-space: nowrap; overflow: hidden; text-overflow: ellipsis; overflow-wrap: normal;")) {
  throw new Error("model menu width must follow its content");
}
const agentSource = fs.readFileSync("../backend/core/agent.py", "utf8");
const serverSource = fs.readFileSync("../backend/api/server.py", "utf8");
if (!agentSource.includes('approval_mode not in {"full", "safe"}') ||
    !agentSource.includes('approval_mode == "remote"')) {
  throw new Error("full access risk boundary missing");
}
if (!html.includes('d.reason === "service_stop"') ||
    !html.includes("即将停止云端服务，必须由你确认")) {
  throw new Error("remote service-stop confirmation wording missing");
}
if (!html.includes("controller.abort()")) throw new Error("health timeout missing");
if (!html.includes("有有效断点时，可在任务卡内继续；也可以补充纠正要求")) {
  throw new Error("stopped task resume guidance missing");
}
if (!html.includes("showContinue(d.reason, d.summary)") ||
    !html.includes('context_limit: "上下文已接近模型窗口上限，进度已保存"') ||
    !html.includes('spinning: "检测到连续重复操作，已停止防止原地打转"') ||
    html.includes('if (reason === "context_exhausted")') ||
    html.includes('这个任务做了 <b>${Number(stepsDone)')) {
  throw new Error("agent stop reasons are not presented separately");
}
if (!html.includes('class="confirm-args"') ||
    !html.includes("formatConfirmArguments(d.arguments)") ||
    !html.includes("typeof value === \"string\" ? truncateConfirmText(value, 200) : value") ||
    !html.includes("return truncateConfirmText(serialized, 2000)") ||
    !html.includes("…（已截断，完整内容见日志）")) {
  throw new Error("bounded confirmation arguments missing");
}
const headerDotSpan = html.match(/header \.dot span\s*\{([^}]+)\}/)?.[1] || "";
if (!html.includes('class="dot"><span>a</span></span>') ||
    !html.includes("font-size: 24px;") ||
    !headerDotSpan.includes("line-height: 1") ||
    !headerDotSpan.includes("transform: translate(1px, -2px)") ||
    !dotRules[0]?.includes("align-items: center") ||
    !dotRules[0]?.includes("justify-content: center")) {
  throw new Error("header assistant label must remain optically centered");
}
const ttsChunkStart = html.indexOf("const STREAM_TTS_MIN_CHARS");
const ttsChunkEnd = html.indexOf("    function feedStreamSpeech", ttsChunkStart);
const takeStreamSpeechChunk = new Function(
  `${html.slice(ttsChunkStart, ttsChunkEnd)}; return takeStreamSpeechChunk;`
)();
const shortGreeting = "我在,平哥。";
if (takeStreamSpeechChunk(shortGreeting, false) !== null) {
  throw new Error("short streaming TTS sentence must wait for more text");
}
const joinedSpeech = "我在,平哥。你想让我做什么？";
const joinedChunk = takeStreamSpeechChunk(joinedSpeech, false);
if (!joinedChunk || joinedChunk[0] !== joinedSpeech || joinedChunk[1] !== "") {
  throw new Error("short streaming TTS sentence must join the following sentence");
}
if (takeStreamSpeechChunk(shortGreeting, true)[0] !== shortGreeting) {
  throw new Error("streaming TTS flush must not drop a short final sentence");
}
if (!html.includes("ttsPrepareSlots: [Promise.resolve(), Promise.resolve()]") ||
    !html.includes("state.ttsPrepareIndex++ % state.ttsPrepareSlots.length") ||
    !html.includes("const preparedAudio = voiceCloud ? queueStreamTts(state, clean) : null") ||
    !html.includes("await speakPart(clean, state.seq, preparedAudio)")) {
  throw new Error("streaming TTS requests must be prefetched independently from playback");
}
const confirmHelpersStart = html.indexOf("const CONFIRM_TRUNCATION_NOTICE");
const confirmHelpersEnd = html.indexOf("    function showConfirm", confirmHelpersStart);
const formatConfirmArguments = new Function(
  `${html.slice(confirmHelpersStart, confirmHelpersEnd)}; return formatConfirmArguments;`
)();
const truncationNotice = "…（已截断，完整内容见日志）";
const singleValue = JSON.parse(formatConfirmArguments({script: "x".repeat(500)})).script;
if (singleValue.length > 200 || !singleValue.endsWith(truncationNotice)) {
  throw new Error("confirmation argument value is not capped at 200 characters");
}
const wholeValue = formatConfirmArguments(Object.fromEntries(
  Array.from({length: 30}, (_, index) => [`value${index}`, "x".repeat(190)])
));
if (wholeValue.length > 2000 || !wholeValue.endsWith(truncationNotice)) {
  throw new Error("confirmation arguments are not capped at 2000 characters");
}
const settingsHtml = fs.readFileSync("src/settings.html", "utf8").replace(/\r\n/g, "\n");
if (!/\.settings-dialog \[hidden\]\s*\{\s*display:\s*none;\s*\}/.test(settingsHtml)) {
if (!settingsHtml.includes("填写对话模型支持的上下文窗口大小；达到约75%时，小A会自动压缩较早的工具结果。")) {
  throw new Error("model context compression explanation must remain visible");
}
  throw new Error("confirmation-only dialogs must not show the hidden prompt label");
}
if (!settingsHtml.includes('scheduleCloudSelected = runtimeIsHub || (role.mode === "worker" && role.connection_configured === true);\n      updateScheduleFormUI();\n      settingsLoaded = true;\n      if (currentPage === "connections") loadChannels().catch(() => {});')) {
  throw new Error("connections page must load channel status after settings initialization");
}
if (settingsHtml.includes('id="context_window"') ||
    !settingsHtml.includes('className = "model-context-window"') ||
    !settingsHtml.includes('context_windows: Object.fromEntries') ||
    !settingsHtml.includes('context_window: provider.context_windows?.[model] || 128000')) {
  throw new Error("context window setting must be independent for each model");
}
if (!settingsHtml.includes('className = "model-reasoning"') ||
    !settingsHtml.includes('aria-label", "推理强度"') ||
    !settingsHtml.includes('localStorage.setItem(REASONING_EFFORT_KEY, value)')) {
  throw new Error("reasoning setting is not next to the model input");
}
if (!settingsHtml.includes('/api/models/discover') ||
    !settingsHtml.includes('model-actions') ||
    !settingsHtml.includes('选择上游模型并自动填入模型框') ||
    !settingsHtml.includes('点击下拉框即可填入') ||
    !settingsHtml.includes('input.value = picker.value')) {
  throw new Error("model discovery dropdown is missing");
}
if (!settingsHtml.includes('id="isolation_max_mb"') ||
    !settingsHtml.includes('isolation_max_mb: Number($("isolation_max_mb").value || 1024)') ||
    !settingsHtml.includes('d.isolation')) {
  throw new Error("isolation workspace setting is not wired");
}
const connectionsStart = settingsHtml.indexOf('<section class="settings-page" data-page="connections"');
const connectionsEnd = settingsHtml.indexOf('<section class="settings-page" data-page="system"', connectionsStart);
const connectionsHtml = settingsHtml.slice(connectionsStart, connectionsEnd);
if (connectionsStart < 0 || connectionsEnd < 0 ||
    (connectionsHtml.match(/<h2>微信 ClawBot<\/h2>/g) || []).length !== 1 ||
    (connectionsHtml.match(/<h2>飞书<\/h2>/g) || []).length !== 1 ||
    /<h2>(?:本机|云端)(?:微信|飞书)/.test(connectionsHtml) ||
    connectionsHtml.includes('id="save_local_feishu"') ||
    connectionsHtml.includes('id="fs_cloud_app_id"')) {
  throw new Error("connections page must render one shared card for each messaging channel");
}
if (!settingsHtml.includes('return cloudRuntimeAvailable ? "cloud" : "local"') ||
    !settingsHtml.includes('const path = cloud ? "/api/cloud/weixin" : "/api/weixin/status"') ||
    !settingsHtml.includes('if (channelTarget() !== "cloud")') ||
    !settingsHtml.includes('fetch(API + "/api/cloud/feishu"') ||
    !settingsHtml.includes('feishu_allowed_open_ids: feishuAllowedIds.join(", ")')) {
  throw new Error("shared messaging cards must route operations to local or cloud runtime");
}
const weixinCard = connectionsHtml.slice(connectionsHtml.indexOf('<h2>微信 ClawBot</h2>'), connectionsHtml.indexOf('<h2>飞书</h2>'));
if (!weixinCard.includes('已连接账号 · 已授权用户') ||
    weixinCard.includes('id="weixin_authorized_list"') ||
    weixinCard.includes('id="weixin_allowed"') ||
    !settingsHtml.includes('className = "channel-remote-toggle"') ||
    !settingsHtml.includes('channel-account-remove')) {
  throw new Error("WeChat accounts must be the single authorization list with capsule controls");
}
if (!settingsHtml.includes('saveActions.hidden = ["connections", "capabilities", "schedule"].includes(currentPage)') ||
    !settingsHtml.includes('$("save_feishu").onclick = async () =>')) {
  throw new Error("connections page must use its channel-specific save action");
}
const systemStart = settingsHtml.indexOf('<section class="settings-page" data-page="system"');
const systemEnd = settingsHtml.indexOf('</section>', systemStart);
const systemHtml = settingsHtml.slice(systemStart, systemEnd);
const systemOrder = ["开机自启", "隔离工作区", "网页访问密码", "远程遥控锁定", "电脑控制", "电脑操作记录"];
if (systemStart < 0 || systemEnd < 0 ||
    systemOrder.some((title, index) => systemHtml.indexOf(`<h2>${title}</h2>`) < 0 ||
      (index > 0 && systemHtml.indexOf(`<h2>${title}</h2>`) <= systemHtml.indexOf(`<h2>${systemOrder[index - 1]}</h2>`))) ||
    !settingsHtml.includes('computerControl.parentNode.insertBefore(saveActions, computerControl)')) {
  throw new Error("system page order or save action placement is incorrect");
}
if (!serverSource.includes('approval_mode="full"')) {
  throw new Error("cloud channel does not use full access");
}
if (!settingsHtml.includes("refreshingChannelLock") ||
    !settingsHtml.includes("setInterval(() => loadChannelLock().catch(() => {}), 30000)") ||
    !settingsHtml.includes('d.locked === null') ||
    !settingsHtml.includes('currentPage !== "system"') ||
    !settingsHtml.includes('if (currentPage === "system")')) {
  throw new Error("channel lock polling guard missing");
}
if (!settingsHtml.includes("在飞书或微信发一句") ||
    !settingsHtml.includes("盗号者在飞书或微信发")) {
  throw new Error("channel lock hint must mention both Feishu and WeChat");
}
if (!settingsHtml.includes("env_managed_fields") ||
    !settingsHtml.includes("input.disabled = Boolean(envName)") ||
    !settingsHtml.includes("当前由环境变量 ${envName} 接管，此处填写不生效") ||
    !settingsHtml.includes('id="security_state"') ||
    !settingsHtml.includes("d.ok && d.warning")) {
  throw new Error("environment-managed settings feedback missing");
}
if (!settingsHtml.includes('/api/cloud/runtime') ||
    !settingsHtml.includes('d.cloud_synced ? "已保存并同步到云端 ✓"')) {
  throw new Error("model and voice cloud sync flow missing");
}
const mainJs = fs.readFileSync("main.js", "utf8").replace(/\r\n/g, "\n");
if (!packageJson.build?.files?.includes("float_alert.js")) {
  throw new Error("Electron package must include float_alert.js");
}
if (!html.includes('window.bridge.notifyFloat("approval")') ||
    !html.includes('window.bridge.notifyFloat("complete")') ||
    !html.includes('window.bridge.notifyFloat("incoming")') ||
    !html.includes('window.bridge.clearFloatAlert("approval")')) {
  throw new Error("desktop alert lifecycle is incomplete");
}
const toggleChatStart = mainJs.indexOf('ipcMain.on("toggle-chat"');
const toggleChatEnd = mainJs.indexOf('ipcMain.on("hide-chat"', toggleChatStart);
if (!mainJs.slice(toggleChatStart, toggleChatEnd).includes("clearFloatAlert();")) {
  throw new Error("opening the chat must stop the persistent approval alert");
}
if (!packageJson.build?.files?.includes("float_position.js")) {
  throw new Error("Electron package must include float_position.js");
}
if (!mainJs.includes('chatWin.on("close", (e) => {') ||
    !mainJs.includes('quitting = true;\n    app.quit();') ||
    !mainJs.includes('floatWin.on("close", () => {') ||
    !mainJs.includes('XIAOA_PARENT_PID: String(process.pid)') ||
    !mainJs.includes('XIAOA_PARENT_START_TIME')) {
  throw new Error("Electron close paths must terminate the backend");
}
if (!settingsHtml.includes('/api/extensions/inspect') ||
    !settingsHtml.includes('settingsConfirm("已显示第三方扩展内容。确认信任该来源并安装？"') ||
    !settingsHtml.includes('confirm_install: true') ||
    !settingsHtml.includes('$("extension_review_content").textContent = inspected.untrusted_content') ||
    settingsHtml.indexOf('/api/extensions/inspect') > settingsHtml.indexOf('/api/extensions/install')) {
  throw new Error("extension review must precede explicit install confirmation");
}
if (settingsHtml.includes('id="role_mode"') ||
    settingsHtml.includes('id="connection_string"') ||
    settingsHtml.includes('id="hub_token"') ||
    settingsHtml.includes('高级：手动填写域名') ||
    settingsHtml.includes('/api/hub/parse') ||
    settingsHtml.includes('/api/hub/test') ||
    settingsHtml.includes('id="save_connection_string"') ||
    settingsHtml.includes('id="test_hub_manual"') ||
    !settingsHtml.includes('id="connect_cloud"') ||
    !settingsHtml.includes('/api/cloud/deploy') ||
    !settingsHtml.includes('id="connection_preview"') ||
    !settingsHtml.includes('d.restart_required')) {
  throw new Error("cloud connection should use the simplified deployment flow");
}
if (!serverSource.includes('@app.post("/api/hub/test")') ||
    !serverSource.includes('@app.post("/api/hub/parse")') ||
    !serverSource.includes('parse_connection_string')) {
  throw new Error("legacy cloud connection compatibility was removed from the backend");
}
const startBat = fs.readFileSync("../start.bat");
const startBatText = startBat.toString("utf8");
if (/(?<!\r)\n/.test(startBatText) || /^\s*rem\b.*[^\x00-\x7F]/im.test(startBatText)) {
  throw new Error("start.bat must use CRLF and ASCII-only REM comments");
}
const floatHtml = fs.readFileSync("src/float.html", "utf8");
const mainSource = fs.readFileSync("main.js", "utf8");
// 只验结构与行为,不锁字号/字形/位移这类会随换图标变动的字面量。
if (!/id="avatar"[^>]*>/.test(floatHtml) || !/<span>[^<]+<\/span>/.test(floatHtml)) {
  throw new Error("float avatar label missing");
}
if (!floatHtml.includes("align-items: center") || !floatHtml.includes("justify-content: center") ||
    !floatHtml.includes("line-height: 1")) {
  throw new Error("float label centering relies on flex + line-height");
}
// Allow a small optical correction, but reject a large vertical glyph shift.
const glyphOffset = floatHtml.match(/\.avatar\s*>\s*span\s*\{\s*transform\s*:\s*translateY\((-?[\d.]+)em\);\s*\}/)?.[1];
if (glyphOffset === undefined || Number(glyphOffset) > 0 || Number(glyphOffset) < -0.1) {
  throw new Error("float label optical offset is outside the centered range");
}
// 尺寸的单一来源是 float_position.js 的常量,main.js 从那里取,CSS 必须与之一致。
const floatPosition = require("./float_position.js");
if (floatPosition.FLOAT_WIDTH !== 88 || floatPosition.FLOAT_HEIGHT !== 88) {
  throw new Error("float size constants changed without updating the test");
}
const circleShape = floatPosition.createCircleShape();
if (circleShape.length !== 72 || circleShape.some(rect => rect.height !== 1)) {
  throw new Error("float circle shape must contain one scanline per shaped row");
}
for (let y = 0; y < 88; y += 1) {
  for (let x = 0; x < 88; x += 1) {
    const dx = x + 0.5 - 44;
    const dy = y + 0.5 - 44;
    const insideCircle = dx * dx + dy * dy <= 36 * 36;
    const covered = circleShape.some(rect =>
      x >= rect.x && x < rect.x + rect.width && y >= rect.y && y < rect.y + rect.height
    );
    if (covered !== insideCircle) throw new Error(`float circle shape mismatch at ${x},${y}`);
  }
}
const fullyVisible = floatPosition.clampFloatPosition(1900, 200, {x: 0, y: 0, width: 1920, height: 1040});
if (fullyVisible.x !== 1832 || fullyVisible.y !== 200) {
  throw new Error("float avatar must remain fully reachable inside the screen");
}
if (!floatHtml.includes(`width: ${floatPosition.FLOAT_WIDTH}px; height: ${floatPosition.FLOAT_HEIGHT}px`) ||
    !floatHtml.includes("position: absolute; inset: 12px")) {
  throw new Error("float CSS size must match float_position.js constants");
}
// 拖动必须过边界收敛,否则球能被拖出屏幕再也点不到。
if (!mainSource.includes("clampFloatPosition") || !mainSource.includes("getDisplayNearestPoint")) {
  throw new Error("float drag must clamp to the display work area");
}
const preloadSource = fs.readFileSync("preload.js", "utf8");
if (!mainSource.includes("floatWin.setShape(createCircleShape())")) {
  throw new Error("native float window shape missing");
}
if (mainSource.includes("floatWin.setIgnoreMouseEvents")) throw new Error("float must use its native shape, not mouse passthrough");
for (const obsolete of ["float-hit-test", "setFloatHitTest", "restoreFloatHitTest"]) {
  if (`${mainSource}\n${preloadSource}\n${floatHtml}`.includes(obsolete)) {
    throw new Error(`obsolete float hit-test mechanism remains: ${obsolete}`);
  }
}
if (!floatHtml.includes("avatar.setPointerCapture(e.pointerId)")) {
  throw new Error("fast float drag capture missing");
}
const floatSource = [...floatHtml.matchAll(/<script(?:\s[^>]*)?>([\s\S]*?)<\/script>/g)].find(match => match[1].includes("avatar.setPointerCapture"))[1];
const windowListeners = {};
const avatarListeners = {};
const dragDeltas = [];
let capturedPointer = null;
let toggleCount = 0;
const classList = {toggle() {}};
const avatar = {
  classList,
  addEventListener(type, listener) { avatarListeners[type] = listener; },
  setPointerCapture(pointerId) { capturedPointer = pointerId; },
  // 88x88 窗口内 inset 12px => 12..76,圆心 (44,44),半径 32
  getBoundingClientRect() { return {left: 12, top: 12, right: 76, bottom: 76, width: 64, height: 64}; },
};
const mockWindow = {
  bridge: {
    dragBy(dx, dy) { dragDeltas.push([dx, dy]); },
    clearFloatAlert() {},
    toggleChat() { toggleCount += 1; },
  },
  addEventListener(type, listener) {
    (windowListeners[type] ||= []).push(listener);
  },
};
const mockDocument = {
  querySelector(selector) { return selector === ".ring" ? {classList} : {}; },
  getElementById() { return avatar; },
};
new Function("window", "document", floatSource)(mockWindow, mockDocument);
const dispatchWindow = (type, event) => {
  for (const listener of windowListeners[type] || []) listener(event);
};
avatarListeners.pointerdown({
  button: 0, pointerId: 7, screenX: 60, screenY: 60, preventDefault() {},
});
dispatchWindow("pointermove", {clientX: 180, clientY: 180, screenX: 180, screenY: 180});
if (capturedPointer !== 7 || dragDeltas.length !== 1) {
  throw new Error("fast float drag was dropped outside the avatar");
}
dispatchWindow("pointerup", {clientX: 180, clientY: 180});
avatarListeners.pointerdown({
  button: 0, pointerId: 8, screenX: 60, screenY: 60, preventDefault() {},
});
dispatchWindow("pointerup", {clientX: 44, clientY: 44});
if (toggleCount !== 1) {
  throw new Error("float click did not toggle chat");
}
(async () => {
  // Wake response follows the saved provider, voice and speed, including first use.
  const hiStart = html.indexOf("let cachedHi = null");
  const hiEnd = html.indexOf("async function playHi", hiStart);
  if (hiStart < 0 || hiEnd < 0) throw new Error("voice cache signature logic missing");
  const makeRefresh = new Function(
    "fetch", "ttsReliable", "voiceCloud", "API",
    `${html.slice(hiStart, hiEnd)}; return refreshCachedHi;`,
  );

  async function scenario(initial, changed) {
    let synth = 0;
    let current = initial;
    const fakeFetch = async () => ({ ok: true, json: async () => ({tts_enabled: true, speed: 0, ...current}) });
    const fakeTts = async () => { synth += 1; return "audio"; };
    const refresh = makeRefresh(fakeFetch, fakeTts, true, "http://x");
    await refresh();        // 首次合成
    await refresh();        // 同签名不合成
    const afterSame = synth;
    current = changed;
    await refresh();        // 签名变了重新合成
    return { afterSame, final: synth };
  }

  const result = await scenario({ voice: 601003 }, { voice: 601004 });
  if (result.afterSame !== 1 || result.final !== 2) {
    throw new Error("Tencent voice change did not refresh cached audio");
  }
  const unchanged = await scenario({ voice: 601003 }, { voice: 601003 });
  if (unchanged.afterSame !== 1 || unchanged.final !== 1) {
    throw new Error("unchanged Tencent voice unexpectedly regenerated audio");
  }

  const baiduZero = await scenario({ provider: "baidu", voice: 0 }, { provider: "baidu", voice: 4189 });
  if (baiduZero.afterSame !== 1 || baiduZero.final !== 2) {
    throw new Error("Baidu voice zero must be cached and refreshed correctly");
  }
  const switchedProvider = await scenario({ provider: "tencent", voice: 1, speed: 5 }, { provider: "baidu", voice: 1, speed: 5 });
  if (switchedProvider.afterSame !== 1 || switchedProvider.final !== 2) {
    throw new Error("provider change must refresh audio even with the same voice number");
  }
  const assertVoice = require("node:assert/strict");
  const playEnd = html.indexOf("function onWakeTriggered", hiStart);
  const makeWake = new Function(
    "fetch", "ttsReliable", "voiceCloud", "API", "window", "Audio", "SpeechSynthesisUtterance", "voiceHint",
    `${html.slice(hiStart, playEnd)}; return {refreshCachedHi, playHi};`,
  );
  const deferred = () => {
    let resolve;
    const promise = new Promise(done => { resolve = done; });
    return {promise, resolve};
  };
  let currentVoice = {voice: 601003, speed: 0, tts_enabled: true};
  const pending = [], requestedVoices = [], played = [];
  let systemSpeech = 0, failStatus = false, failPlayback = false;
  const hint = {};
  const wake = makeWake(
    async () => {
      if (failStatus) throw new Error("offline");
      return {ok: true, json: async () => ({...currentVoice})};
    },
    async (text, voice) => {
      assertVoice.equal(text, "我在");
      requestedVoices.push(voice);
      const next = deferred(); pending.push(next); return next.promise;
    },
    false, "http://x",
    {speechSynthesis: {cancel() {}, speak() { systemSpeech++; }}},
    function(source) { played.push(source); this.play = async () => { if (failPlayback) throw new Error("playback unavailable"); }; this.pause = () => {}; },
    function() {}, hint,
  );
  const tick = () => new Promise(setImmediate);
  const preload = wake.refreshCachedHi(); await tick();
  const firstWake = wake.playHi(); await tick();
  assertVoice.deepEqual(requestedVoices, [601003], "preload and first wake share the same synthesis");
  currentVoice = {voice: 601000, speed: 0, tts_enabled: true};
  const changedWake = wake.playHi(); await tick();
  pending[1].resolve("new-voice"); await changedWake;
  pending[0].resolve("old-voice"); await Promise.all([preload, firstWake]);
  assertVoice.deepEqual(played, ["data:audio/mpeg;base64,new-voice"], "late old audio must never play");
  assertVoice.equal(await wake.refreshCachedHi(), "new-voice", "late old audio must not overwrite the cache");
  assertVoice.equal(systemSpeech, 0, "first wake uses saved cloud voice even before health loads");
  currentVoice.speed = 1;
  const faster = wake.refreshCachedHi(); await tick();
  pending[2].resolve("faster-voice"); assertVoice.equal(await faster, "faster-voice");
  failStatus = true; await wake.playHi();
  assertVoice.equal(systemSpeech, 0, "status failure must not silently select a system voice");
  failStatus = false; currentVoice.tts_enabled = false; await wake.playHi();
  assertVoice.equal(systemSpeech, 0, "wake response must never silently switch to the system voice");
  assertVoice.match(hint.textContent, /配置并保存语音服务/, "missing speech configuration must be explained");
  currentVoice.tts_enabled = true;
  const failedAudio = wake.refreshCachedHi(); await tick(); pending[3].resolve(null); await failedAudio;
  const retryAudio = wake.refreshCachedHi(); await tick(); pending[4].resolve("retry-voice");
  assertVoice.equal(await retryAudio, "retry-voice", "failed synthesis must be retryable");

  failPlayback = true;
  await wake.playHi();
  assertVoice.equal(systemSpeech, 0, "playback failure must not change the voice");
  assertVoice.match(hint.textContent, /无法确认或合成已设音色/, "playback failure must be visible");
  console.log("wake response: first-use preload, provider/voice/speed changes, retry and no system fallback passed");
  console.log("inline script syntax passed");
})().catch((error) => {
  console.error("FAIL:", error.message);
  process.exit(1);
});

// Execute the actual settings functions, not a second implementation of their rules.
(async () => {
  const assert = require("node:assert/strict");
  for (const removed of ["feishu_manual_authorization", "fs_allowed", "save_feishu_allowlist", "saveFeishuAllowlist", "fs_last_open_id", "copy_fs_open_id", "手动添加用户（备用）"]) {
    assert(!settingsHtml.includes(removed), `${removed} must be removed, not just hidden`);
  }
  assert(!settingsHtml.includes("最近已授权用户："), "do not repeat the authorized user list as another action");
  const elements = new Map();
  const makeElement = () => ({
    dataset: {}, hidden: false, value: "", children: [],
    get textContent() { return this.text || ""; },
    set textContent(value) { this.text = value; this.children = []; },
    append(...items) { this.children.push(...items); },
  });
  const $ = id => {
    if (!elements.has(id)) elements.set(id, makeElement());
    return elements.get(id);
  };
  const doc = { hidden: false, createElement: makeElement };
  const paths = [];
  let reply = { last_open_id: "ou_local", pending_open_ids: ["ou_local"] };
  let pending;
  const fetch = async url => {
    paths.push(url);
    if (pending) return await pending;
    return { ok: true, json: async () => reply };
  };
  const source = settingsHtml.slice(settingsHtml.indexOf("    function showRecentFeishuOpenId("),
    settingsHtml.indexOf("    const REASONING_EFFORT_KEY"));
  assert(source.includes("async function loadRecentFeishuOpenId"));
  const authorizations = [];
  const functions = new Function("$", "fetch", "document", "API", "authorizeRecentFeishu", `
    let currentPage = "connections", cloudRuntimeAvailable = false, feishuInitialAppId = "";
    ${source}
    return { show: showRecentFeishuOpenId, refresh: loadRecentFeishuOpenId, load: loadFeishu,
      setCloud: value => { cloudRuntimeAvailable = value; }, setPage: value => { currentPage = value; } };
  `)($, fetch, doc, "http://local", id => authorizations.push(id));
  functions.show({ pending_open_ids: ["ou_user_a", "ou_user_b"] });
  assert(!$('feishu_pending_panel').hidden, "new users have a visible direct authorization panel");
  assert.equal($('feishu_pending_list').children.length, 2, "each pending user gets an authorization button");
  $('feishu_pending_list').children[1].children[1].onclick();
  assert.deepEqual(authorizations, ["ou_user_b"], "authorizing one user must target that user's ID");
  functions.show({ last_open_id: "ou_user_b", pending_open_ids: ["ou_user_a"] });
  assert.equal($('feishu_pending_list').children.length, 1, "authorized users leave the pending list");
  functions.show({ last_open_id: "ou_user_b", pending_open_ids: [] });
  assert($('feishu_pending_panel').hidden, "hide the pending panel after authorization is complete");
  functions.show({ pending_available: true });
  assert(!$('feishu_pending_panel').hidden && !$('authorize_recent_feishu').hidden, "older servers retain direct authorization");
  functions.show(null); assert($("feishu_pending_status").textContent.includes("正在读取"));
  functions.show({}); assert($("feishu_pending_status").textContent.includes("当前版本未提供"));
  functions.show({ last_open_id: "" }); assert($("feishu_pending_status").hidden);
  functions.show({ recent_status: "unavailable" }); assert($("feishu_pending_status").textContent.includes("读取失败"));
  assert(!$("feishu_pending_status").hidden, "a failed read remains visible without the manual UI");
  await functions.refresh();
  assert.equal(paths.at(-1), "http://local/api/feishu/recent");
  assert.equal($("feishu_pending_list").children[0].children[0].textContent, "ou_••••cal");
  assert($("feishu_pending_status").hidden, "successful refresh clears the error");
  functions.setCloud(true); reply = { connected: true, last_open_id: "ou_cloud", pending_open_ids: ["ou_cloud"], app_id: "cloud-app" };
  await functions.load();
  assert.equal(paths.at(-1), "http://local/api/cloud/feishu");
  assert.equal($("feishu_pending_list").children[0].children[0].textContent, "ou_••••oud");
  assert.equal($("fs_app_id").value, "cloud-app");
  // Cloud reachability is not proof that a Feishu channel is configured or delivering messages.
  for (const [appId, hasSecret] of [["", false], ["cloud-app", false], ["", true], ["cloud-app", true]]) {
    reply = { connected: true, app_id: appId, app_secret_configured: hasSecret };
    await functions.load();
    assert.equal($("fs_status").textContent, appId && hasSecret
      ? "云端飞书已配置；实际收发状态以消息验证为准"
      : "云端飞书未配置完整；请填写 App ID 和 App Secret 后保存飞书");
    assert.equal($("fs_app_secret").value, "", "status reads must not expose the cloud secret");
  }
  reply = { connected: false, error: "offline" }; await functions.refresh();
  assert($("feishu_pending_status").textContent.includes("读取失败"));
  assert.equal($("feishu_pending_list").children.length, 0);
  doc.hidden = true; const before = paths.length; await functions.refresh(); assert.equal(paths.length, before);
  doc.hidden = false; functions.setPage("models"); await functions.refresh(); assert.equal(paths.length, before);
  functions.setPage("connections");
  let finish;
  pending = new Promise(resolve => { finish = resolve; });
  const loading = functions.refresh(); await functions.refresh(); assert.equal(paths.length, before + 1);
  functions.setCloud(false); functions.show({ last_open_id: "ou_local", pending_open_ids: ["ou_local"] });
  finish({ ok: true, json: async () => ({ connected: true, last_open_id: "ou_cloud", pending_open_ids: ["ou_cloud"] }) });
  await loading; assert.equal($("feishu_pending_list").children[0].children[0].textContent, "ou_••••cal", "a stale cloud response must not replace the local record");
  functions.setCloud(true);
  pending = new Promise(resolve => { finish = resolve; });
  const initialLoad = functions.load();
  functions.setCloud(false); functions.show({ last_open_id: "ou_local", pending_open_ids: ["ou_local"] });
  $("fs_app_id").value = "local-app";
  finish({ ok: true, json: async () => ({ connected: true, last_open_id: "ou_cloud", pending_open_ids: ["ou_cloud"], app_id: "cloud-app" }) });
  await initialLoad;
  assert.equal($("feishu_pending_list").children[0].children[0].textContent, "ou_••••cal", "a stale initial cloud load must not replace the local record");
  assert.equal($("fs_app_id").value, "local-app", "a stale initial cloud load must not replace the local app");
  functions.setCloud(true);
  pending = new Promise(resolve => { finish = resolve; });
  const failedLoad = functions.load();
  functions.setCloud(false); functions.show({ last_open_id: "ou_local", pending_open_ids: ["ou_local"] });
  finish({ ok: false, json: async () => { throw new Error("offline"); } });
  await failedLoad;
  assert.equal($("feishu_pending_list").children[0].children[0].textContent, "ou_••••cal", "a stale initial load error must not replace the local record");
  assert(!settingsHtml.includes("尚未收到已授权私聊"));
  console.log("Feishu recent status: source routing, direct authorization, errors and polling passed");
})().catch(error => { console.error(error); process.exitCode = 1; });

// A confirmation belongs to the originating conversation, not the selected tab.
{
  const assert = require("node:assert/strict");
  const showConfirm = html.slice(html.indexOf("    function showConfirm(d)"), html.indexOf("    function setBusy(busy)"));
  const sent = [], boxes = [];
  const makeBox = () => ({
    controls: {},
    querySelector(selector) { return this.controls[selector] ||= {}; },
    remove() {},
  });
  const run = new Function("document", "ws", "window", "log", "escapeHtml", "formatConfirmArguments", `
    let currentConversationId = "origin";
    ${showConfirm}
    showConfirm({name: "dangerous_write", arguments: {}, approval_scope: ["task"]});
    showConfirm({name: "dangerous_write", arguments: {}, approval_scope: ["task"], conversation_id: "explicit"});
    currentConversationId = "another-tab";
  `);
  run({createElement: makeBox}, {send: payload => sent.push(JSON.parse(payload))}, {},
      {appendChild: box => boxes.push(box)}, value => value, JSON.stringify);
  assert.match(boxes[0].innerHTML, /允许本次任务/);
  assert.match(boxes[0].innerHTML, /拒绝本次任务/);
  boxes[0].querySelector(".btn-allow").onclick();
  boxes[1].querySelector(".btn-deny").onclick();
  assert.deepEqual(sent, [
    {type: "confirm", allow: true, conversation_id: "origin"},
    {type: "confirm", allow: false, conversation_id: "explicit"},
  ]);
}

(async () => {
  const assert = require("node:assert/strict");
  // Exercise the real provider builders; no real model API calls or user settings writes.
  const element = tag => ({
    tag, children: [], dataset: {}, value: "", disabled: false,
    setAttribute(name, value) { this[name] = value; },
    append(...children) { for (const child of children) { child.parent = this; this.children.push(child); } },
    appendChild(child) { this.append(child); },
    add(child) { this.append(child); }, focus() {},
    remove() { this.parent.children = this.parent.children.filter(child => child !== this); this.parent = null; },
    matches(selector) { return selector.startsWith(".") && (this.className || "").split(/\s+/).includes(selector.slice(1)); },
    closest(selector) { return this.matches(selector) ? this : this.parent?.closest(selector); },
    querySelectorAll(selector) {
      return this.children.flatMap(child => [...(child.matches(selector) ? [child] : []), ...child.querySelectorAll(selector)]);
    },
    querySelector(selector) { return this.querySelectorAll(selector)[0]; },
  });
  const root = element("div"), requests = [];
  let release, hold = false;
  const fetch = async (url, options) => {
    const body = JSON.parse(options.body); requests.push({url, body});
    if (hold) { hold = false; await new Promise(resolve => { release = resolve; }); }
    if (body.model === "network-error") throw new Error("offline");
    return {json: async () => ({ok: body.model !== "bad", error: "unavailable", latency_ms: 12})};
  };
  const source = settingsHtml.slice(settingsHtml.indexOf("    function fillModelPicker("), settingsHtml.indexOf("    function setProviders("));
  const addProvider = new Function("document", "$", "Option", "fetch", "API", "setManagedInput", "REASONING_OPTIONS", "getReasoningEffort", "setReasoningEffort",
    `let providerSeq = 0; ${source}; return addProvider;`
  )({createElement: element}, () => root, function(text, value) { return { ...element("option"), textContent: text, value }; },
    fetch, "http://local", () => {}, [["high", "推理：高"]], () => "high", () => {});
  addProvider({id: "one", protocol: "responses", base_url: "http://mock/v1", models: ["good", "bad", "network-error", "after-error"]});
  addProvider({id: "two", base_url: "http://other/v1", models: ["other"]});
  const first = root.children[0], rows = first.querySelectorAll(".model-row");
  assert.equal(first.querySelector(".provider-protocol").value, "responses");
  const actions = first.querySelector(".model-actions"), test = actions.querySelector(".test-model");
  assert.deepEqual(actions.children.filter(child => child.tag === "button").map(child => child.textContent), ["从上游探测模型", "+ 添加模型", "测试"]);
  assert.equal(first.querySelectorAll(".test-model").length, 1, "one test button per provider");
  assert(rows.every(row => row.querySelectorAll(".test-model").length === 0), "model input rows must not contain test buttons");
  hold = true;
  const running = test.onclick();
  assert.equal(test.disabled, true); assert.equal(test.textContent, "测试中");
  await test.onclick(); assert.equal(requests.length, 1, "repeated clicks cannot duplicate tests");
  release(); await running;
  assert.deepEqual(requests.map(request => request.body.model), ["good", "bad", "network-error", "after-error"]);
  assert(requests.every(request => request.body.provider_id === "one" && request.url.endsWith("/api/models/test")), "test only this provider");
  assert(requests.every(request => request.body.protocol === "responses"), "provider tests must retain Responses protocol");
  assert.equal(rows[0].querySelector(".model-test-status").textContent, "模型可用 · 12ms");
  assert.match(rows[1].querySelector(".model-test-status").textContent, /不可用/);
  assert.match(rows[2].querySelector(".model-test-status").textContent, /offline/);
  assert.equal(test.disabled, false); assert.equal(test.textContent, "测试");
  rows[0].querySelector(".model-option").value = "";
  await test.onclick(); assert.equal(requests.length, 7, "empty model is validated without a request");
  assert.match(rows[0].querySelector(".model-test-status").textContent, /请先填写/);
  first.querySelector(".provider-base-url").value = "";
  await test.onclick(); assert.equal(requests.length, 7, "missing Base URL must not send requests");
  await root.children[1].querySelector(".test-model").onclick();
  assert.equal(requests.at(-1).body.provider_id, "two");
  assert.equal(requests.at(-1).body.model, "other");
  for (const row of rows) row.remove();
  await test.onclick(); assert.match(actions.querySelector(".model-discovery-status").textContent, /请先添加模型/);
  actions.querySelector(".add-model").onclick();
  assert.equal(first.querySelectorAll(".model-row").length, 1, "add model still works");
  assert(settingsHtml.includes('grid-template-columns: minmax(0, 1.6fr) repeat(2, minmax(0, 1fr))'), "mobile action buttons must stay in three columns");
  console.log("model action layout and per-provider batch tests passed");
})().catch(error => { console.error(error); process.exitCode = 1; });


(async () => {
  const vm = require("node:vm"), assert = require("node:assert/strict");
  const elements = new Map();
  const $ = id => {
    if (!elements.has(id)) elements.set(id, {value: "", disabled: false, options: [],
      set textContent(_) { this.options = []; this.value = ""; }, append(option) { this.options.push(option); }});
    return elements.get(id);
  };
  let recipients = [];
  const context = vm.createContext({$, API: "", scheduleCloudSelected: false,
    document: {querySelectorAll: () => []},
    Option: function(text, value) { this.text = text; this.value = value; this.dataset = {}; },
    fetch: async () => ({ok: true, json: async () => ({ok: true, recipients})}),
  });
  const start = settingsHtml.indexOf("    function scheduleForm()");
  const end = settingsHtml.indexOf("    async function loadSchedule()", start);
  const maskStart = settingsHtml.lastIndexOf("    function maskIdentifier(");
  const maskEnd = settingsHtml.indexOf("    let feishuRecentLoading", maskStart);
  vm.runInContext(settingsHtml.slice(maskStart, maskEnd) + settingsHtml.slice(start, end) + "\nglobalThis.test = {loadScheduleRecipients, scheduleForm};", context);
  for (const channel of ["weixin", "feishu"]) {
    $("sched_channel").value = channel;
    recipients = [
      {number: "2", remark: "XiaoA", target: "abc-first-xyz", account_id: channel === "weixin" ? "bot-a" : "", receive_id_type: channel === "weixin" ? "chat_id" : "open_id", user_id: "abc-first-xyz", connected: true},
      {number: "3.1", remark: "张三 <b>备注</b>", target: "abc-second-xyz", account_id: channel === "weixin" ? "bot-b" : "", receive_id_type: "open_id", user_id: "abc-second-xyz", connected: false},
      {target: "abc-third-xyz", account_id: "", connected: true},
    ];
    await context.test.loadScheduleRecipients();
    const options = $("sched_recipient").options;
    assert.match(options[1].text, /^2\. XiaoA · abc••••xyz/);
    assert.match(options[2].text, /^3\.1\. 张三 <b>备注<\/b>.*（未连接）$/);
    assert.equal(options[2].disabled, true);
    assert.match(options[3].text, /^3\. 未备注/);
    assert.match(options[1].title, /abc-first-xyz/);
    $("sched_recipient").value = "1";
    const body = context.test.scheduleForm();
    assert.equal(body.channel_target, "abc-first-xyz");
    assert.equal(body.channel_account_id, recipients[0].account_id);
    assert.equal(body.channel_user_id, "abc-first-xyz");
    assert.equal(body.channel_receive_id_type, recipients[0].receive_id_type);
    recipients = [recipients[0]];
    await context.test.loadScheduleRecipients();
    assert.equal($("sched_recipient").value, "1", "single connected recipient remains auto-selected");
  }
  console.log("schedule recipient labels: channel numbering, remarks, missing remarks, offline users and exact delivery identity passed");
})().catch(error => { console.error(error); process.exitCode = 1; });


(async () => {
  const vm = require("node:vm"), assert = require("node:assert/strict");
  const element = () => ({value: "", disabled: false, dataset: {}, children: [], _text: "",
    set textContent(value) { this._text = value; this.children = []; }, get textContent() { return this._text; },
    append(...children) { this.children.push(...children); }, focus() {},
    querySelectorAll() { return this.children.flatMap(label => label.children || []).filter(x => x.type === "checkbox" && x.checked); },
  });
  const nodes = new Map(), $ = id => { if (!nodes.has(id)) nodes.set(id, element()); return nodes.get(id); };
  let answer = null, target = "local", success = true, confirm = true;
  const calls = [];
  const accounts = [
    {identity: "user:person-a", source: "weixin", number: 1, user_id: "person-a", remark: "项目负责人"},
    {identity: "feishu:cli_test:person-b", source: "feishu", number: 1, user_id: "person-b", remark: "项目负责人"},
  ];
  const library = {id: "default", name: "公共资料", call_name: "项目甲资料", owner: "user:person-a", enabled: true, count: 3,
    members: [{identity: "user:person-a", permission: "admin"}, {identity: "feishu:cli_test:person-b", permission: "query"}, {identity: "user:offline", permission: "query"}]};
  const deletedLibrary = {id: "deleted", name: "旧项目", call_name: "旧项目资料", owner: "user:person-a", enabled: false, members: []};
  const context = vm.createContext({$, API: "", channelTarget: () => target,
    sharedMemorySettings: {}, sharedLibraries: [], sharedLibraryAccounts: [], sharedLibrariesSupported: false,
    maskIdentifier: value => value, document: {createElement: element, createTextNode: text => ({textContent: text})},
    settingsPrompt: async () => answer, settingsConfirm: async () => confirm,
    loadProjectLibraries: async () => context.test.renderSharedLibraries([library], accounts),
    fetch: async (url, options) => {calls.push({url, options}); return {ok: success, json: async () => ({ok: success, detail: "云端需更新"})};},
  });
  const start = settingsHtml.indexOf("    function libraryIdentity("), end = settingsHtml.indexOf("    async function loadProjectLibraries(", start);
  assert.ok(start > 0 && end > start);
  vm.runInContext(settingsHtml.slice(start, end) + "\nglobalThis.test = {renderSharedLibraries, renderSharedMemorySettings, changeSharedMemory};", context);
  const api = context.test;
  api.renderSharedLibraries([deletedLibrary, library], accounts);
  assert.deepEqual($("weixin_library_select").children.map(option => option.value), ["default"], "deleted projects stay out of the settings list");
  const labels = $("weixin_memory_owner").children.map(x => x.textContent);
  assert.match(labels[1], /1.*项目负责人.*person-a/);
  assert.match(labels[2], /飞书 1.*项目负责人.*person-b/);
  const memberInputs = () => $("weixin_library_members").children.flatMap(label => label.children || []).filter(x => x.type === "checkbox");
  assert.equal(memberInputs()[0].checked, true, "administrator is always selected even without a query grant");
  assert.equal(memberInputs()[0].disabled, true, "administrator access cannot be unchecked");
  assert.equal(memberInputs()[1].disabled, false, "other members remain editable");
  memberInputs()[1].checked = false; memberInputs()[1].onchange();
  $("weixin_memory_owner").value = accounts[1].identity; $("weixin_memory_owner").onchange();
  assert.equal(memberInputs()[1].checked && memberInputs()[1].disabled, true, "new administrator is selected and locked immediately");
  assert.equal(memberInputs()[0].checked, false, "previous administrator is not silently granted reader access");
  $("weixin_memory_owner").value = accounts[0].identity; $("weixin_memory_owner").onchange();
  assert.equal(memberInputs()[1].checked, false, "switching administrator preserves edits to other readers");
  memberInputs()[1].checked = true; memberInputs()[1].onchange();
  $("weixin_memory_owner").value = "user:person-a";
  await api.changeSharedMemory();
  assert.equal(calls[0].url, "/api/memory/libraries");
  const body = JSON.parse(calls[0].options.body);
  assert.equal(body.id, "default"); assert.equal(body.action, "save");
  assert.equal(body.context_ttl_minutes, 30);
  $("weixin_shared_ttl").value = "4";
  await api.changeSharedMemory();
  assert.equal(calls.length, 1, "invalid context lifetime must not be submitted");
  $("weixin_shared_ttl").value = "30";
  assert.deepEqual(body.reader_identities, ["feishu:cli_test:person-b"]);
  assert.deepEqual(body.retained_identities, ["user:offline"]);
  await api.changeSharedMemory(true);
  answer = "错误名称"; await api.changeSharedMemory(true);
  assert.equal(calls.length, 1, "cancelled/wrong deletion sends no request");
  answer = library.call_name; target = "cloud"; await api.changeSharedMemory(true);
  assert.equal(calls[1].url, "/api/cloud/memory/libraries");
  assert.equal(JSON.parse(calls[1].options.body).action, "delete");
  confirm = false; await api.changeSharedMemory();
  assert.equal(calls.length, 2, "cancelled permission changes send no request");
  confirm = true; success = false; await api.changeSharedMemory();
  assert.match($("weixin_shared_status").textContent, /操作失败/);
  assert.equal($("weixin_shared_save").disabled, false);
  $("weixin_library_new").onclick();
  assert.equal($("weixin_library_members").querySelectorAll().length, 0, "new libraries default to no readers");
  assert.equal($("weixin_shared_delete").disabled, true);
  api.renderSharedLibraries(null, accounts);
  assert.equal($("weixin_shared_save").disabled, true);
  assert.match($("weixin_shared_status").textContent, /更新云端/);
  console.log("project libraries: stable identity labels, explicit grants, cloud routing, offline retention, exact deletion, errors and old hub states passed");
})().catch(error => { console.error(error); process.exitCode = 1; });

{
  const assert = require("node:assert/strict");
  const actions = settingsHtml.match(/<div class="actions project-library-actions">([\s\S]*?)<\/div>/);
  assert(actions, "project library actions must share one responsive group");
  assert.deepEqual([...actions[1].matchAll(/id="([^"]+)"/g)].map(match => match[1]),
    ["weixin_shared_save", "weixin_shared_delete", "weixin_library_backup"]);
  assert.match(settingsHtml, /#project_libraries_card\s*\{\s*container-type: inline-size;/);
  assert.match(settingsHtml, /\.project-library-actions\s*\{[^}]*grid-template-columns: repeat\(3, max-content\)/);
  assert.match(settingsHtml, /@container \(max-width: 520px\)\s*\{\s*\.project-library-actions\s*\{\s*grid-template-columns: minmax\(0, 1fr\)/);
}

// Mobile viewport changes must keep the composer visible without fighting pinch zoom.
{
  const assert = require("node:assert/strict");
  const vm = require("node:vm");
  const start = html.indexOf('    if (!IS_LOCAL_FILE && window.visualViewport) {');
  const end = html.indexOf('    // 只有当用户已经贴近底部时才自动跟随滚动', start);
  const properties = {}, listeners = {};
  let scrolls = 0, nearBottom = true;
  const visualViewport = {height: 720, offsetTop: 0, scale: 1,
    addEventListener: (name, callback) => { listeners[name] = callback; }};
  const context = vm.createContext({IS_LOCAL_FILE: false, window: {visualViewport},
    document: {documentElement: {style: {setProperty: (key, value) => { properties[key] = value; }}}},
    isLogNearBottom: () => nearBottom, closeComposerMenus: () => {},
    scrollToBottom: () => { scrolls++; }});
  vm.runInContext(html.slice(start, end), context);
  assert.equal(properties['--app-height'], '720px');
  visualViewport.height = 360;
  listeners.resize();
  assert.equal(properties['--app-height'], '360px', 'keyboard reduces the whole app height');
  nearBottom = false;
  const previousScrolls = scrolls;
  visualViewport.offsetTop = 80;
  listeners.scroll();
  assert.equal(properties['--app-top'], '80px');
  assert.equal(scrolls, previousScrolls, 'reading history must not jump to the latest message');
  visualViewport.scale = 2;
  visualViewport.height = 180;
  listeners.resize();
  assert.equal(properties['--app-height'], '360px', 'pinch zoom must not trigger a competing relayout');
}
// Chinese composition and mobile Enter must not accidentally send a message.
{
  const assert = require("node:assert/strict");
  const vm = require("node:vm");
  const start = html.indexOf('    textEl.addEventListener("keydown", (e) => {');
  const end = html.indexOf('    });', start) + '    });'.length;
  let listener, coarse = false, sends = 0, prevented = 0;
  vm.runInNewContext(html.slice(start, end), {
    textEl: {addEventListener: (_name, callback) => { listener = callback; }},
    matchMedia: () => ({matches: coarse}), send: () => { sends++; },
  });
  const event = {key: 'Enter', shiftKey: false, isComposing: false, preventDefault: () => { prevented++; }};
  listener({...event, isComposing: true});
  assert.equal(sends, 0);
  coarse = true; listener(event);
  assert.equal(sends, 0, 'mobile Enter inserts a line; the send button submits');
  coarse = false; listener({...event, shiftKey: true}); listener(event);
  assert.equal(sends, 1, 'desktop Enter still sends, Shift+Enter inserts a line');
  assert.equal(prevented, 1);
}

// Touch closes on click, so releasing the finger cannot reopen Settings underneath.
{
  const assert = require("node:assert/strict");
  const vm = require("node:vm");
  const start = html.indexOf('    closeSettingsModalButton.addEventListener("pointerdown", (event) => {');
  const end = html.indexOf('    settingsModal.addEventListener', start);
  const listeners = {}; let closed = 0;
  vm.runInNewContext(html.slice(start, end), {
    closeSettingsModalButton: {addEventListener: (type, fn) => { listeners[type] = fn; }},
    closeSettingsModal: () => { closed++; },
  });
  listeners.pointerdown({button: 0, pointerType: 'touch'});
  assert.equal(closed, 0, 'modal remains over the header until touch release');
  listeners.click({});
  assert.equal(closed, 1);
  listeners.pointerdown({button: 0, pointerType: 'mouse'});
  assert.equal(closed, 2, 'desktop pointer behavior stays unchanged');
}

// Failed chat handshakes distinguish authentication failure from browser transport issues.
(async () => {
  const assert = require('node:assert/strict');
  const vm = require('node:vm');
  const start = html.indexOf('    let connectionCheckRunning = false;');
  const end = html.indexOf('    document.getElementById("retryConnection").onclick', start);
  const elements = {};
  let status = 401;
  const context = vm.createContext({IS_LOCAL_FILE: false, API: 'https://hub.example', ws: null,
    WebSocket: {OPEN: 1}, AbortController, setTimeout, clearTimeout,
    document: {getElementById: id => elements[id] ||= {}},
    fetch: async () => ({status, ok: status === 200})});
  vm.runInContext(html.slice(start, end), context);
  await context.diagnoseConnection();
  assert.match(elements.connectionNoticeText.textContent, /分别登录/);
  assert.equal(elements.connectionLogin.hidden, false);
  status = 200; await context.diagnoseConnection();
  assert.match(elements.connectionNoticeText.textContent, /聊天通道未连接/);
  assert.equal(elements.connectionLogin.hidden, true);
  status = 403; await context.diagnoseConnection();
  assert.match(elements.connectionNoticeText.textContent, /密码配置/);
  context.fetch = async () => { throw new Error('network rejected'); };
  await context.diagnoseConnection();
  assert.match(elements.connectionNoticeText.textContent, /HTTPS 证书/);
  elements.connectionNotice.hidden = true;
  context.ws = {readyState: 1};
  await context.diagnoseConnection();
  assert.equal(elements.connectionNotice.hidden, true, 'late checks cannot overwrite a recovered connection');
})().catch(error => { console.error(error); process.exitCode = 1; });

// Use the actual provider/status function with independent credential fields.
{
  const assert = require("node:assert/strict");
  const start = settingsHtml.indexOf("    function updateVoiceStatus()");
  const end = settingsHtml.indexOf('    $("speech_provider").onchange', start);
  const fields = Object.fromEntries(["speech_provider", "tencent_voice_card", "baidu_voice_card", "tc_secret_id", "tc_secret_key", "baidu_api_key", "baidu_secret_key", "voice_status"].map(id => [id, {value: "", hidden: false, textContent: ""}]));
  const update = new Function("$", settingsHtml.slice(start, end) + "; return updateVoiceStatus;")(id => fields[id]);
  fields.speech_provider.value = "tencent";
  fields.tc_secret_id.value = "tc-id"; fields.tc_secret_key.value = "tc-key";
  update(); assert.equal(fields.baidu_voice_card.hidden, true);
  fields.speech_provider.value = "baidu";
  update(); assert.equal(fields.tencent_voice_card.hidden, true);
  assert.match(fields.voice_status.textContent, /百度智能云 未配置/);
  fields.baidu_api_key.value = "example-api"; fields.baidu_secret_key.value = "example-secret";
  update(); assert.match(fields.voice_status.textContent, /已填写/);
  fields.speech_provider.value = "tencent"; update();
  assert.equal(fields.baidu_api_key.value, "example-api");
  assert.equal(fields.tc_secret_key.value, "tc-key");
  console.log("speech provider: independent credentials, visible form and status passed");
}
