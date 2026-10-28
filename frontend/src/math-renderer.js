(function (root, factory) {
  const api = factory(typeof module === "object" && module.exports
    ? require("./vendor/katex/katex.min.js") : root.katex);
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.XiaoAMath = api;
})(typeof globalThis !== "undefined" ? globalThis : this, function (katex) {
  "use strict";

  // Called only after Markdown has protected code blocks, inline code and file paths.
  function replace(text, store) {
    if (!katex) return text;
    return text.replace(/\$\$([\s\S]+?)\$\$|\\\[([\s\S]+?)\\\]|\\\(([^\n]+?)\\\)|(?<![\\$])\$([^\n$]+?)\$(?!\$)/g,
      (original, dollars, brackets, parentheses, inline, offset) => {
        const display = dollars !== undefined || brackets !== undefined;
        let formula = dollars ?? brackets ?? parentheses ?? inline;
        // Dollar amounts such as "$5 and $10" are ordinary prose, not math.
        if (inline !== undefined && (/\d/.test(text[offset + original.length] || "") ||
            (!/\\(?:[A-Za-z]+|[%,])|[_^{}=<>+*/-]/.test(inline) && !/^(?:[\d.,]+|[A-Za-z])$/.test(inline.trim())))) return original;
        if (formula.length > 8192 || /\\(?:href|url|includegraphics|html[A-Za-z]*)\b/.test(formula)) return original;
        // Some gateways double-escape unit/comparison commands; never alter matrix row breaks.
        if (!/\\begin\b/.test(formula)) {
          formula = formula.replace(/\\\\(%|,|(?:text|mathrm|textrm|textnormal|leq?|geq?|neq?|times|cdot|pm|mp|approx|frac|sqrt)\b)/g, "\\$1");
        }
        try {
          const markup = katex.renderToString(formula, {
            output: "mathml", displayMode: display, throwOnError: true,
            trust: false, strict: "ignore", maxSize: 20, maxExpand: 500, macros: {},
          });
          return store(`<span class="chat-math${display ? " chat-math-display" : ""}">${markup}</span>`);
        } catch (_) {
          // Invalid or unfinished expressions remain escaped text in the normal renderer.
          return original;
        }
      });
  }

  return {replace};
});
