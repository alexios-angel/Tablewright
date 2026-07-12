# Tablewright

A Python `(q)LL(1)` parser-**generator**: it compiles a compact grammar into the
C++ header of overloaded `rule(...)` functions that drives **CTLL**, the
compile-time LL parser in CTRE and in the `compile-time-*` sibling repos. It is
an independent, open-source re-implementation of Hana Dusíková's closed-source
**Desatomat** tool — NOT derived from her source (attribution: README top +
`tablewright/__init__.py` docstring; license: MIT in `LICENSE`, no NOTICE file).

## Build / run / test

```bash
pip3 install -e .                      # installs `tablewright` AND `desatomat` console scripts
python3 -m tablewright --run-tests     # the test suite (stdlib unittest in tests.py; nothing to pip-install)
python3 -m tablewright --input=x.gram --output=include/   # equivalent to the `tablewright` script
```

- Requires Python 3.10+; the only runtime dep is `lark>=1.1`.
- No pytest, no configured linter/formatter — `--run-tests` (i.e. `run_tests()` in
  `tests.py`) is the whole suite. Add tests there.
- Console entry point: `tablewright.cli:main_cli` (both `tablewright` and `desatomat`).

## CLI essentials

```bash
tablewright --input=pcre.gram --output=include/          # namespace/guard/fname/struct derived from filename
tablewright --input=g.gram --output=/dev/stdout -q       # print header to stdout
tablewright --check --input=g.gram                       # validate only; exits nonzero if invalid
tablewright --lang=lark --input=g.lark --emit-eds=- --check   # lower a frontend to EDS, inspect, don't gen C++
```

- `--lang`: `eds` (default), `ebnf`, `w3c`, `lark`, `antlr` — all frontends lower to EDS first.
- `--generator=cpp_ctll_v2` (default and only). `--input=-` reads stdin.
- Output naming overrides: `--cfg:fname` `--cfg:namespace` `--cfg:guard` `--cfg:grammar_name`
  (aliases `--fname` `--namespace` `--guard` `--grammar-name`).
- Parser model: `--q` Q-grammar (default), `--no-q`/`--strict` for classic LL(1); `--ll` is a no-op (always on).
- `-O0`..`-O3` (state-reducing, language-preserving); `--range-lookaheads` opt-in.
- Inspect/debug: `--syntax`, `--analyze`, `--explain NT`, `--dump-stages DIR`, `--debug-json PATH`, `--stats`.

## Pipeline & layout

Flow: `frontends → EDS text → gramparse → transforms → analysis → codegen`,
i.e. parse → collect terminals/nonterminals/actions → verify refs → expand
literals → eliminate left recursion → left-factor to fixpoint → FIRST/FOLLOW →
`(q)LL(1)` table → render CTLL header.

| File | Role |
| ---- | ---- |
| `cli.py` | arg parsing + end-to-end `main`/`main_cli` |
| `pipeline.py` | library entry (`_build_identifier_table`, `_generate_cpp`) |
| `frontends.py` | Lark/ISO-EBNF/W3C-EBNF/ANTLR readers; `convert_to_eds` |
| `regex_engine.py` | Lark grammar for the regex dialect + `parse_regex` |
| `eds.py` | EDS stringifier (escaping, `[[...]]` ranges, emitter) |
| `gramparse.py` | native `.gram` grammar, parsing, identifier collection/verification |
| `transforms.py` | left-recursion elimination, left factoring, `-O1`..`-O3` |
| `analysis.py` | FIRST/FOLLOW, `(q)LL(1)` table, reachability/health |
| `codegen.py` | render table → CTLL C++ header (`ctll::term/set/range/push`) |
| `chartools.py` `symbols.py` `logutil.py` `version.py` | char utils, core types, logging, constants |
| `tests.py` | the built-in suite |

Public API (re-exported in `__init__.py`): `parse_regex`, `convert_to_eds`,
`ebnf_to_eds`/`lark_to_eds`/`w3c_to_eds`, `main`/`main_cli`, `VERSION`.

## How it plugs into the compile-time-* build

Sibling repos (`../compile-time-lark`, `-json`, `-json5`, `-xml`, `-yaml`) call the
`tablewright` script from their Makefile `regrammar` target, e.g.:

```make
tablewright --ll --q --input=include/ctlark/lark.gram --output=include/ctlark/ \
  --generator=cpp_ctll_v2 --cfg:fname=lark.hpp --cfg:namespace=ctlark \
  --cfg:guard=CTLARK__LARK__HPP --cfg:grammar_name=lark_grammar
```

- The generated `.hpp` is a **build artifact**: edit the `.gram` and rerun
  `make regrammar` in the consumer repo — never hand-edit the generated header.
- Generated-header contract: a `rule(State, ctll::term<'c'>) -> ctll::push<...>`
  overload set consumed by CTLL. It must stay warning-clean under the consumers'
  `-Werror -Wextra -Wconversion` and compile under both their C++17 and C++20
  builds (the CNTTP split lives in the C++ repos, not here). There, "compiling the
  tests is the test."

## Conventions

- Work on `main` (no long-lived branches).
- Prefer ripgrep (`rg`) over `grep`/`find` for searching.
- Keep Desatomat attribution intact in README/`__init__.py`; do not claim derivation from Hana's source.
