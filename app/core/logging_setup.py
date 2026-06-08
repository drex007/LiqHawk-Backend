"""structlog — JSON logs in production, pretty console in dev.

Why structlog over stdlib logging:
  - First-class structured fields: log.info("event", key=value) → grep-able JSON
  - Bound context: contextvars get attached to every log line in a request
  - Plays nicely with FastAPI middleware that wants to add request_id, etc.
"""

from __future__ import annotations

import logging
import sys

import structlog


def configure_logging(level: str = "INFO", fmt: str = "json") -> None:
    """Call once at app startup. Idempotent — safe to call from tests too."""
    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=getattr(logging, level.upper(), logging.INFO),
    )

    processors: list = [
        # contextvars — lets middleware bind request_id, user_id, etc.
        structlog.contextvars.merge_contextvars,
        # adds "level": "info" to the output
        structlog.processors.add_log_level,
        # ISO-format UTC timestamp on every line
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        # pretty stack traces on exceptions
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]

    # The final processor decides the OUTPUT format
    if fmt == "json":
        processors.append(structlog.processors.JSONRenderer())
    else:
        processors.append(structlog.dev.ConsoleRenderer())

    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(logging, level.upper(), logging.INFO)
        ),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    """Use this everywhere: `log = get_logger(__name__)`."""
    return structlog.get_logger(name)
