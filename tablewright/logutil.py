"""Logging for the whole package: one shared logger, the TRACE level,
stage banners and per-stage timing."""

import logging
import time

from contextlib import contextmanager
from sys import stdout



logging.captureWarnings(True)
logger = logging.getLogger(__name__)
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
