// Exercise the actual inline conversation controls without starting Electron.
const assert = require("assert/strict");
const fs = require("fs");
const path = require("path");
const vm = require("vm");
const html = fs.readFileSync(path.join(__dirname, "src", "index.html"), "utf8").replace(/\r\n/g, "\n");
function section(start, end) {
  const begin = html.indexOf(start);
  const finish = html.indexOf(end, begin);
  assert(begin >= 0 && finish > begin, `Missing inline section: ${start}`);
  return html.slice(begin, finish);
}
class Element {
  constructor() { this.attributes = {}; this.dataset = {}; this.children = []; this.style = {}; this.hidden = true; this.className = ""; this.value = ""; this._html = ""; }
  set innerHTML(value) { this._html = value; this.children = []; }
  get innerHTML() { return this._html; }
  append(...children) { this.children.push(...children); }
  appendChild(child) { this.append(child); }
  setAttribute(name, value) { this.attributes[name] = value; }
  querySelector(selector) { return this.children.find(child => child.className === selector.slice(1)) || this.children.flatMap(child=>child.children).find(child => child.className === selector.slice(1)); }
  addEventListener() {}
  classList = {toggle() {}};
}
const messages = [];
const storage = new Map();
const warnings = [];
const elements = Object.fromEntries([
  "conversationList", "conversationButton", "conversationPanel", "closeConversationButton",
  "newConversationButton", "log", "statusEl", "textEl", "voiceHint", "modelButton", "modelLabel", "modelMenu",
].map(name => [name, new Element()]));
let sequence = 0;
const context = vm.createContext({
  ...elements, Map, Array,
  window: {XiaoALanguage: {get: () => "en"}},
  crypto: {randomUUID: () => `test-${++sequence}`},
  document: {createElement: () => new Element(), addEventListener() {}},
  WebSocket: {OPEN: 1},
  ws: {readyState: 1, send: message => messages.push(JSON.parse(message))},
  localStorage: {getItem: key => storage.get(key) || null, setItem: (key, value) => storage.set(key, value), removeItem: key => storage.delete(key)},
  setBusy() {}, addEvent: (...args) => warnings.push(args), closeComposerMenus() {}, confirmConversationDelete: async () => false,
  pendingDeliveries: new Map(), pendingAttachments: [],
  addBubble() {}, renderAttachments() {}, getReasoningEffort: () => "high",
  selectedProviderId: "default", selectedModel: "demo",
  trySendDelivery: delivery => messages.push(delivery.payload),
});
vm.runInContext(`
  let currentConversationId = "default", draftConversationId = "", historyLoaded = false;
  const conversationStore = new Map();
  ${section('    const MODEL_SELECTION_KEY =', '    const REASONING_EFFORT_KEY =')}
  modelsLoaded = true;
  availableModels = [{provider_id: "default", model: "demo", provider_name: "默认接口"}, {provider_id: "other", model: "demo", provider_name: "备用接口"}];
  ${section('    function menuOption(', '    modelButton.onclick =')}

  ${section("    function newClientId()", "    const conversationConfirm =")}
  ${section("    function renderConversationList(", "    function clearDeliveryTimer(")}
  ${section("    function send(options)", "    async function stopCurrentTask()")}
`, context);
function value(expression) { return vm.runInContext(expression, context); }
const old = {id: "default", title: "旧任务"};
function listing(items, current = "default") {
  context.handleConversationControl({type: "conversation_list", data: {conversations: items, current_id: current}});
}
listing([old]);
for (let i = 0; i < 5; i++) context.newConversationButton.onclick();
const draft = value("currentConversationId");
assert.equal(messages.length, 0, "new must not send a create command");
assert.equal(value("draftConversationId"), draft);
assert.equal(value("conversationStore.size"), 1);
assert.equal(elements.conversationList.children.length, 1);
assert(!elements.conversationList.children[0].className.includes("active"));
assert.equal(value("historyLoaded"), true, "late reconnect history cannot clear first message");
listing([old]);
assert.equal(value("currentConversationId"), draft, "server's old selection must not replace draft");
assert.equal(context.send(), false, "blank input must not create a delivery");
elements.textEl.value = "请整理销售报表";
assert.equal(context.send(), true);
const payload = messages.at(-1);
assert.equal(payload.type, "user_input");
assert.equal(payload.response_language, "en");
context.window.XiaoALanguage.get = () => "zh";
assert.equal(context.pendingDeliveries.get(payload.client_id).payload.response_language, "en", "pending delivery keeps its original language");

assert.equal(payload.conversation_id, draft, "fast new/send must not target previous conversation");
assert.equal(payload.new_conversation, true);
assert.equal(value("pendingDeliveries.size"), 1);
listing([old]);
assert.equal(value("currentConversationId"), draft);
const created = {id: draft, title: "请整理销售报表"};
listing([old, created], draft);
assert.equal(value("draftConversationId"), "");
assert.equal(value("currentConversationId"), draft);
elements.textEl.value = "追加一张图";
context.send();
assert.equal(messages.at(-1).new_conversation, false);
// Reconnect must keep the accepted conversation, not jump to default.
listing([old, created], "default");
assert.equal(value("currentConversationId"), draft);
assert.equal(messages.at(-1).type, "conversation_select");
assert.equal(messages.at(-1).conversation_id, draft);
// Starting another draft while an older request completes cannot adopt its record.
context.newConversationButton.onclick();
const secondDraft = value("currentConversationId");
listing([old, created], draft);
assert.equal(value("currentConversationId"), secondDraft);
assert.equal(value("draftConversationId"), secondDraft);
// Switching to an existing row abandons the local draft, without persisting it.
elements.conversationList.children[0].onclick();
assert.equal(value("currentConversationId"), "default");
assert.equal(value("draftConversationId"), "");
listing([old]);
assert.equal(value("conversationStore.size"), 1, "removed entries must not reappear on next new click");
context.newConversationButton.onclick();
assert.equal(elements.conversationList.children.length, 1);
// An attachment-only first message also opts into deferred creation.
context.pendingAttachments = [{name: "报表.txt", type: "text/plain", size: 4, data: "demo"}];
context.send();
assert.equal(messages.at(-1).new_conversation, true);
assert.equal(messages.at(-1).attachments.length, 1);
assert.equal(messages.at(-1).text, "");
// Each conversation restores the exact provider/model pair, not just the model name.
listing([old, created]);
elements.conversationList.children[0].onclick();
value('modelMenu.children[1].onclick()');
assert.equal(context.selectedProviderId, "other");
assert.equal(messages.at(-1).type, "conversation_model", "manual selection saves before sending any chat message");
listing([old, created]); // A stale listing must not undo a just-selected model.
assert.equal(context.selectedProviderId, "other");
elements.conversationList.children[1].onclick();
assert.equal(context.selectedProviderId, "default");
elements.conversationList.children[0].onclick();
assert.equal(context.selectedProviderId, "other");
value("conversationModelSelections.clear()");
value('conversationStore.get("default").model_selection = {provider_id: "other", model: "demo"}');
context.selectedProviderId = "default";
context.restoreConversationModel();
assert.equal(context.selectedProviderId, "other", "server metadata survives a page restart");
// Removing one provider cannot silently select another provider with the same model name.
value('availableModels = availableModels.filter(item => item.provider_id !== "other")');
context.restoreConversationModel();
assert.equal(context.selectedProviderId, "other");
assert.match(elements.modelLabel.textContent, /不可用/);
assert.match(elements.modelButton.attributes["aria-label"], /不可用/);
assert.equal(elements.modelMenu.children[0].disabled, true);
elements.textEl.value = "模型删除后不要偷偷换接口";
context.pendingAttachments = [{name: "keep.txt", data: "keep"}];
const count = messages.length;
assert.equal(context.send(), false);
assert.equal(messages.length, count);
assert.equal(elements.textEl.value, "模型删除后不要偷偷换接口");
assert.equal(context.pendingAttachments.length, 1);
assert.equal(warnings.at(-1)[0], "warning");
// Re-adding the provider makes the original selection usable again.
value('availableModels.push({provider_id: "other", model: "demo", provider_name: "备用接口"})');
context.restoreConversationModel();
assert.equal(elements.modelLabel.textContent, "demo");
value('modelMenu.children[0].onclick()');
assert.equal(context.selectedProviderId, "default");
context.send();
assert.equal(messages.at(-1).provider_id, "default");
assert.equal(messages.at(-1).response_language, "zh");
context.window.XiaoALanguage = undefined;
elements.textEl.value = "hello";
assert.equal(context.send(), true);
assert.equal(messages.at(-1).response_language, "zh", "missing language API falls back to Chinese");
// A new draft must not inherit a missing model from the old conversation.
context.selectedProviderId = "gone";
context.selectedModel = "removed";
context.rememberConversationModel();
context.newConversationButton.onclick();
assert.equal(context.selectedModel, "demo");
assert.equal(context.selectedProviderId, "default");
// Local cache works when the server has no model metadata; storage failures are harmless.
value('conversationModelSelections.clear(); conversationStore.clear(); draftConversationId = ""; currentConversationId = "cached";');
storage.set("xiaoa.modelSelection.cached", JSON.stringify({provider_id: "other", model: "demo"}));
context.restoreConversationModel();
assert.equal(context.selectedProviderId, "other");
// A health response before the first conversation list must not overwrite server metadata.
value('conversationModelSelections.clear(); conversationStore.clear(); currentConversationId = "default";');
context.selectedProviderId = "default";
context.rememberConversationModel();
assert.equal(value('conversationModelSelections.size'), 0);
listing([{...old, model_selection: {provider_id: "other", model: "demo"}}]);
assert.equal(context.selectedProviderId, "other");
context.localStorage.setItem = () => { throw new Error("storage unavailable"); };
assert.doesNotThrow(() => value('modelMenu.children[0].onclick()'));
// Exercise the actual asynchronous health refresh, including an empty catalog.
Object.assign(context, {
  window: {}, AbortController, setTimeout, clearTimeout, API: "http://test", serviceFailed: false,
  fetch: async () => ({ok: true, json: async () => ({model_options: context.healthOptions, chat_model: "demo"})}),
});
value(`${section('    let healthLoading =', '    refreshHealth();\n\n    function closeComposerMenus()')}`);
{
  value('availableModels = [{provider_id:"default",model:"测试",provider_name:"默认接口"},{provider_id:"custom",model:"默认接口",provider_name:"默认接口"}]; renderModelMenu()');
  const copies = elements.modelMenu.children.slice(-2).map(button=>button.children[1]);
  assert.equal(copies[0].children[0].dataset.noI18n, "", "model names must remain untouched");
  assert.equal(copies[0].children[1].dataset.noI18n, undefined, "built-in provider label must translate");
  assert.equal(copies[1].children[1].dataset.noI18n, "", "custom provider names must remain untouched");
}

(async () => {
  context.localStorage.setItem = (key, data) => storage.set(key, data);
  context.healthOptions = [{provider_id: "default", model: "demo"}];
  context.selectedProviderId = "default";
  context.selectedModel = "demo";
  context.rememberConversationModel();
  context.healthOptions = [{provider_id: "other", model: "demo"}];
  await context.refreshHealth();
  assert.equal(context.selectedProviderId, "default", "health must not change providers");
  assert.match(elements.modelLabel.textContent, /不可用/);
  context.healthOptions = [];
  await context.refreshHealth();
  assert.equal(value("availableModels.length"), 0, "an empty catalog must not revive a removed model");
  assert.match(elements.modelLabel.textContent, /不可用/);
  context.healthOptions = [{provider_id: "default", model: "demo"}];
  await context.refreshHealth();
  assert.equal(elements.modelLabel.textContent, "demo");
  console.log("conversation controls, provider/model isolation, persistence, health refresh and unavailable-model checks passed");
})().catch(error => { console.error(error); process.exitCode = 1; });
