"""Character-level utilities shared across the pipeline: escape scanning
and decoding, [[...]] range expansion, literal decoding, case folding and
contiguous-run decomposition."""

import re



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

_REGEX_DIGIT_CHARS = frozenset("0123456789")
_REGEX_WORD_CHARS = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_")
_REGEX_SPACE_CHARS = frozenset(" \t\r\n\f\v")
_REGEX_CONTROL_ESCAPES = {
    "n": "\n", "t": "\t", "r": "\r", "f": "\f", "v": "\v", "a": "\a", "0": "\0",
}

def _fold_case(chars) -> set:
    """Return ``chars`` with the upper- and lowercase form of every member."""
    folded = set()
    for char in chars:
        folded.add(char)
        folded.update(char.lower())
        folded.update(char.upper())
    return folded

def _decode_quoted_literal(token: str) -> "tuple[str, bool]":
    r"""Decode a quoted Lark/EBNF string literal into its character content.

    Handles both quote styles, the Lark ``"..."i`` case-insensitive suffix,
    and Python-style escapes (``\n``-family, ``\xNN``, ``\uNNNN``,
    ``\UNNNNNNNN``; any other escaped character is itself).

    Args:
        token: The literal as it appears in the source, quotes included.

    Returns:
        An ``(text, insensitive)`` pair.
    """
    insensitive = token.endswith("i") and token[0] in "\"'"
    body_token = token[:-1] if insensitive else token
    body = body_token[1:-1]
    out = []
    index = 0
    while index < len(body):
        char = body[index]
        if char == "\\" and index + 1 < len(body):
            escape = body[index + 1]
            if escape in _REGEX_CONTROL_ESCAPES:
                out.append(_REGEX_CONTROL_ESCAPES[escape])
                index += 2
                continue
            if escape == "b":
                out.append("\x08")
                index += 2
                continue
            if escape in "xuU":
                width = {"x": 2, "u": 4, "U": 8}[escape]
                digits = body[index + 2:index + 2 + width]
                if len(digits) == width and all(
                        d in "0123456789abcdefABCDEF" for d in digits):
                    code_point = int(digits, 16)
                    if code_point > 0x10FFFF:
                        raise ValueError(
                            f"escape \\{escape}{digits} in {token} is beyond "
                            "the last code point U+10FFFF")
                    out.append(chr(code_point))
                    index += 2 + width
                    continue
            out.append(escape)
            index += 2
            continue
        out.append(char)
        index += 1
    return "".join(out), insensitive


def _split_regex_literal(token: str) -> "tuple[str, str]":
    """Split a ``/pattern/flags`` literal into its pattern and flag letters."""
    pattern, _, flags = token[1:].rpartition("/")
    return pattern, flags

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
