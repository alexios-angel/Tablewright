# Tablewright

Tablewright is a `(q)LL(1)` parser-generator that turns a compact, human-readable
grammar into the C++ header that drives **CTLL**, the compile-time LL parser at
the heart of Hana Dusíková's
[Compile-Time Regular Expressions](https://github.com/hanickadot/compile-time-regular-expressions)
(CTRE) library.

It is an independent, open-source re-implementation of Hana's closed-source
**Desatomat** tool and its Extended Desátomatu syntax, built by
reverse-engineering the input/output format from CTRE's published headers. It
aims to be a drop-in replacement: it covers the features CTRE's own grammar uses
and produces the same shape of `rule(...)` table CTLL consumes.

> **Attribution.** The grammar format and the original Desatomat tool are Hana
> Dusíková's work. Desatomat is closed source and runs publicly at
> [desatomat.cz](https://www.desatomat.cz/?lang=desatomat&langui=en); see
> [CTRE issue #37](https://github.com/hanickadot/compile-time-regular-expressions/issues/37).
> Tablewright is not derived from Hana's source and is not affiliated with or
> endorsed by her — any differences from the original Desatomat are mine.

## What it does

Given a grammar in the `.gram` dialect, Tablewright emits a C++ header full of
overloaded `rule` functions. CTLL runs a pushdown automaton entirely at compile
time, selecting productions by overload resolution over rules shaped like:

```cpp
static constexpr auto rule(State, ctll::term<'c'>) -> ctll::push<...>;
```

The pipeline parses the grammar, collects terminals/nonterminals/actions,
verifies every reference is defined, expands string literals into atoms,
eliminates left recursion, left-factors to a fixed point, computes
FIRST/FOLLOW and the `(q)LL(1)` parse table, and renders the CTLL header.

## Setup

You need Python 3 and the [lark](https://github.com/lark-parser/lark) parser.

```bash
git clone https://github.com/alexios-angel/tablewright.git
cd tablewright
pip3 install -e .        # installs the `tablewright` (and `desatomat`) command
# or, without installing: pip3 install lark && python3 -m tablewright ...
```

## Usage

In the common case, only the input and output are needed. The C++ namespace,
header guard, output filename, and grammar-struct name are all derived from the
input filename (`pcre.gram` → namespace `pcre`, guard `PCRE_HPP`, file
`pcre.hpp`, struct `pcre`):

```bash
tablewright --input=pcre.gram --output=include/
```

Print the generated header straight to stdout:

```bash
tablewright --input=pcre.gram --output=/dev/stdout -q
```

Override any of the derived names and turn on optimization:

```bash
tablewright --input=pcre.gram --output=include/ \
    --namespace=ctre --guard=CTRE__PCRE__HPP --grammar-name=pcre -O3
```

Validate a grammar without writing anything (handy in CI or while iterating);
it exits nonzero if the grammar is invalid:

```bash
tablewright --check --input=pcre.gram
```

### EBNF, W3C EBNF and Lark input

Use `--lang` to select the input frontend. The existing Extended Desatomat
syntax remains the default (`eds`):

```bash
tablewright --lang=ebnf --input=grammar.ebnf --output=include/
tablewright --lang=w3c  --input=grammar.txt  --output=include/
tablewright --lang=lark --input=grammar.lark --output=include/

# inspect (or keep) the EDS intermediate; --check skips the C++ output
tablewright --lang=lark --input=grammar.lark --emit-eds=grammar.gram --check
```

Two EBNF dialects are supported, sharing one transformer and the same
lowering path as every other frontend (**EBNF → EDS → C++**, `--emit-eds`
included):

* `--lang=ebnf` — ISO/IEC 14977 style: `name = expression ;` rules (`::=`
  and `:` assignment and a `.` terminator are accepted as common
  variants), `,` concatenation, `|` alternation, `[x]` optional, `{x}`
  repetition, `(x)` grouping, `n * x` repetition factors, the `x - y`
  exception, quoted literals, `(* comments *)` and `epsilon`/`empty`;
  postfix `? * +` remain as extensions. A special sequence written
  `?name?` (no inner spaces) is a **semantic action**, EDS's `[name]` —
  ISO's own escape hatch for implementation-defined content, put to work.
* `--lang=w3c` — the W3C notation used by the XML specification:
  terminator-less `Name ::= expression` rules (parsed with Earley — where
  one rule ends is only decidable from the following `::=`),
  juxtaposition for sequence, character classes `[a-z#xB7]` / `[^...]`,
  `#xNN` code-point references, postfix `? * +`, the `A - B` exception,
  and both `/* */` and `(* *)` comments. W3C strings are literal — the
  dialect has no escape mechanism; use `#xNN` references. `?name?` (no
  inner spaces) is a semantic action here too; with a space after a
  quantifier (`Item? Other`) there is never any confusion between the
  two readings.

An exception `x - y` is translatable exactly when both operands denote
character sets — classes, single characters, or references to rules that
reduce to them (the XML spec's `Char - '-'` idiom works); the set
difference is computed during conversion, and anything else is rejected
with an explanation rather than mistranslated.

The Lark frontend parses complete grammar documents with Tablewright's
own **derived Lark grammar** (Lark parsing Lark): a vendored derivative of
the official `lark.lark` specification, extended with a second layer that
spells out the *regex* language as grammar rules. At the document level a
regex stays one token — its extent is lexical, and `%ignore` must never
reach inside a pattern — and the token's body is then parsed with the
regex layer (Earley, every character significant) into the same AST the
rest of the frontend lowers. The frontend accepts parser rules with quoted
literals (the case-insensitive `"..."i` form included), `".."` literal
ranges, grouping, alternation, multiline alternatives, aliases,
priorities, rule modifiers (`?rule`, `!rule`), `?`/`*`/`+` and counted
`~ n` / `~ n..m` repetition. As a Tablewright extension, `@name` anywhere
in an expansion is a **semantic action** — the counterpart of EDS's
`[name]`, positional in the rule and pushed to CTLL's stack at that spot
(`pair: key ":" @push_pair value`). Official Lark tools do not know `@`,
so keep actions out of grammars you share with them.

The whole grammar is lowered to the EDS intermediate (write it out with
`--emit-eds`) and from there compiled to C++ like any native grammar:
**lark → EDS → C++**. The regex layer understands the language-defining
core of the Python/Lark regex dialect: literals, `.`, character classes (ranges,
negation, `\d \w \s`), `\xNN`/`\uNNNN`/`\UNNNNNNNN` escapes, groups
(plain, `(?:...)`, `(?P<name>...)`), alternation, `? * +` and
`{n}`/`{n,}`/`{n,m}` repetition, and the `i`, `s` and `x` flags. Constructs
that select match *positions* rather than characters — anchors, word
boundaries, lookarounds, backreferences, possessive quantifiers — cannot
be expressed by a grammar rule and are rejected with a pointed error.

A terminal whose language is one character out of a set (`DIGIT: /[0-9]/`,
`SIGN: "+" | "-"`) stays a terminal — an EDS set. Any multi-character
terminal (`WORD: /[a-z]+/`, `ARROW: "->"`, `INT: DIGIT+`) is lowered into
EDS *rules* and recognized character by character by the grammar itself.
Mind the semantics: CTLL has no separate lexer, so there is no
longest-match tokenization — where a real lexer would disambiguate
overlapping tokens, the grammar must be `(q)LL(1)` at the character level,
and any conflict surfaces when the parse table is built. `%import` and
`%declare` are parsed as Lark syntax; `%ignore` cannot be honored (there
is no token stream to filter) and says so with a warning — weave optional
whitespace into the rules instead.

## The `.gram` grammar dialect

A grammar is a sequence of **terminal (set) definitions** and **rules**. Run
`tablewright --syntax` for this reference at any time.

### Terminal sets

```
name = {a, b, c}        a positive set: matches a, b or c
name = a, b, c          braces are optional
name : a, b, c          ':' works the same as '='
name = sigma - {a, b}   a negative set: matches any character except a, b
```

### Rules

```
A -> <B>, x, [act] | epsilon
A : <B>, x, [act] | epsilon      ':' works the same as '->'
```

| Token       | Meaning                                                          |
| ----------- | ---------------------------------------------------------------- |
| `<B>`       | reference to nonterminal `B` (nonterminal names must be 2+ chars) |
| `x`         | a single literal character (an *atom*)                           |
| `"abc"`     | a string literal (expands to atoms `a`, `b`, `c`)                |
| `[[a-z]]`   | a regex-style range (expands to a positive set; no negation)     |
| `[act]`     | a semantic action named `act`                                    |
| &#124;      | the vertical bar; separates alternatives                         |
| `,`         | separates the symbols of one alternative                         |
| `epsilon`   | (or `@`) the empty production                                    |

A `#` starts a comment to end of line.

Because `:` can introduce either a set or a rule, a bare `name : a, b, c`
(only atoms) is read as a **set**. To write a rule with `:`, give it a
rule-shaped body — a `<nonterminal>`, a `"string"`, a `[[range]]`, or a `|`
alternation — or simply use `->`.

### Example

```
digit  = {0,1,2,3,4,5,6,7,8,9}
number -> digit, <number_tail>
number_tail -> digit, <number_tail> | epsilon
```

## Parser model

By default Tablewright produces a **Q-grammar**, matching the original
Desatomat's `--q` mode. Pass `--no-q` (or `--strict`) to require a classic
`LL(1)` grammar instead, which rejects grammars with FIRST/FIRST or
FIRST/FOLLOW conflicts. The `--ll` flag is accepted for compatibility but is a
no-op (it is the default).

## Optimization

The `-O0`..`-O3` flags trade generation effort for a smaller table, in the
spirit of a C++ compiler's optimization levels. Every level preserves the
recognized language and the chosen parser model.

| Level | Effect                                                       |
| ----- | ------------------------------------------------------------ |
| `-O0` | No optimization (default)                                    |
| `-O1` | Merge structurally identical nonterminals                    |
| `-O2` | `-O1` + inline single-use nonterminals                       |
| `-O3` | `-O2` + inline single-production nonterminals                |

Because each distinct `rule` overload adds to the set the compiler resolves per
input character, these state-reducing passes are the most effective lever on
compile time; on the bundled PCRE grammar, `-O3` compiles its parser noticeably
faster than `-O0`.

`--range-lookaheads` is a separate, opt-in transform: a wide positive lookahead
set is split into contiguous spans, each emitted as a `ctll::range<lo,hi>`
(two ordered comparisons) instead of a single wide `ctll::set` (one comparison
per member). It is language-preserving and verified against CTRE's full test
suite, and it sharply cuts the number of compile-time character comparisons.
Note the trade-off: it replaces one rule with several, enlarging the overload
set, and for the CTLL/GCC target that overload cost tends to outweigh the
comparison saving — so it is **off by default** and most interesting for other
back ends, very large alphabets, or compilers where set-membership cost
dominates.

## Inspecting and debugging a grammar

Tablewright has a number of tools for understanding what it does with a grammar:

| Option              | Description                                                                                               |
| ------------------- | -------------------------------------------------------------------------------------------------------- |
| `--check` `--validate` | Validate the grammar (parse, undefined symbols, `(q)LL(1)` conflicts) and report problems without writing output; exits nonzero if invalid |
| `--syntax`          | Print a quick reference for the `.gram` dialect and exit                                                  |
| `--analyze`         | Print a grammar health report (nullable, unreachable, unproductive, unused terminals, duplicate productions) before generating |
| `--explain NT`      | Explain a single nonterminal — its productions, FIRST/FOLLOW, parse-table row and emitted rules — and exit without writing output |
| `--dump-stages DIR` | Write each intermediate grammar stage (original, post-recursion, factored) to text files for inspection  |
| `--debug-json PATH` | Write machine-readable diagnostics (FIRST, FOLLOW, parse table, terminal aliases, analysis) to a JSON file for tooling or diffing |
| `--emit-eds PATH`   | Write the normalized EDS intermediate (the `--lang=lark`/`ebnf` conversion result) to a file, or `-` for stdout                  |
| `--stats`           | Print a per-stage timing summary when finished                                                           |

When a grammar is malformed, Tablewright reports the offending line and column
with a caret and a plain-language list of what it expected there, rather than
raw parser internals.

## All options

| Option                 | Description                                                                                                |
| ---------------------- | -------------------------------------------------------------------------------------------------------- |
| `--input`              | Input grammar file. Use `--input=-` to read from stdin                                                    |
| `--lang`               | Input syntax: `eds` (default), `ebnf` (ISO 14977), `w3c` (XML-spec EBNF), or `lark`                       |
| `--emit-eds PATH`      | Write the normalized EDS intermediate grammar to PATH (`-` for stdout); with `--lang=lark`/`ebnf` this is the converted grammar. Combine with `--check` to convert without generating C++ |
| `--output`             | Output directory, or a file path to write directly. Default directory is the current directory            |
| `--fname` `--cfg:fname`| Output filename (default: derived from the input filename, e.g. `pcre.gram` → `pcre.hpp`)                 |
| `--namespace` `--cfg:namespace` | C++ namespace to put the grammar in (default: derived from the input filename)                   |
| `--guard` `--cfg:guard`| C++ header guard, used as `#ifndef GUARD #define GUARD` (default: derived from the input filename)         |
| `--grammar-name` `--cfg:grammar_name` | C++ grammar struct name (default: derived from the input filename)                         |
| `--q`                  | Generate a Q-grammar (default)                                                                            |
| `--no-q` `--strict`    | Require classic `LL(1)` instead of a Q-grammar                                                            |
| `--ll`                 | Accepted for compatibility; ignored, as `LL(1)` output is the default. The original Desatomat uses this   |
| `-O0` … `-O3`          | Optimization level (see [Optimization](#optimization))                                                    |
| `--range-lookaheads`   | Emit contiguous lookahead spans as `ctll::range<lo,hi>` instead of one wide `ctll::set`                   |
| `--quiet` `-q`         | Only log errors                                                                                           |
| `--verbose`            | Verbose output: grammar dumps, FIRST/FOLLOW, parse table, optimization and aliasing details (DEBUG level) |
| `--trace`              | Even more verbose than `--verbose`: log every FIRST/FOLLOW step, parse-table cell, merge/inline and alias |
| `--log LEVEL` `-l`     | Explicitly set the log level (`debug`, `info`, `warn`, `error`, `critical`)                               |
| `--log-file PATH`      | Also write a full timestamped log to this file                                                            |
| `--run-tests`          | Run Tablewright's built-in test suite and exit                                                            |
| `--version` `-v`       | Print version and exit                                                                                    |

Beyond parity, Tablewright adds the `:` operator equivalences, regex-style
`[[a-z]]` ranges, optional set braces, optimization levels, the inspection and
validation tooling above, filename-derived defaults, and friendly error
reporting.

## Development

Tablewright ships with a built-in test suite (using only the Python standard
library). Run it with:

```bash
python3 -m tablewright --run-tests
```

The package splits along the pipeline's own seams:

| Module | Contents |
| ------ | -------- |
| `frontends.py`    | the Lark, ISO-EBNF and W3C-EBNF readers; `convert_to_eds` |
| `regex_engine.py` | Tablewright's Lark grammar for the regex dialect and its AST transformer |
| `eds.py`          | the EDS stringifier: escaping, `[[...]]` ranges, the emitter, name allocation |
| `gramparse.py`    | the native `.gram` dialect: grammar, parsing, identifier collection, verification |
| `transforms.py`   | left-recursion elimination, left factoring, the `-O1`..`-O3` passes |
| `analysis.py`     | FIRST/FOLLOW, the `(q)LL(1)` parse table, reachability and health analysis |
| `codegen.py`      | rendering the parse table into the CTLL C++ header |
| `pipeline.py`     | the library-level text-in/header-out entry points |
| `cli.py`          | argument parsing and the end-to-end `main` |
| `chartools.py`, `symbols.py`, `logutil.py`, `version.py` | character utilities, core data types, logging, constants |
| `tests.py`        | the built-in suite (`--run-tests`) |
