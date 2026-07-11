import argparse

from typing import Optional

from .analysis import (compute_first, compute_follow, construct_parse_table,
    normalize_grammar_keys, stringify_first_follow, stringify_parse_table)
from .chartools import _MIN_RANGE_SET_SIZE, decompose_into_runs
from .logutil import logger, trace
from .symbols import describe_grammar, GrammerType, IdentifierTable, SymbolType
from .transforms import inline_pure_terminal_nonterminals, optimize_grammar

# ======================================================================== #
# CTLL rendering: turning the parse table into a C++ header
# ======================================================================== #

# Readable identifier fragments for punctuation characters, so a single-character
# terminal with no gram name still yields a valid, legible C++ identifier
# (e.g. ``ctll::term<'('>`` -> alias ``open``). Where pcre.gram already names these
# characters the gram name wins; this map is only the fallback.
_PUNCT_NAMES = {
    "(": "open", ")": "close", "[": "sopen", "]": "sclose",
    "{": "copen", "}": "cclose", "<": "angle_open", ">": "angle_close",
    ".": "dot", "/": "slash", "\\": "backslash", "$": "dolar",
    "?": "questionmark", ":": "colon", "+": "plus", "*": "star",
    ",": "comma", "|": "pipe", "^": "caret", "-": "minus",
    "=": "equal_sign", "!": "exclamation_mark", '"': "doublequote",
    "_": "underscore", "@": "at", "#": "hash", "%": "percent",
    "&": "ampersand", "'": "quote", ";": "semicolon", "~": "tilde",
    "`": "backtick", " ": "space",
}


def _identifier_for_char(char: str) -> str:
    """Return a valid C++ identifier fragment naming a single character.

    Letters name themselves; digits become ``d0``..``d9`` (a bare digit is not a
    valid identifier); punctuation uses :data:`_PUNCT_NAMES`; anything else falls
    back to a hex form like ``x0a``.
    """
    if char.isalpha():
        return char
    if char.isdigit():
        return "d" + char
    if char in _PUNCT_NAMES:
        return _PUNCT_NAMES[char]
    return "x" + format(ord(char), "02x")


# Identifiers that must never be used as a terminal alias because they are C++
# keywords (the language reserves them) or near-universal platform/library
# typedefs (``uchar`` is ``unsigned char`` in OpenCV/Qt/Windows, etc.). A gram
# name that lands on one of these is prefixed with ``terminal_`` so the generated
# header compiles everywhere. The set covers the C++23 keyword list plus the
# common fixed-width integer typedef shorthands.
_CPP_RESERVED_WORDS = frozenset({
    # C++ keywords
    "alignas", "alignof", "and", "and_eq", "asm", "atomic_cancel",
    "atomic_commit", "atomic_noexcept", "auto", "bitand", "bitor", "bool",
    "break", "case", "catch", "char", "char8_t", "char16_t", "char32_t",
    "class", "compl", "concept", "const", "consteval", "constexpr", "constinit",
    "const_cast", "continue", "co_await", "co_return", "co_yield", "decltype",
    "default", "delete", "do", "double", "dynamic_cast", "else", "enum",
    "explicit", "export", "extern", "false", "float", "for", "friend", "goto",
    "if", "inline", "int", "long", "mutable", "namespace", "new", "noexcept",
    "not", "not_eq", "nullptr", "operator", "or", "or_eq", "private",
    "protected", "public", "reflexpr", "register", "reinterpret_cast",
    "requires", "return", "short", "signed", "sizeof", "static",
    "static_assert", "static_cast", "struct", "switch", "synchronized",
    "template", "this", "thread_local", "throw", "true", "try", "typedef",
    "typeid", "typename", "union", "unsigned", "using", "virtual", "void",
    "volatile", "wchar_t", "while", "xor", "xor_eq",
    # Common fixed-width / platform integer typedef shorthands.
    "uchar", "schar", "ushort", "uint", "ulong", "ullong", "llong",
    "uint8_t", "uint16_t", "uint32_t", "uint64_t",
    "int8_t", "int16_t", "int32_t", "int64_t",
    "size_t", "ssize_t", "ptrdiff_t", "intptr_t", "uintptr_t",
    "byte", "wchar", "uint128_t", "int128_t",
})


def _safe_identifier(name: str) -> str:
    """Return ``name`` made safe to use as a C++ identifier.

    A name colliding with a C++ keyword or a common platform typedef (see
    :data:`_CPP_RESERVED_WORDS`) is prefixed with ``terminal_`` so it cannot clash
    with the language or ubiquitous library types.
    """
    if name in _CPP_RESERVED_WORDS:
        return "terminal_" + name
    return name


# A synthesized set is named after the parent terminals whose union it is, but
# only when that name stays reasonably short. Sets that decompose into many
# pieces (e.g. a large named class plus a dozen stray punctuation atoms) would
# otherwise produce 200-character identifiers, so beyond this length the emitter
# falls back to a compact ``set_<n>`` name.
_MAX_COMPOSED_SET_NAME = 60


class TerminalAliaser:
    """Assigns stable ``using`` alias names to terminal types and emits them.

    Every distinct rendered terminal (a ``ctll::term<...>`` / ``ctll::set<...>`` /
    ``ctll::neg_set<...>`` string) is mapped to a single C++ identifier so the rules
    can reference terminals by name and the actual types live once in a central
    ``// TERMINALS`` block.

    Naming priority for a terminal:

    1. the global negative "other" set is always ``_others``;
    2. a character set that matches a set defined in the ``.gram`` takes that gram
       name (when several gram names share a character set, the first declared one
       wins, deterministically);
    3. a single-character terminal with no gram name is named from the character
       (letters/digits as themselves, punctuation via :data:`_PUNCT_NAMES`);
    4. any remaining unnamed set is given a synthetic, stable ``set_<n>`` name in
       order of first appearance.

    Collisions are impossible by construction: a name already bound to a different
    type gets a numeric suffix.
    """

    def __init__(self, terminal_table: dict, others_name: str = "_others",
                 reserved: set = None, use_ranges: bool = False):
        """Build the aliaser from the grammar's terminal table.

        Args:
            terminal_table: The terminal section of the identifier table (maps a
                gram name to its :class:`GrammerType`); ``other`` is the global
                negative set.
            others_name: The alias to use for the global negative set.
            reserved: Names already taken in the emitted ``struct`` scope (the
                nonterminal and semantic-action names, plus ``_start``). A terminal
                alias that would collide with one of these is disambiguated, so the
                generated C++ never has a ``using`` clash with a ``struct``.
            use_ranges: When True, positive lookaheads are decomposed into
                ``ctll::range`` runs plus a residual instead of one wide
                ``ctll::set``, trading a few extra rule overloads for far fewer
                compile-time character comparisons.
        """
        self.others_name = others_name
        self.reserved = set(reserved or ())
        self.use_ranges = use_ranges
        # Map a frozenset of characters -> canonical gram name (first declared),
        # and keep the named positive multi-character sets for composing names of
        # synthesized (factored) sets out of their parent terminals.
        self._charset_to_gram_name = {}
        self._named_multichar_sets = []   # list of (name, frozenset) for |set| > 1
        for name, gt in terminal_table.items():
            if name == "other":
                continue
            key = frozenset(gt.value)
            if key not in self._charset_to_gram_name:
                self._charset_to_gram_name[key] = name
            if gt.type != SymbolType.negitive_set and len(key) > 1:
                self._named_multichar_sets.append((name, key))

        self._type_to_alias = {}     # rendered type string -> alias identifier
        self._alias_to_type = {}     # alias identifier -> rendered type string
        self._ordered = []           # (alias, type) in first-seen order
        self._set_counter = 0

    def _unique(self, base: str, type_string: str) -> str:
        """Return a free identifier derived from ``base``.

        The base is first made keyword-safe (a C++ keyword or common platform
        typedef is prefixed with ``terminal_``); it is then returned unchanged if
        free; a collision with a reserved name (a nonterminal/action) adds a
        trailing underscore; any remaining clash with another terminal alias gets
        a numeric suffix.
        """
        base = _safe_identifier(base)
        if base in self.reserved:
            base = base + "_"
        if self._alias_to_type.get(base) in (None, type_string) and base not in self.reserved:
            return base
        i = 2
        while (self._alias_to_type.get(f"{base}{i}") not in (None, type_string)
               or f"{base}{i}" in self.reserved):
            i += 1
        return f"{base}{i}"

    def _compose_set_name(self, charset) -> str:
        """Name a synthesized (factored) set from the terminals that compose it.

        The set's characters are covered greedily by the named multi-character
        gram sets that are subsets of it (largest first); any character left over
        is named individually (a letter as itself, a digit as ``d<digit>``,
        punctuation via :data:`_PUNCT_NAMES`). The chosen pieces are joined with
        ``__`` in ascending order of their lowest character, giving names like
        ``set_control_chars__capture_control_chars`` or ``c__i__m__s``.

        Args:
            charset: The synthesized set's characters.

        Returns:
            A descriptive identifier fragment (not yet made unique/keyword-safe).
        """
        target = set(charset)
        remaining = set(target)
        chosen = []  # (sort_key, name)
        for name, members in sorted(self._named_multichar_sets,
                                    key=lambda nm: (-len(nm[1]), nm[0])):
            if members and members <= remaining:
                chosen.append((min(ord(c) for c in members), name))
                remaining -= members
        # Any leftover characters are named one by one.
        for char in remaining:
            chosen.append((ord(char), _identifier_for_char(char)))
        chosen.sort(key=lambda pair: pair[0])
        return "__".join(name for _key, name in chosen)

    def _base_from_charset(self, charset: frozenset) -> str:
        """Derive an alias base name from a set of member characters.

        A set matching a ``.gram``-declared terminal reuses that name; a single
        character is named after itself; otherwise the set is named after the
        parent terminals whose union it is (a synthesized/factored set), falling
        back to a short ``set_<n>`` when that composition would be unwieldy.
        """
        if charset in self._charset_to_gram_name:
            return self._charset_to_gram_name[charset]
        if len(charset) == 1:
            return _identifier_for_char(next(iter(charset)))
        composed = self._compose_set_name(charset)
        if len(composed) <= _MAX_COMPOSED_SET_NAME:
            return composed
        self._set_counter += 1
        return f"set_{self._set_counter}"

    def alias_for(self, type_string: str, chars, is_neg: bool, gram_name: str = None) -> str:
        """Return the alias for a rendered terminal, recording it if new.

        Args:
            type_string: The rendered terminal, e.g. ``ctll::term<'a'>``.
            chars: The terminal's member characters.
            is_neg: Whether the terminal is a negative set.
            gram_name: For a negative set, the name it was defined under in the
                ``.gram`` (``"other"`` for the implicit global set, or a user name
                like ``uchar``); ``None`` for an anonymous inline ``[[^...]]``
                range, which is named ``not_<members>``. Positive terminals leave
                this ``None`` and are named from their character set instead.

        Returns:
            The C++ identifier to use in place of ``type_string``.
        """
        existing = self._type_to_alias.get(type_string)
        if existing is not None:
            return existing

        charset = frozenset(chars)
        if is_neg:
            # The implicit global "other" set is ``_others``; a user-named negative
            # set (e.g. ``uchar``) keeps its own name; an anonymous inline
            # ``[[^...]]`` range is named after its members with a ``not_`` prefix.
            if gram_name == "other":
                base = self.others_name
            elif gram_name is not None:
                base = gram_name
            else:
                base = "not_" + self._base_from_charset(charset)
        else:
            base = self._base_from_charset(charset)

        alias = self._unique(base, type_string)
        self._type_to_alias[type_string] = alias
        self._alias_to_type[alias] = type_string
        self._ordered.append((alias, type_string))
        return alias

    def emit_section(self, indentation: str) -> str:
        """Render the ``// TERMINALS`` block of ``using`` aliases.

        Aliases are listed in first-appearance order. Returns an empty string if no
        terminals were aliased.
        """
        if not self._ordered:
            return ""
        lines = [f"{indentation}using {alias} = {type_string};"
                 for alias, type_string in self._ordered]
        return f"{indentation}// TERMINALS\n" + "\n".join(lines)

    def log_summary(self) -> None:
        """Log how many terminals were aliased, by kind, and (at TRACE) each one."""
        terms = sum(1 for _a, t in self._ordered if t.startswith("ctll::term<"))
        sets = sum(1 for _a, t in self._ordered if t.startswith("ctll::set<"))
        neg = sum(1 for _a, t in self._ordered if t.startswith("ctll::neg_set<"))
        composed = sum(1 for a, _t in self._ordered if "__" in a)
        synthetic = sum(1 for a, _t in self._ordered if a.startswith("set_"))
        logger.debug(
            f"Terminal aliases: {len(self._ordered)} total "
            f"({terms} term, {sets} set, {neg} neg_set; "
            f"{composed} composed names, {synthetic} set_N fallbacks)"
        )
        for alias, type_string in self._ordered:
            trace(f"  alias {alias} = {type_string}")


def cpp_char_literal(char: str) -> str:
    r"""Render one character as a complete C++ character literal.

    The regex-significant brackets ``( ) { }`` are emitted as hex escapes so they
    never interfere with C++'s own ``<>`` / ``{}`` tokenizing; backslash and
    double-quote get the usual C++ escapes; every other printable ASCII character
    is emitted verbatim, and other bytes use a ``\xNN`` escape. The result is the
    full literal *including its quotes*, e.g. ``'a'`` or ``'\x28'``.

    A character whose code point does not fit in a single byte (> 0xFF) cannot be
    written as a narrow ``'...'`` literal -- ``'\x20AC'`` is an out-of-range
    multi-character literal in C++ -- so it is emitted as a ``char32_t`` literal
    ``U'\xNNNN'`` instead. ``ctll::term`` accepts a value of any type, so the wider
    literal is fine inside ``term<...>`` / ``set<...>``.

    Args:
        char: A single character.

    Returns:
        A complete C++ character literal for ``char``.
    """
    special = {"(": r"\x28", ")": r"\x29", "{": r"\x7B", "}": r"\x7D",
               "\\": r"\\", '"': r"\"", "'": r"\'"}
    if char in special:
        return f"'{special[char]}'"
    code_point = ord(char)
    if code_point > 0xFF:
        # Beyond one byte: a narrow literal would be ill-formed, so use char32_t.
        return f"U'\\x{format(code_point, 'X')}'"
    if code_point >= 0x80:
        # High bytes must compare equal to the parser's unsigned input
        # units; a narrow '\xC2' literal is negative wherever char is
        # signed, so emit a char32_t literal instead.
        return f"U'\\x{format(code_point, '02X')}'"
    if not char.isprintable() or code_point < 0x20 or code_point > 0x7E:
        return f"'\\x{format(code_point, '02X')}'"
    return f"'{char}'"


def render_char_class(chars, kind: str) -> str:
    """Render an ASCII-sorted ``ctll::set<...>`` or ``ctll::neg_set<...>``.

    Args:
        chars: The member characters (duplicates are removed).
        kind: Either ``"set"`` or ``"neg_set"``.

    Returns:
        The rendered class, e.g. ``ctll::set<'a','b'>``.
    """
    inner = ",".join(cpp_char_literal(c) for c in sorted(set(chars), key=ord))
    return f"ctll::{kind}<{inner}>"


def render_neg_set(chars) -> str:
    """Render a ``ctll::neg_set<...>`` from ``chars`` (shorthand for ``render_char_class``)."""
    return render_char_class(chars, "neg_set")


# A contiguous run shorter than this is cheaper kept as individual ``term``/``set``
# members than expressed as a ``ctll::range`` (a range costs two compile-time
# comparisons, so it only pays off once it replaces three or more members).

def render_range(lo: str, hi: str) -> str:
    """Render a ``ctll::range<lo,hi>`` lookahead for an inclusive character span."""
    return f"ctll::range<{cpp_char_literal(lo)},{cpp_char_literal(hi)}>"


def render_positive_lookahead_tokens(chars, aliaser=None):
    """Render a positive lookahead as one or more rule-lookahead token strings.

    Without range optimization the whole set is a single ``term``/``set`` token
    (optionally hoisted to an alias). With it, contiguous runs become
    ``ctll::range`` tokens and the leftover characters a single ``term``/``set``
    token, so the caller emits one ``rule`` overload per returned token -- all
    selecting the same production. The split is language-preserving: the runs and
    the residual are disjoint and cover exactly the original set.

    Args:
        chars: The member characters of the lookahead.
        aliaser: If given and range optimization is *not* in use, the
            :class:`TerminalAliaser` used to hoist the single token to an alias.
            Ranges are emitted inline (each is tiny and rarely repeated).

    Returns:
        A list of lookahead token strings (length 1 in the unoptimized case).
    """
    ordered = sorted(set(chars), key=ord)
    use_ranges = aliaser is not None and getattr(aliaser, "use_ranges", False)
    # Only decompose sets large enough that the comparison saving outweighs the
    # extra overload-resolution cost of the added rules.
    if not use_ranges or len(ordered) < _MIN_RANGE_SET_SIZE:
        if len(ordered) == 1:
            type_string = f"ctll::term<{cpp_char_literal(ordered[0])}>"
        else:
            type_string = render_char_class(ordered, "set")
        if aliaser is not None:
            return [aliaser.alias_for(type_string, ordered, is_neg=False)]
        return [type_string]

    ranges, residual = decompose_into_runs(ordered)
    if not ranges:
        # Nothing contiguous enough to help; fall back to a single aliased token.
        if len(ordered) == 1:
            type_string = f"ctll::term<{cpp_char_literal(ordered[0])}>"
        else:
            type_string = render_char_class(ordered, "set")
        return [aliaser.alias_for(type_string, ordered, is_neg=False)]

    tokens = [render_range(lo, hi) for lo, hi in ranges]
    if len(residual) == 1:
        tokens.append(f"ctll::term<{cpp_char_literal(residual[0])}>")
    elif residual:
        residual_type = render_char_class(residual, "set")
        tokens.append(aliaser.alias_for(residual_type, residual, is_neg=False))
    return tokens


def render_terminal_lookahead(terminal: GrammerType, terminal_table: dict,
                              others_name: str = "_others", aliaser=None) -> str:
    """Render a parse-table lookahead terminal as the second argument of ``rule``.

    * single-character atom                -> ``ctll::term<'c'>``
    * positive / named set                 -> ``ctll::set<...>`` (ASCII sorted)
    * the global "other" negative set       -> the ``_others`` alias
    * any other named negative set (uchar)  -> inline ``ctll::neg_set<...>``
    * end-of-input ``$``                    -> ``ctll::epsilon``

    A *named* terminal stores its name in ``.value``; its real character set is in
    ``terminal_table[name]``. The name is checked before resolving so the global
    ``other`` set can be told apart from a user-named negative set.

    Args:
        terminal: The lookahead symbol.
        terminal_table: The terminal section of the identifier table.
        others_name: The alias to emit for the global ``other`` set.
        aliaser: If given, a :class:`TerminalAliaser`; the terminal is registered
            and its alias name is returned instead of the inline type.

    Returns:
        The rendered C++ lookahead type, or its alias when ``aliaser`` is given.
    """
    if terminal.value == "$" and terminal.is_named_terminal():
        return "ctll::epsilon"

    name = terminal.value if terminal.is_named_terminal() else None
    if terminal.is_named_terminal():
        resolved = terminal_table.get(terminal.value)
        if resolved is not None:
            terminal = resolved

    if terminal.type == SymbolType.negitive_set:
        type_string = render_neg_set(terminal.value)
        if aliaser is not None:
            return aliaser.alias_for(type_string, terminal.value, is_neg=True, gram_name=name)
        # Only the implicit global set collapses to the ``_others`` alias; a
        # user-named set or an inline ``[[^...]]`` range renders its own type.
        if name == "other":
            return others_name
        return type_string

    chars = list(terminal.value)
    if len(chars) == 1:
        type_string = f"ctll::term<{cpp_char_literal(chars[0])}>"
    else:
        type_string = render_char_class(chars, "set")
    if aliaser is not None:
        return aliaser.alias_for(type_string, chars, is_neg=False)
    return type_string


def render_pushed_symbol(symbol: GrammerType, terminal_table: dict, aliaser=None) -> str:
    """Render one symbol of a production body for use inside ``ctll::push<...>``.

    Nonterminals and semantic actions are emitted by name; a single-atom terminal
    becomes ``ctll::term<...>``, a positive set ``ctll::set<...>`` and a negative
    set ``ctll::neg_set<...>``.

    Args:
        symbol: The symbol to render.
        terminal_table: The terminal section of the identifier table.
        aliaser: If given, a :class:`TerminalAliaser`; terminals are registered and
            their alias names are returned instead of inline types.

    Returns:
        The rendered C++ symbol (an alias name for terminals when ``aliaser`` is
        given).
    """
    if symbol.is_non_terminal() or symbol.is_semantic_action():
        return str(symbol)

    name = symbol.value if symbol.is_named_terminal() else None
    if symbol.is_named_terminal():
        resolved = terminal_table.get(symbol.value)
        if resolved is not None:
            symbol = resolved

    if symbol.type == SymbolType.negitive_set:
        type_string = render_neg_set(symbol.value)
        if aliaser is not None:
            return aliaser.alias_for(type_string, symbol.value, is_neg=True, gram_name=name)
        return type_string
    if symbol.is_atom():
        type_string = f"ctll::term<{cpp_char_literal(symbol.value)}>"
        if aliaser is not None:
            return aliaser.alias_for(type_string, [symbol.value], is_neg=False)
        return type_string

    chars = list(symbol.value)
    if len(chars) == 1:
        type_string = f"ctll::term<{cpp_char_literal(chars[0])}>"
    else:
        type_string = render_char_class(chars, "set")
    if aliaser is not None:
        return aliaser.alias_for(type_string, chars, is_neg=False)
    return type_string


def render_production_rhs(production, terminal_table: dict, aliaser=None) -> str:
    """Render the ``-> ...`` body of one ``rule`` overload.

    Following CTLL's pushdown machine:

    * a pure-epsilon production pops the nonterminal and consumes nothing
      -> ``ctll::epsilon``;
    * if the production *begins* with the matched terminal (after any leading
      semantic actions), that terminal is replaced by ``ctll::anything`` (pop one
      input character) with the leading actions kept in front of it;
    * if the production begins with a *nonterminal*, the lookahead is consumed
      inside that nonterminal, so nothing is consumed here and every body terminal
      is rendered literally;
    * a production of only semantic actions is pushed verbatim.

    Args:
        production: The selected production (a sequence of symbols).
        terminal_table: The terminal section of the identifier table.
        aliaser: If given, a :class:`TerminalAliaser`; body terminals are emitted
            by their alias names.

    Returns:
        Either ``ctll::epsilon`` or a ``ctll::push<...>`` expression.
    """
    if len(production) == 1 and production[0].is_epsilon():
        return "ctll::epsilon"

    # Only consume the lookahead if the first real symbol is a terminal.
    leading_is_terminal = False
    for symbol in production:
        if symbol.is_epsilon() or symbol.is_semantic_action():
            continue
        leading_is_terminal = symbol.is_terminal()
        break

    rendered = []
    consumed = not leading_is_terminal
    for symbol in production:
        if symbol.is_epsilon():
            continue
        if not consumed and symbol.is_terminal():
            rendered.append("ctll::anything")
            consumed = True
            continue
        rendered.append(render_pushed_symbol(symbol, terminal_table, aliaser))

    if not rendered:
        return "ctll::epsilon"
    return f"ctll::push<{', '.join(rendered)}>"


def build_parse_table_for_output(table: IdentifierTable, optimization_level: int = 0,
                                 q_grammar: bool = True, kinds_out: Optional[dict] = None):
    """Prepare the grammar and build its parse table for rendering.

    Normalizes keys, inlines pure character-class helpers, applies the requested
    optimization passes, then computes FIRST/FOLLOW and the parse table under the
    chosen parser model. The (possibly optimized) grammar is written back into
    ``table``.

    Args:
        table: The identifier table.
        optimization_level: 0-3; see the module docstring.
        q_grammar: True for the Q-grammar relaxation CTLL uses (a shift rule may
            coexist with an epsilon fallback on the same terminal), False for
            classic LL(1) (any FIRST/FIRST or FIRST/FOLLOW overlap is a conflict).

    Returns:
        A tuple ``(grammar, parse_table, follow)``.
    """
    grammar = normalize_grammar_keys(table[SymbolType.non_terminal])
    grammar = inline_pure_terminal_nonterminals(grammar)
    if optimization_level:
        start_name = str(next(iter(grammar)))
        grammar = optimize_grammar(grammar, start_name, optimization_level, q_grammar)
    table[SymbolType.non_terminal] = grammar

    logger.debug(f"Building parse table ({'Q-grammar' if q_grammar else 'strict LL(1)'} "
                 f"model) for {describe_grammar(grammar)}")
    first = compute_first(grammar)
    logger.debug(stringify_first_follow(first, "FIRST"))
    follow = compute_follow(grammar, first)
    logger.debug(stringify_first_follow(follow, "FOLLOW"))
    parse_table = construct_parse_table(grammar, first, follow, strict=not q_grammar,
                                        kinds_out=kinds_out)
    logger.debug(stringify_parse_table(parse_table))
    return grammar, parse_table, follow


def explain_nonterminal(name: str, table: IdentifierTable,
                        optimization_level: int = 0, q_grammar: bool = True) -> str:
    """Produce a focused, end-to-end explanation of one nonterminal.

    Builds the parse table (under the requested options) and reports, for the
    named nonterminal: its productions in the final grammar, its FIRST and FOLLOW
    sets, its parse-table row (lookahead -> chosen production) and the C++ ``rule``
    overloads that get emitted for it. This is the fastest way to understand why a
    particular nonterminal parses the way it does.

    Args:
        name: The nonterminal to explain (matched by string name).
        table: The identifier table (already through the front-end pipeline).
        optimization_level: 0-3; applied before the explanation so the report
            reflects what will actually be generated.
        q_grammar: Parser model, as elsewhere.

    Returns:
        A multi-line explanation. If no nonterminal matches ``name``, a short
        message listing is returned instead.
    """
    grammar, parse_table, follow = build_parse_table_for_output(
        table, optimization_level, q_grammar
    )
    first = compute_first(grammar)

    target = None
    for non_terminal in grammar:
        if str(non_terminal) == name:
            target = non_terminal
            break
    if target is None:
        available = ", ".join(sorted(str(nt) for nt in grammar))
        return f"No nonterminal named '{name}'. Available: {available}"

    lines = [f"Explanation of nonterminal '{name}':", "", "  Productions:"]
    for production in grammar[target]:
        body = " ".join(str(symbol) for symbol in production) or "epsilon"
        lines.append(f"    {name} -> {body}")

    def render_symbol_set(symbols):
        return ", ".join(sorted(str(s) for s in symbols)) or "(empty)"

    lines.append("")
    lines.append(f"  FIRST({name})  = {{ {render_symbol_set(first.get(target, set()))} }}")
    lines.append(f"  FOLLOW({name}) = {{ {render_symbol_set(follow.get(target, set()))} }}")

    lines.append("")
    lines.append("  Parse-table row (lookahead -> production):")
    row = parse_table.get(target, {})
    if row:
        for terminal in sorted(row.keys(), key=str):
            body = " ".join(str(s) for s in row[terminal]) or "epsilon"
            lines.append(f"    on {str(terminal):<24} -> {body}")
    else:
        lines.append("    (no entries; this nonterminal is never selected)")

    lines.append("")
    lines.append("  Emitted C++ rule overloads:")
    if row:
        reserved = {str(nt) for nt in grammar} | {str(a) for a in table[SymbolType.action]}
        reserved.add("_start")
        aliaser = TerminalAliaser(table[SymbolType.terminal], reserved=reserved)
        block = _emit_rules_for_nonterminal(target, row, table[SymbolType.terminal],
                                            "    ", aliaser)
        lines.append(block)
    else:
        lines.append("    (none)")

    return "\n".join(lines)


def _emit_rules_for_nonterminal(nonterminal, entries: dict, terminal_table: dict,
                                indentation: str, aliaser=None,
                                kinds: Optional[dict] = None) -> str:
    """Render all ``rule`` overloads for one nonterminal as a block of lines.

    Lookaheads selecting the *same* production are merged into a single overload
    whose lookahead is the union of their concrete characters (one
    ``ctll::set<...>`` / ``ctll::term<...>``). Three lookahead kinds stay on their
    own rows: the global "other" set (``_others``), any named negative set (inline
    ``ctll::neg_set<...>``) and end-of-input (``ctll::epsilon``). Groups are
    emitted in order of first appearance of their production.

    Args:
        nonterminal: The nonterminal whose rules to render.
        entries: Its parse-table row, ``{lookahead terminal: production}``.
        terminal_table: The terminal section of the identifier table.
        indentation: The leading indentation for each emitted line.
        aliaser: If given, a :class:`TerminalAliaser`; every emitted terminal (the
            merged lookahead, any negative set, and the body terminals) is referred
            to by its alias name and registered for the ``// TERMINALS`` section.

    Returns:
        The newline-joined block of ``rule`` overloads.
    """
    order = []                # rhs strings, in first-seen order
    group_chars = {}          # rhs -> set of shift-claimed lookahead chars
    group_eps_chars = {}      # rhs -> chars reached via an epsilon fallback cell
    group_has_others = {}     # rhs -> bool   (global 'other' -> _others)
    group_has_eoi = {}        # rhs -> bool   ('$' -> ctll::epsilon)
    group_neg_sets = {}       # rhs -> list of negative-set char tuples
    shift_char_owner = {}     # char -> rhs of the shift production claiming it
    shift_neg_exclusions = [] # exclusion sets of consuming negative sets

    for lookahead, production in entries.items():
        rhs = render_production_rhs(production, terminal_table, aliaser)
        if rhs not in group_chars:
            order.append(rhs)
            group_chars[rhs] = set()
            group_eps_chars[rhs] = set()
            group_has_others[rhs] = False
            group_has_eoi[rhs] = False
            group_neg_sets[rhs] = []

        if lookahead.is_named_terminal() and lookahead.value == "$":
            group_has_eoi[rhs] = True
            continue

        # Resolve named terminals, remembering the name to tell the global
        # 'other' set apart from a user-named negative set.
        name = lookahead.value if lookahead.is_named_terminal() else None
        resolved = lookahead
        if lookahead.is_named_terminal():
            looked_up = terminal_table.get(lookahead.value)
            if looked_up is not None:
                resolved = looked_up

        if resolved.type == SymbolType.negitive_set:
            # Only the implicit global set becomes the ``_others`` lookahead; a
            # user-named set or an inline ``[[^...]]`` range (name is None) is a
            # negative set of its own.
            kind = kinds.get(lookahead) if kinds is not None else None
            if kind != "epsilon":
                # a consuming negative set claims every character it does
                # not exclude; remember the exclusions so epsilon-fallback
                # rows can be reduced to the unclaimed characters
                shift_neg_exclusions.append(set(resolved.value))
            if name == "other":
                group_has_others[rhs] = True
            else:
                entry = (tuple(resolved.value), name)
                if entry not in group_neg_sets[rhs]:
                    group_neg_sets[rhs].append(entry)
        else:
            # The Q-grammar shift/epsilon preference is decided per terminal
            # SYMBOL, but two different named sets can share characters. Track
            # which characters are claimed by consuming ("shift") cells so
            # epsilon-fallback rows can be emitted without them; a character
            # claimed by two different shift productions is a real ambiguity
            # the symbol-level check cannot see.
            kind = kinds.get(lookahead) if kinds is not None else None
            if kind == "epsilon":
                group_eps_chars[rhs].update(resolved.value)
            else:
                for char in resolved.value:
                    owner = shift_char_owner.get(char)
                    if owner is not None and owner != rhs:
                        raise ValueError(
                            f"Grammar is not (q)LL(1): in nonterminal "
                            f"'{nonterminal}', lookahead character {char!r} is "
                            f"claimed by two different consuming productions "
                            f"(via overlapping terminal sets)"
                        )
                    shift_char_owner[char] = rhs
                group_chars[rhs].update(resolved.value)

    # Characters consumed by a shift cell shadow the same characters in any
    # epsilon-fallback cell of this state (Q-grammar: shift wins). A shift
    # negative set claims everything outside its exclusion list, so an
    # epsilon character survives only when every such set excludes it.
    for rhs in order:
        surviving = set()
        for char in group_eps_chars[rhs]:
            if char in shift_char_owner:
                continue
            if all(char in exclusions for exclusions in shift_neg_exclusions):
                surviving.add(char)
        group_chars[rhs] |= surviving

    def lookahead_token(type_string, chars, is_neg, gram_name=None):
        """Return the alias (if aliasing) or the inline type for a lookahead."""
        if aliaser is not None:
            return aliaser.alias_for(type_string, chars, is_neg=is_neg, gram_name=gram_name)
        return type_string

    lines = []
    for rhs in order:
        chars = group_chars[rhs]
        if chars:
            ordered = sorted(chars, key=ord)
            for lhs in render_positive_lookahead_tokens(ordered, aliaser):
                lines.append(f"static constexpr auto rule({nonterminal}, {lhs}) -> {rhs};")
        for key, gram_name in group_neg_sets[rhs]:
            neg_type = render_neg_set(list(key))
            neg = lookahead_token(neg_type, list(key), is_neg=True, gram_name=gram_name)
            lines.append(f"static constexpr auto rule({nonterminal}, {neg}) -> {rhs};")
        if group_has_eoi[rhs]:
            lines.append(f"static constexpr auto rule({nonterminal}, ctll::epsilon) -> {rhs};")
        if group_has_others[rhs]:
            others = aliaser.others_name if aliaser is not None else "_others"
            lines.append(f"static constexpr auto rule({nonterminal}, {others}) -> {rhs};")

    return "\n".join(f"{indentation}{line}" for line in lines)


def table_to_constexpr_cpp(table: IdentifierTable, args: argparse.Namespace) -> str:
    """Render the whole CTLL header from the identifier table.

    Emits, in order: nonterminal forward declarations (the start symbol also gets
    ``using _start = ...``), semantic-action structs, the ``_others`` alias for the
    global negative set (only when it is non-empty), and the grouped ``rule``
    overloads per nonterminal.

    CTLL's ``grammars.hpp`` provides a global ``rule(...) -> ctll::reject``
    catch-all, so any (state, terminal) pair not emitted here rejects
    automatically; every row emitted is a real FIRST/FOLLOW transition.

    Args:
        table: The identifier table (its grammar is finalized in place here).
        args: Parsed command-line options; ``guard``, ``namespace``,
            ``grammer_name``, ``optimization`` and ``q_grammar`` are consulted.

    Returns:
        The complete C++ header as a string, ending with a trailing newline.
    """
    indentation = "\t"
    terminal_table = table[SymbolType.terminal]

    q_grammar = getattr(args, "q_grammar", True)
    cell_kinds: dict = {}
    grammar, parse_table, _follow = build_parse_table_for_output(
        table, getattr(args, "optimization", 0), q_grammar, kinds_out=cell_kinds
    )

    # Nonterminal forward declarations (sorted; start symbol gets _start alias).
    start_symbol = next(iter(grammar))
    nonterminal_lines = []
    for nonterminal in sorted(grammar.keys(), key=str):
        line = f"struct {nonterminal} {{}};"
        if nonterminal == start_symbol:
            line += f" using _start = {nonterminal};"
        nonterminal_lines.append(line)

    # Semantic-action structs (sorted for stable output).
    action_lines = [
        f"struct {action}: ctll::action {{}};"
        for action in sorted(table[SymbolType.action], key=str)
    ]

    # The aliaser hoists every terminal into a named alias. Pre-register the
    # global negative "other" set first (when non-empty) so ``_others`` leads the
    # TERMINALS block; grammars that never use ``other`` (e.g. JSON) skip it and so
    # never get a dead ``using _others = ctll::neg_set<>;``. The alias names must
    # not collide with the nonterminal/action structs declared in the same scope.
    reserved = {str(nt) for nt in grammar.keys()}
    reserved |= {str(a) for a in table[SymbolType.action]}
    reserved.add("_start")
    use_ranges = getattr(args, "range_lookaheads", False)
    aliaser = TerminalAliaser(terminal_table, reserved=reserved, use_ranges=use_ranges)
    other_chars = sorted(terminal_table["other"].value, key=ord)
    if other_chars:
        aliaser.alias_for(render_neg_set(other_chars), other_chars,
                          is_neg=True, gram_name="other")

    # The (q)LL1 rule overloads, in the grammar's own declaration order. Rendering
    # populates the aliaser with every terminal the rules reference.
    rule_blocks = [
        _emit_rules_for_nonterminal(nt, parse_table[nt], terminal_table, indentation, aliaser,
                                    kinds=cell_kinds.get(nt))
        for nt in grammar.keys()
        if parse_table.get(nt)
    ]
    rules_section = "\n\n".join(rule_blocks)

    rule_count = sum(block.count("static constexpr auto rule(") for block in rule_blocks)
    logger.debug(f"Emitted {rule_count} rule overloads across {len(rule_blocks)} nonterminals")
    aliaser.log_summary()

    # The central TERMINALS block, assembled after the rules have registered every
    # terminal they use.
    terminals_section = aliaser.emit_section(indentation)

    nl_indent = f"\n{indentation}"
    header = f"""
#ifndef {args.guard}
#define {args.guard}

// THIS FILE WAS GENERATED BY TABLEWRIGHT TOOL, DO NOT MODIFY THIS FILE

#include "../ctll/grammars.hpp"

namespace {args.namespace} {{

struct {args.grammer_name} {{

{indentation}// NONTERMINALS:
{indentation}{nl_indent.join(nonterminal_lines)}

{indentation}// 'action' types:
{indentation}{nl_indent.join(action_lines)}

{terminals_section}

{indentation}// {'(q)LL1' if q_grammar else 'LL1'} function:
{rules_section}

}};

}}

#endif //{args.guard}
"""
    return header.strip() + "\n"
