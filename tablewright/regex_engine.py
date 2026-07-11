import re

from lark import Lark, Transformer
from lark.exceptions import UnexpectedInput, VisitError

from .chartools import (_fold_case, _REGEX_CONTROL_ESCAPES, _REGEX_DIGIT_CHARS,
    _REGEX_SPACE_CHARS, _REGEX_WORD_CHARS)

# ======================================================================== #
# A small regex engine: Lark regexes parsed into grammar ASTs
# ======================================================================== #

# Tablewright's own regular-expression parser. The Lark frontend meets
# regexes inside terminal definitions (``WORD: /[a-z]+/``) and inline in
# rules; translating them into EDS -- which knows atoms, ``[[...]]``
# character sets, ``"strings"``, ``+``/``*`` repetition and ``|``
# alternation -- requires understanding the pattern structurally, not
# textually. This parser produces the same ``("seq" / "alt" / "quant" /
# "charset")`` node language the external frontends lower, so a parsed
# pattern drops straight into the EDS emitter.
#
# Only the language-defining subset is accepted. Constructs that select
# match *positions* rather than characters -- anchors, word boundaries,
# lookarounds, backreferences -- have no counterpart in a context-free
# rule and are rejected with a pointed error instead of being silently
# mistranslated. Greedy/lazy markers are accepted and ignored (they change
# which match is *preferred*, never which strings are *matched*), while
# possessive quantifiers are rejected (they do change the language).

class RegexSyntaxError(ValueError):
    """A regular expression that cannot be parsed or translated to a grammar."""

    def __init__(self, pattern: str, position: int, message: str):
        caret = " " * position + "^"
        super().__init__(f"{message}\n  /{pattern}/\n   {caret}")
        self.pattern = pattern
        self.position = position

def _repeat_node(node, minimum: int, maximum):
    """Expand a counted repetition into plain sequence/option/star nodes.

    ``X{2,4}`` becomes ``X X (X (X)?)?`` -- the mandatory copies followed by a
    right-nested chain of optionals -- and ``X{2,}`` becomes ``X X X*``. The
    result recognizes exactly the counted language using only the constructs
    EDS can express.

    Args:
        node: The repeated AST node.
        minimum: The minimum number of copies.
        maximum: The maximum number of copies, or ``None`` for unbounded.

    Returns:
        The expanded AST node.
    """
    copies = [node] * minimum
    if maximum is None:
        copies.append(("quant", node, "*"))
    else:
        optional = None
        for _ in range(maximum - minimum):
            inner = node if optional is None else ("seq", [node, optional])
            optional = ("quant", inner, "?")
        if optional is not None:
            copies.append(optional)
    if not copies:
        return ("seq", [])
    if len(copies) == 1:
        return copies[0]
    return ("seq", copies)


# The largest counted repetition worth expanding into copies. Grammars with
# genuinely huge counts would explode the rule set; refuse early.
_MAX_COUNTED_REPEAT = 512


def _strip_verbose(pattern: str) -> str:
    """Apply the /x flag: drop unescaped whitespace and # comments."""
    out = []
    in_class = False
    index = 0
    while index < len(pattern):
        char = pattern[index]
        if char == "\\" and index + 1 < len(pattern):
            out.append(pattern[index:index + 2])
            index += 2
            continue
        if in_class:
            out.append(char)
            in_class = char != "]"
            index += 1
            continue
        if char == "[":
            in_class = True
            out.append(char)
            index += 1
            continue
        if char == "#":
            while index < len(pattern) and pattern[index] != "\n":
                index += 1
            continue
        if char in " \t\n\r\f\v":
            index += 1
            continue
        out.append(char)
        index += 1
    return "".join(out)


# Tablewright's regex dialect, as a Lark grammar (Lark parsing regexes).
# This is the counterpart of the derived document grammar below: at the
# document level a regex stays ONE token -- %ignore must never reach inside
# a pattern, and token extent is a lexical property -- and the token's body
# is then parsed with this grammar, where every character is significant
# (note: no %ignore here, ever).
#
# The grammar deliberately PARSES the untranslatable constructs -- anchors,
# word boundaries, backreferences, lookaround/flag group modifiers,
# possessive markers -- so the transformer can reject each with a message
# saying why it cannot become a grammar rule, instead of a bare syntax
# error. Ambiguities are decided the way Python's re does: a well-formed
# {n,m} is a quantifier, not three literals (postfix.2); 'a-z' inside a
# class is a span, not three members (krange.2); a leading '^' negates
# (negclass.2, write '\^' for the literal).
_REGEX_LARK_GRAMMAR = r"""
regexp: alternation

?alternation: sequence (_PIPE sequence)*

sequence: term*

term: factor postfix?

postfix.2: QUANT MODE?
         | COUNT MODE?

?factor: group
       | charclass
       | dot
       | anchor
       | backref
       | escape
       | char

group: _LPAR GROUPMOD? alternation _RPAR
dot: _DOT
anchor: ANCHOR
backref: BACKREF
escape: HEX2 | HEX4 | HEX8 | ESC
char: CHAR | BRACE

?charclass: negclass | posclass
negclass.2: _LBRACK _KCARET kbody _RBRACK
posclass: _LBRACK kbody _RBRACK

kbody: kfirst kitem*
     | kitem+
kfirst: KFIRSTBRACKET
?kitem: krange | katom | kdash
krange.2: katom _KDASH katom
kdash: KDASH
katom: kescape | KCHAR
kescape: HEX2 | HEX4 | HEX8 | ESC

_PIPE: "|"
_LPAR: "("
_RPAR: ")"
_DOT: "."
_LBRACK: "["
_RBRACK: "]"
_KCARET: "^"
_KDASH: "-"
KDASH: "-"
KFIRSTBRACKET: "]"
ANCHOR.2: /[\^$]/ | /\\[ABZb]/
BACKREF.2: /\\[1-9][0-9]*/
GROUPMOD: /\?(?:[:=!]|<[=!]|P<[A-Za-z_][A-Za-z0-9_]*>|P=[A-Za-z_][A-Za-z0-9_]*|#[^)]*|[a-zA-Z]+(?:-[a-zA-Z]+)?(?=[:)]))/
QUANT: /[*+?]/
MODE: /[?+]/
COUNT.2: /\{[0-9]+(,[0-9]*)?\}/
HEX2.3: /\\x[0-9a-fA-F]{2}/
HEX4.3: /\\u[0-9a-fA-F]{4}/
HEX8.3: /\\U[0-9a-fA-F]{8}/
ESC: /\\./
CHAR: /[^\\|()\[*+?.^${]/
BRACE: "{"
KCHAR: /[^\]\\\-]/
"""

_REGEX_PARSER = Lark(_REGEX_LARK_GRAMMAR, start="regexp", parser="earley",
                     lexer="dynamic", maybe_placeholders=False)


class _RegexAstTransformer(Transformer):
    """Turn the regex grammar's parse tree into the frontend AST.

    Every method mirrors one rule of :data:`_REGEX_LARK_GRAMMAR` and builds
    the ``("seq" / "alt" / "quant" / "charset")`` node language the EDS
    emitter consumes -- this is where the ``i`` (fold characters) and ``s``
    (widen the dot) flags apply, and where the parse-but-untranslatable
    constructs are rejected with positions taken from their tokens.
    """

    def __init__(self, pattern: str, ignorecase: bool, dotall: bool):
        super().__init__()
        self.pattern_text = pattern
        self.ignorecase = ignorecase
        self.dotall = dotall

    def _err(self, token, message: str) -> RegexSyntaxError:
        position = getattr(token, "start_pos", None) or 0
        return RegexSyntaxError(self.pattern_text, position, message)

    def _chars(self, chars) -> frozenset:
        return frozenset(_fold_case(chars) if self.ignorecase else chars)

    # --- leaves ---------------------------------------------------------- #

    def char(self, children):
        return ("charset", self._chars({str(children[0])}), False)

    def dot(self, _children):
        if self.dotall:
            # truly any character: the complement of nothing has no EDS
            # spelling, so say "anything but newline, or a newline"
            return ("alt", [("charset", frozenset("\n"), True),
                            ("charset", frozenset("\n"), False)])
        return ("charset", frozenset("\n"), True)

    def anchor(self, children):
        token = children[0]
        if str(token) == r"\b":
            raise self._err(token, r"the word boundary \b has no meaning "
                                   "in a grammar rule")
        raise self._err(
            token,
            f"the anchor '{token}' has no meaning in a grammar rule; remove "
            "it (grammar symbols already match whole tokens)")

    def backref(self, children):
        raise self._err(children[0], "backreferences are not regular and "
                                     "cannot become grammar rules")

    def _decode_escape(self, token, in_class: bool):
        """Decode one escape token to ``("char", c)`` or ``("set", (chars, neg))``."""
        text = str(token)
        if token.type in {"HEX2", "HEX4", "HEX8"}:
            code_point = int(text[2:], 16)
            if code_point > 0x10FFFF:
                raise self._err(token, f"{text} is beyond the last code "
                                       "point U+10FFFF")
            return ("char", chr(code_point))
        escape = text[1]
        if escape == "d":
            return ("set", (_REGEX_DIGIT_CHARS, False))
        if escape == "D":
            return ("set", (_REGEX_DIGIT_CHARS, True))
        if escape == "w":
            return ("set", (_REGEX_WORD_CHARS, False))
        if escape == "W":
            return ("set", (_REGEX_WORD_CHARS, True))
        if escape == "s":
            return ("set", (_REGEX_SPACE_CHARS, False))
        if escape == "S":
            return ("set", (_REGEX_SPACE_CHARS, True))
        if escape in _REGEX_CONTROL_ESCAPES:
            return ("char", _REGEX_CONTROL_ESCAPES[escape])
        if escape == "b":
            if in_class:
                return ("char", "\x08")
            raise self._err(token, r"the word boundary \b has no meaning "
                                   "in a grammar rule")
        if escape in "ABZ":
            raise self._err(token, f"the anchor \\{escape} has no meaning "
                                   "in a grammar rule")
        if escape == "N":
            raise self._err(token, r"named escapes \N{...} are not supported")
        if escape in "xuU":
            width = {"x": 2, "u": 4, "U": 8}[escape]
            raise self._err(token,
                            f"\\{escape} needs exactly {width} hex digits")
        if escape.isdigit():
            raise self._err(token, "backreferences are not regular and "
                                   "cannot become grammar rules")
        return ("char", escape)

    def escape(self, children):
        kind, payload = self._decode_escape(children[0], in_class=False)
        if kind == "set":
            chars, negated = payload
            return ("charset", self._chars(chars), negated)
        return ("charset", self._chars({payload}), False)

    # --- structure ------------------------------------------------------- #

    def regexp(self, children):
        return children[0]

    def alternation(self, children):
        return ("alt", list(children))

    def sequence(self, children):
        # empty groups -- (?#comments), () -- contribute nothing
        items = [child for child in children if child != ("seq", [])]
        if len(items) == 1:
            return items[0]
        return ("seq", items)

    def postfix(self, children):
        quantifier = children[0]
        mode = children[1] if len(children) > 1 else None
        if mode is not None and str(mode) == "+":
            raise self._err(mode, "possessive quantifiers change the "
                                  "matched language and are not supported")
        return ("postfix-op", quantifier)

    def term(self, children):
        node = children[0]
        if len(children) == 1:
            return node
        quantifier = children[1][1]
        if quantifier.type == "QUANT":
            return ("quant", node, str(quantifier))
        match = re.fullmatch(r"\{(\d+)(?:,(\d*))?\}", str(quantifier))
        minimum = int(match.group(1))
        if match.group(2) is None:
            maximum = minimum
        elif match.group(2):
            maximum = int(match.group(2))
        else:
            maximum = None
        if maximum is not None and maximum < minimum:
            raise self._err(quantifier, "bad repeat interval: max is below min")
        if max(minimum, maximum or 0) > _MAX_COUNTED_REPEAT:
            raise self._err(quantifier,
                            f"counted repetition beyond {_MAX_COUNTED_REPEAT} "
                            "would explode the grammar")
        return _repeat_node(node, minimum, maximum)

    def group(self, children):
        modifier = None
        body = children[-1]
        if len(children) > 1:
            modifier = children[0]
        if modifier is None:
            return body
        text = str(modifier)
        if text in {"?=", "?!", "?<=", "?<!"}:
            raise self._err(modifier, "lookarounds cannot be translated "
                                      "to a grammar")
        if text == "?:" or (text.startswith("?P<") and text.endswith(">")):
            return body
        if text.startswith("?#"):
            return ("seq", [])
        if text.startswith("?P="):
            raise self._err(modifier, "backreferences are not regular and "
                                      "cannot become grammar rules")
        raise self._err(modifier, "unsupported (?...) group (inline flags, "
                                  "conditionals and lookarounds are not "
                                  "translatable)")

    # --- character classes ------------------------------------------------ #
    # katom/kfirst/kdash return (payload, position) pairs so class-level
    # errors can still point into the pattern after transformation.

    def kfirst(self, children):
        return ("]", children[0].start_pos)

    def kdash(self, children):
        return ("-", children[0].start_pos)

    def katom(self, children):
        child = children[0]
        if isinstance(child, tuple):  # a kescape result
            return child
        return (str(child), child.start_pos)

    def kescape(self, children):
        token = children[0]
        kind, payload = self._decode_escape(token, in_class=True)
        if kind == "set":
            chars, negated = payload
            if negated:
                raise self._err(token, "a negated shorthand inside [...] is "
                                       "not supported; rewrite the class "
                                       "explicitly")
            return (frozenset(chars), token.start_pos)
        return (payload, token.start_pos)

    def krange(self, children):
        (low, low_pos), (high, _) = children
        if not isinstance(low, str) or not isinstance(high, str):
            raise self._err_at(low_pos, "a class shorthand cannot bound a range")
        if ord(low) > ord(high):
            raise self._err_at(low_pos,
                               f"range start {low!r} is after end {high!r}")
        return (frozenset(chr(code) for code in range(ord(low), ord(high) + 1)),
                low_pos)

    def _err_at(self, position: int, message: str) -> RegexSyntaxError:
        return RegexSyntaxError(self.pattern_text, position or 0, message)

    def kbody(self, children):
        chars = set()
        for payload, _ in children:
            if isinstance(payload, str):
                chars.add(payload)
            else:
                chars.update(payload)
        return chars

    def posclass(self, children):
        return ("charset", self._chars(children[0]), False)

    def negclass(self, children):
        return ("charset", self._chars(children[0]), True)


def parse_regex(pattern: str, flags: str = ""):
    r"""Parse a regular expression into the frontend grammar AST.

    The pattern is parsed with :data:`_REGEX_LARK_GRAMMAR` -- Tablewright's
    own Lark grammar for the regex dialect -- and the tree is transformed
    into the same node language every frontend lowers, so a parsed pattern
    drops straight into the EDS emitter and from there into the C++ table.

    The supported subset is the language-defining core of Python/Lark
    regexes: literals, ``.``, character classes (ranges, negation, the
    ``\d \w \s`` shorthands), ``\xNN``/``\uNNNN``/``\UNNNNNNNN`` escapes,
    grouping (plain, non-capturing and named), alternation, the ``? * +``
    quantifiers and counted ``{n}``/``{n,}``/``{n,m}`` repetition, plus the
    ``i``, ``s`` and ``x`` flags. Anchors, word boundaries, lookarounds,
    backreferences, possessive quantifiers and inline flags are rejected
    with a :class:`RegexSyntaxError` explaining why.

    Args:
        pattern: The pattern text (without the surrounding slashes).
        flags: Trailing flag letters (as in ``/.../ims``).

    Returns:
        An AST in the frontend node language: ``("seq", [...])``,
        ``("alt", [...])``, ``("quant", node, op)`` and
        ``("charset", frozenset, negated)``.

    Raises:
        RegexSyntaxError: For syntax errors and untranslatable constructs.
    """
    for flag in flags:
        if flag not in "imsxlu":
            raise RegexSyntaxError(pattern, 0, f"unknown regex flag {flag!r}")
    if "l" in flags:
        raise RegexSyntaxError(pattern, 0,
                               "the locale flag /l has no compile-time meaning")
    text = _strip_verbose(pattern) if "x" in flags else pattern
    try:
        tree = _REGEX_PARSER.parse(text)
    except UnexpectedInput as exc:
        position = max((getattr(exc, "pos_in_stream", 0) or 0), 0)
        raise RegexSyntaxError(
            text, min(position, len(text)),
            "cannot parse the pattern here (unbalanced or incomplete "
            "syntax)") from exc
    try:
        return _RegexAstTransformer(text, "i" in flags,
                                    "s" in flags).transform(tree)
    except VisitError as exc:
        if isinstance(exc.orig_exc, RegexSyntaxError):
            raise exc.orig_exc from None
        raise
