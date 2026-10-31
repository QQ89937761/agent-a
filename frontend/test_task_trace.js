// Run the actual task-card and stream implementation against a small inert DOM.
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const assert = require("node:assert/strict");
const html = fs.readFileSync(path.join(__dirname, "src/index.html"), "utf8");
const start = html.indexOf("    const taskTraceCards = new Map();");
const end = html.indexOf("    function addEvent(", start);
assert(start > 0 && end > start);
class Element {
  constructor(tag = "div") { this.tag = tag; this.children = []; this.dataset = {}; this.attrs = {}; this.listeners = {}; this.className = ""; this.hidden = false; this.open = false; this.textContent = ""; }
  get isConnected() { return this.root || !!this.parentNode?.isConnected; }
  appendChild(child) { child.remove(); this.children.push(child); child.parentNode = this; return child; }
  append(...items) { items.forEach(item => this.appendChild(item)); }
  remove() { if (this.parentNode) { this.parentNode.children = this.parentNode.children.filter(item => item !== this); this.parentNode = null; } }
  after(node) { const parent = this.parentNode; node.remove(); parent.children.splice(parent.children.indexOf(this) + 1, 0, node); node.parentNode = parent; }
  replaceChildren(...children) { this.children.forEach(item => {item.parentNode = null;}); this.children = []; this.append(...children); }
  setAttribute(key, value) { this.attrs[key] = value; }
  addEventListener(key, listener) { this.listeners[key] = listener; }
  querySelector(selector) { for (const item of this.children) { if (selector === `.${item.className}`) return item; const found = item.querySelector(selector); if (found) return found; } return null; }
}
const log = new Element(); log.root = true;
const sent = [];
const raf = [], results = [], intervals = new Set();
const ctx = vm.createContext({
  Map, Set, Date, Number, Array, log, currentConversationId: "conv_draft", taskBusy: false,
  selectedProviderId: "default", selectedModel: "demo",
  API: "http://local.test", encodeURIComponent, statusEl: {}, setBusy: value => {ctx.taskBusy = value;},
  ws: {readyState: 1, send: message => sent.push(JSON.parse(message))},
  fetch: async () => ({ok: true, text: async () => "<img src=x onerror=alert(1)>"}),
  TOOL_LABEL: {fs_read_text: "读文件", run_command: "执行命令"},
  document: {createElement: tag => new Element(tag)},
  setInterval: fn => { intervals.add(fn); return fn; }, clearInterval: fn => intervals.delete(fn),
  requestAnimationFrame: fn => raf.push(fn), scrollIfNear() {}, maybeTimeSep() {}, addCopyAction() {},
  setBubbleContent: (node, text) => { node.textContent = text; }, renderMarkdownLive: text => text,
  addBubble: (role, text, ts, attachments) => { results.push({role, text, attachments}); const node = new Element(); log.appendChild(node); return node; },
});
vm.runInContext(html.slice(start, end), ctx);
const run = text => vm.runInContext(text, ctx);
const trace = changes => ({id: "turn", status: "running", has_tools: false, started_at: Date.now() - 3000, step_count: 0, current: "正在准备回复", events: [], ...changes});
const update = (value, options = {}) => {ctx.value = value; ctx.options = options; return run("updateTaskTrace(value, options)");};
let card = update(trace());
assert.equal(card.details.open, false);
assert.match(card.current.textContent, /当前/);
assert.match(card.meta.textContent, /3 秒/);
const {translate} = require("./src/ui-language");
assert.equal(translate(card.title.textContent, "en"), "Running · Reply trace");
assert.equal(translate(card.current.textContent, "en"), "Current: Preparing a reply");
assert.equal(translate(card.meta.textContent, "en"), "0 actions · 3s");
assert.equal(intervals.size, 1);
run('appendToStreamBubble("先检查文件"); finishStreamBubble("先检查文件");');
assert.equal(card.liveNode.parentNode, card.messages, "ordinary chat streams outside the disclosure");
assert.equal(card.messages.parentNode, log);
card.details.open = true;
card = update(trace({has_tools: true, step_count: 1, llm_ms: 3500, tool_ms: 1250, current: "fs_read_text", events: [{kind: "message", text: "先检查文件"}, {kind: "tool", name: "fs_read_text", hint: "report.txt", status: "running", tool_ms: 1250}]}));
assert.equal(card.timings.textContent, "模型等待 3 秒 · 工具执行 1 秒");
assert.equal(card.timings.hidden, false);
assert.match(card.events.children[0].textContent, /1 秒/);
assert.equal(card.details.open, true, "live updates must preserve user expansion");
assert.equal(card.liveNode, null, "the saved preamble replaces its temporary stream");
assert.equal(card.messages.children[0].children[0].textContent, "先检查文件");
assert.equal(card.events.children.length, 1, "only tool steps are folded");
assert.match(card.current.textContent, /读文件/);
run('appendToStreamBubble("检查中");');
assert.equal(card.liveNode.parentNode, card.messages, "dialogue during tool work stays outside the disclosure");
run('finishStreamBubble("检查中");');
card = update(trace({has_tools: true, status: "waiting", current: "等待你确认操作", events: []}));
assert.match(card.title.textContent, /等待确认/);
card = update(trace({has_tools: true, status: "completed", duration_ms: 23000, step_count: 1, events: [{kind: "message", text: "检查中"}, {kind: "tool", name: "fs_read_text", status: "done"}]}), {finalReply: "最终结果", attachments: [{name: "report.txt"}]});
assert.equal(card.details.open, false, "completion returns to concise result view");
assert.equal(card.liveNode, null);
assert.equal(results.at(-1).text, "最终结果");
assert.equal(results.at(-1).attachments[0].name, "report.txt");
assert.equal(intervals.size, 0, "finished history does not keep a clock alive");
update(ctx.value, {finalReply: "最终结果"});
assert.equal(results.length, 1, "duplicate terminal snapshots do not duplicate result bubbles");
run("resetTaskTraces();");
log.replaceChildren();
card = update(trace({status: "completed", events: [], duration_ms: 500}), {finalReply: "你好"});
assert.equal(card.root.hidden, true, "plain chat does not leave empty process cards");
run("resetTaskTraces();"); log.replaceChildren();
card = update(trace({has_tools: true, status: "interrupted", events: [{kind: "tool", name: "run_command", status: "interrupted", detail: '<img src=x onerror=alert(1)>'}]}));
assert.equal(card.details.open, false, "reopened trails are always initially collapsed");
assert.match(card.current.textContent, /未正常结束/);
const detail = card.events.children[0].children[0];
assert.equal(detail.children[1].textContent, '<img src=x onerror=alert(1)>', "raw errors are inert text, not HTML");
card.details.open = true; card.details.listeners.toggle();
assert.equal(card.details.children[0].attrs["aria-expanded"], "true");
console.log("task trace: visible dialogue, folded tools, final result, timing and inert errors passed");

// Real message snapshots replace live text once and survive status/tool updates.
run("resetTaskTraces();"); log.replaceChildren();
const preamble = {kind: "message", text: "我会打开 CAD，先确认图纸单位。", ts: 100};
const tool = {kind: "tool", name: "run_command", status: "running", ts: 101};
let visibleTrace = trace({has_tools: true, events: []});
card = update(visibleTrace);
run('showTaskMessage("我会打开 CAD，先确认图纸单位。");');
assert.equal(card.messages.children.length, 1);
visibleTrace = {...visibleTrace, events: [preamble, tool]};
update(visibleTrace, {reason: "message"});
assert.equal(card.messages.children.length, 1, "message event replaces live dialogue without duplication");
const preambleNode = card.messages.children[0];
card.details.open = false;
update({...visibleTrace, current: "正在整理结果"}, {reason: "tool_result"});
assert.equal(card.messages.children[0], preambleNode, "tool snapshots do not rebuild unchanged dialogue");
assert.equal(card.messages.parentNode, log, "folding details cannot hide assistant dialogue");
run('appendToStreamBubble("图纸已经打开。"); finishStreamBubble("图纸已经打开。");');
const commentary = {kind: "message", text: "图纸已经打开。", ts: 102};
visibleTrace = {...visibleTrace, events: [preamble, tool, commentary]};
update(visibleTrace, {reason: "message_end"});
assert.equal(card.messages.children.length, 2);
assert.equal(card.messages.children[1].children[0].textContent, commentary.text);
update(visibleTrace, {reason: "message_end"});
assert.equal(card.messages.children.length, 2, "duplicate snapshots do not duplicate dialogue");
// The backend removes the last message event when it becomes the final reply.
update({...visibleTrace, status: "completed", events: [preamble, tool]}, {finalReply: commentary.text});
assert.equal(card.messages.children.length, 1);
assert.equal(card.details.open, false);
assert.equal(results.at(-1).text, commentary.text);
assert.equal(card.root.parentNode.children.indexOf(card.messages) < card.root.parentNode.children.indexOf(card.root), true);

// Identical words at different event positions are distinct turns, including replay.
for (const status of ["stopped", "failed", "interrupted"]) {
  run("resetTaskTraces();"); log.replaceChildren();
  const saved = trace({status, has_tools: true, events: [preamble, tool, {...preamble, ts: 103}]});
  card = update(saved);
  assert.equal(card.details.open, false);
  assert.equal(card.messages.children.length, 2, `${status} history keeps both assistant messages visible`);
  assert.equal(card.events.children.length, 1);
  update(saved);
  assert.equal(card.messages.children.length, 2);
}

// Stopping mid-stream must keep the visible partial reply even without a message_end.
run("resetTaskTraces();"); log.replaceChildren();
card = update(trace({has_tools: true}));
run('appendToStreamBubble("正在确认图纸单位");');
update(trace({has_tools: true, status: "stopped"}));
assert.equal(card.messages.children[0].children[0].textContent, "正在确认图纸单位");
assert.equal(card.liveNode, null);
update(trace({has_tools: true, status: "stopped"}));
assert.equal(card.messages.children.length, 1);
console.log("task trace: message handoff, duplicate snapshots, final reply, stopped/failed history and partial streams passed");

// Local stop must freeze immediately, even if an in-flight running snapshot arrives.
sent.length = 0;
run("resetTaskTraces(); taskBusy = false");
card = update(trace({id: "stop-race", has_tools: true}));
run("markCurrentTraceStopped()");
const frozenDuration = card.trace.duration_ms;
assert.equal(card.trace.status, "stopped");
assert.equal(intervals.size, 0);
update(trace({id: "stop-race", has_tools: true, duration_ms: frozenDuration + 5000}));
assert.equal(card.trace.status, "stopped");
assert.equal(card.trace.duration_ms, frozenDuration);
assert.equal(intervals.size, 0);
update(trace({id: "stop-race", status: "stopped", can_resume: true}));
card.resumeButton.onclick();
assert.equal(card.stopRequested, false);
update(trace({id: "stop-race"}));
assert.equal(card.trace.status, "running", "explicit continue may restart the clock");
run("resetTaskTraces()");
sent.length = 0; ctx.taskBusy = false;
console.log("task trace: immediate stop, late snapshot and explicit resume passed");

(async () => {
  run("resetTaskTraces();"); log.replaceChildren();
  const value = trace({has_tools: true, status: "stopped", can_resume: true, conversation_id: "conv_bound", events: [
    {kind: "tool", step: 1, name: "fs_read_text", status: "done", raw_return: {saved_name: "trace-test.txt", truncated: true}},
  ]});
  card = update(value);
  assert.equal(card.actions.hidden, false);
  const view = card.events.children[0].children[0];
  assert.match(view.children[1].textContent, /截断/);
  assert.equal(view.children[2].textContent, "展开后加载。");
  view.open = true; await view.listeners.toggle();
  assert.equal(view.children[2].textContent, '<img src=x onerror=alert(1)>');
  update(value);
  assert.equal(card.events.children[0].children[0], view, "live updates preserve the open raw return");
  card.resumeButton.onclick(); card.resumeButton.onclick();
  assert.deepEqual(sent, [{type: "continue_task", conversation_id: "conv_bound", task_trace_id: "turn", provider_id: "default", model: "demo"}]);
  assert.equal(card.resumeButton.disabled, true);
  ctx.taskBusy = false;
  update({...value, can_resume: false});
  assert.equal(card.actions.hidden, true);
  assert.match(card.current.textContent, /不会从头重复/);
  ctx.fetch = async () => {throw new Error("offline");};
  const failed = update({...value, id: "retry", events: [{kind: "tool", name: "fs_read_text", status: "done", raw_return: {saved_name: "missing.txt"}}]});
  const retry = failed.events.children[0].children[0]; retry.open = true; await retry.listeners.toggle();
  assert.match(retry.children[2].textContent, /加载失败/);
  ctx.fetch = async () => ({ok: true, text: async () => "retried"});
  retry.open = false; await retry.listeners.toggle(); retry.open = true; await retry.listeners.toggle();
  assert.equal(retry.children[2].textContent, "retried");
  ctx.ws.readyState = 0; failed.resumeButton.onclick();
  assert.match(failed.current.textContent, /连接尚未恢复/);
  console.log("task trace: lazy inert raw returns, retry, checkpoint binding and double-click protection passed");
})().catch(error => {console.error(error); process.exitCode = 1;});
