"""The library-level pipeline: grammar text in, identifier table or
rendered C++ header out (the same sequence the CLI drives)."""

import argparse

from lark import Lark

from .codegen import table_to_constexpr_cpp
from .frontends import convert_to_eds
from .gramparse import (add_identifers, add_semantic_action_identifiers, break_strings,
    expand_groups_and_quantifiers, get_other, grammar, identifier_table,
    RuleTransformer, SetTransformer, SpaceTransformer, verify_identifiers)
from .symbols import GrammerType, IdentifierTable, SymbolType
from .transforms import eliminate_left_recursion, left_factor



def _build_identifier_table(gram_text: str, language: str = "eds") -> IdentifierTable:
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
    gram_text = convert_to_eds(gram_text, language)
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
                  guard: str = "G_H", grammar_name: str = "g",
                  language: str = "eds") -> str:
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
    table = _build_identifier_table(gram_text, language)
    args = argparse.Namespace(
        optimization=optimization, q_grammar=q_grammar,
        namespace=namespace, guard=guard, grammer_name=grammar_name,
    )
    return table_to_constexpr_cpp(table, args)
