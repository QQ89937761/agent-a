'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const frames = [], timers = [], observers = [], scrolls = [];
let reduced = false, layoutReads = 0;
const document = {activeElement: null, createElement: () => new Element()};
class Element {
  constructor() { this.children = []; this.attrs = {}; this.listeners = {}; this.scrollTop = 0; this.clientHeight = 160; this.offsetHeight = 28; this.offsetTop = 0; this.textContent = ''; }
  appendChild(child) { this.children.push(child); }
  replaceChildren(...items) { this.children = items; items.forEach((item, i) => { item.offsetTop = i * 28; }); }
  setAttribute(key, value) { this.attrs[key] = value; }
  getAttribute(key) { return this.attrs[key]; }
  removeAttribute(key) { delete this.attrs[key]; }
  addEventListener(type, fn) { this.listeners[type] = fn; }
  contains(node) { return this.children.includes(node); }
  matches() { return false; }
  focus() { document.activeElement = this; }
  blur() { document.activeElement = null; }
  cloneNode() { const node = new Element(); node.textContent = this.textContent; return node; }
  querySelectorAll() { return []; }
}
const list = new Element(), nav = new Element(), log = new Element();
nav.querySelector = () => list; nav.contains = node => list.contains(node);
log.id = 'log'; log.clientHeight = 500; log.scrollHeight = 3000;
log.getBoundingClientRect = () => ({top: 60});
log.scrollTo = options => { scrolls.push(options); log.scrollTop = options.top; };
let nodes = [];
log.querySelectorAll = () => nodes;
const window = {matchMedia: () => ({matches: reduced}), XiaoALanguage: {translate: () => 'Attachment message'}};
const context = vm.createContext({window, document,
  requestAnimationFrame: fn => { frames.push(fn); return frames.length; },
  setTimeout: fn => { timers.push(fn); return timers.length; },
  MutationObserver: class {constructor(fn) { observers.push(fn); } observe() {}},
  ResizeObserver: class {constructor(fn) { observers.push(fn); } observe() {}},
});
vm.runInContext(fs.readFileSync('src/chat-navigator.js', 'utf8'), context);
function flush() { while (timers.length) timers.shift()(); while (frames.length) frames.shift()(); }
function message(text, top) {
  const node = new Element(), bubble = new Element(); bubble.textContent = text;
  node.querySelector = selector => selector === '.bubble' ? bubble : null;
  node.getBoundingClientRect = () => { layoutReads++; return {top: 60 + top - log.scrollTop}; };
  return node;
}
nodes = [message('first <img onerror=alert(1)>', 20), message('second', 1000), message('third', 2000)];
const control = window.createChatNavigator(log, nav); flush();
assert.equal(nav.hidden, false);
assert.equal(list.children.length, 3);
assert.equal(list.children[0].getAttribute('aria-current'), 'true');
assert.equal(list.children[0].children[0].textContent, 'first <img onerror=alert(1)>', 'summaries remain inert text');
assert.equal(list.children[0].getAttribute('aria-controls'), 'log');
const firstButtons = [...list.children], initialReads = layoutReads;
log.scrollTop = 1100; log.listeners.scroll(); flush();
assert.equal(list.children[1].getAttribute('aria-current'), 'true');
assert.equal(layoutReads, initialReads, 'scroll frames reuse cached layout');
for (let i = 0; i < 20; i++) observers[0]();
assert.equal(timers.length, 1, 'stream updates coalesce'); flush();
assert.equal(list.children[0], firstButtons[0], 'streaming must not rebuild navigation buttons');
list.children[1].onclick();
assert.equal(control.isBrowsing(), true);
assert.equal(scrolls.at(-1).top, 984);
assert.equal(scrolls.at(-1).behavior, 'smooth');
reduced = true; list.children[0].onclick();
assert.equal(scrolls.at(-1).behavior, 'instant');
assert.equal(scrolls.at(-1).top, 4);
control.followLatest(); assert.equal(control.isBrowsing(), false);
log.listeners.wheel({deltaY: -1}); assert.equal(control.isBrowsing(), true);
log.scrollTop = 2500; log.listeners.scroll(); flush(); assert.equal(control.isBrowsing(), false);
log.scrollTop = 2000; log.listeners.scroll(); flush(); assert.equal(control.isBrowsing(), true, 'scrollbar/keyboard upward movement disables following');
list.children[0].focus();
let prevented = false;
list.listeners.keydown({key: 'ArrowDown', target: list.children[0], preventDefault: () => { prevented = true; }});
assert.equal(prevented, true); assert.equal(document.activeElement, list.children[1]);
list.listeners.keydown({key: 'Escape', target: document.activeElement}); assert.equal(document.activeElement, null);
nodes = [message('', 20)]; observers[0](); flush();
assert.equal(nav.hidden, true); assert.equal(list.children.length, 1);
assert.equal(list.children[0].getAttribute('aria-label'), 'Attachment message');
nodes = []; observers[0](); flush();
assert.equal(nav.hidden, true); assert.equal(list.children.length, 0); assert.equal(control.isBrowsing(), false);
console.log('chat navigation: safe summaries, cached layout, streaming stability, exact jumps, reduced motion, keyboard, scroll retention and conversation reset passed');
