"""Configure structlog for the entire application. Call once at startup.

2026-09-13 fix: this used to only ever print to whatever console window
happened to be attached (structlog.PrintLoggerFactory() writes straight to
stdout, nothing else) -- data/logs/ was created on disk but nothing was
ever written into it, so the only way to see a run's logs was to have
someone physically watching the terminal at the time. Now routes through
stdlib logging (structlog.stdlib.LoggerFactory()) so the SAME log event
reaches two handlers: the console (unchanged, colored when interactive)
and a rotating file under data/logs/, one file per process start.
"""

from __future__ import annotations

import logging
import logging.handlers
import sys
from datetime import datetime
from pathlib import Path

import structlog


def configure(log_dir: Path | None = None) -> None:
    log_dir = log_dir or Path("C:/LabAutomation/data/logs")
    log_dir.mkdir(parents=True, exist_ok=True)

    shared_processors = [
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
    ]

    structlog.configure(
        processors=shared_processors + [structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
        wrapper_class=structlog.make_filtering_bound_logger(logging.DEBUG),
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )

    # Console: same look as before (colored key=value when the console is a
    # real terminal, e.g. the window "Start CIB Sorting Cell.bat" opens;
    # plain JSON if stdout is redirected/piped).
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(
        structlog.stdlib.ProcessorFormatter(
            processor=structlog.dev.ConsoleRenderer() if sys.stdout.isatty()
            else structlog.processors.JSONRenderer(),
            foreign_pre_chain=shared_processors,
        )
    )

    # File: always plain JSON (one object per line -- easy to grep/parse
    # later, and never garbled by ANSI color codes from ConsoleRenderer).
    # One file per process start rather than a single ever-growing file, so
    # "the log for this run" is just "the newest file in data/logs/". Size-
    # based rotation on top of that as a safety net -- flexi/plc status
    # polling logs at DEBUG level every ~100-200ms, so a long-running
    # process can otherwise accumulate an unbounded amount of data.
    started_at = datetime.now().strftime("%Y%m%d_%H%M%S")
    file_handler = logging.handlers.RotatingFileHandler(
        log_dir / f"cib_{started_at}.log",
        maxBytes=20 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    file_handler.setFormatter(
        structlog.stdlib.ProcessorFormatter(
            processor=structlog.processors.JSONRenderer(),
            foreign_pre_chain=shared_processors,
        )
    )

    root_logger = logging.getLogger()
    root_logger.setLevel(logging.DEBUG)
    root_logger.handlers.clear()
    root_logger.addHandler(console_handler)
    root_logger.addHandler(file_handler)
