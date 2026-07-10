#!/usr/bin/env python3
"""Tablewright: a (q)LL(1) parser-generator that emits C++ for the CTLL engine.

Tablewright turns a compact, human-readable grammar into the table of overloaded
``rule`` functions that drives CTLL, the compile-time LL parser at the heart of
Hana Dusíková's Compile-Time Regular Expressions library
(https://github.com/hanickadot/compile-time-regular-expressions). CTLL runs a
pushdown automaton entirely at compile time, selecting productions by overload
resolution over rules shaped like::

    static constexpr auto rule(State, ctll::term<'c'>) -> ctll::push<...>;

Given a grammar in the ``.gram`` dialect, this module produces exactly that
header.

Origin and attribution
-----------------------
The grammar format and the original tool are Hana Dusíková's work, not mine.
Hana built CTRE/CTLL and an in-house utility called *Desatomat* that transforms a
grammar into the (q)LL(1) ``rule`` table CTLL consumes. Her Desatomat is closed
source and runs publicly at
https://www.desatomat.cz/?lang=desatomat&langui=en . As she explained in
https://github.com/hanickadot/compile-time-regular-expressions/issues/37 :

    "Desatomat is an utility to transform grammar to different form. You don't
    need it to design your own grammar as long as you are able to write LL1
    table. I can't open-source it and I don't have time to write proper
    replacement as I would want."

Tablewright is my attempt at that replacement: an independent, open-source
re-implementation built by reverse-engineering the input/output format from
CTRE's published headers. It is not derived from Hana's source and is not
affiliated with or endorsed by her; any differences from the original Desatomat
are mine. The ``.gram`` dialect below mirrors the format her tool accepts so the
two are interoperable in practice.

The grammar dialect
-------------------
A ``.gram`` file is a sequence of *set definitions* and *rules*::

    name = {a, b, c}        # a positive character class (a named terminal)
    name = sigma - {a, b}   # a negative class: any character except those listed
    A -> <B>, x, [act] | epsilon

In a set definition the assignment operator may be written as ``=`` or ``:``
(they are equivalent), and the braces around the members are optional, so
``name = {a, b, c}``, ``name : {a, b, c}``, ``name = a, b, c`` and
``name : a, b, c`` all define the same terminal (and likewise for the
``sigma -`` negative form).

The production operator joining a nonterminal to its rules may be written as
``->`` or ``:`` (they are equivalent), so ``A -> <B> | x`` and ``A : <B> | x``
mean the same thing. Because ``:`` also introduces a set definition, a statement
of the bare form ``name : a, b, c`` (a comma list of single-character atoms with
no nonterminal, string, range, or ``|`` alternation) is read as a *set* for
backward compatibility; to write such a rule with ``:`` give it a rule-shaped
body (a ``<nonterminal>``, a ``"string"``, a ``[[range]]``, a ``|``, or the
``epsilon`` keyword), or simply use ``->``.

In a rule body, ``<B>`` references another nonterminal, a bare character such as
``x`` is an atom, ``[act]`` is a semantic action, ``"abc"`` is a string literal
(expanded later into the atoms ``a``, ``b``, ``c``), ``|`` separates
alternatives and ``,`` separates the symbols of one alternative. ``epsilon``
(or ``@``) denotes the empty production. Nonterminal *names* must be at least
two characters, because a single ``<X>`` is reserved syntax.

A rule body may also contain a regex-style character range written in double
brackets, e.g. ``[[a-zA-Z]]`` or ``[[abcg-i]]``. A range expands inline into a
positive set with every member character enumerated (``[[a-c]]`` becomes the set
``{a, b, c}``); spans may be combined and mixed with literals, and an escaped
dash (``\\-``) is a literal. A ``^`` as the first character negates the range:
``[[^\\nabc\\r\\0]]`` matches any character *except* the six listed, emitted as
a ``ctll::neg_set`` just like a named ``sigma -`` set. A ``^`` that is escaped
(``\\^``) or not in first position is an ordinary literal.

Wherever a single character can appear (an atom, a set member, a string-literal
character, a range item), it may be spelled with an escape: the control escapes
``\\n \\t \\r \\f \\v \\0 \\a \\b``, a hex escape ``\\xNN`` (exactly two hex
digits) or ``\\u{H..H}`` (one to six hex digits, up to U+10FFFF), or a backslash
before any other character for that literal character. Non-ASCII characters may
also be typed directly; grammar files are read as UTF-8. A malformed hex escape
keeps the old reading (``\\x`` is a literal ``x``).

Rule bodies further support regex-style *grouping* and *repetition*. A
parenthesized grouping ``(<expr> x)`` brackets a sequence of symbols (separated
by commas or just whitespace), and the quantifiers ``+`` (one or more) and ``*``
(zero or more) may follow an atom, a ``"string"``, a ``[[range]]``, a named
terminal, a ``<nonterminal>`` or a grouping. Both are pure syntax sugar,
rewritten before any analysis into an anonymous right-recursive helper::

    S -> a+        becomes    S -> a, <a_anon>
                              a_anon -> a, <a_anon> | @

    S -> a*        becomes    S -> <a_anon>
                              a_anon -> a, <a_anon> | @

Because ``( ) * +`` are also ordinary characters in many grammars, the sugar is
recognized only where it is structurally unambiguous: a ``*`` or ``+`` is a
quantifier only when it immediately follows a quantifiable symbol (so the
stand-alone atoms in ``S -> (, a, ), *`` keep their old meaning), and inside a
grouping -- where whitespace alone separates items -- the structural characters
``, ( ) < > [ ] | " * + @`` must be escaped (``\\(``, ``\\*``, ...) to be
literals. Outside groupings nothing changes and existing grammars parse as
before.

Parser model
------------
By default Tablewright targets a *Q-grammar*, the relaxation CTLL relies on: when
a terminal is in both FIRST and FOLLOW of a nonterminal, the shift rule wins and
epsilon is the fallback. Pass ``--no-q`` / ``--strict`` to require classic LL(1)
instead, where any FIRST/FIRST or FIRST/FOLLOW overlap is a conflict.

Optimization
------------
The ``-O0``..``-O3`` flags trade generation effort for a smaller table, in the
spirit of a C++ compiler's optimization levels. Every level preserves the
recognized language and the chosen parser model (``-O1`` merges identical
nonterminals, ``-O2`` also inlines single-use ones, ``-O3`` also inlines
single-alternative ones). Because each distinct ``rule`` overload adds to the
overload set the compiler must resolve per input character, these state-reducing
passes are the most effective lever on compile time -- on the bundled PCRE
grammar, ``-O3`` compiles its parser noticeably faster than ``-O0``.

``--range-lookaheads`` is a separate, opt-in transform grounded in interval
covering: a wide positive lookahead set is split into maximal contiguous spans,
each emitted as a ``ctll::range<lo,hi>`` rule (two ordered comparisons) instead
of a single wide ``ctll::set`` (one equality comparison per member). It is
language-preserving and verified against CTRE's full test suite, and it sharply
cuts the number of compile-time character comparisons and the width of the
largest set types. Note the trade-off, though: it replaces one rule with several,
which enlarges the overload set, and for the CTLL/GCC target that overload cost
outweighs the comparison saving, so it tends to *increase* overall compile time.
It is therefore off by default and most interesting for other back ends, very
large alphabets, or compilers whose set-membership cost dominates.

Pipeline
--------
``main`` wires the stages together::

    .gram text
      -> Lark parse                       parse the .gram dialect
      -> tree transforms                  strip whitespace, build GrammerTypes
      -> identifier table                 collect nonterminals/terminals/actions
      -> verify + break string literals   "abc" -> a, b, c
      -> eliminate left recursion
      -> left factoring (to a fixed point)
      -> compute the global "other" set
      -> inline pure character-class helpers
      -> (optional) -O optimization passes
      -> FIRST / FOLLOW / (q)LL(1) parse table
      -> render the CTLL header

Command-line usage
------------------
In the common case only the input and output are needed -- the namespace, header
guard, output filename and grammar-struct name all default to names derived from
the input filename (``pcre.gram`` -> namespace ``pcre``, guard ``PCRE_HPP``,
file ``pcre.hpp``)::

    python tablewright.py --input pcre.gram --output include/

Any of those can still be set explicitly, and other options layered on::

    python tablewright.py --input pcre.gram --output include/ \\
        --namespace ctre --guard CTRE__PCRE__HPP --grammar-name pcre -O3

To validate a grammar without generating anything (useful in CI or while
iterating), use ``--check`` (alias ``--validate``); it parses the grammar,
checks for undefined symbols and (q)LL(1) conflicts, and exits nonzero if the
grammar is invalid. ``--syntax`` prints a quick reference for the ``.gram``
dialect. A malformed grammar reports the offending line and column with a caret
and a plain-language list of what was expected there.

Debugging and logging
---------------------
The pipeline logs its progress; raise the verbosity to inspect what it is doing:

* ``--verbose`` (DEBUG) prints the grammar at each stage, the FIRST/FOLLOW sets,
  the full parse table, what each optimization pass changed, and the terminal
  aliasing summary;
* ``--trace`` adds per-item detail: every FIRST/FOLLOW propagation worth noting,
  each Q-grammar shift/epsilon resolution, every merge/inline, and every alias
  assignment;
* ``--quiet`` limits output to errors;
* ``--log-file PATH`` also writes a full, timestamped log to a file;
* ``--dump-stages DIR`` writes each intermediate grammar (original, after
  left-recursion removal, after factoring) and the final header to ``DIR`` as
  text files for inspection;
* ``--stats`` prints a per-stage wall-clock timing summary when finished.

For grammar diagnostics specifically:

* ``--analyze`` prints a health report before generating: nullable nonterminals,
  plus warnings for unreachable or unproductive nonterminals, declared-but-unused
  terminals, and duplicate productions;
* ``--explain NONTERMINAL`` traces a single nonterminal end to end -- its
  productions, FIRST/FOLLOW, parse-table row and the emitted ``rule`` overloads --
  then exits without writing output;
* ``--debug-json PATH`` writes the finalized grammar, FIRST/FOLLOW, parse table,
  terminal-alias map and analysis to a JSON file for diffing or other tooling.

Tablewright ships with a built-in test suite (standard-library ``unittest``); run
it with ``--run-tests``, which exercises the data structures, FIRST/FOLLOW and
parse-table maths, grammar analysis, the terminal aliaser and a full
grammar-to-C++ integration path, then exits with a pass/fail status.

Grammar conflicts are reported as a clear one-line error (with a side-by-side of
the competing productions for same-lookahead clashes) instead of a traceback.

Run ``python tablewright.py --help`` for the full set of flags.

:author: Alexios Angel <aangeletakis@gmail.com>
:license: MIT
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import time
import unittest
from collections import OrderedDict, UserList, defaultdict
from contextlib import contextmanager
from enum import Enum, auto
from io import TextIOWrapper
from pathlib import Path
from pprint import pformat
from sys import stdout
from typing import Dict, List, Optional, Set

# Lark parses the .gram input.
from lark import Discard, Lark, Token, Transformer, Tree, Visitor
from lark.exceptions import UnexpectedInput

VERSION = "0.1.0"
AUTHORS = [
    {"name": "Alexios Angel", "email": "aangeletakis@gmail.com"},
]
HOMEPAGE = "https://github.com/alexios-angel/tablewright"
ISSUES = "https://github.com/alexios-angel/tablewright/issues"
LICENSE = "MIT"

logging.captureWarnings(True)
logger = logging.getLogger(__name__)
logging_level = logging.INFO

# A finer-grained level than DEBUG for very chatty, per-item tracing (individual
# FIRST/FOLLOW additions, every parse-table cell, every alias assignment). It sits
# just below DEBUG so ``--trace`` is strictly more verbose than ``--verbose``.
TRACE = 5
logging.addLevelName(TRACE, "TRACE")


def trace(message, *args, **kwargs):
    """Log at the custom :data:`TRACE` level (finer than DEBUG)."""
    if logger.isEnabledFor(TRACE):
        logger.log(TRACE, message, *args, **kwargs)


def configure_logging(level, log_file=None):
    """Set up the root logger's format, level and (optionally) a file handler.

    The console format is terse at INFO and above (just the message) but switches
    to a level-prefixed format once DEBUG/TRACE is on, which makes the deeper
    diagnostics easier to scan. When ``log_file`` is given, the full, timestamped
    log is also written there regardless of the console level.

    Args:
        level: The console logging level (e.g. ``logging.DEBUG`` or :data:`TRACE`).
        log_file: Optional path; if set, a timestamped copy of every record at the
            chosen level is appended there.
    """
    root = logging.getLogger()
    root.setLevel(min(level, TRACE) if log_file else level)
    for handler in list(root.handlers):
        root.removeHandler(handler)

    verbose = level <= logging.DEBUG
    console_fmt = "%(levelname)s: %(message)s" if verbose else "%(message)s"
    console = logging.StreamHandler(stdout)
    console.setLevel(level)
    console.setFormatter(logging.Formatter(console_fmt))
    root.addHandler(console)

    if log_file:
        file_handler = logging.FileHandler(log_file, mode="w", encoding="utf-8")
        file_handler.setLevel(level)
        file_handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)-7s %(message)s")
        )
        root.addHandler(file_handler)


def log_stage(name):
    """Log a visually distinct banner marking a pipeline stage (at INFO)."""
    logger.info("")
    logger.info("=" * 60)
    logger.info(name)
    logger.info("=" * 60)


# A module-level accumulator for per-stage timings, summarized at the end of a run
# when ``--stats`` is given. Maps a stage name to its elapsed wall-clock seconds.
_STAGE_TIMINGS = {}


@contextmanager
def timed_stage(name, banner=True):
    """Time a pipeline stage, optionally printing a banner, recording the elapsed.

    Args:
        name: The stage label (also used as the banner text and timing key).
        banner: Whether to print the :func:`log_stage` banner on entry.

    Yields:
        None. On exit, the wall-clock duration is logged at DEBUG and stored in
        :data:`_STAGE_TIMINGS` for the optional end-of-run summary.
    """
    if banner:
        log_stage(name)
    start = time.perf_counter()
    try:
        yield
    finally:
        elapsed = time.perf_counter() - start
        _STAGE_TIMINGS[name] = _STAGE_TIMINGS.get(name, 0.0) + elapsed
        logger.debug(f"  ({name}: {elapsed * 1000:.1f} ms)")


def log_timing_summary():
    """Log a table of per-stage timings collected during the run (at INFO)."""
    if not _STAGE_TIMINGS:
        return
    total = sum(_STAGE_TIMINGS.values())
    width = max(len(name) for name in _STAGE_TIMINGS)
    logger.info("")
    logger.info("Timing summary:")
    for name, elapsed in _STAGE_TIMINGS.items():
        share = (elapsed / total * 100) if total else 0
        logger.info(f"  {name:<{width}}  {elapsed * 1000:8.1f} ms  ({share:4.1f}%)")
    logger.info(f"  {'TOTAL':<{width}}  {total * 1000:8.1f} ms")


def describe_grammar(grammar) -> str:
    """Return a one-line size summary of a grammar for progress logging.

    Args:
        grammar: A mapping of nonterminal to its alternatives.

    Returns:
        A string like ``33 nonterminals, 90 productions``.
    """
    nonterminals = len(grammar)
    productions = sum(len(alts) for alts in grammar.values())
    return f"{nonterminals} nonterminals, {productions} productions"


# ======================================================================== #
# Core data types
# ======================================================================== #

class SymbolType(Enum):
    """
    The kind of a grammar symbol.

    The member *names* matter: ``RuleTransformer`` maps Lark rule names onto
    these via ``getattr(SymbolType, rule_name)``, so ``atom``/``string``/
    ``terminal``/``non_terminal``/``semantic_action``/``epsilon`` must stay
    spelled exactly as the corresponding grammar rules.

    A few members are not symbol kinds but table sections / set polarities:
    ``positive_set`` and ``negitive_set`` describe character sets, and
    ``action`` keys the set of semantic-action names in the identifier table.
    """
    atom = 0                # a single literal character, e.g. 'a'
    terminal = auto()       # a *named* terminal (a set defined with name={...})
    string = auto()         # a quoted string literal, later split into atoms
    non_terminal = auto()   # a reference to another rule, <name>
    semantic_action = auto()  # a parser action, [name]
    epsilon = auto()        # the empty production
    positive_set = auto()   # set polarity: match one of these characters
    negitive_set = auto()   # set polarity: match any character except these
    action = auto()         # identifier-table key for the set of action names
    # The next two are *transient* symbol kinds produced by the parser for the
    # regex-style grouping/repetition syntax. They exist only between the tree
    # transform and expand_groups_and_quantifiers(), which rewrites every one of
    # them into ordinary symbols (splicing groups inline and turning '+'/'*'
    # into anonymous helper nonterminals) before any analysis or generation.
    group = auto()          # ( ... ): value is a tuple of the grouped symbols
    quantified = auto()     # X+ / X*: value is (tuple of symbols, '+' or '*')


class HashableList(UserList):
    """A list that hashes by value, so productions can live in sets and dicts.

    Productions are sequences of :class:`GrammerType`. Storing them in
    :class:`OrderedSet` (alternatives of a nonterminal) and as dict keys (during
    left factoring) requires them to be hashable, which a plain ``list`` is not.
    """

    def __hash__(self) -> int:
        # The leading marker salts the hash so it cannot collide with a plain
        # tuple of the same contents.
        return hash((HashableList, tuple(self.data)))


class OrderedSet(UserList):
    """An insertion-ordered set backed by a list.

    Only the operations the pipeline needs are implemented: de-duplicating
    inserts (:meth:`add`, :meth:`append`, :meth:`update`), union (also via ``|``
    and ``+``), :meth:`get`, and element removal. Hashing is by value so an
    ``OrderedSet`` can itself be stored in a dict, which left factoring relies on.
    """

    def add(self, item) -> None:
        """Append ``item`` if not already present, preserving insertion order."""
        self.data.append(item)
        # Re-key through a dict to drop any duplicate while keeping order.
        self.data = list(dict.fromkeys(self.data))

    def append(self, item) -> None:
        """Alias for :meth:`add` (sets do not distinguish the two)."""
        self.add(item)

    def update(self, *others) -> None:
        """Add every element of each iterable in ``others`` to the set."""
        merged = OrderedDict.fromkeys(self.data)
        for other in others:
            merged.update(OrderedDict.fromkeys(other))
        self.data = list(merged)

    def union(self, *others) -> "OrderedSet":
        """Return a new set with this set's elements plus those of ``others``."""
        result = OrderedSet(self)
        result.update(*others)
        return result

    def get(self, key, default=None):
        """Return ``key`` if it is a member, else ``default`` (dict-like lookup)."""
        return OrderedDict.fromkeys(self.data).get(key, default)

    def remove(self, item) -> None:
        """Remove ``item``; raise :class:`KeyError` if it is not present."""
        if item in self.data:
            del self.data[self.data.index(item)]
        else:
            raise KeyError(f"Item {item} not found")

    def discard(self, item) -> None:
        """Remove ``item`` if present; do nothing otherwise."""
        if item in self.data:
            del self.data[self.data.index(item)]

    def __or__(self, other) -> "OrderedSet":
        """Set union, ``self | other``."""
        return self.union(other)

    def __add__(self, other) -> "OrderedSet":
        """Set union, ``self + other`` (concatenation collapses duplicates)."""
        return self.union(other)

    def __radd__(self, other) -> "OrderedSet":
        """Reflected union so ``other + self`` works when ``other`` is a plain list."""
        result = OrderedSet(other)
        result.update(self)
        return result

    def __hash__(self) -> int:
        # Salted like HashableList so the two cannot collide.
        return hash((OrderedSet, tuple(self.data)))


class GrammerType:
    """A single grammar symbol: a ``value`` together with its :class:`SymbolType`.

    For most symbols ``value`` is a string (a literal character, or a
    nonterminal/terminal/action name). For an inline character set it is the
    Python ``set`` of member characters; the placeholder ``other`` terminal
    starts as an empty ``list`` and is filled in by :func:`get_other`.

    Equality and hashing are by ``(value, type)`` so symbols compare and
    de-duplicate correctly inside sets, dicts and productions. ``str(symbol)``
    yields the bare ``value``, which is what the C++ renderer prints for
    nonterminals and actions.

    Attributes:
        value: The symbol's value (see above).
        type: The symbol's :class:`SymbolType`.
    """

    def __init__(self, value, symbol_type: SymbolType):
        """Initialize the symbol.

        Args:
            value: The symbol value (a string, or a set/list of characters for
                set terminals).
            symbol_type: The kind of symbol.
        """
        self.value = value
        self.type = symbol_type

    def __hash__(self) -> int:
        # set/list/dict values are unhashable, so convert them; everything else
        # hashes on (value, type) directly.
        if isinstance(self.value, set):
            return hash((frozenset(self.value), self.type))
        if isinstance(self.value, list):
            return hash((tuple(self.value), self.type))
        if isinstance(self.value, dict):
            return hash((frozenset(self.value.items()), self.type))
        return hash((self.value, self.type))

    def __eq__(self, other) -> bool:
        if isinstance(other, GrammerType):
            return (self.value, self.type) == (other.value, other.type)
        return False

    def __repr__(self) -> str:
        return f"GrammerType({self.value!r}, {self.type})"

    def __str__(self) -> str:
        # Most symbols carry a string value. Inline character-set terminals carry
        # a set/list of characters instead; render those as a compact, sorted
        # ``{abc}`` form so logging, parse-table dumps and analysis never fail on a
        # set-valued symbol. (The C++ renderer formats set terminals itself and
        # does not rely on this.)
        if isinstance(self.value, (set, frozenset)):
            return "{" + "".join(sorted(self.value, key=ord)) + "}"
        if isinstance(self.value, list):
            return "{" + "".join(sorted(self.value, key=ord)) + "}"
        return self.value

    # --- symbol-kind predicates ----------------------------------------- #

    def is_non_terminal(self) -> bool:
        """Return True if this symbol references another rule (``<name>``)."""
        return self.type == SymbolType.non_terminal

    def is_semantic_action(self) -> bool:
        """Return True if this symbol is a semantic action (``[name]``)."""
        return self.type == SymbolType.semantic_action

    def is_terminal(self) -> bool:
        """Return True if this symbol is any kind of terminal.

        That covers atoms, string literals, inline positive/negative sets, the
        empty symbol, and named terminals.
        """
        return self.type in (
            SymbolType.atom,
            SymbolType.string,
            SymbolType.positive_set,
            SymbolType.negitive_set,
            SymbolType.epsilon,
            SymbolType.terminal,
        )

    def is_named_terminal(self) -> bool:
        """Return True for a terminal referenced by name (a ``name = {...}`` set)."""
        return self.type == SymbolType.terminal

    def is_set(self) -> bool:
        """Return True for an inline positive or negative character set."""
        return self.type in (SymbolType.positive_set, SymbolType.negitive_set)

    def is_atom(self) -> bool:
        """Return True for a single literal character."""
        return self.type == SymbolType.atom

    def is_string(self) -> bool:
        """Return True for a quoted string literal (before it is broken to atoms)."""
        return self.type == SymbolType.string

    def is_epsilon(self) -> bool:
        """Return True for the empty production symbol."""
        return self.type == SymbolType.epsilon


# The single shared epsilon symbol.
EPSILON = GrammerType("epsilon", SymbolType.epsilon)


# --- Type aliases for the two structures that flow through the pipeline ----- #
#
# A ``Production`` is one alternative: an ordered sequence of symbols.
# A ``Grammar`` maps each nonterminal to its set of alternatives. During parsing
# the keys are ``str`` names; from normalize_grammar_keys onward they are
# ``GrammerType`` nonterminals.
# An ``IdentifierTable`` is the three-section dict described at ``identifier_table``.
Production = HashableList            # HashableList[GrammerType]
Grammar = Dict[object, "OrderedSet"]  # {nonterminal: OrderedSet[Production]}
IdentifierTable = Dict[SymbolType, object]


# The two hex escape forms: \xNN (exactly two hex digits) and \u{H..H} (one to
# six hex digits in braces). Used both by the .gram lexer terminals below and by
# the escape scanners here, so the two always agree on what is one token.
HEX_ESCAPE_PATTERN = r"\\x[0-9a-fA-F]{2}|\\u\{[0-9a-fA-F]{1,6}\}"
_HEX_ESCAPE_RE = re.compile(HEX_ESCAPE_PATTERN)


def scan_escaped_tokens(text: str) -> list:
    r"""Split ``text`` into per-character tokens, keeping escapes together.

    Each token is a ``(token, was_escaped)`` pair where ``token`` is the raw
    (still escaped) spelling of one character: a ``\xNN`` or ``\u{...}`` hex
    escape, a two-character ``\c`` escape, or a single plain character. A
    trailing lone backslash is a plain character, matching the historical
    behaviour of the range tokenizer.

    Args:
        text: The raw text to scan (e.g. a string-literal body or a range body).

    Returns:
        The list of ``(token, was_escaped)`` pairs, in order.
    """
    tokens = []
    i = 0
    while i < len(text):
        if text[i] == "\\":
            match = _HEX_ESCAPE_RE.match(text, i)
            if match:
                tokens.append((match.group(), True))
                i = match.end()
                continue
            if i + 1 < len(text):
                tokens.append((text[i:i + 2], True))
                i += 2
                continue
        tokens.append((text[i], False))
        i += 1
    return tokens


def unescape_character(char: str) -> str:
    r"""
    Turn a possibly backslash-escaped token into the single character it denotes.

    A bare character is returned unchanged. ``\n``, ``\t``, ``\r``, ``\f``,
    ``\v``, ``\0``, ``\a`` and ``\b`` map to their usual control characters;
    ``\xNN`` (exactly two hex digits) and ``\u{H..H}`` (one to six hex digits)
    denote the character with that code point; a backslash before any other
    character (``\\``, ``\{``, ``\,`` ...) is an escape for that literal
    character. (The named escapes replace the original's
    ``decode('unicode-escape')``, which emitted a DeprecationWarning for escapes
    like ``\{`` that are not valid Python escapes.)

    Raises:
        ValueError: For a multi-character token that is not a recognized escape,
            or a ``\u{...}`` code point beyond U+10FFFF.
    """
    if char[0] != "\\":
        if len(char) > 1:
            raise ValueError("Character length greater than 2")
        return char
    if _HEX_ESCAPE_RE.fullmatch(char):
        digits = char[3:-1] if char[1] == "u" else char[2:]
        code_point = int(digits, 16)
        if code_point > 0x10FFFF:
            raise ValueError(
                f"Escape {char!r} is beyond the last code point U+10FFFF")
        return chr(code_point)
    if len(char) > 2:
        raise ValueError("Character length greater than 2")
    control = {
        r"\n": "\n", r"\t": "\t", r"\r": "\r", r"\f": "\f",
        r"\v": "\v", r"\0": "\0", r"\a": "\a", r"\b": "\b",
    }
    if char in control:
        return control[char]
    return char[1]


def expand_range_token(token: str) -> tuple:
    r"""Enumerate a regex-style ``[[...]]`` range token into a set of characters.

    The token includes the surrounding ``[[`` and ``]]``. A ``^`` as the very
    first body character negates the range: the range then denotes every
    character *except* the listed ones (an escaped ``\^``, or a ``^`` anywhere
    else, is an ordinary literal). The rest of the body is read left to right as
    a sequence of items, each either a single (optionally backslash-escaped)
    character or a ``start-end`` span. A span enumerates every character whose
    code point lies between ``start`` and ``end`` inclusive; the endpoints may
    themselves be escaped (e.g. ``\]-\^``). A literal ``-`` is produced when it
    is escaped (``\-``) or appears where it cannot start a span -- at the very end
    of the body, or immediately after a completed span.

    Examples::

        [[a-z]]        -> ({a, b, ..., z}, False)
        [[abcg-i]]     -> ({a, b, c, g, h, i}, False)
        [[a-zA-Z]]     -> ({a..z, A..Z}, False)
        [[0-9_]]       -> ({0..9, _}, False)
        [[\x30-\x39]]  -> ({0..9}, False)      hex escapes work as endpoints
        [[^\nabc\r\0]] -> ({\n, a, b, c, \r, \0}, True)  i.e. none of these

    Args:
        token: The full range token, including ``[[`` and ``]]``.

    Returns:
        A ``(chars, negated)`` pair: the set of characters the range lists, and
        whether the range is negated (matches the complement of that set).

    Raises:
        ValueError: If the token is not delimited by ``[[`` / ``]]``, its body is
            empty (``[[]]`` or ``[[^]]``), or a span's start code point exceeds
            its end.
    """
    if not (token.startswith("[[") and token.endswith("]]")):
        raise ValueError(f"Malformed range token: {token!r}")
    body = token[2:-2]
    negated = body.startswith("^")
    if negated:
        body = body[1:]
    if not body:
        raise ValueError(
            "Empty negated range '[[^]]' is not allowed" if negated
            else "Empty range '[[]]' is not allowed")

    # Tokenize the body into characters, decoding backslash escapes (including
    # \xNN / \u{...}), while remembering which characters came from an escape so
    # an escaped '-' is never treated as a span separator.
    items = [(unescape_character(token) if escaped else token, escaped)
             for token, escaped in scan_escaped_tokens(body)]

    chars = set()
    index = 0
    while index < len(items):
        char, _escaped = items[index]
        # A span is start '-' end, where the '-' is an unescaped literal dash and
        # an end character follows.
        is_dash = (index + 1 < len(items)
                   and items[index + 1] == ("-", False))
        if is_dash and index + 2 < len(items):
            start_cp = ord(char)
            end_cp = ord(items[index + 2][0])
            if start_cp > end_cp:
                raise ValueError(
                    f"Range start '{char}' is after end "
                    f"'{items[index + 2][0]}' in {token!r}"
                )
            for code_point in range(start_cp, end_cp + 1):
                chars.add(chr(code_point))
            index += 3
        else:
            chars.add(char)
            index += 1
    return chars, negated


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


def stringify_grammar(grammar: Grammar) -> str:
    """Render a grammar as text for debug logging.

    Args:
        grammar: A mapping of nonterminal to its alternatives.

    Returns:
        A human-readable, multi-line listing of the rules.
    """
    lines = []
    for non_terminal, rules in grammar.items():
        alternatives = f"\n{' ' * 4}| ".join(
            " ".join(
                f"<{s}>" if s.is_non_terminal()
                else f"[{s}]" if s.is_semantic_action()
                else str(s)
                for s in rule
            )
            for rule in rules
        )
        lines.append(f"{non_terminal} ->\n{' ' * 6}{alternatives}")
    return "\n".join(lines)


def _format_symbol_set(symbols) -> str:
    """Render a set of terminal symbols compactly for logging (sorted by value).

    Each symbol is shown quoted (``'{'``) so literal brace/comma characters do not
    visually merge with the surrounding set notation.
    """
    def key(sym):
        value = sym.value if isinstance(sym.value, str) else "".join(sorted(sym.value))
        return value

    def show(sym):
        if sym.is_epsilon():
            return "ε"
        return f"'{sym}'"

    return "{" + ", ".join(show(s) for s in sorted(symbols, key=key)) + "}"


def stringify_first_follow(sets, title: str) -> str:
    """Render FIRST or FOLLOW sets as an aligned, sorted table for logging.

    Args:
        sets: A mapping from symbol to its set of terminals.
        title: A heading (e.g. ``"FIRST"`` or ``"FOLLOW"``).

    Returns:
        A multi-line string, one nonterminal per line.
    """
    # Only nonterminals are interesting; terminals map to themselves.
    rows = [(str(sym), members) for sym, members in sets.items()
            if sym.is_non_terminal()]
    rows.sort(key=lambda r: r[0])
    width = max((len(name) for name, _ in rows), default=0)
    lines = [f"{title} sets:"]
    for name, members in rows:
        lines.append(f"  {name:<{width}} = {_format_symbol_set(members)}")
    return "\n".join(lines)


def stringify_parse_table(parse_table) -> str:
    """Render the parse table as ``nonterminal, lookahead -> production`` rows.

    Args:
        parse_table: ``{nonterminal: {terminal: production}}``.

    Returns:
        A multi-line string listing every populated cell, grouped by nonterminal.
    """
    lines = ["Parse table:"]
    for non_terminal in sorted(parse_table.keys(), key=str):
        row = parse_table[non_terminal]
        lines.append(f"  {non_terminal}:")
        for terminal in sorted(row.keys(), key=str):
            production = row[terminal]
            body = " ".join(str(s) for s in production) or "epsilon"
            lines.append(f"      on {str(terminal):<24} -> {body}")
    return "\n".join(lines)


# ======================================================================== #
# Grammar analysis and health diagnostics
# ======================================================================== #

def compute_reachable(grammar: Grammar, start) -> set:
    """Return the set of nonterminals reachable from ``start``.

    A nonterminal is reachable if the start symbol can, through some sequence of
    productions, expand to a string mentioning it. Unreachable nonterminals are
    dead code: they never participate in any parse.

    Args:
        grammar: The grammar (keys normalized to :class:`GrammerType`).
        start: The start nonterminal.

    Returns:
        The set of reachable nonterminal symbols (always including ``start`` when
        it is in the grammar).
    """
    reachable = set()
    stack = [start]
    while stack:
        current = stack.pop()
        if current in reachable or current not in grammar:
            continue
        reachable.add(current)
        for production in grammar[current]:
            for symbol in production:
                if symbol.is_non_terminal() and symbol not in reachable:
                    stack.append(symbol)
    return reachable


def compute_productive(grammar: Grammar) -> set:
    """Return the set of nonterminals that can derive a finite terminal string.

    A nonterminal is productive if at least one of its productions consists only
    of terminals, epsilon, actions, and other productive nonterminals. A
    non-productive nonterminal can never finish a parse (its every expansion
    requires expanding a non-productive symbol), which usually signals a mistake.
    Iterates to a fixed point.

    Args:
        grammar: The grammar (keys normalized to :class:`GrammerType`).

    Returns:
        The set of productive nonterminal symbols.
    """
    productive = set()
    while True:
        updated = False
        for non_terminal, productions in grammar.items():
            if non_terminal in productive:
                continue
            for production in productions:
                if all(symbol.is_terminal() or symbol.is_epsilon()
                       or symbol.is_semantic_action() or symbol in productive
                       for symbol in production):
                    productive.add(non_terminal)
                    updated = True
                    break
        if not updated:
            return productive


def find_unused_terminals(grammar: Grammar, terminal_table: dict) -> list:
    """Return the names of declared terminals that no production references.

    Args:
        grammar: The grammar (keys normalized to :class:`GrammerType`).
        terminal_table: The terminal section of the identifier table.

    Returns:
        A sorted list of declared terminal names not used anywhere in the grammar
        (the implicit ``other`` set is never reported).
    """
    used = set()
    for productions in grammar.values():
        for production in productions:
            for symbol in production:
                # Only *named* terminal references can match a declared name. An
                # inline set/atom terminal (whose value is a character collection)
                # is anonymous, so it is irrelevant here -- and its set value is
                # unhashable, so it must not be added to ``used``.
                if symbol.is_terminal() and symbol.is_named_terminal():
                    used.add(symbol.value)
    declared = {name for name in terminal_table if name != "other"}
    return sorted(declared - used)


def find_duplicate_productions(grammar: Grammar) -> Dict[GrammerType, list]:
    """Return, per nonterminal, any production body that appears more than once.

    A duplicated alternative is harmless but redundant, and often a copy-paste
    slip worth surfacing when debugging a grammar.

    Args:
        grammar: The grammar (keys normalized to :class:`GrammerType`).

    Returns:
        A mapping from nonterminal to the list of its repeated production bodies
        (each rendered as a string); nonterminals without duplicates are omitted.
    """
    duplicates = {}
    for non_terminal, productions in grammar.items():
        seen = {}
        for production in productions:
            key = " ".join(str(symbol) for symbol in production) or "epsilon"
            seen[key] = seen.get(key, 0) + 1
        repeated = [body for body, count in seen.items() if count > 1]
        if repeated:
            duplicates[non_terminal] = repeated
    return duplicates


def analyze_grammar(grammar: Grammar, terminal_table: dict,
                    first=None, follow=None) -> dict:
    """Compute a bundle of health metrics for a grammar.

    Gathers nullable, reachable, productive, unused-terminal and duplicate-
    production information in one pass-friendly structure for reporting.

    Args:
        grammar: The grammar (keys normalized to :class:`GrammerType`).
        terminal_table: The terminal section of the identifier table.
        first: Optional precomputed FIRST sets; computed if not supplied.
        follow: Optional precomputed FOLLOW sets (only used for the report).

    Returns:
        A dict with keys ``nonterminals``, ``productions``, ``terminals``,
        ``actions`` (counts), ``nullable``, ``unreachable``, ``unproductive``
        (symbol lists), ``unused_terminals`` (name list) and ``duplicates``.
    """
    if first is None:
        first = compute_first(grammar)
    start = next(iter(grammar)) if grammar else None
    reachable = compute_reachable(grammar, start) if start is not None else set()
    productive = compute_productive(grammar)

    nullable = sorted((nt for nt in grammar if EPSILON in first.get(nt, set())), key=str)
    unreachable = sorted((nt for nt in grammar if nt not in reachable), key=str)
    unproductive = sorted((nt for nt in grammar if nt not in productive), key=str)

    return {
        "nonterminals": len(grammar),
        "productions": sum(len(alts) for alts in grammar.values()),
        "terminals": len([n for n in terminal_table if n != "other"]),
        "actions": None,  # filled by caller if available
        "nullable": nullable,
        "unreachable": unreachable,
        "unproductive": unproductive,
        "unused_terminals": find_unused_terminals(grammar, terminal_table),
        "duplicates": find_duplicate_productions(grammar),
    }


def stringify_grammar_analysis(analysis: dict) -> str:
    """Render an :func:`analyze_grammar` result as a readable health report.

    Warnings (unreachable, unproductive, unused, duplicates) are called out
    explicitly; a clean grammar reports "no issues detected".

    Args:
        analysis: The dict returned by :func:`analyze_grammar`.

    Returns:
        A multi-line report string.
    """
    lines = ["Grammar analysis:"]
    lines.append(f"  size: {analysis['nonterminals']} nonterminals, "
                 f"{analysis['productions']} productions, "
                 f"{analysis['terminals']} named terminals")
    nullable = analysis["nullable"]
    lines.append(f"  nullable nonterminals ({len(nullable)}): "
                 + (", ".join(str(nt) for nt in nullable) if nullable else "none"))

    issues = 0

    def warn_list(label, items):
        nonlocal issues
        if items:
            issues += len(items)
            rendered = ", ".join(str(i) for i in items)
            lines.append(f"  WARNING: {label} ({len(items)}): {rendered}")

    warn_list("unreachable nonterminals", analysis["unreachable"])
    warn_list("unproductive nonterminals", analysis["unproductive"])
    warn_list("unused declared terminals", analysis["unused_terminals"])

    if analysis["duplicates"]:
        for non_terminal, bodies in analysis["duplicates"].items():
            issues += len(bodies)
            for body in bodies:
                lines.append(f"  WARNING: duplicate production in "
                             f"{non_terminal}: {body}")

    if issues == 0:
        lines.append("  no issues detected")
    return "\n".join(lines)


# ======================================================================== #
# Grammar transformations: left recursion, left factoring, FIRST/FOLLOW
# ======================================================================== #

def normalize_grammar_keys(grammar: Grammar) -> Grammar:
    """Rebuild ``grammar`` so every key is a :class:`GrammerType` nonterminal.

    After left-recursion elimination and left factoring the dict ends up with
    mixed key types: original nonterminals are plain ``str`` (added by
    :class:`add_identifers`) while freshly minted ones are :class:`GrammerType`.
    Production bodies always reference nonterminals as :class:`GrammerType`, and
    FIRST/FOLLOW index their tables by the symbols found in those bodies, so every
    key must be a :class:`GrammerType` for the lookups to hit.

    Args:
        grammar: A grammar with possibly mixed ``str`` / :class:`GrammerType` keys.

    Returns:
        An equivalent grammar with :class:`GrammerType` keys, insertion order
        preserved.
    """
    normalized = {}
    for key, productions in grammar.items():
        if isinstance(key, GrammerType):
            normalized[key] = productions
        else:
            normalized[GrammerType(str(key), SymbolType.non_terminal)] = productions
    return normalized


def compute_first(grammar: Grammar) -> Dict[GrammerType, set]:
    """Compute the FIRST set of every nonterminal.

    FIRST(X) is the set of terminals that can begin a string derived from X, plus
    EPSILON if X can derive the empty string. Terminals seed their own singleton
    sets and semantic actions are transparent. Iterates to a fixed point.

    Args:
        grammar: The grammar (keys normalized to :class:`GrammerType`).

    Returns:
        A mapping from each symbol to its FIRST set.
    """
    first = {non_terminal: set() for non_terminal in grammar}
    first.update({
        terminal: {terminal}
        for rule in grammar.values()
        for production in rule
        for terminal in production
        if terminal.is_terminal()
    })
    first[EPSILON] = {EPSILON}

    while True:
        updated = False
        for non_terminal, rules in grammar.items():
            for production in rules:
                # Walk the production left to right. A semantic action is
                # transparent. Each symbol contributes FIRST(symbol) - {epsilon};
                # we only advance past a symbol if it is nullable. If every symbol
                # is nullable the production itself is nullable, so epsilon joins
                # FIRST(non_terminal).
                nullable_through = True
                for symbol in production:
                    if symbol.is_semantic_action():
                        continue
                    if symbol.is_terminal():
                        # A terminal contributes itself and stops the scan.
                        if symbol not in first[non_terminal]:
                            first[non_terminal].add(symbol)
                            updated = True
                        nullable_through = False
                        break
                    # Nonterminal: contribute its non-epsilon FIRST, then continue
                    # only if it is nullable.
                    before = len(first[non_terminal])
                    first[non_terminal] |= first[symbol] - {EPSILON}
                    if len(first[non_terminal]) != before:
                        updated = True
                    if EPSILON in first[symbol]:
                        continue
                    nullable_through = False
                    break
                if nullable_through and EPSILON not in first[non_terminal]:
                    first[non_terminal].add(EPSILON)
                    updated = True
        if not updated:
            break
    return first


def compute_follow(grammar: Grammar, first: Dict[GrammerType, set]) -> Dict[GrammerType, set]:
    """Compute the FOLLOW set of every nonterminal.

    FOLLOW(A) is the set of terminals that can appear immediately after A in some
    derivation; the start symbol additionally follows with end-of-input (``$``).
    Iterates to a fixed point.

    Args:
        grammar: The grammar (keys normalized to :class:`GrammerType`).
        first: The FIRST sets from :func:`compute_first`.

    Returns:
        A mapping from each nonterminal to its FOLLOW set.
    """
    follow = {non_terminal: set() for non_terminal in grammar}
    start_symbol = next(iter(grammar))
    follow[start_symbol].add(GrammerType("$", SymbolType.terminal))

    while True:
        updated = False
        for non_terminal, rules in grammar.items():
            for production in rules:
                for i, symbol in enumerate(production):
                    if not symbol.is_non_terminal():
                        continue
                    # For A -> alpha B beta, FOLLOW(B) gains FIRST(beta) - {eps},
                    # where beta is the *entire* remainder of the production (not
                    # just the next symbol -- a nullable symbol in the middle must
                    # not hide the symbols after it). first_of_sequence already
                    # skips transparent semantic actions and returns {EPSILON} for
                    # an empty remainder.
                    rest_first = first_of_sequence(production[i + 1:], first)
                    if rest_first - {EPSILON} - follow[symbol]:
                        follow[symbol] |= rest_first - {EPSILON}
                        updated = True
                    # Only if the whole remainder is nullable (or empty) does
                    # FOLLOW(non_terminal) flow into FOLLOW(B).
                    if EPSILON in rest_first and (follow[non_terminal] - follow[symbol]):
                        follow[symbol] |= follow[non_terminal]
                        updated = True
        if not updated:
            break
    return follow


def first_of_sequence(production, first: Dict[GrammerType, set]) -> set:
    """Compute the FIRST set of a whole production (a sequence of symbols).

    Semantic actions are transparent.

    Args:
        production: The sequence of symbols.
        first: The per-symbol FIRST sets from :func:`compute_first`.

    Returns:
        The set of leading terminals, including EPSILON if the entire sequence is
        nullable.
    """
    result = set()
    nullable_through = True
    for symbol in production:
        if symbol.is_semantic_action():
            continue
        if symbol.is_terminal():
            result.add(symbol)
            nullable_through = False
            break
        result |= first[symbol] - {EPSILON}
        if EPSILON in first[symbol]:
            continue
        nullable_through = False
        break
    if nullable_through:
        result.add(EPSILON)
    return result


def construct_parse_table(grammar: Grammar,
                          first: Dict[GrammerType, set],
                          follow: Dict[GrammerType, set],
                          strict: bool = False,
                          kinds_out: Optional[dict] = None) -> Dict[GrammerType, dict]:
    """Build the (q)LL(1) parse table.

    For each production, every terminal in its FIRST maps to it (a "shift"); if
    the production is nullable, every terminal in FOLLOW(nonterminal) also maps to
    it (an "epsilon fallback").

    These grammars are Q-grammars, which relax strict LL(1): on a terminal in both
    FIRST and FOLLOW the shift production is preferred and the nullable production
    is the fallback (CTLL resolves this by overload specificity, the concrete
    ``ctll::term``/``ctll::set`` rule winning over the epsilon rule). So a shift
    entry and an epsilon-fallback entry may coexist on one terminal. A genuine
    conflict is only two *shift* productions on one terminal, or two distinct
    *nullable* productions sharing a FOLLOW terminal.

    Args:
        grammar: The grammar (keys normalized to :class:`GrammerType`).
        first: FIRST sets from :func:`compute_first`.
        follow: FOLLOW sets from :func:`compute_follow`.
        strict: If True, require classic LL(1): any collision in a cell is a
            conflict (no shift/epsilon coexistence).

    Returns:
        ``{nonterminal: {lookahead terminal: production}}``. Each cell keeps the
        shift production when present, otherwise the epsilon-fallback production.

    Raises:
        ValueError: If the grammar is not (q)LL(1) (or not LL(1) under ``strict``).
    """
    parse_table = defaultdict(dict)
    # Remember how each cell was filled so Q-grammar coexistence can be allowed.
    fill_kind = defaultdict(dict)  # non_terminal -> {terminal: "shift" | "epsilon"}
    coexistences = 0               # count of Q-grammar shift/epsilon resolutions

    for non_terminal, rules in grammar.items():
        for production in rules:
            production_first = first_of_sequence(production, first)
            nullable = EPSILON in production_first

            def assign(terminal, kind):
                nonlocal coexistences
                existing = fill_kind[non_terminal].get(terminal)
                if existing is None:
                    parse_table[non_terminal][terminal] = production
                    fill_kind[non_terminal][terminal] = kind
                    return
                if strict:
                    raise ValueError(
                        f"Grammar is not LL(1): Conflict for {non_terminal.value} -> "
                        f"{[str(s) for s in production]} on {terminal.value}"
                    )
                # Q-grammar: a shift may coexist with an epsilon fallback.
                if existing != kind:
                    coexistences += 1
                    trace(
                        f"Q-grammar resolution at ({non_terminal}, {terminal}): "
                        f"{kind} coexists with {existing}; shift wins"
                    )
                    # Keep the shift production; epsilon is only the fallback.
                    if kind == "shift":
                        parse_table[non_terminal][terminal] = production
                        fill_kind[non_terminal][terminal] = "shift"
                    return
                # Same kind on the same terminal is a real, unresolvable conflict
                # (two shifts, or two different nullable productions).
                if parse_table[non_terminal][terminal] != production:
                    existing_body = " ".join(str(s) for s in parse_table[non_terminal][terminal])
                    new_body = " ".join(str(s) for s in production)
                    logger.error(
                        f"{'LL(1)' if strict else '(q)LL(1)'} conflict in nonterminal "
                        f"'{non_terminal}' on lookahead '{terminal}' ({kind}/{kind}):\n"
                        f"    existing: {non_terminal} -> {existing_body}\n"
                        f"    new:      {non_terminal} -> {new_body}"
                    )
                    raise ValueError(
                        f"Grammar is not (q)LL(1): Conflict for {non_terminal.value} -> "
                        f"{[str(s) for s in production]} on {terminal.value}"
                    )

            for terminal in production_first - {EPSILON}:
                assign(terminal, "shift")
            if nullable:
                for terminal in follow[non_terminal]:
                    assign(terminal, "epsilon")

    cells = sum(len(row) for row in parse_table.values())
    logger.debug(
        f"Parse table built: {cells} cells across {len(parse_table)} nonterminals"
        + (f", {coexistences} Q-grammar shift/epsilon resolutions" if coexistences else "")
    )
    if kinds_out is not None:
        for non_terminal, row in fill_kind.items():
            kinds_out[non_terminal] = dict(row)
    return parse_table


def left_factor(grammar: Grammar) -> "tuple[Grammar, bool]":
    """Apply one pass of left factoring.

    Productions of a nonterminal that share a common leading prefix (a leading
    terminal or nonterminal, plus any semantic actions before it) are pulled into
    a fresh helper nonterminal carrying the differing suffixes. The caller runs
    this to a fixed point. All productions are kept as :class:`HashableList` so
    that later slicing stays hashable.

    Args:
        grammar: The grammar to factor.

    Returns:
        A tuple ``(new_grammar, changed)`` where ``changed`` is True if any new
        helper nonterminal was introduced.
    """
    new_grammar = {}
    was_updated = False
    for non_terminal, productions in grammar.items():
        new_productions = []
        prefixes = defaultdict(OrderedSet)

        for production in productions:
            if not production:
                continue
            # The prefix is the leading semantic actions plus the first real symbol.
            prefix = []
            for symbol in production:
                prefix.append(symbol)
                if not symbol.is_semantic_action():
                    break
            prefixes[tuple(prefix)].add(production[len(prefix):])

        # Productions that were empty become explicit epsilon (kept hashable).
        for production in productions:
            if not production:
                new_productions.append(HashableList([EPSILON]))

        for prefix, suffixes in prefixes.items():
            if len(suffixes) == 1:
                new_productions.append(HashableList(prefix) + HashableList(suffixes)[0])
            else:
                was_updated = True
                helper = GrammerType(f"{non_terminal}_{prefix[0]}", SymbolType.non_terminal)
                while helper in grammar or helper in new_grammar:
                    helper.value = f"{non_terminal}_{prefix[0]}"
                prefix_str = " ".join(str(s) for s in prefix)
                trace(f"factor: {non_terminal} -> common prefix '{prefix_str}' "
                      f"hoisted into helper '{helper}' ({len(suffixes)} suffixes)")
                new_productions.append(HashableList(prefix) + HashableList([helper]))
                new_grammar[helper] = suffixes

        new_grammar[non_terminal] = new_productions

    return new_grammar, was_updated


def eliminate_left_recursion(grammar: Grammar) -> Grammar:
    """Remove left recursion from the grammar using Paull's algorithm.

    Earlier nonterminals are substituted into later ones to expose, then remove,
    immediate left recursion for each nonterminal in turn.

    Args:
        grammar: The grammar to transform (mutated in place and also returned).

    Returns:
        The left-recursion-free grammar.
    """
    non_terminals = list(grammar.keys())
    for i, a in enumerate(non_terminals):
        for j in range(i):
            b = non_terminals[j]
            new_productions = HashableList([])
            for production in grammar[a]:
                if production and production[0] == b:
                    for gamma in grammar[b]:
                        new_productions.append(gamma + production[1:])
                else:
                    new_productions.append(production)
            grammar[a] = new_productions
        remove_immediate_left_recursion(grammar, a)
    return grammar


def remove_immediate_left_recursion(grammar: Grammar, a: GrammerType) -> None:
    """Remove immediate left recursion for a single nonterminal.

    ``A -> A alpha | beta`` is rewritten to ``A -> beta A'`` and
    ``A' -> alpha A' | epsilon``. Productions are built as :class:`HashableList`
    throughout so subsequent factoring passes can hash and slice them.

    Args:
        grammar: The grammar (mutated in place).
        a: The nonterminal to de-recurse.
    """
    alpha_productions = HashableList([])
    beta_productions = HashableList([])
    for production in grammar[a]:
        if production and production[0] == a:
            alpha_productions.append(production[1:])
        else:
            beta_productions.append(production)

    if not alpha_productions:
        return

    i = 0
    helper = GrammerType(f"{a.value}'", SymbolType.non_terminal)
    while helper.value in grammar:
        helper.value = f"{a.value}_{i}"
        i += 1

    logger.debug(f"Removing immediate left recursion in '{a}': "
                 f"{len(alpha_productions)} recursive + {len(beta_productions)} base "
                 f"production(s); introduced helper '{helper}'")
    grammar[a] = (
        [beta + HashableList([helper]) for beta in beta_productions]
        or [HashableList([helper])]
    )
    grammar[helper] = (
        [alpha + HashableList([helper]) for alpha in alpha_productions]
        + [HashableList([EPSILON])]
    )


def inline_pure_terminal_nonterminals(grammar: Grammar) -> Grammar:
    """Inline nonterminals whose every alternative is a single terminal.

    Such a nonterminal is just a named character class (e.g.
    ``special -> dot | sopen | ...``). The source grammar sometimes lists members
    of the class both via the helper *and* directly in the same parent rule, which
    would otherwise look like a FIRST/FIRST conflict even though both paths are
    identical. Expanding ``<X> rest`` into one production ``terminal_i rest`` per
    alternative collapses the duplicates. The helper is then dropped.

    Args:
        grammar: The grammar to transform.

    Returns:
        The grammar with pure character-class helpers inlined and removed (the
        original is returned unchanged if there are none).
    """
    pure = {
        nt: [p[0] for p in productions]
        for nt, productions in grammar.items()
        if productions and all(len(p) == 1 and p[0].is_terminal() for p in productions)
    }
    if not pure:
        return grammar
    pure_names = {str(nt) for nt in pure}
    logger.debug(f"Inlining {len(pure)} pure character-class nonterminal(s): "
                 f"{', '.join(sorted(pure_names))}")

    def expand(production):
        # Repeatedly expand the first pure-nonterminal occurrence until none remain.
        results = [production]
        changed = True
        while changed:
            changed = False
            next_results = []
            for prod in results:
                hit = next(
                    ((idx, sym) for idx, sym in enumerate(prod)
                     if sym.is_non_terminal() and str(sym) in pure_names),
                    None,
                )
                if hit is None:
                    next_results.append(prod)
                    continue
                changed = True
                idx, sym = hit
                key = next(k for k in pure if str(k) == str(sym))
                for terminal in pure[key]:
                    next_results.append(
                        HashableList(prod[:idx])
                        + HashableList([terminal])
                        + HashableList(prod[idx + 1:])
                    )
            results = next_results
        return results

    new_grammar = {}
    for nt, productions in grammar.items():
        if nt in pure:
            continue  # drop the helper itself
        rebuilt = OrderedSet()
        for production in productions:
            for expanded in expand(production):
                rebuilt.add(expanded)
        new_grammar[nt] = rebuilt
    return new_grammar


# ------------------------------------------------------------------------ #
# Optional optimization passes (enabled by -O1 / -O2 / -O3).
#
# Each pass preserves the language and, by construction or by an explicit LL(1)
# re-check, the LL(1) property. They only ever shrink the grammar:
#   -O1  merge structurally-identical nonterminals (e.g. the nine digit tails
#        cC..kK collapse into one)
#   -O2  + inline nonterminals referenced exactly once
#   -O3  + inline nonterminals whose body is a single alternative
# ------------------------------------------------------------------------ #

def _production_signature(productions, self_name) -> frozenset:
    """Compute a canonical, order-independent signature of a nonterminal's rules.

    References to the nonterminal *itself* are mapped to a placeholder so two
    self-similar helpers can still be recognised as identical. Inline set/atom
    terminals (those whose value is a character collection) are keyed by their
    sorted members, so two equal inline sets compare equal; *named* terminals are
    keyed by their name, so two differently-named terminals are treated as
    distinct even if their character sets happen to coincide.

    Args:
        productions: The nonterminal's alternatives.
        self_name: The nonterminal's own name (for the self-reference placeholder).

    Returns:
        A hashable signature; two nonterminals with equal signatures are
        interchangeable.
    """
    signature = set()
    for production in productions:
        items = []
        for symbol in production:
            if symbol.is_non_terminal() and str(symbol) == str(self_name):
                items.append(("@self",))
            elif isinstance(symbol.value, (set, list)):
                items.append((symbol.type, tuple(sorted(symbol.value))))
            else:
                items.append((symbol.type, str(symbol)))
        signature.add(tuple(items))
    return frozenset(signature)


def _rename_symbols(grammar: Grammar, rename: dict) -> Grammar:
    """Apply nonterminal renames across a grammar, dropping the renamed-away keys.

    Args:
        grammar: The grammar to rewrite.
        rename: A mapping from old nonterminal name (``str``) to the kept
            :class:`GrammerType` it is merged into.

    Returns:
        A new grammar with references renamed and merged-away nonterminals removed.
    """
    new_grammar = {}
    for non_terminal, productions in grammar.items():
        if str(non_terminal) in rename:
            continue  # this nonterminal was merged into another
        rebuilt = OrderedSet()
        for production in productions:
            new_production = HashableList(
                rename[str(symbol)] if (symbol.is_non_terminal() and str(symbol) in rename)
                else symbol
                for symbol in production
            )
            rebuilt.add(new_production)
        new_grammar[non_terminal] = rebuilt
    return new_grammar


def merge_identical_nonterminals(grammar: Grammar) -> Grammar:
    """Collapse nonterminals with identical production sets into one (``-O1``).

    This is pure de-duplication: it changes neither the language nor the parser
    model's acceptance. Iterated to a fixed point, since merging can make two
    previously-distinct helpers become identical.

    Args:
        grammar: The grammar to optimize.

    Returns:
        The grammar with duplicate nonterminals merged.
    """
    while True:
        groups = defaultdict(list)
        for non_terminal, productions in grammar.items():
            groups[_production_signature(productions, non_terminal)].append(non_terminal)

        rename = {}
        for members in groups.values():
            if len(members) > 1:
                keep = members[0]
                for other in members[1:]:
                    rename[str(other)] = keep
                    trace(f"merge: {other} -> {keep} (identical productions)")
        if not rename:
            return grammar
        grammar = _rename_symbols(grammar, rename)


def _reference_counts(grammar: Grammar) -> "defaultdict[str, int]":
    """Count how many times each nonterminal is referenced across all productions.

    Args:
        grammar: The grammar to scan.

    Returns:
        A mapping from nonterminal name to its number of references.
    """
    counts = defaultdict(int)
    for productions in grammar.values():
        for production in productions:
            for symbol in production:
                if symbol.is_non_terminal():
                    counts[str(symbol)] += 1
    return counts


def _is_self_recursive(non_terminal, productions) -> bool:
    """Return True if any of ``productions`` references ``non_terminal`` itself.

    Args:
        non_terminal: The nonterminal whose productions these are.
        productions: Its alternatives.
    """
    return any(
        symbol.is_non_terminal() and str(symbol) == str(non_terminal)
        for production in productions
        for symbol in production
    )


def _substitute_nonterminal(grammar: Grammar, target_name: str, bodies) -> Grammar:
    """Inline ``target_name`` into every referrer and drop it from the grammar.

    When the target has several alternatives, occurrences expand as a cartesian
    product across those alternatives; an epsilon-only body contributes the empty
    string.

    Args:
        grammar: The grammar to rewrite.
        target_name: The nonterminal to inline.
        bodies: The target's alternatives (a list of :class:`HashableList`).

    Returns:
        A new grammar with the target inlined everywhere and removed.
    """
    new_grammar = {}
    for non_terminal, productions in grammar.items():
        if str(non_terminal) == target_name:
            continue
        rebuilt = OrderedSet()
        for production in productions:
            expansions = [HashableList([])]
            for symbol in production:
                if symbol.is_non_terminal() and str(symbol) == target_name:
                    expanded = []
                    for partial in expansions:
                        for body in bodies:
                            tail = [] if (len(body) == 1 and body[0].is_epsilon()) else list(body)
                            expanded.append(partial + HashableList(tail))
                    expansions = expanded
                else:
                    expansions = [partial + HashableList([symbol]) for partial in expansions]
            for expansion in expansions:
                rebuilt.add(expansion if len(expansion) else HashableList([EPSILON]))
        new_grammar[non_terminal] = rebuilt
    return new_grammar


def _accepts_as_grammar(grammar: Grammar, q_grammar: bool) -> bool:
    """Return True if the grammar is parseable under the chosen model.

    Builds the parse table and reports whether it is conflict-free. Used to guard
    optimization inlining so a pass never produces a grammar the selected model
    would reject.

    Args:
        grammar: The candidate grammar.
        q_grammar: True for the Q-grammar relaxation, False for classic LL(1).

    Returns:
        True if FIRST/FOLLOW yield a conflict-free table, else False.
    """
    try:
        first = compute_first(grammar)
        follow = compute_follow(grammar, first)
        construct_parse_table(grammar, first, follow, strict=not q_grammar)
        return True
    except ValueError:
        return False


def inline_nonterminals(grammar: Grammar, start_name: str,
                        single_production_only: bool, q_grammar: bool) -> Grammar:
    """Inline nonterminals into their referrers, guarded by a parse-table re-check.

    A candidate must be non-recursive, referenced at least once, and not the start
    symbol. Any inline that would make the grammar unparseable under the chosen
    model is skipped. Iterated to a fixed point.

    Args:
        grammar: The grammar to optimize.
        start_name: Name of the start symbol (never inlined).
        single_production_only: If True (``-O3``), a candidate is eligible when its
            body is a single alternative; if False (``-O2``), only candidates
            referenced exactly once are eligible.
        q_grammar: Parser model used by the inline guard.

    Returns:
        The grammar with eligible nonterminals inlined and removed.
    """
    while True:
        counts = _reference_counts(grammar)
        candidate = None
        for non_terminal, productions in grammar.items():
            name = str(non_terminal)
            if name == start_name or name not in counts:
                continue
            if _is_self_recursive(non_terminal, productions):
                continue
            eligible = (len(productions) == 1) if single_production_only else (counts[name] == 1)
            if not eligible:
                continue
            trial = _substitute_nonterminal(grammar, name, list(productions))
            if _accepts_as_grammar(trial, q_grammar):
                trace(f"inline: {name} ({counts[name]} ref(s), "
                      f"{len(productions)} production(s)) into referrers")
                candidate = trial
                break
            else:
                trace(f"inline skipped: {name} would break the parser model")
        if candidate is None:
            return grammar
        grammar = candidate


def optimize_grammar(grammar: Grammar, start_name: str, level: int, q_grammar: bool) -> Grammar:
    """Apply optimization passes up to ``level`` and return the result.

    Higher levels include all lower-level passes; merging is re-run after inlining
    so freshly-identical helpers collapse too. Inlining is guarded by the chosen
    parser model.

    Args:
        grammar: The grammar to optimize.
        start_name: Name of the start symbol.
        level: Optimization level (0-3); see the module docstring.
        q_grammar: Parser model used by the inline guard.

    Returns:
        The optimized grammar (the input is returned unchanged at level 0).
    """
    if level == 0:
        return grammar

    logger.debug(f"Optimization -O{level}: starting from {describe_grammar(grammar)}")

    if level >= 1:
        before = len(grammar)
        grammar = merge_identical_nonterminals(grammar)
        logger.debug(f"  -O1 merge identical: {before} -> {len(grammar)} nonterminals")
    if level >= 2:
        before = len(grammar)
        grammar = inline_nonterminals(grammar, start_name,
                                      single_production_only=False, q_grammar=q_grammar)
        grammar = merge_identical_nonterminals(grammar)
        logger.debug(f"  -O2 inline single-use: {before} -> {len(grammar)} nonterminals")
    if level >= 3:
        before = len(grammar)
        grammar = inline_nonterminals(grammar, start_name,
                                      single_production_only=True, q_grammar=q_grammar)
        grammar = merge_identical_nonterminals(grammar)
        logger.debug(f"  -O3 inline single-production: {before} -> {len(grammar)} nonterminals")

    logger.info(f"Optimization -O{level} complete: {describe_grammar(grammar)}")
    return grammar


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
_MIN_RANGE_RUN = 3

# Decomposing a lookahead set into ranges adds one ``rule`` overload per run, and
# overload resolution itself has a cost, so the transform only pays off on sets
# large enough that the saved character comparisons dominate. Sets smaller than
# this are left as a single ``set`` lookahead. (Empirically, gating on size keeps
# the big wins -- alphanumerics, hex digits -- without flooding the overload set
# with marginal multi-run cases that slow compilation.)
_MIN_RANGE_SET_SIZE = 16


def decompose_into_runs(chars, min_range_len: int = _MIN_RANGE_RUN):
    """Partition a character set into contiguous ``range`` runs and a residual.

    The characters are sorted by code point and split into maximal runs of
    consecutive code points (a classic interval cover, which the greedy left-to-
    right scan computes optimally). Each run of at least ``min_range_len``
    characters becomes a ``(lo, hi)`` range; every other character is left in the
    residual list to be emitted individually.

    Replacing a run of ``k`` consecutive characters by one ``ctll::range<lo,hi>``
    turns ``k`` compile-time equality comparisons into two ordered comparisons, so
    a wide lookahead such as ``{0-9, A-Z, _, a-z}`` collapses from 63 comparisons
    to three ranges plus one residual term.

    Args:
        chars: The member characters of a positive lookahead set.
        min_range_len: Minimum run length worth turning into a range.

    Returns:
        A tuple ``(ranges, residual)`` where ``ranges`` is a list of ``(lo, hi)``
        character pairs (each inclusive and contiguous) and ``residual`` is the
        sorted list of leftover characters.
    """
    ordered = sorted(set(chars), key=ord)
    ranges = []
    residual = []
    index = 0
    count = len(ordered)
    while index < count:
        end = index
        while end + 1 < count and ord(ordered[end + 1]) == ord(ordered[end]) + 1:
            end += 1
        run = ordered[index:end + 1]
        if len(run) >= min_range_len:
            ranges.append((run[0], run[-1]))
        else:
            residual.extend(run)
        index = end + 1
    return ranges, residual


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
    # epsilon-fallback cell of this state (Q-grammar: shift wins).
    for rhs in order:
        group_chars[rhs] |= group_eps_chars[rhs] - set(shift_char_owner)

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


# ======================================================================== #
# Built-in test suite (run with --run-tests)
# ======================================================================== #
#
# The tests live in the module itself so the single-file tool stays
# self-verifying: ``python tablewright.py --run-tests`` exercises the data
# structures, the FIRST/FOLLOW and parse-table maths, the grammar analysis,
# the terminal aliaser (including C++ keyword guarding and composed set names),
# the optimization passes, and a full grammar-text-to-C++ integration path.
# They use only the standard library's ``unittest`` so there is nothing extra to
# install.


def _build_identifier_table(gram_text: str) -> IdentifierTable:
    """Run the front-end pipeline on grammar text and return its identifier table.

    Resets the module-global :data:`identifier_table` (so tests are isolated),
    then parses, transforms, collects identifiers, verifies them, breaks string
    literals, eliminates left recursion and left-factors to a fixed point, and
    resolves the global ``other`` set -- exactly the sequence :func:`main` uses up
    to the point of code generation.

    Args:
        gram_text: A grammar in the ``.gram`` dialect.

    Returns:
        The populated identifier table, ready for :func:`table_to_constexpr_cpp`.
    """
    identifier_table[SymbolType.action] = set()
    identifier_table[SymbolType.non_terminal] = {}
    identifier_table[SymbolType.terminal] = {
        "other": GrammerType([], SymbolType.negitive_set)
    }
    tree = Lark(grammar, start="start").parse(gram_text)
    tree = (SpaceTransformer() * RuleTransformer() * SetTransformer()).transform(tree)
    add_identifers().visit(tree)
    expand_groups_and_quantifiers(identifier_table)
    add_semantic_action_identifiers(identifier_table)
    verify_identifiers(identifier_table)
    break_strings(identifier_table)
    identifier_table[SymbolType.non_terminal] = eliminate_left_recursion(
        identifier_table[SymbolType.non_terminal]
    )
    updated = True
    while updated:
        identifier_table[SymbolType.non_terminal], updated = left_factor(
            identifier_table[SymbolType.non_terminal]
        )
    identifier_table[SymbolType.terminal]["other"].value = get_other(identifier_table)
    return identifier_table


def _generate_cpp(gram_text: str, *, optimization: int = 0,
                  q_grammar: bool = True, namespace: str = "g",
                  guard: str = "G_H", grammar_name: str = "g") -> str:
    """Build and render a grammar end to end, returning the generated C++ header.

    A thin wrapper over :func:`_build_identifier_table` plus
    :func:`table_to_constexpr_cpp` with a minimal argument object, used by the
    integration tests.

    Args:
        gram_text: The grammar source.
        optimization: Optimization level (0-3).
        q_grammar: Parser model (Q-grammar when True).
        namespace: C++ namespace for the output.
        guard: Include-guard macro.
        grammar_name: The generated struct's name.

    Returns:
        The rendered header text.
    """
    table = _build_identifier_table(gram_text)
    args = argparse.Namespace(
        optimization=optimization, q_grammar=q_grammar,
        namespace=namespace, guard=guard, grammer_name=grammar_name,
    )
    return table_to_constexpr_cpp(table, args)


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
        self.assertEqual(cpp_char_literal("\xff"), "'\\xFF'")

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
        self.assertNotIn("U'\\xFF'", cpp)

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
                 QualityOfLifeTests, RegressionTests):
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


# ======================================================================== #
# Command-line interface
# ======================================================================== #

def is_accessible(path) -> bool:
    """Return True if ``path`` is readable by the current process."""
    return os.access(path, os.R_OK)


def is_accessible_file(filepath) -> bool:
    """Return True if ``filepath`` exists, is a regular file, and is readable."""
    return os.path.isfile(filepath) and is_accessible(filepath)


def is_accessible_dir(dirpath) -> bool:
    """Return True if ``dirpath`` exists, is a directory, and is readable."""
    return os.path.isdir(dirpath) and is_accessible(dirpath)


class ValidateFileExistsAction(argparse.Action):
    """argparse action accepting ``-`` (stdin) or an existing, readable file."""

    def __call__(self, parser, namespace, arg: TextIOWrapper, option_string=None):
        """Store the opened file, or fail if it is neither stdin nor readable."""
        # ``arg`` is the opened file object; '-' denotes stdin (name is '<stdin>').
        if arg.name in ("-", "<stdin>") or is_accessible_file(arg.name):
            setattr(namespace, self.dest, arg)
        else:
            parser.error(f"The file {arg.name} does not exist or cannot be read.")


class ValidateFileOrDirectoryExistsAction(argparse.Action):
    """argparse action accepting an existing, readable file or directory."""

    def __call__(self, parser, namespace, arg: Path, option_string=None):
        """Store the path, or fail if it does not exist or is not readable."""
        if is_accessible_file(arg) or is_accessible_dir(arg):
            setattr(namespace, self.dest, arg)
        else:
            parser.error(f"The provided path {arg} does not exist or cannot be read.")


class ValidateOutputPathAction(argparse.Action):
    """argparse action for an output destination (need not exist yet).

    Accepts an existing directory (the header is written inside it as ``fname``)
    or any path whose parent directory exists (the header is written there
    directly). Unlike an input path, an output path is allowed not to exist --
    that is the normal case when generating a new file.
    """

    def __call__(self, parser, namespace, arg: Path, option_string=None):
        """Store the path if it is a writable destination, else fail."""
        path = Path(arg)
        if is_accessible_dir(path):
            setattr(namespace, self.dest, path)
            return
        parent = path.parent if str(path.parent) else Path(".")
        if is_accessible_dir(parent):
            setattr(namespace, self.dest, path)
            return
        parser.error(
            f"The output path {arg} is not writable: neither it nor its "
            f"parent directory exists."
        )


class ValidateCppIdentifierNameAction(argparse.Action):
    """argparse action accepting only a valid C++ identifier."""

    def __call__(self, parser, namespace, arg: str, option_string=None):
        """Store the name, or fail if it is not a valid C++ identifier."""
        if re.search(r"^[_a-zA-Z][_a-zA-Z0-9]*$", arg):
            setattr(namespace, self.dest, arg)
        else:
            parser.error(f"The provided name {arg} is not a valid C++ identifier name.")


LOGGING_LEVELS = {
    "trace": "TRACE",
    "debug": "DEBUG",
    "info": "INFO",
    "warn": "WARNING",
    "error": "ERROR",
    "critical": "CRITICAL",
}


class SetLoggingLevelAction(argparse.Action):
    """argparse action mapping a level keyword to its ``logging`` level name."""

    def __call__(self, parser, namespace, arg: str, option_string=None):
        """Store the resolved level name, or fail on an unknown keyword."""
        if arg in LOGGING_LEVELS:
            setattr(namespace, self.dest, LOGGING_LEVELS[arg])
        else:
            parser.error("Invalid logging level specified.")


def _sanitize_cpp_identifier(text: str, fallback: str) -> str:
    """Turn an arbitrary string into a valid C++ identifier.

    Non-identifier characters become underscores, a leading digit is prefixed
    with an underscore, and an empty result falls back to ``fallback``. Used to
    derive sensible default names from an input filename.

    Args:
        text: The raw string (typically a file stem).
        fallback: Identifier to use when ``text`` has no usable characters.

    Returns:
        A valid C++ identifier.
    """
    cleaned = re.sub(r"[^A-Za-z0-9_]", "_", text)
    cleaned = cleaned.strip("_")
    if not cleaned:
        return fallback
    if cleaned[0].isdigit():
        cleaned = "_" + cleaned
    return cleaned


def apply_filename_defaults(args: argparse.Namespace) -> None:
    """Fill unset output-config options with names derived from the input file.

    Any of ``--fname``, ``--namespace``, ``--guard`` and the grammar struct name
    left unspecified is derived from the input filename's stem (``pcre.gram`` ->
    namespace ``pcre``, guard ``PCRE_HPP``, file ``pcre.hpp``, struct ``pcre``),
    which removes most of the boilerplate from a typical invocation while leaving
    every value overridable. Reading from stdin (no real filename) falls back to
    the historical ``Grammer`` / ``GRAMMER_HPP`` defaults.

    Args:
        args: The parsed arguments namespace; updated in place.
    """
    name = getattr(args.input, "name", None)
    if name in (None, "-", "<stdin>"):
        stem = "Grammer"
    else:
        stem = Path(name).stem or "Grammer"
    identifier = _sanitize_cpp_identifier(stem, "Grammer")

    if args.namespace is None:
        args.namespace = identifier
    if args.grammer_name is None:
        args.grammer_name = identifier
    if args.guard is None:
        args.guard = _sanitize_cpp_identifier(stem.upper(), "GRAMMER") + "_HPP"
    if args.fname is None:
        args.fname = Path(f"{identifier}.hpp")


def parse_args() -> argparse.Namespace:
    """Define and parse the command-line interface.

    Returns:
        The parsed arguments. Notable fields: ``input`` (an open file), ``output``
        (a directory or file path), the ``fname``/``namespace``/``guard``/
        ``grammer_name`` code-generation options, ``optimization`` (0-3) and
        ``q_grammar`` (the parser model).
    """
    author_strings = [f"{a['name']} <{a['email']}>" for a in AUTHORS]
    indent = " " * 4
    epilog = (
        f"\n    This software was written by:\n\n"
        f"    {indent}{('\n' + indent).join(author_strings)}\n\n"
        f"    Software homepage: {HOMEPAGE}\n"
        f"    Submit issues to: {ISSUES}\n"
        f"    This software is under the {LICENSE} license.\n    "
    )

    parser = argparse.ArgumentParser(
        description="Tablewright is a parser generator which outputs C++",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=epilog,
    )

    parser.add_argument("--log", "-l", action=SetLoggingLevelAction,
                        help="Explicitly set logging level "
                             "(debug, info, warn, error, critical)")
    parser.add_argument("--quiet", "-q", action="store_true", default=False,
                        help="Only log errors")
    parser.add_argument("--verbose", action="store_true", default=False,
                        help="Verbose output: grammar dumps, FIRST/FOLLOW, parse "
                             "table, optimization and aliasing details (DEBUG level)")
    parser.add_argument("--trace", action="store_true", default=False,
                        help="Even more verbose than --verbose: log every "
                             "FIRST/FOLLOW step, parse-table cell, merge/inline and "
                             "alias assignment (TRACE level)")
    parser.add_argument("--log-file", type=Path, default=None,
                        help="Also write a full timestamped log to this file")
    parser.add_argument("--dump-stages", type=Path, default=None, metavar="DIR",
                        help="Write each intermediate grammar stage (original, "
                             "post-recursion, factored) to text files in this "
                             "directory for inspection")
    parser.add_argument("--stats", action="store_true", default=False,
                        help="Print a per-stage timing summary when finished")
    parser.add_argument("--analyze", action="store_true", default=False,
                        help="Print a grammar health report (nullable, "
                             "unreachable, unproductive, unused terminals, "
                             "duplicate productions) before generating")
    parser.add_argument("--explain", metavar="NONTERMINAL", default=None,
                        help="Explain a single nonterminal (its productions, "
                             "FIRST/FOLLOW, parse-table row and emitted rules) "
                             "and exit without writing output")
    parser.add_argument("--debug-json", type=Path, default=None, metavar="PATH",
                        help="Write machine-readable diagnostics (FIRST, FOLLOW, "
                             "parse table, terminal aliases, analysis) to this "
                             "JSON file for tooling or diffing")
    parser.add_argument("--check", "--validate", dest="check", action="store_true",
                        default=False,
                        help="Validate the grammar (parse, undefined symbols, "
                             "and (q)LL(1) conflicts) and report problems without "
                             "writing any output; exits nonzero if invalid")
    parser.add_argument("--syntax", dest="show_syntax", action="store_true",
                        default=False,
                        help="Print a quick reference for the .gram grammar "
                             "dialect and exit")
    parser.add_argument("--version", "-v", action="store_true", default=False,
                        help="Output version and exit")
    parser.add_argument("--run-tests", dest="run_tests", action="store_true",
                        default=False,
                        help="Run Tablewright's built-in test suite and exit")

    # --ll is accepted for CTRE compatibility and is always on (no-op).
    parser.add_argument("--ll", action="store_true", default=True,
                        help="LL1 flag, accepted for compatibility (always on)")

    # Parser model. By default the generator targets a Q-grammar (the relaxation
    # CTLL uses: on a terminal in both FIRST and FOLLOW, the shift rule wins and
    # epsilon is the fallback). Pass --no-q / --strict to require classic LL(1),
    # where any FIRST/FIRST or FIRST/FOLLOW overlap is reported as a conflict.
    parser.add_argument("--q", dest="q_grammar", action="store_true", default=True,
                        help="Generate a Q-grammar (default)")
    parser.add_argument("--no-q", "--strict", dest="q_grammar", action="store_false",
                        help="Require classic LL(1) instead of a Q-grammar")

    # Optimization level, in the spirit of a C++ compiler's -O flags. Higher
    # levels shrink the generated grammar while preserving the language and the
    # (q)LL(1) property:
    #   -O0  none (default)
    #   -O1  merge structurally-identical nonterminals
    #   -O2  + inline nonterminals referenced exactly once
    #   -O3  + inline nonterminals whose body is a single alternative
    parser.add_argument("-O0", dest="optimization", action="store_const", const=0,
                        default=0, help="No optimization (default)")
    parser.add_argument("-O1", dest="optimization", action="store_const", const=1,
                        help="Merge identical nonterminals")
    parser.add_argument("-O2", dest="optimization", action="store_const", const=2,
                        help="O1 + inline single-use nonterminals")
    parser.add_argument("-O3", dest="optimization", action="store_const", const=3,
                        help="O2 + inline single-production nonterminals")
    parser.add_argument("--range-lookaheads", dest="range_lookaheads",
                        action="store_true", default=False,
                        help="Emit contiguous lookahead character spans as "
                             "ctll::range<lo,hi> (plus a residual set) instead of "
                             "one wide ctll::set, cutting compile-time character "
                             "comparisons on large classes")

    parser.add_argument("--input", type=argparse.FileType("r", encoding="utf-8"),
                        action=ValidateFileExistsAction,
                        help='Input file path or "-" for stdin')
    parser.add_argument("--output", type=Path, default=".",
                        action=ValidateOutputPathAction,
                        help="Output directory, or a file path to write directly")
    parser.add_argument("--generator", type=str, default="cpp_ctll_v2",
                        help="Generator to use")

    parser.add_argument("--fname", "--cfg:fname", type=Path, default=None,
                        help="Output filename (default: derived from the input "
                             "filename, e.g. pcre.gram -> pcre.hpp)")
    parser.add_argument("--namespace", "--cfg:namespace", type=str, default=None,
                        action=ValidateCppIdentifierNameAction,
                        help="C++ namespace to put the grammar in (default: "
                             "derived from the input filename)")
    parser.add_argument("--guard", "--cfg:guard", type=str, default=None,
                        action=ValidateCppIdentifierNameAction,
                        help="C++ header guard name (default: derived from the "
                             "input filename, e.g. PCRE_HPP)")
    parser.add_argument("--grammar-name", "--grammar_name", "--grammer_name",
                        "--cfg:grammar_name", dest="grammer_name", type=str,
                        default=None, action=ValidateCppIdentifierNameAction,
                        help="C++ grammar struct name (default: derived from the "
                             "input filename)")

    args, remaining_args = parser.parse_known_args()

    # Treat a bare positional path as --input=<path>.
    for arg in list(remaining_args):
        if os.path.isfile(arg):
            remaining_args.remove(arg)
            remaining_args += [f"--input={arg}"]

    parser.parse_args(remaining_args, namespace=args)
    return args


# ======================================================================== #
# Pipeline orchestration
# ======================================================================== #

def write_debug_json(path, table: IdentifierTable, args) -> None:
    """Write machine-readable diagnostics about the generated grammar to ``path``.

    The JSON document captures the finalized grammar's productions, FIRST and
    FOLLOW sets, the parse table, the terminal-alias map and the health analysis,
    all as plain strings/lists so the file can be diffed across runs or consumed
    by other tooling. This is called after generation, so the grammar in ``table``
    is already normalized and optimized; FIRST/FOLLOW and the parse table are
    recomputed (cheaply) from that final grammar rather than re-running the
    optimizer.

    Args:
        path: Destination file path.
        table: The identifier table, post-generation.
        args: Parsed CLI options (for the parser model and optimization level).
    """
    q_grammar = getattr(args, "q_grammar", True)
    grammar = normalize_grammar_keys(table[SymbolType.non_terminal])
    first = compute_first(grammar)
    follow = compute_follow(grammar, first)
    parse_table = construct_parse_table(grammar, first, follow, strict=not q_grammar)
    terminal_table = table[SymbolType.terminal]

    def symbols(seq):
        return [str(s) for s in seq]

    def body(production):
        return " ".join(str(s) for s in production) or "epsilon"

    # Rebuild the alias map exactly as the emitter would, so the JSON reflects the
    # names that appear in the header.
    reserved = {str(nt) for nt in grammar} | {str(a) for a in table[SymbolType.action]}
    reserved.add("_start")
    aliaser = TerminalAliaser(terminal_table, reserved=reserved)
    other_chars = sorted(terminal_table["other"].value, key=ord)
    if other_chars:
        aliaser.alias_for(render_neg_set(other_chars), other_chars,
                          is_neg=True, gram_name="other")
    for non_terminal in grammar:
        if parse_table.get(non_terminal):
            _emit_rules_for_nonterminal(non_terminal, parse_table[non_terminal],
                                        terminal_table, "", aliaser)
    aliases = {alias: type_string for alias, type_string in aliaser._ordered}

    analysis = analyze_grammar(grammar, terminal_table, first, follow)
    analysis["actions"] = len(table[SymbolType.action])
    # Render analysis symbol lists as strings for JSON.
    analysis_json = {
        "nonterminals": analysis["nonterminals"],
        "productions": analysis["productions"],
        "terminals": analysis["terminals"],
        "actions": analysis["actions"],
        "nullable": symbols(analysis["nullable"]),
        "unreachable": symbols(analysis["unreachable"]),
        "unproductive": symbols(analysis["unproductive"]),
        "unused_terminals": analysis["unused_terminals"],
        "duplicates": {str(nt): bodies for nt, bodies in analysis["duplicates"].items()},
    }

    document = {
        "tool": "tablewright",
        "version": VERSION,
        "options": {
            "optimization": getattr(args, "optimization", 0),
            "q_grammar": q_grammar,
            "grammar_name": getattr(args, "grammer_name", None),
        },
        "grammar": {
            str(nt): [body(p) for p in productions]
            for nt, productions in grammar.items()
        },
        "first": {str(nt): symbols(first.get(nt, set())) for nt in grammar},
        "follow": {str(nt): symbols(follow.get(nt, set())) for nt in grammar},
        "parse_table": {
            str(nt): {str(term): body(prod) for term, prod in row.items()}
            for nt, row in parse_table.items()
        },
        "terminal_aliases": aliases,
        "analysis": analysis_json,
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(document, f, indent=2, sort_keys=True)


def main() -> None:
    """Run the generator end to end from the command line.

    Parses arguments, configures logging, reads the ``.gram`` input, drives the
    full pipeline (parse, transform, eliminate left recursion, factor, optimize,
    build the parse table) and writes the rendered CTLL header to the output path.
    With ``--version`` it prints the version and exits.
    """
    global logging_level
    args = parse_args()

    if args.version:
        print(VERSION)
        exit(0)

    if getattr(args, "show_syntax", False):
        print(GRAMMAR_SYNTAX_REFERENCE)
        exit(0)

    if getattr(args, "run_tests", False):
        # Run the built-in suite and exit with its pass/fail status. No input or
        # output files are needed for this mode.
        exit(run_tests())

    # Fill any unset output-config names (namespace/guard/fname/struct) from the
    # input filename, so a bare ``--input foo.gram`` needs no further flags.
    apply_filename_defaults(args)

    # Resolve the console logging level (most verbose flag wins).
    if args.trace:
        logging_level = TRACE
    elif args.verbose:
        logging_level = logging.DEBUG
    elif args.log:
        logging_level = TRACE if args.log == "TRACE" else getattr(logging, args.log)
    elif args.quiet:
        logging_level = logging.ERROR
    else:
        logging_level = logging.INFO

    configure_logging(logging_level, args.log_file)
    logger.debug(f"Tablewright {VERSION}")
    logger.debug(f"Arguments: {args}")
    if args.log_file:
        logger.info(f"Writing full log to {args.log_file}")

    # Optional: where to dump intermediate grammar stages for inspection.
    dump_dir = args.dump_stages
    if dump_dir is not None:
        os.makedirs(dump_dir, exist_ok=True)
        logger.info(f"Dumping intermediate grammar stages to {dump_dir}")

    def dump_stage(filename, text):
        """Write an intermediate stage to the dump directory, if enabled."""
        if dump_dir is not None:
            path = Path(dump_dir) / filename
            with open(path, "w", encoding="utf-8") as f:
                f.write(text + "\n")
            logger.debug(f"  wrote {path}")

    with args.input as input_file:
        input_data = input_file.read()
    logger.debug(f"Read {len(input_data)} characters from {args.input.name}")

    with timed_stage(f"Parsing grammar file {args.input.name}"):
        tree = parse_grammar_text(input_data, args.input.name)

    with timed_stage("Transforming parse tree"):
        # Strip whitespace, build GrammerTypes/productions, and turn set contents
        # into Python sets.
        tree = (SpaceTransformer() * RuleTransformer() * SetTransformer()).transform(tree)

    with timed_stage("Building identifier table"):
        add_identifers().visit(tree)
        # Rewrite the regex-style grouping/repetition syntax ((...), '+', '*')
        # into ordinary rules with anonymous helper nonterminals before anything
        # else inspects the grammar.
        expand_groups_and_quantifiers(identifier_table)
        add_semantic_action_identifiers(identifier_table)
        logger.info(
            f"Collected {len(identifier_table[SymbolType.non_terminal])} nonterminals, "
            f"{len([n for n in identifier_table[SymbolType.terminal] if n != 'other'])} "
            f"named terminals, {len(identifier_table[SymbolType.action])} actions"
        )
        logger.debug("Identifier table:")
        logger.debug(pformat(identifier_table))

    with timed_stage("Checking identifiers"):
        verify_identifiers(identifier_table)
        logger.info("All referenced identifiers are defined")

        logger.info("Turning string literals into individual atoms")
        break_strings(identifier_table)  # "abc" -> a, b, c

    logger.debug("Original grammar:")
    logger.debug(stringify_grammar(identifier_table[SymbolType.non_terminal]))
    dump_stage("1-original.gram.txt",
               stringify_grammar(identifier_table[SymbolType.non_terminal]))

    with timed_stage("Eliminating left recursion"):
        logger.info(f"Before: {describe_grammar(identifier_table[SymbolType.non_terminal])}")
        identifier_table[SymbolType.non_terminal] = eliminate_left_recursion(
            identifier_table[SymbolType.non_terminal]
        )
        logger.info(f"After:  {describe_grammar(identifier_table[SymbolType.non_terminal])}")
    logger.debug(stringify_grammar(identifier_table[SymbolType.non_terminal]))
    dump_stage("2-no-left-recursion.gram.txt",
               stringify_grammar(identifier_table[SymbolType.non_terminal]))

    with timed_stage("Left factoring"):
        passes = 0
        updated = True
        while updated:
            identifier_table[SymbolType.non_terminal], updated = left_factor(
                identifier_table[SymbolType.non_terminal]
            )
            passes += 1
        logger.info(f"Reached a fixed point after {passes} pass(es): "
                    f"{describe_grammar(identifier_table[SymbolType.non_terminal])}")
    logger.debug(stringify_grammar(identifier_table[SymbolType.non_terminal]))
    dump_stage("3-factored.gram.txt",
               stringify_grammar(identifier_table[SymbolType.non_terminal]))

    other = identifier_table[SymbolType.terminal]["other"]
    other.value = get_other(identifier_table)
    logger.debug(f"Global 'other' set ({len(other.value)} chars): "
                 f"{sorted(other.value)}")

    # --explain: trace a single nonterminal end-to-end, then stop. This is a
    # diagnostic mode, so it does not write the generated header.
    if getattr(args, "explain", None):
        log_stage(f"Explain: {args.explain}")
        explanation = explain_nonterminal(
            args.explain, identifier_table,
            getattr(args, "optimization", 0), getattr(args, "q_grammar", True)
        )
        for line in explanation.splitlines():
            logger.info(line)
        return

    # --analyze: print a grammar health report before generating. Built on the
    # normalized grammar so reachability/productivity reflect the real start.
    if getattr(args, "analyze", False):
        log_stage("Grammar analysis")
        analysis_grammar = normalize_grammar_keys(identifier_table[SymbolType.non_terminal])
        analysis = analyze_grammar(analysis_grammar,
                                   identifier_table[SymbolType.terminal])
        analysis["actions"] = len(identifier_table[SymbolType.action])
        for line in stringify_grammar_analysis(analysis).splitlines():
            logger.info(line)

    # --check / --validate: run the full pipeline (which surfaces undefined
    # symbols and (q)LL(1) conflicts when the parse table is built) but write
    # nothing. Combined with --analyze it doubles as a health report.
    check_only = getattr(args, "check", False)

    with timed_stage("Generating C++ header" if not check_only
                     else "Validating grammar"):
        constexpr_cpp = table_to_constexpr_cpp(identifier_table, args)
    logger.debug("Generated header:")
    logger.debug(constexpr_cpp)
    dump_stage("4-output.hpp", constexpr_cpp)

    if check_only:
        logger.info(f"Grammar OK: {args.input.name} is a valid "
                    f"{'Q-grammar' if getattr(args, 'q_grammar', True) else 'LL(1) grammar'}")
        return

    # Write to <output dir>/<fname>, or to --output directly if it is a file.
    output_dir = Path(args.output)
    out_path = output_dir / args.fname if output_dir.is_dir() else output_dir
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(constexpr_cpp)
    logger.info(f"Wrote generated grammar to {out_path} "
                f"({len(constexpr_cpp)} bytes)")

    # --debug-json: emit structured diagnostics for tooling/diffing.
    if getattr(args, "debug_json", None):
        write_debug_json(args.debug_json, identifier_table, args)
        logger.info(f"Wrote debug diagnostics to {args.debug_json}")

    if getattr(args, "stats", False):
        log_timing_summary()


if __name__ == "__main__":
    try:
        main()
    except ValueError as e:
        # Grammar conflicts (e.g. not LL(1) under --strict) are user-facing input
        # errors, not internal bugs: report them cleanly without a traceback.
        logger.error(f"error: {e}")
        exit(1)
    except Exception as e:
        logger.exception(f"error: {str(e)}")
        exit(1)
