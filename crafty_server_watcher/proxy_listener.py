"""Async TCP proxy listeners for hibernating Minecraft servers.

For every managed server that is in the STOPPED / STARTING / CRASHED
state, a lightweight TCP server binds to the configured port and handles
the Minecraft protocol just enough to:
- Answer Server List Pings with a custom MOTD.
- On Login attempts, send a kick message and trigger a server start.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Coroutine
from typing import Any

from .access_control import AccessController
from .crafty_api import CraftyApiClient
from .crafty_events import CRAFTY_CONSOLE
from .mc_protocol import (
    Handshake,
    LoginStart,
    build_disconnect,
    build_pong,
    build_status_response,
    read_packet,
)
from .server_state import ServerStateMachine, State
from .webhook import WebhookNotifier

log = logging.getLogger(__name__)


class ProxyManager:
    """Manages per-port asyncio TCP servers for hibernating MC servers.

    The idle monitor calls :meth:`ensure_listeners` after each poll to
    start / stop listeners as the server states change.
    """

    def __init__(
        self,
        state_machines: dict[str, ServerStateMachine],
        crafty_api: CraftyApiClient,
        webhook: WebhookNotifier | None = None,
    ):
        self._sms = state_machines
        self._api = crafty_api
        self._webhook = webhook
        self._access = {
            name: AccessController(name, sm.cfg.access) for name, sm in state_machines.items()
        }
        # name → running asyncio.Server (or None)
        self._listeners: dict[str, asyncio.Server | None] = {name: None for name in state_machines}
        # Servers where we triggered a start — NEVER re-bind proxy for these
        # until they go back to STOPPED or CRASHED.
        self._start_lockout: set[str] = set()
        # Servers whose listener is mid-rebind: _start_listener() can spend 30s
        # waiting for a stopping JVM to hand the socket back, and a poll landing
        # in that window must not start a second retry loop for the same port.
        self._rebinding: set[str] = set()
        # Strong references to fire-and-forget tasks.  asyncio only holds a weak
        # one, so a notification parked in a bare local can be collected before
        # it is sent.
        self._tasks: set[asyncio.Task[Any]] = set()

    def _spawn(self, coro: Coroutine[Any, Any, Any]) -> None:
        """Run *coro* in the background, holding a reference until it is done.

        Every background job here is fire-and-forget by design: a Discord
        hiccup must not hold up a server that is already booting, and a slow
        rebind must not hold up the HTTP reply Crafty is waiting on.
        """
        task = asyncio.ensure_future(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def run(self, shutdown: asyncio.Event) -> None:
        """Block until the shutdown event is set, then close all listeners."""
        await shutdown.wait()
        await self.stop_all()

    def reload_access(self) -> None:
        """Re-read the access settings after a SIGHUP config reload."""
        for name, sm in self._sms.items():
            self._access[name].update(sm.cfg.access)

    async def ensure_listeners(self) -> None:
        """Start or stop listeners to match the current server states."""
        for name, sm in self._sms.items():
            # If we triggered a start, NEVER re-bind until server is back to
            # STOPPED or CRASHED.
            if name in self._start_lockout:
                if sm.state in (State.STOPPED, State.CRASHED):
                    # Server went back to stopped — clear lockout, allow proxy.
                    self._start_lockout.discard(name)
                    log.info(f"Start lockout cleared for '{name}' (state={sm.state.value})")
                else:
                    # Still starting/online — keep port free.
                    continue

            if sm.is_proxy_needed:
                await self._start_listener(name)
            else:
                await self._stop_listener(name)

    async def stop_all(self) -> None:
        """Shut down every active listener."""
        for name in list(self._listeners):
            await self._stop_listener(name)

    async def release_for_start(self, name: str) -> str:
        """Free *name*'s port for a start the watcher did not trigger.

        Crafty fires its `start_server` webhook a few milliseconds after
        spawning the JVM, which binds the port a few seconds later.  Polling
        cannot win that race — at the default 30s interval the watcher is
        still holding the port when the server gives up — so the event has to
        drive the release directly.

        Returns a short status string, for logging and for the HTTP reply.
        """
        sm = self._sms.get(name)
        if sm is None:
            return "not_managed"
        if name in self._start_lockout or sm.state == State.STARTING:
            return "already_starting"
        if sm.state not in (State.STOPPED, State.CRASHED):
            return "not_stopped"

        await self._release_port_for_start(name, sm)
        log.info(
            f"Port {sm.cfg.listen_port} released for '{name}' on Crafty's start event "
            "(lockout active)",
        )
        if self._webhook:
            # Fire-and-forget, as on the login path: a Discord hiccup must not
            # hold up a server that is already booting.
            self._spawn(self._webhook.notify_started(name, source=CRAFTY_CONSOLE))
        return "released"

    async def reclaim_after_stop(
        self,
        name: str,
        *,
        crashed: bool = False,
        source: str = "",
    ) -> str:
        """Take *name*'s port back the moment Crafty says the server is going down.

        The mirror of :meth:`release_for_start`, and the same race run the
        other way: polling only notices the stop up to a full interval later,
        and until then nobody is listening on the port.  A player pinging in
        that window gets a refused connection instead of the hibernating MOTD,
        and a player *connecting* fails to wake the server at all — which is
        the whole point of holding the port.

        Returns a short status string, for logging and for the HTTP reply.
        """
        sm = self._sms.get(name)
        if sm is None:
            return "not_managed"
        if sm.state in (State.STOPPED, State.CRASHED):
            return "already_stopped"  # the port is already ours

        # Read before the transition: STOPPING means this is the watcher's own
        # idle shutdown, which _check_idle_shutdown() already announces.  Any
        # other state means the stop came from elsewhere and nobody has said so.
        was_ours = sm.state == State.STOPPING

        # A stop cancels whatever start we were keeping the port free for.
        self._start_lockout.discard(name)
        sm.transition(State.CRASHED if crashed else State.STOPPED)

        # Rebind in the background.  Crafty fires the event when it *asks* for
        # the stop, and the JVM keeps the socket for as long as saving the world
        # takes; _start_listener() already retries for 30s.  Awaiting it here
        # would hold the HTTP reply open, and Crafty raises on a slow dispatch.
        self._spawn(self._start_listener(name))
        log.info(
            f"Port {sm.cfg.listen_port} reclaimed for '{name}' on Crafty's "
            f"{'crash' if crashed else 'stop'} event",
        )

        if self._webhook and not was_ours:
            if crashed:
                self._spawn(self._webhook.notify_crashed(name))
            else:
                self._spawn(self._webhook.notify_stopped(name, source=source))
        return "reclaimed"

    async def _release_port_for_start(self, name: str, sm: ServerStateMachine) -> None:
        """Step off the port and hold the state machine in STARTING.

        Shared by the two ways a start begins: a player waking the server up,
        and Crafty announcing a start of its own.  The order matters in both.
        """
        # ── CRITICAL: release the port BEFORE the MC server needs it ──
        # Stop the proxy listener so the MC server can bind to the port.
        await self._stop_listener(name)

        # Lock out this server from ensure_listeners re-binding.
        self._start_lockout.add(name)

        # Transition BEFORE anything that yields: a concurrent
        # ensure_listeners() poll landing in that window would otherwise see
        # state == STOPPED, clear the lockout and re-bind the port under the
        # MC server.
        sm.transition(State.STARTING)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    async def _start_listener(self, name: str) -> None:
        """Bind the proxy listener for *name* if it isn't already running."""
        if self._listeners[name] is not None:
            return  # already listening
        if name in self._rebinding:
            return  # a retry loop is already waiting for this port to come free

        sm = self._sms[name]

        async def _client_cb(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            await self._handle_client(name, reader, writer)

        self._rebinding.add(name)
        try:
            for attempt in range(15):  # retry binding for up to 30s
                # A start can be announced while this loop is still waiting for
                # a dying JVM to let go — and then the port belongs to the next
                # JVM, not to us.  _stop_listener() cannot cancel a bind that
                # has not happened yet, so the loop has to check for itself.
                if name in self._start_lockout:
                    log.info(f"Rebind for '{name}' abandoned: a start claimed the port")
                    return
                try:
                    server = await asyncio.start_server(
                        _client_cb,
                        host=sm.cfg.listen_host,
                        port=sm.cfg.listen_port,
                    )
                    self._listeners[name] = server
                    log.info(
                        f"Proxy listener started on {sm.cfg.listen_host}:{sm.cfg.listen_port} for server '{name}'",
                    )
                    return
                except OSError as exc:
                    if attempt < 14:
                        log.debug(
                            f"Port {sm.cfg.listen_port} not free yet (attempt {attempt + 1}): {exc}",
                        )
                        await asyncio.sleep(2)
                    else:
                        log.error(
                            f"Cannot bind to port {sm.cfg.listen_port} for server '{name}' after 30s: {exc}",
                        )
        finally:
            self._rebinding.discard(name)

    async def _stop_listener(self, name: str) -> None:
        """Close the proxy listener for *name* if it is running."""
        server = self._listeners.get(name)
        if server is None:
            return
        server.close()
        await server.wait_closed()
        self._listeners[name] = None
        sm = self._sms[name]
        log.info(
            f"Proxy listener stopped on port {sm.cfg.listen_port} for server '{name}'",
        )

    async def _handle_client(
        self,
        name: str,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        """Handle a single incoming MC client connection."""
        sm = self._sms[name]
        peer = writer.get_extra_info("peername", ("?", 0))
        try:
            # 1) Read Handshake (packet id 0x00 in handshake state)
            pkt_id, stream = await asyncio.wait_for(read_packet(reader), timeout=10)
            if pkt_id != 0x00:
                return
            handshake = Handshake.parse(stream)

            if handshake.next_state == 1:
                # ── Status (Server List Ping) ────────────────────────
                await self._handle_status(sm, reader, writer)

            elif handshake.next_state == 2:
                # ── Login ────────────────────────────────────────────
                await self._handle_login(name, sm, reader, writer, peer)

        except (EOFError, TimeoutError, asyncio.IncompleteReadError):
            # Client disconnected or timed out — ignore silently.
            pass
        except Exception:
            log.exception(f"Error handling client from {peer} on port {sm.cfg.listen_port}")
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

    async def _handle_status(
        self,
        sm: ServerStateMachine,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        """Handle Server List Ping: send fake MOTD, answer Ping with Pong."""
        # Read Status Request (packet 0x00, empty payload)
        await asyncio.wait_for(read_packet(reader), timeout=5)

        resp = build_status_response(
            motd=sm.cfg.motd_hibernating,
            version_name="Hibernating",
            protocol=-1,
            max_players=sm.last_known_max,
            online_players=0,
            # No favicon: Crafty cannot read back the icon it stores itself.
            # It re-encodes the icon with base64.encodebytes and drops the
            # "data:image/png;base64," prefix (remote_stats/stats.py), then
            # slices a fixed 22 characters off any favicon it receives before
            # decoding it (remote_stats/ping.py). Replaying the cached icon
            # therefore hands it a body 22 characters short, and the resulting
            # binascii.Error escapes the `except OSError` guarding its ping —
            # killing Crafty's start thread before it registers the server's
            # stats jobs. Sending no favicon is the only shape it parses.
            favicon="",
        )
        writer.write(resp)
        await writer.drain()

        # Read Ping → send Pong
        try:
            pkt_id, stream = await asyncio.wait_for(read_packet(reader), timeout=5)
            if pkt_id == 0x01:
                payload_long = stream.read(8)
                writer.write(build_pong(payload_long))
                await writer.drain()
        except (EOFError, TimeoutError, asyncio.IncompleteReadError):
            pass

    async def _handle_login(
        self,
        name: str,
        sm: ServerStateMachine,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        peer: Any,
    ) -> None:
        """Handle Login Start: kick the player, release the port, then trigger a server start."""
        # Read Login Start (packet 0x00 in login state)
        pkt_id, stream = await asyncio.wait_for(read_packet(reader), timeout=5)
        if pkt_id != 0x00:
            return
        login = LoginStart.parse(stream)

        # ── Access control ───────────────────────────────────────────
        # Checked before anything else happens: a refused player must not
        # cost a JVM start.  Nothing about the server's state, the listener
        # or the start lockout is touched, so the proxy stays bound and
        # ready for a legitimate player.
        if not self._access[name].is_allowed(login.player_name):
            log.warning(
                f"Wake-up DENIED for player '{login.player_name}' ({peer[0]}) on port "
                f"{sm.cfg.listen_port} (server '{name}'): not whitelisted",
            )
            writer.write(build_disconnect(sm.cfg.access.deny_message))
            await writer.drain()
            if self._webhook:
                self._spawn(self._webhook.notify_denied(name, login.player_name, peer[0]))
            return

        log.info(
            f"Wake-up trigger from player '{login.player_name}' ({peer[0]}) on port {sm.cfg.listen_port} (server '{name}')",
        )

        # Send Disconnect (kick) message
        writer.write(build_disconnect(sm.cfg.kick_message))
        await writer.drain()

        # Close this client connection immediately so the port isn't held.
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:
            pass

        # Trigger server start if not already starting
        if sm.state in (State.STOPPED, State.CRASHED):
            await self._release_port_for_start(name, sm)

            # Give the OS a moment to fully release the socket.  Crafty's own
            # start event gets no such pause: there the JVM is already up and
            # the wait would eat into the few seconds before it binds.
            await asyncio.sleep(5)

            try:
                await self._api.start_server(sm.cfg.crafty_server_id)
                log.info(
                    f"Port {sm.cfg.listen_port} released and start_server sent for '{name}' (lockout active)",
                )
                if self._webhook:
                    self._spawn(self._webhook.notify_started(name, player_name=login.player_name))
            except Exception:
                log.exception(f"Failed to start server '{name}' via Crafty API")
                # Roll back the optimistic transition, clear the lockout and
                # re-bind the proxy so players can still see the MOTD.
                sm.transition(State.STOPPED)
                self._start_lockout.discard(name)
                await self._start_listener(name)
