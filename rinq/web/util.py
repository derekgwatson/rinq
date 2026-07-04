"""Shared helpers for the web (Jinja) route layer."""
import logging

from flask import flash

logger = logging.getLogger(__name__)


def flash_error(message: str, exc: Exception = None) -> None:
    """Flash an error to the user AND log it server-side.

    Route handlers that only flash exceptions leave no server-side trail —
    a bug shipped to production is invisible until a user reports the flash
    text. Always route user-facing failures through here so the traceback
    lands in the logs.
    """
    if exc is not None:
        logger.exception(message)
    else:
        logger.error(message)
    flash(message, "error")
