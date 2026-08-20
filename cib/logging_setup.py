"""Configure structlog for the entire application. Call once at startup."""

from __future__ import annotations

import logging
import sys
from pathlib import Path

import structlog


def configure(log_dir: Path | None = None) -> None:
    # Dev-machine default (2026-08-20): kept inside the project folder rather
    # than C:\LabAutomation — revert when deploying to the HMI.
    log_dir = log_dir or Path(__file__).resolve().parent.parent / "output" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)

    structlog.configure(
        processors=[
            structlog.stdlib.add_log_level,
            # add_logger_name is omitted — requires stdlib LoggerFactory, not PrintLoggerFactory
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.StackInfoRenderer(),
            structlog.dev.ConsoleRenderer() if sys.stdout.isatty()
            else structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.DEBUG),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(),
    )
