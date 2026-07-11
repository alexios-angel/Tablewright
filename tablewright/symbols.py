"""The core data types a grammar is made of: symbol kinds, GrammerType,
the ordered containers, and the pipeline type aliases."""

from collections import OrderedDict, UserList
from enum import auto, Enum
from typing import Dict



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
