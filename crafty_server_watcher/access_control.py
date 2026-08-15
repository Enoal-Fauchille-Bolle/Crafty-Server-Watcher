"""Decide who is allowed to wake a hibernating server.

Any login attempt reaching the proxy starts the real Minecraft server.
Internet-wide scanners find open Minecraft ports within hours, so an
unguarded watcher hands strangers a button that boots a JVM and holds
gigabytes of RAM — even when the server's own whitelist then refuses
them at the door.

This module moves that refusal one step earlier, before anything is
started.  The default source of truth is the server's existing
``whitelist.json``: the list is already maintained through Crafty, and
reusing it means there is no second list to keep in sync.

Known limitation
----------------
The player name arrives in the Login Start packet, before Mojang
authentication has happened — it is a claim, not a proof.  Someone who
knows a whitelisted name can still trigger a wake-up.  This stops
scanners trying arbitrary names; it is not a security boundary.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from .config import AccessConfig

log = logging.getLogger(__name__)


class AccessController:
    """Answers "may this player wake the server?" for one server.

    Parameters
    ----------
    server_name:
        Watcher-side name, used only for log messages.
    cfg:
        Per-server access settings.
    """

    def __init__(self, server_name: str, cfg: AccessConfig):
        self._server = server_name
        self._cfg = cfg
        self._path = Path(cfg.whitelist_file) if cfg.whitelist_file else None
        self._names: set[str] = set()
        self._mtime: float | None = None
        self._extra = {n.casefold() for n in cfg.allowed_players}
        # Remember the last failure so a permanently missing file logs once
        # per change, not once per connection attempt.
        self._last_error: str | None = None

    def update(self, cfg: AccessConfig) -> None:
        """Apply a reloaded config (SIGHUP), dropping the cached list."""
        self._cfg = cfg
        self._path = Path(cfg.whitelist_file) if cfg.whitelist_file else None
        self._extra = {n.casefold() for n in cfg.allowed_players}
        self._names = set()
        self._mtime = None
        self._last_error = None

    @property
    def enabled(self) -> bool:
        """True if this controller actually filters anything."""
        return self._cfg.mode == "whitelist"

    def is_allowed(self, player_name: str) -> bool:
        """True if *player_name* may trigger a server start.

        Fails **open**: when the whitelist cannot be read, every player is
        allowed and the problem is logged.  A broken bind mount should
        degrade to the previous behaviour, not lock everyone — including
        the owner — out of their own servers.
        """
        if not self.enabled:
            return True

        wanted = player_name.casefold()
        if wanted in self._extra:
            return True

        allowed = self._load_names()
        if allowed is None:
            return True  # fail-open, already logged by _load_names()

        return wanted in allowed

    def _load_names(self) -> set[str] | None:
        """Return the whitelisted names, or None if unreadable.

        Re-reads the file only when its mtime changed, so adding a friend
        with ``/whitelist add`` takes effect without restarting anything.
        """
        if self._path is None:
            self._log_problem("no whitelist_file configured")
            return None

        try:
            mtime = self._path.stat().st_mtime
        except OSError as exc:
            self._log_problem(f"cannot stat {self._path}: {exc}")
            return None

        if mtime == self._mtime:
            return self._names

        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            self._log_problem(f"cannot read {self._path}: {exc}")
            return None

        if not isinstance(raw, list):
            self._log_problem(f"{self._path} is not a JSON list")
            return None

        names = {
            entry["name"].casefold()
            for entry in raw
            if isinstance(entry, dict) and isinstance(entry.get("name"), str)
        }

        self._names = names
        self._mtime = mtime
        self._last_error = None
        log.info(f"Server '{self._server}': loaded {len(names)} whitelisted player(s)")
        return names

    def _log_problem(self, message: str) -> None:
        """Log a whitelist problem, but only when it is new."""
        if message == self._last_error:
            return
        self._last_error = message
        log.error(
            f"Server '{self._server}': whitelist unavailable ({message}) — "
            "allowing all wake-up requests until it is readable again",
        )
