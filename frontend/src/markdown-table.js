(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.XiaoAMarkdownTable = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";

  function splitRow(line) {
    let value = String(line || "").trim();
    if (!value.includes("|")) return null;
    if (value.startsWith("|")) value = value.slice(1);
    if (value.endsWith("|")) value = value.slice(0, -1);
    const cells = value.split("|").map(cell => cell.trim());
    return cells.length >= 2 ? cells : null;
  }

  function alignment(cell) {
    if (!/^:?-{3,}:?$/.test(cell)) return null;
    if (cell.startsWith(":") && cell.endsWith(":")) return "center";
    if (cell.endsWith(":")) return "right";
    return "left";
  }

  function render(text) {
    const lines = String(text || "").split("\n");
    const output = [];
    for (let i = 0; i < lines.length; i += 1) {
      const header = splitRow(lines[i]);
      const separator = i + 1 < lines.length ? splitRow(lines[i + 1]) : null;
      const aligns = separator ? separator.map(alignment) : [];
      if (!header || !separator || header.length !== separator.length || aligns.some(value => !value)) {
        output.push(lines[i]);
        continue;
      }

      const rows = [];
      i += 2;
      while (i < lines.length) {
        const row = splitRow(lines[i]);
        if (!row || row.length !== header.length) break;
        rows.push(row);
        i += 1;
      }
      i -= 1;

      const head = header.map((cell, index) =>
        `<th class="align-${aligns[index]}">${cell}</th>`
      ).join("");
      const body = rows.map(row => `<tr>${row.map((cell, index) =>
        `<td class="align-${aligns[index]}">${cell}</td>`
      ).join("")}</tr>`).join("");
      output.push(`<div class="md-table-wrap"><table class="md-table"><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table></div>`);
    }
    return output.join("\n");
  }

  return { render, splitRow };
});
