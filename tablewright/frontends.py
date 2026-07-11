import re

from lark import Discard, Lark, Token, Transformer
from lark.exceptions import UnexpectedInput, VisitError

from .chartools import _decode_quoted_literal, _fold_case, _split_regex_literal
from .eds import (_allocate_eds_names, _collect_referenced_names, _eds_escape_char,
    _EdsEmitter, _rename_ast)
from .gramparse import format_grammar_syntax_error
from .logutil import logger
from .regex_engine import _repeat_node, parse_regex

# Tablewright's EBNF document grammars. Two dialects share one transformer
# and lower to the same frontend AST as every other frontend:
#
#  * ISO/IEC 14977 style (--lang=ebnf): ``name = expression ;`` rules with
#    ',' concatenation, '|' alternation, '[x]' optional, '{x}' repetition,
#    '(x)' grouping, 'n * x' repetition factors and the 'x - y' exception.
#    '::='/':' and a '.' terminator are accepted as common variants, and
#    the '? * +' postfix quantifiers remain as extensions.
#
#  * W3C style (--lang=w3c, the XML-specification notation): terminator-less
#    ``Name ::= expression`` rules, juxtaposition for sequence, character
#    classes ``[a-z#xB7]`` / ``[^...]``, ``#xNN`` code-point references,
#    postfix '? * +' and the 'A - B' exception. Parsed with Earley: without
#    terminators, where one rule ends and the next begins is only decidable
#    from the following '::=' -- global context a deterministic parser
#    cannot see.
#
# Exceptions are translatable exactly when both operands denote character
# sets (classes, single characters, references to rules that reduce to
# them); the difference is computed at conversion time and anything else is
# rejected with an explanation, never mistranslated.
_EBNF_LARK_GRAMMAR = r"""
    ebnf: rule+
    rule: NAME _ASSIGN alternatives _TERM
    alternatives: sequence ("|" sequence)*
    sequence: factor (","? factor)* ","? |
    factor: repeated ("-" repeated)?
    repeated: (INT "*")? atom QUANT?
    ?atom: NAME                 -> name
         | STRING               -> literal
         | "(" alternatives ")" -> group
         | "[" alternatives "]" -> optional
         | "{" alternatives "}" -> repeat

    _ASSIGN: "::=" | "=" | ":"
    _TERM: ";" | "."
    QUANT: "?" | "*" | "+"
    INT: /[0-9]+/
    NAME: /[A-Za-z_][A-Za-z_0-9]*/
    STRING: /"(\\.|[^"\\])*"|'(\\.|[^'\\])*'/
    EBNF_COMMENT: /\(\*(.|\n)*?\*\)/
    %ignore EBNF_COMMENT
    %import common.WS
    %ignore WS
"""

_EBNF_PARSER = Lark(_EBNF_LARK_GRAMMAR, parser="lalr", start="ebnf")

_W3C_EBNF_GRAMMAR = r"""
    w3c: w3c_rule+
    w3c_rule: NAME _W3CASSIGN alternatives
    alternatives: sequence ("|" sequence)*
    sequence: w3c_factor*
    w3c_factor: w3c_item ("-" w3c_item)*
    w3c_item: w3c_primary QUANT?
    ?w3c_primary: NAME             -> name
                | W3C_STRING       -> w3c_string
                | W3C_CLASS        -> w3c_class
                | HEXREF           -> w3c_hexref
                | "(" alternatives ")" -> group

    _W3CASSIGN: "::="
    QUANT: /[?*+]/
    HEXREF: /#x[0-9a-fA-F]{1,6}/
    W3C_CLASS: /\[\^?[^\]]+\]/
    W3C_STRING: /"[^"]*"|'[^']*'/
    NAME: /[A-Za-z_][A-Za-z_0-9]*/
    W3C_COMMENT: /\/\*(.|\n)*?\*\//
    EBNF_COMMENT: /\(\*(.|\n)*?\*\)/
    %ignore W3C_COMMENT
    %ignore EBNF_COMMENT
    %import common.WS
    %ignore WS
"""

_W3C_EBNF_PARSER = Lark(_W3C_EBNF_GRAMMAR, parser="earley", start="w3c",
                        maybe_placeholders=False)


def _charset_subtract(left, right):
    """Compute the character-set difference of two charset nodes, or ``None``.

    All four polarity combinations are exact set algebra: ``A - B``,
    ``!A - B = !(A|B)``, ``A - !B = A&B`` and ``!A - !B = B - A``.
    """
    if left is None or right is None:
        return None
    _, left_chars, left_negated = left
    _, right_chars, right_negated = right
    if not left_negated and not right_negated:
        return ("charset", frozenset(left_chars - right_chars), False)
    if left_negated and not right_negated:
        return ("charset", frozenset(left_chars | right_chars), True)
    if not left_negated and right_negated:
        return ("charset", frozenset(left_chars & right_chars), False)
    return ("charset", frozenset(right_chars - left_chars), False)


def _resolve_exceptions(node, env: dict):
    """Rewrite every EBNF ``("exception", a, b)`` node into a charset.

    ``env`` maps rule names to their expressions so an operand may be a
    reference to a rule that itself reduces to a character set (the common
    ``Char - '-'`` idiom of the XML specification).

    Raises:
        ValueError: When an operand does not denote a character set, or the
            difference removes every character.
    """
    kind = node[0]
    if kind == "exception":
        left = _resolve_exceptions(node[1], env)
        right = _resolve_exceptions(node[2], env)
        result = _charset_subtract(_reduce_to_charset(left, env, set()),
                                   _reduce_to_charset(right, env, set()))
        if result is None:
            raise ValueError(
                "the EBNF exception '-' is only translatable when both "
                "sides denote character sets (classes, single characters, "
                "or rules that reduce to them)")
        if not result[2] and not result[1]:
            raise ValueError("the EBNF exception removes every character")
        return result
    if kind in {"seq", "alt"}:
        return (kind, [_resolve_exceptions(child, env) for child in node[1]])
    if kind == "quant":
        return ("quant", _resolve_exceptions(node[1], env), node[2])
    return node


class ExternalExpressionTransformer(Transformer):
    """Turn both EBNF dialects' parse trees into the frontend AST."""

    # --- shared shapes ---------------------------------------------------- #

    def name(self, children):
        return ("name", str(children[0]))

    def literal(self, children):
        return ("literal", str(children[0]))

    def sequence(self, children):
        return ("seq", list(children))

    def alternatives(self, children):
        return ("alt", list(children))

    def group(self, children):
        return children[0]

    def optional(self, children):
        return ("quant", children[0], "?")

    def repeat(self, children):
        return ("quant", children[0], "*")

    def rule(self, children):
        return (str(children[0]), children[1])

    def ebnf(self, children):
        return list(children)

    # --- ISO factors ------------------------------------------------------ #

    def repeated(self, children):
        count = None
        quantifier = None
        node = None
        for child in children:
            if isinstance(child, Token) and child.type == "INT":
                count = int(child)
            elif isinstance(child, Token) and child.type == "QUANT":
                quantifier = str(child)
            else:
                node = child
        if quantifier is not None:
            node = ("quant", node, quantifier)
        if count is not None:
            node = _repeat_node(node, count, count)
        return node

    def factor(self, children):
        if len(children) == 1:
            return children[0]
        return ("exception", children[0], children[1])

    # --- W3C forms ---------------------------------------------------------- #

    def w3c_string(self, children):
        # W3C strings have no escape mechanism; the body is literal text
        return ("text", str(children[0])[1:-1])

    def w3c_hexref(self, children):
        return ("charset", frozenset({_w3c_code_point(str(children[0]))}), False)

    def w3c_class(self, children):
        token = str(children[0])
        body = token[1:-1]
        negated = body.startswith("^")
        if negated:
            body = body[1:]
        if not body:
            raise ValueError(f"empty character class {token}")

        def read_point(index):
            if body.startswith("#x", index):
                match = re.match(r"#x[0-9a-fA-F]{1,6}", body[index:])
                if match:
                    return _w3c_code_point(match.group()), index + match.end()
            return body[index], index + 1

        chars = set()
        index = 0
        while index < len(body):
            low, index = read_point(index)
            if index < len(body) - 1 and body[index] == "-":
                high, index = read_point(index + 1)
                if ord(low) > ord(high):
                    raise ValueError(f"range {low!r}-{high!r} in {token} is "
                                     "reversed")
                chars.update(chr(code) for code in range(ord(low), ord(high) + 1))
            else:
                chars.add(low)
        return ("charset", frozenset(chars), negated)

    def w3c_item(self, children):
        node = children[0]
        if len(children) == 2:
            return ("quant", node, str(children[1]))
        return node

    def w3c_factor(self, children):
        node = children[0]
        for right in children[1:]:
            node = ("exception", node, right)
        return node

    def w3c_rule(self, children):
        return (str(children[0]), children[1])

    def w3c(self, children):
        return list(children)


def _w3c_code_point(reference: str) -> str:
    """Decode a W3C ``#xNN`` code-point reference to its character."""
    code_point = int(reference[2:], 16)
    if code_point > 0x10FFFF:
        raise ValueError(f"{reference} is beyond the last code point U+10FFFF")
    return chr(code_point)

def _external_rules_to_eds(rules: "list[tuple[str, object]]") -> str:
    """Lower parsed external grammar expressions into the native EDS syntax."""
    env = {name: node for name, node in rules}
    rules = [(name, _resolve_exceptions(node, env)) for name, node in rules]
    rename = _allocate_eds_names([name for name, _ in rules])
    emitter = _EdsEmitter(nonterminals={rename[name] for name, _ in rules})
    renamed = [(rename[name], _rename_ast(node, rename))
               for name, node in rules]
    return "\n".join(emitter.stringify_rules(renamed)) + "\n"


def _external_document_to_eds(source: str, parser, filename: str,
                              empty_message: str) -> str:
    """Parse one EBNF dialect and lower its rules to EDS."""
    try:
        tree = parser.parse(source)
    except UnexpectedInput as exc:
        raise ValueError(
            format_grammar_syntax_error(exc, source, filename)) from exc
    try:
        rules = ExternalExpressionTransformer().transform(tree)
    except VisitError as exc:
        if isinstance(exc.orig_exc, ValueError):
            raise exc.orig_exc from None
        raise
    if not rules:
        raise ValueError(empty_message)
    return _external_rules_to_eds(rules)


def ebnf_to_eds(source: str) -> str:
    """Convert ISO 14977-style EBNF into Tablewright's native syntax."""
    return _external_document_to_eds(source, _EBNF_PARSER, "<ebnf>",
                                     "EBNF grammar contains no rules")


def w3c_to_eds(source: str) -> str:
    """Convert W3C (XML-specification) EBNF into Tablewright's native syntax."""
    return _external_document_to_eds(source, _W3C_EBNF_PARSER, "<w3c-ebnf>",
                                     "W3C EBNF grammar contains no rules")


# Tablewright's derived grammar of the Lark language, vendored so the
# frontend does not depend on the grammar file shipped inside whichever
# lark package happens to be installed. It is derived from lark's own
# ``grammars/lark.lark`` (MIT, (c) the lark-parser project) and recognizes
# the same documents.
#
# A regex deliberately stays ONE token (``REGEXP``) at this level: its
# extent is a lexical property, and the document's ``%ignore`` terminals
# (inline whitespace, comments) must never apply inside a pattern. The
# token's body is then parsed structurally with Tablewright's regex
# grammar, :data:`_REGEX_LARK_GRAMMAR` above -- together the two layers
# are one derived Lark grammar that includes the regex language.
_TABLEWRIGHT_LARK_GRAMMAR = r"""
start: (_item? _NL)* _item?

_item: rule
     | token
     | statement

rule: RULE rule_params priority? ":" expansions
token: TOKEN token_params priority? ":" expansions

rule_params: ["{" RULE ("," RULE)* "}"]
token_params: ["{" TOKEN ("," TOKEN)* "}"]

priority: "." NUMBER

statement: "%ignore" expansions                    -> ignore
         | "%import" import_path ["->" name]       -> import
         | "%import" import_path name_list         -> multi_import
         | "%override" rule                        -> override_rule
         | "%declare" name+                        -> declare

!import_path: "."? name ("." name)*
name_list: "(" name ("," name)* ")"

?expansions: alias (_VBAR alias)*

?alias: expansion ["->" RULE]

?expansion: expr*

?expr: atom [OP | "~" NUMBER [".." NUMBER]]

?atom: "(" expansions ")"
     | "[" expansions "]" -> maybe
     | value

?value: STRING ".." STRING -> literal_range
      | name
      | (REGEXP | STRING) -> literal
      | name "{" value ("," value)* "}" -> template_usage

name: RULE
    | TOKEN

_VBAR: _NL? "|"
OP: /[+*]|[?](?![a-z])/
RULE: /!?[_?]?[a-z][_a-z0-9]*/
TOKEN: /_?[A-Z][_A-Z0-9]*/
STRING: _STRING "i"?
REGEXP: /\/(?!\/)(\\\/|\\\\|[^\/])*?\/[imslux]*/
_NL: /(\r?\n)+\s*/

%import common.ESCAPED_STRING -> _STRING
%import common.SIGNED_INT -> NUMBER
%import common.WS_INLINE

COMMENT: /\s*/ "//" /[^\n]/* | /\s*/ "#" /[^\n]/*

%ignore WS_INLINE
%ignore COMMENT
"""

_LARK_GRAMMAR_PARSER = Lark(_TABLEWRIGHT_LARK_GRAMMAR, parser="lalr")


class LarkGrammarTransformer(Transformer):
    """Transform the official ``lark.lark`` parse tree into frontend records."""

    def name(self, children):
        return ("name", str(children[0]))

    def literal(self, children):
        return ("literal", str(children[0]))

    def literal_range(self, children):
        return ("literal_range", str(children[0]), str(children[1]))

    def expr(self, children):
        values = [child for child in children if child is not None]
        node = values[0]
        if len(values) == 1:
            return node
        operator = str(values[1])
        if operator in {"?", "*", "+"}:
            return ("quant", node, operator)
        # a counted repetition: expr ~ n or expr ~ n..m
        minimum = int(operator)
        maximum = int(str(values[2])) if len(values) > 2 else minimum
        if maximum < minimum:
            raise ValueError(
                f"Lark repetition ~ {minimum}..{maximum} has max below min")
        return _repeat_node(node, minimum, maximum)

    def expansion(self, children):
        return ("seq", list(children))

    def alias(self, children):
        # Parse aliases according to the official grammar. They only name Lark
        # parse-tree branches and do not affect the recognized language.
        return children[0]

    def expansions(self, children):
        return ("alt", list(children))

    def maybe(self, children):
        return ("quant", children[0], "?")

    def rule_params(self, children):
        if any(child is not None for child in children):
            raise ValueError("Lark templates are not supported")
        return None

    token_params = rule_params

    @staticmethod
    def _definition(children, kind: str):
        # a leading '?' or '!' only shapes Lark's parse tree; strip it
        name = str(children[0]).lstrip("?!")
        expression = next(
            (child for child in reversed(children)
             if isinstance(child, tuple)),
            None,
        )
        if expression is None:
            raise ValueError(f"Lark {kind} {name} has no expression")
        if expression[0] != "alt":
            if expression[0] != "seq":
                expression = ("seq", [expression])
            expression = ("alt", [expression])
        return (kind, name, expression)

    def rule(self, children):
        return self._definition(children, "rule")

    def token(self, children):
        return self._definition(children, "token")

    def override_rule(self, children):
        return children[0]

    def ignore(self, _children):
        return ("directive", "ignore")

    def import_(self, _children):
        return Discard

    def import_path(self, children):
        return children

    def multi_import(self, _children):
        return Discard

    def declare(self, children):
        names = [child[1] for child in children
                 if isinstance(child, tuple) and child[0] == "name"]
        return ("declare", names)

    def priority(self, _children):
        return None

    def start(self, children):
        return [child for child in children if child is not None]


def _reduce_to_charset(node, terminal_asts: dict, visiting: set):
    """Reduce a terminal AST to one character set, or ``None`` if it isn't one.

    A terminal whose whole language is "exactly one character out of this
    set" can stay a *terminal* in EDS (a set definition); anything with
    structure -- repetition, multi-character sequences -- must become a rule.
    References to other terminals are followed (cycles bail out to ``None``
    and are reported later as ordinary rule-level errors).
    """
    kind = node[0] if isinstance(node, tuple) else None
    if kind == "charset":
        return node
    if kind == "literal":
        token = node[1]
        if token.startswith("/"):
            pattern, flags = _split_regex_literal(token)
            return _reduce_to_charset(parse_regex(pattern, flags),
                                      terminal_asts, visiting)
        text, insensitive = _decode_quoted_literal(token)
        if len(text) != 1:
            return None
        chars = _fold_case({text}) if insensitive else {text}
        return ("charset", frozenset(chars), False)
    if kind == "text":
        if len(node[1]) != 1:
            return None
        return ("charset", frozenset(node[1]), False)
    if kind == "exception":
        return _charset_subtract(
            _reduce_to_charset(node[1], terminal_asts, visiting),
            _reduce_to_charset(node[2], terminal_asts, visiting))
    if kind == "literal_range":
        low, _ = _decode_quoted_literal(node[1])
        high, _ = _decode_quoted_literal(node[2])
        if len(low) != 1 or len(high) != 1 or ord(low) > ord(high):
            return None
        return ("charset",
                frozenset(chr(code) for code in range(ord(low), ord(high) + 1)),
                False)
    if kind == "name":
        name = node[1]
        if name in visiting or name not in terminal_asts:
            return None
        visiting.add(name)
        try:
            return _reduce_to_charset(terminal_asts[name], terminal_asts,
                                      visiting)
        finally:
            visiting.discard(name)
    if kind == "seq":
        return (_reduce_to_charset(node[1][0], terminal_asts, visiting)
                if len(node[1]) == 1 else None)
    if kind == "alt":
        parts = [_reduce_to_charset(branch, terminal_asts, visiting)
                 for branch in node[1]]
        if any(part is None for part in parts):
            return None
        if len(parts) == 1:
            return parts[0]
        if any(part[2] for part in parts):
            return None  # a union with a complement is no longer one set
        union = frozenset().union(*(part[1] for part in parts))
        return ("charset", union, False)
    return None

def lark_to_eds(source: str) -> str:
    """Convert a Lark grammar document into Tablewright's native EDS syntax.

    Parser rules become EDS rules. A terminal whose language is one
    character out of a set (``DIGIT: /[0-9]/``, ``SIGN: "+" | "-"``) stays a
    terminal -- an EDS set definition -- while any multi-character terminal
    (``WORD: /[a-z]+/``, ``ARROW: "->"``) is lowered into EDS *rules*, its
    regexes translated by Tablewright's own regex parser. This means such
    tokens are recognized character by character *by the grammar itself*:
    CTLL has no separate lexer, so there is no longest-match tokenization --
    where a real lexer would disambiguate overlapping tokens, the grammar
    must be (q)LL(1) at the character level, and conflicts surface when the
    parse table is built. ``%ignore`` declarations are parsed but cannot be
    honored (there is no token stream to filter); a warning says so.
    """
    try:
        tree = _LARK_GRAMMAR_PARSER.parse(source)
    except UnexpectedInput as exc:
        raise ValueError(format_grammar_syntax_error(exc, source, "<lark>")) from exc
    records = LarkGrammarTransformer().transform(tree)
    definitions = [record for record in records
                   if isinstance(record, tuple) and len(record) == 3
                   and record[0] in {"rule", "token"}]
    declared = {name for record in records
                if isinstance(record, tuple) and record[0] == "declare"
                for name in record[1]}
    if any(isinstance(record, tuple) and record == ("directive", "ignore")
           for record in records):
        logger.warning(
            "%%ignore is parsed but not applied: CTLL grammars read "
            "characters directly (there is no token stream to filter), so "
            "weave optional whitespace into the rules instead")
    parser_rules = [(name, expression) for kind, name, expression in definitions
                    if kind == "rule"]
    terminals = [(name, expression) for kind, name, expression in definitions
                 if kind == "token"]
    if not parser_rules:
        raise ValueError("Lark grammar contains no parser rules")

    # Undefined references are Lark-level errors; report them with Lark
    # terminology before any renaming muddies the water.
    defined = {name for name, _ in parser_rules} | {name for name, _ in terminals}
    referenced = set()
    for _, expression in parser_rules + terminals:
        _collect_referenced_names(expression, referenced)
    missing = sorted(referenced - defined)
    undeclared = [name for name in missing if name not in declared]
    if undeclared:
        raise ValueError(
            "Lark grammar references undefined names: " + ", ".join(undeclared))
    used_declared = sorted(set(missing) & declared)
    if used_declared:
        raise ValueError(
            "%declare terminals have no definition to translate (CTLL has no "
            "external lexer): " + ", ".join(used_declared))

    # Decide which terminals can stay EDS terminal sets. The rest become
    # rules, recognized character by character.
    terminal_asts = dict(terminals)
    set_terminals = {}
    rule_terminals = []
    for name, expression in terminals:
        charset = _reduce_to_charset(expression, terminal_asts, {name})
        if charset is not None:
            set_terminals[name] = charset
        else:
            rule_terminals.append((name, expression))

    # The EDS start symbol is the first rule emitted; honor Lark's 'start'
    # convention when present.
    ordered_rules = sorted(parser_rules,
                           key=lambda rule: rule[0] != "start")

    rename = _allocate_eds_names(
        [name for name, _ in ordered_rules]
        + [name for name, _ in rule_terminals]
        + list(set_terminals))
    nonterminal_names = ({rename[name] for name, _ in ordered_rules}
                         | {rename[name] for name, _ in rule_terminals})
    set_names = {rename[name] for name in set_terminals}

    lines = []
    for name, charset in set_terminals.items():
        members = ", ".join(_eds_escape_char(char)
                            for char in sorted(charset[1], key=ord))
        if charset[2]:
            lines.append(f"{rename[name]} = sigma - {{{members}}}")
        else:
            lines.append(f"{rename[name]} = {{{members}}}")

    emitter = _EdsEmitter(nonterminal_names, set_names)
    eds_rules = [(rename[name], _rename_ast(expression, rename))
                 for name, expression in ordered_rules + rule_terminals]
    lines.extend(emitter.stringify_rules(eds_rules))
    return "\n".join(lines) + "\n"


def convert_to_eds(source: str, language: str) -> str:
    """Normalize a supported input language to the native EDS frontend."""
    converters = {"eds": lambda text: text, "ebnf": ebnf_to_eds,
                  "lark": lark_to_eds, "w3c": w3c_to_eds}
    return converters[language](source)
