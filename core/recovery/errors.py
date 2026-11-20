"""Typed modem recovery failures used to avoid futile retry loops."""

class PreambleNotFoundError(ValueError):
    """Raised when no sufficiently confident frame preamble exists."""

