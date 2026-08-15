"""End-to-end tests of the login path, over a real socket.

Two behaviours are covered, both of which cost a real Minecraft server
when they regress:

1. The start race — the proxy must keep the port free for the whole
   start window, or the booting server dies with "FAILED TO BIND TO
   PORT".  A poll tick landing inside the window used to clear the
   lockout and re-bind underneath it.
2. Whitelist enforcement — a refused player must not cause a start.
"""

from __future__ import annotations

import asyncio
import json
import socket

import pytest

from crafty_server_watcher.config import AccessConfig, CooldownConfig, ServerConfig
from crafty_server_watcher.mc_protocol import build_packet, write_utf, write_varint
from crafty_server_watcher.proxy_listener import ProxyManager
from crafty_server_watcher.server_state import ServerStateMachine, State

PORT = 25599  # kept out of the usual 25565/25500 range so local servers are safe


class FakeApi:
    """Stands in for CraftyApiClient; records the start call."""

    def __init__(self):
        self.started: list[str] = []

    async def start_server(self, server_id: str) -> None:
        self.started.append(server_id)


def port_is_free() -> bool:
    """True if a fresh listener can bind PORT — i.e. the MC server could boot."""
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    # Netty (and every real server) sets SO_REUSEADDR, so TIME_WAIT sockets
    # left by the kicked player must not count as "port taken" — only a live
    # listener does.
    probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        probe.bind(("0.0.0.0", PORT))
        return True
    except OSError:
        return False
    finally:
        probe.close()


def make_sm(access: AccessConfig | None = None) -> ServerStateMachine:
    cfg = ServerConfig(
        name="server-1",
        crafty_server_id="uuid-1",
        listen_port=PORT,
        access=access or AccessConfig(),
    )
    return ServerStateMachine(cfg=cfg, cooldowns=CooldownConfig())


async def send_login(player_name: str) -> asyncio.StreamWriter:
    """Connect and send Handshake(next_state=2) + Login Start."""
    _reader, writer = await asyncio.open_connection("127.0.0.1", PORT)
    handshake = (
        write_varint(770) + write_utf("mc.example.org") + PORT.to_bytes(2, "big") + write_varint(2)
    )
    writer.write(build_packet(0x00, handshake))
    writer.write(build_packet(0x00, write_utf(player_name) + b"\x00" * 16))
    await writer.drain()
    return writer


@pytest.mark.parametrize("initial", [State.STOPPED, State.CRASHED])
def test_port_stays_free_through_the_start_window(initial):
    async def scenario() -> None:
        sm = make_sm()
        sm.transition(initial)
        api = FakeApi()
        pm = ProxyManager({"server-1": sm}, api)

        await pm.ensure_listeners()
        assert not port_is_free(), "setup failed: proxy should hold the port while asleep"

        writer = await send_login("TestPlayer")

        # Let _handle_login reach its 5s sleep, then fire the poll tick that
        # used to steal the port back.
        await asyncio.sleep(1)
        await pm.ensure_listeners()

        assert port_is_free(), "proxy re-bound the port — MC would fail to bind"

        # Let the start sequence finish; the API must still have been called.
        await asyncio.sleep(5)
        assert api.started == ["uuid-1"]

        writer.close()
        await pm.stop_all()

    asyncio.run(scenario())


def test_denied_player_does_not_start_the_server(tmp_path):
    whitelist = tmp_path / "whitelist.json"
    whitelist.write_text(
        json.dumps([{"uuid": "uuid-a", "name": "AZERTY____"}]),
        encoding="utf-8",
    )

    async def scenario() -> None:
        sm = make_sm(AccessConfig(mode="whitelist", whitelist_file=str(whitelist)))
        sm.transition(State.STOPPED)
        api = FakeApi()
        pm = ProxyManager({"server-1": sm}, api)

        await pm.ensure_listeners()
        writer = await send_login("Cornbread2100_")
        await asyncio.sleep(1)

        assert api.started == [], "a non-whitelisted player triggered a start"
        assert sm.state is State.STOPPED, "state changed for a refused player"
        assert not port_is_free(), "proxy dropped the port for a refused player"

        writer.close()
        await pm.stop_all()

    asyncio.run(scenario())


def test_whitelisted_player_still_starts_the_server(tmp_path):
    whitelist = tmp_path / "whitelist.json"
    whitelist.write_text(
        json.dumps([{"uuid": "uuid-a", "name": "AZERTY____"}]),
        encoding="utf-8",
    )

    async def scenario() -> None:
        sm = make_sm(AccessConfig(mode="whitelist", whitelist_file=str(whitelist)))
        sm.transition(State.STOPPED)
        api = FakeApi()
        pm = ProxyManager({"server-1": sm}, api)

        await pm.ensure_listeners()
        writer = await send_login("AZERTY____")
        await asyncio.sleep(6)

        assert api.started == ["uuid-1"]
        assert sm.state is State.STARTING

        writer.close()
        await pm.stop_all()

    asyncio.run(scenario())
