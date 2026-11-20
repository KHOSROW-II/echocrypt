"""Typed modem recovery failures used to avoid futile retry loops."""

class PreambleNotFoundError(ValueError):
    """Raised when no sufficiently confident frame preamble exists."""


class HeaderCRCError(ValueError):
    """Raised when sync is found but no candidate yields a valid frame header."""

    def __init__(self, message, *, sync_score=0.0):
        super().__init__(message)
        self.sync_score = float(sync_score)

