"""Readable text for channels that do not render desktop Markdown/MathML.

Only the outgoing copy is formatted. Unknown TeX is retained, never evaluated.
"""
from __future__ import annotations

import re

_MATH = re.compile(r"\$\$([\s\S]+?)\$\$|\\\[([\s\S]+?)\\\]|\\\(([^\n]+?)\\\)|(?<![\\$])\$([^\n$]+?)\$(?!\$)")
_SYMBOLS = {
    "pm": "±", "mp": "∓", "times": "×", "cdot": "·", "sim": "～",
    "approx": "≈", "le": "≤", "leq": "≤", "ge": "≥", "geq": "≥",
    "ne": "≠", "neq": "≠", "infty": "∞", "circ": "°",
    "alpha": "α", "beta": "β", "gamma": "γ", "delta": "δ", "Delta": "Δ",
    "theta": "θ", "lambda": "λ", "mu": "μ", "pi": "π", "sigma": "σ",
    "Sigma": "Σ", "phi": "φ", "omega": "ω", "Omega": "Ω",
    "to": "→", "rightarrow": "→", "in": "∈", "notin": "∉",
    "quad": " ", "qquad": " ", "left": "", "right": "",
}
_SUPER = str.maketrans("0123456789+-=()ni", "⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻⁼⁽⁾ⁿⁱ")
_SUB = str.maketrans("0123456789+-=()aehijklmnoprstuvx", "₀₁₂₃₄₅₆₇₈₉₊₋₌₍₎ₐₑₕᵢⱼₖₗₘₙₒₚᵣₛₜᵤᵥₓ")


def _math_text(source: str) -> str:
    if len(source) > 8192:
        raise ValueError("formula too long")
    # Gateways sometimes double-escape commands. Matrix row breaks stay intact.
    if r"\begin" not in source:
        source = re.sub(r"\\\\(%|,|[A-Za-z]+)", r"\\\1", source)

    def parse(pos: int, depth: int = 0, grouped: bool = False) -> tuple[str, int]:
        if depth > 24:
            raise ValueError("formula nesting too deep")
        result = []

        def argument(index: int) -> tuple[str, int]:
            while index < len(source) and source[index].isspace():
                index += 1
            if index >= len(source) or source[index] != "{":
                raise ValueError("missing argument")
            return parse(index + 1, depth + 1, True)

        while pos < len(source):
            ch = source[pos]
            pos += 1
            if ch == "}":
                if not grouped:
                    raise ValueError("unmatched brace")
                return "".join(result), pos
            if ch == "{":
                inner, pos = parse(pos, depth + 1, True)
                result.append("(" + inner + ")")
            elif ch == "\\":
                command = re.match(r"[A-Za-z]+|.", source[pos:])
                if not command:
                    raise ValueError("unfinished command")
                name = command[0]
                pos += len(name)
                if name in ("text", "mathrm", "textrm", "textnormal", "mathbf", "mathit", "operatorname"):
                    value, pos = argument(pos)
                    result.append(value)
                elif name in ("frac", "dfrac", "tfrac"):
                    numerator, pos = argument(pos)
                    denominator, pos = argument(pos)
                    result.append(f"({numerator})/({denominator})")
                elif name == "sqrt":
                    value, pos = argument(pos)
                    result.append(f"√({value})")
                elif name in _SYMBOLS:
                    result.append(_SYMBOLS[name])
                elif name in (",", ";", ":", " "):
                    result.append(" ")
                elif name == "!":
                    pass
                elif name in ("%", "$", "_", "{", "}", "#", "&"):
                    result.append(name)
                else:
                    raise ValueError("unsupported command")
            elif ch in ("^", "_"):
                if pos < len(source) and source[pos] == "{":
                    value, pos = argument(pos)
                elif pos < len(source) and source[pos].isalnum():
                    value, pos = source[pos], pos + 1
                else:
                    raise ValueError("unsupported script")
                mapping = _SUPER if ch == "^" else _SUB
                result.append(value.translate(mapping) if all(ord(c) in mapping for c in value)
                              else f"{ch}({value})")
            else:
                result.append(ch)
        if grouped:
            raise ValueError("unclosed brace")
        return "".join(result), pos

    return parse(0)[0].strip()


def _replace_math(match: re.Match) -> str:
    formula = next(value for value in match.groups() if value is not None)
    inline = match[4]
    if inline is not None:
        following = match.string[match.end():match.end() + 1]
        if following.isdigit() or (not re.search(r"\\(?:[A-Za-z]+|[%,])|[_^{}=<>+*/-]", inline)
                and not re.fullmatch(r"(?:[\d.,]+|[A-Za-z]|\([A-Za-z]\))", inline.strip())):
            return match[0]  # Dollar amounts/variables such as $5 and $10, $PATH.
    try:
        return _math_text(formula)
    except ValueError:
        return match[0]


def _tables(text: str) -> str:
    """Turn supported Markdown tables into labelled rows for narrow screens."""
    def cells(line: str) -> list[str]:
        return [cell.strip() for cell in line.strip().strip("|").split("|")]

    lines = text.split("\n")
    output = []
    index = 0
    while index < len(lines):
        header = cells(lines[index])
        separator = cells(lines[index + 1]) if index + 1 < len(lines) else []
        if len(header) < 2 or len(header) != len(separator) or not all(
                re.fullmatch(r":?-{3,}:?", cell) for cell in separator):
            output.append(lines[index])
            index += 1
            continue
        index += 2
        rows = []
        while index < len(lines) and "|" in lines[index]:
            row = cells(lines[index])
            if len(row) != len(header):
                break
            rows.append("\n".join(f"{label}：{value}" for label, value in zip(header, row)))
            index += 1
        output.append("\n\n".join(rows) if rows else " / ".join(header))
    return "\n".join(output)


def format_channel_text(text: str) -> str:
    """Strip presentation syntax, preserving code, links, paths and amounts."""
    protected = []
    prefix = "\x00XIAOACODE"
    while prefix in text:
        prefix += "X"

    def store(value: str) -> str:
        protected.append(value)
        return f"{prefix}{len(protected) - 1}\x00"

    # Protect literals before any presentation rules; tokens cannot collide with input.
    text = re.sub(r"```(?:[\w+.-]*[ \t]*\n)?([\s\S]*?)```|`([^`\n]+)`",
                  lambda m: store(m[1] if m[1] is not None else m[2]), text)
    # Keep rendered comparisons (e.g. > 4 m) out of quote/list Markdown rules.
    text = _MATH.sub(lambda m: m[0] if prefix in m[0] else store(_replace_math(m)), text)
    text = re.sub(r"(?:https?://|[A-Za-z]:[\\/])[^\s<>`\x00]+", lambda m: store(m[0]), text)
    text = re.sub(r"\*\*([^*]+)\*\*|__([^_]+)__", lambda m: m[1] or m[2], text)
    text = _tables(text)
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.I)
    text = re.sub(r"^[ \t]{0,3}#{1,6}[ \t]+", "", text, flags=re.M)
    text = re.sub(r"^[ \t]*[-*][ \t]+", "· ", text, flags=re.M)
    text = re.sub(r"^[ \t]*>[ \t]?", "", text, flags=re.M)
    return re.sub(re.escape(prefix) + r"(\d+)\x00", lambda m: protected[int(m[1])], text)
