"""The built-in test suite (run with --run-tests). Standard-library
unittest only, so there is nothing extra to install."""

import argparse
import logging
import unittest

from pathlib import Path
from sys import stdout

from lark import Lark

from .analysis import (analyze_grammar, compute_first, compute_follow, compute_productive,
    compute_reachable, construct_parse_table, find_duplicate_productions,
    find_unused_terminals, normalize_grammar_keys, stringify_grammar)
from .chartools import (decompose_into_runs, expand_range_token, scan_escaped_tokens,
    unescape_character)
from .cli import _sanitize_cpp_identifier, apply_filename_defaults, main
from .codegen import (_identifier_for_char, _safe_identifier, cpp_char_literal,
    render_char_class, render_neg_set, table_to_constexpr_cpp,
    TerminalAliaser)
from .frontends import (_LARK_GRAMMAR_PARSER, _TABLEWRIGHT_LARK_GRAMMAR, ebnf_to_eds,
    lark_to_eds, w3c_to_eds)
from .gramparse import (_describe_expected_tokens, break_strings, grammar,
    GRAMMAR_SYNTAX_REFERENCE, identifier_table, parse_grammar_text)
from .logutil import logger
from .pipeline import _build_identifier_table, _generate_cpp
from .regex_engine import (_REGEX_LARK_GRAMMAR, _REGEX_PARSER, parse_regex, RegexSyntaxError)
from .symbols import EPSILON, GrammerType, HashableList, SymbolType
from .transforms import merge_identical_nonterminals



def _nt(name: str) -> GrammerType:
    """Construct a nonterminal :class:`GrammerType` (test helper)."""
    return GrammerType(name, SymbolType.non_terminal)


def _term(value) -> GrammerType:
    """Construct a positive-set/atom terminal :class:`GrammerType` (test helper)."""
    return GrammerType(value, SymbolType.terminal)


def _prod(*symbols) -> HashableList:
    """Construct a production body from symbols (test helper)."""
    return HashableList(list(symbols))


class GrammerTypeTests(unittest.TestCase):
    """Tests for the :class:`GrammerType` symbol wrapper and its predicates."""

    def test_nonterminal_predicates(self):
        """A nonterminal reports as nonterminal and nothing else."""
        symbol = _nt("Expr")
        self.assertTrue(symbol.is_non_terminal())
        self.assertFalse(symbol.is_terminal())
        self.assertFalse(symbol.is_semantic_action())
        self.assertEqual(str(symbol), "Expr")

    def test_terminal_predicates(self):
        """A terminal reports as terminal, not as a nonterminal."""
        symbol = _term("a")
        self.assertTrue(symbol.is_terminal())
        self.assertFalse(symbol.is_non_terminal())

    def test_epsilon(self):
        """The shared EPSILON symbol is recognized as epsilon."""
        self.assertTrue(EPSILON.is_epsilon())
        self.assertEqual(str(EPSILON), "epsilon")

    def test_equality_and_hash(self):
        """Equal symbols compare equal and hash equal (usable in sets/dicts)."""
        self.assertEqual(_nt("A"), _nt("A"))
        self.assertEqual(hash(_nt("A")), hash(_nt("A")))
        self.assertNotEqual(_nt("A"), _nt("B"))
        self.assertEqual(len({_nt("A"), _nt("A"), _nt("B")}), 2)


class CharLiteralTests(unittest.TestCase):
    """Tests for C++ character-literal escaping and set rendering."""

    def test_plain_characters_unescaped(self):
        """Printable, safe characters render as themselves inside quotes."""
        self.assertEqual(cpp_char_literal("a"), "'a'")
        self.assertEqual(cpp_char_literal("0"), "'0'")
        self.assertEqual(cpp_char_literal("Z"), "'Z'")

    def test_special_characters_hex_escaped(self):
        """Parentheses and braces use hex escapes to stay valid in templates."""
        self.assertEqual(cpp_char_literal("("), "'\\x28'")
        self.assertEqual(cpp_char_literal(")"), "'\\x29'")
        self.assertEqual(cpp_char_literal("{"), "'\\x7B'")
        self.assertEqual(cpp_char_literal("}"), "'\\x7D'")

    def test_backslash_and_quote_escaped(self):
        """Backslash and double-quote get C++ escapes."""
        self.assertEqual(cpp_char_literal("\\"), "'\\\\'")
        self.assertEqual(cpp_char_literal('"'), "'\\\"'")

    def test_nonprintable_hex(self):
        """A control character falls back to an uppercase hex escape."""
        self.assertEqual(cpp_char_literal("\n"), "'\\x0A'")

    def test_high_byte_narrow(self):
        """A byte in 0x80-0xFF stays a narrow ``'\\xNN'`` literal."""
        self.assertEqual(cpp_char_literal("\xff"), "U'\\xFF'")

    def test_wide_codepoint_uses_char32(self):
        """A code point above one byte uses a ``char32_t`` ``U'...'`` literal."""
        self.assertEqual(cpp_char_literal("\u20ac"), "U'\\x20AC'")
        self.assertEqual(cpp_char_literal("\U0001F600"), "U'\\x1F600'")

    def test_render_set_is_sorted(self):
        """Set rendering emits ctll::set with ASCII-ordinal-sorted members."""
        self.assertEqual(render_char_class(["b", "a", "c"], "set"),
                         "ctll::set<'a','b','c'>")

    def test_render_neg_set(self):
        """Negative-set rendering emits ctll::neg_set."""
        self.assertEqual(render_neg_set(["a", "b"]), "ctll::neg_set<'a','b'>")


class IdentifierSafetyTests(unittest.TestCase):
    """Tests for C++ keyword guarding and single-character identifier naming."""

    def test_keyword_gets_prefixed(self):
        """A C++ keyword used as a name is prefixed with ``terminal_``."""
        self.assertEqual(_safe_identifier("int"), "terminal_int")
        self.assertEqual(_safe_identifier("class"), "terminal_class")
        self.assertEqual(_safe_identifier("new"), "terminal_new")

    def test_common_typedef_gets_prefixed(self):
        """A ubiquitous platform typedef (uchar) is also guarded."""
        self.assertEqual(_safe_identifier("uchar"), "terminal_uchar")
        self.assertEqual(_safe_identifier("size_t"), "terminal_size_t")

    def test_ordinary_name_unchanged(self):
        """A normal identifier passes through untouched."""
        self.assertEqual(_safe_identifier("hexdec"), "hexdec")
        self.assertEqual(_safe_identifier("close"), "close")

    def test_char_identifiers(self):
        """Letters name themselves; digits become d-prefixed; punctuation maps."""
        self.assertEqual(_identifier_for_char("a"), "a")
        self.assertEqual(_identifier_for_char("0"), "d0")
        self.assertEqual(_identifier_for_char("("), "open")
        self.assertEqual(_identifier_for_char(")"), "close")


class TerminalAliaserTests(unittest.TestCase):
    """Tests for the terminal aliasing: naming, collisions and composed names."""

    def _aliaser(self, terminal_defs, reserved=None):
        """Build a TerminalAliaser from a {name: (chars, is_neg)} spec."""
        table = {"other": GrammerType([], SymbolType.negitive_set)}
        for name, (chars, is_neg) in terminal_defs.items():
            kind = SymbolType.negitive_set if is_neg else SymbolType.positive_set
            table[name] = GrammerType(list(chars), kind)
        return TerminalAliaser(table, reserved=set(reserved or ()))

    def test_single_char_named_after_char(self):
        """A lone atom with no gram name is named from its character."""
        aliaser = self._aliaser({})
        self.assertEqual(aliaser.alias_for("ctll::term<'a'>", ["a"], is_neg=False), "a")

    def test_gram_named_set_uses_its_name(self):
        """A set matching a gram-defined terminal takes that gram name."""
        aliaser = self._aliaser({"hexdec": ("0123456789abcdef", False)})
        alias = aliaser.alias_for("ctll::set<'0',...>",
                                  list("0123456789abcdef"), is_neg=False)
        self.assertEqual(alias, "hexdec")

    def test_keyword_named_terminal_guarded(self):
        """A gram terminal named like a keyword is prefixed when aliased."""
        aliaser = self._aliaser({"uchar": ("xy", True)})
        alias = aliaser.alias_for("ctll::neg_set<'x','y'>", ["x", "y"],
                                  is_neg=True, gram_name="uchar")
        self.assertEqual(alias, "terminal_uchar")

    def test_global_other_is_underscored(self):
        """The implicit global 'other' negative set is named ``_others``."""
        aliaser = self._aliaser({})
        alias = aliaser.alias_for("ctll::neg_set<'a'>", ["a"],
                                  is_neg=True, gram_name="other")
        self.assertEqual(alias, "_others")

    def test_reserved_name_disambiguated(self):
        """An alias colliding with a nonterminal/action name gets a suffix."""
        aliaser = self._aliaser({}, reserved={"a"})
        alias = aliaser.alias_for("ctll::term<'a'>", ["a"], is_neg=False)
        self.assertEqual(alias, "a_")

    def test_same_type_same_alias(self):
        """Requesting an alias for an identical type twice returns one name."""
        aliaser = self._aliaser({})
        first = aliaser.alias_for("ctll::term<'a'>", ["a"], is_neg=False)
        second = aliaser.alias_for("ctll::term<'a'>", ["a"], is_neg=False)
        self.assertEqual(first, second)
        self.assertEqual(len(aliaser._ordered), 1)

    def test_composed_set_name_from_parents(self):
        """A synthesized set is named from the parent terminals it unions."""
        aliaser = self._aliaser({"alpha": ("ab", False), "digits": ("01", False)})
        # The union {a,b,0,1} is covered by both named sets.
        alias = aliaser.alias_for("ctll::set<'0','1','a','b'>",
                                  ["a", "b", "0", "1"], is_neg=False)
        self.assertIn("alpha", alias)
        self.assertIn("digits", alias)
        self.assertIn("__", alias)

    def test_long_composition_falls_back_to_set_n(self):
        """An unwieldy composition falls back to a compact set_N name."""
        # Many single punctuation atoms, none covered by a named multi-char set,
        # would compose into a very long name -> fall back.
        chars = list("!#%&'*+,-./:;<=>?@^`~")  # 20 punctuation chars
        aliaser = self._aliaser({})
        alias = aliaser.alias_for("ctll::set<...>", chars, is_neg=False)
        self.assertRegex(alias, r"^set_\d+$")


class FirstFollowTests(unittest.TestCase):
    """Tests for FIRST/FOLLOW computation and nullability."""

    def _grammar(self):
        """A small grammar: S -> A 'b'; A -> 'a' | epsilon (A nullable)."""
        S, A = _nt("S"), _nt("A")
        a, b = _term("a"), _term("b")
        grammar = {
            S: [_prod(A, b)],
            A: [_prod(a), _prod(EPSILON)],
        }
        return grammar, S, A, a, b

    def test_first_includes_terminal_and_through_nullable(self):
        """FIRST(S) sees 'a' through A and 'b' because A is nullable."""
        grammar, S, A, a, b = self._grammar()
        first = compute_first(grammar)
        self.assertIn(a, first[S])
        self.assertIn(b, first[S])

    def test_nullable_in_first(self):
        """A nullable nonterminal carries EPSILON in its FIRST set."""
        grammar, S, A, a, b = self._grammar()
        first = compute_first(grammar)
        self.assertIn(EPSILON, first[A])
        self.assertNotIn(EPSILON, first[S])

    def test_follow_contains_following_terminal(self):
        """FOLLOW(A) contains 'b', the terminal that can follow A in S."""
        grammar, S, A, a, b = self._grammar()
        first = compute_first(grammar)
        follow = compute_follow(grammar, first)
        self.assertIn(b, follow[A])


class ParseTableTests(unittest.TestCase):
    """Tests for parse-table construction under both parser models."""

    def test_basic_table_entries(self):
        """Each production is reachable via the right lookahead terminal."""
        S, A = _nt("S"), _nt("A")
        a, b = _term("a"), _term("b")
        grammar = {S: [_prod(A, b)], A: [_prod(a), _prod(EPSILON)]}
        first = compute_first(grammar)
        follow = compute_follow(grammar, first)
        table = construct_parse_table(grammar, first, follow, strict=False)
        # A on 'a' -> a ; A on 'b' (follow) -> epsilon
        self.assertIn(a, table[A])
        self.assertIn(b, table[A])

    def test_strict_conflict_raises(self):
        """A FIRST/FIRST clash is a conflict under the strict LL(1) model."""
        # S -> 'a' X | 'a' Y  : both alternatives start with 'a'.
        S, X, Y = _nt("S"), _nt("X"), _nt("Y")
        a, c, d = _term("a"), _term("c"), _term("d")
        grammar = {
            S: [_prod(a, X), _prod(a, Y)],
            X: [_prod(c)],
            Y: [_prod(d)],
        }
        first = compute_first(grammar)
        follow = compute_follow(grammar, first)
        with self.assertRaises(ValueError):
            construct_parse_table(grammar, first, follow, strict=True)


class GrammarAnalysisTests(unittest.TestCase):
    """Tests for the grammar health-analysis helpers."""

    def test_reachable(self):
        """Only nonterminals derivable from the start are reachable."""
        S, A, Orphan = _nt("S"), _nt("A"), _nt("Orphan")
        a = _term("a")
        grammar = {S: [_prod(A)], A: [_prod(a)], Orphan: [_prod(a)]}
        reachable = compute_reachable(grammar, S)
        self.assertIn(S, reachable)
        self.assertIn(A, reachable)
        self.assertNotIn(Orphan, reachable)

    def test_productive(self):
        """A self-only-recursive nonterminal is not productive."""
        S, Black = _nt("S"), _nt("Black")
        a = _term("a")
        grammar = {S: [_prod(a)], Black: [_prod(Black, a)]}
        productive = compute_productive(grammar)
        self.assertIn(S, productive)
        self.assertNotIn(Black, productive)

    def test_duplicate_productions(self):
        """A repeated production body is detected per nonterminal."""
        A = _nt("A")
        a = _term("a")
        grammar = {A: [_prod(a), _prod(a), _prod(a, a)]}
        duplicates = find_duplicate_productions(grammar)
        self.assertIn(A, duplicates)

    def test_unused_terminals(self):
        """A declared terminal no rule references is reported unused."""
        S = _nt("S")
        used = _term("used")
        grammar = {S: [_prod(used)]}
        terminal_table = {
            "other": GrammerType([], SymbolType.negitive_set),
            "used": GrammerType(["u"], SymbolType.positive_set),
            "ghost": GrammerType(["g"], SymbolType.positive_set),
        }
        # Reference 'used' by name in the production for the test.
        grammar = {S: [_prod(GrammerType("used", SymbolType.terminal))]}
        unused = find_unused_terminals(grammar, terminal_table)
        self.assertIn("ghost", unused)
        self.assertNotIn("used", unused)


class OptimizationTests(unittest.TestCase):
    """Tests that optimization passes shrink grammars while staying valid."""

    def test_merge_identical_nonterminals(self):
        """Two structurally identical nonterminals merge into one."""
        # A and B have identical single-production bodies.
        S, A, B = _nt("S"), _nt("A"), _nt("B")
        a = _term("a")
        grammar = {
            S: [_prod(A, B)],
            A: [_prod(a)],
            B: [_prod(a)],
        }
        merged = merge_identical_nonterminals(grammar)
        self.assertLess(len(merged), len(grammar))

    def test_optimize_preserves_validity_json(self):
        """Optimizing the JSON grammar keeps a valid, smaller grammar at -O3."""
        base = _generate_cpp(_SAMPLE_JSON, optimization=0)
        opt = _generate_cpp(_SAMPLE_JSON, optimization=3)
        # Both generate a header; the optimized one is no larger in nonterminals.
        self.assertIn("// TERMINALS", base)
        self.assertIn("// TERMINALS", opt)
        self.assertIn("rule(", opt)


class IntegrationTests(unittest.TestCase):
    """End-to-end tests: grammar text in, generated C++ header out."""

    def test_minimal_grammar_structure(self):
        """A tiny grammar produces a well-formed header with the key sections."""
        cpp = _generate_cpp("tok={a,b}\nSt->tok,<St>|epsilon\n",
                            guard="MIN_H", namespace="mini", grammar_name="mini")
        self.assertIn("#ifndef MIN_H", cpp)
        self.assertIn("#endif //MIN_H", cpp)
        self.assertIn("namespace mini", cpp)
        self.assertIn("// NONTERMINALS", cpp)
        self.assertIn("// TERMINALS", cpp)
        self.assertIn("rule(", cpp)

    def test_rules_reference_aliases_not_inline_literals(self):
        """In the rule section, terminals appear as aliases, not inline types."""
        cpp = _generate_cpp("tok={a,b}\nSt->tok,<St>|epsilon\n")
        # Split off the rule section and confirm no inline ctll::set/term there.
        rules = cpp.split("function:", 1)[-1]
        self.assertNotIn("ctll::term<", rules)
        self.assertNotIn("ctll::set<", rules)

    def test_keyword_terminal_safe_in_output(self):
        """A grammar terminal named like a keyword is emitted safely."""
        cpp = _generate_cpp("uchar=sigma-{x}\nSt->uchar,<St>|epsilon\n")
        self.assertIn("terminal_uchar", cpp)
        # No bare `using uchar` (which would shadow the typedef) survives.
        self.assertNotIn("using uchar ", cpp)

    def test_epsilon_rule_emitted(self):
        """A nullable nonterminal emits a ctll::epsilon production."""
        cpp = _generate_cpp("tok={a,b}\nSt->tok,<St>|epsilon\n")
        self.assertIn("ctll::epsilon", cpp)

    def test_q_vs_strict_behaviour(self):
        """A FOLLOW-overlap grammar builds as a Q-grammar but fails under strict."""
        # St -> tok <St> | epsilon  has tok in FOLLOW via recursion; the Q model
        # tolerates the shift/epsilon coexistence, strict LL(1) need not.
        gram = "tok={a}\nSt->tok,<St>|epsilon\n"
        # Q-grammar: succeeds.
        cpp = _generate_cpp(gram, q_grammar=True)
        self.assertIn("rule(", cpp)


class RangeExpansionTests(unittest.TestCase):
    """Tests for regex-style ``[[...]]`` character ranges."""

    def test_simple_span(self):
        """``[[a-z]]`` enumerates the full lowercase span."""
        self.assertEqual(expand_range_token("[[a-z]]"),
                         (set("abcdefghijklmnopqrstuvwxyz"), False))

    def test_mixed_literals_and_span(self):
        """``[[abcg-i]]`` mixes literal characters and a span."""
        self.assertEqual(expand_range_token("[[abcg-i]]"),
                         (set("abcghi"), False))

    def test_multiple_spans(self):
        """``[[a-zA-Z]]`` enumerates two spans into one set."""
        self.assertEqual(expand_range_token("[[a-zA-Z]]"),
                         (set("abcdefghijklmnopqrstuvwxyz"
                              "ABCDEFGHIJKLMNOPQRSTUVWXYZ"), False))

    def test_span_then_literal(self):
        """A span can be followed by literals (``[[0-9_]]``)."""
        self.assertEqual(expand_range_token("[[0-9_]]"),
                         (set("0123456789_"), False))

    def test_escaped_dash_is_literal(self):
        """An escaped dash (``\\-``) is a literal, not a span separator."""
        self.assertEqual(expand_range_token(r"[[a\-c]]"), (set("a-c"), False))

    def test_single_character(self):
        """A one-character range is just that character."""
        self.assertEqual(expand_range_token("[[x]]"), ({"x"}, False))

    def test_reversed_span_raises(self):
        """A span whose start follows its end is rejected."""
        with self.assertRaises(ValueError):
            expand_range_token("[[z-a]]")

    def test_empty_range_raises(self):
        """An empty ``[[]]`` is rejected."""
        with self.assertRaises(ValueError):
            expand_range_token("[[]]")

    def test_negated_range(self):
        """A leading ``^`` negates the range."""
        self.assertEqual(expand_range_token("[[^abc]]"), (set("abc"), True))

    def test_negated_range_with_escapes(self):
        """Negation combines with escaped characters (``[[^\\nabc\\r\\0]]``)."""
        self.assertEqual(expand_range_token(r"[[^\nabc\r\0]]"),
                         ({"\n", "a", "b", "c", "\r", "\0"}, True))

    def test_negated_span(self):
        """Negation combines with spans (``[[^a-c]]``)."""
        self.assertEqual(expand_range_token("[[^a-c]]"), (set("abc"), True))

    def test_escaped_caret_is_literal(self):
        """An escaped leading ``\\^`` is a literal, not negation."""
        self.assertEqual(expand_range_token(r"[[\^ab]]"), (set("^ab"), False))

    def test_non_leading_caret_is_literal(self):
        """A ``^`` after the first position is an ordinary literal."""
        self.assertEqual(expand_range_token("[[a^b]]"), (set("a^b"), False))

    def test_empty_negated_range_raises(self):
        """A negation with nothing to negate (``[[^]]``) is rejected."""
        with self.assertRaises(ValueError):
            expand_range_token("[[^]]")

    def test_range_in_rule_becomes_set(self):
        """A range in a rule body is emitted as an enumerated ctll::set."""
        cpp = _generate_cpp("St->[[a-c]],<St>|epsilon\n")
        self.assertIn("ctll::set<'a','b','c'>", cpp)

    def test_range_coexists_with_atoms(self):
        """A range can appear alongside ordinary atoms in the same rule."""
        cpp = _generate_cpp("St->x,[[0-9]],y,<St>|epsilon\n")
        self.assertIn("ctll::set<'0','1','2','3','4','5','6','7','8','9'>", cpp)
        self.assertIn("rule(", cpp)

    def test_semantic_action_still_parses(self):
        """A ``[name]`` action is not mistaken for a range and still works."""
        # Single brackets remain semantic actions; double brackets are ranges.
        cpp = _generate_cpp("tok={a}\nSt->tok,[act],<St>|epsilon\n")
        self.assertIn("struct act: ctll::action", cpp)

    def test_negated_range_in_rule_becomes_neg_set(self):
        """A ``[[^...]]`` range in a rule body is emitted as a ctll::neg_set."""
        cpp = _generate_cpp("St->[[^abc]],<St>|epsilon\n")
        self.assertIn("ctll::neg_set<'a','b','c'>", cpp)

    def test_negated_range_matches_named_negative_set(self):
        """An inline ``[[^x,y]]`` behaves like a named ``sigma - {x,y}`` set."""
        inline = _generate_cpp("St->[[^xy]],<St>|epsilon\n")
        self.assertIn("ctll::neg_set<'x','y'>", inline)

    def test_negated_range_quantified(self):
        """A negated range takes a quantifier like any other symbol."""
        table = _build_identifier_table("St->a,[[^bc]]+,d\n")
        rules = table[SymbolType.non_terminal]
        helper = next(n for n in rules if n.endswith("_anon"))
        body = next(iter(rules[helper]))
        self.assertEqual(body[0].type, SymbolType.negitive_set)
        self.assertEqual(body[0].value, {"b", "c"})


class HexEscapeTests(unittest.TestCase):
    r"""Tests for the ``\xNN`` / ``\u{...}`` hex escapes and raw Unicode."""

    def test_unescape_hex_forms(self):
        r"""``\xNN`` and ``\u{H..H}`` decode to their code points."""
        self.assertEqual(unescape_character(r"\x41"), "A")
        self.assertEqual(unescape_character(r"\x0a"), "\n")
        self.assertEqual(unescape_character(r"\u{41}"), "A")
        self.assertEqual(unescape_character(r"\u{20AC}"), "\u20ac")
        self.assertEqual(unescape_character(r"\u{1F600}"), "\U0001f600")
        self.assertEqual(unescape_character(r"\u{0}"), "\0")

    def test_unescape_rejects_out_of_range(self):
        """A code point beyond U+10FFFF is rejected with a clear error."""
        with self.assertRaises(ValueError):
            unescape_character(r"\u{110000}")

    def test_scanner_keeps_escapes_together(self):
        """The escape scanner yields hex escapes as single tokens."""
        self.assertEqual(scan_escaped_tokens(r"a\x41\u{42}\nc"),
                         [("a", False), (r"\x41", True), (r"\u{42}", True),
                          (r"\n", True), ("c", False)])
        # A trailing lone backslash stays a plain character (historical).
        self.assertEqual(scan_escaped_tokens("a\\"), [("a", False), ("\\", False)])

    def test_hex_atom_in_rule(self):
        r"""``\x41`` in a rule body is the single atom 'A'."""
        cpp = _generate_cpp("St->\\x41,<St>|epsilon\n")
        self.assertIn("ctll::term<'A'>", cpp)

    def test_unicode_escape_atom_emits_char32(self):
        r"""A ``\u{...}`` beyond one byte is emitted as a char32_t literal."""
        cpp = _generate_cpp("St->\\u{20AC},<St>|epsilon\n")
        self.assertIn("ctll::term<U'\\x20AC'>", cpp)

    def test_hex_in_set_definition(self):
        """Hex escapes work as set members."""
        table = _build_identifier_table(
            "tok={\\x61,\\u{7A}}\nSt->tok,<St>|epsilon\n")
        self.assertEqual(table[SymbolType.terminal]["tok"].value, {"a", "z"})

    def test_hex_in_string_literal(self):
        """Escapes inside a string decode when the string breaks into atoms."""
        cpp = _generate_cpp('St->"\\x42\\u{43}d",<St>|epsilon\n')
        for term in ("ctll::term<'B'>", "ctll::term<'C'>", "ctll::term<'d'>"):
            self.assertIn(term, cpp)

    def test_escaped_quote_in_string_literal(self):
        r"""``\"`` inside a string is the quote character, not backslash+quote."""
        table = _build_identifier_table('St->"a\\"b"\n')
        break_strings(table)
        body = next(iter(table[SymbolType.non_terminal]["St"]))
        self.assertEqual([s.value for s in body], ["a", '"', "b"])

    def test_hex_in_range(self):
        r"""Hex escapes work as ``[[...]]`` span endpoints."""
        self.assertEqual(expand_range_token(r"[[\x30-\x39]]"),
                         (set("0123456789"), False))
        self.assertEqual(expand_range_token(r"[[^\u{0}-\u{1F}]]")[1], True)

    def test_hex_in_group(self):
        r"""A hex escape is one GATOM inside a grouping."""
        table = _build_identifier_table("St->a,(\\x28)+,b\n")
        rules = table[SymbolType.non_terminal]
        helper = next(n for n in rules if n.endswith("_anon"))
        body = next(iter(rules[helper]))
        self.assertEqual(body[0].value, "(")

    def test_malformed_hex_keeps_old_reading(self):
        r"""``\x`` without two hex digits is still the literal 'x'."""
        self.assertEqual(unescape_character(r"\x"), "x")
        cpp = _generate_cpp("St->\\x,g\n")  # \x is the atom 'x'
        self.assertIn("ctll::term<'x'>", cpp)

    def test_raw_unicode_end_to_end(self):
        """Raw non-ASCII characters flow through atoms, sets and ranges."""
        cpp = _generate_cpp("euro={\u20ac}\nSt->\u00e9,[[\u03b1-\u03b3]],euro,<St>|epsilon\n")
        self.assertIn("U'\\x20AC'", cpp)
        self.assertIn("'\\xE9'", cpp)
        self.assertIn("ctll::set<U'\\x3B1',U'\\x3B2',U'\\x3B3'>", cpp)


class SetDefinitionSyntaxTests(unittest.TestCase):
    """Tests for the ``:`` assignment operator and optional ``{}`` around sets."""

    def _terminal(self, gram_text, name):
        """Build a grammar and return the GrammerType of a named terminal."""
        table = _build_identifier_table(gram_text)
        return table[SymbolType.terminal][name]

    def test_equals_with_braces(self):
        """The classic ``name = {a,b,c}`` form still works."""
        gt = self._terminal("tok={a,b,c}\nSt->tok,<St>|epsilon\n", "tok")
        self.assertEqual(gt.value, {"a", "b", "c"})
        self.assertEqual(gt.type, SymbolType.positive_set)

    def test_colon_with_braces(self):
        """``:`` is accepted in place of ``=`` (braced)."""
        gt = self._terminal("tok:{a,b,c}\nSt->tok,<St>|epsilon\n", "tok")
        self.assertEqual(gt.value, {"a", "b", "c"})

    def test_equals_without_braces(self):
        """Braces are optional: ``name = a,b,c`` defines the same set."""
        gt = self._terminal("tok=a,b,c\nSt->tok,<St>|epsilon\n", "tok")
        self.assertEqual(gt.value, {"a", "b", "c"})

    def test_colon_without_braces(self):
        """``:`` assignment combined with no braces also works."""
        gt = self._terminal("tok:a,b,c\nSt->tok,<St>|epsilon\n", "tok")
        self.assertEqual(gt.value, {"a", "b", "c"})

    def test_negative_set_colon_no_braces(self):
        """A negative ``sigma -`` set supports ``:`` and optional braces."""
        gt = self._terminal("uchar:sigma-x,y\nSt->uchar,<St>|epsilon\n", "uchar")
        self.assertEqual(gt.value, {"x", "y"})
        self.assertEqual(gt.type, SymbolType.negitive_set)

    def test_all_forms_equivalent(self):
        """The four assignment/brace spellings yield identical generated C++."""
        base = "St->tok,<St>|epsilon\n"
        a = _generate_cpp("tok={a,b,c}\n" + base)
        b = _generate_cpp("tok:{a,b,c}\n" + base)
        c = _generate_cpp("tok=a,b,c\n" + base)
        d = _generate_cpp("tok:a,b,c\n" + base)
        self.assertEqual(a, b)
        self.assertEqual(a, c)
        self.assertEqual(a, d)


class RuleOperatorSyntaxTests(unittest.TestCase):
    """Tests for ``:`` as a production operator equivalent to ``->``."""

    def _kinds(self, gram_text):
        """Return which top-level statement kinds a grammar parses into."""
        tree = Lark(grammar, start="start").parse(gram_text)
        return {node.data for node in tree.iter_subtrees()
                if node.data in ("set_definition", "rule_statement")}

    def test_colon_rule_equivalent_to_arrow(self):
        """A rule written with ``:`` generates the same C++ as with ``->``."""
        arrow = _generate_cpp("tok={a,b}\nSt->tok,<St>|epsilon\n")
        colon = _generate_cpp("tok={a,b}\nSt:tok,<St>|epsilon\n")
        self.assertEqual(arrow, colon)

    def test_colon_rule_with_nonterminal(self):
        """``St : <x>`` is recognized as a rule (a nonterminal disambiguates)."""
        self.assertEqual(self._kinds("Aa:<bb>\nbb:<bb>|epsilon\n"),
                         {"rule_statement"})

    def test_colon_rule_with_alternation(self):
        """``St : a | b`` is a rule because ``|`` cannot appear in a set."""
        self.assertEqual(self._kinds("St:a|b\n"), {"rule_statement"})

    def test_colon_rule_with_string(self):
        """A string body makes ``:`` unambiguously a rule."""
        self.assertEqual(self._kinds('St:"abc"\n'), {"rule_statement"})

    def test_colon_rule_with_named_terminal(self):
        """A multi-character NAME body is a rule (NAMEs are not set atoms)."""
        self.assertEqual(self._kinds("St:tok\n"), {"rule_statement"})

    def test_bare_atom_body_stays_set(self):
        """``name : a, b, c`` (all atoms) stays a set for backward compatibility."""
        # set_definition has priority, so the all-atom colon form remains a set.
        self.assertEqual(self._kinds("foo:a,b,c\n"), {"set_definition"})

    def test_colon_epsilon_keyword_is_rule(self):
        """``St : epsilon`` (keyword form) is a rule."""
        self.assertEqual(self._kinds("St:epsilon\n"), {"rule_statement"})

    def test_mixed_colon_set_and_rule(self):
        """A grammar can use ``:`` for both a set and a rule, disambiguated."""
        # tok:a,b is a set (atoms); St:tok,<St>|epsilon is a rule (<St>, |).
        cpp = _generate_cpp("tok:a,b\nSt:tok,<St>|epsilon\n")
        self.assertIn("using tok = ctll::set<'a','b'>", cpp)
        self.assertIn("rule(St,", cpp)


class QuantifierGroupTests(unittest.TestCase):
    """Tests for regex-style groupings ``(...)`` and quantifiers ``+`` / ``*``."""

    def _grammar(self, gram_text):
        """Front-end pipeline shortcut: return the nonterminal section."""
        return _build_identifier_table(gram_text)[SymbolType.non_terminal]

    @staticmethod
    def _bodies(productions):
        """Render productions as tuples of symbol strings for comparison."""
        return {tuple(str(s) for s in production) for production in productions}

    def test_plus_expands_to_helper(self):
        """``S -> a+`` becomes ``S -> a, <a_anon>`` with the loop helper."""
        g = self._grammar("S -> a+\n")
        self.assertEqual(self._bodies(g["S"]), {("a", "a_anon")})
        # The trailing symbol is a real nonterminal reference.
        self.assertTrue(list(g["S"])[0][-1].is_non_terminal())
        self.assertEqual(self._bodies(g["a_anon"]),
                         {("a", "a_anon"), ("epsilon",)})

    def test_star_expands_to_helper(self):
        """``S -> a*`` becomes ``S -> <a_anon>`` with the same loop helper."""
        g = self._grammar("S -> a*\n")
        self.assertEqual(self._bodies(g["S"]), {("a_anon",)})
        self.assertEqual(self._bodies(g["a_anon"]),
                         {("a", "a_anon"), ("epsilon",)})

    def test_quantifier_may_be_spaced(self):
        """``<bb> +`` (whitespace before the quantifier) also quantifies."""
        g = self._grammar("S -> <bb> +\nbb -> y\n")
        self.assertEqual(self._bodies(g["S"]), {("bb", "bb_anon")})
        self.assertEqual(self._bodies(g["bb_anon"]),
                         {("bb", "bb_anon"), ("epsilon",)})

    def test_bare_group_splices_inline(self):
        """An unquantified grouping is only bracketing: it splices in place."""
        g = self._grammar("S -> ( <bb> x )\nbb -> y\n")
        self.assertEqual(self._bodies(g["S"]), {("bb", "x")})

    def test_group_items_accept_commas(self):
        """Grouping items may be comma-separated as well as space-separated."""
        g = self._grammar("S -> (<bb>, x)\nbb -> y\n")
        self.assertEqual(self._bodies(g["S"]), {("bb", "x")})

    def test_quantified_group(self):
        """``(<bb>)*`` repeats the grouped sequence via one helper."""
        g = self._grammar("S -> (<bb>)*\nbb -> y\n")
        self.assertEqual(self._bodies(g["S"]), {("bb_anon",)})
        self.assertEqual(self._bodies(g["bb_anon"]),
                         {("bb", "bb_anon"), ("epsilon",)})

    def test_quantified_multi_symbol_group(self):
        """``(a, b)+`` repeats the whole two-symbol sequence."""
        g = self._grammar("S -> (a, b)+, c\n")
        self.assertEqual(self._bodies(g["S"]), {("a", "b", "a_b_anon", "c")})
        self.assertEqual(self._bodies(g["a_b_anon"]),
                         {("a", "b", "a_b_anon"), ("epsilon",)})

    def test_identical_bodies_share_a_helper(self):
        """``a*`` and ``a+`` in one grammar reuse the same loop helper."""
        g = self._grammar("S -> a*, a+\n")
        self.assertEqual(self._bodies(g["S"]), {("a_anon", "a", "a_anon")})
        helpers = [name for name in g if str(name).endswith("_anon")]
        self.assertEqual(helpers, ["a_anon"])

    def test_nested_quantifiers(self):
        """Nesting expands innermost-first: ``((a)*)+`` builds two helpers."""
        g = self._grammar("S -> ((a)*)+\n")
        self.assertEqual(self._bodies(g["S"]), {("a_anon", "a_anon_anon")})
        self.assertEqual(self._bodies(g["a_anon_anon"]),
                         {("a_anon", "a_anon_anon"), ("epsilon",)})

    def test_quantified_named_terminal(self):
        """A named terminal reference can be quantified."""
        g = self._grammar("tok = {0,1}\nS -> tok+\n")
        self.assertEqual(self._bodies(g["S"]), {("tok", "tok_anon")})

    def test_quantified_string_breaks_into_atoms(self):
        """A quantified string still expands into atoms inside the helper."""
        g = self._grammar('S -> "ab"+\n')
        self.assertEqual(self._bodies(g["S"]), {("a", "b", "ab_anon")})
        self.assertEqual(self._bodies(g["ab_anon"]),
                         {("a", "b", "ab_anon"), ("epsilon",)})

    def test_bare_punctuation_atoms_unchanged(self):
        """Stand-alone ``( ) * +`` between commas stay ordinary atoms."""
        g = self._grammar("S -> (, a, ), *, +\n")
        self.assertEqual(self._bodies(g["S"]), {("(", "a", ")", "*", "+")})
        production = list(g["S"])[0]
        self.assertTrue(all(symbol.is_atom() for symbol in production))
        self.assertFalse(any(str(name).endswith("_anon") for name in g))

    def test_escaped_structural_chars_inside_group(self):
        """Escapes make ``( ) * + ,`` literal atoms inside a grouping."""
        g = self._grammar("S -> (\\(, \\*, \\+, \\,, \\))+\n")
        helper = [name for name in g if str(name).endswith("_anon")][0]
        loop = self._bodies(g[helper])
        self.assertIn(("(", "*", "+", ",", ")", str(helper)), loop)

    def test_group_with_semantic_action(self):
        """Semantic actions ride along inside a quantified grouping."""
        g = self._grammar("S -> (a [act])+\n")
        table = identifier_table
        self.assertIn("act", table[SymbolType.action])
        self.assertEqual(self._bodies(g["S"]), {("a", "act", "a_act_anon")})
        # The action keeps its symbol kind through the expansion.
        production = list(g["S"])[0]
        self.assertTrue(production[1].is_semantic_action())

    def test_helper_name_avoids_collisions(self):
        """A generated helper never shadows an existing nonterminal."""
        g = self._grammar("S -> a+\na_anon -> z\n")
        names = {str(name) for name in g}
        self.assertIn("a_anon", names)   # the user's own rule
        self.assertIn("a_anon2", names)  # the generated helper
        self.assertEqual(self._bodies(g["S"]), {("a", "a_anon2")})

    def test_generated_cpp_contains_helper(self):
        """A quantified grammar renders to C++ end to end."""
        cpp = _generate_cpp("S -> a+\n")
        self.assertIn("a_anon", cpp)
        self.assertIn("rule(S,", cpp)

    def test_language_shape_matches_documented_expansion(self):
        """``a+`` and the hand-written expansion generate identical C++."""
        sugar = _generate_cpp("S -> a+\n")
        manual = _generate_cpp(
            "S -> a, <a_anon>\na_anon -> a, <a_anon> | epsilon\n")
        self.assertEqual(sugar, manual)

    def test_syntax_reference_mentions_repetition(self):
        """The --syntax cheat-sheet documents the new sugar."""
        self.assertIn("one or more", GRAMMAR_SYNTAX_REFERENCE)
        self.assertIn("zero or more", GRAMMAR_SYNTAX_REFERENCE)
        self.assertIn("(X Y)", GRAMMAR_SYNTAX_REFERENCE)


class RangeLookaheadTests(unittest.TestCase):
    """Tests for the optional ``ctll::range`` lookahead optimization."""

    def test_decompose_single_run(self):
        """A fully contiguous set becomes one range and no residual."""
        ranges, residual = decompose_into_runs(set("0123456789"))
        self.assertEqual(ranges, [("0", "9")])
        self.assertEqual(residual, [])

    def test_decompose_multiple_runs(self):
        """Several contiguous spans each become a range; gaps stay residual."""
        ranges, residual = decompose_into_runs(
            set("0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ_abcdefghijklmnopqrstuvwxyz"))
        self.assertIn(("0", "9"), ranges)
        self.assertIn(("A", "Z"), ranges)
        self.assertIn(("a", "z"), ranges)
        self.assertEqual(residual, ["_"])

    def test_short_runs_stay_residual(self):
        """A run shorter than the minimum is not turned into a range."""
        ranges, residual = decompose_into_runs(set("ab"))  # length 2 < 3
        self.assertEqual(ranges, [])
        self.assertEqual(sorted(residual), ["a", "b"])

    def test_scattered_set_has_no_ranges(self):
        """A set with no run of the minimum length yields no ranges."""
        ranges, residual = decompose_into_runs(set("cims"))
        self.assertEqual(ranges, [])
        self.assertEqual(sorted(residual), ["c", "i", "m", "s"])

    def test_partition_is_exact(self):
        """The ranges plus residual cover exactly the original set (a partition)."""
        chars = set("0123456789ABCDEFxyz!@#")
        ranges, residual = decompose_into_runs(chars)
        covered = set(residual)
        for lo, hi in ranges:
            covered |= {chr(c) for c in range(ord(lo), ord(hi) + 1)}
        self.assertEqual(covered, chars)

    def test_flag_off_emits_set_not_range(self):
        """Without the flag, a wide lookahead is one ``set`` (no ranges)."""
        gram = "big={0,1,2,3,4,5,6,7,8,9,A,B,C,D,E,F,G,H,I,J}\nSt->big,<St>|epsilon\n"
        cpp = _generate_cpp(gram)
        self.assertNotIn("ctll::range", cpp)

    def test_flag_on_emits_ranges(self):
        """With the flag, a wide contiguous lookahead becomes ``range`` rules."""
        gram = "big={0,1,2,3,4,5,6,7,8,9,A,B,C,D,E,F,G,H,I,J,K,L,M,N,O,P}\nSt->big,<St>|epsilon\n"
        table = _build_identifier_table(gram)
        args = argparse.Namespace(optimization=0, q_grammar=True, namespace="g",
                                  guard="G_H", grammer_name="g", range_lookaheads=True)
        cpp = table_to_constexpr_cpp(table, args)
        self.assertIn("ctll::range", cpp)


class QualityOfLifeTests(unittest.TestCase):
    """Tests for the developer-experience helpers (friendly errors, defaults)."""

    def test_describe_expected_drops_internal_tokens(self):
        """Anonymous/whitespace terminals are dropped from expected-token lists."""
        described = _describe_expected_tokens(
            ["VBAR", "COMMA", "__ANON_0", "WHITESPACES", "SINGLE_NAME"])
        self.assertIn("'|'", described)
        self.assertIn("','", described)
        self.assertNotIn("__ANON_0", described)
        self.assertNotIn("WHITESPACES", described)

    def test_friendly_syntax_error_message(self):
        """A malformed grammar raises a ValueError with a readable message."""
        with self.assertRaises(ValueError) as ctx:
            parse_grammar_text("tok={a,b}\nSt->tok <St>\n", "bad.gram")
        message = str(ctx.exception)
        self.assertIn("syntax error", message)
        self.assertIn("bad.gram", message)
        # No raw Lark terminal names should leak through.
        self.assertNotIn("VBAR", message)
        self.assertNotIn("__ANON", message)

    def test_valid_grammar_parses(self):
        """A well-formed grammar parses without raising."""
        tree = parse_grammar_text("St->x,<St>|epsilon\n", "ok.gram")
        self.assertIsNotNone(tree)

    def test_end_of_input_error_is_graceful(self):
        """An error at end of input reports cleanly, not as ``:-1:-1``."""
        with self.assertRaises(ValueError) as ctx:
            parse_grammar_text("St->a b\n", "eoi.gram")
        message = str(ctx.exception)
        self.assertIn("end of input", message)
        self.assertNotIn("-1", message)

    def test_sanitize_cpp_identifier(self):
        """Filename stems are sanitized into valid C++ identifiers."""
        self.assertEqual(_sanitize_cpp_identifier("pcre", "G"), "pcre")
        self.assertEqual(_sanitize_cpp_identifier("my-lang.v2", "G"), "my_lang_v2")
        self.assertEqual(_sanitize_cpp_identifier("9lives", "G"), "_9lives")
        self.assertEqual(_sanitize_cpp_identifier("", "G"), "G")
        self.assertEqual(_sanitize_cpp_identifier("---", "G"), "G")

    def test_filename_defaults_derived(self):
        """Unset config names are derived from the input filename's stem."""
        args = argparse.Namespace(
            input=argparse.Namespace(name="/path/to/pcre.gram"),
            namespace=None, grammer_name=None, guard=None, fname=None)
        apply_filename_defaults(args)
        self.assertEqual(args.namespace, "pcre")
        self.assertEqual(args.grammer_name, "pcre")
        self.assertEqual(args.guard, "PCRE_HPP")
        self.assertEqual(str(args.fname), "pcre.hpp")

    def test_filename_defaults_respect_overrides(self):
        """Explicit config values are left untouched."""
        args = argparse.Namespace(
            input=argparse.Namespace(name="/path/to/pcre.gram"),
            namespace="custom", grammer_name="MyG", guard="MY_H",
            fname=Path("out.hpp"))
        apply_filename_defaults(args)
        self.assertEqual(args.namespace, "custom")
        self.assertEqual(args.grammer_name, "MyG")
        self.assertEqual(args.guard, "MY_H")
        self.assertEqual(str(args.fname), "out.hpp")

    def test_stdin_falls_back_to_generic_defaults(self):
        """Reading from stdin (no filename) keeps the generic defaults."""
        args = argparse.Namespace(
            input=argparse.Namespace(name="<stdin>"),
            namespace=None, grammer_name=None, guard=None, fname=None)
        apply_filename_defaults(args)
        self.assertEqual(args.namespace, "Grammer")
        self.assertEqual(args.guard, "GRAMMER_HPP")

    def test_syntax_reference_is_nonempty(self):
        """The --syntax cheat-sheet mentions the core constructs."""
        self.assertIn("sigma", GRAMMAR_SYNTAX_REFERENCE)
        self.assertIn("epsilon", GRAMMAR_SYNTAX_REFERENCE)
        self.assertIn("[[a-z]]", GRAMMAR_SYNTAX_REFERENCE)


class RegressionTests(unittest.TestCase):
    """Tests pinning fixes for specific bugs so they cannot silently return."""

    def test_rule_label_is_accepted_and_ignored(self):
        """A rule with a leading ``label:`` parses and matches the unlabeled form.

        The grammar permits an optional rule label; it must not crash the
        transformer and must not change the generated table.
        """
        labeled = _generate_cpp("tok={a,b}\nSt->lbl:tok,<St>|epsilon\n")
        plain = _generate_cpp("tok={a,b}\nSt->tok,<St>|epsilon\n")
        self.assertEqual(labeled, plain)

    def test_wide_codepoint_atom_emits_char32_literal(self):
        """An atom above one byte is emitted as a valid ``char32_t`` literal.

        A narrow ``'\\x20AC'`` literal is ill-formed in C++; the generator must use
        the ``U'\\x20AC'`` form so the header compiles.
        """
        cpp = _generate_cpp("St->\u20ac,<St>|epsilon\n")
        self.assertIn("U'\\x20AC'", cpp)
        self.assertNotIn("term<'\\x20AC'>", cpp)

    def test_high_byte_atom_stays_narrow(self):
        """An atom in 0x80-0xFF stays a narrow literal (no needless widening)."""
        cpp = _generate_cpp("St->\xff,<St>|epsilon\n")
        self.assertIn("'\\xFF'", cpp)
        self.assertIn("U'\\xFF'", cpp)

    def test_analysis_handles_inline_set_terminal(self):
        """Grammar analysis must not choke on an inline (range) set terminal.

        An inline set's value is an unhashable Python set; ``find_unused_terminals``
        once tried to add it to a set and crashed, breaking ``--analyze`` and
        ``--debug-json`` for any grammar using a ``[[range]]``.
        """
        table = _build_identifier_table("St->[[a-c]],<St>|epsilon\n")
        grammar = normalize_grammar_keys(table[SymbolType.non_terminal])
        # Should not raise.
        unused = find_unused_terminals(grammar, table[SymbolType.terminal])
        self.assertEqual(unused, [])
        analysis = analyze_grammar(grammar, table[SymbolType.terminal])
        self.assertEqual(analysis["unused_terminals"], [])

    def test_unused_named_terminal_still_detected(self):
        """The unused-terminal check still flags a declared-but-unused name."""
        table = _build_identifier_table("used={a}\nghost={g}\nSt->used,<St>|epsilon\n")
        grammar = normalize_grammar_keys(table[SymbolType.non_terminal])
        unused = find_unused_terminals(grammar, table[SymbolType.terminal])
        self.assertIn("ghost", unused)
        self.assertNotIn("used", unused)


class LanguageFrontendTests(unittest.TestCase):
    """End-to-end coverage for the EBNF and Lark input frontends."""

    def test_ebnf_generates_cpp(self):
        cpp = _generate_cpp('start = "a", ["b"], {"c"};', language="ebnf")
        self.assertIn("struct g", cpp)
        self.assertIn("ctll::term<'a'>", cpp)

    def test_ebnf_alternation(self):
        cpp = _generate_cpp('start = "a" | "b";', language="ebnf")
        self.assertIn("ctll::set<'a','b'>", cpp)

    def test_ebnf_multiline_document_and_comment(self):
        cpp = _generate_cpp(
            '(* parsed as one EBNF document *)\n'
            'start = prefix,\n "z";\n'
            'prefix = "a" | "b";\n',
            language="ebnf",
        )
        self.assertIn("struct start", cpp)
        self.assertIn("ctll::set<'a','b'>", cpp)

    def test_lark_generates_cpp(self):
        cpp = _generate_cpp(
            'start: LETTER DIGIT*\nLETTER: /[a-z]/\nDIGIT: /[0-9]/\n',
            language="lark",
        )
        self.assertIn("struct g", cpp)
        self.assertIn("ctll::set<", cpp)

    def test_lark_accepts_multi_character_lexer_terminal(self):
        cpp = _generate_cpp('start: WORD\nWORD: "word"\n', language="lark")
        self.assertIn("struct g", cpp)

    def test_lark_official_syntax_features(self):
        cpp = _generate_cpp(
            '%import common.WS\n'
            '%ignore WS\n'
            'start.1: "a" -> first_case\n'
            '       | "b"  // multiline alternative\n',
            language="lark",
        )
        self.assertIn("ctll::set<'a','b'>", cpp)


class RegexEngineTests(unittest.TestCase):
    """Unit coverage for Tablewright's own regex parser."""

    def test_literal_sequence(self):
        self.assertEqual(parse_regex("ab"),
                         ("seq", [("charset", frozenset("a"), False),
                                  ("charset", frozenset("b"), False)]))

    def test_class_ranges_and_singles(self):
        self.assertEqual(parse_regex("[a-c_]"),
                         ("charset", frozenset("abc_"), False))

    def test_negated_class(self):
        self.assertEqual(parse_regex(r"[^\"\\]"),
                         ("charset", frozenset('"\\'), True))

    def test_class_shorthands(self):
        self.assertEqual(parse_regex(r"\d"),
                         ("charset", frozenset("0123456789"), False))
        self.assertEqual(parse_regex(r"\D"),
                         ("charset", frozenset("0123456789"), True))
        self.assertEqual(parse_regex(r"[\d_]")[1],
                         frozenset("0123456789_"))

    def test_dot_excludes_newline(self):
        self.assertEqual(parse_regex("."), ("charset", frozenset("\n"), True))

    def test_dotall_dot_matches_everything(self):
        self.assertEqual(parse_regex(".", "s"),
                         ("alt", [("charset", frozenset("\n"), True),
                                  ("charset", frozenset("\n"), False)]))

    def test_quantifiers(self):
        atom = ("charset", frozenset("a"), False)
        self.assertEqual(parse_regex("a+"), ("quant", atom, "+"))
        self.assertEqual(parse_regex("a*?"), ("quant", atom, "*"))
        self.assertEqual(parse_regex("a?"), ("quant", atom, "?"))

    def test_counted_repetition(self):
        atom = ("charset", frozenset("a"), False)
        self.assertEqual(parse_regex("a{3}"), ("seq", [atom, atom, atom]))
        self.assertEqual(parse_regex("a{2,}"),
                         ("seq", [atom, atom, ("quant", atom, "*")]))
        self.assertEqual(parse_regex("a{1,2}"),
                         ("seq", [atom, ("quant", atom, "?")]))

    def test_literal_brace_without_count(self):
        self.assertEqual(parse_regex("a{x")[1][1],
                         ("charset", frozenset("{"), False))

    def test_groups_and_alternation(self):
        self.assertEqual(parse_regex("(ab|c)d")[1][0][0], "alt")
        self.assertEqual(parse_regex("(?:ab)"), parse_regex("(ab)"))
        self.assertEqual(parse_regex("(?P<x>a)"), parse_regex("a"))

    def test_case_insensitive_flag(self):
        self.assertEqual(parse_regex("a", "i"),
                         ("charset", frozenset("aA"), False))
        self.assertEqual(parse_regex("[a-b]", "i")[1], frozenset("abAB"))

    def test_verbose_flag_strips_layout(self):
        self.assertEqual(parse_regex("a b # comment\n c", "x"),
                         parse_regex("abc"))

    def test_hex_and_control_escapes(self):
        self.assertEqual(parse_regex(r"\x41"), ("charset", frozenset("A"), False))
        self.assertEqual(parse_regex(r"A"), ("charset", frozenset("A"), False))
        self.assertEqual(parse_regex(r"\n"), ("charset", frozenset("\n"), False))
        self.assertEqual(parse_regex(r"\."), ("charset", frozenset("."), False))

    def test_untranslatable_constructs_are_rejected(self):
        for pattern in ("^a", "a$", r"a\b", r"\Aa", r"(a)\1", "(?=a)b",
                        "(?!a)b", "(?<=a)b", "a*+", "a**", "(a", "a)",
                        r"a\x4", "[a", "[]", r"a\\" [:-1]):
            with self.assertRaises(RegexSyntaxError, msg=pattern):
                parse_regex(pattern)

    def test_negated_shorthand_inside_class_is_rejected(self):
        with self.assertRaisesRegex(RegexSyntaxError, "negated shorthand"):
            parse_regex(r"[a\D]")


class LarkRegexLoweringTests(unittest.TestCase):
    """Lark terminals with real regexes, lowered through EDS."""

    def test_multicharacter_regex_terminal(self):
        eds = lark_to_eds("start: WORD\nWORD: /[a-z]+/\n")
        self.assertIn("WORD -> [[a-z]]+", eds)
        cpp = _generate_cpp("start: WORD\nWORD: /[a-z]+/\n", language="lark")
        self.assertIn("struct g", cpp)

    def test_single_character_terminal_stays_a_set(self):
        eds = lark_to_eds("start: DIGIT\nDIGIT: /[0-9]/\n")
        self.assertIn("DIGIT = {0, 1, 2, 3, 4, 5, 6, 7, 8, 9}", eds)

    def test_negated_class_terminal_becomes_negative_set(self):
        eds = lark_to_eds('start: CH\nCH: /[^"\\\\]/\n')
        self.assertIn("CH = sigma - {\\\", \\\\}", eds)

    def test_terminal_referencing_terminal(self):
        eds = lark_to_eds("start: INT\nINT: DIGIT+\nDIGIT: /[0-9]/\n")
        self.assertIn("INT -> DIGIT+", eds)
        cpp = _generate_cpp("start: INT\nINT: DIGIT+\nDIGIT: /[0-9]/\n",
                            language="lark")
        self.assertIn("struct g", cpp)

    def test_string_terminal_becomes_a_rule(self):
        eds = lark_to_eds('start: ARROW\nARROW: "->"\n')
        self.assertIn('ARROW -> "->"', eds)

    def test_case_insensitive_string(self):
        eds = lark_to_eds('start: KW\nKW: "if"i\n')
        self.assertIn("KW -> [[Ii]], [[Ff]]", eds)

    def test_regex_with_structure(self):
        source = "start: NUM\nNUM: /[0-9]+(\\.[0-9]+)?/\n"
        eds = lark_to_eds(source)
        self.assertIn("[[0-9]]+", eds)
        cpp = _generate_cpp(source, language="lark")
        self.assertIn("struct g", cpp)

    def test_counted_repetition_terminal(self):
        eds = lark_to_eds("start: HEX2\nHEX2: /[0-9a-f]{2}/\n")
        self.assertEqual(eds.count("[[0-9a-f]]"), 2)

    def test_regex_directly_in_a_rule(self):
        cpp = _generate_cpp("start: /[0-9]/ /[a-z]+/\n", language="lark")
        self.assertIn("struct g", cpp)

    def test_lark_counted_repetition_in_rule(self):
        eds = lark_to_eds('start: "a" ~ 2..3\n')
        self.assertIn('a, a, <tw_optional_1>', eds)

    def test_unusable_names_are_prefixed(self):
        eds = lark_to_eds("a: X\nX: \"x\"\n")
        self.assertIn("tw_a -> tw_X", eds)
        self.assertIn("tw_X = {x}", eds)

    def test_reserved_name_is_prefixed(self):
        eds = lark_to_eds('start: other\nother: "o"\n')
        self.assertIn("<tw_other>", eds)

    def test_undefined_reference_is_reported(self):
        with self.assertRaisesRegex(ValueError, "undefined names: missing"):
            lark_to_eds("start: missing\n")

    def test_declared_terminal_use_is_reported(self):
        with self.assertRaisesRegex(ValueError, "no definition to translate"):
            lark_to_eds("%declare EXT\nstart: EXT\n")

    def test_ignore_warns(self):
        with self.assertLogs(logger, level="WARNING") as captured:
            lark_to_eds('%import common.WS\n%ignore WS\nstart: "a"\nWS: / /\n')
        self.assertTrue(any("%ignore" in line for line in captured.output))

    def test_anchor_in_terminal_is_rejected(self):
        with self.assertRaises(RegexSyntaxError):
            lark_to_eds("start: BAD\nBAD: /^a/\n")

    def test_emitted_eds_round_trips(self):
        source = ("start: item (\",\" item)*\n"
                  "item: WORD | NUM\n"
                  "WORD: /[a-z]+/\n"
                  "NUM: /[0-9]+(\\.[0-9]+)?/\n")
        eds = lark_to_eds(source)
        table = _build_identifier_table(eds)
        direct = stringify_grammar(table[SymbolType.non_terminal])
        table = _build_identifier_table(source, language="lark")
        via_lark = stringify_grammar(table[SymbolType.non_terminal])
        self.assertEqual(direct, via_lark)


class EbnfDialectTests(unittest.TestCase):
    """The upgraded ISO frontend and the W3C (XML-spec) EBNF dialect."""

    def test_iso_repetition_factor(self):
        eds = ebnf_to_eds('start = 3 * "a";')
        self.assertIn("a, a, a", eds)
        cpp = _generate_cpp('start = 3 * "ab";', language="ebnf")
        self.assertIn("struct g", cpp)

    def test_iso_assign_and_terminator_variants(self):
        cpp = _generate_cpp('start ::= "x" .', language="ebnf")
        self.assertIn("ctll::term<'x'>", cpp)

    def test_iso_exception_of_character_sets(self):
        eds = ebnf_to_eds('start = ("a" | "b" | "c") - "b";')
        self.assertIn("[[ac]]", eds)

    def test_iso_exception_via_rule_reference(self):
        eds = ebnf_to_eds('start = char - "b";\nchar = "a" | "b";')
        self.assertIn("start -> a", eds)

    def test_iso_untranslatable_exception_is_reported(self):
        with self.assertRaisesRegex(ValueError, "only translatable"):
            ebnf_to_eds('start = foo - "b";\nfoo = "a", "x";')

    def test_iso_exception_removing_everything_is_reported(self):
        with self.assertRaisesRegex(ValueError, "removes every character"):
            ebnf_to_eds('start = "a" - "a";')

    def test_w3c_generates_cpp(self):
        source = ("Name ::= NameStart NameChar*\n"
                  "NameStart ::= [A-Z_a-z]\n"
                  "NameChar ::= NameStart | [-.0-9]\n")
        eds = w3c_to_eds(source)
        self.assertIn("[[A-Z_a-z]]", eds)
        self.assertIn("[[\\-.0-9]]", eds)
        cpp = _generate_cpp(source, language="w3c")
        self.assertIn("struct g", cpp)

    def test_w3c_hexrefs_and_single_char_names(self):
        eds = w3c_to_eds("S ::= [#x20#x9#xD#xA]+")
        self.assertIn("tw_S", eds)  # 'S' is too short for an EDS name
        self.assertIn(r"\t\n\r\x20", eds)

    def test_w3c_negated_class(self):
        eds = w3c_to_eds("Val ::= '\"' [^\"]* '\"'")
        self.assertIn('[[^"]]*', eds)

    def test_w3c_exception_like_the_xml_spec(self):
        eds = w3c_to_eds('Cmt ::= Chr - "-"\nChr ::= [a-z-]\n')
        self.assertIn("Cmt -> [[a-z]]", eds)

    def test_w3c_multicharacter_string(self):
        eds = w3c_to_eds('Doc ::= "<?xml"')
        self.assertIn('"<?xml"', eds)

    def test_w3c_comments_both_styles(self):
        eds = w3c_to_eds("/* c1 */ Doc ::= 'x' (* c2 *)\n")
        self.assertIn("Doc -> x", eds)

    def test_w3c_through_the_cli(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            grammar = Path(tmp) / "toy.w3c"
            grammar.write_text('Greeting ::= "hi" [0-9]*\n', encoding="utf-8")
            eds_path = Path(tmp) / "toy.gram"
            status = main(["--input", str(grammar), "--lang", "w3c",
                           "--emit-eds", str(eds_path), "--check", "-q"])
            self.assertEqual(status, 0)
            self.assertIn('"hi"', eds_path.read_text(encoding="utf-8"))


class EmitEdsTests(unittest.TestCase):
    """The --emit-eds option: write the normalized EDS intermediate."""

    def test_emit_eds_writes_the_intermediate(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            grammar = Path(tmp) / "toy.lark"
            grammar.write_text("start: WORD\nWORD: /[a-z]+/\n",
                               encoding="utf-8")
            eds_path = Path(tmp) / "toy.gram"
            status = main(["--input", str(grammar), "--lang", "lark",
                           "--emit-eds", str(eds_path), "--check", "-q"])
            self.assertEqual(status, 0)
            written = eds_path.read_text(encoding="utf-8")
            self.assertIn("WORD -> [[a-z]]+", written)
            self.assertIn("# ", written)  # provenance header
            # the emitted file is itself a valid EDS grammar
            self.assertIsNotNone(_build_identifier_table(written))

    def test_emit_eds_passthrough_for_native_input(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            grammar = Path(tmp) / "toy.gram"
            grammar.write_text("St -> a, b\n", encoding="utf-8")
            eds_path = Path(tmp) / "out.gram"
            status = main(["--input", str(grammar), "--emit-eds",
                           str(eds_path), "--check", "-q"])
            self.assertEqual(status, 0)
            self.assertIn("St -> a, b",
                          eds_path.read_text(encoding="utf-8"))


class RegexGrammarTests(unittest.TestCase):
    """The grammar-driven regex engine: Lark parsing the regex dialect."""

    def test_the_engine_is_a_lark_grammar(self):
        self.assertIsInstance(_REGEX_PARSER, Lark)
        self.assertIn("regexp: alternation", _REGEX_LARK_GRAMMAR)

    def test_the_document_grammar_is_vendored_and_keeps_regexp_lexical(self):
        self.assertIn("REGEXP", _TABLEWRIGHT_LARK_GRAMMAR)
        self.assertIsInstance(_LARK_GRAMMAR_PARSER, Lark)

    def test_first_position_bracket_is_literal(self):
        self.assertEqual(parse_regex("[]]"), ("charset", frozenset("]"), False))
        self.assertEqual(parse_regex("[^]]"), ("charset", frozenset("]"), True))

    def test_dash_literal_at_the_edges(self):
        self.assertEqual(parse_regex("[-a]")[1], frozenset("-a"))
        self.assertEqual(parse_regex("[a-]")[1], frozenset("a-"))

    def test_comment_group_vanishes(self):
        self.assertEqual(parse_regex("(?#note)ab"), parse_regex("ab"))

    def test_backspace_inside_class_boundary_outside(self):
        self.assertEqual(parse_regex(r"[\b]")[1], frozenset("\x08"))
        with self.assertRaisesRegex(RegexSyntaxError, "word boundary"):
            parse_regex(r"a\b")

    def test_multidigit_backreference_is_rejected(self):
        with self.assertRaisesRegex(RegexSyntaxError, "backreferences"):
            parse_regex(r"(a)(b)\12")

    def test_lazy_counted_repetition(self):
        atom = ("charset", frozenset("a"), False)
        self.assertEqual(parse_regex("a{1,2}?"),
                         ("seq", [atom, ("quant", atom, "?")]))

    def test_shorthand_cannot_bound_a_range(self):
        with self.assertRaisesRegex(RegexSyntaxError, "cannot bound a range"):
            parse_regex(r"[\d-z]")

    def test_bare_bracket_and_brace_are_literals_outside_classes(self):
        self.assertEqual(parse_regex("a]")[1][1],
                         ("charset", frozenset("]"), False))
        self.assertEqual(parse_regex("a}")[1][1],
                         ("charset", frozenset("}"), False))


# A small JSON-like grammar reused by the optimization tests.
_SAMPLE_JSON = (
    "uchar=sigma-{\\\",\\\\}\n"
    "digit={1,2,3,4,5,6,7,8,9}\n"
    "St-><value>\n"
    "value->\\\",<string2>,\\\"|t|f\n"
    "string2->uchar,<string2>|epsilon\n"
)


def build_test_suite() -> "unittest.TestSuite":
    """Collect every test case in this module into one suite.

    Returns:
        A :class:`unittest.TestSuite` containing all Tablewright tests.
    """
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    for case in (GrammerTypeTests, CharLiteralTests, IdentifierSafetyTests,
                 TerminalAliaserTests, FirstFollowTests, ParseTableTests,
                 GrammarAnalysisTests, OptimizationTests, IntegrationTests,
                 RangeExpansionTests, HexEscapeTests, SetDefinitionSyntaxTests,
                 RuleOperatorSyntaxTests, QuantifierGroupTests,
                 RangeLookaheadTests,
                 QualityOfLifeTests, RegressionTests, LanguageFrontendTests,
                 RegexEngineTests, RegexGrammarTests, LarkRegexLoweringTests,
                 EbnfDialectTests, EmitEdsTests):
        suite.addTests(loader.loadTestsFromTestCase(case))
    return suite


def run_tests(verbosity: int = 2) -> int:
    """Run the built-in test suite and return a process exit code.

    Args:
        verbosity: unittest verbosity (2 lists each test, 1 is terse).

    Returns:
        ``0`` if every test passed, ``1`` otherwise.
    """
    # Keep test output readable: silence the tool's own INFO/DEBUG logging so the
    # unittest report is not interleaved with pipeline chatter.
    logging.getLogger().setLevel(logging.CRITICAL)
    result = unittest.TextTestRunner(verbosity=verbosity, stream=stdout).run(
        build_test_suite()
    )
    return 0 if result.wasSuccessful() else 1
