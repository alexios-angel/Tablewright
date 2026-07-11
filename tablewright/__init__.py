"""Tablewright: a (q)LL(1) parser-generator targeting CTLL.

Tablewright turns a compact, human-readable grammar -- native EDS, ISO or
W3C EBNF, or Lark -- into the C++ header of ``rule(...)`` overloads that
drives CTLL, the compile-time LL(1) parser at the heart of Hana Dusikova's
Compile-Time Regular Expressions library. It is an independent, open-source
re-implementation of Hana's closed-source Desatomat tool; see the README
for attribution, the input dialects and the full option reference.

The package splits along the pipeline's own seams::

    frontends -> EDS text -> gramparse -> transforms -> analysis -> codegen

with ``regex_engine`` (a Lark grammar for the regex dialect) and ``eds``
(the EDS stringifier) serving the frontends, ``chartools``/``symbols``/
``logutil`` underneath everything, ``pipeline`` as the library entry,
``cli`` as the command line and ``tests`` as the built-in suite.

Public surface: :func:`parse_regex`, :func:`convert_to_eds` and the
per-dialect converters, :func:`main`/:func:`main_cli`, and ``VERSION``.

:author: Alexios Angel <aangeletakis@gmail.com>
:license: MIT
"""

from .version import AUTHORS, HOMEPAGE, ISSUES, LICENSE, VERSION  # noqa: F401
from .regex_engine import RegexSyntaxError, parse_regex  # noqa: F401
from .frontends import convert_to_eds, ebnf_to_eds  # noqa: F401
from .frontends import lark_to_eds, w3c_to_eds  # noqa: F401
from .cli import main, main_cli  # noqa: F401
