// 三种提醒都"一直闪到清除"(不再有 cycles 自动停):
// 清除时机是用户点悬浮球 / 打开对话面板(float.html 与 main.js 的 toggle-chat 都会 clear)。
const FLOAT_ALERTS = Object.freeze({
  approval: Object.freeze({priority: 3, colors: ["#FF2D2D", "#FFD400"], interval: 400}),
  complete: Object.freeze({priority: 2, colors: ["#0A84FF", "#7DF9FF"], interval: 700}),
  incoming: Object.freeze({priority: 1, colors: ["#00E676", "#EEFF41"], interval: 700}),
});

function createFloatAlertQueue(onChange, timers = globalThis) {
  let active = null;
  let timer = null;
  const queued = [];

  function publish() {
    const config = active ? FLOAT_ALERTS[active] : null;
    onChange(config ? {active: true, kind: active, ...config} : {active: false});
  }

  function schedule() {
    if (timer) timers.clearTimeout(timer);
    timer = null;
    const config = active && FLOAT_ALERTS[active];
    if (!config || !config.cycles) return;
    timer = timers.setTimeout(() => finish(active), config.interval * config.cycles);
  }

  function start(kind) {
    active = kind;
    publish();
    schedule();
  }

  function addQueued(kind) {
    if (!queued.includes(kind)) queued.push(kind);
    queued.sort((left, right) => FLOAT_ALERTS[right].priority - FLOAT_ALERTS[left].priority);
  }

  function finish(kind) {
    if (kind && active !== kind) {
      const index = queued.indexOf(kind);
      if (index >= 0) queued.splice(index, 1);
      return;
    }
    if (timer) timers.clearTimeout(timer);
    timer = null;
    active = null;
    if (queued.length) start(queued.shift());
    else publish();
  }

  return {
    enqueue(kind) {
      const config = FLOAT_ALERTS[kind];
      if (!config) return false;
      if (!active) start(kind);
      else if (active === kind) schedule();
      else if (config.priority > FLOAT_ALERTS[active].priority) {
        addQueued(active);
        start(kind);
      } else {
        addQueued(kind);
      }
      return true;
    },
    clear(kind) {
      if (!kind) {
        queued.length = 0;
        finish(active);
      } else {
        finish(kind);
      }
    },
    state() { return {active, queued: [...queued]}; },
  };
}

module.exports = {FLOAT_ALERTS, createFloatAlertQueue};
