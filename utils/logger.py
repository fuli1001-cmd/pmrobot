"""Structured logging configuration using structlog."""

import logging
import sys
from typing import Optional

import structlog


def setup_logging(
    level: str = "INFO",
    json_format: bool = False,
    log_file: Optional[str] = None,
) -> None:
    """
    Configure structured logging for the application.

    Args:
        level: Logging level (DEBUG, INFO, WARNING, ERROR, CRITICAL)
        json_format: If True, output logs in JSON format
        log_file: Optional file path to write logs to
    """
    # Configure structlog processors
    shared_processors = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.PositionalArgumentsFormatter(),
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.UnicodeDecoder(),
    ]

    structlog.configure(
        processors=shared_processors + [
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )

    # Configure formatters
    if json_format:
        console_processor = structlog.processors.JSONRenderer()
        file_processor = structlog.processors.JSONRenderer()
    else:
        # Console gets colors, file gets plain text
        console_processor = structlog.dev.ConsoleRenderer(colors=True)
        file_processor = structlog.dev.ConsoleRenderer(colors=False)

    console_formatter = structlog.stdlib.ProcessorFormatter(
        processor=console_processor,
        foreign_pre_chain=shared_processors,
    )
    
    file_formatter = structlog.stdlib.ProcessorFormatter(
        processor=file_processor,
        foreign_pre_chain=shared_processors,
    )

    # Configure root logger
    root_logger = logging.getLogger()
    root_logger.setLevel(getattr(logging, level.upper()))
    root_logger.handlers = []  # Clear existing handlers

    # Add console handler
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(console_formatter)
    root_logger.addHandler(console_handler)

    # Add file handler if specified
    if log_file:
        file_handler = logging.FileHandler(log_file, encoding="utf-8")
        file_handler.setFormatter(file_formatter)
        root_logger.addHandler(file_handler)


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    """
    Get a logger instance with the given name.

    Args:
        name: Logger name (usually __name__)

    Returns:
        Configured structlog logger
    """
    return structlog.get_logger(name)
