"""Centralized logging configuration using loguru.

Call ``setup_logging()`` once at application startup (in ``create_app()``
or at the top of a Modal function) to configure the loguru sink and
intercept stdlib ``logging`` so third-party libraries (uvicorn, sqlalchemy,
httpx) route through the same pipeline.
"""

import logging
import sys

from loguru import logger


class _InterceptHandler(logging.Handler):
    """Bridge stdlib logging → loguru.

    Installed as the root handler so that any library using
    ``logging.getLogger(...)`` emits through loguru with the correct
    caller depth and level.
    """

    def emit(self, record: logging.LogRecord) -> None:
        # Map stdlib level to loguru level name
        try:
            level = logger.level(record.levelname).name
        except ValueError:
            level = record.levelno

        # Find the caller frame that originated the log call
        frame, depth = logging.currentframe(), 0
        while frame is not None:
            if frame.f_code.co_filename == logging.__file__:
                frame = frame.f_back
                depth += 1
                continue
            break

        logger.opt(depth=depth, exception=record.exc_info).log(
            level, record.getMessage()
        )


def setup_logging(level: str = "INFO") -> None:
    """Configure loguru as the single logging sink for the application.

    - Removes the default loguru handler.
    - Adds a stderr handler with a human-readable format that includes
      timestamp, level, module, function, and message.
    - Intercepts stdlib ``logging`` so libraries like uvicorn and
      sqlalchemy also route through loguru.

    Args:
        level: Minimum log level (DEBUG, INFO, WARNING, ERROR).
    """
    # Remove default loguru handler
    logger.remove()

    # Add a single stderr handler with structured, readable format
    logger.add(
        sys.stderr,
        level=level.upper(),
        format=(
            "<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | "
            "<level>{level: <8}</level> | "
            "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - "
            "<level>{message}</level>"
        ),
        backtrace=True,
        diagnose=False,  # disable variable inspection in prod for safety
    )

    # Intercept stdlib logging
    logging.basicConfig(handlers=[_InterceptHandler()], level=0, force=True)
