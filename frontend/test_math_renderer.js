"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const math = require("./src/math-renderer.js");
const table = require("./src/markdown-table.js");
const html = fs.readFileSync(path.join(__dirname, "src/index.html"), "utf8");
const context = vm.createContext({
  window: {XiaoAMath: math, XiaoAMarkdownTable: table},
  log: {addEventListener() {}},
});
const a = html.indexOf("    function _escapeHtmlText("), b = html.indexOf("    async function copyText(", a);
vm.runInContext(html.slice(a, b) + "\nglobalThis.test = {renderMarkdown, renderMarkdownLive};", context);
const {renderMarkdown: render, renderMarkdownLive: live} = context.test;
for (const input of [String.raw`长度 $185\text{m}$、$158\\text{m}$`,
  String.raw`面积 $23,971.34\text{m}^2$`, String.raw`厚度 $> 4\\text{m}$，B区 $\le 4\text{m}$`,
  String.raw`\(x^2 + y^2 = z^2\)`, String.raw`\[\frac{1}{2} \times 4\]`,
  String.raw`$$\begin{matrix}a&b\\c&d\end{matrix}$$`]) {
  const output = render(input);
  assert.match(output, /<math\b/);
  assert.doesNotMatch(output, /katex-error|katex-html|<mspace linebreak="newline"/);
  assert.equal(live(input), output, "stream completion uses the same renderer as saved history");
}
assert.match(render(String.raw`$\le 4\text{m}$`), /<mo>≤<\/mo>/);
assert.match(render(String.raw`$4\\text{m}$`), /<mtext>m<\/mtext>/);
for (const formula of [String.raw`$67\%$`, String.raw`$67\\%$`, String.raw`$(546294.03,\, 2393385.00)$`, String.raw`$(546294.03,\\, 2393385.00)$`, String.raw`**$67\\%$** and **$(546294.03,\\, 2393385.00)$**`]) {
  const output = render(formula);
  assert.match(output, /<math\b/, formula);
  assert.doesNotMatch(output, /\$|\*\*|katex-error|<mspace linebreak="newline"/);
  assert.equal(live(formula), output);
}
assert.match(render(String.raw`$23,971.34\text{m}^2$`), /<msup>/);
assert.match(render(String.raw`\[\frac{1}{2}\]`), /<mfrac>/);
assert.match(render(String.raw`$$\begin{matrix}a&b\\c&d\end{matrix}$$`), /<mtable\b/);
assert.match(render(String.raw`**面积 $4\text{m}^2$**`), /<b>面积 <span class="chat-math">/);
assert.match(render('| 面积 | 数量 |\n|---|---|\n| $x^2$ | 1 |'), /<td[^>]*><span class="chat-math">/);
const htmlBreakTable = render('| A | B |\n|---|---|\n| first<br>second | value |');
assert.match(htmlBreakTable, /<td[^>]*>first<br>second<\/td>/);
assert.doesNotMatch(htmlBreakTable, /&lt;br/);
assert.equal(render('plain <br/> break'), 'plain <br> break');
const screenshotTable = String.raw`| Mode | Parameters |
|---|---|
| **2+1**<br>(two passes + one full pass)<br>standard | **$67\\%$** |
| CAD | **$67\%$**<br>$(546294.03,\, 2393385.00)$ |`;
assert.equal(live(screenshotTable), render(screenshotTable));
assert.match(render(screenshotTable), /<b>2\+1<\/b><br>/);
assert.equal((render(screenshotTable).match(/<math\b/g) || []).length, 3);
assert.match(render(screenshotTable), /<mn>67<\/mn><mi mathvariant="normal">%<\/mi>/);
assert.doesNotMatch(render(screenshotTable), /\$|&lt;br|katex-error|<mspace linebreak="newline"/);

assert.equal(render('first<BR />second<br>third'), 'first<br>second<br>third');
assert.equal(render('`<br>`'), '<code>&lt;br&gt;</code>');
assert.equal(render('```html\n<br>\n```'), '<pre class="code">html\n&lt;br&gt;</pre>');
assert.doesNotMatch(render('<br onclick="alert(1)"><br onmouseover=alert(1)><img src=x onerror=alert(1)>'), /<br|<img|<script/);

for (const prose of ['价格 $5 和 $10，合计 $15', 'Cost $5 and $10', 'Budget: $1,000.00; total: $2,000.00', '$PATH', '变量 $PATH 和 $HOME', String.raw`转义 \$5 和 \$10`]) {
  assert.equal(render(prose), prose);
}
for (const code of ['`$x^2$`', '```latex\n$x^2$\n```']) {
  assert.doesNotMatch(render(code), /<math/);
  assert.match(render(code), /\$x\^2\$/);
}
for (const dangerous of [String.raw`$\href{javascript:alert(1)}{打开}$`,
  String.raw`$\htmlClass{evil}{x}$`, String.raw`$\includegraphics{https://remote.test/tracker}$`,
  '$<img src=x onerror=alert(1)>$', '<script>alert(1)</script>']) {
  const output = render(dangerous);
  assert.doesNotMatch(output, /<script|<img|<a\b|onclick=|onerror="|class="evil"/);
}
for (const invalid of [String.raw`$\unknown{x}$`, '$x^{', '$' + 'x'.repeat(8193) + '$']) {
  assert.doesNotMatch(render(invalid), /<math\b/);
  assert.match(render(invalid), /\$/);
}
assert.doesNotMatch(render(String.raw`$\gdef\injected{x}$ $\injected$`), /<mi>x<\/mi>.*<mi>x<\/mi>/);
const withoutMath = vm.createContext({window: {}, log: {addEventListener() {}}});
vm.runInContext(html.slice(a, b) + "\nglobalThis.render = renderMarkdown;", withoutMath);
assert.equal(withoutMath.render('$x^2$'), '$x^2$', "missing optional renderer keeps readable source");
const pkg = JSON.parse(fs.readFileSync(path.join(__dirname, "package.json"), "utf8"));
for (const file of ['src/math-renderer.js', 'src/vendor/katex/katex.min.js', 'src/vendor/katex/LICENSE']) {
  assert(pkg.build.files.includes(file), "formula renderer and license must be included in the portable application");
  assert(fs.existsSync(path.join(__dirname, file)));
}
assert(html.includes('<script src="./vendor/katex/katex.min.js"></script>'));
assert(html.includes('<script src="./math-renderer.js"></script>'));
console.log("math rendering: screenshot units, inequalities, exponents, fractions, matrices, streaming/history, code, currency, inert errors and trusted local assets passed");
