"""Exceptions raised by the public ECHO-CRYPT SDK."""


class EchoCryptSDKError(Exception):
    """Base class for SDK-level errors."""


class ClientClosedError(EchoCryptSDKError):
    """The client was permanently closed and cannot be restarted."""


class ClientNotRunningError(EchoCryptSDKError):
    """An operation requires a started acoustic client."""


class PacketTooLargeError(EchoCryptSDKError, ValueError):
    """A packet exceeds the modem's configured payload limit."""


class ReceiveTimeout(EchoCryptSDKError, TimeoutError):
    """No packet was received before the requested timeout."""
