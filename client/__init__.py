"""client — installable shared client package for the OpAMP server.

Re-exports the public API of :mod:`client.opamp_client` so both
``from client import OpampClient`` and ``from client.opamp_client import OpampClient``
work.
"""

from client.opamp_client import (
    DEFAULT_SERVER_URL,
    DEFAULT_TIMEOUT,
    OpampApiError,
    OpampClient,
    OpampClientError,
    OpampConnectionError,
)

__all__ = [
    "OpampClient",
    "OpampClientError",
    "OpampApiError",
    "OpampConnectionError",
    "DEFAULT_SERVER_URL",
    "DEFAULT_TIMEOUT",
]
