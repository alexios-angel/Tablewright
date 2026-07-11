from lark import Discard, Lark, Token, Transformer, Tree, Visitor
from lark.exceptions import UnexpectedInput

from .chartools import (expand_range_token, scan_escaped_tokens, unescape_character)
from .logutil import logger, trace
from .symbols import (GrammerType, HashableList, IdentifierTable, OrderedSet, SymbolType)

# ======================================================================== #
# The .gram input grammar (parsed by Lark) and its tree transforms
# ======================================================================== #

# Grammar for the .gram dialect itself. The trailing terminal definitions pin
# down the lexical shape: names, atoms, the '->' arrow, 'epsilon', etc.
grammar = r"""
    start: (SPACES? (rule_statement | set_definition | comment)? WHITESPACES?)*

    comment: /#.*/

    rule_statement: SINGLE_NAME PRODUCES rule_list 
    rule_list: rule ("|" rule)*
    rule: epsilon_empty | ((SINGLE_NAME ":")? rule_content)
    rule_content: rule_atom ("," rule_atom)* ","?
    rule_atom: epsilon | atom | string | range | terminal | non_terminal | semantic_action | group | quantified
    epsilon: (EPSILON_AT|EPSILON)
    # Empty rule can signify epsilon
    epsilon_empty:

    // Regex-style repetition: '+' (one or more) and '*' (zero or more) may follow
    // an atom, a "string", a [[range]], a named terminal, a <nonterminal> or a
    // (grouping). Both are expanded into an anonymous helper nonterminal before
    // any analysis runs (see expand_groups_and_quantifiers). A '*' or '+' that
    // does not immediately follow such a symbol -- e.g. one standing alone
    // between commas, as in ``S -> a, *, b`` -- is still an ordinary atom, so
    // existing grammars that use these characters as terminals keep working.
    quantified: quant_base QUANT
    quant_base: atom | string | range | terminal | non_terminal | group

    // A parenthesized grouping, e.g. ``(<expr> x)`` or ``(<expr>, x)*``. Its
    // items may be separated by commas or simply by whitespace. Because bare
    // whitespace separates items, the structural characters , ( ) < > [ ] | " * +
    // and @ must be written escaped (\( \* ...) to mean their literal selves
    // *inside* a grouping; outside a grouping the comma-separated syntax is
    // unchanged and those characters remain plain atoms (``S -> (, a, )`` is
    // still the three atoms '(' 'a' ')'). group_atom's lower priority makes a
    // multi-character word inside a grouping resolve as a NAME (a named-terminal
    // reference), matching what it means at top level.
    group: "(" group_content ")"
    group_content: group_item (","? group_item)* ","?
    group_item: epsilon | group_atom | string | range | terminal | non_terminal | semantic_action | group | group_quantified
    group_quantified: group_quant_base QUANT
    group_quant_base: group_atom | string | range | terminal | non_terminal | group
    group_atom.-1: GATOM

    terminal: NAME | "*" NAME | NAME EPSILON_AT ATOM
    string: "\"" TEXT "\""
    atom: ATOM
    # A regex-style character range, e.g. [[a-zA-Z]] or [[abcg-i]]. It expands to a
    # positive set with every member character enumerated. A leading '^' negates
    # the range ([[^abc]] matches any character except a, b, c).
    range: RANGE
    TEXT: /((\\.)|[^"])+/
    non_terminal: "<" NAME ">"
    semantic_action: "[" NAME "]"

    // A set definition. It is given a higher priority than rule_statement so that
    // an all-atoms body written with ':' (e.g. ``name : a, b, c``) is still read as
    // a set, exactly as it was before ':' became a rule operator. A rule that uses
    // ':' is disambiguated by its content (a nonterminal <x>, a string, a range, a
    // semantic action, or a '|' alternation) or by using '->'.
    set_definition.2: NAME ASSIGN minus_sigma? set_body
    set_body: ("{" set_contents "}") | set_contents
    minus_sigma: "sigma" "-"
    set_contents: ATOM ("," ATOM)* ","?
    
    ARROW: "->"
    # The production operator joining a nonterminal to its rules. Both '->' and ':'
    # are accepted and mean the same thing.
    PRODUCES: "->" | ":"
    # Assignment operator for a terminal/set definition. Both '=' and ':' are
    # accepted and mean the same thing.
    ASSIGN: "=" | ":"
    # [[ ... ]] with a non-empty body of escapes or non-']' characters. Matched as a
    # single high-priority terminal so it cannot be confused with a "[" NAME "]"
    # semantic action or with bare '[' / ']' atoms.
    RANGE: /\[\[((\\.)|[^\]])+\]\]/
    SINGLE_NAME: /[a-zA-Z_][a-zA-Z_0-9]*/
    NAME: /[a-zA-Z][a-zA-Z_0-9]+/
    EPSILON_AT: /(?<!\\)@/
    EPSILON: "epsilon"
    # A repetition quantifier ('one or more' / 'zero or more').
    QUANT: "+" | "*"
    # An atom inside a (grouping): any single character except unescaped
    # whitespace or the grouping's structural characters; escapes lift the
    # restriction (e.g. \( \) \* \+ \, are the literal characters). The hex
    # escapes \xNN and \u{H..H} come first so they match as one token.
    GATOM: /\\x[0-9a-fA-F]{2}|\\u\{[0-9a-fA-F]{1,6}\}|\\.|[^\s,()<>\[\]|"*+@]/
    # A rule-body / set-member atom: one possibly escaped character. The hex
    # escapes \xNN (two digits) and \u{H..H} (1-6 digits) denote a code point;
    # a malformed hex escape falls back to the old reading (\x is a literal x).
    ATOM: /\\x[0-9a-fA-F]{2}|\\u\{[0-9a-fA-F]{1,6}\}|\\?[^\s]/
    SPACES: /[ \t\f]+/
    WHITESPACES: /\s+/ 
    %ignore WHITESPACES
"""


# A concise, user-facing cheat-sheet for the .gram dialect, shown by --syntax.
GRAMMAR_SYNTAX_REFERENCE = """\
Tablewright .gram syntax quick reference
========================================

A grammar is a sequence of terminal (set) definitions and rules.

Terminal sets
-------------
  name = {a, b, c}        a positive set: matches a, b or c
  name = a, b, c          braces are optional
  name : a, b, c          ':' works the same as '='
  name = sigma - {a, b}   a negative set: matches any character except a, b

Rules
-----
  A -> <B>, x, [act] | epsilon
  A : <B>, x, [act] | epsilon      ':' works the same as '->'

  <B>        reference to nonterminal B   (names must be 2+ characters)
  x          a single literal character (an atom)
  "abc"      a string literal (expands to atoms a, b, c)
  [[a-z]]    a regex-style range (expands to a positive set)
  [[^abc]]   a negated range: any character except those listed; a leading
             '^' negates, an escaped '\\^' or non-leading '^' is a literal
  [act]      a semantic action named 'act'
  |          separates alternatives
  ,          separates the symbols of one alternative
  epsilon    (or '@') the empty production

Escapes (atoms, set members, strings, ranges)
---------------------------------------------
  \\n \\t \\r \\f \\v \\0 \\a \\b    the usual control characters
  \\xNN                       code point NN (exactly two hex digits)
  \\u{H..H}                   code point (1-6 hex digits, up to U+10FFFF)
  \\c                         any other escaped character is that literal
  Non-ASCII characters may also be typed directly (files are read as UTF-8).

Repetition and grouping
-----------------------
  X+         one or more X:   S -> a+   =   S -> a, <a_anon>
                                            a_anon -> a, <a_anon> | @
  X*         zero or more X:  S -> a*   =   S -> <a_anon>  (same helper)
  (X Y)      groups a sequence; items separated by commas or whitespace
  (X Y)*     a grouping may itself be quantified

  X may be an atom, "string", [[range]], named terminal, <nonterminal>,
  or (grouping). A '*' or '+' is a quantifier only right after such a
  symbol; standing alone (e.g. 'S -> a, *, b') it is still a plain atom.
  Inside a grouping, escape , ( ) < > [ ] | " * + @ to use them as
  literal characters (\\(, \\*, ...).

Notes
-----
  * '#' starts a comment to end of line.
  * A bare 'name : a, b, c' (only atoms) is read as a set, not a rule;
    give a rule a <nonterminal>, "string", [[range]] or '|' to disambiguate,
    or just use '->'.

Example
-------
  digit  = {0,1,2,3,4,5,6,7,8,9}
  number -> digit, <number_tail>
  number_tail -> digit, <number_tail> | epsilon
"""


# Human-readable names for the grammar's terminals, used to rewrite Lark's raw
# parser-error token names (``VBAR``, ``__ANON_0``, ...) into the concrete syntax
# a grammar author actually types. Anything not listed (e.g. internal anonymous
# terminals) is dropped from the "expected" list rather than shown as noise.
_TOKEN_DESCRIPTIONS = {
    "PRODUCES": "'->' or ':'",
    "ARROW": "'->'",
    "ASSIGN": "'=' or ':'",
    "COLON": "':'",
    "COMMA": "','",
    "VBAR": "'|'",
    "NAME": "a terminal/nonterminal name",
    "SINGLE_NAME": "a name",
    "ATOM": "a character (a plain char, \\c, \\xNN or \\u{...})",
    "GATOM": "a character (a plain char, \\c, \\xNN or \\u{...})",
    "QUANT": "'+' or '*'",
    "STAR": "'*'",
    "PLUS": "'+'",
    "EPSILON": "'epsilon'",
    "EPSILON_AT": "'@'",
    "TEXT": "a quoted string",
    "RANGE": "a [[a-z]] range",
    "LPAR": "'('",
    "RPAR": "')'",
    "DBLQUOTE": "'\"'",
    "LESSTHAN": "'<'",
    "MORETHAN": "'>'",
    "LSQB": "'['",
    "RSQB": "']'",
}


def _describe_expected_tokens(token_names) -> list:
    """Translate Lark terminal names into human-friendly syntax descriptions.

    Internal/anonymous terminals (Lark names them ``__ANON_n`` or with literal
    punctuation) and pure-whitespace terminals are dropped, since listing them as
    "expected" only confuses a grammar author. The result is de-duplicated and
    sorted for stable output.

    Args:
        token_names: An iterable of Lark terminal names from a parse error.

    Returns:
        A sorted list of human-readable descriptions (possibly empty).
    """
    described = set()
    for name in token_names:
        if name in ("WHITESPACES", "SPACES"):
            continue
        if name.startswith("__"):
            # Anonymous terminal for an inline literal; usually punctuation that
            # is already implied by the surrounding context.
            continue
        described.add(_TOKEN_DESCRIPTIONS.get(name, name))
    return sorted(described)


def format_grammar_syntax_error(error, source: str, filename: str) -> str:
    """Render a Lark parse error as a clear, source-anchored message.

    Produces the offending file location, the line of source with a caret under
    the problem column, and -- when available -- a humanized list of what the
    parser expected there, so the user sees ``',' or '->'`` instead of raw
    terminal names like ``COMMA`` / ``PRODUCES``.

    Args:
        error: A Lark ``UnexpectedInput`` (or subclass) instance.
        source: The full grammar text being parsed.
        filename: The grammar's filename, for the location line.

    Returns:
        A multi-line, human-readable error message.
    """
    line = getattr(error, "line", None)
    column = getattr(error, "column", None)
    # Lark uses -1 for line/column when the error is at end of input; treat any
    # non-positive value as "no concrete location" rather than printing ":-1:-1".
    has_location = (isinstance(line, int) and line > 0
                    and isinstance(column, int) and column > 0)
    location = f"{filename}:{line}:{column}" if has_location else filename

    parts = [f"syntax error in {location}"]
    if not has_location:
        parts.append("unexpected end of input (the grammar ends mid-rule)")
    try:
        context = error.get_context(source)
        if context and has_location:
            parts.append(context.rstrip("\n"))
    except Exception:
        pass

    # The unexpected item itself (a character or a token), when Lark provides it.
    unexpected = None
    char = getattr(error, "char", None)
    token = getattr(error, "token", None)
    if char is not None:
        unexpected = repr(char)
    elif token is not None:
        unexpected = repr(str(token))
    if unexpected is not None:
        parts.append(f"unexpected {unexpected}")

    allowed = getattr(error, "allowed", None) or getattr(error, "expected", None)
    if allowed:
        described = _describe_expected_tokens(allowed)
        if described:
            parts.append("expected one of: " + ", ".join(described))
    return "\n".join(parts)


def parse_grammar_text(source: str, filename: str = "<grammar>"):
    """Parse ``.gram`` source into a Lark tree, with friendly error reporting.

    Wraps the Lark parser so a malformed grammar raises a :class:`ValueError`
    carrying a clear, source-anchored message (handled like other user-facing
    input errors) instead of surfacing Lark's internal exception text.

    Args:
        source: The grammar text.
        filename: The grammar's filename, used in error messages.

    Returns:
        The parsed Lark tree.

    Raises:
        ValueError: If the grammar cannot be parsed; the message is
            human-readable and points at the offending location.
    """
    parser = Lark(grammar, start="start")
    try:
        return parser.parse(source)
    except UnexpectedInput as exc:
        raise ValueError(format_grammar_syntax_error(exc, source, filename)) from exc

class SpaceTransformer(Transformer):
    """Drop the whitespace tokens (``SPACES``, ``WHITESPACES``) from the tree."""

    def WHITESPACES(self, tok: Token):
        """Discard a run of mixed whitespace."""
        return Discard

    def SPACES(self, tok: Token):
        """Discard a run of spaces/tabs/form-feeds."""
        return Discard


class RuleTransformer(Transformer):
    """Turn ``rule_atom`` and ``rule`` subtrees into symbols and productions.

    Grouping (``(...)``) and repetition (``X+`` / ``X*``) nodes become symbols
    of the transient :class:`SymbolType` kinds ``group`` and ``quantified``;
    :func:`expand_groups_and_quantifiers` rewrites those away immediately after
    the identifier table is built.
    """

    def _inner_symbol(self, node) -> GrammerType:
        """Build the :class:`GrammerType` for one inner symbol node.

        The inner node's name (``atom``, ``string``, ``range``, ``terminal``,
        ``non_terminal``, ``semantic_action`` or ``epsilon``) is also a
        :class:`SymbolType` member (except ``range``, handled specially), so the
        type is recovered by ``getattr``. Atom values are unescaped first; a
        ``range`` token is enumerated into an inline positive set (or a
        negative set for ``[[^...]]``). Nodes that a
        deeper transform already turned into a :class:`GrammerType` (groups,
        quantified symbols, group atoms) pass through unchanged.
        """
        if isinstance(node, GrammerType):
            return node
        if node.data == "range":
            # [[a-z]] -> an inline positive set with members enumerated;
            # [[^abc]] -> an inline negative set (any character but these).
            chars, negated = expand_range_token(node.children[0].value)
            polarity = (SymbolType.negitive_set if negated
                        else SymbolType.positive_set)
            return GrammerType(chars, polarity)
        value = node.children[0].value
        if node.data == "atom":
            value = unescape_character(value)
        symbol_type = getattr(SymbolType, node.data)
        return GrammerType(value, symbol_type)

    def rule_atom(self, tree: Tree) -> GrammerType:
        """Build the :class:`GrammerType` for one symbol of a rule body."""
        return self._inner_symbol(tree[0])

    # A grouping's items use the same symbol kinds as a rule body (with the
    # restricted GATOM as the atom terminal), so they convert identically.
    group_item = rule_atom

    def group_atom(self, tree) -> GrammerType:
        """An atom inside a grouping (``GATOM``): unescape it like ``atom``."""
        return GrammerType(unescape_character(tree[0].value), SymbolType.atom)

    def group_content(self, tree) -> tuple:
        """Collect a grouping's items (already :class:`GrammerType`) in order."""
        return tuple(tree)

    def group(self, tree) -> GrammerType:
        """Build the transient ``group`` symbol; its value is the item tuple."""
        return GrammerType(tree[0], SymbolType.group)

    def _quantified(self, tree) -> GrammerType:
        """Build the transient ``quantified`` symbol for ``X+`` / ``X*``.

        Its value is ``(body, quant)`` where ``body`` is the tuple of symbols
        being repeated (a quantified group contributes its items directly, so
        ``(a b)*`` repeats the two-symbol sequence) and ``quant`` is ``'+'`` or
        ``'*'``.
        """
        base = self._inner_symbol(tree[0])
        quant = tree[1].value
        body = base.value if base.type == SymbolType.group else (base,)
        return GrammerType((tuple(body), quant), SymbolType.quantified)

    quantified = _quantified
    group_quantified = _quantified

    def quant_base(self, tree) -> GrammerType:
        """Unwrap the symbol a quantifier applies to."""
        return self._inner_symbol(tree[0])

    group_quant_base = quant_base

    def rule(self, tok) -> HashableList:
        """Build one production (a :class:`HashableList` of symbols).

        A rule may carry an optional leading label (``label: body``); the label is
        a bare token, so it is skipped and only the ``rule_content`` subtree is
        used. An empty rule body is represented as the single-symbol epsilon
        production.
        """
        # Skip an optional leading label token (``SINGLE_NAME ":"``); the
        # ``rule_content``/``epsilon_empty`` node is the last child.
        rule_tok = tok[-1]
        if rule_tok.data == "epsilon_empty":
            return HashableList([GrammerType("epsilon", SymbolType.epsilon)])
        return HashableList(rule_tok.children)


class SetTransformer(Transformer):
    """Turn a ``set_contents`` subtree into a Python set of characters."""

    def set_contents(self, tree) -> set:
        """Collect, unescape and de-duplicate the characters of a set definition.

        ``minus_sigma`` (the ``sigma -`` prefix marking a negative set), if
        present, lives on the parent ``set_definition`` node and is handled in
        :class:`add_identifers`, so it is not seen here.
        """
        return {unescape_character(token.value) for token in tree}


# ======================================================================== #
# Identifier table: collecting nonterminals, terminals and actions
# ======================================================================== #

# The identifier table has three sections:
#   action       -> set of semantic-action names
#   non_terminal -> {name: OrderedSet of productions}
#   terminal     -> {name: GrammerType(set, positive/negative)}
# It is seeded with the implicit global "other" negative set, whose members are
# filled in later by get_other().
identifier_table = {
    SymbolType.action: set(),
    SymbolType.non_terminal: {},
    SymbolType.terminal: {
        "other": GrammerType([], SymbolType.negitive_set),
    },
}


class add_identifers(Visitor):
    """Populate the module-level ``identifier_table`` while walking the tree."""

    def set_definition(self, tree) -> None:
        """Record a ``name = ...`` set as a positive or negative terminal.

        The assignment operator may be ``=`` or ``:`` and the braces around the
        members are optional, so the children vary; the ``minus_sigma`` marker
        (the ``sigma -`` prefix) is detected by node type rather than position,
        and the set body is unwrapped from its ``set_body`` node. A definition
        carrying ``minus_sigma`` is a negative set, otherwise positive.
        """
        name = tree.children[0]
        is_negative = any(isinstance(child, Tree) and child.data == "minus_sigma"
                          for child in tree.children)
        # The final child is the set_body node; unwrap it to the set of members
        # (SetTransformer has already turned set_contents into a Python set).
        body = tree.children[-1]
        set_contents = body.children[0] if isinstance(body, Tree) else body
        set_type = SymbolType.negitive_set if is_negative else SymbolType.positive_set
        identifier_table[SymbolType.terminal][name.value] = GrammerType(set_contents, set_type)

    def rule_statement(self, tree) -> None:
        """Record a rule's alternatives under its nonterminal name.

        A nonterminal may be defined across several ``A -> ...`` lines, so the
        alternatives are merged into any existing set rather than replacing it.
        """
        name = tree.children[0].value
        rules = tree.children[-1].children
        nonterminals = identifier_table[SymbolType.non_terminal]
        if name not in nonterminals:
            nonterminals[name] = OrderedSet()
        nonterminals[name] |= OrderedSet(rules)


def add_semantic_action_identifiers(table: IdentifierTable) -> None:
    """Collect every semantic-action name used in the grammar into the table.

    Args:
        table: The identifier table; its ``action`` section is overwritten with
            the set of action names found across all productions.
    """
    actions = set()
    for productions in table[SymbolType.non_terminal].values():
        for production in productions:
            for symbol in production:
                if symbol.is_semantic_action():
                    actions.add(symbol.value)
    table[SymbolType.action] = actions


def _anonymous_helper_name(body, taken) -> str:
    """Derive a readable, unique nonterminal name for a repetition helper.

    The name is built from the repeated body so the generated grammar stays
    self-describing: ``a+`` gets ``a_anon`` (matching the documented expansion),
    ``<expr>*`` gets ``expr_anon``, and a multi-symbol body such as ``(a b)+``
    joins its first symbols (``a_b_anon``). Characters that are not valid in an
    identifier are spelled as ``xNN`` hex escapes so the name survives into the
    generated C++; a numeric suffix guarantees uniqueness against ``taken``.

    Args:
        body: The tuple of symbols being repeated.
        taken: Names already in use (nonterminals, terminals, prior helpers).

    Returns:
        A fresh name ending in ``_anon`` (or ``_anonN``).
    """
    def sanitize(symbol) -> str:
        value = symbol.value
        if not isinstance(value, str):
            return "set"  # an inline [[range]] / character set
        cleaned = "".join(
            ch if (ch.isalnum() or ch == "_") else f"x{ord(ch):02X}"
            for ch in value
        )
        return cleaned or "sym"

    base = "_".join(sanitize(symbol) for symbol in body[:2])
    if len(body) > 2:
        base += "_seq"
    # Helper names appear verbatim as C++ identifiers, so avoid the reserved
    # shapes: collapse '__' runs and never start with '_' or a digit.
    while "__" in base:
        base = base.replace("__", "_")
    base = base.lstrip("_")
    if not base:
        base = "group"
    if base[0].isdigit():
        base = "n" + base
    name = f"{base}_anon"
    suffix = 2
    while name in taken:
        name = f"{base}_anon{suffix}"
        suffix += 1
    return name


def expand_groups_and_quantifiers(table: IdentifierTable) -> int:
    """Rewrite grouping and ``+``/``*`` repetition syntax into plain rules.

    Runs right after the identifier table is built, before anything else looks
    at the grammar, and removes every transient ``group`` / ``quantified``
    symbol the parser produced:

    * A bare grouping ``(X Y)`` is spliced inline: it is only bracketing.
    * ``body+`` (one or more) becomes ``body, <body_anon>``.
    * ``body*`` (zero or more) becomes ``<body_anon>``.

    where the shared helper is the right-recursive loop::

        body_anon -> body, <body_anon> | epsilon

    so, per the documented example, ``S -> a+`` becomes ``S -> a, <a_anon>``
    with ``a_anon -> a, <a_anon> | epsilon``, and ``S -> a*`` becomes
    ``S -> <a_anon>`` with the same helper (equivalent to the
    ``S -> a, a_anon | @`` form: the helper alone already derives epsilon).
    Right recursion keeps the result (q)LL(1)-friendly and needs no further
    left-recursion elimination. Identical repeated bodies share one helper, and
    nesting (``((a)*)+``) is expanded innermost-first.

    Args:
        table: The identifier table; its nonterminal section is rewritten in
            place and gains one helper nonterminal per distinct repeated body.

    Returns:
        The number of helper nonterminals created.
    """
    nonterminals = table[SymbolType.non_terminal]
    taken = set(nonterminals) | set(table[SymbolType.terminal])
    helpers = {}      # body tuple -> helper name (for reuse)
    helper_defs = {}  # helper name -> OrderedSet of its two productions

    def helper_for(body: tuple) -> str:
        """Return (creating on first use) the loop helper for ``body``."""
        if body in helpers:
            return helpers[body]
        name = _anonymous_helper_name(body, taken)
        taken.add(name)
        helpers[body] = name
        loop = HashableList(list(body)
                            + [GrammerType(name, SymbolType.non_terminal)])
        empty = HashableList([GrammerType("epsilon", SymbolType.epsilon)])
        helper_defs[name] = OrderedSet([loop, empty])
        trace(f"repetition helper: {name} -> "
              f"{' '.join(str(s) for s in body)} <{name}> | epsilon")
        return name

    def expand_symbols(symbols) -> list:
        """Expand groups/quantifiers in a symbol sequence, innermost first."""
        expanded = []
        for symbol in symbols:
            if symbol.type == SymbolType.group:
                expanded.extend(expand_symbols(symbol.value))
            elif symbol.type == SymbolType.quantified:
                inner, quant = symbol.value
                body = [s for s in expand_symbols(inner) if not s.is_epsilon()]
                if not body:
                    continue  # (epsilon)* / (epsilon)+ repeat nothing
                name = helper_for(tuple(body))
                if quant == "+":
                    expanded.extend(body)
                expanded.append(GrammerType(name, SymbolType.non_terminal))
            else:
                expanded.append(symbol)
        return expanded

    rewrites = 0
    for name, productions in list(nonterminals.items()):
        new_productions = OrderedSet()
        for production in productions:
            has_transient = any(
                s.type in (SymbolType.group, SymbolType.quantified)
                for s in production
            )
            if not has_transient:
                new_productions.add(production)
                continue
            symbols = expand_symbols(production)
            # A production reduced to nothing (e.g. only epsilon-bodied
            # repetitions) is the empty production.
            if not symbols:
                symbols = [GrammerType("epsilon", SymbolType.epsilon)]
            # An epsilon is meaningful only when it stands alone.
            if len(symbols) > 1:
                symbols = [s for s in symbols if not s.is_epsilon()] or symbols
            new_productions.add(HashableList(symbols))
            rewrites += 1
        nonterminals[name] = new_productions
    nonterminals.update(helper_defs)

    if helper_defs or rewrites:
        logger.info(
            f"Expanded grouping/repetition syntax in {rewrites} production(s), "
            f"adding {len(helper_defs)} helper nonterminal(s): "
            f"{', '.join(sorted(helper_defs))}"
        )
    return len(helper_defs)


def break_strings(table: IdentifierTable) -> None:
    """Expand each string-literal symbol in place into its individual atoms.

    A ``"abc"`` symbol inside a production is replaced by the three atoms ``a``,
    ``b``, ``c`` at the same position.

    Args:
        table: The identifier table; its nonterminal productions are mutated in
            place.
    """
    nonterminals = table[SymbolType.non_terminal]
    expansions = 0
    for name, productions in nonterminals.items():
        for rule_index, rule in list(enumerate(productions)):
            for item_index, item in list(enumerate(rule)):
                if item.is_string():
                    text = item.value
                    # Decode escapes (\n, \", \xNN, \u{...}) so each atom is the
                    # character it denotes, not the raw escape spelling.
                    characters = [unescape_character(token) if escaped else token
                                  for token, escaped in scan_escaped_tokens(text)]
                    rule_list = list(nonterminals[name])[rule_index]
                    rule_list.pop(item_index)
                    for character in reversed(characters):
                        rule_list.insert(item_index, GrammerType(character, SymbolType.atom))
                    expansions += 1
                    trace(f"break string: \"{text}\" in '{name}' -> "
                          f"{len(characters)} atom(s)")
    if expansions:
        logger.debug(f"Expanded {expansions} string literal(s) into atoms")


def get_indexed_nonterminals(productions, table: IdentifierTable) -> set:
    """Collect the characters that can begin any of ``productions``.

    Recurses through a leading nonterminal so that, for example, the first
    symbols reachable from ``<X> rest`` include everything ``X`` can start with.
    Used by :func:`get_other` to resolve what the placeholder ``other`` terminal
    expands to.

    Args:
        productions: The alternatives to inspect (only their first symbol).
        table: The identifier table, for resolving nonterminals and named sets.

    Returns:
        The set of concrete first-characters (atoms are returned as their
        :class:`GrammerType`; set members as their raw characters).
    """
    chars = set()
    for production in productions:
        item = production[0]
        if item.is_non_terminal():
            chars |= get_indexed_nonterminals(table[SymbolType.non_terminal][item.value], table)
        elif item.is_atom():
            chars.add(item)
        elif item.is_named_terminal():
            chars |= set(table[SymbolType.terminal][item.value].value)
        elif item.is_set():
            # An inline [[range]]: its members are stored directly in the symbol.
            if item.type == SymbolType.negitive_set:
                raise Exception(
                    "A negated range [[^...]] cannot appear alongside 'other' "
                    "(its first-characters cannot be enumerated)")
            chars |= set(item.value)
    return chars


def get_other(table: IdentifierTable) -> set:
    """Compute the members of the implicit global ``other`` negative set.

    ``other`` means "any character used somewhere in the grammar that is not
    otherwise spelled out at this position". For every place the ``other`` symbol
    appears, the concrete first-characters of the sibling alternatives at that
    position are unioned in.

    Args:
        table: The identifier table.

    Returns:
        The set of characters ``other`` stands for.

    Raises:
        Exception: If an ``other`` position resolves to nothing (ambiguous), or a
            symbol of an unexpected kind is encountered.
    """
    other = set()
    for productions in table[SymbolType.non_terminal].values():
        other_indices = set()
        for production in productions:
            for index, item in enumerate(production):
                if item.value == "other":
                    other_indices.add(index)
        for production in productions:
            for index in other_indices:
                item = production[index]
                if item.is_non_terminal():
                    resolved = get_indexed_nonterminals(
                        table[SymbolType.non_terminal][item.value], table
                    )
                    if not resolved:
                        raise Exception("Ambiguous pattern when trying to discover other")
                    other |= resolved
                elif item.is_atom():
                    other.add(item.value)
                elif item.is_named_terminal():
                    other |= set(table[SymbolType.terminal][item.value].value)
                elif item.is_set():
                    # An inline [[range]]: members live in the symbol itself.
                    if item.type == SymbolType.negitive_set:
                        raise Exception(
                            "A negated range [[^...]] cannot appear alongside "
                            "'other' (its members cannot be enumerated)")
                    other |= set(item.value)
                elif item.is_epsilon():
                    continue
                else:
                    raise Exception("Unknown type when trying to discover other")
    if other:
        logger.debug(f"Resolved global 'other' negative set to {len(other)} character(s)")
    return other


def verify_identifiers(table: IdentifierTable) -> None:
    """Check that every referenced nonterminal and terminal is defined.

    Args:
        table: The identifier table.

    Raises:
        Exception: If any production references a nonterminal or named terminal
            that has no definition.
    """
    used_nonterminals = set()
    used_terminals = set()
    for productions in table[SymbolType.non_terminal].values():
        for production in productions:
            for symbol in production:
                if symbol.is_non_terminal():
                    used_nonterminals.add(symbol.value)
                elif symbol.is_named_terminal():
                    used_terminals.add(symbol.value)

    missing_nonterminals = used_nonterminals - set(table[SymbolType.non_terminal].keys())
    if missing_nonterminals:
        raise Exception(f"Unknown nonterminal(s): {', '.join(missing_nonterminals)}")

    missing_terminals = used_terminals - set(table[SymbolType.terminal].keys())
    if missing_terminals:
        raise Exception(f"Unknown terminal(s): {', '.join(missing_terminals)}")
