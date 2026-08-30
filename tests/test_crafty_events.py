"""Tests for the Crafty event receiver — the path that makes a start from the
Crafty console survive.

The regression these guard against is concrete: the watcher sits on the
server's port while it hibernates, Crafty spawns the JVM, and the JVM dies a
few seconds later with "FAILED TO BIND TO PORT" because the watcher only
polls every 30s.  The `start_server` webhook arrives in between, and must
take the watcher off the port immediately.
"""

from __future__ import annotations

import asyncio
import json
import socket

import pytest

from crafty_server_watcher.config import CooldownConfig, CraftyEventsConfig, ServerConfig
from crafty_server_watcher.crafty_events import BODY_TEMPLATE, parse_event
from crafty_server_watcher.health_server import HealthServer
from crafty_server_watcher.proxy_listener import ProxyManager
from crafty_server_watcher.server_state import ServerStateMachine, State

PORT = 25598  # kept away from the usual MC ports so a local server is safe
HEALTH_PORT = 8195
SERVER_UUID = "c5da3465-e127-4ad2-9d36-bd313bf3eebe"


class FakeApi:
    async def start_server(self, server_id: str) -> None:  # pragma: no cover - unused
        pass


def make_sm(state: State = State.STOPPED) -> ServerStateMachine:
    cfg = ServerConfig(name="server-3", crafty_server_id=SERVER_UUID, listen_port=PORT)
    sm = ServerStateMachine(cfg=cfg, cooldowns=CooldownConfig())
    sm.transition(state)
    return sm


def discord_payload(server_id: str = SERVER_UUID, event: str = "start_server") -> str:
    """What Crafty actually posts: our body template inside a Discord embed."""
    body = BODY_TEMPLATE.replace("{{ server_id }}", server_id).replace(
        "{{ event_type }}", event
    )
    return json.dumps(
        {
            "username": "Crafty Bot",
            "embeds": [
                {
                    "title": "Watcher",
                    "description": body,
                    "author": {"name": "SMP"},
                    "footer": {"text": "Crafty Controller v.4.10.8"},
                }
            ],
        }
    )


def port_is_free() -> bool:
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        probe.bind(("0.0.0.0", PORT))
        return True
    except OSError:
        return False
    finally:
        probe.close()


# ---------------------------------------------------------------------------
# Payload parsing
# ---------------------------------------------------------------------------


# Captured verbatim from Crafty Controller 4.10.8 on 2026-08-31, posted by a
# `start_server` webhook 0.8s after the JVM was spawned.  Note the newline
# Jinja leaves in front of the rendered body, and the JSON nested as a string
# inside the embed description — both are the reason the parser digs rather
# than reads a field.
REAL_CRAFTY_PAYLOAD = r'''{"username": "Crafty Controller", "avatar_url": "https://gitlab.com/crafty-controller/crafty-4/-/raw/master/app/frontend/static/assets/images/Crafty_4-0.png", "embeds": [{"title": "watcher-start-probe", "description": "\n{\"server_id\": \"c5da3465-e127-4ad2-9d36-bd313bf3eebe\", \"event\": \"start_server\"}", "color": 23761, "author": {"name": "SMP 26.2"}, "footer": {"text": "Crafty Controller v.4.10.8"}, "timestamp": "2026-08-30T22:18:41.779Z"}]}'''


def test_parses_a_real_crafty_payload():
    event = parse_event(REAL_CRAFTY_PAYLOAD, {SERVER_UUID})
    assert event is not None
    assert event.server_id == SERVER_UUID
    assert event.event == "start_server"


def test_parses_the_template_out_of_a_discord_embed():
    event = parse_event(discord_payload(), {SERVER_UUID})
    assert event is not None
    assert event.server_id == SERVER_UUID
    assert event.event == "start_server"


def test_parses_a_flat_payload():
    raw = json.dumps({"server_id": SERVER_UUID, "event_type": "stop_server"})
    event = parse_event(raw, {SERVER_UUID})
    assert event is not None
    assert event.event == "stop_server"


def test_falls_back_to_scanning_a_hand_written_body():
    """A body that only mentions the id and the event still identifies both."""
    raw = json.dumps(
        {"embeds": [{"description": f"Server {SERVER_UUID} fired start_server"}]}
    )
    event = parse_event(raw, {SERVER_UUID})
    assert event is not None
    assert event.server_id == SERVER_UUID
    assert event.event == "start_server"


@pytest.mark.parametrize("raw", ["", "not json at all", json.dumps({"a": "b"})])
def test_unreadable_payloads_are_rejected(raw):
    assert parse_event(raw, {SERVER_UUID}) is None


# ---------------------------------------------------------------------------
# Releasing the port
# ---------------------------------------------------------------------------


def test_release_for_start_frees_the_port_and_holds_it_free():
    async def scenario() -> None:
        sm = make_sm()
        pm = ProxyManager({"server-3": sm}, FakeApi())
        await pm.ensure_listeners()
        assert not port_is_free(), "setup failed: the proxy should hold the port"

        assert await pm.release_for_start("server-3") == "released"
        assert port_is_free(), "the JVM would still fail to bind"
        assert sm.state == State.STARTING

        # A poll tick landing now must not steal the port back.
        await pm.ensure_listeners()
        assert port_is_free(), "proxy re-bound under the booting server"

        await pm.stop_all()

    asyncio.run(scenario())


class RecordingWebhook:
    """Stands in for WebhookNotifier; records the announcement."""

    def __init__(self):
        self.started: list[tuple[str, str]] = []

    async def notify_started(self, server_name: str, player_name: str = "", source: str = "") -> None:
        self.started.append((server_name, source))


def test_a_start_from_crafty_is_announced_on_discord():
    """A start nobody in the channel triggered still has to be explained."""

    async def scenario() -> None:
        sm = make_sm()
        hook = RecordingWebhook()
        pm = ProxyManager({"server-3": sm}, FakeApi(), webhook=hook)
        await pm.ensure_listeners()

        assert await pm.release_for_start("server-3") == "released"
        await asyncio.sleep(0.05)  # let the fire-and-forget task run
        assert hook.started == [("server-3", "Crafty (console or API)")]

        await pm.stop_all()

    asyncio.run(scenario())


def test_a_refused_release_announces_nothing():
    async def scenario() -> None:
        sm = make_sm()
        sm.transition(State.ONLINE)
        hook = RecordingWebhook()
        pm = ProxyManager({"server-3": sm}, FakeApi(), webhook=hook)

        assert await pm.release_for_start("server-3") == "not_stopped"
        await asyncio.sleep(0.05)
        assert hook.started == []

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("state", "expected"),
    [(State.ONLINE, "not_stopped"), (State.IDLE, "not_stopped"), (State.STARTING, "already_starting")],
)
def test_release_for_start_is_a_no_op_when_the_server_is_not_asleep(state, expected):
    async def scenario() -> None:
        sm = make_sm()
        sm.transition(state)
        pm = ProxyManager({"server-3": sm}, FakeApi())
        assert await pm.release_for_start("server-3") == expected

    asyncio.run(scenario())


def test_release_for_start_ignores_an_unmanaged_server():
    async def scenario() -> None:
        pm = ProxyManager({}, FakeApi())
        assert await pm.release_for_start("server-9") == "not_managed"

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# HTTP endpoint
# ---------------------------------------------------------------------------


class RecordingProxy:
    """Stands in for ProxyManager; records what the endpoint asked for."""

    def __init__(self):
        self.released: list[str] = []

    async def release_for_start(self, name: str) -> str:
        self.released.append(name)
        return "released"


async def post(path: str, body: str) -> tuple[int, str]:
    reader, writer = await asyncio.open_connection("127.0.0.1", HEALTH_PORT)
    payload = body.encode()
    writer.write(
        f"POST {path} HTTP/1.1\r\nHost: localhost\r\nContent-Type: application/json\r\n"
        f"Content-Length: {len(payload)}\r\n\r\n".encode()
        + payload
    )
    await writer.drain()
    raw = (await reader.read(-1)).decode()
    writer.close()
    status = int(raw.split(" ")[1])
    return status, raw.split("\r\n\r\n", 1)[-1]


async def run_health(proxy, cfg, scenario):
    shutdown = asyncio.Event()
    srv = HealthServer(
        {"server-3": make_sm()},
        host="127.0.0.1",
        port=HEALTH_PORT,
        proxy_manager=proxy,
        events_cfg=cfg,
    )
    task = asyncio.ensure_future(srv.run(shutdown))
    await asyncio.sleep(0.1)
    try:
        await scenario()
    finally:
        shutdown.set()
        await task


def test_start_event_releases_the_port():
    proxy = RecordingProxy()
    cfg = CraftyEventsConfig(enabled=True, path="/events/crafty", token="s3cret")

    async def scenario() -> None:
        status, body = await post("/events/crafty?token=s3cret", discord_payload())
        assert status == 200
        assert json.loads(body)["result"] == "released"
        assert proxy.released == ["server-3"]

    asyncio.run(run_health(proxy, cfg, scenario))


def test_other_events_are_acknowledged_but_do_nothing():
    """Crafty raises on a non-2xx, from inside the call that starts the server."""
    proxy = RecordingProxy()
    cfg = CraftyEventsConfig(enabled=True, path="/events/crafty", token="")

    async def scenario() -> None:
        status, body = await post(
            "/events/crafty", discord_payload(event="backup_server")
        )
        assert status == 200
        assert json.loads(body)["result"] == "ignored"
        assert proxy.released == []

    asyncio.run(run_health(proxy, cfg, scenario))


def test_a_wrong_token_is_refused():
    proxy = RecordingProxy()
    cfg = CraftyEventsConfig(enabled=True, path="/events/crafty", token="s3cret")

    async def scenario() -> None:
        status, _ = await post("/events/crafty?token=nope", discord_payload())
        assert status == 403
        assert proxy.released == []

    asyncio.run(run_health(proxy, cfg, scenario))


def test_an_unknown_server_is_reported_not_acted_on():
    proxy = RecordingProxy()
    cfg = CraftyEventsConfig(enabled=True, path="/events/crafty", token="")

    async def scenario() -> None:
        status, body = await post(
            "/events/crafty", discord_payload(server_id="00000000-dead-beef")
        )
        assert status == 200
        assert json.loads(body)["result"] == "unknown_server"
        assert proxy.released == []

    asyncio.run(run_health(proxy, cfg, scenario))


def test_the_endpoint_is_absent_until_enabled():
    proxy = RecordingProxy()
    cfg = CraftyEventsConfig(enabled=False)

    async def scenario() -> None:
        status, _ = await post("/events/crafty", discord_payload())
        assert status == 404
        assert proxy.released == []

    asyncio.run(run_health(proxy, cfg, scenario))
