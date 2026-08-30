"""Lightweight HTTP health-check, status, metrics and event server.

Exposes four endpoints:
- GET  /health         → 200 OK (for Docker HEALTHCHECK / Uptime Kuma)
- GET  /status         → 200 JSON with per-server state details
- GET  /metrics        → 200 Prometheus text exposition format
- POST /events/crafty  → Crafty's own webhooks (path configurable, opt-in)
"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import time
from http import HTTPStatus
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qs

from .crafty_events import parse_event
from .metrics import generate_metrics
from .server_state import ServerStateMachine

if TYPE_CHECKING:
    from .config import CraftyEventsConfig
    from .proxy_listener import ProxyManager

# Crafty's payloads are a few hundred bytes; anything larger is not ours.
_MAX_BODY_BYTES = 64 * 1024

log = logging.getLogger(__name__)


class HealthServer:
    """Minimal async HTTP server using stdlib asyncio streams.

    Parameters
    ----------
    state_machines:
        Mapping of server name → state machine (shared with IdleMonitor).
    host:
        Address to bind on.
    port:
        TCP port for the HTTP server.
    """

    def __init__(
        self,
        state_machines: dict[str, ServerStateMachine],
        host: str = "127.0.0.1",
        port: int = 8095,
        proxy_manager: ProxyManager | None = None,
        events_cfg: CraftyEventsConfig | None = None,
    ):
        self._sms = state_machines
        self._host = host
        self._port = port
        self._proxy = proxy_manager
        self._events_cfg = events_cfg
        self._server: asyncio.Server | None = None
        self._start_time = time.monotonic()

    async def run(self, shutdown: asyncio.Event) -> None:
        """Start the server, wait for shutdown, then close."""
        self._start_time = time.monotonic()
        self._server = await asyncio.start_server(
            self._handle_request,
            self._host,
            self._port,
        )
        log.info(f"Health server listening on {self._host}:{self._port}")

        await shutdown.wait()

        self._server.close()
        await self._server.wait_closed()
        log.info("Health server stopped")

    async def _handle_request(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        """Parse a minimal HTTP request and route to /health or /status."""
        try:
            request_line = await asyncio.wait_for(reader.readline(), timeout=5)
            if not request_line:
                return

            parts = request_line.decode("utf-8", errors="replace").strip().split()
            if len(parts) < 2:
                self._send_response(writer, HTTPStatus.BAD_REQUEST, "Bad Request")
                return

            method, path = parts[0], parts[1]

            headers: dict[str, str] = {}
            while True:
                line = await asyncio.wait_for(reader.readline(), timeout=5)
                if line in (b"\r\n", b"\n", b""):
                    break
                key, _, value = line.decode("utf-8", errors="replace").partition(":")
                headers[key.strip().lower()] = value.strip()

            if method == "POST":
                await self._handle_post(path, headers, reader, writer)
            elif method != "GET":
                self._send_response(writer, HTTPStatus.METHOD_NOT_ALLOWED, "Method Not Allowed")
            elif path == "/health":
                self._send_response(writer, HTTPStatus.OK, "OK")
            elif path == "/status":
                body = self._build_status_json()
                self._send_json(writer, HTTPStatus.OK, body)
            elif path == "/metrics":
                body = self._build_metrics()
                self._send_plain(writer, HTTPStatus.OK, body)
            else:
                self._send_response(writer, HTTPStatus.NOT_FOUND, "Not Found")

        except (TimeoutError, ConnectionResetError, EOFError):
            pass
        except Exception:
            log.exception("Health server request error")
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Crafty event receiver
    # ------------------------------------------------------------------

    async def _handle_post(
        self,
        path: str,
        headers: dict[str, str],
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        """Route a POST to the Crafty event endpoint, if it is enabled."""
        route = path.partition("?")[0]
        query = path.partition("?")[2]
        cfg = self._events_cfg

        if cfg is None or not cfg.enabled or route != cfg.path:
            self._send_response(writer, HTTPStatus.NOT_FOUND, "Not Found")
            return

        if cfg.token and not self._token_matches(cfg.token, query, headers):
            log.warning(f"Crafty event on {route} rejected: bad or missing token")
            self._send_response(writer, HTTPStatus.FORBIDDEN, "Forbidden")
            return

        body = await self._read_body(headers, reader)
        result = await self._process_crafty_event(body)
        self._send_json(writer, HTTPStatus.OK, result)

    @staticmethod
    def _token_matches(expected: str, query: str, headers: dict[str, str]) -> bool:
        """Compare the configured secret with the one the request carries.

        Crafty only lets us choose the URL, so the query string is the usual
        carrier; the header is there for anything else calling this endpoint.
        """
        supplied = parse_qs(query).get("token", [""])[0] or headers.get("x-watcher-token", "")
        return hmac.compare_digest(supplied, expected)

    @staticmethod
    async def _read_body(headers: dict[str, str], reader: asyncio.StreamReader) -> str:
        """Read the request body, bounded by Content-Length."""
        try:
            length = int(headers.get("content-length", "0"))
        except ValueError:
            length = 0
        if length <= 0:
            return ""
        if length > _MAX_BODY_BYTES:
            log.warning(f"Crafty event body too large ({length} bytes) — truncating")
            length = _MAX_BODY_BYTES
        try:
            raw = await asyncio.wait_for(reader.readexactly(length), timeout=5)
        except (TimeoutError, asyncio.IncompleteReadError):
            return ""
        return raw.decode("utf-8", errors="replace")

    async def _process_crafty_event(self, body: str) -> dict[str, Any]:
        """Act on one Crafty webhook, and describe what was done.

        Only `start_server` needs an action: it is the one event that arrives
        while the watcher is still sitting on the port the JVM is about to
        want.  Everything else is acknowledged and dropped — the reply must
        stay 2xx, because Crafty raises on a failed dispatch from inside the
        very call that started the server.
        """
        ids = {sm.cfg.crafty_server_id: name for name, sm in self._sms.items()}
        event = parse_event(body, ids)
        if event is None:
            return {"result": "unparsed"}

        name = ids.get(event.server_id)
        if name is None:
            log.warning(
                f"Crafty event for unknown server id '{event.server_id}' — check the "
                "webhook's server against the watcher config",
            )
            return {"result": "unknown_server", "server_id": event.server_id}

        if event.event != "start_server":
            log.debug(f"Crafty event '{event.event}' for '{name}': nothing to do")
            return {"result": "ignored", "server": name, "event": event.event}

        if self._proxy is None:
            log.error("Crafty start event received but no proxy manager is wired in")
            return {"result": "no_proxy_manager", "server": name}

        status = await self._proxy.release_for_start(name)
        if status != "released":
            log.info(f"Crafty start event for '{name}': {status}")
        return {"result": status, "server": name, "event": event.event}

    def _build_status_json(self) -> dict[str, Any]:
        uptime = time.monotonic() - self._start_time
        servers: dict[str, Any] = {}
        for name, sm in self._sms.items():
            servers[name] = {
                "state": sm.state.value,
                "port": sm.cfg.listen_port,
                "players_online": sm.last_known_online,
                "players_max": sm.last_known_max,
                "idle_seconds": round(sm.idle_elapsed(), 1) if sm.idle_since else None,
                "crafty_server_id": sm.cfg.crafty_server_id,
            }
        return {
            "status": "ok",
            "uptime_seconds": round(uptime, 1),
            "servers": servers,
        }

    def _build_metrics(self) -> str:
        """Generate Prometheus text exposition payload."""
        uptime = time.monotonic() - self._start_time
        start_counts = {name: sm.start_count for name, sm in self._sms.items()}
        stop_counts = {name: sm.stop_count for name, sm in self._sms.items()}
        return generate_metrics(
            state_machines=self._sms,
            uptime_seconds=uptime,
            start_count=start_counts,
            stop_count=stop_counts,
        )

    @staticmethod
    def _send_response(writer: asyncio.StreamWriter, status: HTTPStatus, body: str) -> None:
        response = (
            f"HTTP/1.1 {status.value} {status.phrase}\r\n"
            f"Content-Type: text/plain\r\n"
            f"Content-Length: {len(body)}\r\n"
            f"Connection: close\r\n"
            f"\r\n"
            f"{body}"
        )
        writer.write(response.encode("utf-8"))

    @staticmethod
    def _send_json(writer: asyncio.StreamWriter, status: HTTPStatus, data: dict) -> None:
        body = json.dumps(data, indent=2)
        response = (
            f"HTTP/1.1 {status.value} {status.phrase}\r\n"
            f"Content-Type: application/json\r\n"
            f"Content-Length: {len(body)}\r\n"
            f"Connection: close\r\n"
            f"\r\n"
            f"{body}"
        )
        writer.write(response.encode("utf-8"))

    @staticmethod
    def _send_plain(writer: asyncio.StreamWriter, status: HTTPStatus, body: str) -> None:
        encoded = body.encode("utf-8")
        header = (
            f"HTTP/1.1 {status.value} {status.phrase}\r\n"
            f"Content-Type: text/plain; version=0.0.4; charset=utf-8\r\n"
            f"Content-Length: {len(encoded)}\r\n"
            f"Connection: close\r\n"
            f"\r\n"
        )
        writer.write(header.encode("utf-8") + encoded)
