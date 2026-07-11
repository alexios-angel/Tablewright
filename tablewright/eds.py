import re

from .chartools import (_decode_quoted_literal, _fold_case, _split_regex_literal,
    decompose_into_runs)
from .regex_engine import parse_regex

# ======================================================================== #
# EDS emission: stringify frontend ASTs back into the .gram dialect
# ======================================================================== #

# Characters that must be escaped when emitted as a rule-body atom or a set
# member: the structural syntax of the .gram dialect plus the comment mark.
_EDS_STRUCTURAL_CHARS = frozenset(',(){}<>[]|"*+@\\#')
# Structural characters of a [[...]] range body.
_EDS_RANGE_STRUCTURAL_CHARS = frozenset("]^-\\")
_EDS_NAMED_ESCAPES = {
    "\n": r"\n", "\t": r"\t", "\r": r"\r", "\f": r"\f",
    "\v": r"\v", "\0": r"\0", "\a": r"\a", "\b": r"\b",
}
# Names with a fixed meaning somewhere in the EDS pipeline: 'epsilon' and
# 'empty'/'sigma' in the dialect itself, 'other' as the auto-computed
# catch-all terminal.
_EDS_RESERVED_NAMES = frozenset({"epsilon", "empty", "sigma", "other"})
_EDS_NAME_RE = re.compile(r"[a-zA-Z][a-zA-Z_0-9]+")


def _eds_escape_char(char: str, structural=_EDS_STRUCTURAL_CHARS) -> str:
    """Spell one character safely for an EDS atom / set member / range body."""
    if char in _EDS_NAMED_ESCAPES:
        return _EDS_NAMED_ESCAPES[char]
    code = ord(char)
    if code < 0x20 or code == 0x7F:
        return f"\\x{code:02x}"
    if code > 0x7E:
        return f"\\u{{{code:x}}}"
    if char == " ":
        # '\ ' would not lex (ATOM never spans whitespace); spell the code point
        return r"\x20"
    if char in structural:
        return "\\" + char
    return char


def _eds_string(text: str) -> str:
    """Spell ``text`` as an EDS ``"..."`` string literal."""
    parts = []
    for char in text:
        if char == '"':
            parts.append('\\"')
        elif char == "\\":
            parts.append("\\\\")
        elif char in _EDS_NAMED_ESCAPES:
            parts.append(_EDS_NAMED_ESCAPES[char])
        elif ord(char) < 0x20 or ord(char) == 0x7F:
            parts.append(f"\\x{ord(char):02x}")
        elif ord(char) > 0x7E:
            parts.append(f"\\u{{{ord(char):x}}}")
        else:
            parts.append(char)
    return '"' + "".join(parts) + '"'


def _eds_range_token(chars, negated: bool) -> str:
    """Spell a character set as an EDS ``[[...]]`` / ``[[^...]]`` range."""
    ranges, residual = decompose_into_runs(chars, min_range_len=3)
    items = [(lo, f"{_eds_escape_char(lo, _EDS_RANGE_STRUCTURAL_CHARS)}-"
                  f"{_eds_escape_char(hi, _EDS_RANGE_STRUCTURAL_CHARS)}")
             for lo, hi in ranges]
    items += [(char, _eds_escape_char(char, _EDS_RANGE_STRUCTURAL_CHARS))
              for char in residual]
    body = "".join(spelling for _, spelling in sorted(items, key=lambda i: ord(i[0][0])))
    return f"[[^{body}]]" if negated else f"[[{body}]]"


def _eds_charset_token(chars, negated: bool) -> str:
    """Spell a charset as the shortest applicable EDS symbol."""
    if not negated and len(chars) == 1:
        return _eds_escape_char(next(iter(chars)))
    return _eds_range_token(chars, negated)

class _EdsEmitter:
    """Stringify frontend grammar ASTs into the native EDS dialect.

    This is the shared back half of every external frontend: the EBNF and
    Lark readers produce ASTs in one small node language -- ``("name", n)``,
    ``("literal", tok)``, ``("literal_range", lo, hi)``, ``("charset",
    chars, negated)``, ``("seq", [...])``, ``("alt", [...])`` and
    ``("quant", node, op)`` -- and this class serializes those ASTs to
    ``.gram`` text, inventing ``tw_*`` helper nonterminals for the shapes
    EDS cannot spell inline (alternation groups and ``?`` optionality).
    Regex literals are parsed with :func:`parse_regex` and their ASTs are
    emitted through the same path.
    """

    def __init__(self, nonterminals=(), terminal_names=()):
        self.nonterminals = set(nonterminals)
        self.terminal_names = set(terminal_names)
        self.generated = []
        self.counter = 0

    def helper(self, node, suffix: str) -> str:
        """Emit ``node`` as a fresh helper nonterminal and return its name."""
        self.counter += 1
        name = f"tw_{suffix}_{self.counter}"
        alternatives = self.emit_alternatives(node)
        self.generated.append(f"{name} -> {' | '.join(alternatives)}")
        self.nonterminals.add(name)
        return name

    def emit_item(self, node) -> list:
        """Serialize one AST node into a list of EDS rule-body tokens."""
        kind = node[0]
        if kind == "name":
            name = node[1]
            if name in {"epsilon", "empty"}:
                return ["epsilon"]
            return [f"<{name}>" if name in self.nonterminals else name]
        if kind == "literal":
            return self._emit_literal(node[1])
        if kind == "text":  # raw characters, no escape decoding
            content = node[1]
            if not content:
                return ["epsilon"]
            if len(content) == 1:
                return [_eds_escape_char(content)]
            return [_eds_string(content)]
        if kind == "literal_range":
            low, _ = _decode_quoted_literal(node[1])
            high, _ = _decode_quoted_literal(node[2])
            if len(low) != 1 or len(high) != 1:
                raise ValueError(
                    f"the range {node[1]}..{node[2]} needs single-character "
                    "endpoints")
            chars = frozenset(chr(code)
                              for code in range(ord(low), ord(high) + 1))
            return [_eds_charset_token(chars, False)]
        if kind == "charset":
            return [_eds_charset_token(node[1], node[2])]
        if kind in {"seq", "alt"}:
            return [f"<{self.helper(node, 'group')}>"]
        if kind == "quant":
            base, quantifier = node[1], node[2]
            if quantifier == "?":
                optional = self.helper(("alt", [base, ("seq", [])]), "optional")
                return [f"<{optional}>"]
            tokens = self.emit_item(base)
            if len(tokens) == 1 and tokens[0] != "epsilon":
                # every single token is a valid quant_base: an atom, a
                # "string", a [[range]], a bare terminal or a <nonterminal>
                return [tokens[0] + quantifier]
            repeated = self.helper(base, "repeat")
            return [f"<{repeated}>{quantifier}"]
        raise ValueError(f"unsupported expression node {kind!r}")

    def _emit_literal(self, token: str) -> list:
        if token.startswith("/"):
            pattern, flags = _split_regex_literal(token)
            return self.emit_item(parse_regex(pattern, flags))
        text, insensitive = _decode_quoted_literal(token)
        if not text:
            return ["epsilon"]
        if insensitive:
            return [_eds_charset_token(frozenset(_fold_case({char})), False)
                    for char in text]
        if len(text) == 1:
            return [_eds_escape_char(text)]
        return [_eds_string(text)]

    def emit_alternatives(self, node) -> list:
        if node[0] == "alt":
            return [self.emit_sequence(branch) for branch in node[1]]
        return [self.emit_sequence(node)]

    def emit_sequence(self, node) -> str:
        items = node[1] if node[0] == "seq" else [node]
        emitted = [token for item in items for token in self.emit_item(item)]
        return ", ".join(emitted) if emitted else "epsilon"

    def stringify_rules(self, rules) -> list:
        """Serialize ``(name, ast)`` rules plus any helpers they spawned."""
        lines = [f"{name} -> {' | '.join(self.emit_alternatives(node))}"
                 for name, node in rules]
        return lines + self.generated

def _allocate_eds_names(names) -> dict:
    """Map every Lark rule/terminal name onto a legal, unique EDS name.

    EDS references require ``[a-zA-Z][a-zA-Z_0-9]+`` (two or more characters,
    no leading underscore) and a few names are reserved by the dialect and
    the pipeline; anything unusable keeps its spelling behind a ``tw_``
    prefix.
    """
    mapping = {}
    used = set(_EDS_RESERVED_NAMES)
    for name in names:
        candidate = name
        if not _EDS_NAME_RE.fullmatch(candidate) or candidate in used:
            candidate = f"tw_{name}"
            serial = 2
            while candidate in used or not _EDS_NAME_RE.fullmatch(candidate):
                candidate = f"tw_{name}_{serial}"
                serial += 1
        mapping[name] = candidate
        used.add(candidate)
    return mapping


def _rename_ast(node, mapping: dict):
    """Apply a name mapping across a frontend AST."""
    kind = node[0]
    if kind == "name":
        return ("name", mapping.get(node[1], node[1]))
    if kind in {"seq", "alt"}:
        return (kind, [_rename_ast(child, mapping) for child in node[1]])
    if kind == "quant":
        return ("quant", _rename_ast(node[1], mapping), node[2])
    return node


def _collect_referenced_names(node, into: set):
    """Collect every ``("name", ...)`` reference in a frontend AST."""
    kind = node[0]
    if kind == "name":
        into.add(node[1])
    elif kind in {"seq", "alt"}:
        for child in node[1]:
            _collect_referenced_names(child, into)
    elif kind == "quant":
        _collect_referenced_names(node[1], into)
