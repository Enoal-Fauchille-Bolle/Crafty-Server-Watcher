"""Parsing of the event notifications Crafty sends to the watcher.

Crafty's per-server webhooks (``start_server``, ``stop_server``, …) fire
straight after the action they describe.  For ``start_server`` that is a few
milliseconds after the JVM is spawned and several seconds before it binds its
port — early enough for the watcher to step off the port in time, which
polling can never be.

Crafty has no "custom" webhook provider: every payload is shaped for a chat
service (Discord embeds, Slack blocks, …).  The one part we control is the
message body, a Jinja2 template.  Rendering a small JSON object there and
digging it back out of whatever envelope the provider used keeps this parser
independent of the provider chosen in the Crafty UI.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from typing import Any

log = logging.getLogger(__name__)

# The event names Crafty can fire, from WebhookFactory.get_monitored_events().
# Note the word order: the trigger is `start_server`, not `server_start` — the
# name comes from the decorated method, and Crafty's own API docs get it wrong.
KNOWN_EVENTS = (
    "start_server",
    "stop_server",
    "crash_detected",
    "backup_server",
    "jar_update",
    "send_command",
    "kill",
)

# Body template to paste into the Crafty webhook form.  Documented here so the
# parser and the thing it parses stay side by side.
BODY_TEMPLATE = '{"server_id": "{{ server_id }}", "event": "{{ event_type }}"}'

_MAX_DEPTH = 6


@dataclass(frozen=True)
class CraftyEvent:
    """A parsed Crafty webhook notification."""

    server_id: str
    event: str


def _iter_strings(node: Any, depth: int = 0) -> Iterator[str]:
    """Yield every string in a nested JSON structure, outermost first."""
    if depth > _MAX_DEPTH:
        return
    if isinstance(node, str):
        yield node
    elif isinstance(node, dict):
        for value in node.values():
            yield from _iter_strings(value, depth + 1)
    elif isinstance(node, list):
        for value in node:
            yield from _iter_strings(value, depth + 1)


def _as_object(text: str) -> dict[str, Any] | None:
    """Parse *text* as a JSON object, or return None."""
    text = text.strip()
    if not text.startswith("{"):
        return None
    try:
        parsed = json.loads(text)
    except ValueError:
        return None
    return parsed if isinstance(parsed, dict) else None


def parse_event(raw_body: str, known_ids: Iterable[str]) -> CraftyEvent | None:
    """Extract the server id and event name from a Crafty webhook body.

    Two passes, in order of trust:

    1. The rendered ``BODY_TEMPLATE`` object, found anywhere in the payload —
       inside a Discord embed description, a Slack block, or at the top level.
    2. A plain scan for a known server id and a known event name, so a
       hand-written body template still works as long as it mentions both.

    Returns None when neither pass identifies both halves.
    """
    known = set(known_ids)

    payload: Any = _as_object(raw_body)
    if payload is None:
        try:
            payload = json.loads(raw_body)
        except ValueError:
            payload = None

    server_id: str | None = None
    event: str | None = None

    if payload is not None:
        for chunk in _iter_strings(payload):
            data = _as_object(chunk)
            if data is None:
                continue
            candidate_id = data.get("server_id")
            candidate_event = data.get("event") or data.get("event_type")
            if isinstance(candidate_id, str) and server_id is None:
                server_id = candidate_id
            if isinstance(candidate_event, str) and event is None:
                event = candidate_event
        if isinstance(payload, dict):
            top_id = payload.get("server_id")
            top_event = payload.get("event") or payload.get("event_type")
            if isinstance(top_id, str) and server_id is None:
                server_id = top_id
            if isinstance(top_event, str) and event is None:
                event = top_event

    if server_id is None:
        server_id = next((sid for sid in known if sid and sid in raw_body), None)
    if event is None:
        event = next((name for name in KNOWN_EVENTS if name in raw_body), None)

    if server_id is None or event is None:
        log.warning(
            "Crafty event ignored: could not read %s from the payload",
            "a server id" if server_id is None else "an event name",
        )
        return None

    return CraftyEvent(server_id=server_id, event=event)
