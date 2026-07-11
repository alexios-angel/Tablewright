"""Read-only grammar mathematics: stringification, reachability and
productivity, health analysis, FIRST/FOLLOW and the (q)LL(1) parse table."""

from collections import defaultdict
from typing import Dict, Optional

from .logutil import logger, trace
from .symbols import EPSILON, Grammar, GrammerType, SymbolType



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
