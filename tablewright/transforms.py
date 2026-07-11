"""Language-preserving grammar rewrites: left-recursion elimination, left
factoring, terminal inlining and the -O1..-O3 optimization passes."""

from collections import defaultdict

from .analysis import compute_first, compute_follow, construct_parse_table
from .logutil import logger, trace
from .symbols import (describe_grammar, EPSILON, Grammar, GrammerType, HashableList,
    OrderedSet, SymbolType)



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
    start = next(iter(grammar), None)
    pure = {
        nt: [p[0] for p in productions]
        for nt, productions in grammar.items()
        if nt != start and productions
        and all(len(p) == 1 and p[0].is_terminal() for p in productions)
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
