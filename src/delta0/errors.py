"""The bot's exception hierarchy.

Every error the bot raises on purpose derives from `Delta0Error`, so the loop
can tell "an operation failed" from "the code is wrong". Until chantier 6.4,
thirteen `except Exception` could not: an `AttributeError` from a typo was
logged as "le cycle a levé" and the tracer carried on forever, a bug reading as
a network hiccup.

The failures the loop may survive are listed once, in
`delta0.failure.OPERATIONAL_ERRORS`. Anything else propagates and stops the
process loudly: systemd restarts it — five times in five minutes, then gives
up — and the boot reconciliation checks the bands, the anchor, the debt and the
HF before the loop resumes. A bot that
crashes on a bug is recoverable; one that keeps trading around it is not.
"""

from __future__ import annotations


class Delta0Error(Exception):
    """Base of every error the bot raises on purpose."""


class VenueError(Delta0Error):
    """A venue answered, and the answer is not one the bot can act on."""


class RpcResponseError(VenueError):
    """Every RPC endpoint answered, and none with a body that could be read.

    A proxy or CDN in front of a provider can answer HTTP 200 with an HTML page;
    web3 then raises `json.JSONDecodeError`, a ValueError. Left as such it read
    as a programming error and stopped the process instead of failing over.
    """


class BootRefused(Delta0Error):  # noqa: N818 - mirrors SafetyRefused naming
    """The boot checks found a state the bot must not start on.

    Raised instead of a bare ValueError so the CLI can refuse with a readable
    message and an exit code, not a traceback.
    """
