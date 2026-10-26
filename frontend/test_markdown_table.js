const assert = require("node:assert/strict");
const table = require("./src/markdown-table.js");

const rendered = table.render([
  "| 编号 | 部门 | 人数 |",
  "|:---|---|---:|",
  "| 1 | 项目部 | 5 |",
  "| 2 | 技术部 | 6 |",
].join("\n"));

assert.match(rendered, /<table class="md-table">/);
assert.match(rendered, /<th class="align-left">编号<\/th>/);
assert.match(rendered, /<td class="align-right">5<\/td>/);
assert.doesNotMatch(rendered, /^\|/);
assert.equal(table.render("普通 | 文本"), "普通 | 文本");

console.log("markdown table rendering passed");
