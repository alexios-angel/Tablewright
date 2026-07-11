import argparse
import json
import logging
import re

from io import TextIOBase
from pathlib import Path
from pprint import pformat
from typing import Optional, Sequence

from .analysis import (analyze_grammar, compute_first, compute_follow, construct_parse_table,
    normalize_grammar_keys, stringify_grammar, stringify_grammar_analysis)
from .codegen import (_emit_rules_for_nonterminal, explain_nonterminal, render_neg_set,
    table_to_constexpr_cpp, TerminalAliaser)
from .frontends import convert_to_eds
from .gramparse import (add_identifers, add_semantic_action_identifiers, break_strings,
    expand_groups_and_quantifiers, get_other, GRAMMAR_SYNTAX_REFERENCE,
    identifier_table, parse_grammar_text, RuleTransformer, SetTransformer,
    SpaceTransformer, verify_identifiers)
from .logutil import (configure_logging, log_stage, log_timing_summary, logger, timed_stage,
    TRACE)
from .symbols import describe_grammar, GrammerType, IdentifierTable, SymbolType
from .transforms import eliminate_left_recursion, left_factor
from .version import AUTHORS, HOMEPAGE, ISSUES, LICENSE, VERSION

# ======================================================================== #
# Command-line interface
# ======================================================================== #

def is_accessible(path: str | Path) -> bool:
    """Return True if ``path`` is readable by the current process."""
    try:
        with Path(path).open("rb"):
            return True
    except (OSError, ValueError):
        return False


def is_accessible_file(filepath: str | Path) -> bool:
    """Return True if ``filepath`` exists, is a regular file, and is readable."""
    path = Path(filepath)
    return path.is_file() and is_accessible(path)


def is_accessible_dir(dirpath: str | Path) -> bool:
    """Return True if ``dirpath`` exists, is a directory, and is readable."""
    path = Path(dirpath)
    try:
        next(path.iterdir(), None)
        return path.is_dir()
    except (OSError, ValueError):
        return False


class ValidateFileExistsAction(argparse.Action):
    """argparse action accepting ``-`` (stdin) or an existing, readable file."""

    def __call__(self, parser, namespace, arg: TextIOBase, option_string=None):
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


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
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
    parser.add_argument("--lang", choices=("eds", "ebnf", "lark", "w3c"),
                        default="eds",
                        help="Input grammar language (default: eds); w3c is "
                             "the XML-specification EBNF notation")
    parser.add_argument("--emit-eds", dest="emit_eds", type=str, default=None,
                        metavar="PATH",
                        help="Write the normalized EDS intermediate grammar to "
                             "PATH ('-' for stdout). With --lang=lark/ebnf this "
                             "is the converted grammar; combine with --check to "
                             "convert without generating C++")

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

    args, remaining_args = parser.parse_known_args(argv)

    # Treat a bare positional path as --input=<path>.
    for arg in list(remaining_args):
        if Path(arg).is_file():
            remaining_args.remove(arg)
            remaining_args += [f"--input={arg}"]

    parser.parse_args(remaining_args, namespace=args)
    standalone_mode = args.version or args.show_syntax or args.run_tests
    if args.input is None and not standalone_mode:
        parser.error("the following argument is required: --input")
    return args


def _resolve_logging_level(args: argparse.Namespace) -> int:
    """Return the effective console logging level for parsed CLI options."""
    if args.trace:
        return TRACE
    if args.verbose:
        return logging.DEBUG
    if args.log:
        return TRACE if args.log == "TRACE" else getattr(logging, args.log)
    if args.quiet:
        return logging.ERROR
    return logging.INFO


def _write_text(path: Path, content: str) -> None:
    """Write UTF-8 text using :class:`pathlib.Path` consistently."""
    path.write_text(content, encoding="utf-8")


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
    Path(path).write_text(
        json.dumps(document, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Run the generator end to end from the command line.

    Parses arguments, configures logging, reads the ``.gram`` input, drives the
    full pipeline (parse, transform, eliminate left recursion, factor, optimize,
    build the parse table) and writes the rendered CTLL header to the output path.
    With ``--version`` it prints the version and exits.
    """
    args = parse_args(argv)

    if args.version:
        print(VERSION)
        return 0

    if getattr(args, "show_syntax", False):
        print(GRAMMAR_SYNTAX_REFERENCE)
        return 0

    if getattr(args, "run_tests", False):
        # Run the built-in suite and exit with its pass/fail status. No input or
        # output files are needed for this mode.
        from .tests import run_tests  # deferred: tests import the CLI
        return run_tests()

    # Fill any unset output-config names (namespace/guard/fname/struct) from the
    # input filename, so a bare ``--input foo.gram`` needs no further flags.
    apply_filename_defaults(args)

    # Resolve the console logging level (most verbose flag wins).
    logging_level = _resolve_logging_level(args)
    configure_logging(logging_level, args.log_file)
    logger.debug(f"Tablewright {VERSION}")
    logger.debug(f"Arguments: {args}")
    if args.log_file:
        logger.info(f"Writing full log to {args.log_file}")

    # Optional: where to dump intermediate grammar stages for inspection.
    dump_dir = args.dump_stages
    if dump_dir is not None:
        dump_dir.mkdir(parents=True, exist_ok=True)
        logger.info(f"Dumping intermediate grammar stages to {dump_dir}")

    def dump_stage(filename, text):
        """Write an intermediate stage to the dump directory, if enabled."""
        if dump_dir is not None:
            path = Path(dump_dir) / filename
            _write_text(path, text + "\n")
            logger.debug(f"  wrote {path}")

    # Start from a fresh identifier table: main() may be invoked more than
    # once in one process (the built-in tests, library use).
    identifier_table[SymbolType.action] = set()
    identifier_table[SymbolType.non_terminal] = {}
    identifier_table[SymbolType.terminal] = {
        "other": GrammerType([], SymbolType.negitive_set)
    }

    with args.input as input_file:
        input_data = input_file.read()
    logger.debug(f"Read {len(input_data)} characters from {args.input.name}")

    with timed_stage(f"Reading {args.lang.upper()} frontend", banner=args.lang != "eds"):
        input_data = convert_to_eds(input_data, args.lang)
    if args.lang != "eds":
        logger.debug("Normalized EDS grammar:\n%s", input_data)

    # --emit-eds: write the (converted) EDS grammar itself, before the
    # pipeline consumes it, so the intermediate is inspectable even when a
    # later stage rejects the grammar.
    if getattr(args, "emit_eds", None):
        provenance = (f"# Normalized EDS grammar written by Tablewright {VERSION}\n"
                      f"# from {args.input.name} (--lang={args.lang})\n\n")
        eds_payload = provenance + input_data.rstrip("\n") + "\n"
        if args.emit_eds == "-":
            print(eds_payload, end="")
        else:
            _write_text(Path(args.emit_eds), eds_payload)
            logger.info(f"Wrote EDS intermediate to {args.emit_eds} "
                        f"({len(eds_payload)} bytes)")

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
        return 0

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
        return 0

    # Write to <output dir>/<fname>, or to --output directly if it is a file.
    output_dir = Path(args.output)
    out_path = output_dir / args.fname if output_dir.is_dir() else output_dir
    _write_text(out_path, constexpr_cpp)
    logger.info(f"Wrote generated grammar to {out_path} "
                f"({len(constexpr_cpp)} bytes)")

    # --debug-json: emit structured diagnostics for tooling/diffing.
    if getattr(args, "debug_json", None):
        write_debug_json(args.debug_json, identifier_table, args)
        logger.info(f"Wrote debug diagnostics to {args.debug_json}")

    if getattr(args, "stats", False):
        log_timing_summary()

    return 0


def main_cli() -> None:
    """Console-script entry point that converts failures to process exit codes."""
    try:
        status = main()
    except ValueError as error:
        logger.error("error: %s", error)
        status = 1
    except Exception:
        logger.exception("unexpected error")
        status = 1
    raise SystemExit(status)
