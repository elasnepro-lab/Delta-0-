"""Keep secrets carried in URLs out of every log line, alert and journal entry.

Two secrets travel inside URLs: the RPC provider's API key (Alchemy puts it in
the path, `/v2/<key>`) and the Telegram bot token (`/bot<token>/`). A library
exception that names the URL carries them with it — aiohttp's
`ClientResponseError` prints `url='https://arb-mainnet.g.alchemy.com/v2/<key>'`
whenever both RPC endpoints answer a 429 or a 5xx. Before this module, that
text reached journald through `log.exception`, the SQLite journal through the
recorded failure cause, and — once alerts were armed — the Telegram group.

The rule is blunt on purpose: every URL keeps its scheme and host and loses its
path, query and credentials. A host is enough to diagnose an outage; a path is
where providers hide keys, and guessing which paths are safe is how one leaks.
"""

from __future__ import annotations

import re
from collections.abc import MutableMapping
from typing import Any

# scheme://[userinfo@]host[:port][/path?query#fragment], stopping at whitespace or
# at the quotes and brackets that surround a URL inside a repr.
_URL = re.compile(
    r"(?P<scheme>(?:https?|wss?)://)(?:[^@/\s'\"<>()\[\]]*@)?(?P<host>[^/\s'\"<>()\[\]]+)(?P<rest>/[^\s'\"<>()\[\]]*)?"
)


def redact_secrets(text: str) -> str:
    """`text` with every URL reduced to its scheme and host."""

    def _keep_host(match: re.Match[str]) -> str:
        tail = "/…" if match.group("rest") else ""
        return f"{match.group('scheme')}{match.group('host')}{tail}"

    return _URL.sub(_keep_host, text)


def redact_event(
    _logger: Any,
    _name: str,
    event_dict: MutableMapping[str, Any],
) -> MutableMapping[str, Any]:
    """structlog processor: redact every string value of the event.

    Placed after `format_exc_info`, so the rendered traceback is covered, and
    before the alert processor and the renderer, so neither ever sees a secret.
    """
    for key, value in event_dict.items():
        if isinstance(value, str):
            event_dict[key] = redact_secrets(value)
    return event_dict
