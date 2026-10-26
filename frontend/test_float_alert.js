const assert = require("node:assert/strict");
const {FLOAT_ALERTS, createFloatAlertQueue} = require("./float_alert");

assert.deepEqual(FLOAT_ALERTS.approval.colors, ["#FF2D2D", "#FFD400"]);
assert.deepEqual(FLOAT_ALERTS.complete.colors, ["#0A84FF", "#7DF9FF"]);
assert.deepEqual(FLOAT_ALERTS.incoming.colors, ["#00E676", "#EEFF41"]);

const scheduled = [];
const events = [];
const timers = {
  setTimeout(callback, delay) {
    const item = {callback, delay, cleared: false};
    scheduled.push(item);
    return item;
  },
  clearTimeout(item) { if (item) item.cleared = true; },
};
const alerts = createFloatAlertQueue(event => events.push(event), timers);

alerts.enqueue("approval");
alerts.enqueue("incoming");
assert.equal(alerts.state().active, "approval");
assert.deepEqual(alerts.state().queued, ["incoming"]);
alerts.clear("approval");
assert.equal(alerts.state().active, "incoming");

alerts.clear();
const beforeComplete = scheduled.length;
alerts.enqueue("complete");
assert.equal(events.at(-1).kind, "complete");
// complete 也"一直闪到清除"(用户要求),不再 2.8 秒自动停
assert.equal(scheduled.length, beforeComplete, "complete 不该安排自动停的定时器");
assert.equal(alerts.state().active, "complete");
alerts.clear("complete");
assert.equal(alerts.state().active, null);
assert.equal(events.at(-1).active, false);

alerts.enqueue("incoming");
alerts.enqueue("approval");
assert.equal(alerts.state().active, "approval");
assert.deepEqual(alerts.state().queued, ["incoming"]);
alerts.clear();
assert.equal(alerts.state().active, null);
assert.deepEqual(alerts.state().queued, []);

console.log("float alert queue passed");
